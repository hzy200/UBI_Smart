"""A2/A3/A4 系统级端到端验收（本地手动执行）。

`scripts/phase_g_e2e.py` 已经覆盖完整链路，但它是一个脚本，CI 里没有
“固定入口”。这个用例把脚本输出当作结构化结果来断言：

    导入 → 第二次导入 → 会话绑定 → 回滚 → 归档恢复 → 浏览器式上传
    → HMAC → 真实模型运行 → 真实审批暂停/恢复 → 快照回滚

**默认不执行**：需要专用 WSL 发行版、已安装进该发行版的当前代码、以及
`.env` 中的 DEEPSEEK_API_KEY。显式运行：

```bash
AGENT_SYSTEM_E2E=1 AGENT_WSL_DISTRO=CodingAgent \
  pytest -m system_e2e tests/integration/test_system_e2e.py -q
# 或 make system-e2e
```

标记同时带 `llm`，所以 `pytest -m "not llm"` 与 PR CI 都会跳过。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox

ROOT = Path(__file__).resolve().parents[2]

# WSL 检测到指向 localhost 的 Windows 代理时会拒绝启动（NAT 模式不支持）。
# 本机开发环境常驻 HTTP_PROXY/ALL_PROXY；系统 E2E 需要把这类变量从子进程
# 环境里摘掉，让 WSL 走自己的网络栈。
_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)

pytestmark = [
    pytest.mark.llm,
    pytest.mark.system_e2e,
]


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _PROXY_KEYS:
        env.pop(key, None)
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _last_json(stdout: str) -> dict | None:
    """脚本最后一行是 JSON；前面的 supervisor 日志不能干扰解析。"""
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not (text.startswith("{") and text.endswith("}")):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _sources(base: Path) -> tuple[Path, Path]:
    """源目录必须位于 /mnt/d 下：脚本把可挂载根固定为 /mnt/d。"""
    first = Path(tempfile.mkdtemp(prefix="system-e2e-src-", dir=base))
    (first / "README.md").write_text("v1\n", encoding="utf-8")
    second = Path(tempfile.mkdtemp(prefix="system-e2e-src2-", dir=base))
    (second / "README.md").write_text("v2\n", encoding="utf-8")
    (second / "VERSION2").write_text("2\n", encoding="utf-8")
    return first, second


def test_phase_g_system_e2e() -> None:
    if os.environ.get("AGENT_SYSTEM_E2E") != "1":
        pytest.skip("设置 AGENT_SYSTEM_E2E=1 才执行系统级 E2E")

    settings = Settings()
    distro = os.environ.get("AGENT_WSL_DISTRO", settings.wsl_distro)
    if not WslSandbox.available(distro):
        pytest.skip(f"WSL 发行版 {distro} 不可用")
    if not settings.deepseek_api_key:
        pytest.skip("未配置 DEEPSEEK_API_KEY")

    base = ROOT / ".agent"
    base.mkdir(exist_ok=True)
    first, second = _sources(base)
    try:
        command = [
            sys.executable,
            str(ROOT / "scripts" / "phase_g_e2e.py"),
            "--source",
            str(first),
            "--second-source",
            str(second),
            "--distro",
            distro,
            "--agent-port",
            str(_free_port()),
            "--real-model",
            "--real-approval",
            "--hmac",
        ]
        proc = subprocess.run(
            command,
            cwd=ROOT,
            env=_clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            check=False,
        )
        steps = _last_json(proc.stdout)
        assert steps is not None, (
            f"脚本没有输出 JSON\nstdout:\n{proc.stdout[-3000:]}\n"
            f"stderr:\n{proc.stderr[-3000:]}"
        )
        assert proc.returncode == 0 and steps.get("ok") is True, steps

        assert steps["import_final"]["status"] == "active"
        assert steps["second_import"]["status"] == "active"
        assert steps["revisions_after_second"] >= 2
        assert steps["revisions_after_rollback"] >= 2
        assert steps["revisions_after_restore"] >= steps[
            "revisions_after_rollback"
        ]
        assert steps["upload"]["final"]["status"] == "active"
        assert steps["git_commit"]
        assert steps["hmac"] is True

        approval = steps["real_approval"]
        assert approval["ok"] is True
        assert approval["approvals"] >= 1
        assert approval["snapshots"] >= 1
        assert approval["restore"]
    finally:
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)
