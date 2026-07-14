"""Shared pytest fixtures: isolated XTALK_HOME per test, fresh SessionCtx per session role."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def xtalk_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate XTALK_HOME to a temp dir and reload storage/server so their
    module-level XTALK_ROOT picks it up."""
    tmp = Path(tempfile.mkdtemp(prefix="xtalk-test-"))
    monkeypatch.setenv("XTALK_HOME", str(tmp))
    from xtalk import storage
    importlib.reload(storage)
    from xtalk import server
    importlib.reload(server)
    server.storage.XTALK_ROOT = storage.XTALK_ROOT
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def server_module(xtalk_home: Path):
    from xtalk import server
    return server


def new_ctx(server_module, sid: str | None = None):
    """Create a fresh SessionCtx, optionally with a forced sid."""
    ctx = server_module.SessionCtx()
    if sid is not None:
        ctx.sid = sid
    return ctx


def switch(server_module, ctx) -> None:
    server_module.CTX = ctx
