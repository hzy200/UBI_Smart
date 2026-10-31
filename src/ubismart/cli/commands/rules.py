"""命令放行规则命令。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.markup import escape

from ubismart.cli.context import console, get_settings
from ubismart.config import apply_security_profile

_RULES_ADD_OPTION = typer.Option(
    None, "--add", help="添加规则，可重复，如 shell_exec:git status*"
)

_RULES_REMOVE_OPTION = typer.Option(
    None, "--remove", help="删除规则，可重复"
)

def _split_allow_rules(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]

def _read_env_allow_rules(path: Path, fallback: str) -> list[str]:
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("AGENT_ALLOW_RULES="):
                    return _split_allow_rules(line.split("=", 1)[1])
        except OSError:
            pass
    return _split_allow_rules(fallback)

def _write_env_allow_rules(path: Path, rules: list[str]) -> None:
    """只改 AGENT_ALLOW_RULES 一行，保留其余 .env 内容。"""
    line = "AGENT_ALLOW_RULES=" + ",".join(rules)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, existing in enumerate(lines):
        if existing.startswith("AGENT_ALLOW_RULES="):
            lines[index] = line
            replaced = True
            break
    if not replaced:
        lines.append(line)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)

def rules(
    add: list[str] | None = _RULES_ADD_OPTION,
    remove: list[str] | None = _RULES_REMOVE_OPTION,
    clear: bool = typer.Option(False, "--clear", help="清空全部放行规则"),
    env_file: str = typer.Option(
        ".env", "--env-file", help="要维护的 env 文件"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """查看或维护 L2 持久放行规则（AGENT_ALLOW_RULES）。"""
    settings = apply_security_profile(get_settings())
    path = Path(env_file)
    current = _read_env_allow_rules(path, settings.allow_rules)
    additions = [item.strip() for item in (add or []) if item.strip()]
    removals = {item.strip() for item in (remove or []) if item.strip()}
    for rule in additions:
        if ":" not in rule:
            console.print(
                f"[red]规则格式应为 tool:pattern：{escape(rule)}[/]"
            )
            raise typer.Exit(code=2)

    changed = bool(additions or removals or clear)
    updated = [] if clear else list(current)
    for rule in additions:
        if rule not in updated:
            updated.append(rule)
    if removals:
        updated = [rule for rule in updated if rule not in removals]

    if changed:
        try:
            _write_env_allow_rules(path, updated)
        except OSError as exc:
            console.print(f"[red]写入 {escape(str(path))} 失败：{exc}[/]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]已更新 {escape(str(path))} 的 AGENT_ALLOW_RULES"
            "（新进程生效）[/]"
        )

    if json_output:
        console.print_json(
            json.dumps({"allow_rules": updated}, ensure_ascii=False)
        )
        return
    if not updated:
        console.print("[dim]当前没有持久放行规则。[/]")
        return
    console.print("[bold]当前持久放行规则[/]")
    for rule in updated:
        console.print(f"  {escape(rule)}")
    if str(settings.security_profile or "").lower() != "local":
        console.print(
            "[yellow]注意：untrusted/production profile 会忽略 allow_rules。[/]"
        )
