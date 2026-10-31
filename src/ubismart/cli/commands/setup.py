"""环境自检与工作区初始化命令。"""

from __future__ import annotations

import importlib.metadata as metadata
import sys
from pathlib import Path

import typer
from rich.markup import escape

from ubismart.cli.context import console, get_settings
from ubismart.cli.platform import (
    SandboxProbe,
    SandboxProbeError,
    resolve_workspace,
)
from ubismart.config import Settings, shadowed_env_keys
from ubismart.memory.embeddings import embedding_status

_DISTRIBUTIONS = (
    "langgraph",
    "langchain-core",
    "langchain-openai",
    "langsmith",
    "langgraph-checkpoint-sqlite",
)

def _report(label: str, passed: bool, detail: str = "") -> bool:
    tag = "[green]PASS[/]" if passed else "[red]FAIL[/]"
    suffix = f" — {escape(detail)}" if detail else ""
    console.print(f"{tag}  {label}{suffix}")
    return passed

def _warn(label: str, detail: str = "") -> None:
    """提示性输出：影响使用但不该让自检直接失败。"""
    suffix = f" — {escape(detail)}" if detail else ""
    console.print(f"[yellow]WARN[/]  {label}{suffix}")

def _check_langsmith(settings: Settings) -> list[bool]:
    """追踪要么真正可用，要么提前说清楚。

    凭据无效时 langsmith 会在**每次调用后**往 stderr 打一串 ingestion 失败 ——
    与其让用户在一堆噪音里猜，不如在这里一次性验掉。
    """
    if not settings.langsmith_tracing:
        return [_report("LangSmith 追踪", True, f"关闭 · project={settings.langsmith_project}")]

    if not settings.langsmith_api_key:
        return [_report("LangSmith 追踪", False, "已开启但缺 LANGSMITH_API_KEY")]

    try:
        from langsmith import Client

        client = Client()
        project = next(iter(client.list_projects(limit=1)), None)
    except Exception as exc:  # noqa: BLE001 - 凭据/网络问题都要给出可读提示
        return [
            _report(
                "LangSmith 追踪",
                False,
                f"凭据或网络不可用（{type(exc).__name__}）。"
                f"追踪会静默失败并往 stderr 打错误，建议先 LANGSMITH_TRACING=false",
            )
        ]

    detail = f"可用 · project={settings.langsmith_project}"
    if project is not None and getattr(project, "name", "") != settings.langsmith_project:
        detail += f"（该凭据下可见的第一个项目是 {project.name}）"
    return [_report("LangSmith 追踪", True, detail)]

def _check_host_isolation(settings: Settings) -> list[bool]:
    """A2L 隔离能力：可选能力 WARN，配置依赖缺失才 FAIL。"""
    from ubismart.host_broker.availability import probe_isolation

    if not settings.host_broker_enabled:
        return [_report("A2L host 隔离", True, "未启用")]
    availability = probe_isolation()
    results = [
        _report(
            "A2L Job Object",
            availability.job_object,
            availability.reasons.get("job_object", ""),
        ),
        _report(
            "A2L AppContainer",
            availability.appcontainer,
            availability.reasons.get("appcontainer", ""),
        ),
        _report(
            "A2L 低完整性",
            availability.low_integrity,
            availability.reasons.get("low_integrity", ""),
        ),
    ]
    tier = str(settings.host_broker_security_tier or "")
    if tier == "appcontainer" and not availability.appcontainer:
        results.append(
            _report(
                "A2L 安全档",
                False,
                "配置为 appcontainer 但当前机器不可用",
            )
        )
    if settings.host_broker_exec_enabled:
        if not availability.appcontainer:
            results.append(
                _report(
                    "A2L host.exec",
                    False,
                    "已启用但 AppContainer 不可用；exec 会 fail closed",
                )
            )
        elif tier != "appcontainer":
            _warn(
                "A2L host.exec",
                f"已启用但 security_tier={tier}；exec 只在 appcontainer 档工作",
            )
    if availability.tpm:
        _warn("A2L TPM", "可用（CNG Platform Crypto Provider）")
    else:
        _warn(
            "A2L TPM",
            availability.reasons.get(
                "tpm", "不可用；设备证明会回退软件密钥"
            ),
        )
    if availability.software_attestation:
        _warn("A2L 软件设备证明", "可用")
    elif str(settings.host_broker_attestation or "off") != "off":
        results.append(
            _report(
                "A2L 软件设备证明",
                False,
                availability.reasons.get(
                    "software_attestation", "cryptography 或密钥目录不可用"
                ),
            )
        )
    else:
        _warn(
            "A2L 软件设备证明",
            availability.reasons.get(
                "software_attestation", "不可用"
            ),
        )
    return results

def _check_memory_vectors(settings: Settings) -> list[bool]:
    ok, detail = embedding_status(settings)
    return [_report("记忆向量检索", ok, detail)]

def _check_file_locks(settings: Settings) -> list[bool]:
    """探测审计目录是否支持 OS 文件锁。

    不支持时不阻断运行：审计/steering/journal 是协调数据，降级为线程互斥
    仍然可用；但必须让用户知道跨进程保护已经失效。
    """
    from ubismart.platform.filelock import FileLock

    path = settings.resolved_audit_dir / ".doctor.lock"
    try:
        lock = FileLock(path)
        with lock:
            degraded = lock.degraded
    except Exception as exc:  # noqa: BLE001 - 自检只报告，不抛栈
        return [
            _report(
                "跨进程文件锁",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        ]
    finally:
        try:
            path.unlink()
        except OSError:
            pass

    if degraded:
        _warn(
            "跨进程文件锁",
            "OS 锁不可用：审计/steering/journal 只有线程互斥（多进程保护降级）",
        )
        return []
    return [_report("跨进程文件锁", True, "OS 文件锁可用")]

def doctor() -> None:
    """检查运行环境：Python、依赖、API Key、WSL 沙箱。"""
    settings = get_settings()
    results: list[bool] = []

    console.print("[bold]环境自检[/]\n")

    shadowed = shadowed_env_keys()
    if shadowed:
        _warn(
            "环境变量被 .env 覆盖",
            f"{', '.join(shadowed)} 在环境变量里也有，但优先级是「.env > 环境变量」，"
            f"当前生效的是 .env 里那份。若你以为在生效的是环境变量，请改 .env",
        )
        console.print()

    v = sys.version_info
    results.append(_report("Python 版本", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}"))

    for dist in _DISTRIBUTIONS:
        try:
            version = metadata.version(dist)
            results.append(_report(f"依赖 {dist}", True, version))
        except metadata.PackageNotFoundError:
            results.append(_report(f"依赖 {dist}", False, "未安装，请 pip install -e ."))

    results.append(
        _report(
            "DEEPSEEK_API_KEY",
            bool(settings.deepseek_api_key),
            "已配置" if settings.deepseek_api_key else "未配置（agent chat/run 不可用）",
        )
    )
    results.extend(_check_langsmith(settings))
    results.extend(_check_memory_vectors(settings))
    results.extend(_check_file_locks(settings))
    results.extend(_check_host_isolation(settings))
    profile = str(settings.security_profile or "local").strip().lower()
    results.append(
        _report(
            "安全 profile",
            True,
            f"{profile} · network={'on' if settings.shell_network else 'off'} · "
            f"repo_instructions={settings.trust_repo_instructions} · "
            f"repo_memory={settings.trust_repo_memory}",
        )
    )

    distros = SandboxProbe.list_distros()
    has_wsl = settings.wsl_distro in distros
    results.append(
        _report(
            f"WSL 发行版 {settings.wsl_distro}",
            has_wsl,
            f"已安装：{', '.join(distros) or '无'}" if distros else "WSL 不可用或未安装发行版",
        )
    )

    if has_wsl:
        sandbox = SandboxProbe(settings)
        workspace = resolve_workspace(settings, sandbox)
        # bwrap 的 workdir 读 sandbox.settings；配置留空时写回推导值。
        sandbox.settings = settings.model_copy(
            update={"wsl_workspace": workspace}
        )

        healthy = sandbox.health(force=True)
        results.append(
            _report(
                "WSL 健康探针",
                healthy,
                f"circuit={sandbox.circuit_state()}",
            )
        )

        try:
            ident = sandbox.identity()
            results.append(
                _report(
                    "沙箱执行探针",
                    True,
                    f"{ident.user or '未知用户'} @ {ident.kernel or '未知内核'}",
                )
            )
        except SandboxProbeError as exc:
            ident = None
            results.append(_report("沙箱执行探针", False, str(exc)))
        user = ident.user if ident is not None else ""
        results.append(
            _report("沙箱运行用户", user not in {"", "root"}, f"{user or '未知'}（不应为 root）")
        )

        try:
            backend = bool(sandbox.which("rg"))
        except SandboxProbeError:
            backend = False
        results.append(
            _report(
                "检索后端",
                True,
                "ripgrep" if backend else "grep（未装 ripgrep，大仓库会慢一些）",
            )
        )

        found = sandbox.dir_exists(workspace)
        results.append(
            _report(
                f"工作区 {workspace}",
                found,
                "存在" if found else "不存在，运行 agent sandbox-init 创建",
            )
        )

        if settings.shell_sandbox == "bwrap":
            available = sandbox.bwrap_available()
            results.append(
                _report(
                    "shell 隔离 (bwrap)",
                    available,
                    "可用" if available else "已配置但不可用：agent run 会拒绝执行",
                )
            )
        else:
            results.append(_report("shell 隔离", True, "off（仅工作区 cwd 约束）"))

    console.print()
    if all(results):
        console.print("[green bold]全部通过[/]")
    else:
        console.print("[red bold]存在未通过项[/]")
        raise typer.Exit(code=1)

def sandbox_init() -> None:
    """创建并校验沙箱工作区目录。"""
    settings = get_settings()
    sandbox = SandboxProbe(settings)

    if not SandboxProbe.available(settings.wsl_distro):
        console.print(f"[red]WSL 发行版 {settings.wsl_distro} 不可用[/]")
        raise typer.Exit(code=1)

    try:
        user = sandbox.identity().user
    except SandboxProbeError as exc:
        console.print(f"[red]无法探活沙箱[/]\n{escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    if user in {"", "root"}:
        console.print("[red]沙箱当前以 root 运行，隔离形同虚设。请改用普通用户。[/]")
        raise typer.Exit(code=1)

    workspace = resolve_workspace(settings, sandbox)
    try:
        resolved = sandbox.ensure_dir(workspace)
    except SandboxProbeError as exc:
        console.print(f"[red]创建工作区失败[/]\n{escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]工作区就绪[/] {escape(resolved)}（用户 {escape(user)}）")
    if not settings.wsl_workspace:
        console.print("[dim]当前按 $HOME 自动推导。如需固定，请写入 .env：[/]")
        console.print(f"[dim]AGENT_WSL_WORKSPACE={escape(resolved)}[/]")

def init(
    force: bool = typer.Option(
        False, "--force", help="覆盖已存在的 .env"
    ),
    env_file: str = typer.Option(
        ".env", "--env-file", help="目标文件路径"
    ),
) -> None:
    """从 .env.example 创建本地 .env。"""
    target = Path(env_file)
    source = Path.cwd() / ".env.example"
    if not source.exists():
        console.print(
            f"[red]找不到模板 {escape(str(source))}；"
            "请在仓库根目录执行，或手动创建 .env。[/]"
        )
        raise typer.Exit(code=1)
    if target.exists() and not force:
        console.print(
            f"[yellow]{escape(str(target))} 已存在；"
            "如需覆盖请加 --force。[/]"
        )
        return
    try:
        target.write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    except OSError as exc:
        console.print(f"[red]写入 {escape(str(target))} 失败：{exc}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]已创建 {escape(str(target))}[/]")
    console.print(
        "[dim]下一步：填入 DEEPSEEK_API_KEY，然后运行 "
        "agent doctor 与 agent sandbox-init。[/]"
    )
