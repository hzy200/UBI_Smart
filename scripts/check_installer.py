"""安装包脚本的一致性校验。

真包只能在有 WSL2 与签名证书的机器上构建（`build_wsl_distro.py` 要跑 apt），
托管 CI 做不了。但有几类问题**静态就能拦住**，而且都属于"上线才发现"的那种：

- 版本号又写死回 `0.1.0`（`AppVersion` 与 `__version__` 漂移）；
- 卸载时把 `%LOCALAPPDATA%` 下的用户数据一起删了；
- 安装需要提权（`PrivilegesRequired` 不是 `lowest`）；
- `.iss` 引用的路径被改名/搬走，构建时才炸。

用法：
    python scripts/check_installer.py            # 人读
    python scripts/check_installer.py --check    # 只返回退出码（CI）
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "packaging" / "installer" / "CodingAgent.iss"
BUILD_PS1 = ROOT / "scripts" / "build_installer.ps1"
VERSION_FILE = ROOT / "src" / "coding_agent" / "__init__.py"


def _version_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit(f"无法从 {path} 读取 __version__")
    return match.group(1)


def project_version() -> str:
    return _version_of(VERSION_FILE)


def check(
    iss_path: Path = ISS,
    ps1_path: Path = BUILD_PS1,
    version_file: Path = VERSION_FILE,
) -> list[str]:
    """返回问题清单；空列表表示通过。路径可注入，方便单测。"""
    errors: list[str] = []
    for path in (iss_path, ps1_path, version_file):
        if not path.is_file():
            errors.append(f"缺少文件：{path}")
    if errors:
        return errors

    iss = iss_path.read_text(encoding="utf-8")
    ps1 = ps1_path.read_text(encoding="utf-8")
    version = _version_of(version_file)

    # 1. 版本必须由构建参数注入，而不是写死在安装脚本里。
    if "AppVersion={#AppVersion}" not in iss:
        errors.append(
            "[Setup] 的 AppVersion 必须是 {#AppVersion} 占位符；写死会让安装包"
            "版本与 __version__ 漂移"
        )
    fallback = re.search(r'#define AppVersion "([^"]+)"', iss)
    if fallback is None:
        errors.append('缺少 `#ifndef AppVersion` 的兜底定义（单独用 ISCC 时会报未定义）')
    elif fallback.group(1) != version:
        errors.append(
            f"兜底版本 {fallback.group(1)} 与 __version__ {version} 不一致"
        )

    # 2. 构建脚本要从包版本读，不能自己写一个。
    if "/DAppVersion=" not in ps1:
        errors.append("build_installer.ps1 没有把版本传给 ISCC")
    elif re.search(r"/DAppVersion=(?!\$)\S+", ps1):
        errors.append("build_installer.ps1 把版本写死了，应传 $Version")

    # 3. 安装必须免提权。
    if "PrivilegesRequired=lowest" not in iss:
        errors.append("PrivilegesRequired 必须是 lowest（不允许提权安装）")

    # 4. 卸载不能删用户数据：那里有 WSL 发行版、日志与会话。
    if re.search(r"^\s*\[UninstallDelete\]", iss, re.MULTILINE) and re.search(
        r"localappdata|%LOCALAPPDATA%", iss, re.IGNORECASE
    ):
        errors.append(
            "卸载段不能删除 %LOCALAPPDATA% 下的用户数据（发行版/日志/会话）"
        )

    # 5. 引用的产物路径要存在（CI 里由 job 造 stub，本地构建前也应当存在）。
    sources = re.findall(r'Source:\s*"([^"]+)"', iss)
    if not sources:
        errors.append(".iss 里没有任何 Source（安装包会什么都没有）")
    for source in sources:
        target = (iss_path.parent / source).resolve()
        if any(char in source for char in "*?"):
            # 通配：检查它的父目录是否至少有个存在的 ancestor
            base = target
            while any(char in str(base) for char in "*?"):
                base = base.parent
            if not base.exists() and "vendor" not in source:
                errors.append(f"Source 通配的父目录不存在：{source}")
        elif not target.exists() and "vendor" not in source:
            errors.append(f"Source 指向的文件不存在：{source}")

    # 6. 桌面快捷方式与图标：图标文件缺失会让 ISCC 直接失败，提前说清楚。
    icon = re.search(r"SetupIconFile=([^\r\n]+)", iss)
    if icon is None:
        errors.append("缺少 SetupIconFile")
    elif not (iss_path.parent / icon.group(1).strip()).resolve().is_file():
        errors.append(f"SetupIconFile 指向的文件不存在：{icon.group(1).strip()}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装包脚本一致性校验")
    parser.add_argument("--check", action="store_true", help="只返回退出码")
    args = parser.parse_args(argv)

    errors = check()
    if errors:
        print("安装包脚本有问题：")
        for item in errors:
            print(f"  - {item}")
        return 1
    if not args.check:
        print(
            f"安装包脚本一致（版本 {project_version()}，免提权安装，卸载保留用户数据）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
