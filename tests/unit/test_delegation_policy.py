"""委派的成本策略：仓库规模 → 是否允许、最多几次。

A/B 的教训是 38 个文件的任务也会拆 3–5 个子智能体。这里钉住分级规则与
「探测失败不误伤」的行为。
"""

from __future__ import annotations

from ubismart.agents.policy import (
    RepoProfile,
    delegation_policy,
    profile_workspace,
    should_orchestrate,
)
from ubismart.config import Settings
from ubismart.sandbox.wsl_exec import ExecResult


class _Sandbox:
    """`SandboxFs.count_files` 的假实现：回显根目录，并按组返回计数。"""

    def __init__(self, counts=None, *, ok: bool = True, raises: bool = False):
        self.counts = {"total": 38, "tests": 9} if counts is None else counts
        self.ok = ok
        self.raises = raises
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: str | None = None) -> ExecResult:
        self.commands.append(command)
        if self.raises:
            raise AssertionError("sandbox unavailable")
        stdout = ""
        if self.ok and command.strip().startswith("realpath -m -- "):
            stdout = command.strip().split("-- ", 1)[1].strip("'\"") + "\n"
        elif self.ok:
            stdout = "".join(f"{key}={value}\n" for key, value in self.counts.items())
        return ExecResult(
            command=command,
            exit_code=0 if self.ok else 1,
            stdout=stdout,
            stderr="",
            duration_ms=1,
        )


def _settings(**overrides) -> Settings:
    values = {"explore_enabled": True}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_profile_parses_file_and_test_counts() -> None:
    profile = profile_workspace(
        _Sandbox({"total": 38, "tests": 12}), "/ws"  # type: ignore[arg-type]
    )
    assert profile == RepoProfile(file_count=38, test_file_count=12)


def test_profile_uses_one_traversal_call() -> None:
    """解析根 + 一次统计：不能退化成"每个 glob 一次 wsl.exe"。"""
    sandbox = _Sandbox({"total": 38, "tests": 12})
    profile_workspace(sandbox, "/ws")  # type: ignore[arg-type]
    assert len(sandbox.commands) == 2  # 1 次解析根 + 1 次统计
    assert "tests" in sandbox.commands[-1]  # 两组计数在同一段脚本里


def test_profile_is_unknown_on_probe_failure() -> None:
    assert profile_workspace(_Sandbox(raises=True), "/ws") == RepoProfile()  # type: ignore[arg-type]
    assert profile_workspace(_Sandbox(ok=False), "/ws") == RepoProfile()  # type: ignore[arg-type]
    assert (
        profile_workspace(_Sandbox({"total": "nonsense"}), "/ws")  # type: ignore[arg-type]
        == RepoProfile()
    )


def test_small_repo_disables_delegation() -> None:
    policy = delegation_policy(RepoProfile(file_count=10), _settings())
    assert policy.allowed is False
    assert policy.max_calls == 0


def test_medium_repo_caps_delegation_at_two() -> None:
    policy = delegation_policy(RepoProfile(file_count=50), _settings())
    assert policy.allowed is True
    assert policy.max_calls == 2


def test_large_repo_caps_delegation_at_four() -> None:
    policy = delegation_policy(RepoProfile(file_count=150), _settings())
    assert policy.allowed is True
    assert policy.max_calls == 4


def test_very_large_repo_uses_the_configured_maximum() -> None:
    policy = delegation_policy(RepoProfile(file_count=500), _settings())
    assert policy.allowed is True
    assert policy.max_calls == 6


def test_unknown_size_stays_enabled_to_avoid_false_negatives() -> None:
    policy = delegation_policy(RepoProfile(), _settings())
    assert policy.allowed is True
    assert policy.max_calls == 6


def test_explore_disabled_wins_over_size() -> None:
    policy = delegation_policy(
        RepoProfile(file_count=500), _settings(explore_enabled=False)
    )
    assert policy.allowed is False
    assert policy.max_calls == 0


def test_should_orchestrate_requires_a_known_large_repo() -> None:
    assert should_orchestrate("重构认证流程", RepoProfile(), _settings()) is False
    assert (
        should_orchestrate(
            "重构认证流程", RepoProfile(file_count=10), _settings()
        )
        is False
    )
    assert (
        should_orchestrate(
            "重构认证流程", RepoProfile(file_count=50), _settings()
        )
        is True
    )


def test_should_orchestrate_prefers_direct_for_a_single_file() -> None:
    profile = RepoProfile(file_count=50)

    assert should_orchestrate("在 app/alpha.py 实现折扣逻辑", profile, _settings()) is False
    # 两个文件 + 多交付物信号：仍交给编排。
    assert (
        should_orchestrate(
            "同时修改 app/alpha.py 和 app/beta.py", profile, _settings()
        )
        is True
    )


def test_should_orchestrate_handles_readonly_and_small_edits() -> None:
    profile = RepoProfile(file_count=50)

    assert should_orchestrate("解释 app/main.py 的启动流程", profile, _settings()) is False
    assert should_orchestrate("修一下 README 的 typo", profile, _settings()) is False
    # 带写动作的"怎么..."不是只读问答。
    assert should_orchestrate("怎么实现登录重试", profile, _settings()) is True


def test_should_orchestrate_can_be_disabled() -> None:
    profile = RepoProfile(file_count=50)
    settings = _settings(orchestrator_smart_entry=False)

    assert should_orchestrate("在 app/alpha.py 实现折扣", profile, settings) is True
