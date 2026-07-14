"""Daemon subscriptions file lifecycle + inbox delivery when the relay pushes an event."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path

import aiohttp
import pytest


@pytest.mark.asyncio
async def test_daemon_bridges_relay_event_into_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XTALK_HOME", str(tmp_path))
    from xtalk import storage, daemon
    importlib.reload(storage)
    importlib.reload(daemon)

    # Bring up relay
    from xtalk import relay as relay_mod
    importlib.reload(relay_mod)
    app = relay_mod.build_app(tmp_path / "relay.db", cleanup_interval=3600)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    try:
        # Register a room + a subscription file for our fake session
        async with aiohttp.ClientSession() as sess:
            await sess.post(f"http://127.0.0.1:{port}/rooms", json={
                "room_id": "room-daemon",
                "name": "d",
                "invite_verifier": "v-d",
                "ttl_seconds": 3600,
                "e2ee": False,
            })

        sub = daemon.Subscription(
            relay_url=f"ws://127.0.0.1:{port}/ws",
            room_id="room-daemon",
            sid="sid-listener",
            alias="listener",
            verifier="v-d",
            resume_cursor=0,
        )
        daemon.add_subscription(sub)
        assert len(daemon.load_subscriptions()) == 1

        supervisor = daemon.DaemonSupervisor(poll_interval=0.2)
        supervisor_task = asyncio.create_task(supervisor.run())

        # Publish an event via a separate WS as an emulated peer
        await asyncio.sleep(0.5)  # let supervisor connect
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
                await ws.send_json({"op": "join", "room": "room-daemon", "verifier": "v-d", "sid": "sid-peer", "alias": "peer"})
                json.loads((await ws.receive()).data)
                await ws.send_json({
                    "op": "publish",
                    "room": "room-daemon",
                    "tid": "tid-1",
                    "msg": {"msg_id": "msg-1", "from": "sid-peer", "kind": "ask", "ts": "t", "body": "hi"},
                })
                json.loads((await ws.receive()).data)  # ack

        # Wait for daemon to deliver into inbox
        inbox = tmp_path / "rooms" / "room-daemon" / "inbox" / "sid-listener.jsonl"
        for _ in range(30):
            if inbox.exists() and storage.concurrent_read_text(inbox).strip():
                break
            await asyncio.sleep(0.1)
        assert inbox.exists(), "daemon did not create inbox file"
        line = json.loads(storage.concurrent_read_text(inbox).splitlines()[0])
        assert line["msg_id"] == "msg-1"
        assert line["room"] == "room-daemon"

        supervisor.stop()
        await asyncio.wait_for(supervisor_task, timeout=5)
    finally:
        await runner.cleanup()


def test_subscription_file_add_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XTALK_HOME", str(tmp_path))
    from xtalk import storage, daemon
    importlib.reload(storage)
    importlib.reload(daemon)

    sub = daemon.Subscription("ws://x", "room-1", "sid-a", "a", "v", 0)
    daemon.add_subscription(sub)
    assert len(daemon.load_subscriptions()) == 1
    daemon.remove_subscription("ws://x", "room-1", "sid-a")
    assert daemon.load_subscriptions() == []


def test_daemon_control_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XTALK_HOME", str(tmp_path))
    from xtalk import storage, daemon, server
    importlib.reload(storage)
    importlib.reload(daemon)
    importlib.reload(server)

    server.CTX = server.SessionCtx()
    server.CTX.sid = "sid-test"
    server.CTX.active_room = "room-test"
    server.CTX.memberships["room-test"] = "test-alias"

    room = storage.Room("room-test")
    room.ensure("", name="test-room", invite_verifier="v-test")

    res = server.handle_daemon_control({
        "action": "subscribe",
        "room": "room-test",
        "relay_url": "ws://dummy:7889/ws"
    })
    assert res["subscribed"] is True
    assert "daemon_id" in res
    assert res["daemon_id"].startswith("did-")

    status_res = server.handle_daemon_control({"action": "status"})
    assert status_res["subscriptions"] == 1

    unsub_res = server.handle_daemon_control({
        "action": "unsubscribe",
        "room": "room-test"
    })
    assert unsub_res["unsubscribed"] is True

    status_res2 = server.handle_daemon_control({"action": "status"})
    assert status_res2["subscriptions"] == 0
