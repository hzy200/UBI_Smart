"""长期记忆命令。"""

from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

from ubismart.cli.context import AgentRuntime, console, get_settings
from ubismart.cli.options import _WORKSPACE_OPTION


def _print_vector_status(runtime: AgentRuntime, scope: str) -> None:
    status = runtime.memory.status(scope=scope)
    console.print(
        f"[bold]向量检索[/] {'启用' if status['enabled'] else '未启用'}"
    )
    console.print(
        f"provider={status['provider'] or '—'} "
        f"model={status['model'] or '—'} "
        f"dim={status['dimension'] or '—'}"
    )
    if status["error"]:
        console.print(f"[yellow]{escape(status['error'])}[/]")
    info = status["scopes"].get(scope) or {}
    console.print(
        f"{scope}: facts={info.get('facts', 0)} "
        f"vectors={info.get('vectors', 0)} "
        f"missing={info.get('missing', 0)} "
        f"header_ok={info.get('header_ok', False)}"
    )


def _run_reindex(runtime: AgentRuntime, scope: str, force: bool) -> None:
    result = runtime.memory.reindex(scope=scope, force=force)
    for name, info in result.items():
        console.print(
            f"[green]{name}：索引 {info['indexed']} 条，"
            f"清理 {info['pruned']} 条，当前 {info['facts']} 条[/]"
        )


def _show_history(runtime: AgentRuntime, scope: str) -> None:
    entries = runtime.memory.history(limit=20, scope=scope)
    if not entries:
        console.print("[dim]还没有记忆备份。[/]")
        return
    table = Table(show_header=True, header_style="bold", box=None)
    for column in ("备份 id", "时间"):
        table.add_column(column)
    for entry in entries:
        table.add_row(entry["backup_id"], entry["created_at"])
    console.print(table)


def _show_conflicts(runtime: AgentRuntime, scope: str) -> None:
    conflicts = runtime.memory.conflicts(scope=scope)
    if not conflicts:
        console.print("[dim]没有标记为冲突的记忆。[/]")
        return
    for item in conflicts:
        console.print(
            f"[yellow]{escape(str(item['fact']))}[/]"
            f" ↔ {escape('；'.join(item['conflicts_with']))}"
        )


def _parse_priority(priority: str) -> tuple[str, str, int, int]:
    raw_index, sep, raw_value = priority.partition("=")
    if not sep:
        raise ValueError("--priority 格式应为 N=值")
    return raw_index, raw_value, int(raw_index), int(raw_value)


def _apply_mutation(
    runtime: AgentRuntime,
    *,
    restore: str,
    clear: bool,
    add: str,
    forget: int,
    pin: int,
    unpin: int,
    priority: str,
    thread_id: str,
    reason: str,
    ttl_days: int,
    scope: str,
) -> None:
    if restore:
        runtime.restore_memory(restore, thread_id=thread_id, scope=scope)
        console.print(f"[green]已恢复记忆备份 {escape(restore)}[/]")
    elif clear:
        runtime.clear_memory(thread_id=thread_id, scope=scope)
        console.print(f"[green]已清空{scope}记忆[/]")
    elif add:
        runtime.remember(
            add,
            thread_id=thread_id,
            reason=reason,
            ttl_days=ttl_days or None,
            scope=scope,
        )
        replaced = getattr(runtime.memory, "last_replaced", [])
        related = getattr(runtime.memory, "last_related", [])
        suffix = (
            f"（已替换旧版本：{'；'.join(replaced)}）" if replaced else ""
        )
        if related:
            suffix += (
                f"（可能相关、未替换：{'；'.join(related)}；"
                "可用 --conflicts 查看）"
            )
        console.print(f"[green]已记住{suffix}[/]")
    elif forget:
        runtime.forget(forget, thread_id=thread_id, scope=scope)
        console.print(f"[green]已删除第 {forget} 条[/]")
    elif pin:
        runtime.pin_memory(pin, pinned=True, thread_id=thread_id, scope=scope)
        console.print(f"[green]已置顶第 {pin} 条[/]")
    elif unpin:
        runtime.pin_memory(
            unpin, pinned=False, thread_id=thread_id, scope=scope
        )
        console.print(f"[green]已取消置顶第 {unpin} 条[/]")
    elif priority:
        raw_index, raw_value, index, value = _parse_priority(priority)
        runtime.set_memory_priority(
            index,
            value,
            thread_id=thread_id,
            scope=scope,
        )
        console.print(
            f"[green]已设置第 {raw_index} 条优先级为 {raw_value}[/]"
        )


def _show_facts(runtime: AgentRuntime, scope: str) -> None:
    facts = runtime.memories(scope=scope)
    skipped = getattr(runtime.memory, "last_unsafe", [])
    if skipped:
        console.print(
            f"[yellow]检测到 {len(skipped)} 条疑似密钥，"
            "它们不会注入模型。[/]"
        )
    path_fn = getattr(runtime.memory, "path_for", None)
    path = path_fn(scope) if callable(path_fn) else runtime.memory.path
    if not facts:
        console.print(f"[dim]当前没有{scope}记忆。文件位置：{path}[/]")
        return

    console.print(f"[dim]{path}（{len(facts)} 条）[/]\n")
    for index, fact in enumerate(facts, start=1):
        console.print(f"  {index}. {escape(fact)}")


def memory(
    add: str = typer.Option("", "--add", help="新增一条项目记忆"),
    forget: int = typer.Option(0, "--forget", help="删除第 N 条（序号从 1 开始）"),
    clear: bool = typer.Option(False, "--clear", help="清空全部记忆"),
    ttl_days: int = typer.Option(
        0, "--ttl-days", help="新增记忆的过期天数；0 = 长期有效"
    ),
    reason: str = typer.Option("", "--reason", help="记录新增原因（写入元数据）"),
    thread_id: str = typer.Option(
        "", "--thread-id", help="关联会话 id（写入审计）"
    ),
    show_history: bool = typer.Option(
        False, "--history", help="列出最近的记忆备份"
    ),
    restore: str = typer.Option(
        "", "--restore", help="恢复到指定备份 id"
    ),
    pin: int = typer.Option(0, "--pin", help="置顶第 N 条记忆"),
    unpin: int = typer.Option(0, "--unpin", help="取消置顶第 N 条记忆"),
    priority: str = typer.Option(
        "", "--priority", help="设置优先级：N=值（值 -10..10）"
    ),
    show_conflicts: bool = typer.Option(
        False, "--conflicts", help="列出可能相关/冲突的记忆"
    ),
    global_scope: bool = typer.Option(
        False, "--global", help="操作跨工作区全局记忆（默认项目记忆）"
    ),
    vector_status: bool = typer.Option(
        False, "--vector-status", help="查看向量索引状态"
    ),
    reindex: bool = typer.Option(
        False, "--reindex", help="为缺失/过期的事实重建向量"
    ),
    reindex_force: bool = typer.Option(
        False, "--reindex-force", help="强制重建全部向量"
    ),
    workspace: str = _WORKSPACE_OPTION,
) -> None:
    """查看或维护跨会话的项目/全局记忆。"""
    runtime = AgentRuntime(get_settings(), workspace=workspace or None)
    scope = "global" if global_scope else "project"

    if vector_status:
        _print_vector_status(runtime, scope)
        return

    if reindex or reindex_force:
        _run_reindex(runtime, scope, reindex_force)
        return

    if show_history:
        _show_history(runtime, scope)
        return

    if show_conflicts:
        _show_conflicts(runtime, scope)
        return

    try:
        _apply_mutation(
            runtime,
            restore=restore,
            clear=clear,
            add=add,
            forget=forget,
            pin=pin,
            unpin=unpin,
            priority=priority,
            thread_id=thread_id,
            reason=reason,
            ttl_days=ttl_days,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001 - 记忆文件读写失败要给可读提示
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc

    _show_facts(runtime, scope)
