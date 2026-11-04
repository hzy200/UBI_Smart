"""契约归一化与独立计划评审（S2/S4）。

这一层只做两件事，且都不调用执行工具：

1. 把模型给出的 Contract 归一化成稳定字典；缺失字段由宿主保守补齐；
2. 按三个问题评审任务图：验收是否可判定、是否存在不可验证/无界步骤、
   是否有明显重复步骤。评审只回答 pass/fail + 原因，不参与生成。

`unknown` 非空不是评审失败，而是放行闸门：调用方会把问题交回用户澄清，
不进入 scheduler。评审失败同样不进入 scheduler，避免“先跑跑看”。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_AUTO_HINTS = (
    "测试",
    "test",
    "退出码",
    "exit",
    "通过",
    "pass",
    "存在",
    "包含",
    "生成",
    "路径",
    "文件",
    "schema",
    "格式",
    "行数",
    "覆盖",
    "url",
    "锚点",
    "引用",
    "file",
    "line",
    "证据",
    "断言",
    "构建",
    "编译",
)


@dataclass(slots=True)
class PlanReview:
    ok: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": list(self.reasons)}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            return dict(value.model_dump())
        except Exception:  # noqa: BLE001 - 非契约对象按空处理
            return {}
    return {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def infer_auto(rule: str) -> bool:
    text = str(rule or "").lower()
    return any(hint in text for hint in _AUTO_HINTS)


def _acceptance_entry(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        rule = value.strip()
        return {"rule": rule, "auto": infer_auto(rule)} if rule else None
    data = _as_dict(value)
    rule = str(data.get("rule") or "").strip()
    if not rule:
        return None
    raw_auto = data.get("auto")
    return {
        "rule": rule,
        "auto": bool(raw_auto) if raw_auto is not None else infer_auto(rule),
    }


def _normalize_acceptance(values: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values or []:
        entry = _acceptance_entry(value)
        if entry is None:
            continue
        key = entry["rule"].lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _origin_rule(task: dict[str, Any], index: int) -> dict[str, Any]:
    """任务没有可判定验收时，按角色补一条保守规则。

    这只是兜底，目的是不让“模型漏写 acceptance”变成静默放行；真正的
    acceptance 仍以模型输出为准，`source=derived` 会在评审里保留痕迹。
    """
    role = str(task.get("role", "explore"))
    if role in {"implement", "author"}:
        rule = "任务产出通过自动验证（测试/构建/语法检查），退出码为 0"
    elif role == "test":
        rule = "运行项目测试并报告退出码"
    else:
        rule = "产出至少一条带路径/锚点的结构化证据"
    return {
        "rule": rule,
        "auto": True,
        "source": "derived",
        "task_id": str(task.get("id", f"task-{index}")),
    }


def _task_acceptance_rules(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for index, task in enumerate(tasks):
        for value in task.get("acceptance") or []:
            entry = _acceptance_entry(value)
            if entry is not None:
                rules.append(entry)
        if not task.get("acceptance") and not task.get("acceptance_refs"):
            rules.append(_origin_rule(task, index))
    return rules


def _task_side_effects(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("id", ""))
        for scope in task.get("write_scope") or []:
            effects.append(
                {
                    "kind": "file",
                    "target": str(scope),
                    "idempotency_key": f"{task_id}:{scope}",
                    "compensation": "restore pre-write snapshot",
                }
            )
        for raw in task.get("side_effects") or []:
            if isinstance(raw, dict):
                effects.append(dict(raw))
    return effects


def _normalize_side_effects(values: Iterable[Any]) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for value in values or []:
        if isinstance(value, str):
            effects.append(
                {
                    "kind": "external",
                    "target": value.strip(),
                    "idempotency_key": "",
                    "compensation": "",
                }
            )
            continue
        data = _as_dict(value)
        target = str(data.get("target") or "").strip()
        if not target:
            continue
        kind = str(data.get("kind") or "external").strip().lower()
        if kind not in {"file", "git", "external"}:
            kind = "external"
        effects.append(
            {
                "kind": kind,
                "target": target,
                "idempotency_key": str(data.get("idempotency_key") or ""),
                "compensation": str(data.get("compensation") or ""),
            }
        )
    return effects


def normalize_contract(
    raw: Any,
    *,
    request: str,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """补齐契约；缺失 acceptance/side_effects 时从任务图保守推导。"""
    data = _as_dict(raw)
    goal = str(data.get("goal") or request or "").strip()
    acceptance = _normalize_acceptance(data.get("acceptance") or [])
    if not acceptance:
        acceptance = _normalize_acceptance(_task_acceptance_rules(tasks))
    # 任务漏写 acceptance 时把宿主推导出的规则回填到任务上，保证评审的
    # “每个任务至少映射一条验收”在旧模型输出上也能成立。
    for index, task in enumerate(tasks):
        if not task.get("acceptance") and not task.get("acceptance_refs"):
            rule = _origin_rule(task, index)
            task["acceptance"] = [rule["rule"]]
    known = data.get("known")
    unknown = _string_list(data.get("unknown") or [])
    # 子任务声明的 unknown 也要上浮：任何一层缺失信息都不该“先跑跑看”。
    for task in tasks:
        task_id = str(task.get("id", ""))
        for item in task.get("unknown") or []:
            text = str(item).strip()
            if text:
                unknown.append(f"{task_id}: {text}" if task_id else text)
    side_effects = _normalize_side_effects(data.get("side_effects") or [])
    if not side_effects:
        side_effects = _normalize_side_effects(_task_side_effects(tasks))
    return {
        "goal": goal,
        "constraints": _string_list(data.get("constraints") or []),
        "acceptance": acceptance,
        "known": dict(known) if isinstance(known, dict) else {},
        "unknown": list(dict.fromkeys(unknown)),
        "side_effects": side_effects,
    }


def validate_nested_plan(
    tasks: list[dict[str, Any]],
    *,
    enabled: bool,
    depth: int,
    max_depth: int,
    max_fanout: int,
) -> list[str]:
    """嵌套编排的硬边界：MAX_DEPTH / 扇出 / 子契约完整性。"""
    children = [
        task
        for task in tasks
        if str(task.get("role", "")) == "sub_orchestrator"
    ]
    if not children:
        return []
    errors: list[str] = []
    if not enabled:
        errors.append("当前未启用嵌套编排，不能生成 sub_orchestrator 任务")
    if int(depth) >= int(max_depth):
        errors.append(
            f"嵌套深度 {depth} 已达上限 {max_depth}，子编排器不能再派生子任务"
        )
    if len(children) > int(max_fanout):
        errors.append(
            f"子编排器数量 {len(children)} 超过单层扇出上限 {max_fanout}"
        )
    for task in children:
        task_id = str(task.get("id", ""))
        contract = task.get("sub_contract")
        if not isinstance(contract, dict) or not str(
            contract.get("goal") or ""
        ).strip():
            errors.append(
                f"子编排器 {task_id} 缺少 sub_contract.goal"
            )
        if task.get("write_scope"):
            errors.append(f"子编排器 {task_id} 不能声明 write_scope")
    return errors


def _goal_key(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


def review_plan(
    contract: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    request: str = "",
) -> PlanReview:
    """三问评审：验收可判定、无不可验证/无界步骤、无重复步骤。

    失败只返回原因，不修改计划；调用方负责决定重规划还是向用户澄清。
    """
    reasons: list[str] = []
    goal = str(contract.get("goal") or request or "").strip()
    if not goal:
        reasons.append("契约缺少可判定的 goal")
    acceptance = contract.get("acceptance") or []
    if not acceptance:
        reasons.append("契约缺少 acceptance")
    elif not any(bool(item.get("auto")) for item in acceptance):
        reasons.append("契约没有任何可自动检查的 acceptance 规则")

    if not tasks:
        reasons.append("任务图为空")

    seen: dict[tuple[str, str], str] = {}
    for task in tasks:
        task_id = str(task.get("id", ""))
        if not task.get("verifiable", True):
            reasons.append(f"任务 {task_id} 标记为不可验证，必须拆分")
        if str(task.get("branching", "none")) == "unbounded":
            reasons.append(f"任务 {task_id} 存在无界分支，必须给出上界")
        refs = task.get("acceptance_refs") or []
        for ref in refs:
            if not isinstance(ref, int) or ref < 0 or ref >= len(acceptance):
                reasons.append(
                    f"任务 {task_id} 的 acceptance_refs 越界：{ref}"
                )
        if not task.get("acceptance") and not refs:
            reasons.append(f"任务 {task_id} 没有映射到任何 acceptance 规则")
        key = (str(task.get("role", "")), _goal_key(task.get("goal")))
        if key[1]:
            previous = seen.get(key)
            if previous:
                reasons.append(
                    f"任务 {previous} 与 {task_id} 目标重复，应合并"
                )
            else:
                seen[key] = task_id

    # 去重后仍保留稳定顺序，便于直接回灌给规划模型。
    unique = list(dict.fromkeys(reasons))
    return PlanReview(ok=not unique, reasons=unique)
