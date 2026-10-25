"""增量测试选择器：导入图、反向依赖与保守回退。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "select_tests.py"


def _load():
    spec = importlib.util.spec_from_file_location("select_tests", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析字符串注解时会回查 sys.modules，动态加载必须登记。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(root: Path, relpath: str, content: str = "") -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    _write(tmp_path, "src/pkg/__init__.py")
    _write(tmp_path, "src/pkg/core.py", "VALUE = 1\n")
    _write(
        tmp_path,
        "src/pkg/service.py",
        "from pkg.core import VALUE\n",
    )
    _write(tmp_path, "src/pkg/unrelated.py", "OTHER = 2\n")
    _write(
        tmp_path,
        "tests/unit/test_core.py",
        "from pkg.core import VALUE\n",
    )
    _write(
        tmp_path,
        "tests/unit/test_service.py",
        "from pkg.service import VALUE\n",
    )
    _write(
        tmp_path,
        "tests/unit/test_unrelated.py",
        "from pkg.unrelated import OTHER\n",
    )
    _write(tmp_path, "tests/unit/test_architecture.py")
    _write(tmp_path, "tests/unit/test_layer_contracts.py")
    _write(tmp_path, "tests/unit/test_contract_compat.py")
    _write(tmp_path, "tests/smoke/test_system_smoke.py")
    _write(tmp_path, "tests/conftest.py")
    _write(tmp_path, "pyproject.toml")
    _write(tmp_path, "docs/guide.md", "# guide\n")
    return tmp_path


def test_module_for_path_handles_packages() -> None:
    module = _load()
    assert module._module_for_path("src/pkg/core.py") == "pkg.core"
    assert module._module_for_path("src/pkg/__init__.py") == "pkg"
    assert module._module_for_path("docs/guide.md") is None


def test_low_level_change_selects_transitive_tests(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)
    plan = module.select(root, ["src/pkg/core.py"])

    assert plan.fallback is False
    assert "tests/unit/test_core.py" in plan.tests
    assert "tests/unit/test_service.py" in plan.tests
    assert "tests/unit/test_unrelated.py" not in plan.tests


def test_package_init_change_selects_submodule_tests(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)
    plan = module.select(root, ["src/pkg/__init__.py"])

    assert plan.fallback is False
    assert "tests/unit/test_core.py" in plan.tests
    assert "tests/unit/test_service.py" in plan.tests


def test_changed_test_runs_itself(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)
    plan = module.select(root, ["tests/unit/test_service.py"])

    assert plan.fallback is False
    assert "tests/unit/test_service.py" in plan.tests


def test_docs_only_change_runs_nothing(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)
    plan = module.select(root, ["docs/guide.md"])

    assert plan.fallback is False
    assert plan.tests == []
    assert module.build_command(plan, root) == []


def test_test_infrastructure_falls_back(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)

    for path in (
        "pyproject.toml",
        "tests/conftest.py",
        "scripts/x.py",
        ".github/workflows/ci.yml",
        "tests/eval/tasks.py",
    ):
        plan = module.select(root, [path])
        assert plan.fallback is True, path
        assert "-m" in module.build_command(plan, root)


def test_deleted_source_falls_back(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)
    plan = module.select(root, ["src/pkg/removed.py"])
    assert plan.fallback is True


def test_unmapped_source_falls_back(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)
    _write(root, "src/pkg/orphan.py", "X = 1\n")
    _write(root, "tests/unit/test_other.py", "from pathlib import Path\n")
    plan = module.select(root, ["src/pkg/orphan.py"])
    assert plan.fallback is True


def test_parse_imports_handles_relative_imports(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)
    _write(
        root,
        "src/pkg/nested.py",
        "from .core import VALUE\nfrom . import service\n",
    )
    known = set(module.source_modules(root))
    deps = module.parse_imports(
        root / "src/pkg/nested.py", "pkg.nested", known
    )
    assert "pkg.core" in deps
    assert "pkg.service" in deps


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_changed_paths_includes_worktree_and_untracked(tmp_path) -> None:
    module = _load()
    root = _repo(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")

    (root / "src/pkg/core.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "untracked.py").write_text("X = 1\n", encoding="utf-8")

    args = SimpleNamespace(base=None, staged=False, worktree=True)
    paths = module.changed_paths(root, args)
    assert "src/pkg/core.py" in paths
    assert "untracked.py" in paths

    _git(root, "add", "src/pkg/core.py")
    staged = module.changed_paths(
        root, SimpleNamespace(base=None, staged=True, worktree=False)
    )
    assert staged == ["src/pkg/core.py"]


def test_cli_help_is_available() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--base" in result.stdout
    assert "--exec" in result.stdout
