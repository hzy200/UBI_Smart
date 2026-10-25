"""Phase G：桌面/WSL 部署冒烟（RFC-0001，M4）。

检查目标发行版是否满足托管工作区的最低要求：

- 发行版存在、用户非 root；
- `/etc/wsl.conf` 的 `appendWindowsPath=false`；
- Python / git / tar 可用；
- 工作区在 ext4，而不是 `/mnt`。

不调用模型、不启动 A2/A3，也不写业务数据，因此可以在 CI 或新机器上先跑。
真正的运行链路由 `pytest -m wsl` 与桌面手工验收覆盖。

    python scripts/phase_g_smoke.py --distro Ubuntu
    python scripts/phase_g_smoke.py --workspace /home/agent/agent-ws/e2e
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.desktop import hardening  # noqa: E402
from coding_agent.desktop.wsl import (  # noqa: E402
    distro_exists,
    home,
    list_distros,
    run,
)


def _text(raw: bytes) -> str:
    return (raw or b"").decode("utf-8", errors="replace").strip()


def _probe(distro: str, script: str) -> tuple[int, str]:
    try:
        proc = run(distro, script, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"
    return int(proc.returncode), _text(proc.stdout or proc.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="桌面/WSL 部署冒烟（不调用模型）"
    )
    parser.add_argument("--distro", default="Ubuntu")
    parser.add_argument(
        "--workspace",
        default="",
        help="待检查的工作区；默认 <HOME>/.coding-agent/e2e-ws",
    )
    args = parser.parse_args(argv)

    distros = list_distros()
    if not distro_exists(args.distro):
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "WSL 发行版不存在",
                    "distro": args.distro,
                    "distros": distros,
                },
                ensure_ascii=False,
            )
        )
        return 2

    problems: list[str] = []
    warnings: list[str] = []
    report = hardening.check(args.distro)
    problems.extend(report.problems)
    warnings.extend(report.warnings)

    tools: dict[str, str] = {}
    for name, command in (
        ("python3", "python3 -V"),
        ("git", "git --version"),
        ("tar", "tar --version | head -n 1"),
        ("bwrap", "command -v bwrap || true"),
    ):
        code, text = _probe(args.distro, command)
        tools[name] = text
        if code != 0:
            if name == "bwrap":
                warnings.append("未找到 bwrap；本地/开发档可用，生产档需安装")
            else:
                problems.append(f"{name} 不可用：{text}")

    workspace = args.workspace.strip()
    if not workspace:
        workspace = f"{home(args.distro)}/.coding-agent/e2e-ws"
    if not workspace.startswith("/"):
        problems.append("工作区必须是 WSL 内的绝对路径")
    workspace_info: dict[str, str] = {"path": workspace}
    if workspace.startswith("/mnt/"):
        problems.append("工作区位于 /mnt；托管工作区必须在 ext4")
    code, text = _probe(
        args.distro,
        f"mkdir -p {shlex.quote(workspace)} && "
        f"df -T {shlex.quote(workspace)} | tail -n 1",
    )
    workspace_info["df"] = text
    if code != 0:
        problems.append(f"工作区不可创建：{text}")
    elif "ext4" not in text:
        warnings.append("工作区文件系统不是 ext4（请人工确认）")

    payload = {
        "ok": not problems,
        "distro": args.distro,
        "hardening": {
            "ok": report.ok,
            "uid": report.uid,
            "problems": report.problems,
            "warnings": report.warnings,
        },
        "tools": tools,
        "workspace": workspace_info,
        "problems": problems,
        "warnings": warnings,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
