from __future__ import annotations

from collections.abc import Iterator

import pytest

from ubismart.config import Settings, get_settings
from ubismart.sandbox.wsl_exec import WslSandbox


@pytest.fixture(autouse=True)
def _isolate_control_db(request, monkeypatch) -> None:
    """普通用例不继承宿主/`.env` 里的控制面数据库。

    `Settings(_env_file=None)` 仍会读进程环境；开发机可能为了测试把
    `AGENT_CONTROL_DB_URL` 放进环境。若不隔离，普通用例会连到共享 MySQL，
    造成会话/配额等跨文件污染。MySQL 用例显式放行，并从
    `AGENT_TEST_MYSQL_URL` 或 `.env` 读取。
    """
    if request.node.get_closest_marker("mysql"):
        return
    monkeypatch.delenv("AGENT_CONTROL_DB_URL", raising=False)


@pytest.fixture(autouse=True)
def _forbid_real_wsl_unless_marked(request: pytest.FixtureRequest, monkeypatch) -> None:
    """非 WSL 用例不得真的执行沙箱命令。

    Linux CI 上没有 wsl.exe，漏标的用例只会在那边才炸；这个守卫让它在**任何平台**
    当场失败（本机有 WSL 也一样），把问题挡在推送之前。
    确实需要探测宿主 WSL 的用例（如 `doctor`）标 `@pytest.mark.wsl_env` 放行；
    需要真实沙箱的集成用例标 `@pytest.mark.wsl`。
    """
    if (
        request.node.get_closest_marker("wsl")
        or request.node.get_closest_marker("wsl_env")
        or request.node.get_closest_marker("execution")
        # 智能体级 / 系统级 E2E 自己有标记：它们真实调用模型或启动 A2/A3，
        # 不能被 `pytest -m wsl`（只跑沙箱层）顺带选中，因此不挂 wsl 标记。
        or request.node.get_closest_marker("agent_e2e")
        or request.node.get_closest_marker("system_e2e")
    ):
        return

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "非 WSL 用例触发了一次真实的沙箱执行。请改用假依赖（例如注入空记忆），"
            "或为该用例加 @pytest.mark.wsl（真实沙箱）/ @pytest.mark.wsl_env（探测宿主）。"
        )

    # 常驻 shell 默认开启后，命令可能走 `_run_in_session` 而不是 `_exec`；
    # 两条执行入口都要挡住，否则漏标的用例会悄悄跑真实沙箱。
    monkeypatch.setattr(WslSandbox, "_exec", _boom)
    monkeypatch.setattr(WslSandbox, "_run_in_session", _boom)


@pytest.fixture(autouse=True)
def _isolate_agent_data_dirs(tmp_path, monkeypatch) -> Iterator[None]:
    """把宿主侧数据目录重定向到临时目录。

    默认落在 `<cwd>/.agent`，且 `pytest-xdist` 多个 worker 共享同一目录；
    共享的 checkpoint 与限流状态会让本次结果依赖此前运行，产生“像回归”
    的噪声。显式传 `checkpoint_path=":memory:"` 的用例仍以自己的参数为准。
    """
    monkeypatch.setenv("AGENT_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("AGENT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv(
        "AGENT_SNAPSHOT_MANIFEST_DIR", str(tmp_path / "snapshot-manifests")
    )
    monkeypatch.setenv("AGENT_STEERING_DIR", str(tmp_path / "steering"))
    monkeypatch.setenv(
        "AGENT_RATE_LIMIT_STATE_PATH", str(tmp_path / "rate_limits.json")
    )
    monkeypatch.setenv("AGENT_WEB_DATA_ROOT", str(tmp_path / "web"))
    monkeypatch.setenv("AGENT_AGENT_DATA_ROOT", str(tmp_path / "agent"))
    monkeypatch.setenv(
        "AGENT_HOST_SNAPSHOT_DIR", str(tmp_path / "host-snapshots")
    )
    monkeypatch.setenv(
        "AGENT_CONTROL_BACKUP_DIR", str(tmp_path / "backups")
    )
    # 宿主环境里的企业登录/密钥/前端托管配置不得渗进单元测试。
    for name in (
        "AGENT_WEB_AUTH_TOKENS",
        "AGENT_WEB_AUTH_TOKENS_FILE",
        "AGENT_WEB_ACCOUNTS_ENABLED",
        "AGENT_WEB_ACCOUNTS_REQUIRE_MYSQL",
        "AGENT_WEB_FRONTEND_DIR",
        "AGENT_WEB_STATIC_DIR",
        "AGENT_SECRET_MASTER_KEY",
        "AGENT_SECRET_MASTER_KEY_FILE",
        "AGENT_LLM_PROVIDERS",
        "AGENT_EVENT_TRANSPORT",
        "AGENT_EVENT_TRANSPORT_URL",
        "AGENT_COST_CURRENCY",
        "AGENT_USD_TO_CNY",
    ):
        monkeypatch.delenv(name, raising=False)

    # 限流协调器是模块级单例，可能已被上一个测试/宿主配置指向真实文件。
    from ubismart.llm.ratelimit import GLOBAL_LIMITER

    GLOBAL_LIMITER.configure(str(tmp_path / "rate_limits.json"))
    GLOBAL_LIMITER.reset()
    # get_settings 是 lru_cache：不清理会把上一个测试的路径带进来。
    get_settings.cache_clear()
    yield
    GLOBAL_LIMITER.reset()
    GLOBAL_LIMITER.configure(None)
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def sandbox(settings: Settings) -> WslSandbox:
    return WslSandbox(settings)


@pytest.fixture
def require_wsl(sandbox: WslSandbox) -> WslSandbox:
    """需要真实 WSL 的测试用它，环境不具备时自动跳过。"""
    if not WslSandbox.available(sandbox.settings.wsl_distro):
        pytest.skip(f"WSL 发行版 {sandbox.settings.wsl_distro} 不可用")
    return sandbox
