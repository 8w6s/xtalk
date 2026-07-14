"""Input validation: path traversal, oversize bodies, malformed ids."""
from __future__ import annotations

import pytest
from pathlib import Path

from xtalk import storage

from tests.conftest import new_ctx, switch


def _register(server, alias, sid, cwd):
    switch(server, new_ctx(server))
    server.storage.session_id = lambda: sid  # type: ignore[assignment]
    return server.handle_register({"alias": alias, "workspace": cwd, "capabilities": ["long_poll"]}), server.CTX


def test_thread_id_path_traversal_rejected(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx = _register(server_module, "a", "sid-A", str(ws))
    switch(server_module, ctx)
    with pytest.raises(ValueError, match="invalid identifier"):
        server_module.handle_read({"thread": "../../../etc/passwd"})


def test_oversize_body_rejected(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))
    switch(server_module, ctx_b)
    with pytest.raises(ValueError):
        server_module.handle_ask({"to": "a", "body": "x" * (10 * 1024)})


def test_alias_too_long_rejected(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    with pytest.raises(ValueError, match="alias must be"):
        _register(server_module, "x" * 100, "sid-A", str(ws))


def test_leave_without_register_returns_note(server_module, tmp_path):
    switch(server_module, new_ctx(server_module))
    result = server_module.handle_leave({})
    assert result["ok"] is True
    assert "not registered" in result.get("note", "")


def test_concurrent_read_retries_sharing_violation(tmp_path, monkeypatch):
    path = tmp_path / "inbox.jsonl"
    path.write_text("event\n")
    original = Path.read_text
    calls = 0

    def flaky_read(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if self == path and calls == 1:
            raise PermissionError("sharing violation")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    assert storage.concurrent_read_text(path) == "event\n"
    assert calls == 2
