"""Cross-platform bootstrap path behavior."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


INSTALLER = Path(__file__).parents[1] / "install.py"
SHELL_INSTALLER = Path(__file__).parents[1] / "install.sh"


def test_shell_installer_uses_official_skills_cli_and_maps_antigravity():
    source = SHELL_INSTALLER.read_text()
    assert 'python3 "$HERE/install.py" --mcp-only --quiet-pip "$@"' in source
    assert 'python3 "$HERE/install.py" --skill-only "$@"' in source
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


def test_runtime_is_outside_repository():
    assert installer.ROOT not in installer.VENV.parents


def test_default_clients_are_explicit():
    assert installer.selected_clients(None) == ["claude-code", "codex", "cursor", "antigravity"]


def test_skill_installer_targets_only_supported_agents(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/npx")

    def fake_run(*args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, "Installed 1 skill\n", "")

    monkeypatch.setattr(installer, "run", fake_run)
    installer.install_skills(clients=None)
    command = list(seen["args"])
    assert "promptscript" not in command
    for agent in ("claude-code", "codex", "cursor", "antigravity-cli"):
        assert agent in command


def test_skill_installer_rejects_partial_success(monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/npx")
    monkeypatch.setattr(
        installer,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "Failed to install 1\n", ""),
    )
    try:
        installer.install_skills(clients=["codex"])
    except RuntimeError as exc:
        assert "partially" in str(exc)
    else:
        raise AssertionError("partial skill install must fail")
