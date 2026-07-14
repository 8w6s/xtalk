"""E2EE round-trip and tamper detection through the MCP handlers."""
from __future__ import annotations

import pytest

from tests.conftest import new_ctx, switch


def _register(server, alias, sid, cwd):
    switch(server, new_ctx(server))
    server.storage.session_id = lambda: sid  # type: ignore[assignment]
    return server.handle_register({"alias": alias, "workspace": cwd, "capabilities": ["long_poll"]}), server.CTX


def test_e2ee_round_trip(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()

    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    switch(server_module, ctx_a)
    created = server_module.handle_room_create({"name": "secret", "e2ee": True})
    assert created["e2ee"] is True

    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))
    switch(server_module, ctx_b)
    server_module.handle_room_join({"invite": created["invite"], "alias": "reader"})

    switch(server_module, ctx_a)
    server_module.handle_room_use({"room": created["room"]})
    ask = server_module.handle_ask({"to": "reader", "body": "top secret"})

    # On-disk thread should not contain the plaintext
    room_path = server_module.storage.Room(created["room"]).thread_path(ask["thread_id"])
    on_disk = room_path.read_text(encoding="utf-8")
    assert "top secret" not in on_disk
    assert '"enc"' in on_disk

    # Reader with the key sees plaintext
    switch(server_module, ctx_b)
    server_module.handle_room_use({"room": created["room"]})
    read = server_module.handle_read({"thread": ask["thread_id"]})
    assert read["messages"][0]["body"] == "top secret"


def test_e2ee_wrong_key_fails_cleanly(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()

    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    switch(server_module, ctx_a)
    created = server_module.handle_room_create({"name": "s1", "e2ee": True})
    ask = server_module.handle_ask({"to": "*", "body": "hush"})

    # New session joins without the invite — no key
    _, ctx_c = _register(server_module, "c", "sid-C", str(ws))
    switch(server_module, ctx_c)
    # Manually attach membership without decrypting (simulate someone reading
    # the raw JSONL after the fact)
    ctx_c.memberships[created["room"]] = "peek"
    ctx_c.active_room = created["room"]
    read = server_module.handle_read({"thread": ask["thread_id"], "room": created["room"]})
    assert read["messages"][0]["body"] == "[encrypted — missing key]"
