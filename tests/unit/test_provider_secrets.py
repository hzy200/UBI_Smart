"""GUI/密钥环是显式动作，优先于 .env 的默认 provider key。"""

from __future__ import annotations

from ubismart.config import Settings
from ubismart.llm.deepseek import _provider_specs


class FakeStore:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str:
        return self.values.get(name, "")


def test_secret_store_overrides_default_env_key() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="sk-env",
        deepseek_model="deepseek-chat",
    )
    specs = _provider_specs(settings, FakeStore({"deepseek": "sk-store"}))
    assert specs[0]["api_key"] == "sk-store"


def test_selected_provider_does_not_require_other_provider_keys() -> None:
    settings = Settings(
        _env_file=None,
        llm_providers=(
            '[{"name":"a","api_key":"sk-a","model":"m-a"},'
            '{"name":"b","model":"m-b"}]'
        ),
    )
    specs = _provider_specs(settings, FakeStore({}), "a")
    assert [spec["name"] for spec in specs] == ["a"]


def test_env_key_is_used_when_store_is_empty() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="sk-env",
        deepseek_model="deepseek-chat",
    )
    specs = _provider_specs(settings, FakeStore({}))
    assert specs[0]["api_key"] == "sk-env"
