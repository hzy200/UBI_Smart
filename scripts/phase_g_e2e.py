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
import secrets
import shlex
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from coding_agent.config import Settings  # noqa: E402
from coding_agent.control.agent_client import HttpAgentClient  # noqa: E402
from coding_agent.desktop import supervisor  # noqa: E402
from coding_agent.desktop.config import LauncherConfig  # noqa: E402
from coding_agent.desktop.wsl import run  # noqa: E402
from coding_agent.platform.paths import win_to_wsl  # noqa: E402
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


def _stream(
    http: TestClient,
    method: str,
    path: str,
    payload: dict,
    headers: dict[str, str],
) -> tuple[list[str], dict]:
    seen: list[str] = []
    last: dict = {}
    with http.stream(
        method, path, json=payload, headers=headers
    ) as response:
        if response.status_code >= 400:
            raise RuntimeError(response.text)
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            kind = str(event.get("type") or "")
            if kind:
                seen.append(kind)
            last = event
            if kind in {
                "run_finished",
                "run_failed",
                "approval_requested",
            }:
                break
    return seen, last


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
    parser.add_argument(
        "--real-model",
        action="store_true",
        help="使用 .env 中的 DeepSeek key 跑一次极简真实运行",
    )
    parser.add_argument(
        "--real-approval",
        action="store_true",
        help="真实运行并触发一次写工具审批，随后验证快照回滚",
    )
    parser.add_argument(
        "--hmac",
        action="store_true",
        help="启用 A2→A3 context HMAC 签名联调",
    )
    parser.add_argument(
        "--real-deny",
        action="store_true",
        help="真实模型请求 L3 命令并拒绝审批，验证命令未执行",
    )
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
    hmac_key = secrets.token_hex(32) if args.hmac else ""
    if hmac_key:
        os.environ["AGENT_INTERNAL_HMAC_KEY"] = hmac_key
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
    try:
        server = supervisor.start_agent(
            config,
            log_path=work / "agent.log",
            port=args.agent_port,
        )
    except Exception as exc:  # noqa: BLE001 - 启动失败也要输出结构化结果
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "start_agent",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 1
    steps: dict = {
        "source": wsl_source,
        "agent_port": server.port,
        "distro": args.distro,
        "hmac": bool(hmac_key),
    }
    try:
        env_settings = Settings()
        api_key = (
            env_settings.deepseek_api_key
            if args.real_model
            else "sk-e2e-not-real"
        )
        if args.real_model and not api_key:
            raise RuntimeError(".env 中没有 DEEPSEEK_API_KEY")
        settings = Settings(
            _env_file=None,
            audit_dir=str(work / "audit"),
            checkpoint_path=str(work / "checkpoints.sqlite"),
            deepseek_api_key=api_key,
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
            agent_internal_hmac_key=hmac_key,
        )
        client = HttpAgentClient(
            settings.agent_internal_url,
            server.token,
            hmac_key=hmac_key,
        )
        app = create_app(
            settings,
            auth=AuthConfig(tokens={TOKEN: USER}),
            agent_client=client,
            allow_write=bool(args.real_approval or args.real_deny),
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

            # 灾难恢复：破坏工作区后从 revision 归档恢复。
            run(
                args.distro,
                f"rm -f {shlex.quote(root)}/README.md",
            )
            restore = http.post(
                f"/api/workspaces/{workspace_id}/revisions/"
                f"{target_revision['revision_id']}/restore",
                headers=headers,
            )
            if restore.status_code >= 400:
                raise RuntimeError(restore.text)
            steps["restore"] = restore.json()
            check_restore = run(
                args.distro,
                f"test -f {shlex.quote(root)}/README.md",
            )
            if check_restore.returncode != 0:
                raise RuntimeError("归档恢复后 README 仍然缺失")
            after_restore = http.get(
                f"/api/workspaces/{workspace_id}/revisions",
                headers=headers,
            ).json()["revisions"]
            steps["revisions_after_restore"] = len(after_restore)

            # 浏览器式上传：创建 upload 工作区，multipart 归档经 A2 流式转发。
            upload_zip = work / "upload.zip"
            with zipfile.ZipFile(
                upload_zip, "w", zipfile.ZIP_DEFLATED
            ) as archive:
                archive.write(source / "README.md", "README.md")
                archive.writestr(
                    "UPLOADED.txt", "uploaded through the web API\n"
                )
            upload_ws = http.post(
                "/api/workspaces",
                json={
                    "display_name": "E2E Upload",
                    "source": "upload",
                },
                headers=headers,
            )
            if upload_ws.status_code >= 400:
                raise RuntimeError(upload_ws.text)
            upload_row = upload_ws.json()
            with upload_zip.open("rb") as handle:
                upload_started = http.post(
                    f"/api/workspaces/{upload_row['workspace_id']}/uploads",
                    files={
                        "archive": (
                            "upload.zip",
                            handle,
                            "application/zip",
                        )
                    },
                    headers=headers,
                )
            if upload_started.status_code >= 400:
                raise RuntimeError(upload_started.text)
            upload_job = upload_started.json()
            upload_final = _wait(
                http,
                f"/api/workspaces/{upload_row['workspace_id']}/uploads/"
                f"{upload_job['job_id']}",
                headers,
            )
            if upload_final.get("status") != "active":
                raise RuntimeError(f"上传失败：{upload_final}")
            check_upload = run(
                args.distro,
                "test -f "
                f"{shlex.quote(upload_row['root_path'])}/UPLOADED.txt",
            )
            if check_upload.returncode != 0:
                raise RuntimeError("上传内容未落到工作区")
            steps["upload"] = {
                "workspace": upload_row,
                "final": upload_final,
            }

            if args.real_deny:
                if not args.real_model:
                    raise RuntimeError("--real-deny 需要 --real-model")
                thread = "e2e-deny"
                sentinel = "/tmp/agent-e2e-nonexistent"
                run(
                    args.distro,
                    f"mkdir -p {shlex.quote(sentinel)} && "
                    f"touch {shlex.quote(sentinel)}/sentinel",
                )
                created = http.post(
                    "/api/sessions",
                    json={
                        "thread_id": thread,
                        "workspace_id": workspace_id,
                        "access_mode": "write_ask",
                    },
                    headers=headers,
                )
                if created.status_code >= 400:
                    raise RuntimeError(created.text)
                seen, last = _stream(
                    http,
                    "POST",
                    "/api/run",
                    {
                        "thread_id": thread,
                        "prompt": (
                            "请必须调用 shell_exec 工具执行命令："
                            f"rm -rf {sentinel}。"
                            "只调用工具，不要解释。"
                        ),
                    },
                    headers,
                )
                events = list(seen)
                levels: list[str] = []
                approvals = 0
                for _ in range(3):
                    if last.get("type") == "run_finished":
                        break
                    if last.get("type") != "approval_requested":
                        raise RuntimeError(f"预期 L3 审批：{last}")
                    levels.append(str(last.get("level") or ""))
                    request_id = str(last.get("request_id") or "")
                    if not request_id:
                        payload = http.get(
                            f"/api/approvals/{thread}", headers=headers
                        ).json()
                        items = payload.get("approvals") or []
                        request_id = str(
                            items[0].get("request_id") or ""
                        ) if items else ""
                    if not request_id:
                        raise RuntimeError("审批事件没有 request_id")
                    approvals += 1
                    seen, last = _stream(
                        http,
                        "POST",
                        f"/api/resume/{thread}",
                        {"decisions": {request_id: False}},
                        headers,
                    )
                    events.extend(seen)
                if last.get("type") != "run_finished":
                    raise RuntimeError(f"拒绝后运行未收尾：{last}")
                if not any("L3" in item for item in levels):
                    raise RuntimeError(f"审批级别不是 L3：{levels}")
                check_sentinel = run(
                    args.distro,
                    f"test -f {shlex.quote(sentinel)}/sentinel",
                )
                if check_sentinel.returncode != 0:
                    raise RuntimeError("L3 命令在拒绝后仍被执行")
                steps["real_deny"] = {
                    "approvals": approvals,
                    "levels": levels,
                    "sentinel": True,
                    "events": events[-12:],
                    "ok": True,
                }
            elif args.real_approval:
                if not args.real_model:
                    raise RuntimeError("--real-approval 需要 --real-model")
                thread = "e2e-approval"
                created = http.post(
                    "/api/sessions",
                    json={
                        "thread_id": thread,
                        "workspace_id": workspace_id,
                        "access_mode": "write_ask",
                    },
                    headers=headers,
                )
                if created.status_code >= 400:
                    raise RuntimeError(created.text)
                prompt = (
                    "请必须调用 file_write 工具（不是 shell_exec）"
                    "在当前工作区创建 approval.txt，内容为 ok。"
                    "不要只回复文字。"
                )
                seen, last = _stream(
                    http,
                    "POST",
                    "/api/run",
                    {"thread_id": thread, "prompt": prompt},
                    headers,
                )
                events = list(seen)
                approvals = 0
                for _ in range(3):
                    if last.get("type") == "run_finished":
                        break
                    if last.get("type") != "approval_requested":
                        raise RuntimeError(
                            f"预期审批，实际收到：{last}"
                        )
                    request_id = str(last.get("request_id") or "")
                    if not request_id:
                        payload = http.get(
                            f"/api/approvals/{thread}", headers=headers
                        ).json()
                        items = payload.get("approvals") or []
                        request_id = str(
                            items[0].get("request_id") or ""
                        ) if items else ""
                    if not request_id:
                        raise RuntimeError("审批事件没有 request_id")
                    approvals += 1
                    seen, last = _stream(
                        http,
                        "POST",
                        f"/api/resume/{thread}",
                        {"decisions": {request_id: True}},
                        headers,
                    )
                    events.extend(seen)
                if last.get("type") != "run_finished":
                    raise RuntimeError(f"审批恢复未完成：{last}")
                check_file = run(
                    args.distro,
                    f"test -f {shlex.quote(root)}/approval.txt",
                )
                if check_file.returncode != 0:
                    raise RuntimeError("审批后 approval.txt 未创建")
                snapshots = http.get(
                    f"/api/snapshots?thread_id={thread}",
                    headers=headers,
                ).json().get("snapshots", [])
                restore = http.post(
                    "/api/restore",
                    json={"thread_id": thread, "path": "approval.txt"},
                    headers=headers,
                )
                if restore.status_code >= 400:
                    raise RuntimeError(restore.text)
                check_gone = run(
                    args.distro,
                    f"test ! -f {shlex.quote(root)}/approval.txt",
                )
                if check_gone.returncode != 0:
                    raise RuntimeError("快照恢复后 approval.txt 仍存在")
                steps["real_approval"] = {
                    "approvals": approvals,
                    "snapshots": len(snapshots),
                    "restore": restore.json(),
                    "events": events[-12:],
                    "ok": True,
                }
            elif args.real_model:
                run_session = http.post(
                    "/api/sessions",
                    json={
                        "thread_id": "e2e-run",
                        "workspace_id": workspace_id,
                        "access_mode": "read_only",
                    },
                    headers=headers,
                )
                if run_session.status_code >= 400:
                    raise RuntimeError(run_session.text)
                seen: list[str] = []
                failed = ""
                with http.stream(
                    "POST",
                    "/api/run",
                    json={
                        "thread_id": "e2e-run",
                        "prompt": (
                            "只回复 OK，不要调用任何工具，"
                            "也不要读取任何文件。"
                        ),
                    },
                    headers=headers,
                ) as response:
                    if response.status_code >= 400:
                        raise RuntimeError(response.text)
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            payload = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        kind = str(payload.get("type") or "")
                        if kind:
                            seen.append(kind)
                        if kind == "run_failed":
                            failed = str(payload)
                        if kind == "approval_requested":
                            failed = "unexpected approval_requested"
                        if kind in {
                            "run_finished",
                            "run_failed",
                            "approval_requested",
                        }:
                            break
                steps["real_model"] = {
                    "events": seen[-12:],
                    "ok": bool(seen and seen[-1] == "run_finished"),
                }
                if failed or not steps["real_model"]["ok"]:
                    raise RuntimeError(
                        f"真实模型运行失败：{failed or seen[-5:]}"
                    )

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
