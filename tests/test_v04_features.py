"""New surface added in the v0.4 UX pass.

Covers:
- Mention parsing + wake semantics
- Unbounded xtalk_wait (no timeout_ms)
- xtalk_assign / xtalk_ack / xtalk_tasks ledger
- xtalk_unregister full teardown
- xtalk_register with a new alias renames instead of silently ignoring
"""
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


# --- Mention ---------------------------------------------------------

def test_parse_mentions_extracts_alias_tokens():
    from xtalk.storage import parse_mentions
    assert parse_mentions("hey @alice, ping @bob!") == ["alice", "bob"]
    # Duplicates collapse; email-style @ isn't a mention.
    assert parse_mentions("@alice you there @alice") == ["alice"]
    assert parse_mentions("email alice@example.com is not a mention") == []


def test_mention_delivers_wake_event_to_mentioned_sid(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "alice", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "bob", "sid-B", str(ws))
    _, ctx_c = _register(server_module, "carol", "sid-C", str(ws))

    # Alice asks Carol, mentioning Bob. Bob is NOT a direct recipient, so
    # the mention event must be the wake signal that reaches him.
    switch(server_module, ctx_a)
    server_module.handle_ask({"to": "carol", "body": "quick check-in with @bob please"})

    switch(server_module, ctx_b)
    result = server_module.handle_wait({"kinds": ["ask"], "timeout_ms": 1000})
    assert result["timed_out"] is False
    assert result.get("mention") is True
    assert result["event"]["kind"] == "mention"
    assert result["event"]["mentioned_alias"] == "bob"


def test_mention_deduped_when_recipient_is_mentioned(server_module, tmp_path):
    """When the mentioned member is already an explicit recipient, only the
    base event should appear in their inbox — no duplicate mention line."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "alice", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "bob", "sid-B", str(ws))

    switch(server_module, ctx_a)
    server_module.handle_ask({"to": "bob", "body": "hey @bob take a look"})

    room = server_module.storage.Room(ctx_b.active_room)
    inbox_lines = room.inbox_path("sid-B").read_text().splitlines()
    mention_lines = [ln for ln in inbox_lines if '"kind":"mention"' in ln]
    ask_lines = [ln for ln in inbox_lines if '"kind":"ask"' in ln]
    assert len(mention_lines) == 0, "recipient should not get a duplicate mention event"
    assert len(ask_lines) == 1


def test_mention_does_not_fire_for_unknown_alias(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "alice", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "bob", "sid-B", str(ws))

    switch(server_module, ctx_a)
    server_module.handle_broadcast({"body": "hey @carol, are you here?"})

    # Nobody named `carol` in the room → Bob's inbox has the broadcast only.
    switch(server_module, ctx_b)
    result = server_module.handle_wait({"timeout_ms": 500})
    # Broadcast still arrives, but not as a mention.
    assert result["timed_out"] is False
    assert result.get("mention") is not True


# --- Unbounded wait --------------------------------------------------

def test_wait_default_is_unbounded_until_event(server_module, tmp_path):
    """No timeout_ms → wait blocks until an event actually lands. Feed an
    event from another thread to prove the wait can return without a
    deadline."""
    import threading
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    result_box: dict = {}

    def waiter():
        switch(server_module, ctx_a)
        # No timeout_ms passed; unbounded wait relies on the event arriving.
        result_box["result"] = server_module.handle_wait({})

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    # Give the waiter a moment to enter its loop, then post.
    time.sleep(0.3)
    switch(server_module, ctx_b)
    server_module.handle_ask({"to": "a", "body": "wake up"})
    t.join(timeout=3)

    assert not t.is_alive(), "unbounded wait failed to return after event"
    assert result_box["result"]["timed_out"] is False


# --- Task ledger -----------------------------------------------------

def test_assign_creates_pending_task(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_boss = _register(server_module, "boss", "sid-boss", str(ws))
    _, ctx_worker = _register(server_module, "worker", "sid-worker", str(ws))

    switch(server_module, ctx_boss)
    r = server_module.handle_assign({
        "to": "worker", "title": "Ship the feature",
        "description": "Land it before EOW", "priority": "high",
    })
    assert r["task_id"].startswith("task-")
    assert r["status"] == "pending"
    assert r["assignee_sid"] == "sid-worker"

    tasks = server_module.handle_tasks({})
    assert tasks["count"] == 1
    assert tasks["tasks"][0]["title"] == "Ship the feature"


def test_assign_wakes_the_assignee(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_boss = _register(server_module, "boss", "sid-boss", str(ws))
    _, ctx_worker = _register(server_module, "worker", "sid-worker", str(ws))

    switch(server_module, ctx_boss)
    r = server_module.handle_assign({"to": "worker", "title": "Fix bug"})

    switch(server_module, ctx_worker)
    result = server_module.handle_wait({"timeout_ms": 1000})
    assert result["timed_out"] is False
    assert result["event"]["kind"] == "task_assigned"
    assert result["event"]["task_id"] == r["task_id"]


def test_ack_transitions_task_status(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_boss = _register(server_module, "boss", "sid-boss", str(ws))
    _, ctx_worker = _register(server_module, "worker", "sid-worker", str(ws))

    switch(server_module, ctx_boss)
    r = server_module.handle_assign({"to": "worker", "title": "Do X"})
    task_id = r["task_id"]

    switch(server_module, ctx_worker)
    server_module.handle_ack({"task_id": task_id, "status": "in_progress"})
    server_module.handle_ack({"task_id": task_id, "status": "done", "note": "shipped"})

    tasks = server_module.handle_tasks({"assignee": "me"})
    assert tasks["tasks"][0]["status"] == "done"
    assert tasks["tasks"][0]["last_note"] == "shipped"


def test_ack_notifies_the_other_party(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_boss = _register(server_module, "boss", "sid-boss", str(ws))
    _, ctx_worker = _register(server_module, "worker", "sid-worker", str(ws))

    switch(server_module, ctx_boss)
    r = server_module.handle_assign({"to": "worker", "title": "Do X"})

    # Drain the boss's inbox first — they receive their own
    # `task_assigned_ack` mirror when the assignment lands. Loop until the
    # inbox is empty so the subsequent wait only sees the ack.
    switch(server_module, ctx_boss)
    while True:
        drained = server_module.handle_wait({"timeout_ms": 100})
        if drained.get("timed_out"):
            break

    switch(server_module, ctx_worker)
    server_module.handle_ack({"task_id": r["task_id"], "status": "done"})

    switch(server_module, ctx_boss)
    result = server_module.handle_wait({"timeout_ms": 1000})
    assert result["timed_out"] is False
    assert result["event"]["kind"] == "task_update"
    assert result["event"]["status"] == "done"


def test_ack_rejects_unauthorized_caller(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_boss = _register(server_module, "boss", "sid-boss", str(ws))
    _, ctx_worker = _register(server_module, "worker", "sid-worker", str(ws))
    _, ctx_bystander = _register(server_module, "bystander", "sid-bystander", str(ws))

    switch(server_module, ctx_boss)
    r = server_module.handle_assign({"to": "worker", "title": "Do X"})

    switch(server_module, ctx_bystander)
    with pytest.raises(ValueError, match="only the assignee or assigner"):
        server_module.handle_ack({"task_id": r["task_id"], "status": "done"})


def test_tasks_filter_by_assignee_me(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_boss = _register(server_module, "boss", "sid-boss", str(ws))
    _, ctx_a = _register(server_module, "worker-a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "worker-b", "sid-B", str(ws))

    switch(server_module, ctx_boss)
    server_module.handle_assign({"to": "worker-a", "title": "Task for A"})
    server_module.handle_assign({"to": "worker-b", "title": "Task for B"})

    switch(server_module, ctx_a)
    mine = server_module.handle_tasks({"assignee": "me"})
    assert mine["count"] == 1
    assert mine["tasks"][0]["title"] == "Task for A"


def test_assign_rejects_self_assignment(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx = _register(server_module, "solo", "sid-solo", str(ws))
    switch(server_module, ctx)
    with pytest.raises(ValueError, match="cannot assign a task to yourself"):
        server_module.handle_assign({"to": "solo", "title": "reflect"})


# --- Session lifecycle ---------------------------------------------

def test_register_with_new_alias_renames_in_place(server_module, tmp_path):
    """Re-calling xtalk_register with a different alias should rename the
    session in the current room, not silently ignore the new alias."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx = _register(server_module, "old-name", "sid-A", str(ws))
    room_id = ctx.active_room

    r = server_module.handle_register({"alias": "new-name", "workspace": str(ws)})
    assert r["alias"] == "new-name"
    room = server_module.storage.Room(room_id)
    members = room.current_members()
    aliases = {m["alias"] for m in members if m["sid"] == "sid-A"}
    assert aliases == {"new-name"}


def test_register_different_workspace_refuses(server_module, tmp_path):
    ws1 = tmp_path / "one"; ws1.mkdir()
    ws2 = tmp_path / "two"; ws2.mkdir()
    _, ctx = _register(server_module, "agent", "sid-A", str(ws1))

    with pytest.raises(ValueError, match="different workspace"):
        server_module.handle_register({"alias": "agent", "workspace": str(ws2)})


def test_stream_returns_members_and_recent_events(server_module, tmp_path):
    """xtalk_stream must give a non-blocking snapshot: current members,
    open tasks, and recent inbox events since the caller's cursor."""
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_a = _register(server_module, "alice", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "bob", "sid-B", str(ws))

    # Push a couple of events into Alice's inbox.
    switch(server_module, ctx_b)
    server_module.handle_ask({"to": "alice", "body": "one"})
    server_module.handle_ask({"to": "alice", "body": "two"})

    switch(server_module, ctx_a)
    snap = server_module.handle_stream({})
    aliases = {m["alias"] for m in snap["members"]}
    assert aliases == {"alice", "bob"}
    # Alice's inbox picked up at least the two asks (a `member_joined`
    # event lands too when Bob registers, which is fine).
    ask_events = [e for e in snap["events"] if e.get("kind") == "ask"]
    assert len(ask_events) == 2
    assert snap["next_cursor"] > 0

    # Delta call after cursor: no new events yet.
    delta = server_module.handle_stream({"cursor": snap["next_cursor"]})
    assert delta["event_count"] == 0
    assert delta["next_cursor"] == snap["next_cursor"]

    # New event → delta picks it up.
    switch(server_module, ctx_b)
    server_module.handle_ask({"to": "alice", "body": "three"})
    switch(server_module, ctx_a)
    delta2 = server_module.handle_stream({"cursor": snap["next_cursor"]})
    assert delta2["event_count"] == 1


def test_stream_includes_open_tasks(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx_boss = _register(server_module, "boss", "sid-boss", str(ws))
    _, ctx_worker = _register(server_module, "worker", "sid-worker", str(ws))

    switch(server_module, ctx_boss)
    r = server_module.handle_assign({"to": "worker", "title": "Do X"})
    snap = server_module.handle_stream({})
    ids = {t["task_id"] for t in snap["tasks"]}
    assert r["task_id"] in ids

    # Closing the task removes it from the snapshot's `tasks` list.
    switch(server_module, ctx_worker)
    server_module.handle_ack({"task_id": r["task_id"], "status": "done"})
    switch(server_module, ctx_boss)
    snap2 = server_module.handle_stream({})
    ids2 = {t["task_id"] for t in snap2["tasks"]}
    assert r["task_id"] not in ids2


def test_unregister_clears_session_file_and_ctx(server_module, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    _, ctx = _register(server_module, "a", "sid-A", str(ws))
    sess_path = server_module.storage.session_path("sid-A")
    assert sess_path.exists()

    server_module.handle_unregister({})
    assert not sess_path.exists()
    assert server_module.CTX.sid is None
