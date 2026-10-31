"""合并前的高风险补丁审查。

**这不是安全边界。** write_scope 的写时校验、命令分级审批、路径守卫仍然
独立生效；即使审查被绕过，补丁也越不过那些防线。这里只解决一个质量问题：
并行 implement 各自在 worktree 里改完就直接合并，主工作区在合并前没有任何
人看过补丁。高风险改动（测试/配置/CI/安全路径，或大 diff）值得花一次
便宜模型的调用做最后一道人工替代。

取舍：
- 只审高风险 patch，普通小改动不付额外成本；
- 审查基础设施失败时 **fail-open**：review 不可用不应阻塞合并；
- 结果转成 findings 回灌重试上下文，让 implement 知道上一次被拒的原因。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from ubismart.config import Settings
from ubismart.llm.deepseek import with_json_output

PatchReviewer = Callable[
    [dict[str, Any], str, RunnableConfig | None], Awaitable[dict[str, Any]]
]


class PatchIssue(BaseModel):
    path: str = ""
    line: int = 0
    message: str = ""
    severity: Literal["blocking", "warning"] = "warning"


class PatchReview(BaseModel):
    approve: bool = True
    issues: list[PatchIssue] = Field(default_factory=list)


PATCH_REVIEW_PROMPT = """\
你是代码审查器。只审查给定的 unified diff，判断它是否可以安全合并。

关注（按严重度）：
- blocking：真实逻辑错误、安全风险、需求明显没实现、测试被改弱/跳过、
  越界修改了与任务无关的文件；
- warning：命名/风格/可读性问题，或需要后续确认但不应阻塞合并。

规则：
- 只依据 diff 与任务目标判断，不要臆测 diff 之外的内容；
- 证据不足时不要编造，放到 warning 并说明需要确认什么；
- 没有 blocking 问题时 approve=true。warning 不影响 approve。
- 只输出 JSON：{"approve": bool, "issues": [{"path": "...", "line": 0,
  "message": "...", "severity": "blocking|warning"}]}
"""

_HIGH_RISK_PATTERNS = (
    re.compile(r"(^|/)tests?/", re.IGNORECASE),
    re.compile(r"(^|/)(test_[^/]+|[^/]+_test\.[^/]+)$", re.IGNORECASE),
    re.compile(r"(^|/)spec/", re.IGNORECASE),
    re.compile(r"(^|/)\.github/", re.IGNORECASE),
    re.compile(r"(^|/)(Dockerfile|Makefile)$", re.IGNORECASE),
    re.compile(r"\.(toml|ya?ml|sql|sh|ps1|env)$", re.IGNORECASE),
    re.compile(r"(^|/)\.env[^/]*$", re.IGNORECASE),
    re.compile(r"(^|/)(sandbox|security)/", re.IGNORECASE),
    re.compile(
        r"(^|/)(approve|policy|auth|secrets?|crypto)[^/]*\.py$",
        re.IGNORECASE,
    ),
)


def _changed_paths(record: dict[str, Any]) -> list[str]:
    return [
        str(path).replace("\\", "/")
        for path in (record.get("files") or [])
        if str(path).strip()
    ]


def diff_line_count(patch_text: str) -> int:
    """只数真正的增删行，不把 `+++` / `---` 文件头算进去。"""
    return sum(
        1
        for line in patch_text.splitlines()
        if line.startswith(("+", "-"))
        and not line.startswith(("+++", "---"))
    )


def is_high_risk_patch(
    record: dict[str, Any], patch_text: str, settings: Settings
) -> bool:
    if not bool(
        getattr(settings, "orchestrator_patch_review", True)
    ):
        return False
    if any(
        pattern.search(path)
        for path in _changed_paths(record)
        for pattern in _HIGH_RISK_PATTERNS
    ):
        return True
    threshold = max(
        1,
        int(
            getattr(
                settings, "orchestrator_patch_review_max_lines", 400
            )
            or 400
        ),
    )
    return diff_line_count(patch_text) > threshold


def _normalize_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith(("a/", "b/")):
        text = text[2:]
    return posixpath.normpath(text).lstrip("/")


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def patch_line_map(patch_text: str) -> dict[str, int]:
    """解析 unified diff，返回 {路径: 新文件里出现过的最大行号}。

    只用于校验审查模型给的行号是否落在补丁范围内，不做完整 diff 解析。
    """
    current = ""
    result: dict[str, int] = {}
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            raw_path = line[4:].strip().split("\t")[0]
            if raw_path == "/dev/null":
                current = ""
                continue
            current = _normalize_path(raw_path)
            if current:
                result.setdefault(current, 0)
            continue
        match = _HUNK.match(line)
        if match and current:
            start = int(match.group(1))
            count = max(1, int(match.group(2) or 1))
            result[current] = max(result[current], start + count - 1)
    return result


def validate_review_issues(
    issues: list[dict[str, Any]],
    record: dict[str, Any],
    patch_text: str,
) -> tuple[list[dict[str, Any]], int]:
    """丢弃路径/行号对不上补丁的审查意见；返回 (保留项, 丢弃数)。

    审查模型会幻觉出不存在的文件或越界行号。这些意见若直接触发重跑，
    等于让模型为一次幻觉付一整轮 implement 的成本。
    """
    line_map = patch_line_map(patch_text)
    allowed = {_normalize_path(path) for path in (record.get("files") or [])}
    allowed |= set(line_map)
    allowed.discard("")
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in issues:
        if not isinstance(item, dict):
            dropped += 1
            continue
        path = _normalize_path(item.get("path", ""))
        try:
            line = int(item.get("line", 0) or 0)
        except (TypeError, ValueError):
            line = 0
        if not path or path not in allowed:
            dropped += 1
            continue
        max_line = line_map.get(path, 0)
        if line > 0 and max_line > 0 and line > max_line:
            dropped += 1
            continue
        kept.append({**item, "path": path, "line": line})
    return kept, dropped


def _task_view(record: dict[str, Any]) -> str:
    goal = str(record.get("goal", "")).strip()
    findings = [
        str(item).strip()
        for item in (record.get("findings") or [])
        if str(item).strip()
    ]
    lines = [f"任务目标：{goal or '（未提供）'}"]
    if findings:
        lines.append("worker 自述：\n" + "\n".join(f"- {item}" for item in findings[:5]))
    return "\n".join(lines)


def make_patch_reviewer(
    llm: Any, settings: Settings
) -> PatchReviewer:
    """构造合并前审查器；非高风险 / 关闭时直接放行。"""
    structured = with_json_output(llm, PatchReview)
    max_chars = max(
        4_000,
        int(
            getattr(settings, "orchestrator_patch_review_max_chars", 60_000)
            or 60_000
        ),
    )

    async def review(
        record: dict[str, Any],
        patch_text: str,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        if not patch_text.strip():
            return {"approve": True, "skipped": True, "issues": []}
        if not is_high_risk_patch(record, patch_text, settings):
            return {"approve": True, "skipped": True, "issues": []}

        diff = patch_text[:max_chars]
        if len(patch_text) > max_chars:
            diff += "\n…（diff 已截断，仅审查以上部分）"
        body = f"{_task_view(record)}\n\nunified diff：\n{diff}"
        try:
            decision = await structured.ainvoke(
                [
                    SystemMessage(content=PATCH_REVIEW_PROMPT),
                    HumanMessage(content=body),
                ],
                config,
            )
        except Exception as exc:  # noqa: BLE001 - 审查不可用不能阻塞合并
            return {
                "approve": True,
                "skipped": True,
                "issues": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        if isinstance(decision, PatchReview):
            issues, dropped = validate_review_issues(
                [issue.model_dump() for issue in decision.issues[:8]],
                record,
                patch_text,
            )
            blocking = [
                item
                for item in issues
                if str(item.get("severity", "")) == "blocking"
            ]
            # 以留下来的 blocking issue 为准：模型说 approve=false 但没有
            # 任何可核验的阻断项时，不因一次幻觉重跑整个 implement。
            return {
                "approve": not blocking,
                "skipped": False,
                "issues": issues,
                "dropped_issues": dropped,
            }
        # json_mode 偶尔会返回裸文本；没有可解析结论时 fail-open。
        return {
            "approve": True,
            "skipped": True,
            "issues": [],
            "error": f"无法解析审查结果：{str(decision)[:200]}",
        }

    return review
