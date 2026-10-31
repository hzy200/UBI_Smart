"""长期记忆：跨会话保留的项目事实与约定。

存放在**工作区内**的 `.agent/memory.md`，而不是宿主侧的用户目录 ——
事实是关于项目的（"这个仓库用 pytest"、"别动 legacy/"），换个工作区就该换一套。

刻意做成**显式增删**，不做 LLM 自动提炼：
自动记忆既容易积累噪声，又难以审计"这条结论是哪来的"。
原型阶段宁可少而准，需要时再讨论自动提炼。

文件是纯文本、每行一条，用户可以直接手工编辑。
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError, normalize_root
from coding_agent.sandbox.snapshots import AGENT_STATE_DIRNAME
from coding_agent.security.secrets import secret_kinds

MEMORY_RELPATH = f"{AGENT_STATE_DIRNAME}/memory.md"
# 元数据侧车：来源、时间、过期。主文件保持纯文本一行一条，用户仍可手工编辑。
MEMORY_META_RELPATH = f"{AGENT_STATE_DIRNAME}/memory.meta.json"
# 每次写入前留一份历史，支持 `agent memory --restore`。
MEMORY_BACKUP_RELPATH = f"{AGENT_STATE_DIRNAME}/memory.backups"
MAX_BACKUPS = 20
MAX_FACTS = 50
MAX_FACT_CHARS = 300
# 锁目录超过该秒数视为残留；清理后再重试一次。
LOCK_STALE_SECONDS = 60
_BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6,18}$")
# 两条事实的词语 Jaccard 相似度超过该值即视为同一件事的新旧版本。
CONFLICT_SIMILARITY = 0.55
# 介于 related 与 replace 之间：保留两条并标记 conflicts_with。
CONFLICT_RELATED = 0.40
# BM25 参数与记忆权重。
BM25_K1 = 1.2
BM25_B = 0.75
PIN_BOOST = 2.0
PRIORITY_WEIGHT = 0.1
RECENCY_HALF_LIFE_DAYS = 90.0

HEADER = """\
# 项目记忆
#
# 由 agent 维护，也可以手工编辑：每行一条事实，以 "- " 开头。
# 内容会注入每次运行的上下文，请写短句，只记稳定的项目约定。
"""

PREFIX = "- "

# 英文/标识符按词切；中文没有空格，按「单字 + 相邻双字」切，足以做重叠打分。
_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")


class MemoryError(RuntimeError):
    """记忆文件读写失败。"""


def normalize_fact(text: str) -> str:
    """压成单行：多行事实会让文件格式与解析都变复杂。"""
    return " ".join(text.split())[:MAX_FACT_CHARS]


def _tokens(text: str) -> set[str]:
    """检索用的轻量词袋：英文小写词 + 中文单字/双字。"""
    lowered = text.lower()
    tokens = set(_WORD_RE.findall(lowered))
    for run in _CJK_RUN_RE.findall(lowered):
        tokens.update(run)
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _similarity(new: str, old: str) -> float:
    """两条事实的词语 Jaccard 相似度；主语明显不同时返回 0。

    只看词袋会把「不要动 legacy/」和「不要动 vendor/」判成冲突 ——
    两者都重要。因此当两边都含 ASCII 标识符、且标识符集合互不包含时，
    认为主语不同，不合并。中文事实没有这个信号，直接按相似度判断。
    """
    new_words = set(_WORD_RE.findall(new.lower()))
    old_words = set(_WORD_RE.findall(old.lower()))
    if new_words and old_words and not (
        new_words <= old_words or old_words <= new_words
    ):
        return 0.0
    a, b = _tokens(new), _tokens(old)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def resolve_memory_root(
    settings: Any, sandbox: Any, workspace: str
) -> str:
    """记忆根目录：显式配置 > 仓库内（可信）> 沙箱家目录下（不可信）。

    不可信仓库不能把 `.agent/memory.md` 当成可信事实注入，所以必须换到
    仓库之外的路径；不同工作区再用哈希分目录，避免跨项目串味。
    Web 租户会显式配置 per-user memory_root。
    """
    configured = str(getattr(settings, "memory_root", "") or "").strip()
    if configured:
        return normalize_root(configured)
    if getattr(settings, "trust_repo_memory", True):
        return workspace
    home = sandbox.home()
    digest = hashlib.sha1(
        str(workspace).encode("utf-8")
    ).hexdigest()[:12]
    return f"{home}/.agent-memory/{digest}"


def resolve_global_memory_root(settings: Any, sandbox: Any) -> str:
    """跨工作区全局记忆根；未配置且沙箱不可用时返回空（禁用）。"""
    configured = str(
        getattr(settings, "memory_global_root", "") or ""
    ).strip()
    if configured:
        return normalize_root(configured)
    home = getattr(sandbox, "home", None)
    if not callable(home):
        return ""
    try:
        resolved = home()
    except Exception:  # noqa: BLE001 - 全局记忆是增强信息
        return ""
    return f"{resolved}/.agent-memory/global"


class LongTermMemory:
    def __init__(
        self, fs: SandboxFs, root: str, *, state_root: str | None = None
    ) -> None:
        self._fs = fs
        # `root` 保留为展示用工作区根；实际文件与锁在 state_root。
        self.root = root
        self.state_root = state_root or root
        # 最近一次 add 因冲突而被替换掉的旧事实（供 CLI/工具如实提示）
        self.last_replaced: list[str] = []
        # 最近一次 add 发现“可能相关但未自动替换”的旧事实
        self.last_related: list[str] = []
        # 最近一次 remove 删掉的事实
        self.last_removed: str = ""
        # 最近一次 load 检出的疑似密钥事实；只阻止注入，不改写用户文件
        self.last_unsafe: list[str] = []
        # 最近一次写入前生成的备份 id
        self.last_backup_id: str = ""

    @property
    def fs(self) -> SandboxFs:
        return self._fs

    def meta_weight(self, fact: str) -> float:
        return self._meta_boost(self._load_meta().get(fact))

    def meta_weights(self, facts: Iterable[str]) -> dict[str, float]:
        meta = self._load_meta()
        return {
            fact: self._meta_boost(meta.get(fact)) for fact in facts
        }

    @property
    def path(self) -> str:
        return posixpath.join(self.state_root, MEMORY_RELPATH)

    @property
    def meta_path(self) -> str:
        return posixpath.join(self.state_root, MEMORY_META_RELPATH)

    @contextmanager
    def _write_lock(self, timeout_s: float = 3.0) -> Iterator[None]:
        """跨进程写锁（沙箱内 `mkdir` 原子抢占）。

        记忆写入是「读全文 → 改一条 → 全量重写」，两个进程同时写会丢更新。
        锁要放在写入侧而不是读取侧：读取失败只会看到旧内容，不会破坏文件。

        拿不到锁时**仍然继续写**：记忆是辅助能力，不能因为一个残留锁把
        `/remember` 永久卡死。这个降级窗口很小，且写入仍是完整的。

        这里只用 `SandboxFs` 的目录能力（`mkdir` / `mkdir_exclusive` /
        `rmdir` / `stat`），不再拼 shell：早先实现直接取 `self._fs._sandbox`
        跑 `mkdir`/`rmdir` 脚本，等于在 A4 之外又开了一条命令通道（TD-034）。
        """
        mkdir_exclusive = getattr(self._fs, "mkdir_exclusive", None)
        if not callable(mkdir_exclusive):
            yield  # 测试用的假 fs：单进程，无竞争
            return
        lock_dir = posixpath.join(
            self.state_root, AGENT_STATE_DIRNAME, "memory.lock.d"
        )
        try:
            # 首次写入时 `.agent/` 可能还不存在；`mkdir` 是 -p 语义，
            # 只负责保证父目录在，互斥仍靠下面的原子 mkdir。
            self._fs.mkdir(posixpath.dirname(lock_dir))
        except (SandboxFsError, SandboxPathError):
            yield
            return
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                acquired = mkdir_exclusive(lock_dir)
            except (SandboxFsError, SandboxPathError):
                acquired = False
            if acquired:
                break
            self._clear_stale_lock(lock_dir)
            time.sleep(0.05)
        else:
            yield
            return
        try:
            yield
        finally:
            try:
                self._fs.rmdir(lock_dir)
            except (SandboxFsError, SandboxPathError):
                pass

    def _clear_stale_lock(self, lock_dir: str) -> None:
        """进程崩溃会留下锁目录；只有超过阈值才清，且只动这一个固定路径。"""
        try:
            info = self._fs.stat(lock_dir)
        except (SandboxFsError, SandboxPathError):
            return
        if not info.exists:
            return
        age = time.time() - float(getattr(info, "mtime", 0.0) or 0.0)
        if age <= LOCK_STALE_SECONDS:
            return
        try:
            self._fs.rmdir(lock_dir)
        except (SandboxFsError, SandboxPathError):
            pass

    # ------------------------------------------------------------------

    def _load_meta(self) -> dict[str, dict[str, object]]:
        try:
            raw = self._fs.read_text(self.meta_path, max_bytes=1_000_000)
        except (SandboxFsError, SandboxPathError):
            return {}
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}  # 手工改坏元数据不该让记忆整体不可用
        facts = parsed.get("facts") if isinstance(parsed, dict) else None
        return facts if isinstance(facts, dict) else {}

    def _save_meta(self, meta: dict[str, dict[str, object]]) -> None:
        content = json.dumps(
            {"version": 1, "facts": meta},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        try:
            self._fs.write_text(self.meta_path, content)
        except (SandboxFsError, SandboxPathError):
            # 元数据是增强信息：写失败不能把一次成功的记忆写入变成失败
            pass

    def load(self) -> list[str]:
        try:
            raw = self._fs.read_text(self.path, max_bytes=1_000_000)
        except (SandboxFsError, SandboxPathError):
            return []  # 文件不存在就是「还没有记忆」
        facts = self._parse(raw)
        meta = self._load_meta()
        now = _now_iso()
        alive: list[str] = []
        for fact in facts:
            expires = str((meta.get(fact) or {}).get("expires_at") or "")
            if expires and expires <= now:
                continue  # 已过期：不再注入，也不参与检索
            alive.append(fact)
        # 手工编辑可能把密钥写进 memory.md；读取时再扫一次，只阻止注入，
        # 不擅自删除用户文件内容。
        self.last_unsafe = [
            fact for fact in alive if secret_kinds(fact)
        ]
        return alive

    @staticmethod
    def _parse(raw: str) -> list[str]:
        facts: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(PREFIX):
                fact = stripped[len(PREFIX) :].strip()
                if fact:
                    facts.append(fact)
        return facts

    def _backup(self) -> None:
        """写入前留一份 memory.md/meta 的原始副本。"""
        try:
            raw = self._fs.read_text(self.path, max_bytes=1_000_000)
        except (SandboxFsError, SandboxPathError):
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        backup_id = stamp
        suffix = 0
        while True:
            base = posixpath.join(
                self.state_root, MEMORY_BACKUP_RELPATH, backup_id
            )
            try:
                self._fs.read_text(base + ".md", max_bytes=1_000_000)
            except (SandboxFsError, SandboxPathError):
                break
            suffix += 1
            backup_id = f"{stamp}{suffix:02d}"
        try:
            self._fs.write_text(base + ".md", raw)
            try:
                meta_raw = self._fs.read_text(
                    self.meta_path, max_bytes=1_000_000
                )
            except (SandboxFsError, SandboxPathError):
                meta_raw = ""
            if meta_raw:
                self._fs.write_text(base + ".meta.json", meta_raw)
        except (SandboxFsError, SandboxPathError):
            return
        self.last_backup_id = backup_id
        self._prune_backups()

    def _backup_ids(self, limit: int = MAX_BACKUPS) -> list[str]:
        list_dir = getattr(self._fs, "list_dir", None)
        if not callable(list_dir):
            return []
        backup_dir = posixpath.join(
            self.state_root, MEMORY_BACKUP_RELPATH
        )
        try:
            entries = list_dir(
                backup_dir, max_depth=1, limit=max(limit * 3, 60)
            )
        except (SandboxFsError, SandboxPathError):
            return []
        ids = {
            rel[:-3]
            for kind, rel in entries
            if kind == "f" and rel.endswith(".md")
        }
        return sorted(ids, reverse=True)[:limit]

    def _prune_backups(self) -> None:
        remove = getattr(self._fs, "remove", None)
        if not callable(remove):
            return
        for backup_id in self._backup_ids(limit=10_000)[MAX_BACKUPS:]:
            for suffix in (".md", ".meta.json"):
                try:
                    remove(
                        posixpath.join(
                            MEMORY_BACKUP_RELPATH, backup_id + suffix
                        ),
                        known_file=True,
                    )
                except (SandboxFsError, SandboxPathError):
                    pass

    def history(self, limit: int = 20) -> list[dict[str, str]]:
        """最近的记忆备份（新的在前）。"""
        result: list[dict[str, str]] = []
        for backup_id in self._backup_ids(limit=max(1, limit)):
            created = f"{backup_id[:4]}-{backup_id[4:6]}-{backup_id[6:8]}T"
            created += f"{backup_id[9:11]}:{backup_id[11:13]}:{backup_id[13:15]}"
            result.append(
                {"backup_id": backup_id, "created_at": created}
            )
        return result

    def restore(self, backup_id: str) -> list[str]:
        """恢复到指定备份；恢复前会先把当前状态再备份一次。"""
        if not _BACKUP_ID_RE.fullmatch(str(backup_id or "")):
            raise MemoryError(f"非法的备份 id：{backup_id}")
        base = posixpath.join(
            self.state_root, MEMORY_BACKUP_RELPATH, backup_id
        )
        try:
            raw = self._fs.read_text(base + ".md", max_bytes=1_000_000)
        except (SandboxFsError, SandboxPathError) as exc:
            raise MemoryError(f"备份不存在或不可读：{backup_id}") from exc
        try:
            meta_raw = self._fs.read_text(
                base + ".meta.json", max_bytes=1_000_000
            )
        except (SandboxFsError, SandboxPathError):
            meta_raw = ""

        with self._write_lock():
            self._backup()
            try:
                self._fs.write_text(self.path, raw)
                if meta_raw:
                    self._fs.write_text(self.meta_path, meta_raw)
                else:
                    remove = getattr(self._fs, "remove", None)
                    if callable(remove):
                        try:
                            remove(self.meta_path, known_file=True)
                        except (SandboxFsError, SandboxPathError):
                            pass
            except (SandboxFsError, SandboxPathError) as exc:
                raise MemoryError(f"恢复记忆失败：{exc}") from exc
        return self.load()

    def _save(
        self,
        facts: list[str],
        meta: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._backup()
        body = "\n".join(f"{PREFIX}{fact}" for fact in facts)
        content = f"{HEADER}\n{body}\n" if body else f"{HEADER}\n"
        try:
            self._fs.write_text(self.path, content)
        except (SandboxFsError, SandboxPathError) as exc:
            raise MemoryError(f"记忆文件写入失败：{exc}") from exc
        if meta is not None:
            # 只保留仍然存在的事实，过期/删除的条目不会无限堆积
            self._save_meta({fact: meta[fact] for fact in facts if fact in meta})

    # ------------------------------------------------------------------

    def add(
        self,
        text: str,
        *,
        source: str = "user",
        ttl_days: int | None = None,
        reason: str = "",
        thread_id: str = "",
        run_id: str = "",
    ) -> list[str]:
        fact = normalize_fact(text)
        if not fact:
            raise MemoryError("记忆内容不能为空。")
        # 记忆会被注入后续每次运行的 system prompt；写入密钥等于主动外泄
        kinds = secret_kinds(fact)
        if kinds:
            raise MemoryError(
                f"记忆内容疑似包含密钥（{', '.join(kinds)}），已拒绝写入。"
                "请删除密钥后只记录不含敏感值的约定。"
            )
        self.last_replaced = []
        self.last_related = []

        with self._write_lock():
            facts = self.load()
            if fact in facts:
                # 幂等：不新增行，但刷新时间与来源；重复写入通常会带新 TTL。
                meta = self._load_meta()
                entry = dict(meta.get(fact) or {})
                now = _now_iso()
                entry.setdefault("created_at", now)
                entry.update(
                    {
                        "source": source,
                        "updated_at": now,
                        "reason": reason or str(entry.get("reason") or ""),
                        "thread_id": thread_id
                        or str(entry.get("thread_id") or ""),
                        "run_id": run_id or str(entry.get("run_id") or ""),
                    }
                )
                if ttl_days is not None and ttl_days > 0:
                    entry["expires_at"] = (
                        datetime.now(UTC) + timedelta(days=ttl_days)
                    ).isoformat(timespec="seconds")
                meta[fact] = entry
                self._save(facts, meta)
                return facts

            meta = self._load_meta()
            similarities = [
                (old, _similarity(fact, old)) for old in facts
            ]
            conflicts = [
                old
                for old, score in similarities
                if score >= CONFLICT_SIMILARITY
            ]
            related = [
                old
                for old, score in similarities
                if CONFLICT_RELATED <= score < CONFLICT_SIMILARITY
            ]
            for old in conflicts:
                facts.remove(old)
                meta.pop(old, None)
            self.last_replaced = conflicts
            self.last_related = related

            if len(facts) >= MAX_FACTS:
                raise MemoryError(
                    f"记忆条数已达上限 {MAX_FACTS}。"
                    "请先删除不再适用的条目（/forget）。"
                )

            now = _now_iso()
            entry: dict[str, object] = {
                "source": source,
                "reason": reason,
                "thread_id": thread_id,
                "run_id": run_id,
                "created_at": now,
                "updated_at": now,
                "pinned": False,
                "priority": 0,
            }
            if conflicts:
                entry["replaces"] = conflicts
            if related:
                entry["conflicts_with"] = related
            if ttl_days is not None and ttl_days > 0:
                entry["expires_at"] = (
                    datetime.now(UTC) + timedelta(days=ttl_days)
                ).isoformat(timespec="seconds")

            facts.append(fact)
            meta[fact] = entry
            self._save(facts, meta)
            return facts

    def remove(self, index: int) -> list[str]:
        """按 1 起的序号删除。"""
        with self._write_lock():
            facts = self.load()
            if index < 1 or index > len(facts):
                raise MemoryError(
                    f"序号超出范围：{index}（当前共 {len(facts)} 条）"
                )
            removed = facts.pop(index - 1)
            self.last_removed = removed
            meta = self._load_meta()
            meta.pop(removed, None)
            self._save(facts, meta)
            return facts

    def clear(self) -> list[str]:
        with self._write_lock():
            self._save([], {})
        self.last_removed = ""
        return []

    @staticmethod
    def _entry_time(entry: dict[str, object] | None) -> datetime | None:
        raw = str((entry or {}).get("updated_at") or (entry or {}).get("created_at") or "")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _meta_boost(self, entry: dict[str, object] | None) -> float:
        """pinned / priority / 时间衰减的乘性权重。"""
        pinned = bool((entry or {}).get("pinned"))
        try:
            priority = max(-10, min(int((entry or {}).get("priority", 0) or 0), 10))
        except (TypeError, ValueError):
            priority = 0
        boost = PIN_BOOST if pinned else 1.0
        boost *= 1.0 + PRIORITY_WEIGHT * priority
        timestamp = self._entry_time(entry)
        if timestamp is not None:
            age_days = max(
                0.0, (datetime.now(UTC) - timestamp).total_seconds() / 86400.0
            )
            recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
            boost *= 0.25 + 0.75 * recency
        return max(boost, 0.05)

    def _ordered_by_weight(
        self, facts: list[str], meta: dict[str, dict[str, object]]
    ) -> list[str]:
        def key(item: tuple[int, str]) -> tuple[int, int, int]:
            index, fact = item
            entry = meta.get(fact) or {}
            try:
                priority = int(entry.get("priority", 0) or 0)
            except (TypeError, ValueError):
                priority = 0
            return (-int(bool(entry.get("pinned"))), -priority, index)

        return [fact for _, fact in sorted(enumerate(facts), key=key)]

    def search(
        self,
        query: str = "",
        limit: int = 8,
        *,
        fallback: bool = True,
    ) -> list[str]:
        """BM25 检索；`fallback=True` 时无命中回退到最早的前 `limit` 条。

        权重来自 meta：pinned、priority 与 updated_at 时间衰减。
        `limit<=0` 表示不限制；事实数不超过 limit 时保持文件顺序。
        """
        facts = self.load()
        unsafe = set(self.last_unsafe)
        if unsafe:
            # 只阻止注入，不改写用户文件；CLI/TUI 会显示跳过数量。
            facts = [fact for fact in facts if fact not in unsafe]
        if not facts:
            return []
        if limit <= 0:
            return list(facts)

        meta = self._load_meta()
        query_tokens = _tokens(query)
        if not query_tokens:
            return self._ordered_by_weight(facts, meta)[:limit]
        if len(facts) <= limit and fallback:
            return list(facts)

        docs = [_tokens(fact) for fact in facts]
        lengths = [len(tokens) for tokens in docs]
        avgdl = (sum(lengths) / len(lengths)) if lengths else 0.0
        document_freq: Counter[str] = Counter()
        for tokens in docs:
            document_freq.update(set(tokens))
        total = len(docs)

        scored: list[tuple[float, int, str]] = []
        for index, (fact, tokens) in enumerate(zip(facts, docs, strict=True)):
            if not tokens:
                continue
            tf = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                frequency = tf.get(token, 0)
                if not frequency:
                    continue
                df = document_freq[token]
                idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
                denominator = frequency + BM25_K1 * (
                    1.0 - BM25_B + BM25_B * lengths[index] / (avgdl or 1.0)
                )
                score += idf * frequency * (BM25_K1 + 1.0) / denominator
            if score > 0:
                score *= self._meta_boost(meta.get(fact))
                scored.append((-score, index, fact))

        scored.sort()
        hits = [fact for _, _, fact in scored][:limit]
        return hits or (facts[:limit] if fallback else [])

    def set_pinned(self, index: int, pinned: bool) -> list[str]:
        with self._write_lock():
            facts = self.load()
            if index < 1 or index > len(facts):
                raise MemoryError(
                    f"序号超出范围：{index}（当前共 {len(facts)} 条）"
                )
            fact = facts[index - 1]
            meta = self._load_meta()
            entry = dict(meta.get(fact) or {})
            entry["pinned"] = bool(pinned)
            entry.setdefault("updated_at", _now_iso())
            meta[fact] = entry
            self._save(facts, meta)
            return facts

    def set_priority(self, index: int, priority: int) -> list[str]:
        value = max(-10, min(int(priority), 10))
        with self._write_lock():
            facts = self.load()
            if index < 1 or index > len(facts):
                raise MemoryError(
                    f"序号超出范围：{index}（当前共 {len(facts)} 条）"
                )
            fact = facts[index - 1]
            meta = self._load_meta()
            entry = dict(meta.get(fact) or {})
            entry["priority"] = value
            entry.setdefault("updated_at", _now_iso())
            meta[fact] = entry
            self._save(facts, meta)
            return facts

    def conflicts(self) -> list[dict[str, object]]:
        """列出被标记为“可能相关/冲突”的记忆条目。"""
        meta = self._load_meta()
        result: list[dict[str, object]] = []
        for fact in self.load():
            entry = meta.get(fact) or {}
            related = entry.get("conflicts_with")
            if related:
                result.append({"fact": fact, "conflicts_with": related})
        return result

    def render(self, query: str = "", limit: int = 8) -> str:
        """给系统提示用的文本；没有记忆时返回空串。

        传入 `query` 时只注入相关的前 `limit` 条，避免每条记忆都进每一轮
        system prompt —— 记忆条数有上限，但提示词的 token 成本会累积。
        """
        facts = self.search(query, limit) if query else self.search("", limit)
        if not facts:
            return ""
        lines = "\n".join(f"- {fact}" for fact in facts)
        return f"关于这个项目的长期记忆（跨会话保留）：\n{lines}"


class LayeredMemory:
    """项目记忆 + 可选的跨工作区全局记忆。

    写入默认落项目；显式 `scope="global"` 才写全局。检索时项目结果优先，
    全局只补足空位 —— 具体项目的约定不该被通用偏好覆盖。
    """

    SCOPES = frozenset({"project", "global"})

    def __init__(
        self,
        project: LongTermMemory,
        global_memory: LongTermMemory | None = None,
    ) -> None:
        self.project = project
        self.global_memory = global_memory
        self._last_scope: LongTermMemory = project
        self.last_unsafe: list[str] = []

    def _target(self, scope: str) -> LongTermMemory:
        scope = str(scope or "project").strip().lower()
        if scope not in self.SCOPES:
            raise MemoryError(f"未知的记忆作用域：{scope}")
        if scope == "global":
            if self.global_memory is None:
                raise MemoryError("未配置全局记忆根（AGENT_MEMORY_GLOBAL_ROOT）")
            return self.global_memory
        return self.project

    @property
    def path(self) -> str:
        return self._last_scope.path

    @property
    def meta_path(self) -> str:
        return self._last_scope.meta_path

    def path_for(self, scope: str) -> str:
        return self._target(scope).path

    @property
    def last_replaced(self) -> list[str]:
        return self._last_scope.last_replaced

    @property
    def last_related(self) -> list[str]:
        return self._last_scope.last_related

    @property
    def last_removed(self) -> str:
        return self._last_scope.last_removed

    @property
    def last_backup_id(self) -> str:
        return self._last_scope.last_backup_id

    def load(self, *, scope: str | None = None) -> list[str]:
        if scope is not None:
            target = self._target(scope)
            self._last_scope = target
            facts = target.load()
            self.last_unsafe = list(target.last_unsafe)
            return facts
        project_facts = self.project.load()
        global_facts = (
            self.global_memory.load() if self.global_memory is not None else []
        )
        seen = set(project_facts)
        merged = [fact for fact in global_facts if fact not in seen]
        merged.extend(project_facts)
        self.last_unsafe = list(
            dict.fromkeys(
                [
                    *self.project.last_unsafe,
                    *(self.global_memory.last_unsafe if self.global_memory else []),
                ]
            )
        )
        return merged

    def search(self, query: str = "", limit: int = 8) -> list[str]:
        project_hits = self.project.search(query, limit, fallback=False)
        global_hits = (
            self.global_memory.search(query, limit, fallback=False)
            if self.global_memory is not None
            else []
        )
        self.last_unsafe = list(
            dict.fromkeys(
                [
                    *self.project.last_unsafe,
                    *(self.global_memory.last_unsafe if self.global_memory else []),
                ]
            )
        )
        if limit <= 0:
            return list(dict.fromkeys([*project_hits, *global_hits]))
        merged: list[str] = []
        for fact in [*project_hits, *global_hits]:
            if fact not in merged:
                merged.append(fact)
            if len(merged) >= limit:
                break
        return merged or self.load()[:limit]

    def add(
        self,
        text: str,
        *,
        source: str = "user",
        scope: str = "project",
        ttl_days: int | None = None,
        reason: str = "",
        thread_id: str = "",
        run_id: str = "",
    ) -> list[str]:
        target = self._target(scope)
        self._last_scope = target
        return target.add(
            text,
            source=source,
            ttl_days=ttl_days,
            reason=reason,
            thread_id=thread_id,
            run_id=run_id,
        )

    def remove(self, index: int, *, scope: str = "project") -> list[str]:
        target = self._target(scope)
        self._last_scope = target
        return target.remove(index)

    def clear(self, *, scope: str = "project") -> list[str]:
        target = self._target(scope)
        self._last_scope = target
        return target.clear()

    def history(
        self, limit: int = 20, *, scope: str = "project"
    ) -> list[dict[str, str]]:
        return self._target(scope).history(limit)

    def restore(
        self, backup_id: str, *, scope: str = "project"
    ) -> list[str]:
        target = self._target(scope)
        self._last_scope = target
        return target.restore(backup_id)

    def set_pinned(
        self, index: int, pinned: bool, *, scope: str = "project"
    ) -> list[str]:
        target = self._target(scope)
        self._last_scope = target
        return target.set_pinned(index, pinned)

    def set_priority(
        self, index: int, priority: int, *, scope: str = "project"
    ) -> list[str]:
        target = self._target(scope)
        self._last_scope = target
        return target.set_priority(index, priority)

    def conflicts(
        self, *, scope: str | None = None
    ) -> list[dict[str, object]]:
        if scope is not None:
            return self._target(scope).conflicts()
        result = [
            {**item, "scope": "project"}
            for item in self.project.conflicts()
        ]
        if self.global_memory is not None:
            for item in self.global_memory.conflicts():
                result.append({**item, "scope": "global"})
        return result
