"""构建专用 CodingAgent-WSL 发行版（Phase 2）。

在 Windows 上运行，需要 WSL2 与网络：

    python scripts/build_wsl_distro.py

流程：
  Ubuntu base rootfs → wsl --import CodingAgentBuild
  → apt 安装 Python/git/ripgrep/bubblewrap
  → /opt/coding-agent/venv 安装 ubismart[web]
  → 创建 agent 用户、workspace、wsl.conf
  → wsl --export → packaging/wsl/CodingAgent-rootfs.tar
  → wsl --unregister CodingAgentBuild

产物：packaging/wsl/CodingAgent-rootfs.tar
"""

from __future__ import annotations

import argparse
import gzip
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from ubismart.desktop.wsl import to_wsl_path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging" / "wsl"
BASE_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/"
    "ubuntu-base-24.04.3-base-amd64.tar.gz"
)


def _run(args: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess:
    print("+", " ".join(args))
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def _download(url: str, target: Path) -> None:
    if target.is_file():
        print(f"已缓存：{target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        with target.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def _decompress(source: Path, target: Path) -> None:
    if target.is_file() and target.stat().st_size > 0:
        print(f"已解压：{target}")
        return
    print(f"解压 {source} -> {target}")
    with gzip.open(source, "rb") as src, target.open("wb") as dst:
        shutil.copyfileobj(src, dst)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distro", default="CodingAgentBuild")
    parser.add_argument(
        "--output",
        default=str(PACKAGING / "CodingAgent-rootfs.tar"),
    )
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args()

    base_gz = PACKAGING / "ubuntu-base-24.04.3.tar.gz"
    base_tar = PACKAGING / "ubuntu-base-24.04.3.tar"
    if not args.skip_download:
        _download(BASE_URL, base_gz)
    _decompress(base_gz, base_tar)

    install_dir = PACKAGING / "build-root"
    if install_dir.exists():
        print(f"移除旧构建目录：{install_dir}")
        shutil.rmtree(install_dir)

    # 同名构建发行版可能残留；先清理再导入。
    _run(["wsl.exe", "--unregister", args.distro])
    result = _run(
        [
            "wsl.exe",
            "--import",
            args.distro,
            str(install_dir),
            str(base_tar),
            "--version",
            "2",
        ]
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        return 1

    project = to_wsl_path(str(ROOT))
    script = f"""set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip git ripgrep bubblewrap \
  ca-certificates curl
useradd -m -s /bin/bash agent || true
mkdir -p /opt/coding-agent
python3 -m venv /opt/coding-agent/venv
/opt/coding-agent/venv/bin/python -m pip install --upgrade pip
/opt/coding-agent/venv/bin/pip install {shlex.quote(project + "[web,secrets]")}
mkdir -p /home/agent/.coding-agent/workspaces/default
chown -R agent:agent /home/agent/.coding-agent
printf '[user]\\ndefault=agent\\n[interop]\\nappendWindowsPath=false\\n' > /etc/wsl.conf
printf 'phase2\\n' > /opt/coding-agent/VERSION
apt-get clean
rm -rf /var/lib/apt/lists/*
"""
    # 首次构建要下载 apt 包与全部 Python 依赖；慢网络下 20 分钟不够。
    result = _run(
        ["wsl.exe", "-d", args.distro, "--", "/bin/bash", "-lc", script],
        timeout=3600,
    )
    print(result.stdout[-4000:])
    if result.returncode != 0:
        print(result.stderr[-4000:])
        return 1

    _run(["wsl.exe", "--terminate", args.distro])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        ["wsl.exe", "--export", args.distro, str(output)], timeout=1200
    )
    if result.returncode != 0:
        print(result.stderr)
        return 1
    print(f"已生成：{output}（{output.stat().st_size / 1024 / 1024:.0f} MB）")

    if not args.keep:
        _run(["wsl.exe", "--unregister", args.distro])
    return 0


if __name__ == "__main__":
    sys.exit(main())
