"""架构约束：UI 层只能通过 AgentRuntime 触达能力层。

若 TUI / Web 直接导入 tools 或 sandbox，就会长出第二条执行路径，
最危险的后果是「Web 端绕过命令分级审批」。这条约束用一个静态扫描守住。

**这条约束是安全属性，所以扫描本身必须堵死所有写法。** 早先的实现只处理
`import a.b` 与 `from a.b import c`（且要求 `node.level == 0`），于是三种写法
都能溜过去：

    from ..sandbox import policy        # 相对导入：level != 0，被整条跳过
    from coding_agent import sandbox    # 只记下 `coding_agent`，不匹配任何禁忌前缀
    from ..tools import files           # 同上

下面把「提取」与「判定」分开，并**为每种写法各留一条防绕过用例** ——
安全测试自己也要有测试。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "coding_agent"

# 前端包：只允许依赖 runtime / events / config 这些中立层
UI_PACKAGES = ("tui", "web", "desktop")
FORBIDDEN = ("coding_agent.tools", "coding_agent.sandbox")


def _modules(package: str) -> list[Path]:
    directory = SRC / package
    if not directory.exists():
        return []
    return sorted(directory.rglob("*.py"))


def _package_of(path: Path, src: Path = SRC) -> str:
    """模块所属的包名：`.../coding_agent/tui/app.py` → `coding_agent.tui`。"""
    rel = path.resolve().relative_to(src.resolve())
    parts = rel.parts[:-1]
    return ".".join(["coding_agent", *parts]) if parts else "coding_agent"


def _absolute_module(node: ast.ImportFrom, package: str) -> str | None:
    """把（可能是相对的）ImportFrom 解析成绝对模块名。

    `level=1` 指向当前包，`level=2` 指向上一级，依此类推。
    越出顶层的 level 返回 None（那种写法 Python 自己也会导入失败）。
    """
    if node.level == 0:
        return node.module

    parts = package.split(".")
    drop = node.level - 1
    if drop >= len(parts):
        return None
    base = parts[: len(parts) - drop]
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _imports(source: str, package: str) -> set[str]:
    """抽出源码里被导入的所有绝对模块名。

    `from a.b import c` 同时记下 `a.b` 与 `a.b.c`：后者让
    `from coding_agent import sandbox` 这种「导入子模块」的写法也能被认出。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(node, package)
            if base is None:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _violations(imported: set[str]) -> list[str]:
    """命中了禁忌前缀的导入（判定与提取分开，好单独测）。"""
    return sorted(
        name
        for name in imported
        if any(name == bad or name.startswith(f"{bad}.") for bad in FORBIDDEN)
    )


def _imported_modules(path: Path) -> set[str]:
    return _imports(path.read_text(encoding="utf-8"), _package_of(path))


@pytest.mark.parametrize("package", UI_PACKAGES)
def test_ui_does_not_import_capability_or_sandbox(package: str) -> None:
    for path in _modules(package):
        offenders = _violations(_imported_modules(path))
        assert not offenders, (
            f"{path.relative_to(SRC)} 直接导入了 {offenders}；"
            f"UI 层只能通过 AgentRuntime 触达能力层，否则会出现绕过审批的旁路。"
        )


# ---------------- 扫描自身的防绕过 ----------------

# 每一种都必须被认出来。漏掉任何一种，这条安全约束就只剩虚假保证。
# 基准是 `coding_agent/tui/app.py`，其**所属包**是 `coding_agent.tui`。
_BYPASS_SPELLINGS = [
    "import coding_agent.sandbox.policy",
    "from coding_agent.sandbox import policy",
    "from coding_agent.sandbox.policy import classify",
    "from coding_agent import sandbox",
    "from coding_agent import tools",
    "from ..sandbox import policy",
    "from .. import sandbox",
    "from ..tools import files",
]

_LEGITIMATE = [
    "import coding_agent.runtime",
    "from coding_agent.runtime import AgentRuntime",
    "from coding_agent.events import RunFinished",
    "from coding_agent import runtime",
    "from . import events",
    "from .policy import decide",
    "from ..memory import sessions",
]


@pytest.mark.parametrize("source", _BYPASS_SPELLINGS)
def test_every_spelling_of_a_forbidden_import_is_caught(source: str) -> None:
    found = _imports(source, "coding_agent.tui")
    assert _violations(found), f"逃过扫描：{source!r} → 只抽出 {sorted(found)}"


@pytest.mark.parametrize("source", _LEGITIMATE)
def test_legitimate_imports_are_not_flagged(source: str) -> None:
    """反向对照：约束必须是因为「不该导入的没导入」而成立，不是因为扫描太宽。

    注意 `from . import events` 在 `tui/` 里指的是 `coding_agent.tui.events`
    （同包兄弟模块），并不是禁忌层 —— 判定要能区分这两者。
    """
    found = _imports(source, "coding_agent.tui")
    assert not _violations(found), f"误报：{source!r} → {sorted(found)}"


def test_tui_reaches_capabilities_only_through_runtime() -> None:
    """防止约束测试变成空转：满足约束必须是因为走了 runtime，而不是因为没写 UI。"""
    modules = _modules("tui")
    assert modules, "tui 包不存在，架构约束测试失去意义"
    assert any(
        "coding_agent.runtime" in _imported_modules(path) for path in modules
    ), "tui 没有通过 AgentRuntime 触达能力层"


# ---------------- A1–A4 分层边 ----------------

def _has_prefix(imported: set[str], prefix: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for name in imported)


def test_agent_server_depends_on_neutral_protocol_not_a2() -> None:
    paths = [SRC / "agent_server.py", *_modules("agent_server_routes")]
    for path in paths:
        imported = _imported_modules(path)
        assert not _has_prefix(imported, "coding_agent.control"), path
        assert not _has_prefix(imported, "coding_agent.web"), path
    assert "coding_agent.agent_protocol" in _imported_modules(
        SRC / "agent_server.py"
    )


def test_agent_core_does_not_import_a2_or_web() -> None:
    paths = [*_modules("runtime"), *_modules("graph"), *_modules("orchestrator")]
    for path in paths:
        imported = _imported_modules(path)
        assert not _has_prefix(imported, "coding_agent.control"), path
        assert not _has_prefix(imported, "coding_agent.web"), path


def test_a4_does_not_import_a2_or_web() -> None:
    for package in ("execution", "tools", "sandbox"):
        for path in _modules(package):
            imported = _imported_modules(path)
            assert not _has_prefix(imported, "coding_agent.control"), path
            assert not _has_prefix(imported, "coding_agent.web"), path


def test_control_does_not_import_agent_core() -> None:
    forbidden = (
        "coding_agent.runtime",
        "coding_agent.graph",
        "coding_agent.orchestrator",
        "coding_agent.tools",
        "coding_agent.sandbox",
    )
    for path in _modules("control"):
        imported = _imported_modules(path)
        assert not any(_has_prefix(imported, item) for item in forbidden), path


def test_web_runtime_import_is_limited_to_same_process_fallback() -> None:
    """P1.2：只有 web/local_runtime.py 的同进程适配器可以持有 runtime。"""
    for path in _modules("web"):
        if path.name == "local_runtime.py":
            continue
        assert "coding_agent.runtime" not in _imported_modules(path), path
    assert "coding_agent.runtime" in _imported_modules(
        SRC / "web" / "local_runtime.py"
    )


def test_cli_sandbox_import_is_limited_to_platform_adapter() -> None:
    """P1.2：只有 cli/platform.py 的探测适配器可以 import sandbox。"""
    for path in _modules("cli"):
        if path.name == "platform.py":
            continue
        assert not _violations(_imported_modules(path)), path
    assert _violations(_imported_modules(SRC / "cli" / "platform.py")), (
        "CLI 的平台适配器不再触达 sandbox，请删除这条 allowlist"
    )


def test_shared_modules_stay_neutral() -> None:
    forbidden = (
        "coding_agent.control",
        "coding_agent.web",
        "coding_agent.runtime",
        "coding_agent.tools",
        "coding_agent.sandbox",
    )
    for rel in ("agent_protocol.py", "event_transport.py", "workspace/browse.py"):
        imported = _imported_modules(SRC / rel)
        assert not any(_has_prefix(imported, item) for item in forbidden), rel


# ---------------- 依赖形状：环与跨层边 ----------------
#
# `docs/COMPLEXITY.md` 曾写「依赖无环」，而同一文档的依赖图里就画着
# `graph -> orchestrator` 与 `orchestrator -> graph.state`（包的**双向**依赖）。
# 两者都对：包级是有环的，模块级是无环的 —— 真正的约束在模块粒度。
# 下面把这句话变成可执行的事实，并按"不许新增"冻结已知例外。

# 2026-10-29 清零：`scheduler_run` 曾顶层 import `scheduler`，而 `scheduler`
# 在函数里反向 import `scheduler_run` —— 靠"晚导入"绕开导入期环。现在纯函数
# 下沉到 `scheduler_plan.py`，两个模块之间只剩单向边。
# 基线必须保持为空：新增任何环都得先拆掉，而不是往这里加。
_KNOWN_CYCLIC_MODULES: set[str] = set()


def _all_modules() -> dict[str, Path]:
    return {
        ".".join(
            ["coding_agent", *path.relative_to(SRC).with_suffix("").parts]
        ): path
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _module_graph() -> dict[str, set[str]]:
    """模块级依赖图（只保留项目内的模块）。"""
    modules = _all_modules()
    return {
        name: {
            imported
            for imported in _imported_modules(path)
            if imported in modules and imported != name
        }
        for name, path in modules.items()
    }


def _reachable(start: str, goal: str, graph: dict[str, set[str]]) -> bool:
    seen = {start}
    queue = [start]
    while queue:
        node = queue.pop()
        for target in graph.get(node, ()):
            if target == goal:
                return True
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return False


def _cyclic_modules(graph: dict[str, set[str]]) -> set[str]:
    """处在某个环上的模块（含自环）。"""
    cyclic: set[str] = set()
    for node, targets in graph.items():
        for target in targets:
            if target == node or _reachable(target, node, graph):
                cyclic.update((node, target))
    return cyclic


def test_dependency_graph_has_no_new_cycles() -> None:
    """模块级依赖图必须是 DAG —— 已知例外冻结在 `_KNOWN_CYCLIC_MODULES`。

    为什么只看模块级：把模块折叠成包之后，`graph ⇄ orchestrator`、
    `config ⇄ llm` 看起来都是环，因为它们**搭的车不一样**（前者真的双向，
    后者是延迟 import + 属性访问）。Python 在乎的是模块级顺序，所以这里
    冻结的也是模块级事实。
    """
    cyclic = _cyclic_modules(_module_graph())
    assert not cyclic - _KNOWN_CYCLIC_MODULES, (
        f"新增了依赖环（涉及的模块：{sorted(cyclic - _KNOWN_CYCLIC_MODULES)}）。"
        f"环会让模块初始化顺序变成隐式契约；请先用本地函数/延迟导入拆掉，"
        f"确有必要时才更新本文件的已知例外并写清理由。"
    )
    assert not _KNOWN_CYCLIC_MODULES - cyclic, (
        f"已知环已不存在（{sorted(_KNOWN_CYCLIC_MODULES - cyclic)}）："
        f"请从 `_KNOWN_CYCLIC_MODULES` 里删掉，基线不该留着已经修好的东西。"
    )


def test_orchestrator_does_not_import_graph_build() -> None:
    """包级环安全的前提：`orchestrator` 不得指向 `graph.build`。

    `graph/build.py` 已经 import 了整个 orchestrator；反向 import 会立刻
    闭合模块级环。上一条测试的报错里也能看到，但这里给了更准的位置。
    """
    for path in _modules("orchestrator"):
        imported = _imported_modules(path)
        assert "coding_agent.graph.build" not in imported, (
            f"{path.relative_to(SRC)} 反向依赖了 graph.build："
            f"`graph -> orchestrator` 已经是单向边，反向会形成模块级环。"
        )


# CLI 跑在宿主上，直接 import 控制面会把 A1/A2 边界压平：改用户、改配额如果
# 走 `control.store` 而不是 A2 的 HTTP 面，鉴权与审批就被绕过去了。每条边都要
# 显式登记 —— 新增即失败，偿还条件见 docs/tech-debt.yaml TD-032。
#
# `cli/admin.py -> control` 保留但已收窄：它现在只调
# `control.bootstrap.create_first_admin()`（租户下没有任何账号时才允许创建，
# 见 tests/unit/test_admin_bootstrap.py），不再持有 `ControlStore` 句柄。
# 冷启动必须有这条路径 —— 服务还没起来，走 HTTP 无门。
_FROZEN_CLI_EDGES = {
    ("cli/admin.py", "coding_agent.control"),
    ("cli/commands/web.py", "coding_agent.web"),
    ("cli/commands/desktop.py", "coding_agent.tui"),
}


def test_cli_cross_layer_imports_are_frozen() -> None:
    actual: set[tuple[str, str]] = set()
    for path in _modules("cli"):
        imported = _imported_modules(path)
        for forbidden in (
            "coding_agent.control",
            "coding_agent.web",
            "coding_agent.tui",
        ):
            if _has_prefix(imported, forbidden):
                actual.add((path.relative_to(SRC).as_posix(), forbidden))

    assert not actual - _FROZEN_CLI_EDGES, (
        f"CLI 新增了跨层边：{sorted(actual - _FROZEN_CLI_EDGES)}。"
        f"CLI 应当经 runtime 或 A2 的 HTTP 面行事，不要直接拿着 control.store 改用户。"
    )
    assert not _FROZEN_CLI_EDGES - actual, (
        f"冻结的跨层边已不存在：{sorted(_FROZEN_CLI_EDGES - actual)}。"
        f"请从 `_FROZEN_CLI_EDGES` 删掉 —— 基线里不留已经修好的债。"
    )
