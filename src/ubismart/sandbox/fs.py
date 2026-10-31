"""沙箱文件系统操作。

所有读写都在沙箱内执行（复用 WslSandbox），内容经 base64 传输，
保证字节精确、不受引号/换行/编码影响。

**双重路径校验**（这是「路径安全」的关键）：

1. 词法校验：`ensure_inside` 归一化后必须在工作区内，挡住 `..`、`~`、越界绝对路径。
2. 真实路径校验：在沙箱内 `realpath` 解析符号链接，确认解析结果仍在工作区内。

只做第 1 步是不够的 —— 攻击面是「模型用 shell 建一个指向 /etc 的符号链接，
再让文件工具去读写它」。第 2 步专门堵这个洞。
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Iterable, Mapping, MutableMapping

from ubismart.sandbox.fs_scripts import (
    WRITE_RETRY_ATTEMPTS as _WRITE_RETRY_ATTEMPTS,
)
from ubismart.sandbox.fs_scripts import (
    WRITE_RETRY_DELAYS as _WRITE_RETRY_DELAYS,
)
from ubismart.sandbox.fs_scripts import (
    FileStat,
    SandboxFsError,
    SandboxFsTooLarge,
    parse_stat,
    stat_script,
)
from ubismart.sandbox.fs_scripts import (
    is_retryable_write_failure as _is_retryable_write_failure,
)
from ubismart.sandbox.fs_scripts import (
    parse_kv as _parse_kv,
)
from ubismart.sandbox.fs_scripts import (
    quote as _quote,
)
from ubismart.sandbox.fs_scripts import (
    wrap as _wrap,
)
from ubismart.sandbox.pathguard import (
    SandboxPathError,
    ensure_inside,
    is_within,
)
from ubismart.sandbox.wsl_exec import WslSandbox

# `collect_sources` 的返回标记：脚本最后一行是 `标记 + json`。
SOURCES_MARKER = "AGENT_INDEX_JSON:"


class SandboxFs:
    """工作区内的文件读写。"""

    def __init__(
        self,
        sandbox: WslSandbox,
        root: str,
        *,
        cache: MutableMapping[str, bytes] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self.root = root
        self._real_root: str | None = None
        # run 级只读内容缓存：并发只读 worker 共享同一份，避免重复读同一文件。
        # 写路径会主动失效；实现 worker 使用独立 worktree + 独立缓存。
        self._cache = cache

    # ------------------------------------------------------------------
    # 路径校验
    # ------------------------------------------------------------------

    @property
    def real_root(self) -> str:
        """工作区的规范路径（工作区自身也可能是符号链接）。"""
        if self._real_root is None:
            result = self._sandbox.run(f"realpath -m -- {_quote(self.root)}")
            if not result.ok or not result.stdout.strip():
                raise SandboxFsError(f"无法解析工作区路径：{result.render(500)}")
            self._real_root = result.stdout.strip()
        return self._real_root

    def _stat_script(
        self,
        path: str,
        *,
        include_data: bool = False,
        max_bytes: int | None = None,
    ) -> str:
        return stat_script(path, include_data=include_data, max_bytes=max_bytes)

    @staticmethod
    def _parse_stat(stdout: str) -> tuple[FileStat, bytes | None]:
        return parse_stat(stdout)

    def stat(self, path: str) -> FileStat:
        """查询文件状态；不存在的路径返回其「若创建会是」的规范路径。"""
        result = self._sandbox.run(self._stat_script(path))
        if not result.ok:
            raise SandboxFsError(f"无法读取文件状态：{result.render(500)}")
        return self._parse_stat(result.stdout)[0]

    def _check_real_path(self, lexical: str, info: FileStat) -> None:
        """把 realpath 校验单独抽出来，好让读取路径复用它而不多跑一趟。"""
        if not info.real_path:
            raise SandboxFsError(f"无法解析真实路径：{lexical}")
        if not is_within(info.real_path, self.real_root):
            raise SandboxPathError(
                f"路径经符号链接逃出工作区：{lexical} → {info.real_path}"
                f"（工作区 {self.real_root}）"
            )

    def probe(self, path: str) -> tuple[str, FileStat]:
        """一次调用里完成路径校验 + 状态查询。

        调用方几乎总是既要路径又要状态（"存在吗、是文件吗、多大"），
        拆成 `resolve()` + `stat()` 就是两次 `wsl.exe` 进程启动。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        info = self.stat(lexical)
        self._check_real_path(lexical, info)
        return lexical, info

    def lexical(self, path: str) -> str:
        """只做词法归一化，**零 I/O**。

        相对路径按工作区根展开 —— 模型习惯给 `src/a.py` 这样的相对路径。

        与 `resolve()` 的区别：`resolve` 还要在沙箱内 `realpath` 校验符号链接，
        那是一次 `wsl.exe` 进程启动。而紧随其后的 `read_text` / `read_bytes`
        本来就会做同一套校验（见 `read_bytes`），于是「先 resolve 再读」等于
        为同一次读付两次启动。只要接着就要读/写，用它就够了。
        """
        return ensure_inside(path, self.root, cwd=self.root)

    def resolve(self, path: str) -> str:
        """返回通过双重校验的可用路径。

        相对路径按工作区根展开 —— 模型习惯给 `src/a.py` 这样的相对路径。

        只在**不接着读写**时才该用它（例如只要一个展示用路径）。
        """
        return self.probe(path)[0]

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def invalidate(self, path: str) -> None:
        """丢掉某个路径的读取缓存，让下一次读取真正回到磁盘。"""
        if self._cache is None:
            return
        try:
            lexical = ensure_inside(path, self.root, cwd=self.root)
        except SandboxPathError:
            return
        self._cache.pop(lexical, None)

    def cached_bytes(self, path: str) -> bytes | None:
        """返回模型最近一次读取到的内容（没有缓存则 None）。

        写路径用它做乐观并发检查：模型读到写之间，文件可能被外部进程改过。
        """
        if self._cache is None:
            return None
        try:
            lexical = ensure_inside(path, self.root, cwd=self.root)
        except SandboxPathError:
            return None
        return self._cache.get(lexical)

    def read_bytes(
        self, path: str, *, max_bytes: int, use_cache: bool = True
    ) -> bytes:
        """读文件内容：**一次**进程启动里完成路径校验 + 状态 + 读取。

        `max_bytes` 会传进脚本，超限时脚本根本不读内容（见 `_stat_script`）。
        `use_cache=False` 用于写前复核：必须拿到磁盘上的当前内容，不能吃缓存。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        cached = (
            self._cache.get(lexical)
            if use_cache and self._cache is not None
            else None
        )
        if cached is not None:
            if len(cached) > max_bytes:
                raise SandboxFsTooLarge(
                    f"文件过大：{len(cached)} 字节（本次操作上限 {max_bytes} 字节）"
                )
            return cached
        result = self._sandbox.run(
            self._stat_script(lexical, include_data=True, max_bytes=max_bytes)
        )
        if not result.ok:
            raise SandboxFsError(f"读取失败：{result.render(500)}")

        info, data = self._parse_stat(result.stdout)
        self._check_real_path(lexical, info)
        if not info.exists:
            raise SandboxFsError(f"文件不存在：{lexical}")
        if not info.is_file:
            raise SandboxFsError(f"不是普通文件：{lexical}")
        if info.size > max_bytes:
            # 只说事实、不说办法：调用方才知道自己有什么退路（file_read 有
            # offset/limit，file_edit 没有 —— 曾经统一写「请用 offset/limit
            # 分段读取」，把改不动大文件的模型直接推向 shell）。
            # 类型本身携带「这是大小问题」，退路由调用方各自补。
            raise SandboxFsTooLarge(
                f"文件过大：{info.size} 字节（本次操作上限 {max_bytes} 字节）"
            )
        if data is None:
            raise SandboxFsError(f"读取失败：没有取到内容（{lexical}）")
        # `use_cache=False` 是写前复核：不能把“模型没读过的当前内容”写回缓存，
        # 否则陈旧读取检查会被悄悄绕过。
        if self._cache is not None and use_cache:
            self._cache[lexical] = data
        return data

    def read_text(
        self, path: str, *, max_bytes: int, use_cache: bool = True
    ) -> str:
        """读取文本；非 UTF-8 或含 NUL 视为二进制并拒绝。"""
        data = self.read_bytes(
            path, max_bytes=max_bytes, use_cache=use_cache
        )
        if b"\x00" in data:
            raise SandboxFsError(f"二进制文件（含 NUL 字节），不支持读取：{path}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxFsError(f"非 UTF-8 文本，不支持读取：{path}（{exc.reason}）") from exc

    def read_for_update(
        self, path: str, *, max_bytes: int
    ) -> tuple[str, FileStat, str | None]:
        """写前读取：一次进程启动拿到 realpath / 类型 / 大小 / 当前内容。

        与 `read_text` 的区别：文件不存在不是异常，而是返回 `(path, info, None)`，
        让调用方决定“新建”还是“报错”。`use_cache=False` 语义：读的是磁盘当前
        内容，不会把模型没读过的版本写回缓存。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        result = self._sandbox.run(
            self._stat_script(
                lexical, include_data=True, max_bytes=max_bytes
            )
        )
        if not result.ok:
            raise SandboxFsError(f"读取失败：{result.render(500)}")
        info, data = self._parse_stat(result.stdout)
        self._check_real_path(lexical, info)
        if not info.exists:
            return lexical, info, None
        if not info.is_file:
            raise SandboxFsError(f"不是普通文件：{lexical}")
        if info.size > max_bytes:
            raise SandboxFsTooLarge(
                f"文件过大：{info.size} 字节（本次操作上限 {max_bytes} 字节）"
            )
        if data is None:
            raise SandboxFsError(f"读取失败：没有取到内容（{lexical}）")
        if b"\x00" in data:
            raise SandboxFsError(
                f"二进制文件（含 NUL 字节），不支持读取：{path}"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxFsError(
                f"非 UTF-8 文本，不支持读取：{path}（{exc.reason}）"
            ) from exc
        return lexical, info, text

    def write_text(self, path: str, content: str) -> int:
        """写入文本（覆盖或新建），自动创建父目录。返回写入字节数。"""
        return self.write_bytes(path, content.encode("utf-8"))

    def write_bytes(self, path: str, data: bytes) -> int:
        """原子写入字节（上传文件使用），自动创建父目录。返回写入字节数。

        校验与写入**合在一次进程启动里**：脚本自己先解析 realpath、
        确认仍在工作区内，再落盘。校验发生在写入之前，所以越界时一个字节都不会写。
        词法校验留在 Python 侧（不需要 I/O），realpath 校验由脚本执行。

        存在性判定用 `-e || -L`：悬空符号链接的 `-e` 为假，只判 `-e` 会跳过校验，
        再顺着链接把内容写到工作区外。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        if self._cache is not None:
            self._cache.pop(lexical, None)
        payload = base64.b64encode(data).decode("ascii")
        # 原子写：先写同目录临时文件，再 `mv` 覆盖目标。
        # 直接 `> "$p"` 会在进程被杀/磁盘满时留下半截文件；临时文件 + rename
        # 保证读者看到的要么是旧内容，要么是完整的新内容。
        # 目标本身是符号链接时写到 realpath，保留“跟随链接”的既有语义。
        script = f"""\
p={_quote(lexical)}
root={_quote(self.real_root)}
target="$p"
if [ -e "$p" ] || [ -L "$p" ]; then
  real_p=$(realpath -m -- "$p")
  case "$real_p" in
    "$root"|"$root"/*) ;;
    *) echo "ESCAPED=$real_p" >&2; exit 9 ;;
  esac
  target="$real_p"
else
  real_dir=$(realpath -m -- "$(dirname -- "$p")")
  case "$real_dir" in
    "$root"|"$root"/*) ;;
    *) echo "ESCAPED=$real_dir" >&2; exit 9 ;;
  esac
fi
real_dir=$(dirname -- "$target")
mkdir -p -- "$real_dir" || exit 10
tmp="$target.agent-tmp.$$"
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
base64 -d > "$tmp" <<'__AGENT_PAYLOAD__'
{_wrap(payload)}
__AGENT_PAYLOAD__
if [ $? -ne 0 ]; then exit 11; fi
# 覆盖已有文件时沿用原权限位（原子 replace 不会自动保留）。
if [ -e "$target" ]; then chmod --reference="$target" "$tmp" 2>/dev/null || true; fi
mv -f -- "$tmp" "$target" || exit 12
trap - EXIT HUP INT TERM
"""
        result = self._sandbox.run(script)
        attempt = 0
        while (
            not result.ok
            and _is_retryable_write_failure(result.stderr or "")
            and attempt < len(_WRITE_RETRY_DELAYS)
        ):
            time.sleep(_WRITE_RETRY_DELAYS[attempt])
            attempt += 1
            result = self._sandbox.run(script)
        if not result.ok:
            if "ESCAPED=" in (result.stderr or ""):
                raise SandboxPathError(
                    f"路径经符号链接逃出工作区，已拒绝写入：{lexical}"
                    f"（{result.stderr.strip()}）"
                )
            if _is_retryable_write_failure(result.stderr or ""):
                raise SandboxFsError(
                    f"写入失败：文件可能被其他进程占用（已重试 "
                    f"{_WRITE_RETRY_ATTEMPTS} 次）：{result.render(500)}"
                )
            raise SandboxFsError(f"写入失败：{result.render(500)}")
        return len(data)

    def write_many(self, files: dict[str, str]) -> int:
        """一次进程启动写入多个文件，返回写入的总字节数。

        为什么需要它：`write_text` 是"一个文件一次 `wsl.exe`"（每次约 0.2–0.3s），
        而几十个文件的工作区（评测任务的种子、参考解、判定前恢复测试）每次都要
        整份重写 —— 逐个写就是几十次进程启动，光物化就十几秒。

        与 `write_text` 的关系是**同一个不变量的两种粒度**，不是绕过它：
        路径校验、realpath 校验、父目录创建都在脚本里做，且**先全量校验再落盘** ——
        任何一个路径越界就一个字节都不写，不留下半成品工作区（逐个写做不到这点，
        写到第 7 个文件才发现越界时前 6 个已经落盘了）。

        内容来自宿主自己的任务定义（不是模型产出），但校验一样不能省：路径仍可能
        因为配置或任务写错而越界。
        """
        if not files:
            return 0

        # 宿主侧先做词法校验（零 I/O）：绝对路径与 `..` 在这里就拒掉，
        # 不必让沙箱去发现一个本来就能提前知道的问题。
        targets: dict[str, str] = {}
        for path, content in files.items():
            lexical = ensure_inside(path, self.root, cwd=self.root)
            targets[lexical] = content
            if self._cache is not None:
                self._cache.pop(lexical, None)

        payload = base64.b64encode(
            json.dumps(targets, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        script = f"""\
root={_quote(self.real_root)}
python3 - "$root" <<'__AGENT_WRITE_MANY__'
import base64, json, os, stat, sys, tempfile

root = os.path.realpath(sys.argv[1])
files = json.loads(base64.b64decode("__AGENT_PAYLOAD__").decode("utf-8"))

# 先全量校验，再落盘 —— 越界时一个字节都不写
def escaped(where):
    sys.stderr.write("ESCAPED=" + where + "\\n")
    raise SystemExit(9)


def inside(path):
    return path == root or path.startswith(root + os.sep)


checked = []
for path, content in files.items():
    # 父目录与目标自身都要按 realpath 校验（与 write_text 同一套判定）：
    # 只判父目录挡不住"目标本身是指向工作区外的符号链接"。
    if not inside(os.path.realpath(os.path.dirname(path))):
        escaped(os.path.realpath(os.path.dirname(path)))
    target = path
    if os.path.exists(path) or os.path.islink(path):
        if not inside(os.path.realpath(path)):
            escaped(os.path.realpath(path))
        target = os.path.realpath(path)
    checked.append((target, content))

written = 0
for target, content in checked:
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".agent-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        if os.path.exists(target):
            # 原子 replace 不保留原权限位（可执行脚本会丢 +x）。
            os.chmod(tmp, stat.S_IMODE(os.stat(target).st_mode))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    written += len(content.encode("utf-8"))
print("WROTE=%d" % written)
__AGENT_WRITE_MANY__
"""
        # 不折行：这份 payload 是内联的 **Python 字符串字面量**（不像 write_text
        # 那样喂给 `base64 -d`），折行会直接变成语法错。脚本经 stdin 传，
        # 没有命令行长度上限，一整行再长也无妨。
        script = script.replace("__AGENT_PAYLOAD__", payload)
        result = self._sandbox.run(script)
        attempt = 0
        while (
            not result.ok
            and _is_retryable_write_failure(result.stderr or "")
            and attempt < len(_WRITE_RETRY_DELAYS)
        ):
            time.sleep(_WRITE_RETRY_DELAYS[attempt])
            attempt += 1
            result = self._sandbox.run(script)
        if not result.ok:
            if "ESCAPED=" in (result.stderr or ""):
                raise SandboxPathError(
                    f"路径经符号链接逃出工作区，已拒绝写入（批量）：{result.stderr.strip()}"
                )
            if _is_retryable_write_failure(result.stderr or ""):
                raise SandboxFsError(
                    f"批量写入失败：文件可能被其他进程占用（已重试 "
                    f"{_WRITE_RETRY_ATTEMPTS} 次）：{result.render(500)}"
                )
            raise SandboxFsError(f"批量写入失败：{result.render(500)}")
        return sum(len(content.encode("utf-8")) for content in files.values())

    def remove(self, path: str, *, known_file: bool = False) -> bool:
        """删除工作区内的**普通文件**，返回它此前是否存在。

        `known_file=True` 表示调用方刚刚读过并确认它是普通文件（例如 move/
        delete 工具），省掉一次 `stat` 往返；realpath 校验仍由脚本执行。

        唯一用途是「回滚一次新建」——留底时那个文件还不存在，把它还原成空文件
        等于留下一堆垃圾，还原成「不存在」才是真的回到原状。因此这里只删文件：
        目录、符号链接一律拒绝（符号链接可能是逃逸通道，删它也可能删到工作区外）。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        if self._cache is not None:
            self._cache.pop(lexical, None)
        if not known_file:
            # 先探一次：既拿到状态（存在吗、是文件吗），也完成 realpath 校验
            info = self.stat(lexical)
            self._check_real_path(lexical, info)
            if not info.exists:
                return False
            if not info.is_file:
                raise SandboxFsError(f"不是普通文件，拒绝删除：{lexical}")

        script = f"""\
p={_quote(lexical)}
root={_quote(self.real_root)}
if [ -e "$p" ] || [ -L "$p" ]; then
  real_p=$(realpath -m -- "$p")
  case "$real_p" in
    "$root"|"$root"/*) ;;
    *) echo "ESCAPED=$real_p" >&2; exit 9 ;;
  esac
fi
rm -f -- "$p"
"""
        result = self._sandbox.run(script)
        if not result.ok:
            if "ESCAPED=" in (result.stderr or ""):
                raise SandboxPathError(
                    f"路径经符号链接逃出工作区，已拒绝删除：{lexical}"
                    f"（{result.stderr.strip()}）"
                )
            raise SandboxFsError(f"删除失败：{result.render(500)}")
        return True

    def mkdir(self, path: str) -> str:
        """创建目录（含父目录），返回 realpath；目标是文件时拒绝。"""
        lexical = ensure_inside(path, self.root, cwd=self.root)
        script = f"""\
p={_quote(lexical)}
root={_quote(self.real_root)}
parent=$(dirname -- "$p")
real_parent=$(realpath -m -- "$parent")
case "$real_parent" in
  "$root"|"$root"/*) ;;
  *) echo "ESCAPED=$real_parent" >&2; exit 9 ;;
esac
if [ -e "$p" ] || [ -L "$p" ]; then
  real_p=$(realpath -m -- "$p")
  case "$real_p" in
    "$root"|"$root"/*) ;;
    *) echo "ESCAPED=$real_p" >&2; exit 9 ;;
  esac
  if [ ! -d "$real_p" ]; then echo "NOTDIR=$real_p" >&2; exit 10; fi
else
  mkdir -p -- "$p" || exit 11
  real_p=$(realpath -m -- "$p")
fi
echo "REAL=$real_p"
"""
        result = self._sandbox.run(script)
        if not result.ok:
            if "ESCAPED=" in (result.stderr or ""):
                raise SandboxPathError(
                    f"路径经符号链接逃出工作区，已拒绝创建目录：{lexical}"
                )
            if "NOTDIR=" in (result.stderr or ""):
                raise SandboxFsError(f"目标已存在且不是目录：{lexical}")
            raise SandboxFsError(f"创建目录失败：{result.render(500)}")
        if self._cache is not None:
            self._cache.clear()
        lines = result.stdout.strip().splitlines()
        if not lines:
            # 脚本末尾一定会 `echo REAL=…`；没有输出说明沙箱回话被截断，
            # 不能拿空串冒充路径（旧实现会在这里抛 IndexError）。
            raise SandboxFsError(f"创建目录后未返回真实路径：{lexical}")
        return lines[-1].removeprefix("REAL=")

    def mkdir_exclusive(self, path: str) -> bool:
        """原子创建目录：已存在返回 False（不带 `-p`）。

        存在性判断就是"抢锁"的全部语义 —— `mkdir -p` 在目录已存在时也成功，
        用它拿锁等于谁都能拿到，互斥形同虚设。父目录要先由调用方用 `mkdir()`
        建好；父目录不存在时返回 False（当作没抢到）。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        script = f"""\
p={_quote(lexical)}
root={_quote(self.real_root)}
parent=$(dirname -- "$p")
real_parent=$(realpath -m -- "$parent")
case "$real_parent" in
  "$root"|"$root"/*) ;;
  *) echo "ESCAPED=$real_parent" >&2; exit 9 ;;
esac
if mkdir -- "$p" 2>/dev/null; then echo "CREATED=1"; else echo "CREATED=0"; fi
"""
        result = self._sandbox.run(script)
        if not result.ok:
            if "ESCAPED=" in (result.stderr or ""):
                raise SandboxPathError(
                    f"路径经符号链接逃出工作区，已拒绝创建目录：{lexical}"
                )
            raise SandboxFsError(f"创建目录失败：{result.render(500)}")
        return _parse_kv(result.stdout).get("CREATED") == "1"

    def rmdir(self, path: str) -> bool:
        """删除**空**目录；非目录或非空一律报错，绝不递归。

        只服务"撤销自己刚建的目录"（例如记忆写锁）。递归删除是 `rm -rf` 的
        语义，不应该出现在文件 API 里 —— 调用方要是想清空一棵树，应该用
        `list_dir` 自己走完再逐个 `remove`，让每一步都可见。
        """
        lexical = ensure_inside(path, self.root, cwd=self.root)
        script = f"""\
p={_quote(lexical)}
root={_quote(self.real_root)}
if [ -e "$p" ] || [ -L "$p" ]; then
  real_p=$(realpath -m -- "$p")
  case "$real_p" in
    "$root"|"$root"/*) ;;
    *) echo "ESCAPED=$real_p" >&2; exit 9 ;;
  esac
  if [ ! -d "$p" ] || [ -L "$p" ]; then echo "NOTDIR=$real_p" >&2; exit 10; fi
  rmdir -- "$p" 2>/dev/null || exit 12
fi
"""
        result = self._sandbox.run(script)
        if not result.ok:
            if "ESCAPED=" in (result.stderr or ""):
                raise SandboxPathError(
                    f"路径经符号链接逃出工作区，已拒绝删除目录：{lexical}"
                )
            if "NOTDIR=" in (result.stderr or ""):
                raise SandboxFsError(f"不是目录，拒绝删除：{lexical}")
            raise SandboxFsError(
                f"删除目录失败（可能非空）：{result.render(500)}"
            )
        if self._cache is not None:
            self._cache.clear()
        return True

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        """列出目录内容，返回 `(type, relative_path)` 列表（不跟随符号链接）。"""
        lexical = ensure_inside(path, self.root, cwd=self.root)
        depth = max(1, int(max_depth))
        cap = max(1, int(limit))
        script = f"""\
p={_quote(lexical)}
root={_quote(self.real_root)}
if [ ! -e "$p" ] && [ ! -L "$p" ]; then echo "MISSING=1" >&2; exit 12; fi
real_p=$(realpath -m -- "$p")
case "$real_p" in
  "$root"|"$root"/*) ;;
  *) echo "ESCAPED=$real_p" >&2; exit 9 ;;
esac
if [ ! -d "$real_p" ]; then echo "NOTDIR=$real_p" >&2; exit 10; fi
find "$real_p" -mindepth 1 -maxdepth {depth} -printf '%y\\t%P\\n' 2>/dev/null \\
  | LC_ALL=C sort | head -n {cap}
"""
        result = self._sandbox.run(script)
        if not result.ok:
            if "ESCAPED=" in (result.stderr or ""):
                raise SandboxPathError(
                    f"路径经符号链接逃出工作区，已拒绝列出：{lexical}"
                )
            if "MISSING=" in (result.stderr or ""):
                raise SandboxFsError(f"目录不存在：{lexical}")
            if "NOTDIR=" in (result.stderr or ""):
                raise SandboxFsError(f"不是目录：{lexical}")
            raise SandboxFsError(f"列目录失败：{result.render(500)}")
        entries: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            kind, sep, rel = line.partition("\t")
            if not sep or not rel:
                continue
            entries.append((kind, rel))
        return entries


    def find_files(
        self,
        names: Iterable[str],
        *,
        max_depth: int = 4,
        exclude_dirs: Iterable[str] = (),
        limit: int = 200,
    ) -> list[str]:
        """按**文件名**递归查找，返回相对 `root` 的路径（不跟随符号链接）。

        只服务"按名字找固定文件"（仓库指令文件就是典型）：调用方给的是文件名
        清单而不是任意 `find` 表达式，所以这里不会退化成通用命令通道。
        名字里带分隔符的条目直接忽略 —— 它只能用来限定 basename。
        """
        safe_names = [
            str(name)
            for name in names
            if name and "/" not in str(name) and "\\" not in str(name)
        ]
        if not safe_names:
            return []
        depth = max(1, int(max_depth))
        cap = max(1, int(limit))
        name_expr = " -o ".join(f"-name {_quote(n)}" for n in safe_names)
        excludes = " ".join(
            f'-not -path "$real_root/{d}/*"'
            for d in (str(item) for item in exclude_dirs)
            if d and "/" not in d and "\\" not in d
        )
        script = f"""\
root={_quote(self.real_root)}
if [ ! -d "$root" ]; then echo "MISSING=1" >&2; exit 12; fi
real_root=$(realpath -m -- "$root")
find "$real_root" -maxdepth {depth} -type f \\( {name_expr} \\) {excludes} \\
  -printf '%P\\n' 2>/dev/null | LC_ALL=C sort | head -n {cap}
"""
        result = self._sandbox.run(script)
        if not result.ok:
            if "MISSING=" in (result.stderr or ""):
                raise SandboxFsError(f"目录不存在：{self.real_root}")
            raise SandboxFsError(f"查找文件失败：{result.render(500)}")
        return [line for line in result.stdout.splitlines() if line]


    def collect_sources(
        self,
        suffixes: Iterable[str],
        *,
        exclude_dirs: Iterable[str] = (),
        max_files: int = 2_000,
        max_file_bytes: int = 1_000_000,
        max_total_bytes: int = 32_000_000,
    ) -> dict[str, str]:
        """一次调用收集匹配后缀的文本源码，返回 `{相对路径: 内容}`。

        只服务"读回来做静态分析"（Python AST 索引、repo map）：调用方给的是
        后缀清单和三个上限，脚本由 A4 生成。**刻意不支持传任意脚本** —— 那等于
        在 `fs` 里开一个通用执行入口，正是 TD-034 要关掉的东西。

        为什么必须一次调用：逐文件 read_text 每个文件都要起一个 `wsl.exe`
        （0.2–0.3s），两千个文件就是十分钟。`max_total_bytes` 是硬上限，内容会
        整体经 stdout 回到宿主内存，只按单文件限制仍可能物化出数百 MB。
        """
        safe_suffixes = [
            text
            for text in (str(item) for item in suffixes)
            if text.startswith(".") and text[1:].isalnum()
        ]
        if not safe_suffixes:
            return {}
        ignored = [
            text
            for text in (str(item) for item in exclude_dirs)
            if text and "/" not in text and "\\" not in text
        ]
        script = f"""\
python3 - <<'__AGENT_SOURCES__'
import json, os
suffixes = {json.dumps(safe_suffixes)}
ignored = set({json.dumps(sorted(ignored))})
out = {{}}
count = 0
total = 0
for base, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ignored]
    for name in files:
        if not name.endswith(tuple(suffixes)):
            continue
        path = os.path.relpath(os.path.join(base, name), '.')
        path = path.replace(os.sep, '/')
        try:
            size = os.path.getsize(path)
            if size > {int(max_file_bytes)}:
                continue
            if total + size > {int(max_total_bytes)}:
                break
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                out[path] = fh.read()
        except OSError:
            continue
        total += size
        count += 1
        if count >= {int(max_files)}:
            break
    if count >= {int(max_files)} or total >= {int(max_total_bytes)}:
        break
print('{SOURCES_MARKER}' + json.dumps(out))
__AGENT_SOURCES__
"""
        result = self._sandbox.run(script, cwd=self.real_root)
        if not result.ok:
            raise SandboxFsError(f"收集源码失败：{result.render(500)}")
        for line in reversed(result.stdout.splitlines()):
            if not line.startswith(SOURCES_MARKER):
                continue
            try:
                payload = json.loads(line[len(SOURCES_MARKER) :])
            except json.JSONDecodeError as exc:
                raise SandboxFsError(f"源码收集结果无法解析：{exc}") from exc
            if not isinstance(payload, dict):
                raise SandboxFsError("源码收集结果不是对象")
            return {
                str(key): str(value)
                for key, value in payload.items()
                if isinstance(value, str)
            }
        raise SandboxFsError("源码收集没有返回结果")


    def count_files(
        self,
        groups: Mapping[str, Iterable[str]],
        *,
        exclude_dirs: Iterable[str] = (),
        limit: int = 50_000,
    ) -> dict[str, int]:
        """按文件名 glob 分组统计文件数，**一次调用**拿回全部结果。

        `groups` 形如 `{"total": (), "tests": ("test_*.py", "*_test.py")}`：
        空元组表示"全部文件"。键名会拼进脚本的 `echo`，所以只接受字母数字
        下划线；模式只接受 basename（带分隔符的条目忽略），命令仍由 A4 生成。

        `limit` 是**每组**的扫描上限：估算量级不需要遍历完一个几十万文件的
        仓库，`head` 提前截断就够了（结果是 min(实际, limit)）。
        """
        safe_groups: dict[str, list[str]] = {}
        for key, patterns in groups.items():
            name = str(key)
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                continue
            safe_groups[name] = [
                text
                for text in (str(item) for item in patterns)
                if text and "/" not in text and "\\" not in text
            ]
        if not safe_groups:
            return {}
        cap = max(1, int(limit))
        excludes = " ".join(
            f'-not -path "$real_root/{d}/*"'
            for d in (str(item) for item in exclude_dirs)
            if d and "/" not in d and "\\" not in d
        )
        lines = [
            f"root={_quote(self.real_root)}",
            'real_root=$(realpath -m -- "$root")',
            'if [ ! -d "$real_root" ]; then echo "MISSING=1" >&2; exit 12; fi',
        ]
        for name, patterns in safe_groups.items():
            selector = ""
            if patterns:
                expr = " -o ".join(f"-name {_quote(item)}" for item in patterns)
                selector = f"\\( {expr} \\)"
            lines.append(
                f'c_{name}=$(find "$real_root" -type f {selector} {excludes} '
                f"-print 2>/dev/null | head -n {cap} | wc -l)"
            )
            lines.append(f'echo "{name}=$c_{name}"')
        result = self._sandbox.run("\n".join(lines) + "\n")
        if not result.ok:
            if "MISSING=" in (result.stderr or ""):
                raise SandboxFsError(f"目录不存在：{self.real_root}")
            raise SandboxFsError(f"统计文件数失败：{result.render(500)}")
        data = _parse_kv(result.stdout)
        counts: dict[str, int] = {}
        for name in safe_groups:
            raw = data.get(name, "0").strip()
            try:
                counts[name] = int(raw)
            except ValueError as exc:
                raise SandboxFsError(
                    f"统计结果不是数字：{name}={raw!r}"
                ) from exc
        return counts
