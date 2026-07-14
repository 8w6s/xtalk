"""Deadlock prevention: presence, ask warnings, watchdog-emitted deadlock_hint."""
from __future__ import annotations

import time

import pytest

from tests.conftest import new_ctx, switch


def _register(server, alias, sid, cwd, capabilities=None):
    switch(server, new_ctx(server))
    server.storage.session_id = lambda: sid  # type: ignore[assignment]
    return server.handle_register({
        "alias": alias, "workspace": cwd,
        "capabilities": capabilities or ["long_poll"],
    }), server.CTX


def test_presence_reported_by_discover_after_listen(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "coder", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "reviewer", "sid-B", str(ws))
    switch(server_module, ctx_b)
    server_module.handle_listen({})
    switch(server_module, ctx_a)
    disc = server_module.handle_discover({})
    reviewer = next(m for m in disc["members"] if m["alias"] == "reviewer")
    assert reviewer["mode"] == "listening"


def test_ask_no_warning_when_target_listening(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "coder", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "reviewer", "sid-B", str(ws))
    switch(server_module, ctx_b)
    server_module.handle_listen({})
    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "reviewer", "body": "hi"})
    assert "warning" not in ask
    assert ask.get("deadlock_risk") is not True


def test_ask_warns_when_all_targets_waiting_reply(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    # B enters waiting_reply first (asks A)
    switch(server_module, ctx_b)
    server_module.handle_ask({"to": "a", "body": "q from B"})

    # Now A tries to ask B → should be warned
    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "q from A"})
    assert "warning" in ask
    assert ask["deadlock_risk"] is True


def test_wait_command_contains_deadlock_hint_pattern(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))
    switch(server_module, ctx_b)
    ask = server_module.handle_ask({"to": "a", "body": "?"})
    assert '"kind":"deadlock_hint"' in ask["wait_command"]
    assert f'"in_reply_to":"{ask["msg_id"]}"' in ask["wait_command"]


def test_reply_resets_presence_to_idle(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "q"})
    # A now in waiting_reply
    disc = server_module.handle_discover({})
    assert next(m for m in disc["members"] if m["sid"] == "sid-A")["mode"] == "waiting_reply"

    switch(server_module, ctx_b)
    server_module.handle_reply({"thread": ask["thread_id"], "body": "a", "in_reply_to": ask["msg_id"]})
    disc = server_module.handle_discover({})
    assert next(m for m in disc["members"] if m["sid"] == "sid-B")["mode"] == "idle"


def test_deadlock_detector_emits_hint_when_grace_expired(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    # Both enter waiting_reply
    switch(server_module, ctx_a)
    server_module.handle_ask({"to": "b", "body": "q1"})
    switch(server_module, ctx_b)
    server_module.handle_ask({"to": "a", "body": "q2"})

    # Fast-forward deadline by rewriting presence with a past deadline
    room = server_module.storage.Room(ctx_a.active_room)
    past = time.time() - 1
    room.append_presence("sid-A", "a", "waiting_reply", target_msg_id="msg-x", deadline_ts=past)
    room.append_presence("sid-B", "b", "waiting_reply", target_msg_id="msg-y", deadline_ts=past)

    waiters = room.check_deadlock()
    assert set(waiters or []) == {"sid-A", "sid-B"}

    room.emit_deadlock_hint(waiters or [])

    # Both should see a deadlock_hint in their inbox
    for sid in ("sid-A", "sid-B"):
        inbox_lines = room.inbox_path(sid).read_text().splitlines()
        hints = [ln for ln in inbox_lines if '"kind":"deadlock_hint"' in ln]
        assert hints, f"no hint delivered to {sid}"


def test_wait_returns_deadlock_hint_when_stuck(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    switch(server_module, ctx_a)
    ask_a = server_module.handle_ask({"to": "b", "body": "q1"})
    switch(server_module, ctx_b)
    ask_b = server_module.handle_ask({"to": "a", "body": "q2"})

    # Force past deadline
    room = server_module.storage.Room(ctx_a.active_room)
    past = time.time() - 1
    room.append_presence("sid-A", "a", "waiting_reply", target_msg_id=ask_a["msg_id"], deadline_ts=past)
    room.append_presence("sid-B", "b", "waiting_reply", target_msg_id=ask_b["msg_id"], deadline_ts=past)

    switch(server_module, ctx_a)
    # xtalk_wait should detect deadlock during its opportunistic check
    # (its 5s check window is not hit inside our short timeout, so pre-seed the hint)
    room.emit_deadlock_hint(["sid-A", "sid-B"])
    result = server_module.handle_wait({"in_reply_to": ask_a["msg_id"], "timeout_ms": 1000})
    assert result["timed_out"] is False
    assert result.get("deadlock_hint") is True
    assert result["event"]["kind"] == "deadlock_hint"


def test_presence_tool_explicit(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    switch(server_module, ctx_a)
    r = server_module.handle_presence({"mode": "listening"})
    assert r["presence"] == "listening"
    disc = server_module.handle_discover({})
    assert next(m for m in disc["members"] if m["sid"] == "sid-A")["mode"] == "listening"
    r2 = server_module.handle_presence({"mode": "idle"})
    assert r2["presence"] == "idle"
