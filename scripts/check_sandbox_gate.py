"""核验 P0-3 的沙箱门禁是否真的在拦合并。

两种用法：

    python scripts/check_sandbox_gate.py --local
        在 self-hosted runner / 本机确认 wsl.exe 可用且有发行版。

    GITHUB_TOKEN=... python scripts/check_sandbox_gate.py \
        --repo owner/name --branch main
        通过 GitHub API 确认分支保护把 `Sandbox / sandbox-gate` 设为
        required check。

它只做检查，不修改仓库设置；分支保护仍需仓库管理员在 GitHub 上配置。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request

# 必须要求汇总门禁，而不是 `wsl` job：没有在线 runner 时 `wsl` 会被跳过，
# 只 required 它会让"跳过"变成"永不拦合并"。
REQUIRED_CHECK = "Sandbox / sandbox-gate"


def check_local() -> int:
    try:
        # WSL 默认按 UTF-16LE 输出；宿主 locale 是 GBK 时 text=True 会在
        # 读取线程里抛 UnicodeDecodeError。强制 UTF-8 + replace 兜底。
        result = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env={**os.environ, "WSL_UTF8": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    distros = [
        line.strip().replace("\x00", "")
        for line in result.stdout.splitlines()
        if line.strip().replace("\x00", "")
    ]
    ok = result.returncode == 0 and bool(distros)
    print(
        json.dumps(
            {
                "ok": ok,
                "returncode": result.returncode,
                "distros": distros,
                "required_check": REQUIRED_CHECK,
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def check_github(repo: str, branch: str, token: str) -> int:
    url = (
        f"https://api.github.com/repos/{repo}"
        f"/branches/{branch}/protection/required_status_checks"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"HTTP {exc.code}",
                    "hint": (
                        "分支保护未配置或 token 无 repo 管理权限；"
                        f"需要把 {REQUIRED_CHECK} 设为 required check"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 1
    contexts = [
        str(item)
        for item in (payload.get("contexts") or [])
    ]
    contexts += [
        str(item.get("context") or "")
        for item in (payload.get("checks") or [])
        if isinstance(item, dict)
    ]
    ok = REQUIRED_CHECK in contexts
    print(
        json.dumps(
            {
                "ok": ok,
                "required_check": REQUIRED_CHECK,
                "contexts": contexts,
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="核验沙箱门禁")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    if args.local:
        return check_local()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = str(args.repo or "").strip()
    if not token or not repo:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "需要 GITHUB_TOKEN 与 --repo owner/name",
                },
                ensure_ascii=False,
            )
        )
        return 2
    return check_github(repo, str(args.branch), token)


if __name__ == "__main__":
    raise SystemExit(main())
