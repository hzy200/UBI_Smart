"""动态图构建器：由 ChangeContract 生成接口优先任务图。"""

from __future__ import annotations

from ubismart.config import Settings
from ubismart.orchestrator.workflow_builder import build_dynamic_plan


def _settings(**overrides) -> Settings:
    values = {
        "deep_workflows_enabled": True,
        "orchestrator_max_tasks": 8,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _summary() -> dict:
    return {
        "change_contract": {
            "waves": [
                {
                    "stage": 0,
                    "files": ["pkg/core.py"],
                    "modules": ["pkg.core"],
                },
                {
                    "stage": 1,
                    "files": ["pkg/api.py"],
                    "modules": ["pkg.api"],
                },
            ],
            "affected_tests": ["tests/test_core.py"],
            "affected_modules": ["pkg.core", "pkg.api"],
        }
    }


def test_builds_interface_first_waves() -> None:
    plan = build_dynamic_plan(
        "把 run 改成 calculate",
        _summary(),
        _settings(),
        write_available=True,
    )

    assert plan is not None
    assert plan["workflow"] == "architecture_change"
    implements = [task for task in plan["tasks"] if task["role"] == "implement"]
    assert [task["stage"] for task in implements] == [0, 1]
    assert implements[0]["write_scope"] == ["pkg/core.py"]
    assert implements[1]["write_scope"] == ["pkg/api.py"]
    assert implements[1]["depends_mode"] == "any"
    assert implements[1]["depends_on"] == [implements[0]["id"]]
    verify = next(task for task in plan["tasks"] if task["role"] == "test")
    assert verify["stage"] == 2
    assert set(verify["depends_on"]) == {task["id"] for task in implements}


def test_requires_write_and_deep_mode() -> None:
    settings = _settings()
    assert (
        build_dynamic_plan(
            "改接口", _summary(), settings, write_available=False
        )
        is None
    )
    assert (
        build_dynamic_plan(
            "改接口",
            _summary(),
            _settings(deep_workflows_enabled=False),
            write_available=True,
        )
        is None
    )


def test_single_wave_falls_back() -> None:
    summary = {
        "change_contract": {
            "waves": [{"stage": 0, "files": ["a.py"]}],
        }
    }
    assert (
        build_dynamic_plan(
            "改一处", summary, _settings(), write_available=True
        )
        is None
    )
