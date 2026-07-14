"""xtalk_wait long-poll semantics + xtalk_status output."""
from __future__ import annotations

import threading
import time

import pytest

from tests.conftest import new_ctx, switch


def _register(server, alias, sid, cwd):
    switch(server, new_ctx(server))
    server.storage.session_id = lambda: sid  # type: ignore[assignment]
    return server.handle_register({"alias": alias, "workspace": cwd, "capabilities": ["long_poll"]}), server.CTX


def test_wait_returns_when_event_arrives(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "coder", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "reviewer", "sid-B", str(ws))

    # A → B in background thread, B waits
    def _producer():
        time.sleep(0.05)
        switch(server_module, ctx_a)
        server_module.handle_ask({"to": "reviewer", "body": "hi"})

    t = threading.Thread(target=_producer)
    t.start()

    switch(server_module, ctx_b)
    result = server_module.handle_wait({"timeout_ms": 3000, "kinds": ["ask"]})
    t.join()

    assert result["timed_out"] is False
    assert result["event"]["from"] == "sid-A"
    assert result["event"]["kind"] == "ask"


def test_wait_times_out_cleanly(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "coder", "sid-A", str(ws))
    switch(server_module, ctx_a)
    r = server_module.handle_wait({"timeout_ms": 100, "kinds": ["ask"]})
    assert r["timed_out"] is True


def test_wait_filters_by_thread_and_reply(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "a", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "b", "sid-B", str(ws))

    switch(server_module, ctx_a)
    ask = server_module.handle_ask({"to": "b", "body": "q1"})

    # noise: another ask in a different thread should NOT wake a filtered wait
    server_module.handle_ask({"to": "b", "body": "q2"})

    def _reply_after_delay():
        time.sleep(0.1)
        switch(server_module, ctx_b)
        server_module.handle_reply({"thread": ask["thread_id"], "body": "a1", "in_reply_to": ask["msg_id"]})

    t = threading.Thread(target=_reply_after_delay)
    t.start()

    switch(server_module, ctx_a)
    r = server_module.handle_wait({
        "thread": ask["thread_id"],
        "in_reply_to": ask["msg_id"],
        "kinds": ["reply"],
        "timeout_ms": 3000,
    })
    t.join()

    assert r["timed_out"] is False
    assert r["event"]["tid"] == ask["thread_id"]


def test_status_reports_membership(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "coder", "sid-A", str(ws))
    switch(server_module, ctx_a)
    created = server_module.handle_room_create({"name": "extra"})
    st = server_module.handle_status({})
    assert st["registered"] is True
    assert st["rooms"] == 2
    assert st["active_room"] == created["room"]
    assert st["recommended_resume_strategy"] == "long_poll"


def test_background_process_uses_daemon_without_monitor(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx = _register(server_module, "coder", "sid-A", str(ws))
    ctx.capabilities.add("background_process")
    switch(server_module, ctx)
    assert server_module.handle_status({})["recommended_resume_strategy"] == "daemon"

    ctx.capabilities.add("monitor")
    assert server_module.handle_status({})["recommended_resume_strategy"] == "monitor"


def test_new_tools_thread_list_and_broadcast(server_module, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    _, ctx_a = _register(server_module, "coder", "sid-A", str(ws))
    _, ctx_b = _register(server_module, "reviewer", "sid-B", str(ws))

    # Test Broadcast
    switch(server_module, ctx_a)
    broadcast_res = server_module.handle_broadcast({"body": "Important Announcement"})
    assert broadcast_res["to"] == ["*"]
    assert broadcast_res["presence"] == "idle"

    # Test Thread List
    switch(server_module, ctx_b)
    list_res = server_module.handle_thread_list({})
    assert len(list_res["threads"]) == 1
    assert list_res["threads"][0]["thread_id"] == broadcast_res["thread_id"]
    assert list_res["threads"][0]["closed"] is False
    assert list_res["threads"][0]["last_message"]["body"] == "Important Announcement"
