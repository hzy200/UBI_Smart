"""长期记忆的按需检索，以及暴露给模型的 memory 工具。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from coding_agent.config import Settings
from coding_agent.memory.longterm import LayeredMemory, LongTermMemory


def test_memory_state_root_can_be_outside_the_workspace() -> None:
    """不可信仓库下记忆放在仓库之外，路径必须使用 state_root。"""
    memory = LongTermMemory(
        object(), "/repo", state_root="/home/tester/agent-memory"
    )  # type: ignore[arg-type]

    assert memory.path == "/home/tester/agent-memory/.agent/memory.md"
    assert memory.meta_path == "/home/tester/agent-memory/.agent/memory.meta.json"


class _FakeFs:
    """只实现 LongTermMemory 用到的读写接口。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def read_text(self, path: str, *, max_bytes: int) -> str:
        if path not in self.files:
            from coding_agent.sandbox.fs import SandboxFsError

            raise SandboxFsError("not found")
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        prefix = path.rstrip("/") + "/"
        entries = [
            ("f", key[len(prefix) :])
            for key in self.files
            if key.startswith(prefix) and "/" not in key[len(prefix) :]
        ]
        return sorted(entries)[:limit]

    def remove(self, path: str, *, known_file: bool = False) -> bool:
        return self.files.pop(path, None) is not None


def _memory(*facts: str) -> LongTermMemory:
    fs = _FakeFs()
    memory = LongTermMemory(fs, "/work")  # type: ignore[arg-type]
    for fact in facts:
        memory.add(fact)
    return memory


FACTS = (
    "这个仓库用 pytest 跑测试",
    "不要动 legacy/ 目录",
    "依赖用 uv 管理，不要用 pip",
    "提交信息用中文",
    "验证命令是 make check",
)


# ---------------- 检索 ----------------

def test_search_ranks_by_overlap() -> None:
    memory = _memory(*FACTS)
    hits = memory.search("测试怎么跑，pytest？", limit=2)
    assert hits[0] == "这个仓库用 pytest 跑测试"


def test_search_matches_chinese_bigrams() -> None:
    """中文没有空格，靠单字/双字重叠命中。"""
    memory = _memory(*FACTS)
    hits = memory.search("依赖管理方式", limit=1)
    assert hits == ["依赖用 uv 管理，不要用 pip"]


def test_search_falls_back_to_oldest_when_nothing_matches() -> None:
    memory = _memory(*FACTS)
    hits = memory.search("完全无关的词汇 zzzz", limit=2)
    assert hits == list(FACTS[:2])


def test_search_without_query_keeps_order() -> None:
    memory = _memory(*FACTS)
    assert memory.search("", limit=3) == list(FACTS[:3])


def test_search_returns_everything_when_under_limit() -> None:
    memory = _memory(*FACTS)
    assert memory.search("pytest", limit=50) == list(FACTS)


def test_search_on_empty_memory() -> None:
    assert _memory().search("pytest") == []


def test_add_rejects_secrets() -> None:
    """记忆会进入每次运行的 system prompt，密钥不能写进去。"""
    from coding_agent.memory.longterm import MemoryError

    memory = _memory()
    with pytest.raises(MemoryError, match="密钥"):
        memory.add("api_key=sk-abcdefghijklmnopqrstuvwxyz")
    assert memory.load() == []


def test_add_records_provenance_and_ttl() -> None:
    memory = _memory()
    memory.add("用 pytest", source="model", ttl_days=7)
    meta = memory._load_meta()  # noqa: SLF001 - 元数据是本次改动的一部分
    entry = meta["用 pytest"]
    assert entry["source"] == "model"
    assert entry["created_at"]
    assert entry["expires_at"]


def test_add_replaces_conflicting_fact() -> None:
    memory = _memory()
    memory.add("提交信息使用中文")
    memory.add("提交信息使用英文")
    assert memory.load() == ["提交信息使用英文"]
    assert memory.last_replaced == ["提交信息使用中文"]


def test_render_only_includes_selected_facts() -> None:
    memory = _memory(*FACTS)
    rendered = memory.render("legacy 目录", limit=1)
    assert "legacy" in rendered
    assert "pytest" not in rendered


def test_layered_memory_project_takes_precedence() -> None:
    project_fs, global_fs = _FakeFs(), _FakeFs()
    project = LongTermMemory(project_fs, "/work")  # type: ignore[arg-type]
    global_memory = LongTermMemory(global_fs, "/global")  # type: ignore[arg-type]
    layered = LayeredMemory(project, global_memory)

    layered.add("全局约定：提交信息用中文", scope="global")
    layered.add("项目约定：测试用 pytest", scope="project")

    assert layered.load() == [
        "全局约定：提交信息用中文",
        "项目约定：测试用 pytest",
    ]
    assert layered.search("pytest", 1) == ["项目约定：测试用 pytest"]
    assert layered.search("中文", 1) == ["全局约定：提交信息用中文"]

    layered.remove(1, scope="global")
    assert layered.load(scope="global") == []
    assert layered.load(scope="project") == ["项目约定：测试用 pytest"]


def test_layered_memory_without_global_scope_rejects_writes() -> None:
    layered = LayeredMemory(
        LongTermMemory(_FakeFs(), "/work")  # type: ignore[arg-type]
    )
    with pytest.raises(Exception, match="全局记忆"):
        layered.add("全局约定", scope="global")


def test_read_time_secret_scan_blocks_injection() -> None:
    from coding_agent.memory.longterm import HEADER

    fs = _FakeFs()
    fs.files["/work/.agent/memory.md"] = (
        HEADER
        + "\n- 这个仓库用 pytest\n"
        + "- api_key=sk-abcdefghijklmnopqrstuvwxyz\n"
    )
    memory = LongTermMemory(fs, "/work")  # type: ignore[arg-type]

    assert len(memory.load()) == 2
    assert len(memory.last_unsafe) == 1
    assert memory.search("pytest", 10) == ["这个仓库用 pytest"]


def test_add_records_provenance_v2() -> None:
    memory = _memory()
    memory.add(
        "用 pytest",
        source="model",
        reason="用户确认",
        thread_id="t-1",
        run_id="r-1",
        ttl_days=7,
    )

    entry = memory._load_meta()["用 pytest"]  # noqa: SLF001
    assert entry["reason"] == "用户确认"
    assert entry["thread_id"] == "t-1"
    assert entry["run_id"] == "r-1"
    assert entry["pinned"] is False
    assert entry["priority"] == 0
    assert entry["expires_at"]


def test_idempotent_add_refreshes_metadata() -> None:
    memory = _memory()
    memory.add("用 pytest", reason="第一次")
    memory.add("用 pytest", reason="第二次", ttl_days=3)

    entry = memory._load_meta()["用 pytest"]  # noqa: SLF001
    assert entry["reason"] == "第二次"
    assert entry["expires_at"]


def test_pinned_and_priority_change_order() -> None:
    memory = _memory(*FACTS)
    memory.set_pinned(5, True)
    memory.set_priority(1, 10)

    hits = memory.search("", 2)
    assert hits == [FACTS[4], FACTS[0]]


def test_bm25_prefers_a_specific_fact() -> None:
    memory = _memory()
    memory.add("测试用 pytest")
    memory.add("这个仓库用 pytest 跑测试，并且还有一大段与问题无关的长描述")
    memory.add("依赖用 uv")

    hits = memory.search("pytest", 1)
    assert hits == ["测试用 pytest"]


def test_related_fact_is_kept_and_marked() -> None:
    memory = _memory()
    memory.add("不要改测试目录")
    memory.add("不要改配置目录")

    assert memory.load() == ["不要改测试目录", "不要改配置目录"]
    assert memory.last_replaced == []
    assert memory.last_related == ["不要改测试目录"]
    conflicts = memory.conflicts()
    assert conflicts and conflicts[0]["fact"] == "不要改配置目录"


def test_backup_history_and_restore() -> None:
    fs = _FakeFs()
    memory = LongTermMemory(fs, "/work")  # type: ignore[arg-type]
    memory.add("A")
    memory.add("B")
    backups = memory.history()
    assert backups
    first = backups[0]["backup_id"]

    memory.add("C")
    assert memory.load() == ["A", "B", "C"]

    restored = memory.restore(first)
    assert restored == ["A"]


# ---------------- memory 工具 ----------------

@pytest.fixture
def memory_tools(monkeypatch):
    from coding_agent.tools import memory as memory_tools

    fs = _FakeFs()
    monkeypatch.setattr(
        memory_tools, "SandboxFs", lambda sandbox, root, cache=None: fs
    )
    settings = Settings(
        _env_file=None, deepseek_api_key="x", wsl_workspace="/work"
    )
    sandbox = SimpleNamespace()
    return (
        memory_tools.build_memory_tools(settings, sandbox, allow_write=True),  # type: ignore[arg-type]
        fs,
    )


def test_memory_search_tool_reads_file(memory_tools) -> None:
    tools, fs = memory_tools
    fs.files["/work/.agent/memory.md"] = (
        "# 项目记忆\n- 这个仓库用 pytest 跑测试\n- 依赖用 uv\n"
    )
    search = {tool.name: tool for tool in tools}["memory_search"]
    text = search.invoke({"query": "pytest", "limit": 5, "reason": "查约定"})
    assert "pytest" in text


def test_memory_save_tool_appends_fact(memory_tools) -> None:
    tools, fs = memory_tools
    save = {tool.name: tool for tool in tools}["memory_save"]
    text = save.invoke({"fact": "别动 legacy/", "reason": "用户强调"})
    assert "已记住" in text
    assert "别动 legacy/" in fs.files["/work/.agent/memory.md"]


class _LockFs(_FakeFs):
    """带目录能力的假 fs：记录抢锁/清锁动作，不拼任何 shell。"""

    def __init__(self, *, acquire_after: int = 1, mtime_age: float = 0.0) -> None:
        super().__init__()
        self.acquire_after = acquire_after
        self.mtime_age = mtime_age
        self.attempts = 0
        self.made_dirs: list[str] = []
        self.removed_dirs: list[str] = []

    def mkdir(self, path: str) -> str:
        self.made_dirs.append(path)
        return path

    def mkdir_exclusive(self, path: str) -> bool:
        self.attempts += 1
        return self.attempts >= self.acquire_after

    def stat(self, path: str):
        from coding_agent.sandbox.fs import FileStat

        if not path.endswith("memory.lock.d"):
            return FileStat(exists=False, is_file=False, size=0, real_path=path)
        return FileStat(
            exists=True,
            is_file=False,
            size=0,
            real_path=path,
            mtime=time.time() - self.mtime_age,
        )

    def rmdir(self, path: str) -> bool:
        self.removed_dirs.append(path)
        return True


def test_stale_memory_lock_is_cleaned_up() -> None:
    """崩溃留下的锁目录超过阈值才清，且清完再抢、用完再释放。"""
    fs = _LockFs(acquire_after=3, mtime_age=3600)
    memory = LongTermMemory(fs, "/work")  # type: ignore[arg-type]
    memory.add("用 pytest")

    assert fs.attempts == 3  # 前两次抢不到，清锁后第三次成功
    # 两次失败各清一次过期锁，末尾释放再删一次 —— 三次都是同一个固定路径。
    assert fs.removed_dirs == ["/work/.agent/memory.lock.d"] * 3
    assert fs.made_dirs and fs.made_dirs[0] == "/work/.agent"


def test_fresh_lock_is_not_stolen_and_write_still_proceeds() -> None:
    """拿不到锁时降级写入，但**不能**去删别人正在用的锁。"""
    fs = _LockFs(acquire_after=10_000, mtime_age=0)
    memory = LongTermMemory(fs, "/work")  # type: ignore[arg-type]

    with memory._write_lock(timeout_s=0.02):  # noqa: SLF001
        pass

    assert fs.removed_dirs == []  # 新锁不该被清
    assert "/work/.agent/memory.md" not in fs.files  # 只是拿锁，没写内容


def test_user_memory_writes_are_audited(tmp_path) -> None:
    from coding_agent.audit import AuditLogger, read_records
    from coding_agent.memory.longterm import LayeredMemory
    from coding_agent.runtime import AgentRuntime

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._memory = LayeredMemory(  # type: ignore[assignment]
        LongTermMemory(_FakeFs(), "/work")
    )
    runtime._workspace = "/work"
    runtime.audit = AuditLogger(tmp_path)

    runtime.remember("用 pytest", thread_id="t1", reason="约定")
    runtime.forget(1, thread_id="t1")

    records = read_records(AuditLogger(tmp_path).path, limit=0)
    assert [record.kind for record in records] == ["memory", "memory"]
    assert "add[project]: 用 pytest" in records[0].detail
    assert records[0].thread_id == "t1"
    assert "forget[project] #1" in records[1].detail


def test_memory_save_is_not_exposed_without_write(monkeypatch) -> None:
    from coding_agent.tools import memory as memory_tools

    monkeypatch.setattr(
        memory_tools, "SandboxFs", lambda sandbox, root, cache=None: _FakeFs()
    )
    settings = Settings(
        _env_file=None, deepseek_api_key="x", wsl_workspace="/work"
    )
    tools = memory_tools.build_memory_tools(
        settings, SimpleNamespace(), allow_write=False  # type: ignore[arg-type]
    )
    assert [tool.name for tool in tools] == ["memory_search"]
