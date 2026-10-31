"""审计查看命令。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from coding_agent.audit import AuditLogger, read_records_many, verify_audit_files
from coding_agent.cli.context import console, get_settings


def audit(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近多少条记录"),
    thread_id: str = typer.Option("", "--thread-id", help="只显示指定会话"),
    path: str = typer.Option("", "--path", help="审计文件路径，默认用配置推导"),
    verify: bool = typer.Option(False, "--verify", help="验证审计哈希链"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """查看或验证审计日志。"""
    settings = get_settings()

    if path:
        paths = [Path(path)]
        label = str(Path(path))
    else:
        # 默认看当天全部片段（轮转后一天可能不止一个文件）
        logger = AuditLogger(settings.resolved_audit_dir)
        paths = logger.files_today()
        label = f"{settings.resolved_audit_dir}（{datetime.now(UTC):%Y-%m-%d}*.jsonl）"

    if verify:
        ok, message = verify_audit_files(paths)
        style = "green" if ok else "red"
        console.print(f"[{style}]审计哈希链：{escape(message)}[/]")
        if not ok:
            raise typer.Exit(code=1)
        return

    records = read_records_many(
        paths, thread_id=thread_id or None, limit=limit
    )

    if not records:
        if json_output:
            console.print_json("[]")
        else:
            console.print(f"[dim]没有审计记录：{label}[/]")
        return

    if json_output:
        console.print_json(
            json.dumps(
                [record.model_dump(mode="json") for record in records],
                ensure_ascii=False,
            )
        )
        return

    console.print(f"[dim]{label}（最近 {len(records)} 条）[/]\n")
    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("时间", "类型", "工具/对象", "级别", "判定", "结果"):
        table.add_column(column)

    for record in records:
        failed_verify = record.kind == "verify" and record.ok is False
        bad = (
            record.decision in ("rejected", "denied")
            or record.kind == "run_error"
            or failed_verify
        )
        table.add_row(
            record.ts[11:19],
            record.kind,
            record.tool or record.path or "",
            record.level,
            record.decision,
            _audit_outcome(record),
            style="red" if bad else "",
        )
    console.print(table)

def _audit_outcome(record) -> str:
    if record.kind == "tool_call":
        if record.decision in ("rejected", "denied"):
            # denied 是人工拒绝，rejected 是策略/路径守卫拒绝，都没执行
            return "已拒绝"
        if record.ok is None:
            return ""
        if not record.ok:
            # 文件工具没有退出码，失败原因在工具返回文本里，这里只标失败
            return f"失败 exit={record.exit_code}" if record.exit_code is not None else "失败"
        mark = "已批准" if record.decision == "approved" else "OK"
        return f"{mark} {record.duration_ms}ms" if record.duration_ms is not None else mark
    if record.kind in ("file_change", "rollback"):
        return f"+{record.added} -{record.removed}"
    if record.kind == "plan":
        return f"{len(record.steps)} 步"
    if record.kind == "verify":
        mark = "通过" if record.ok else "失败"
        return f"{mark} {escape(record.detail[:48])}"
    if record.kind == "repair":
        return escape(record.detail[:60])
    if record.kind == "run_error":
        return escape(record.detail[:60])
    return ""
