"""Canonical storage-root migration and cross-process convergence."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from xtalk.storage import _converge_posix_root


def test_new_install_uses_modern_root(tmp_path: Path):
    modern = tmp_path / ".xtalk"
    legacy = tmp_path / ".claude" / "xtalk"

    assert _converge_posix_root(modern, legacy) == modern
    assert not modern.exists()


def test_legacy_only_install_gets_canonical_symlink(tmp_path: Path):
    modern = tmp_path / ".xtalk"
    legacy = tmp_path / ".claude" / "xtalk"
    legacy.mkdir(parents=True)
    (legacy / "history.jsonl").write_text("legacy", encoding="utf-8")

    first = _converge_posix_root(modern, legacy)
    second = _converge_posix_root(modern, legacy)

    assert first == second == modern
    assert modern.is_symlink()
    assert (modern / "history.jsonl").read_text(encoding="utf-8") == "legacy"
    assert modern.samefile(legacy)


def test_conflicting_stores_fail_loudly(tmp_path: Path):
    modern = tmp_path / ".xtalk"
    legacy = tmp_path / ".claude" / "xtalk"
    modern.mkdir()
    legacy.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="conflicting xtalk stores"):
        _converge_posix_root(modern, legacy)


@pytest.mark.skipif(os.name == "nt", reason="POSIX legacy migration only")
def test_concurrent_mcp_processes_converge_on_same_root(tmp_path: Path):
    legacy = tmp_path / ".claude" / "xtalk"
    legacy.mkdir(parents=True)
    env = dict(os.environ)
    env.pop("XTALK_HOME", None)
    env["HOME"] = str(tmp_path)
    command = [sys.executable, "-c", "from xtalk.storage import XTALK_ROOT; print(XTALK_ROOT)"]

    repo_root = Path(__file__).parents[1]
    processes = [
        subprocess.Popen(command, cwd=repo_root, env=env, stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    roots = [process.communicate(timeout=10)[0].strip() for process in processes]

    assert all(process.returncode == 0 for process in processes)
    assert roots == [str(tmp_path / ".xtalk")] * 4
    assert (tmp_path / ".xtalk").samefile(legacy)
