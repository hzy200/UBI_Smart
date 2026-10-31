"""WSL 发行版加固检查（RFC-0001，M4）。

只在工作区注册表启用或显式要求时执行；默认不阻断开发环境。

检查项：

- 非 root 用户；
- `/etc/wsl.conf` 的 `[interop] appendWindowsPath = false`；
- 可选：`[interop] enabled = false`（高风险/生产档）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ubismart.desktop.wsl import run


@dataclass(frozen=True, slots=True)
class HardeningReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    uid: str = ""
    conf: dict[str, dict[str, str]] = field(default_factory=dict)


def parse_wsl_conf(text: str) -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = {}
    section = ""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            data.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        text_value = value.strip()
        if (
            len(text_value) >= 2
            and text_value[0] == text_value[-1]
            and text_value[0] in {'"', "'"}
        ):
            text_value = text_value[1:-1]
        data.setdefault(section, {})[key.strip().lower()] = text_value
    return data


def evaluate(
    uid: str,
    conf: dict[str, dict[str, str]],
    *,
    require_no_interop: bool = False,
) -> HardeningReport:
    problems: list[str] = []
    warnings: list[str] = []
    if str(uid).strip() == "0":
        problems.append("WSL 用户是 root；请改为专用非 root 用户")
    interop = conf.get("interop", {})
    append_path = str(
        interop.get("appendwindowspath", "")
    ).strip().lower()
    if append_path != "false":
        problems.append(
            "[interop] appendWindowsPath 未设为 false"
        )
    enabled = str(interop.get("enabled", "")).strip().lower()
    if enabled == "true":
        message = "[interop] enabled = true；高风险档建议关闭"
        if require_no_interop:
            problems.append(message)
        else:
            warnings.append(message)
    if not conf:
        warnings.append("未读取到 /etc/wsl.conf")
    return HardeningReport(
        ok=not problems,
        problems=problems,
        warnings=warnings,
        uid=str(uid),
        conf=conf,
    )


def probe_script() -> str:
    return (
        'printf "__UID__%s\\n" "$(id -u)"; '
        'printf "__CONF_START__\\n"; '
        "cat /etc/wsl.conf 2>/dev/null || true"
    )


def check(
    distro: str, *, require_no_interop: bool = False
) -> HardeningReport:
    proc = run(distro, probe_script(), timeout=20)
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0 and not text:
        detail = (proc.stderr or b"").decode(
            "utf-8", errors="replace"
        )
        return HardeningReport(
            ok=False,
            problems=[f"无法读取 WSL 加固信息：{detail.strip()}"],
        )
    uid = ""
    conf_text = ""
    for line in text.splitlines():
        if line.startswith("__UID__"):
            uid = line[len("__UID__") :].strip()
        elif line.startswith("__CONF_START__"):
            continue
        else:
            conf_text += line + "\n"
    return evaluate(
        uid,
        parse_wsl_conf(conf_text),
        require_no_interop=require_no_interop,
    )
