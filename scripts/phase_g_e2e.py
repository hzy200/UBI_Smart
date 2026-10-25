"""Phase G：专用 WSL 发行版上的工作区端到端验收。

不调用模型。流程：

    创建托管工作区 → 从 /mnt 导入 → git revision + 归档
    → 会话绑定 → 回滚（自动生成 pre-rollback）

前置：专用发行版已导入，并且当前代码已安装进它的 venv：

    wsl --import CodingAgent <dir> packaging/wsl/CodingAgent-rootfs.tar
    wsl -d CodingAgent -u root -- /opt/coding-agent/venv/bin/pip install \
        --no-deps --force-reinstall /mnt/d/.../CodingAgent

    python scripts/phase_g_e2e.py --source "D:\\User\\e2e-src"
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from coding_agent.config import Settings  # noqa: E402
from coding_agent.control.agent_client import HttpAgentClient  # noqa: E402
from coding_agent.desktop import supervisor  # noqa: E402
from coding_agent.desktop.config import LauncherConfig  # noqa: E402
from coding_agent.desktop.wsl import run  # noqa: E402
from coding_agent.paths import win_to_wsl  # noqa: E402
from coding_agent.web import create_app  # noqa: E402
from coding_agent.web.auth import AuthConfig  # noqa: E402

TOKEN = "e2e-token"
USER = "alice"


def _text(raw: bytes) -> str:
    return (raw or b"").decode("utf-8", errors="replace").strip()


def _wsl_source(source: str) -> str:
    text = str(source or "").strip()
    if text.startswith("/"):
        return text
    return win_to_wsl(text)


def _wait(
    http: TestClient,
    path: str,
    headers: dict[str, str],
    *,
    timeout_s: int = 180,
) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        response = http.get(path, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(response.text)
        last = response.json()
        if last.get("status") in {"active", "failed", "cancelled"}:
            return last
        time.sleep(1.0)
    raise RuntimeError(f"任务超时：{last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="工作区端到端验收（无模型）")
    parser.add_argument("--distro", default="CodingAgent")
    parser.add_argument(
        "--python", default="/opt/coding-agent/venv/bin/python"
    )
    parser.add_argument(
        "--wsl-workspace",
        default="/home/agent/.coding-agent/workspaces/default",
    )
    parser.add_argument(
        "--workspace-root", default="/home/agent/agent-ws/users"
    )
    parser.add_argument(
        "--archive-root", default="/home/agent/.coding-agent/revisions"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument(
        "--second-source",
        default="",
        help="可选：第二次导入的源目录，用于验证 active 工作区追加 revision",
    )
    parser.add_argument("--agent-port", type=int, default=8890)
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.exists():
        print(
            json.dumps(
                {"ok": False, "error": f"源目录不存在：{source}"},
                ensure_ascii=False,
            )
        )
        return 2
    wsl_source = _wsl_source(str(source.resolve()))

    work = Path(tempfile.mkdtemp(prefix="coding-agent-e2e-"))
    os.environ.update(
        {
            "AGENT_WORKSPACE_REGISTRY": "true",
            "AGENT_WORKSPACE_ROOT": args.workspace_root,
            "AGENT_WORKSPACE_ARCHIVE_ROOT": args.archive_root,
            "AGENT_WORKSPACE_MOUNT_ROOTS": "/mnt/d",
            "AGENT_WSL_REQUIRE_HARDENING": "true",
        }
    )
    config = LauncherConfig(
        distro=args.distro,
        workspace=args.wsl_workspace,
        python=args.python,
        allow_write=True,
    )
    server = supervisor.start_agent(
        config,
        log_path=work / "agent.log",
        port=args.agent_port,
    )
    steps: dict = {"source": wsl_source, "agent_port": server.port}
    try:
        settings = Settings(
            _env_file=None,
            audit_dir=str(work / "audit"),
            checkpoint_path=str(work / "checkpoints.sqlite"),
            deepseek_api_key="sk-e2e-not-real",
            wsl_distro=args.distro,
            wsl_workspace=args.wsl_workspace,
            web_data_root=str(work / "web"),
            control_db_url=f"sqlite:///{work / 'control.db'}",
            workspace_registry=True,
            workspace_root=args.workspace_root,
            workspace_archive_root=args.archive_root,
            workspace_mount_roots="/mnt/d",
            workspace_scan_provider="none",
            agent_internal_url=f"http://127.0.0.1:{server.port}",
            agent_internal_token=server.token,
        )
        client = HttpAgentClient(
            settings.agent_internal_url, server.token
        )
        app = create_app(
            settings,
            auth=AuthConfig(tokens={TOKEN: USER}),
            agent_client=client,
        )
        headers = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(app) as http:
            created = http.post(
                "/api/workspaces",
                json={
                    "display_name": "E2E Project",
                    "source": "local_import",
                },
                headers=headers,
            )
            if created.status_code >= 400:
                raise RuntimeError(created.text)
            workspace = created.json()
            workspace_id = workspace["workspace_id"]
            steps["workspace"] = workspace

            started = http.post(
                f"/api/workspaces/{workspace_id}/imports",
                json={"source_path": wsl_source},
                headers=headers,
            )
            if started.status_code >= 400:
                raise RuntimeError(started.text)
            job = started.json()
            steps["import_started"] = job
            final = _wait(
                http,
                f"/api/workspaces/{workspace_id}/imports/{job['job_id']}",
                headers,
            )
            steps["import_final"] = final
            if final.get("status") != "active":
                raise RuntimeError(f"导入失败：{final}")

            revisions = http.get(
                f"/api/workspaces/{workspace_id}/revisions",
                headers=headers,
            ).json()["revisions"]
            steps["revisions"] = revisions
            if not revisions:
                raise RuntimeError("导入后没有 revision")

            second_source = str(args.second_source or "").strip()
            if second_source:
                second_path = Path(second_source)
                if not second_path.exists():
                    raise RuntimeError(
                        f"second-source 不存在：{second_path}"
                    )
                wsl_second = _wsl_source(
                    str(second_path.resolve())
                )
                started_second = http.post(
                    f"/api/workspaces/{workspace_id}/imports",
                    json={"source_path": wsl_second},
                    headers=headers,
                )
                if started_second.status_code >= 400:
                    raise RuntimeError(started_second.text)
                job_second = started_second.json()
                final_second = _wait(
                    http,
                    f"/api/workspaces/{workspace_id}/imports/"
                    f"{job_second['job_id']}",
                    headers,
                )
                steps["second_import"] = final_second
                if final_second.get("status") != "active":
                    raise RuntimeError(
                        f"第二次导入失败：{final_second}"
                    )
                revisions = http.get(
                    f"/api/workspaces/{workspace_id}/revisions",
                    headers=headers,
                ).json()["revisions"]
                steps["revisions_after_second"] = len(revisions)
                if len(revisions) < 2:
                    raise RuntimeError("第二次导入没有新增 revision")
                root = workspace["root_path"]
                check_second = run(
                    args.distro,
                    f"test -f {shlex.quote(root)}/VERSION2",
                )
                if check_second.returncode != 0:
                    raise RuntimeError("第二次导入的内容未生效")

            session = http.post(
                "/api/sessions",
                json={
                    "thread_id": "e2e-session",
                    "workspace_id": workspace_id,
                    "access_mode": "read_only",
                },
                headers=headers,
            )
            if session.status_code >= 400:
                raise RuntimeError(session.text)
            steps["session"] = session.json()

            root = workspace["root_path"]
            check = run(
                args.distro,
                " && ".join(
                    [
                        f"test -f {shlex.quote(root)}/README.md",
                        f"git -C {shlex.quote(root)} log -1 --format=%H",
                        f"test -d {shlex.quote(root)}/.git",
                    ]
                ),
            )
            steps["git_commit"] = _text(check.stdout)
            if check.returncode != 0:
                raise RuntimeError(
                    f"工作区校验失败：{_text(check.stderr)}"
                )

            target_revision = (
                revisions[-1]
                if str(args.second_source or "").strip()
                else revisions[0]
            )
            rollback = http.post(
                f"/api/workspaces/{workspace_id}/rollback",
                json={"revision_id": target_revision["revision_id"]},
                headers=headers,
            )
            if rollback.status_code >= 400:
                raise RuntimeError(rollback.text)
            steps["rollback"] = rollback.json()
            after = http.get(
                f"/api/workspaces/{workspace_id}/revisions",
                headers=headers,
            ).json()["revisions"]
            steps["revisions_after_rollback"] = len(after)
            if len(after) < 2:
                raise RuntimeError("回滚没有生成 pre-rollback revision")
            if str(args.second_source or "").strip():
                check_rollback = run(
                    args.distro,
                    f"test ! -f {shlex.quote(root)}/VERSION2",
                )
                if check_rollback.returncode != 0:
                    raise RuntimeError("回滚后第二个版本仍然存在")

        steps["ok"] = True
    except Exception as exc:  # noqa: BLE001 - 脚本边界
        steps["ok"] = False
        steps["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass

    print(json.dumps(steps, ensure_ascii=False))
    return 0 if steps.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
