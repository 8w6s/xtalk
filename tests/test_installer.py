"""Cross-platform bootstrap path behavior."""
from __future__ import annotations

import importlib.util
from pathlib import Path


INSTALLER = Path(__file__).parents[1] / "install.py"
SHELL_INSTALLER = Path(__file__).parents[1] / "install.sh"


def test_shell_installer_uses_official_skills_cli_and_maps_antigravity():
    source = SHELL_INSTALLER.read_text()
    assert 'skills add "$REPO" --skill xtalk --global --yes' in source
    assert "antigravity) agent=antigravity-cli" in source
    assert 'python3 "$HERE/install.py" --skip-skill-copy "$@"' in source
    assert 'if [ "${NO_COLOR:-}" = "" ]; then' in source
    assert "[ -t 1 ]" not in source
    assert "ESC=$(printf '\\033')" in source
SPEC = importlib.util.spec_from_file_location("xtalk_installer", INSTALLER)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_posix_venv_entrypoints():
    assert installer.venv_bin("python", windows=False) == installer.VENV / "bin" / "python"
    assert installer.venv_bin("xtalk-mcp", windows=False) == installer.VENV / "bin" / "xtalk-mcp"


def test_windows_venv_entrypoints():
    assert installer.venv_bin("python", windows=True) == installer.VENV / "Scripts" / "python.exe"
    assert installer.venv_bin("xtalk-mcp", windows=True) == installer.VENV / "Scripts" / "xtalk-mcp.exe"
