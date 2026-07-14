"""Relay: publish/subscribe with cursor resume, TTL cleanup, health."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiohttp
import pytest

from xtalk.relay import build_app


@pytest.mark.asyncio
async def test_health_and_room_bootstrap(tmp_path: Path):
    app = build_app(tmp_path / "relay.db", cleanup_interval=3600)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base = f"http://127.0.0.1:{port}"

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{base}/health") as r:
                data = await r.json()
                assert data["ok"] is True
                assert data["rooms"] == 0

            async with sess.post(f"{base}/rooms", json={
                "room_id": "room-abc",
                "name": "test",
                "invite_verifier": "verifier-1",
                "ttl_seconds": 3600,
                "e2ee": False,
            }) as r:
                assert r.status == 200
                out = await r.json()
                assert out["ok"] is True

            async with sess.get(f"{base}/health") as r:
                data = await r.json()
                assert data["rooms"] == 1
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_publish_and_subscribe(tmp_path: Path):
    app = build_app(tmp_path / "relay.db", cleanup_interval=3600)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    try:
        async with aiohttp.ClientSession() as sess:
            await sess.post(f"http://127.0.0.1:{port}/rooms", json={
                "room_id": "room-x",
                "name": "chat",
                "invite_verifier": "v-x",
                "ttl_seconds": 3600,
                "e2ee": False,
            })

            async with sess.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws_a, \
                       sess.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws_b:
                await ws_a.send_json({"op": "join", "room": "room-x", "verifier": "v-x", "sid": "sid-A", "alias": "a"})
                await ws_b.send_json({"op": "join", "room": "room-x", "verifier": "v-x", "sid": "sid-B", "alias": "b"})

                joined_a = json.loads((await ws_a.receive()).data)
                joined_b = json.loads((await ws_b.receive()).data)
                assert joined_a["op"] == "joined"
                assert joined_b["op"] == "joined"

                await ws_a.send_json({
                    "op": "publish",
                    "room": "room-x",
                    "tid": "tid-01",
                    "msg": {"msg_id": "msg-1", "from": "sid-A", "kind": "ask", "ts": "2026-07-15T00:00:00Z", "body": "hi"},
                })

                # B should receive the event; A gets an ack
                first = json.loads((await ws_b.receive()).data)
                assert first["op"] == "event"
                assert first["event"]["msg_id"] == "msg-1"

                ack = json.loads((await ws_a.receive()).data)
                assert ack["op"] == "ack"
                assert ack["cursor"] > 0
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_cursor_resume_backfill(tmp_path: Path):
    app = build_app(tmp_path / "relay.db", cleanup_interval=3600)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    try:
        async with aiohttp.ClientSession() as sess:
            await sess.post(f"http://127.0.0.1:{port}/rooms", json={
                "room_id": "room-r",
                "name": "resume",
                "invite_verifier": "v-r",
                "ttl_seconds": 3600,
                "e2ee": False,
            })

            # Publish two events with A while B is offline
            async with sess.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws_a:
                await ws_a.send_json({"op": "join", "room": "room-r", "verifier": "v-r", "sid": "sid-A", "alias": "a"})
                json.loads((await ws_a.receive()).data)
                for i in (1, 2):
                    await ws_a.send_json({
                        "op": "publish",
                        "room": "room-r",
                        "tid": "tid-1",
                        "msg": {"msg_id": f"msg-{i}", "from": "sid-A", "kind": "ask", "ts": "t", "body": ""},
                    })
                    # drain ack
                    json.loads((await ws_a.receive()).data)

            # B connects from cursor 0, should backfill both events
            async with sess.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws_b:
                await ws_b.send_json({"op": "join", "room": "room-r", "verifier": "v-r", "sid": "sid-B", "alias": "b", "resume_cursor": 0})
                joined = json.loads((await ws_b.receive()).data)
                assert joined["op"] == "joined"
                assert len(joined["backfill"]) == 2
                assert [b["event"]["msg_id"] for b in joined["backfill"]] == ["msg-1", "msg-2"]
    finally:
        await runner.cleanup()
