"""桌面入口的无头覆盖：参数、配置、启动失败与向导分支（GUI 仍手测）。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from coding_agent.desktop import launcher, setup
from coding_agent.desktop.config import LauncherConfig


class _FakeLock:
    instances: list[_FakeLock] = []

    def __init__(self, directory: Any, name: str) -> None:
        self.directory = directory
        self.name = name
        self.acquired = True
        self.released = False
        _FakeLock.instances.append(self)

    def acquire(self) -> bool:
        return self.acquired

    def release(self) -> None:
        self.released = True


class _Server:
    def __init__(self, port: int = 41000, token: str = "tok") -> None:
        self.port = port
        self.token = token
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _fake_webview() -> types.ModuleType:
    module = types.ModuleType("webview")
    module.windows = []
    module.FileDialog = types.SimpleNamespace(FOLDER="folder")
    module.create_window = lambda *a, **k: None
    module.start = lambda *a, **k: None
    return module


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    _FakeLock.instances.clear()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setitem(sys.modules, "webview", _fake_webview())
    monkeypatch.setattr(launcher, "WorkspaceLock", _FakeLock)
    monkeypatch.setattr(launcher, "_logging", lambda: None)
    config = LauncherConfig(
        distro="Ubuntu", workspace="/home/me/ws", python="/home/me/py"
    )
    monkeypatch.setattr(LauncherConfig, "load", classmethod(lambda cls: config))
    saved: list[LauncherConfig] = []
    monkeypatch.setattr(LauncherConfig, "save", lambda self: saved.append(self))
    return types.SimpleNamespace(config=config, saved=saved, monkeypatch=monkeypatch)


def test_parser_defaults_and_flags() -> None:
    parser = launcher._parser()
    args = parser.parse_args([])
    assert args.setup is False and args.host_mode is False
    assert args.workspace == "" and args.write is False
    args = parser.parse_args(["--setup", "--write", "--workspace", "D:/ws"])
    assert args.setup and args.write and args.workspace == "D:/ws"


def test_logging_creates_log_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(launcher.logging, "basicConfig", lambda **kwargs: None)
    launcher._logging()
    assert (tmp_path / "CodingAgent" / "logs").is_dir()


def test_host_mode_delegates_to_bootstrap(desktop) -> None:
    called: list[bool] = []
    desktop.monkeypatch.setattr(
        "coding_agent.desktop.bootstrap.run_desktop", lambda: called.append(True)
    )
    assert launcher.main(["--host-mode"]) == 0
    assert called == [True]


def test_setup_cancel_returns_one(desktop) -> None:
    desktop.monkeypatch.setattr(launcher, "run_setup", lambda: None)
    assert launcher.main(["--setup"]) == 1


def test_setup_error_is_wrapped(desktop) -> None:
    def boom():
        raise RuntimeError("cancelled")

    desktop.monkeypatch.setattr(launcher, "run_setup", boom)
    with pytest.raises(RuntimeError, match="配置向导失败"):
        launcher.main(["--setup"])


def test_missing_config_without_setup_runs_wizard(desktop) -> None:
    desktop.monkeypatch.setattr(
        LauncherConfig, "load", classmethod(lambda cls: None)
    )
    desktop.monkeypatch.setattr(launcher, "bundled_rootfs", lambda: None)
    wizard = LauncherConfig(
        distro="Ubuntu", workspace="/home/u/ws", python="/home/u/py"
    )
    desktop.monkeypatch.setattr(launcher, "run_setup", lambda: wizard)
    started = _install_servers(desktop)
    assert launcher.main([]) == 0
    assert desktop.saved[-1] is wizard
    assert started["agent"].port == 41001


def test_bundled_rootfs_is_imported_once(desktop) -> None:
    desktop.monkeypatch.setattr(
        LauncherConfig, "load", classmethod(lambda cls: None)
    )
    desktop.monkeypatch.setattr(
        launcher, "bundled_rootfs", lambda: Path("/rootfs.tar")
    )
    imported: list[tuple[str, str, str]] = []
    desktop.monkeypatch.setattr(launcher, "distro_exists", lambda name: False)
    desktop.monkeypatch.setattr(
        launcher,
        "import_distro",
        lambda name, install, rootfs: imported.append(
            (name, str(install), str(rootfs))
        ),
    )
    _install_servers(desktop)
    assert launcher.main([]) == 0
    assert imported and imported[0][0] == "CodingAgent"
    assert desktop.saved[-1].distro == "CodingAgent"


def test_existing_bundled_distro_is_not_reimported(desktop) -> None:
    desktop.monkeypatch.setattr(
        LauncherConfig, "load", classmethod(lambda cls: None)
    )
    desktop.monkeypatch.setattr(
        launcher, "bundled_rootfs", lambda: Path("/rootfs.tar")
    )
    desktop.monkeypatch.setattr(launcher, "distro_exists", lambda name: True)
    desktop.monkeypatch.setattr(
        launcher,
        "import_distro",
        lambda *args: pytest.fail("不应重复导入"),
    )
    _install_servers(desktop)
    assert launcher.main([]) == 0


def test_rootfs_import_failure_falls_back_to_wizard(desktop) -> None:
    desktop.monkeypatch.setattr(
        LauncherConfig, "load", classmethod(lambda cls: None)
    )
    desktop.monkeypatch.setattr(
        launcher, "bundled_rootfs", lambda: Path("/rootfs.tar")
    )
    desktop.monkeypatch.setattr(launcher, "distro_exists", lambda name: False)

    def boom(*args):
        raise OSError("tar broken")

    desktop.monkeypatch.setattr(launcher, "import_distro", boom)
    wizard = LauncherConfig(
        distro="Ubuntu", workspace="/home/u/ws", python="/home/u/py"
    )
    desktop.monkeypatch.setattr(launcher, "run_setup", lambda: wizard)
    _install_servers(desktop)
    assert launcher.main([]) == 0
    assert desktop.saved[-1] is wizard


def test_workspace_and_write_overrides_are_saved(desktop) -> None:
    desktop.monkeypatch.setattr(launcher, "to_wsl_path", lambda p: f"/mnt/{p}")
    _install_servers(desktop)
    assert launcher.main(["--workspace", "D:/ws", "--write"]) == 0
    assert desktop.config.workspace == "/mnt/D:/ws"
    assert desktop.config.allow_write is True
    assert desktop.saved


def test_second_instance_returns_two(desktop) -> None:
    desktop.monkeypatch.setattr(_FakeLock, "acquire", lambda self: False)
    assert launcher.main([]) == 2
    # 拿不到锁时直接返回，没有进入 try/finally，因此不会调用 release。
    assert _FakeLock.instances[-1].released is False


def test_agent_start_failure_returns_three(desktop) -> None:
    desktop.monkeypatch.setattr(
        launcher, "start_agent", lambda *a, **k: (_ for _ in ()).throw(OSError("no"))
    )
    assert launcher.main([]) == 3


def test_open_window_requires_webview(desktop) -> None:
    desktop.monkeypatch.setitem(sys.modules, "webview", None)
    with pytest.raises(RuntimeError, match="desktop"):
        launcher._open_window(None, None, desktop.config)


def test_failure_window_errors_are_swallowed(desktop) -> None:
    desktop.monkeypatch.setattr(
        launcher, "start_agent", lambda *a, **k: (_ for _ in ()).throw(OSError("no"))
    )
    module = _fake_webview()

    def broken_window(*args, **kwargs):
        raise RuntimeError("no display")

    module.create_window = broken_window
    desktop.monkeypatch.setitem(sys.modules, "webview", module)
    assert launcher.main([]) == 3


def test_control_start_failure_stops_agent_and_returns_three(desktop) -> None:
    agent = _Server()
    desktop.monkeypatch.setattr(launcher, "start_agent", lambda *a, **k: agent)
    desktop.monkeypatch.setattr(
        launcher,
        "start_control",
        lambda *a, **k: (_ for _ in ()).throw(OSError("busy")),
    )
    assert launcher.main([]) == 3
    assert agent.stopped is True


def _install_servers(desktop) -> dict[str, _Server]:
    agent = _Server(port=41001)
    control = _Server(port=8781, token="ctl")
    desktop.monkeypatch.setattr(launcher, "start_agent", lambda *a, **k: agent)
    desktop.monkeypatch.setattr(launcher, "start_control", lambda *a, **k: control)
    opened: list[tuple[Any, Any, Any]] = []
    desktop.monkeypatch.setattr(
        launcher, "_open_window", lambda c, a, cfg: opened.append((c, a, cfg))
    )
    return {"agent": agent, "control": control, "opened": opened}  # type: ignore[dict-item]


# ----------------------------------------------------------------------
# setup.py
# ----------------------------------------------------------------------


def _setup_api(monkeypatch, distros=("Ubuntu",), home_dir="/home/me", statuses=None):
    monkeypatch.setattr(setup, "list_distros", lambda: list(distros))
    monkeypatch.setattr(setup, "home", lambda distro: home_dir)
    outcomes = list(statuses or [(True, "deps ok")])
    monkeypatch.setattr(setup, "python_status", lambda distro, py: outcomes.pop(0))
    return setup.SetupApi()


def test_detect_without_distros(monkeypatch) -> None:
    api = _setup_api(monkeypatch, distros=())
    detected = api.detect()
    assert detected["distros"] == []
    assert detected["python_ok"] is False
    assert "未检测到" in detected["python_message"]


def test_detect_picks_first_working_python(monkeypatch) -> None:
    api = _setup_api(
        monkeypatch,
        statuses=[(False, "missing"), (True, "ok")],
    )
    detected = api.detect()
    assert detected["python_ok"] is True
    assert detected["python"] == "python3"
    assert "依赖检查通过" in detected["python_message"]


def test_detect_reports_last_failure(monkeypatch) -> None:
    api = _setup_api(
        monkeypatch,
        statuses=[(False, "no venv"), (False, "no python3")],
    )
    detected = api.detect()
    assert detected["python_ok"] is False
    assert "no python3" in detected["python_message"]


def test_detect_wraps_probe_exception(monkeypatch) -> None:
    api = _setup_api(monkeypatch)
    monkeypatch.setattr(
        setup, "home", lambda distro: (_ for _ in ()).throw(OSError("wsl down"))
    )
    detected = api.detect()
    assert "检测失败" in detected["python_message"]
    assert "OSError" in detected["python_message"]


def test_pick_folder_without_webview_returns_empty(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "webview", None)
    assert setup.SetupApi().pick_folder() == ""


def test_pick_folder_converts_selected_path(monkeypatch) -> None:
    picked = types.SimpleNamespace(
        windows=[types.SimpleNamespace(create_file_dialog=lambda kind: ["C:/ws"])]
    )
    module = _fake_webview()
    module.windows = picked.windows
    module.FileDialog = types.SimpleNamespace(FOLDER="folder")
    monkeypatch.setitem(sys.modules, "webview", module)
    assert setup.SetupApi().pick_folder() == "/mnt/c/ws"


def test_finish_rejects_invalid_json() -> None:
    result = setup.SetupApi().finish("{not json")
    assert result == {"ok": False, "errors": ["无效的配置"]}


def test_finish_returns_validation_errors(monkeypatch) -> None:
    api = setup.SetupApi()
    result = api.finish(
        {"distro": "", "workspace": "relative", "python": "python3"}
    )
    assert result["ok"] is False
    assert len(result["errors"]) == 3


def test_finish_checks_python_and_destroys_window(monkeypatch) -> None:
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(setup, "ensure_dir", lambda d, w: saved.append((d, w)))
    monkeypatch.setattr(setup, "python_status", lambda d, p: (True, "ok"))
    module = _fake_webview()
    destroyed: list[bool] = []
    module.windows = [types.SimpleNamespace(destroy=lambda: destroyed.append(True))]
    monkeypatch.setitem(sys.modules, "webview", module)
    api = setup.SetupApi()
    result = api.finish(
        {
            "distro": "Ubuntu",
            "workspace": "/home/me/ws",
            "python": "/home/me/py",
            "allow_write": True,
        }
    )
    assert result == {"ok": True}
    assert api.state.result is not None
    assert api.state.result.allow_write is True
    assert saved == [("Ubuntu", "/home/me/ws")]
    assert destroyed == [True]


def test_finish_tolerates_destroy_failure(monkeypatch) -> None:
    monkeypatch.setattr(setup, "ensure_dir", lambda d, w: None)
    monkeypatch.setattr(setup, "python_status", lambda d, p: (True, "ok"))
    module = _fake_webview()

    def boom() -> None:
        raise RuntimeError("window already gone")

    module.windows = [types.SimpleNamespace(destroy=boom)]
    monkeypatch.setitem(sys.modules, "webview", module)
    result = setup.SetupApi().finish(
        {"distro": "Ubuntu", "workspace": "/ws", "python": "/py"}
    )
    assert result == {"ok": True}


def test_finish_reports_unusable_python(monkeypatch) -> None:
    monkeypatch.setattr(setup, "ensure_dir", lambda d, w: None)
    monkeypatch.setattr(setup, "python_status", lambda d, p: (False, "missing"))
    result = setup.SetupApi().finish(
        {"distro": "Ubuntu", "workspace": "/ws", "python": "/py"}
    )
    assert result["ok"] is False
    assert "WSL Python 不可用" in result["errors"][0]


def test_finish_wraps_probe_exception(monkeypatch) -> None:
    monkeypatch.setattr(
        setup,
        "ensure_dir",
        lambda d, w: (_ for _ in ()).throw(OSError("cannot mkdir")),
    )
    result = setup.SetupApi().finish(
        {"distro": "Ubuntu", "workspace": "/ws", "python": "/py"}
    )
    assert result["ok"] is False
    assert "OSError" in result["errors"][0]


def test_run_setup_requires_webview(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "webview", None)
    with pytest.raises(RuntimeError, match="desktop"):
        setup.run_setup()


def test_run_setup_returns_api_result(monkeypatch) -> None:
    module = _fake_webview()
    config = LauncherConfig(
        distro="Ubuntu", workspace="/ws", python="/py", allow_write=False
    )

    def create_window(*args, **kwargs):
        kwargs["js_api"].state.result = config

    module.create_window = create_window
    monkeypatch.setitem(sys.modules, "webview", module)
    assert setup.run_setup() is config
