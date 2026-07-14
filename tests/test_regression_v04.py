"""Regression coverage for the v0.4 hardening pass.

Each test targets one of the bugs uncovered in the audit and would fail on
v0.3.0 unpatched. Names include the finding id for traceability.
"""
from __future__ import annotations

import json
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


def test_h3_wait_recovers_cursor_after_inbox_truncate(server_module, tmp_path):
    """handle_wait must reset a stale cursor when the inbox shrinks below it.

    Before the fix, after any external truncate/rotate of the inbox JSONL, the
    stored cursor pointed past EOF and every subsequent event was silently
    skipped until the file grew back past the old cursor.
    """
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    # A asks B, B replies, A drains it — cursor advances.
    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "hi"})
    switch(server_module, ctx_b)
    server_module.handle_reply({"thread": ask["thread_id"], "body": "hello", "in_reply_to": ask["msg_id"]})
    switch(server_module, ctx_a)
    first = server_module.handle_wait({"in_reply_to": ask["msg_id"], "timeout_ms": 500})
    assert first["timed_out"] is False
    old_cursor = ctx_a.cursors[ctx_a.active_room]
    assert old_cursor > 0

    # Simulate storage reset — truncate A's inbox behind the server's back.
    room = server_module.storage.Room(ctx_a.active_room)
    room.inbox_path("sid-A").write_text("")

    # B posts a fresh event; the recovered cursor must let A pick it up.
    switch(server_module, ctx_b)
    server_module.handle_ask({"to": "a", "body": "second question"})
    switch(server_module, ctx_a)
    result = server_module.handle_wait({"timeout_ms": 1500})

    assert result["timed_out"] is False, "cursor drift regression — event silently skipped"
    assert result["event"]["kind"] == "ask"


def test_m4_close_notifies_report_to_lurker(server_module, tmp_path):
    """`done` events must reach a nominated reporter even if they never posted.

    The old logic derived recipients from thread history alone, so a lurker
    picked as report_to received nothing in their inbox.
    """
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))
    _, ctx_c = _register(server_module, "c", "sid-C", str(ws))  # lurker

    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "q"})
    switch(server_module, ctx_b)
    server_module.handle_reply({"thread": ask["thread_id"], "body": "a", "in_reply_to": ask["msg_id"]})

    switch(server_module, ctx_a)
    server_module.handle_close({
        "thread": ask["thread_id"], "summary": "wrapped up",
        "report_to": "c",
    })

    room = server_module.storage.Room(ctx_c.active_room)
    inbox_lines = room.inbox_path("sid-C").read_text().splitlines()
    done_events = [ln for ln in inbox_lines if '"kind":"done"' in ln]
    assert done_events, "lurker reporter never received the `done` notification"


def test_h4_atomic_json_serializes_concurrent_writers(server_module, tmp_path):
    """Two threads writing the same JSON path concurrently must not clobber.

    Without the sidecar lock, one writer's tmp file could be renamed on top
    of the other's, silently losing whichever change lost the race. The lock
    makes writes appear serialized — the final file is exactly one of the
    payloads written, and every write completed without raising.
    """
    target = tmp_path / "shared.json"
    barrier = threading.Barrier(8)
    errors: list[Exception] = []

    def writer(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            server_module.storage.atomic_json(target, {"writer": i, "ts": time.time()})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent atomic_json raised: {errors}"
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "writer" in data and 0 <= data["writer"] < 8


def test_h1_join_alias_race_stays_unique(server_module, tmp_path):
    """`_join` must not admit two members with the same alias in the same room.

    The old check-then-append was TOCTOU: two threads could both observe the
    alias free and both write a join event. The lock-guarded check-and-append
    keeps the alias unique even under contention.
    """
    ws = tmp_path / "proj"
    ws.mkdir()
    _, seed = _register(server_module, "seed", "sid-seed", str(ws))
    room_id = seed.active_room
    room = server_module.storage.Room(room_id)

    errors: list[str] = []
    ok_count = 0
    lock = threading.Lock()

    def racer(i: int) -> None:
        nonlocal ok_count
        ctx = server_module.SessionCtx()
        ctx.sid = f"sid-race{i}"
        ctx.client = "race"
        ctx.memberships[room_id] = None
        try:
            # Call the shared helper directly so we bypass the per-CTX state
            # the register-path would otherwise hold.
            room.join_with_alias_check({
                "event": "join", "sid": ctx.sid, "alias": "collision",
                "epoch": time.time(), "ts": server_module.now_iso(),
                "client": "race", "pid": 1,
            })
            with lock:
                ok_count += 1
        except ValueError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert ok_count == 1, f"expected exactly one winner, got {ok_count}"
    members = room.current_members()
    collisions = [m for m in members if m.get("alias") == "collision"]
    assert len(collisions) == 1, f"duplicate alias slipped through: {collisions}"


def test_m5_deadlock_hint_dedup_survives_intervening_traffic(server_module, tmp_path):
    """Dedup window must be wide enough to catch a hint buried under noise.

    Before the fix, `read_thread(count=5)` was too small: any 5+ unrelated
    messages between two flap cycles pushed the earlier hint out of view and
    a second identical hint was emitted for the same waiter set.
    """
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))
    room = server_module.storage.Room(ctx_a.active_room)

    room.emit_deadlock_hint(["sid-A", "sid-B"])

    # Fill the system thread with 10 unrelated messages.
    system_tid = f"tid-system-{room.id[:16]}"
    for i in range(10):
        msg = server_module.storage.Message(
            msg_id=f"msg-noise-{i:02d}",
            ts=server_module.now_iso(),
            from_sid="sid-system",
            from_alias="system",
            to=["*"],
            kind="notice",
            body=f"noise {i}",
        )
        server_module.storage.atomic_append(room.thread_path(system_tid), msg.to_json())

    room.emit_deadlock_hint(["sid-A", "sid-B"])

    tail = room.read_thread(system_tid, count=100)
    hints = [m for m in tail if m.kind == "deadlock_hint"]
    assert len(hints) == 1, f"dedup window failed — got {len(hints)} hints"
