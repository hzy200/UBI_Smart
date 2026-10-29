"""安装包脚本校验器本身要有效：坏输入必须被拦下。

门禁最怕"永远返回 0"。这里逐条构造坏输入，确认 check_installer 真的能报出来。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def guard():
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_installer

    return check_installer


GOOD_ISS = """\
#ifndef AppVersion
  #define AppVersion "{version}"
#endif

[Setup]
AppVersion={{#AppVersion}}
PrivilegesRequired=lowest
SetupIconFile=icon.ico

[Files]
Source: "dist\\*"; DestDir: "{{app}}"; Flags: recursesubdirs
"""

GOOD_PS1 = """\
$Version = (& python -c "import coding_agent; print(coding_agent.__version__)").Trim()
& $Iscc "/DAppVersion=$Version" "packaging\\installer\\CodingAgent.iss"
"""


def _tree(tmp_path: Path, *, iss: str = "", ps1: str = "", version: str = "1.2.3"):
    """造一份最小可校验的文件树。"""
    packaging = tmp_path / "packaging" / "installer"
    packaging.mkdir(parents=True)
    (packaging / "icon.ico").write_bytes(b"icon")
    (packaging / "dist").mkdir()
    (packaging / "CodingAgent.iss").write_text(
        iss or GOOD_ISS.format(version=version), encoding="utf-8"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "build_installer.ps1").write_text(ps1 or GOOD_PS1, encoding="utf-8")
    init = tmp_path / "src" / "coding_agent"
    init.mkdir(parents=True)
    (init / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    return (
        packaging / "CodingAgent.iss",
        scripts / "build_installer.ps1",
        init / "__init__.py",
    )


def test_good_tree_passes(guard, tmp_path) -> None:
    assert guard.check(*_tree(tmp_path)) == []


def test_hardcoded_app_version_is_rejected(guard, tmp_path) -> None:
    iss = GOOD_ISS.format(version="1.2.3").replace(
        "AppVersion={#AppVersion}", "AppVersion=1.2.3"
    )
    errors = guard.check(*_tree(tmp_path, iss=iss))
    assert any("占位符" in item for item in errors)


def test_fallback_version_drift_is_rejected(guard, tmp_path) -> None:
    iss = GOOD_ISS.format(version="0.9.0")  # 兜底 0.9.0，包版本 1.2.3
    errors = guard.check(*_tree(tmp_path, iss=iss))
    assert any("不一致" in item for item in errors)


def test_hardcoded_version_in_build_script_is_rejected(guard, tmp_path) -> None:
    ps1 = GOOD_PS1.replace('"/DAppVersion=$Version"', '"/DAppVersion=0.1.0"')
    errors = guard.check(*_tree(tmp_path, ps1=ps1))
    assert any("写死" in item for item in errors)


def test_privileged_install_is_rejected(guard, tmp_path) -> None:
    iss = GOOD_ISS.format(version="1.2.3").replace(
        "PrivilegesRequired=lowest", "PrivilegesRequired=admin"
    )
    errors = guard.check(*_tree(tmp_path, iss=iss))
    assert any("lowest" in item for item in errors)


def test_uninstall_deleting_user_data_is_rejected(guard, tmp_path) -> None:
    iss = (
        GOOD_ISS.format(version="1.2.3")
        + '\n[UninstallDelete]\nType: filesandordirs; Name: "{localappdata}\\CodingAgent"\n'
    )
    errors = guard.check(*_tree(tmp_path, iss=iss))
    assert any("用户数据" in item for item in errors)


def test_missing_icon_is_rejected(guard, tmp_path) -> None:
    paths = _tree(tmp_path)
    (tmp_path / "packaging" / "installer" / "icon.ico").unlink()
    errors = guard.check(*paths)
    assert any("SetupIconFile" in item for item in errors)
