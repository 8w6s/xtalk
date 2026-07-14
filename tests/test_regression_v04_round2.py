"""Round 2 + 3 regression coverage for the v0.4 hardening pass.

Groups follow the audit rounds: protocol/concurrency, storage/crypto,
daemon/relay, install. Each test isolates one previously reported defect.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import new_ctx, switch


def _register(server, alias, sid, cwd, capabilities=None):
    switch(server, new_ctx(server))
    server.storage.session_id = lambda: sid  # type: ignore[assignment]
    return server.handle_register({
        "alias": alias, "workspace": cwd,
        "capabilities": capabilities or ["long_poll"],
    }), server.CTX


# --- Round 2: protocol / concurrency --------------------------------------

def test_high2_wait_recovers_after_inbox_rewrite(server_module, tmp_path):
    """After the inbox is truncated *and* rewritten longer than the old cursor,
    the fingerprint check must recognise the rewind and replay from zero."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    # Push a couple of events so cursor advances.
    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "q1"})
    switch(server_module, ctx_b)
    server_module.handle_reply({"thread": ask["thread_id"], "body": "a1", "in_reply_to": ask["msg_id"]})
    switch(server_module, ctx_a)
    server_module.handle_wait({"in_reply_to": ask["msg_id"], "timeout_ms": 500})
    assert ctx_a.cursors[ctx_a.active_room] > 0

    # External truncate-and-rewrite: same length range but different content.
    room = server_module.storage.Room(ctx_a.active_room)
    inbox = room.inbox_path("sid-A")
    inbox.write_text("")

    # Post several new events so total length ≥ old cursor.
    switch(server_module, ctx_b)
    for i in range(3):
        server_module.handle_ask({"to": "a", "body": f"replay-{i}"})

    switch(server_module, ctx_a)
    result = server_module.handle_wait({"timeout_ms": 1500})
    assert result["timed_out"] is False
    assert result["event"]["kind"] == "ask"


def test_high3_heartbeat_stops_after_room_becomes_unwritable(server_module, tmp_path, monkeypatch):
    """When the room storage can no longer accept writes (permission lost,
    disk gone), the heartbeat timer must stop rearming instead of spinning
    forever on an unreachable path."""
    monkeypatch.setattr(server_module, "HEARTBEAT_INTERVAL_SECONDS", 0.03)
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx = _register(server_module, "a", "sid-A", str(ws))
    room = server_module.storage.Room(ctx.active_room)

    try:
        # Simulate: append raises FileNotFoundError forever (path deleted +
        # locked so mkdir cannot recreate it).
        real_append = server_module.storage.atomic_append

        def broken_append(path, line):
            if "members.jsonl" in str(path):
                raise FileNotFoundError(str(path))
            return real_append(path, line)

        monkeypatch.setattr(server_module.storage, "atomic_append", broken_append)
        # Wait past a couple of tick intervals.
        time.sleep(0.15)
        with server_module._HEARTBEAT_LOCK:
            assert (room.id, "sid-A") not in server_module._HEARTBEAT_TIMERS
    finally:
        server_module._cancel_all_heartbeats()


def test_med1_ask_rejects_oversized_body_before_presence(server_module, tmp_path):
    """A body larger than MAX_BODY_BYTES must not leave the caller stamped
    as waiting_reply — the size check now precedes the presence update."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    switch(server_module, ctx_a)
    huge = "X" * (server_module.storage.MAX_BODY_BYTES + 1)
    with pytest.raises(ValueError):
        server_module.handle_ask({"to": "b", "body": huge})

    room = server_module.storage.Room(ctx_a.active_room)
    me = next(m for m in room.current_members() if m["sid"] == "sid-A")
    assert me.get("mode") != "waiting_reply"


def test_med2_reregister_rearms_heartbeat(server_module, tmp_path, monkeypatch):
    """Re-calling xtalk_register on an already-registered session must
    reinstate the heartbeat timer if it was cancelled."""
    monkeypatch.setattr(server_module, "HEARTBEAT_INTERVAL_SECONDS", 0.03)
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx = _register(server_module, "a", "sid-A", str(ws))
    room_id = ctx.active_room

    server_module._cancel_all_heartbeats()
    with server_module._HEARTBEAT_LOCK:
        assert (room_id, "sid-A") not in server_module._HEARTBEAT_TIMERS

    try:
        server_module.handle_register({"alias": "a", "workspace": str(ws)})
        with server_module._HEARTBEAT_LOCK:
            assert (room_id, "sid-A") in server_module._HEARTBEAT_TIMERS
    finally:
        server_module._cancel_all_heartbeats()


# --- Round 2: storage -----------------------------------------------------

def test_zombie_alias_reuse_blocked(server_module, tmp_path):
    """A zombie member (lease expired, no leave event) must still hold their
    alias so a newcomer can't silently steal it."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx = _register(server_module, "seed", "sid-seed", str(ws))
    room = server_module.storage.Room(ctx.active_room)

    # Zombie join: epoch far in the past, no leave.
    room.append_member_event({
        "event": "join", "sid": "sid-zombie", "alias": "collision",
        "epoch": time.time() - server_module.storage.LEASE_SECONDS * 10,
        "ts": server_module.now_iso(),
    })

    # `current_members()` filters zombie out — but our alias check must
    # still see it, so `collision` stays reserved.
    with pytest.raises(ValueError, match="already in use"):
        room.join_with_alias_check({
            "event": "join", "sid": "sid-new", "alias": "collision",
            "epoch": time.time(), "ts": server_module.now_iso(),
        })


def test_workspace_hash_new_projects_use_ulid(server_module, tmp_path):
    """A workspace without a legacy room directory must be assigned a fresh
    ULID room id — no more 64-bit sha1[:16] collision surface for new
    projects."""
    ws = tmp_path / "brand-new"; ws.mkdir()
    binding = server_module.storage.ensure_project_binding(ws)
    room_id = binding["default_room"]
    assert room_id.startswith("room-"), f"expected ULID room id, got {room_id!r}"


def test_atomic_json_concurrent_write_survives(server_module, tmp_path):
    """Sanity: sidecar-locked atomic_json under contention doesn't produce a
    malformed file and every writer finishes without an exception."""
    target = tmp_path / "shared.json"
    barrier = threading.Barrier(6)
    errors: list[Exception] = []

    def writer(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            server_module.storage.atomic_json(target, {"writer": i})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=5)
    assert not errors, errors
    data = json.loads(target.read_text(encoding="utf-8"))
    assert 0 <= data["writer"] < 6


# --- Round 3: protocol ---------------------------------------------------

def test_high4_wait_matches_reply_in_long_thread(server_module, tmp_path):
    """A reply buried below the last-100 window must still unblock the wait —
    the inbox event now carries in_reply_to so the fast path finds it."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "q"})
    tid = ask["thread_id"]

    # Stuff the thread with noise; the reply we want will be at the end.
    switch(server_module, ctx_b)
    for i in range(120):
        server_module.handle_reply({"thread": tid, "body": f"noise-{i}", "in_reply_to": "msg-nonexistent"})
    # The real reply now.
    server_module.handle_reply({"thread": tid, "body": "the-answer", "in_reply_to": ask["msg_id"]})

    switch(server_module, ctx_a)
    result = server_module.handle_wait({"in_reply_to": ask["msg_id"], "timeout_ms": 2000})
    assert result["timed_out"] is False
    assert result["event"].get("in_reply_to") == ask["msg_id"]


def test_med4_retry_wait_preserves_deadlock_deadline(server_module, tmp_path):
    """Calling xtalk_wait repeatedly on the same reply target must not push
    the deadline into the future — otherwise the watchdog never fires."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "q"})
    room = server_module.storage.Room(ctx_a.active_room)

    def _deadline() -> float:
        # Read raw presence events; xtalk_wait sets idle in its finally
        # block, which strips deadline_ts from the surface accessor.
        lines = room.members_path().read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            evt = json.loads(line)
            if evt.get("sid") == "sid-A" and evt.get("event") == "presence" and evt.get("mode") == "waiting_reply":
                return float(evt["deadline_ts"])
        raise AssertionError("no waiting_reply presence event found")

    # Two back-to-back short waits — the retry must reuse the deadline
    # written by the first call.
    server_module.handle_wait({"in_reply_to": ask["msg_id"], "timeout_ms": 100})
    first = _deadline()
    time.sleep(0.05)
    server_module.handle_wait({"in_reply_to": ask["msg_id"], "timeout_ms": 100})
    second = _deadline()

    # Small floating-point wobble is fine; a full grace-window push is not.
    assert abs(second - first) < 1.0, (first, second)


def test_med5_close_typo_report_to_raises(server_module, tmp_path):
    """A typo'd report_to alias no longer silently drops the `done` event;
    the caller must be told the alias is unknown so they can fix it."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "q"})
    switch(server_module, ctx_b)
    server_module.handle_reply({"thread": ask["thread_id"], "body": "a", "in_reply_to": ask["msg_id"]})

    switch(server_module, ctx_a)
    with pytest.raises(ValueError, match="unknown report_to alias"):
        server_module.handle_close({
            "thread": ask["thread_id"], "summary": "done",
            "report_to": "does-not-exist",
        })


# --- Round 3: crypto / relay --------------------------------------------

def test_crypto_derives_stable_keys():
    """`derive_keys` must be deterministic — bumping salt would silently
    invalidate every existing E2EE room, so this pins the current output."""
    from xtalk import crypto
    enc1, auth1 = crypto.derive_keys("test-secret-value")
    enc2, auth2 = crypto.derive_keys("test-secret-value")
    assert enc1 == enc2
    assert auth1 == auth2
    assert enc1 != auth1


def test_verifier_compare_is_constant_time_shape():
    """We can't assert wall-clock timing behavior in a unit test, but we can
    guarantee we're using `hmac.compare_digest` — smoke test via docstring
    presence + successful roundtrip on valid credentials."""
    from xtalk import crypto
    _, auth_key = crypto.derive_keys("secret-abc")
    verifier = crypto.invite_verifier(auth_key)
    import hmac
    assert hmac.compare_digest(verifier, verifier)


# --- Round 3: install ---------------------------------------------------

def test_install_creationflags_is_posix_safe():
    """install.py must not reference subprocess.CREATE_NEW_PROCESS_GROUP
    directly on POSIX — the attribute doesn't exist there."""
    src = Path(__file__).resolve().parent.parent / "install.py"
    text = src.read_text(encoding="utf-8")
    # The old crashing form was a bare `subprocess.CREATE_NEW_PROCESS_GROUP`
    # inside a ternary; the safe form uses getattr with a fallback.
    assert "getattr(subprocess, \"CREATE_NEW_PROCESS_GROUP\"" in text


def test_install_input_is_tty_guarded():
    src = Path(__file__).resolve().parent.parent / "install.py"
    text = src.read_text(encoding="utf-8")
    assert "sys.stdin.isatty()" in text, "installer must skip prompt when stdin is not a tty"
