"""LangSmith 全功能评测套件。

两类用例：

- `capability`：复用 tests/eval 的真实 WSL + 模型任务，评判「能不能做完」；
- `mechanism`：不调用模型的确定性探针，覆盖安全/路由/持久化/代码索引等
  不靠“通过率”体现的功能。

用法：

    python tests/eval/langsmith_suite.py --upload
    python tests/eval/langsmith_suite.py --run --only mechanism
    python tests/eval/langsmith_suite.py --run --only capability --limit 3
    python tests/eval/langsmith_suite.py --run --only all --kinds all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (_REPO_ROOT / "src", Path(__file__).resolve().parent):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from harness import (  # noqa: E402
    EvalTask,
    eval_root,
    run_task,
    task_root,
)
from tasks import TASKS  # noqa: E402
from tasks_full import TASKS_FULL  # noqa: E402
from tasks_hard import TASKS_HARD  # noqa: E402
from tasks_long import LONG_TASKS  # noqa: E402
from tasks_real import TASKS_REAL, TASKS_REAL_ADVANCED  # noqa: E402

from coding_agent.codeintel.python_index import (  # noqa: E402
    build_index,
    related_test_files,
)
from coding_agent.codeintel.symbols import search_symbols  # noqa: E402
from coding_agent.config import Settings, get_settings  # noqa: E402
from coding_agent.llm.billing import billing_targets  # noqa: E402
from coding_agent.llm.ratelimit import RateLimitCoordinator  # noqa: E402
from coding_agent.llm.router import ProviderRouter  # noqa: E402
from coding_agent.sandbox.policy import (  # noqa: E402
    AUTO,
    CommandLevel,
    SessionPolicy,
    classify,
    parse_allow_rules,
)
from coding_agent.sandbox.wsl_exec import WslSandbox  # noqa: E402
from coding_agent.security.write_policy import protected_write_reason  # noqa: E402
from coding_agent.steering import SteeringCommand, SteeringController  # noqa: E402

DEFAULT_DATASET = "coding-agent-full-v1"

# 项目里已经存在的 capability 任务：默认全部纳入数据集。
CAPABILITY_IDS: tuple[str, ...] = tuple(
    task.id for task in (*TASKS, *LONG_TASKS, *TASKS_FULL)
)

# hard 套件单独成数据集：它的单任务成本更高，不能默认混进全量。
HARD_IDS: tuple[str, ...] = tuple(task.id for task in TASKS_HARD)

# real 套件同样独立：完整标准库模块会让上下文和成本显著上升。
REAL_IDS: tuple[str, ...] = tuple(task.id for task in TASKS_REAL)
REAL_ADVANCED_IDS: tuple[str, ...] = tuple(
    task.id for task in TASKS_REAL_ADVANCED
)

# 一次覆盖主要回路的抽样：direct、跨文件、排障、隐藏测试、并发读、并发写/合并。
SMOKE_IDS: tuple[str, ...] = (
    "single_file/off_by_one",
    "cross_file/signature_change",
    "debug/circular_import",
    "deep/spec_pagination",
    "long/discount_and_audit",
    "long/parallel_modules",
    "agent/orchestrated_implement",
)

# 智能体级用例：复用 capability 任务，但显式覆盖运行设置，确保真的走到目标
# 工作流与 worker/合并路径。capability 用例走的是 `get_settings()` 默认值，
# 默认 `orchestrator_write_enabled=false`，因此写编排不会被覆盖到。
AGENT_CASES: dict[str, dict[str, Any]] = {
    "agent/orchestrated_implement": {
        "task_id": "long/parallel_modules",
        "settings": {
            "orchestrator_write_enabled": True,
            "orchestrator_min_files": 0,
            "orchestrator_max_tasks": 6,
        },
        "expected_workflow": "orchestrated_implement",
        "min_workers": 2,
        "min_merged": 1,
    },
    # 子智能体委派路径：关掉编排，让主 agent 自己决定是否 delegate。
    # prompt 明确要求“同时派两个”，否则模型可能只读不委派，这条回路就测不到。
    "agent/subagent_explore": {
        "task_id": "long/discount_pipeline",
        "settings": {
            "orchestrator_enabled": False,
            "explore_enabled": True,
            "delegate_min_files": 0,
            "max_delegate_calls": 4,
        },
        "prompt_prefix": (
            "动手前先用 `delegate_explore` **同时**派出 2 个只读子智能体：\n"
            "1. 一个查看 models / service / order_intake 的数据流；\n"
            "2. 一个查看 reporting / handlers / discount_approval 的交付面。\n"
            "拿到两份摘要后再开始实现。\n\n"
        ),
        "min_subagents": 1,
        "min_delegate_batch": 2,
    },
}

# capability 用例上的机制断言。默认设置下（写编排关闭），这两个长程任务
# 的请求都含多交付物信号，规则层会选 `parallel_readonly`。
#
# 之所以放在 capability 上而不是另跑 agent case：同一个任务、同一套设置再跑
# 一遍只为了断言 workflow，会白白多花约 1M token。
CAPABILITY_CHECKS: dict[str, dict[str, Any]] = {
    "long/discount_and_audit": {
        "expected_workflow": "parallel_readonly",
        "min_workers": 2,
    },
    "long/parallel_modules": {
        "expected_workflow": "parallel_readonly",
        "min_workers": 2,
    },
}


class _FakeProvider:
    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self.text = text
        self.model_name = name

    async def ainvoke(self, messages, config=None, **kwargs):
        return self.text

    async def astream(self, messages, config=None, **kwargs):
        yield self.text

    def invoke(self, messages, config=None, **kwargs):
        return self.text

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return self

    def with_retry(self, **kwargs):
        return self


def _ok(checks: dict[str, bool], detail: str = "") -> dict[str, Any]:
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "detail": detail,
    }


def _mechanism_secret_redaction() -> dict[str, Any]:
    from coding_agent.security.secrets import redact_secrets, secret_kinds

    text = "sk-" + "a" * 32 + " password=hunter2hunter2"
    redacted = redact_secrets(text)
    return _ok(
        {
            "openai_redacted": "sk-" not in redacted,
            "password_redacted": "hunter2hunter2" not in redacted,
            "kind_detected": "openai_key" in secret_kinds(text),
        },
        redacted,
    )


def _mechanism_write_policy() -> dict[str, Any]:
    return _ok(
        {
            "env_denied": protected_write_reason(".env") is not None,
            "template_allowed": protected_write_reason(".env.example") is None,
            "git_denied": protected_write_reason(".git/config") is not None,
            "normal_allowed": protected_write_reason("app/main.py") is None,
        }
    )


def _mechanism_allow_rules() -> dict[str, Any]:
    policy = SessionPolicy(
        allow_rules=parse_allow_rules("shell_exec:git status*")
    )
    allowed = policy.decide_call(
        "shell_exec", {"command": "git status --short"}, CommandLevel.MUTATE
    )
    denied = policy.decide_call(
        "shell_exec", {"command": "rm -rf /"}, CommandLevel.DANGER
    )
    return _ok(
        {
            "l2_rule_allowed": allowed == AUTO,
            "danger_not_bypassed": denied != AUTO,
        }
    )


def _mechanism_steering_persistence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        first = SteeringController(directory)
        first.submit("t", SteeringCommand("add", "检查边界", "review"))
        first.mark_paused("t")
        second = SteeringController(directory)
        commands = second.peek("t")
        paused = second.is_paused("t")
        consumed = second.consume("t")
        third = SteeringController(directory)
        return _ok(
            {
                "command_survived": [item.action for item in commands]
                == ["add"],
                "pause_survived": paused is True,
                "consumed_once": [item.action for item in consumed]
                == ["add"],
                "not_replayed": third.peek("t") == [],
            }
        )


def _mechanism_router_cost_and_capability() -> dict[str, Any]:
    limiter = RateLimitCoordinator()
    router = ProviderRouter(
        [_FakeProvider("expensive", "expensive"), _FakeProvider("cheap", "cheap")],
        ["expensive", "cheap"],
        ["k-expensive", "k-cheap"],
        limiter=limiter,
        costs=[(100.0, 100.0), (1.0, 1.0)],
        strategy="cost",
    )
    chosen = asyncio.run(router.ainvoke([]))

    typed = ProviderRouter(
        [_FakeProvider("agent", "agent"), _FakeProvider("worker", "worker")],
        ["agent", "worker"],
        ["k-agent", "k-worker"],
        limiter=RateLimitCoordinator(),
        task_type="worker",
        task_types=[{"agent"}, {"worker"}],
        strategy="priority",
    )
    typed_choice = asyncio.run(typed.ainvoke([]))
    return _ok(
        {
            "cost_route": chosen == "cheap",
            "task_type_route": typed_choice == "worker",
        }
    )


def _mechanism_codeintel() -> dict[str, Any]:
    sources = {
        "src/app/core.py": "def compute():\n    return 1\n",
        "src/app/api.py": (
            "from app.core import compute\n\ndef handler():\n"
            "    return compute()\n"
        ),
        "tests/test_api.py": (
            "from app.api import handler\n\ndef test_handler():\n"
            "    assert handler()\n"
        ),
    }
    index = build_index(sources)
    related = related_test_files(index, ["src/app/core.py"])
    symbols = search_symbols(sources, "compute")
    return _ok(
        {
            "import_graph": related == ["tests/test_api.py"],
            "symbol_search": any(item.name == "compute" for item in symbols),
        }
    )


def _mechanism_billing_config() -> dict[str, Any]:
    settings = Settings(
        _env_file=None,
        llm_providers=json.dumps(
            [
                {
                    "name": "primary",
                    "api_key": "llm-key",
                    "model": "m1",
                    "billing": {
                        "kind": "openai_costs",
                        "base_url": "https://billing.invalid/v1",
                        "api_key": "billing-key",
                    },
                }
            ]
        ),
    )
    targets = billing_targets(settings)
    return _ok(
        {
            "target_parsed": len(targets) == 1,
            "endpoint": bool(
                targets and targets[0].url.endswith("/organization/costs")
            ),
            "key_separate": bool(
                targets and targets[0].api_key == "billing-key"
            ),
        }
    )


def _mechanism_shell_policy() -> dict[str, Any]:
    return _ok(
        {
            "read_allowed": classify("ls").level == CommandLevel.READ,
            "rm_danger": classify("rm -rf /").level
            == CommandLevel.DANGER,
            "outside_path_escalated": classify("cat /etc/shadow").level
            == CommandLevel.MUTATE,
        }
    )


def _mechanism_lsp_framing() -> dict[str, Any]:
    from coding_agent.sandbox.wsl_exec import _parse_session_output

    raw = (
        b"noise\n"
        b"\n__AGENT_OUT_tok__\nhello\n"
        b"\n__AGENT_ERR_tok__\noops\n"
        b"\n__AGENT_END_tok__:3\n"
    )
    return _ok(
        {"framing_parsed": _parse_session_output(raw, "tok") == ("hello\n", "oops\n", 3)}
    )


def _mechanism_persistent_shell() -> dict[str, Any]:
    settings = get_settings().model_copy(
        update={
            "persistent_shell": True,
            "persistent_shell_pool": 1,
            "rate_limit_state_path": ":memory:",
        }
    )
    sandbox = WslSandbox(settings)
    try:
        first = sandbox.run("printf hello")
        second = sandbox.run("printf bye")
        return _ok(
            {
                "first_ok": first.stdout == "hello",
                "second_ok": second.stdout == "bye",
                "reused": sandbox.command_count == 1,
            }
        )
    finally:
        sandbox.close()


def _mechanism_run_usage() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        first = RateLimitCoordinator(path=Path(directory) / "limits.json")
        second = RateLimitCoordinator(path=Path(directory) / "limits.json")
        first.record_usage(
            "p1", input_tokens=10, output_tokens=5, cost_usd=0.01, run_id="r"
        )
        second.record_usage(
            "p2", input_tokens=20, output_tokens=10, cost_usd=0.02, run_id="r"
        )
        usage = first.run_usage("r")
        return _ok(
            {
                "tokens": usage["input_tokens"] == 30
                and usage["output_tokens"] == 15,
                "cost": abs(usage["cost_usd"] - 0.03) < 1e-9,
                "providers": set(usage["providers"]) == {"p1", "p2"},
            }
        )


def _mechanism_unregistered_tool() -> dict[str, Any]:
    """未登记工具必须 fail-closed 到 L3，且 L3 不能被自动批准绕过。"""
    from coding_agent.graph.nodes.approve import tool_level
    from coding_agent.sandbox.policy import ASK, SessionPolicy

    level = tool_level("totally_new_tool", {})
    policy = SessionPolicy(approval_mode="approve")
    decision = policy.decide_call("totally_new_tool", {}, level)
    return _ok(
        {
            "falls_back_to_danger": level == CommandLevel.DANGER,
            "asks_even_when_auto_approve": decision == ASK,
        },
        f"level={level.value} decision={decision}",
    )


def _mechanism_path_guard() -> dict[str, Any]:
    from coding_agent.paths import PathGuardError, ensure_inside

    kept = ensure_inside("/home/u/ws/app/x.py", "/home/u/ws")
    try:
        ensure_inside("/home/u/ws/../../etc/passwd", "/home/u/ws")
        escaped = True
    except PathGuardError:
        escaped = False
    return _ok(
        {
            "inside_kept": kept == "/home/u/ws/app/x.py",
            "escape_rejected": not escaped,
        },
        kept,
    )


def _mechanism_audit_chain() -> dict[str, Any]:
    """写两条记录 → 链校验通过；改一条内容 → 必须被发现。"""
    from coding_agent.audit import (
        AuditLogger,
        AuditRecord,
        verify_audit_files,
    )

    with tempfile.TemporaryDirectory() as directory:
        logger = AuditLogger(directory)
        for kind in ("run_start", "run_end"):
            logger.write(
                AuditRecord(
                    ts="2026-01-01T00:00:00+00:00",
                    kind=kind,
                    thread_id="t",
                )
            )
        path = logger.files_today()[0]
        ok, _ = verify_audit_files([path])

        lines = path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[1])
        payload["detail"] = "tampered"
        lines[1] = json.dumps(payload, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tampered, _ = verify_audit_files([path])
        return _ok({"chain_ok": ok, "tamper_detected": not tampered})


def _mechanism_archive_safety() -> dict[str, Any]:
    from coding_agent.workspace.archive import ArchiveError, safe_extract

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "evil.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("../evil.txt", b"x")
        dest = Path(directory) / "out"
        try:
            safe_extract(str(source), str(dest), name="evil.zip")
            rejected = False
        except ArchiveError:
            rejected = True
        return _ok(
            {
                "zip_slip_rejected": rejected,
                "no_escape_file": not (Path(directory) / "evil.txt").exists(),
            }
        )


def _mechanism_request_hmac() -> dict[str, Any]:
    from coding_agent.agent_protocol import sign_context, verify_context

    header = '{"user":"alice"}'
    signature = sign_context(header, "secret")
    return _ok(
        {
            "valid_signature": verify_context(header, signature, "secret"),
            "tampered_rejected": not verify_context(
                header + "x", signature, "secret"
            ),
            "missing_rejected": not verify_context(header, "", "secret"),
        }
    )


def _mechanism_delegate_wiring() -> dict[str, Any]:
    """委派入口的确定性探针：工具调用必须被宿主 runner 接管。

    不调用模型、不依赖 WSL；fake runner 返回带 tool_calls 的 artifact，
    断言委派计数、摘要回填与工具调用透传都成立。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from coding_agent.graph.nodes.tools import make_tools_node
    from coding_agent.tools.artifacts import SubAgentArtifact
    from coding_agent.tools.delegate import DELEGATE_EXPLORE, build_delegate_tool

    seen: list[dict[str, Any]] = []
    expected_calls = [{"name": "file_read", "ok": True}]

    async def runner(**kwargs: Any) -> SubAgentArtifact:
        seen.append(kwargs)
        return SubAgentArtifact(
            agent_id=f"explore:{kwargs['call_id']}",
            parent_call_id=kwargs["call_id"],
            role="explore",
            goal=kwargs["args"]["goal"],
            status="success",
            summary="入口在 main.py",
            relevant_files=["main.py"],
            tool_calls=list(expected_calls),
        )

    node = make_tools_node(
        [build_delegate_tool()],
        delegate_runner=runner,
        max_delegate_calls=2,
    )
    state = {
        "messages": [
            HumanMessage(content="看一下这个项目"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": DELEGATE_EXPLORE,
                        "args": {
                            "goal": "找到入口",
                            "paths": ["."],
                            "reason": "不熟",
                        },
                        "id": "d1",
                    }
                ],
            ),
        ],
        "cwd": "/home/u/ws",
        "approvals": {"d1": "auto"},
        "delegate_calls": 0,
        "plan": ["探索"],
        "plan_kinds": ["explore"],
        "delegation_allowed": True,
        "delegation_budget": 6,
    }
    out = asyncio.run(node(state, None))
    message = out["messages"][0]
    artifact = getattr(message, "artifact", None) or {}
    return _ok(
        {
            "delegate_executed": out.get("delegate_calls") == 1,
            "runner_called": len(seen) == 1,
            "summary_backfilled": artifact.get("summary") == "入口在 main.py",
            "tool_calls_preserved": artifact.get("tool_calls")
            == expected_calls,
            "content_contains_summary": "入口在 main.py" in message.content,
        }
    )


MECHANISMS: dict[str, Any] = {
    "security/secret_redaction": _mechanism_secret_redaction,
    "security/write_policy": _mechanism_write_policy,
    "security/allow_rules": _mechanism_allow_rules,
    "security/unregistered_tool": _mechanism_unregistered_tool,
    "security/path_guard": _mechanism_path_guard,
    "audit/hash_chain": _mechanism_audit_chain,
    "workspace/archive_safety": _mechanism_archive_safety,
    "protocol/hmac": _mechanism_request_hmac,
    "agent/delegate_wiring": _mechanism_delegate_wiring,
    "steering/persistence": _mechanism_steering_persistence,
    "router/cost_and_capability": _mechanism_router_cost_and_capability,
    "codeintel/import_graph": _mechanism_codeintel,
    "billing/config": _mechanism_billing_config,
    "shell/policy": _mechanism_shell_policy,
    "lsp/framing": _mechanism_lsp_framing,
    "shell/persistent": _mechanism_persistent_shell,
    "cost/run_usage": _mechanism_run_usage,
}


def _tasks_by_id() -> dict[str, EvalTask]:
    return {
        task.id: task
        for task in (
            *TASKS,
            *LONG_TASKS,
            *TASKS_FULL,
            *TASKS_HARD,
            *TASKS_REAL,
            *TASKS_REAL_ADVANCED,
        )
    }


def _case_ids(kind: str) -> list[str]:
    if kind == "mechanism":
        return list(MECHANISMS)
    if kind == "capability":
        return list(CAPABILITY_IDS)
    if kind == "agent":
        return list(AGENT_CASES)
    if kind == "hard":
        return list(HARD_IDS)
    if kind == "real":
        return list(REAL_IDS)
    if kind == "real_advanced":
        return list(REAL_ADVANCED_IDS)
    return [*MECHANISMS, *AGENT_CASES, *CAPABILITY_IDS]


def _case_input(case_id: str) -> dict[str, Any]:
    if case_id in MECHANISMS:
        return {
            "case_id": case_id,
            "kind": "mechanism",
            "category": "mechanism",
        }
    if case_id in AGENT_CASES:
        return {
            "case_id": case_id,
            "kind": "agent",
            "category": "agent",
        }
    task = _tasks_by_id()[case_id]
    return {
        "case_id": case_id,
        "kind": "capability",
        "category": task.category,
    }


def _prepare_env(settings: Settings) -> tuple[WslSandbox, Any, str]:
    if not WslSandbox.available(settings.wsl_distro):
        raise RuntimeError(f"WSL 发行版 {settings.wsl_distro} 不可用")
    sandbox = WslSandbox(settings)
    from coding_agent.sandbox.fs import SandboxFs
    from coding_agent.sandbox.wsl_exec import resolve_workspace

    fs = SandboxFs(sandbox, resolve_workspace(settings, sandbox))
    return sandbox, fs, eval_root(settings, sandbox)


def run_with_retries(
    run_case: Callable[[], dict[str, Any]], retries: int
) -> dict[str, Any]:
    """能力用例失败后隔离重跑。

    长程/spec 类任务存在模型非确定性；单次失败不足以说明能力回归。
    `retries` 是**额外**尝试次数，0 表示不重跑。
    """
    result = run_case()
    attempts = 1
    # 首次失败的原因是判断 flaky 的唯一证据。只保留最终结果会让
    # “重跑后通过”看起来和“一次通过”完全一样。
    first = dict(result)
    while not result.get("passed") and attempts <= max(0, int(retries)):
        result = run_case()
        attempts += 1
    result = dict(result)
    result["attempts"] = attempts
    result["first_attempt"] = {
        "passed": bool(first.get("passed")),
        "verdict": first.get("verdict"),
        "detail": str(first.get("detail") or "")[:2_000],
        "checks": first.get("checks") or {},
    }
    result["first_attempt_passed"] = bool(first.get("passed"))
    result["recovered_on_retry"] = bool(attempts > 1 and result.get("passed"))
    return result


def _result_metrics(result: Any) -> dict[str, Any]:
    return {
        "tier": result.tier,
        "agent_seconds": result.agent_seconds,
        "judge_seconds": result.judge_seconds,
        "tool_calls": result.tool_calls,
        "writes": result.writes,
        "steps": result.steps,
        "replans": result.replans,
        "verifications": result.verifications,
        "repairs": result.repairs,
        "approvals": result.approvals,
        "approvals_approved": result.approvals_approved,
        "workflow": result.workflow,
        "tasks_planned": result.tasks_planned,
        "workers": result.workers,
        "worker_statuses": result.worker_statuses,
        "worker_statuses_by_role": result.worker_statuses_by_role,
        "max_worker_batch": result.max_worker_batch,
        "worker_retries": result.worker_retries,
        "worker_merged": result.worker_merged,
        "worker_tool_calls": result.worker_tool_calls,
        "worker_input_tokens": result.worker_input_tokens,
        "worker_output_tokens": result.worker_output_tokens,
        "worker_seconds": result.worker_seconds,
        "worker_roles": result.worker_roles,
        "worker_merge_attempts": result.worker_merge_attempts,
        "worker_conflicts": result.worker_conflicts,
        "subagents": result.subagents,
        "subagent_statuses": result.subagent_statuses,
        "max_delegate_batch": result.max_delegate_batch,
        "subagent_input_tokens": result.subagent_input_tokens,
        "subagent_output_tokens": result.subagent_output_tokens,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_hit_tokens": result.cache_hit_tokens,
        "cache_miss_tokens": result.cache_miss_tokens,
        "budget": result.budget,
        "context_profile": result.context_profile,
    }


def _mechanism_checks(spec: dict[str, Any], result: Any) -> dict[str, bool]:
    """用例声明的机制断言；没有声明时返回空 dict（`all({})` 为 True）。"""
    checks: dict[str, bool] = {}
    expected = spec.get("expected_workflow")
    if expected:
        checks["workflow"] = result.workflow == expected
    min_workers = int(spec.get("min_workers") or 0)
    if min_workers:
        checks["workers"] = result.workers >= min_workers
    min_merged = int(spec.get("min_merged") or 0)
    if min_merged:
        checks["merged"] = result.worker_merged >= min_merged
    min_subagents = int(spec.get("min_subagents") or 0)
    if min_subagents:
        checks["subagents"] = result.subagents >= min_subagents
    min_delegate_batch = int(spec.get("min_delegate_batch") or 0)
    if min_delegate_batch:
        checks["delegate_batch"] = (
            result.max_delegate_batch >= min_delegate_batch
        )
    return checks


def _run_capability(
    task_id: str,
    settings: Settings,
    sandbox: WslSandbox,
    fs: Any,
    base: str,
) -> dict[str, Any]:
    task = _tasks_by_id()[task_id]
    result = asyncio.run(
        run_task(
            task,
            settings,
            sandbox,
            fs,
            task_root(settings, sandbox, task.id),
        )
    )
    checks = _mechanism_checks(CAPABILITY_CHECKS.get(task_id, {}), result)
    return {
        "passed": result.verdict == "passed" and all(checks.values()),
        "verdict": result.verdict,
        "detail": str(result.detail)[:2_000],
        "checks": checks,
        "metrics": _result_metrics(result),
    }


def _run_agent_case(
    case_id: str,
    settings: Settings,
    sandbox: WslSandbox,
    fs: Any,
    base: str,
) -> dict[str, Any]:
    """智能体级用例：先判目标机制是否发生，再判任务是否完成。"""
    spec = AGENT_CASES[case_id]
    scoped = settings.model_copy(update=dict(spec.get("settings") or {}))
    task = _tasks_by_id()[str(spec["task_id"])]
    # 只改提示词，不改任务本身：判定仍用任务自带的测试。
    if spec.get("prompt_prefix"):
        task = replace(task, prompt=str(spec["prompt_prefix"]) + task.prompt)
    # 独立工作区：同一个 task 可能同时作为 capability 用例在跑，不能共用
    # `.agent/eval/<task_id>`，否则两个 run 会互相清理对方的工作区。
    suffix = case_id.rsplit("/", 1)[-1]
    root = f"{task_root(scoped, sandbox, task.id)}-agent-{suffix}"
    result = asyncio.run(run_task(task, scoped, sandbox, fs, root))

    checks = _mechanism_checks(spec, result)
    return {
        "passed": result.verdict == "passed" and all(checks.values()),
        "verdict": result.verdict,
        "detail": str(result.detail)[:2_000],
        "checks": checks,
        "metrics": _result_metrics(result),
    }


def upload_dataset(client: Any, dataset: str, case_ids: list[str]) -> None:
    try:
        existing = client.read_dataset(dataset_name=dataset)
        dataset_id = existing.id
    except Exception:  # noqa: BLE001 - 不存在则创建
        created = client.create_dataset(
            dataset_name=dataset,
            description="CodingAgent 全功能评测：能力任务 + 机制探针",
        )
        dataset_id = created.id
    existing_ids = {
        str((example.inputs or {}).get("case_id", ""))
        for example in client.list_examples(dataset_id=dataset_id)
    }
    new_examples = [
        {
            "inputs": _case_input(case_id),
            "outputs": {"passed": True},
        }
        for case_id in case_ids
        if case_id not in existing_ids
    ]
    if new_examples:
        client.create_examples(dataset_id=dataset_id, examples=new_examples)
    print(f"LangSmith 数据集 {dataset}: {len(existing_ids) + len(new_examples)} 条")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LangSmith 全功能评测")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--upload", action="store_true", help="上传/补齐数据集")
    parser.add_argument("--run", action="store_true", help="运行实验")
    parser.add_argument(
        "--kinds",
        default="all",
        choices=[
            "all",
            "mechanism",
            "capability",
            "agent",
            "hard",
            "real",
            "real_advanced",
        ],
    )
    parser.add_argument("--only", default="", help="只跑某个 case_id")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="跑覆盖主要能力回路的 6 个 capability 抽样",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--case-retries",
        type=int,
        default=1,
        help="capability 用例失败后的额外重跑次数；0 = 不重跑",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="LangSmith 并发任务数；过高会触发模型/WSL 限流",
    )
    parser.add_argument("--experiment-prefix", default="coding-agent-full")
    args = parser.parse_args(argv)

    if not args.upload and not args.run:
        parser.print_help()
        return 2

    settings = get_settings()
    if not settings.langsmith_api_key:
        print("未配置 LANGSMITH_API_KEY")
        return 2
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project

    from langsmith import Client, evaluate

    client = Client()
    if args.smoke:
        args.kinds = "capability"
        case_ids = list(SMOKE_IDS)
    else:
        case_ids = _case_ids(args.kinds)
    if args.only:
        case_ids = [case_id for case_id in case_ids if case_id == args.only]
    if args.run and args.kinds in {
        "capability",
        "agent",
        "hard",
        "real",
        "real_advanced",
        "all",
    }:
        # capability / agent 需要 WSL；机制用例不依赖它。
        try:
            sandbox, fs, base = _prepare_env(settings)
        except RuntimeError as exc:
            print(exc)
            return 2
    else:
        sandbox = fs = base = None

    if args.upload:
        upload_dataset(client, args.dataset, case_ids)

    if not args.run:
        return 0

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        case_id = str(inputs.get("case_id", ""))
        if case_id in MECHANISMS:
            return MECHANISMS[case_id]()
        if sandbox is None:
            return {
                "passed": False,
                "detail": "capability/agent case 需要 WSL 环境",
            }
        if case_id in AGENT_CASES:
            return run_with_retries(
                lambda: _run_agent_case(
                    case_id, settings, sandbox, fs, base
                ),
                args.case_retries,
            )
        return run_with_retries(
            lambda: _run_capability(case_id, settings, sandbox, fs, base),
            args.case_retries,
        )

    # LangSmith 的 evaluate(data=...) 需要 dataset/example 对象，不接受裸 dict。
    # 子集运行时建一个派生 dataset，而不是把全部 46 条都跑一遍。
    data: Any = args.dataset
    if args.limit > 0 or args.only or args.kinds != "all":
        chosen = case_ids[: args.limit] if args.limit > 0 else case_ids
        subset_name = (
            "smoke" if args.smoke else f"{args.kinds}-{args.only or ''}"
        )
        suffix = re.sub(
            r"[^A-Za-z0-9_.-]", "_", subset_name
        ).strip("_-")
        data = f"{args.dataset}-{suffix}" if suffix else args.dataset
        upload_dataset(client, data, chosen)
    results = evaluate(
        target,
        data=data,
        evaluators=[_evaluator],
        experiment_prefix=args.experiment_prefix,
        max_concurrency=max(1, args.concurrency),
    )
    print(results)
    return 0


def _evaluator(run: Any, example: Any) -> list[dict[str, Any]]:
    outputs = run.outputs or {}
    checks = outputs.get("checks") or {}
    passed = bool(outputs.get("passed"))
    metrics = outputs.get("metrics") or {}
    result = [
        {"key": "passed", "score": 1.0 if passed else 0.0},
        {
            # "重跑后通过" 与 "一次通过" 必须分开：前者说明用例 flaky。
            # 机制探针不走重跑，默认按首次通过计。
            "key": "first_attempt_passed",
            "score": 1.0 if outputs.get("first_attempt_passed", True) else 0.0,
        },
        {
            "key": "mechanism_checks",
            "score": (
                1.0
                if all(bool(value) for value in checks.values())
                else 0.0
            )
            if checks
            else 1.0,
        },
    ]
    if metrics:
        result.append(
            {
                "key": "tool_calls",
                "score": float(metrics.get("tool_calls", 0) or 0),
            }
        )
        result.append(
            {
                "key": "writes",
                "score": float(metrics.get("writes", 0) or 0),
            }
        )
        hit = metrics.get("cache_hit_tokens")
        miss = metrics.get("cache_miss_tokens")
        if hit is not None and miss is not None and int(hit) + int(miss) > 0:
            result.append(
                {
                    "key": "cache_hit_rate",
                    "score": int(hit) / (int(hit) + int(miss)),
                }
            )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
