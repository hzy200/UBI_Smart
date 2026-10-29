"""前端静态资源的目录解析。

显式配置或发布包缺文件时启动失败（fail-closed），避免独立部署静默退回
包内旧产物。
"""

from __future__ import annotations

from pathlib import Path

from coding_agent.config import Settings

STATIC_DIR = Path(__file__).parent / "static"


def resolve_frontend_dirs(
    settings: Settings, desktop_mode: bool
) -> tuple[bool, Path, Path]:
    """解析前端托管开关与目录。

    显式配置的目录或发布包缺文件时启动失败（fail-closed），避免独立部署
    静默退回包内旧产物。未配置时保持包内 `web/static` 的原有行为。
    """
    frontend_raw = str(settings.web_frontend_dir or "").strip()
    static_raw = str(settings.web_static_dir or "").strip()
    frontend_dir = (
        Path(frontend_raw).expanduser().resolve()
        if frontend_raw
        else STATIC_DIR
    )
    if static_raw:
        static_dir = Path(static_raw).expanduser().resolve()
    elif (frontend_dir / "static").is_dir() and (frontend_dir / "index.html").is_file():
        # 发布包结构：<release>/index.html + <release>/static/*。
        static_dir = frontend_dir / "static"
    else:
        # 包内结构：index.html 与 app.js 同目录。
        static_dir = frontend_dir
    # 桌面壳必须能加载页面，配置不能把它关掉。
    serve = bool(settings.web_serve_frontend) or bool(desktop_mode)
    if not serve:
        return False, frontend_dir, static_dir
    for label, directory in (
        ("AGENT_WEB_FRONTEND_DIR", frontend_dir),
        ("AGENT_WEB_STATIC_DIR", static_dir),
    ):
        if not directory.is_dir():
            raise ValueError(f"{label} 指向的目录不存在：{directory}")
    for name in ("index.html", "sw.js", "manifest.webmanifest"):
        if not (frontend_dir / name).is_file():
            raise ValueError(f"前端发布包缺少 {name}：{frontend_dir}")
    return True, frontend_dir, static_dir
