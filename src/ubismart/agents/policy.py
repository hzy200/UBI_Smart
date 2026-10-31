"""委派的成本策略：根据仓库规模与当前步骤决定「能不能委派、最多几次」。

A/B 的教训：38 个文件的长程任务里，模型会一次拆 3–5 个子智能体；探索本身
确实有用，但每个子智能体都要独立跑一轮 LLM + 多次只读工具，成本直接翻倍。
所以委派不能只看「开关是否打开」，还要看：

- **仓库规模**：小仓库直接读更便宜；只有跨模块的大仓库才值得拆。
- **当前步骤类型**：只有 `kind=explore` 的步骤允许委派；实现/验证步自己完成。
- **任务级预算**：按规模给 0 / 2 / 4 / 6 次，而不是一律给最大值。

探测失败（profile 未知）时**不关闭**功能：这通常意味着沙箱本身有问题，
此时再用规模阈值去限制只会让 agent 更早卡死。保持配置的最大值，让上层
的通用错误处理去暴露真正的问题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ubismart.config import Settings
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.wsl_exec import WslSandbox


@dataclass(frozen=True, slots=True)
class RepoProfile:
    """仓库的轻量画像。`file_count == 0` 表示没探到（未知，不是空仓库）。"""

    file_count: int = 0
    test_file_count: int = 0

    @property
    def known(self) -> bool:
        return self.file_count > 0


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    """一次运行的委派策略。"""

    allowed: bool
    max_calls: int
    reason: str


# 排除目录与检索工具的口径一致；空元组表示"全部文件"。
_PROFILE_GROUPS: dict[str, tuple[str, ...]] = {
    "total": (),
    "tests": ("test_*.py", "*_test.py"),
}
_PROFILE_EXCLUDES = (".git", ".agent", "__pycache__", "node_modules", ".venv")


def profile_workspace(sandbox: WslSandbox, root: str) -> RepoProfile:
    """一次沙箱调用拿到文件数；失败时返回未知画像。

    计数交给 `SandboxFs.count_files`：画像只需要量级，没必要为此在 agents
    层握着一条"跑任意脚本"的通道（TD-034）。
    """
    try:
        counts = SandboxFs(sandbox, root).count_files(
            _PROFILE_GROUPS, exclude_dirs=_PROFILE_EXCLUDES
        )
    except Exception:  # noqa: BLE001 - 画像失败不能拖垮 run()，一律按未知处理
        # 这里刻意兜住所有异常：沙箱不可用时 `SandboxFs` 可能抛 OSError、
        # 解析失败抛 SandboxFsError，而对一次"成本估算"来说，任何失败都等价于
        # "规模未知" —— 上层会保留最大委派预算，让真正的错误在别处暴露。
        return RepoProfile()
    return RepoProfile(
        file_count=int(counts.get("total", 0)),
        test_file_count=int(counts.get("tests", 0)),
    )


_REQUEST_FILE_RE = re.compile(
    r"[A-Za-z0-9_./-]+\.(?:py|pyi|ts|tsx|js|jsx|md|json|toml|yaml|yml|css|html|"
    r"sh|go|rs|java|kt|c|h|cpp|hpp)\b",
    re.IGNORECASE,
)
_READ_ONLY_PREFIXES = (
    "解释",
    "说明",
    "为什么",
    "是什么",
    "怎么",
    "如何",
    "看看",
    "读一下",
    "分析一下",
    "介绍一下",
)
_WRITE_VERBS = (
    "实现",
    "修改",
    "修复",
    "添加",
    "新增",
    "删除",
    "重构",
    "改成",
    "重写",
    "补上",
    "加一个",
    "加个",
)
_MULTI_HINTS = (
    "同时",
    "分别",
    "各自",
    "多个",
    "两个",
    "三个",
    "两条",
    "一起",
    "另外",
    "各加",
    "各修",
    "都要",
)
_SIMPLE_HINTS = ("typo", "拼写", "重命名", "rename", "格式化", "缩进", "注释")


def should_orchestrate(
    request: str, profile: RepoProfile, settings: Settings
) -> bool:
    """是否值得进入 orchestrated 子图。

    规模阈值是必要条件，不是充分条件。明显的单文件修改、只读问答与
    拼写/重命名这类小动作直接走 single agent，避免为一次 `file_read`
    付 survey + 工作流选择的固定成本。判定只做**高置信**的 direct：
    拿不准时仍然编排（宁可多花一点，不要错误地跳过并行）。
    """
    if not settings.orchestrator_enabled:
        return False
    if (
        not profile.known
        or profile.file_count < settings.orchestrator_min_files
    ):
        return False
    if not bool(getattr(settings, "orchestrator_smart_entry", True)):
        return True

    text = str(request or "").strip()
    if not text:
        return True
    lowered = text.lower()
    has_write = any(verb in text for verb in _WRITE_VERBS)
    has_multi = any(hint in text for hint in _MULTI_HINTS)

    # 只读问答：明确不要求改动，且请求不长。
    if (
        not has_write
        and len(text) <= 200
        and text.startswith(_READ_ONLY_PREFIXES)
    ):
        return False

    # 点名单个文件：没有多交付物信号时，单个文件通常一条功能线。
    files = set(_REQUEST_FILE_RE.findall(text))
    if len(files) == 1 and len(text) <= 300 and not has_multi:
        return False

    # 拼写/重命名/格式化/缩进/注释这类低风险小动作。
    if (
        any(hint in lowered for hint in _SIMPLE_HINTS)
        and len(text) <= 120
        and not has_multi
    ):
        return False

    return True


def delegation_policy(profile: RepoProfile, settings: Settings) -> DelegationPolicy:
    """按仓库规模计算委派预算。"""
    if not settings.explore_enabled:
        return DelegationPolicy(False, 0, "explore disabled")

    if not profile.known:
        # 探测失败：不因规模判断而误伤功能
        return DelegationPolicy(
            True, settings.max_delegate_calls, "repo size unknown"
        )

    if profile.file_count < settings.delegate_min_files:
        return DelegationPolicy(
            False, 0, f"{profile.file_count} files < {settings.delegate_min_files}"
        )
    if profile.file_count < settings.delegate_medium_files:
        return DelegationPolicy(
            True,
            min(settings.max_delegate_calls, settings.delegate_medium_calls),
            f"medium repo ({profile.file_count} files)",
        )
    if profile.file_count < settings.delegate_large_files:
        return DelegationPolicy(
            True,
            min(settings.max_delegate_calls, settings.delegate_large_calls),
            f"large repo ({profile.file_count} files)",
        )
    return DelegationPolicy(
        True,
        settings.max_delegate_calls,
        f"very large repo ({profile.file_count} files)",
    )
