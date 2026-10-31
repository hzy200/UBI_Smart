"""GUI 管理的 provider 档案（Phase 4）。

不保存 API Key：密钥仍由 OS 密钥环按 provider 名保存。档案只描述
`base_url` / `model` / 成本 / 配额 / 能力，最终合并进 `llm_providers` JSON。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(slots=True)
class ProviderProfile:
    name: str
    base_url: str
    model: str
    enabled: bool = True
    priority: int = 0
    input_cost_per_1m: float = 0.0
    output_cost_per_1m: float = 0.0
    max_tokens: float = 0.0
    max_cost: float = 0.0
    capabilities: list[str] = field(default_factory=list)
    task_types: list[str] = field(default_factory=list)

    def to_item(self) -> dict[str, Any]:
        return asdict(self)


def profile_from_payload(payload: dict[str, Any]) -> ProviderProfile:
    name = str(payload.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("provider 名称只允许字母、数字、_ . -（1–64）")
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url 必须是 http(s):// 地址")
    model = str(payload.get("model") or "").strip()
    if not model:
        raise ValueError("model 不能为空")

    def _float(key: str) -> float:
        try:
            value = float(payload.get(key, 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是数字") from exc
        if value < 0:
            raise ValueError(f"{key} 不能为负")
        return value

    def _int(key: str) -> int:
        return int(_float(key))

    def _list(key: str) -> list[str]:
        raw = payload.get(key) or []
        if not isinstance(raw, list):
            raise ValueError(f"{key} 必须是数组")
        return [str(item).strip() for item in raw if str(item).strip()]

    return ProviderProfile(
        name=name,
        base_url=base_url,
        model=model,
        enabled=bool(payload.get("enabled", True)),
        priority=_int("priority"),
        input_cost_per_1m=_float("input_cost_per_1m"),
        output_cost_per_1m=_float("output_cost_per_1m"),
        max_tokens=_float("max_tokens"),
        max_cost=_float("max_cost"),
        capabilities=_list("capabilities"),
        task_types=_list("task_types"),
    )


class ProviderProfileStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[ProviderProfile]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = payload if isinstance(payload, list) else []
        profiles: list[ProviderProfile] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                profiles.append(profile_from_payload(row))
            except ValueError:
                continue
        return profiles

    def save(self, profiles: list[ProviderProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                [profile.to_item() for profile in profiles],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp.replace(self.path)

    def upsert(self, profile: ProviderProfile) -> list[ProviderProfile]:
        profiles = [item for item in self.load() if item.name != profile.name]
        profiles.append(profile)
        profiles.sort(key=lambda item: (item.priority, item.name))
        self.save(profiles)
        return profiles

    def delete(self, name: str) -> list[ProviderProfile]:
        profiles = [item for item in self.load() if item.name != name]
        self.save(profiles)
        return profiles


def merge_provider_items(
    base_raw: str, profiles: list[ProviderProfile]
) -> list[dict[str, Any]]:
    """把档案合并进环境里的 provider 列表；同名时档案字段优先。"""
    items: list[dict[str, Any]] = []
    if str(base_raw or "").strip():
        try:
            parsed = json.loads(base_raw)
        except ValueError:
            parsed = []
        items = [
            dict(item)
            for item in (parsed if isinstance(parsed, list) else [parsed])
            if isinstance(item, dict)
        ]
    by_name: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        name = str(item.get("name") or f"provider-{index}")
        item.setdefault("name", name)
        by_name[name] = item
        ordered.append(item)
    for profile in profiles:
        item = profile.to_item()
        existing = by_name.get(profile.name)
        if existing is not None:
            existing.update(item)
        else:
            ordered.append(item)
            by_name[profile.name] = item
    return ordered
