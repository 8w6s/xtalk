"""Multi-room semantics: register + custom rooms, alias uniqueness, room_use."""
from __future__ import annotations

import pytest

from tests.conftest import new_ctx, switch


def _register(server, alias, sid, cwd, capabilities=None):
    switch(server, new_ctx(server, sid=None))
    server.storage.session_id = lambda: sid  # type: ignore[assignment]
    return server.handle_register({"alias": alias, "workspace": cwd, "capabilities": capabilities or ["long_poll"]}), server.CTX


def test_workspace_room_shared_between_sessions(server_module, xtalk_home, tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()

    r_a, ctx_a = _register(server_module, "coder", "sid-A", str(ws))
    assert r_a["other_members"] == []
    r_b, ctx_b = _register(server_module, "reviewer", "sid-B", str(ws))
    assert any(m["alias"] == "coder" for m in r_b["other_members"])
    assert r_a["room_id"] == r_b["room_id"]
    room = server_module.Room(r_a["room_id"])
    joined = [server_module.json.loads(line) for line in room.inbox_path("sid-A").read_text().splitlines()]
    assert joined[-1]["kind"] == "member_joined"
    assert joined[-1]["from_alias"] == "reviewer"
    manifest = ws / ".xtalk" / "project.json"
    assert manifest.exists()
    assert r_a["project_manifest"] == str(manifest)
    assert r_a["project_id"] == r_b["project_id"]
    assert r_a["room_restored"] is False
    assert r_b["room_restored"] is True


def test_project_manifest_restores_room_after_workspace_moves(server_module, tmp_path):
    ws = tmp_path / "original"
    ws.mkdir()
    first, _ = _register(server_module, "coder", "sid-A", str(ws))

    moved = tmp_path / "moved"
    ws.rename(moved)
    second, _ = _register(server_module, "reviewer", "sid-B", str(moved))

    assert second["room_id"] == first["room_id"]
    assert second["project_id"] == first["project_id"]
    assert second["room_restored"] is True
    assert server_module.Room(second["room_id"]).metadata()["workspace_path"] == str(moved)


def test_invalid_project_manifest_is_rejected(server_module, tmp_path):
    ws = tmp_path / "proj"
    (ws / ".xtalk").mkdir(parents=True)
    (ws / ".xtalk" / "project.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid xtalk project manifest"):
        _register(server_module, "coder", "sid-A", str(ws))


def test_mcp_startup_initializes_project_asset(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()

    binding = server_module.initialize_project_asset(ws)

    assert (ws / ".xtalk" / "project.json").exists()
    assert binding["default_room"]


def test_alias_collision_within_room_rejected(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()

    _register(server_module, "shared", "sid-A", str(ws))
    with pytest.raises(ValueError, match="alias already in use"):
        _register(server_module, "shared", "sid-B", str(ws))


def test_same_alias_different_rooms_allowed(server_module, tmp_path):
    ws1 = tmp_path / "one"; ws1.mkdir()
    ws2 = tmp_path / "two"; ws2.mkdir()
    _register(server_module, "coder", "sid-A", str(ws1))
    r_b, _ = _register(server_module, "coder", "sid-B", str(ws2))
    assert r_b["room_id"] != _register(server_module, "reviewer", "sid-C", str(ws1))[0]["room_id"]


def test_room_create_join_switch(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()

    _, ctx_a = _register(server_module, "coder", "sid-A", str(ws))
    switch(server_module, ctx_a)
    created = server_module.handle_room_create({"name": "review", "alias": "author"})
    assert created["invite"].startswith("xtalk://join/")

    _, ctx_b = _register(server_module, "reviewer", "sid-B", str(ws))
    switch(server_module, ctx_b)
    joined = server_module.handle_room_join({"invite": created["invite"], "alias": "critic"})
    assert joined["room"] == created["room"]

    listing = server_module.handle_room_list({})
    assert len(listing["rooms"]) == 2

    # switch active room and ask should target the new room
    server_module.handle_room_use({"room": created["room"]})
    switch(server_module, ctx_a)
    server_module.handle_room_use({"room": created["room"]})
    ask = server_module.handle_ask({"to": "critic", "body": "look at this"})
    assert ask["room"] == created["room"]


def test_room_join_bad_secret(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    switch(server_module, ctx_a)
    invite = server_module.handle_room_create({"name": "r"})["invite"]

    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))
    switch(server_module, ctx_b)
    # tamper: change last chars of secret
    tampered = invite[:-4] + "XXXX"
    with pytest.raises(ValueError, match="invalid invite secret"):
        server_module.handle_room_join({"invite": tampered, "alias": "sneak"})


def test_leave_notifies_remaining_room_members(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    switch(server_module, ctx_b)
    server_module.handle_leave({})

    room = server_module.Room(ctx_a.active_room)
    events = [server_module.json.loads(line) for line in room.inbox_path("sid-A").read_text().splitlines()]
    assert events[-1]["kind"] == "member_left"
    assert events[-1]["from_alias"] == "b"
