"""会话、快照与回滚命令。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from ubismart.cli.context import AgentRuntime, console, get_settings
from ubismart.cli.options import _WORKSPACE_OPTION
from ubismart.memory.sessions import SessionIndex, format_table_rows


def snapshots(
    workspace: str = _WORKSPACE_OPTION,
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近多少条"),
) -> None:
    """列出文件快照（每次写入/编辑前的留底）。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)
    entries = runtime.list_snapshots(limit=limit)
    if not entries:
        console.print(f"[dim]还没有任何快照。工作区 {runtime.workspace}[/]")
        return

    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("快照 id", "文件"):
        table.add_column(column)
    for entry in entries:
        table.add_row(entry.snapshot_id, escape(entry.path))
    console.print(f"[dim]工作区 {runtime.workspace}（最近 {len(entries)} 条）[/]\n")
    console.print(table)

def _resolve_undo_prefix(
    runtime: AgentRuntime, since: str, thread_id: str
) -> str:
    if since:
        prefix = runtime._snapshot_prefix(since)
        if not prefix:
            console.print(f"[red]无法解析时间：{escape(since)}[/]")
            raise typer.Exit(code=1)
        return prefix
    started = runtime.last_run_started_at(thread_id or None)
    if not started:
        console.print("[red]找不到最近一次运行记录。[/]")
        raise typer.Exit(code=1)
    return runtime._snapshot_prefix(started)


def _show_planned(planned: list[Any]) -> None:
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("快照 id")
    table.add_column("文件")
    for item in planned:
        table.add_row(item.snapshot_id, escape(item.path))
    console.print(f"[dim]将按新→旧回滚 {len(planned)} 处改动：[/]\n")
    console.print(table)
    console.print(
        "[dim]按时间过滤；同一工作区若有并发运行，可能包含其改动。"
        "回滚前每个文件都会先留底，仍可再次 undo。[/]"
    )


def _undo_run(
    runtime: AgentRuntime,
    *,
    since: str,
    thread_id: str,
    dry_run: bool,
    yes: bool,
) -> None:
    prefix = _resolve_undo_prefix(runtime, since, thread_id)
    planned = runtime.restore_since(prefix, dry_run=True)
    if not planned:
        console.print("[dim]该时间之后没有产生任何快照。[/]")
        return

    _show_planned(planned)
    if dry_run:
        return
    if not yes and not typer.confirm("确认回滚？", default=False):
        raise typer.Exit(code=1)

    results = runtime.restore_since(prefix)
    failed = [item for item in results if not item.ok]
    for item in results:
        color = "green" if item.ok else "red"
        console.print(f"[{color}]{escape(item.message)}[/]")
    if failed:
        raise typer.Exit(code=1)


def undo(
    workspace: str = _WORKSPACE_OPTION,
    snapshot_id: str = typer.Option("", "--snapshot", help="指定快照 id，默认回滚最近一次改动"),
    path: str = typer.Option("", "--path", help="只回滚这个文件"),
    last_run: bool = typer.Option(
        False, "--last-run", help="回滚最近一次运行产生的全部文件改动"
    ),
    since: str = typer.Option(
        "", "--since", help="回滚该 ISO 时间（审计里的 run_start）之后的全部改动"
    ),
    thread_id: str = typer.Option(
        "", "--thread-id", help="配合 --last-run 定位会话"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只列出将回滚的文件，不写盘"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
) -> None:
    """把文件回滚到某次修改前的状态。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)

    if last_run or since:
        _undo_run(
            runtime,
            since=since,
            thread_id=thread_id,
            dry_run=dry_run,
            yes=yes,
        )
        return

    result = runtime.restore(snapshot_id=snapshot_id or None, path=path or None)

    if not result.ok:
        console.print(f"[red]{escape(result.message)}[/]")
        raise typer.Exit(code=1)

    console.print(f"[green]{escape(result.message)}[/]")
    if result.undo_snapshot_id:
        console.print(f"[dim]回滚前的状态已留底：{result.undo_snapshot_id}[/]")
    if result.diff:
        console.print()
        console.print(_diff_text(result.diff))

def diff(
    workspace: str = _WORKSPACE_OPTION,
    snapshot_id: str = typer.Option("", "--snapshot", help="指定快照 id，默认最近一次改动"),
    path: str = typer.Option("", "--path", help="只看这个文件"),
) -> None:
    """查看某次改动之后文件变成了什么样（快照 → 当前）。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)
    text = runtime.diff_snapshot(snapshot_id=snapshot_id or None, path=path or None)
    if not text:
        console.print("[dim]没有可显示的快照，或内容与快照一致。[/]")
        return
    console.print(_diff_text(text))

def patch(
    thread_id: str = typer.Argument(..., help="会话 id（用 agent sessions 查）"),
    workspace: str = _WORKSPACE_OPTION,
    out: str = typer.Option("", "--out", "-o", help="写到文件而不是打印"),
) -> None:
    """把某个会话本轮的改动导出成一份补丁（`git apply` 可直接用）。

    与 `agent diff` 的区别：那个看的是"某一个快照"，这里看的是"这次运行
    总共改了什么"，同一文件只算一次（取最早那份快照与当前内容比较）。
    """
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)
    text = runtime.session_patch(thread_id)
    if not text:
        console.print("[dim]这个会话没有可导出的改动。[/]")
        return
    if out:
        Path(out).write_text(text, encoding="utf-8")
        console.print(f"[green]已写入 {out}[/]")
        return
    console.print(_diff_text(text))

def _diff_text(raw: str) -> Text:
    """给 diff 上色：加行绿、删行红、文件头加粗。"""
    styled = Text()
    for line in raw.splitlines():
        if line.startswith(("+++", "---")):
            styled.append(line + "\n", style="bold")
        elif line.startswith("+"):
            styled.append(line + "\n", style="green")
        elif line.startswith("-"):
            styled.append(line + "\n", style="red")
        elif line.startswith("@@"):
            styled.append(line + "\n", style="cyan")
        else:
            styled.append(line + "\n")
    return styled

def sessions(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近多少个会话"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """列出历史会话（从审计日志归纳）。"""
    settings = get_settings()
    index = SessionIndex(settings.resolved_audit_dir)
    found = index.list(limit=limit)

    if not found:
        if json_output:
            console.print_json("[]")
        else:
            console.print(
                f"[dim]没有历史会话记录：{settings.resolved_audit_dir}[/]"
            )
        return

    if json_output:
        payload = [
            {
                "thread_id": info.thread_id,
                "title": info.title,
                "workspace": info.workspace,
                "last_seen": info.last_seen,
                "prompts": info.prompts,
                "tool_calls": info.tool_calls,
            }
            for info in found
        ]
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("会话 id", "最近活跃", "提问", "工具调用", "内容"):
        table.add_column(column)
    for row in format_table_rows(found):
        table.add_row(row[0], row[1], row[2], row[3], escape(row[4]))
    console.print(table)
    console.print("\n[dim]用 agent run --thread-id <会话 id> \"…\" 继续该会话[/]")

def history(
    thread_id: str = typer.Option(..., "--thread-id", "-t", help="会话 id"),
    limit: int = typer.Option(0, "--limit", "-n", help="最多显示多少条；0 = 全部"),
    offset: int = typer.Option(0, "--offset", help="跳过开头多少条"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """查看某个会话的自然语言对话记录。"""

    async def _go():
        runtime = AgentRuntime(get_settings())
        try:
            return await runtime.history(
                thread_id,
                limit=limit or None,
                offset=max(0, offset),
            )
        finally:
            await runtime.aclose()

    try:
        messages = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001 - 会话不存在要给可读提示
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc

    if json_output:
        payload = [
            {"role": message.role, "text": message.text}
            for message in messages
        ]
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if not messages:
        console.print("[dim]该会话还没有对话记录。[/]")
        return
    for message in messages:
        label = "你" if message.role == "user" else "助手"
        style = "bold cyan" if message.role == "user" else "green"
        console.print(f"[{style}]{label} › [/]{escape(message.text)}")

def checkpoints(
    thread_id: str = typer.Option(..., "--thread-id", "-t", help="会话 id"),
    limit: int = typer.Option(20, "--limit", "-n", help="最多列出多少个"),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """列出某个会话的 checkpoint（新的在前），供 fork/rewind 选落点。"""

    async def _go() -> list[dict[str, Any]]:
        runtime = AgentRuntime(get_settings(), workspace=workspace or None)
        try:
            return await runtime.checkpoints(thread_id, limit=limit)
        finally:
            await runtime.aclose()

    try:
        items = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001 - 会话/checkpoint 不存在要给可读提示
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if not items:
        console.print("[dim]没有 checkpoint。[/]")
        return
    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("checkpoint id", "step", "内容"):
        table.add_column(column)
    for item in items:
        table.add_row(
            item["checkpoint_id"],
            str(item["step"]),
            escape(item["preview"]),
        )
    console.print(table)

def fork(
    thread_id: str = typer.Option(..., "--thread-id", "-t", help="源会话 id"),
    checkpoint_id: str = typer.Option(
        "", "--checkpoint", "-c", help="从指定 checkpoint 分叉；省略用当前状态"
    ),
    new_thread_id: str = typer.Option(
        "", "--new-thread-id", help="新会话 id；省略自动生成"
    ),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """从当前或指定 checkpoint 分叉出一个新会话。"""

    async def _go() -> str:
        runtime = AgentRuntime(get_settings(), workspace=workspace or None)
        try:
            return await runtime.fork(
                thread_id,
                checkpoint_id=checkpoint_id,
                new_thread_id=new_thread_id,
            )
        finally:
            await runtime.aclose()

    try:
        target = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]已分叉到 {escape(target)}[/]")
    console.print(f'[dim]继续该会话：agent run --thread-id {escape(target)} "…"[/]')

def rewind(
    thread_id: str = typer.Option(..., "--thread-id", "-t", help="会话 id"),
    steps: int = typer.Option(1, "--steps", "-n", help="回退多少个 checkpoint"),
    checkpoint_id: str = typer.Option(
        "", "--checkpoint", "-c", help="回退到指定 checkpoint；优先于 --steps"
    ),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """把会话回退到更早的 checkpoint（历史保留，可再次找回）。"""

    async def _go() -> str:
        runtime = AgentRuntime(get_settings(), workspace=workspace or None)
        try:
            return await runtime.rewind(
                thread_id, checkpoint_id=checkpoint_id, steps=steps
            )
        finally:
            await runtime.aclose()

    try:
        new_checkpoint = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]已回退，新 checkpoint：{escape(new_checkpoint)}[/]")
