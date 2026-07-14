"""Self-hosted xtalk relay: WebSocket + SQLite TTL persistence.

Endpoints:
    GET /health   - liveness JSON
    GET /version  - relay/protocol version
    WS  /ws       - frame protocol below

Frame protocol (JSON per WebSocket text frame):

    Client → server
        {op: "join", room, verifier, sid, alias, resume_cursor?, e2ee?}
        {op: "publish", room, tid, msg: {msg_id, ts, from, from_alias, to, kind, body, in_reply_to?, meta?}}
        {op: "ack", cursor}                (advance our stored cursor for this session)
        {op: "heartbeat"}

    Server → client
        {op: "joined", cursor, ttl_seconds, e2ee, backfill: [event, ...]}
        {op: "event",  cursor, event: {msg_id, tid, room, from, kind, ts, msg}}
        {op: "pong"}
        {op: "error", code, message}

The relay never decrypts message bodies. For `e2ee` rooms, the "body" field is
whatever the client sent (typically empty; the ciphertext lives in
`msg.meta.enc`).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

RELAY_VERSION = "0.2.0"
PROTOCOL_VERSION = "xtalk-relay/1"
DEFAULT_TTL_SECONDS = 86_400
DEFAULT_PORT = 7889
CLEANUP_INTERVAL_SECONDS = 60
MAX_MESSAGE_BYTES = 32 * 1024  # relay-side cap; e2ee ciphertext + envelope
MAX_ROOMS = 1000
MAX_MEMBERS_PER_ROOM = 256


# ---------- storage ----------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    name TEXT,
    created_ts REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    invite_verifier TEXT NOT NULL,
    e2ee INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    tid TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_sid TEXT NOT NULL,
    ts TEXT NOT NULL,
    msg_json TEXT NOT NULL,
    expires_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_room_id ON events (room_id, id);
CREATE INDEX IF NOT EXISTS events_by_expiry ON events (expires_ts);
CREATE TABLE IF NOT EXISTS cursors (
    room_id TEXT NOT NULL,
    sid TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL,
    PRIMARY KEY (room_id, sid)
);
"""


class RelayStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()

    # Rooms ------------------------------------------------------------

    async def upsert_room(self, room_id: str, name: str, ttl_seconds: int, verifier: str, e2ee: bool) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO rooms (room_id, name, created_ts, ttl_seconds, invite_verifier, e2ee) "
                "VALUES (?, COALESCE((SELECT name FROM rooms WHERE room_id=?), ?), "
                "COALESCE((SELECT created_ts FROM rooms WHERE room_id=?), ?), ?, ?, ?)",
                (room_id, room_id, name, room_id, time.time(), ttl_seconds, verifier, 1 if e2ee else 0),
            )
            self._conn.commit()

    async def get_room(self, room_id: str) -> dict[str, Any] | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT room_id, name, created_ts, ttl_seconds, invite_verifier, e2ee FROM rooms WHERE room_id=?",
                (room_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "room_id": row[0],
            "name": row[1],
            "created_ts": row[2],
            "ttl_seconds": row[3],
            "invite_verifier": row[4],
            "e2ee": bool(row[5]),
        }

    async def count_rooms(self) -> int:
        async with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]

    # Events -----------------------------------------------------------

    async def append_event(self, room_id: str, ttl_seconds: int, msg: dict[str, Any]) -> int:
        expires = time.time() + ttl_seconds
        async with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (room_id, tid, msg_id, kind, from_sid, ts, msg_json, expires_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    msg.get("tid", ""),
                    msg["msg_id"],
                    msg.get("kind", ""),
                    msg.get("from", ""),
                    msg.get("ts", ""),
                    json.dumps(msg, ensure_ascii=False, separators=(",", ":")),
                    expires,
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    async def events_since(self, room_id: str, after_cursor: int, limit: int = 500) -> list[tuple[int, dict[str, Any]]]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT id, msg_json FROM events WHERE room_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (room_id, after_cursor, limit),
            ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    async def count_pending(self) -> int:
        async with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM events WHERE expires_ts > ?", (time.time(),)).fetchone()[0]

    async def purge_expired(self) -> int:
        async with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE expires_ts <= ?", (time.time(),))
            self._conn.commit()
            return cur.rowcount or 0

    # Cursors ----------------------------------------------------------

    async def get_cursor(self, room_id: str, sid: str) -> int:
        async with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM cursors WHERE room_id=? AND sid=?", (room_id, sid)
            ).fetchone()
        return int(row[0]) if row else 0

    async def set_cursor(self, room_id: str, sid: str, cursor: int) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO cursors (room_id, sid, cursor, updated_ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(room_id, sid) DO UPDATE SET cursor=excluded.cursor, updated_ts=excluded.updated_ts",
                (room_id, sid, cursor, time.time()),
            )
            self._conn.commit()


# ---------- websocket handler ----------

class _Subscription:
    __slots__ = ("ws", "room_id", "sid", "alias", "cursor")

    def __init__(self, ws: web.WebSocketResponse, room_id: str, sid: str, alias: str, cursor: int):
        self.ws = ws
        self.room_id = room_id
        self.sid = sid
        self.alias = alias
        self.cursor = cursor


class Relay:
    def __init__(self, store: RelayStore):
        self.store = store
        # room_id -> list of subscriptions
        self._subs: dict[str, list[_Subscription]] = {}
        self._subs_lock = asyncio.Lock()

    async def _broadcast(self, room_id: str, cursor: int, msg: dict[str, Any], skip_sid: str | None = None) -> None:
        async with self._subs_lock:
            targets = list(self._subs.get(room_id, []))
        payload = json.dumps({"op": "event", "cursor": cursor, "room": room_id, "event": msg}, ensure_ascii=False)
        for sub in targets:
            if skip_sid and sub.sid == skip_sid:
                continue
            with contextlib.suppress(ConnectionResetError, RuntimeError):
                await sub.ws.send_str(payload)

    async def _register(self, sub: _Subscription) -> None:
        async with self._subs_lock:
            self._subs.setdefault(sub.room_id, []).append(sub)

    async def _unregister(self, sub: _Subscription) -> None:
        async with self._subs_lock:
            bucket = self._subs.get(sub.room_id, [])
            if sub in bucket:
                bucket.remove(sub)
            if not bucket:
                self._subs.pop(sub.room_id, None)

    async def _send_err(self, ws: web.WebSocketResponse, code: str, message: str) -> None:
        with contextlib.suppress(Exception):
            await ws.send_str(json.dumps({"op": "error", "code": code, "message": message}))

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        subs: list[_Subscription] = []
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    await self._send_err(ws, "bad_frame", "invalid JSON")
                    continue
                op = frame.get("op")
                if op == "heartbeat":
                    await ws.send_str(json.dumps({"op": "pong"}))
                    continue
                if op == "join":
                    sub = await self._handle_join(ws, frame)
                    if sub:
                        subs.append(sub)
                    continue
                if op == "publish":
                    await self._handle_publish(ws, frame, subs)
                    continue
                if op == "ack":
                    await self._handle_ack(frame, subs)
                    continue
                await self._send_err(ws, "unknown_op", f"unknown op: {op}")
        finally:
            for sub in subs:
                await self._unregister(sub)
        return ws

    async def _handle_join(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> _Subscription | None:
        room_id = str(frame.get("room", ""))
        verifier = str(frame.get("verifier", ""))
        sid = str(frame.get("sid", ""))
        alias = str(frame.get("alias", ""))
        if not room_id or not verifier or not sid:
            await self._send_err(ws, "bad_join", "room, verifier, sid required")
            return None
        room = await self.store.get_room(room_id)
        if not room:
            await self._send_err(ws, "unknown_room", "no such room")
            return None
        if room["invite_verifier"] != verifier:
            await self._send_err(ws, "bad_verifier", "invite verifier mismatch")
            return None
        resume = int(frame.get("resume_cursor") or await self.store.get_cursor(room_id, sid))
        history = await self.store.events_since(room_id, resume, limit=500)
        cursor = history[-1][0] if history else resume
        backfill = [{"cursor": cid, "event": ev} for cid, ev in history]
        await ws.send_str(json.dumps({
            "op": "joined",
            "room": room_id,
            "cursor": cursor,
            "ttl_seconds": room["ttl_seconds"],
            "e2ee": room["e2ee"],
            "backfill": backfill,
        }, ensure_ascii=False))
        sub = _Subscription(ws, room_id, sid, alias, cursor)
        await self._register(sub)
        return sub

    async def _handle_publish(self, ws: web.WebSocketResponse, frame: dict[str, Any], subs: list[_Subscription]) -> None:
        room_id = str(frame.get("room", ""))
        sub = next((s for s in subs if s.room_id == room_id), None)
        if not sub:
            await self._send_err(ws, "not_joined", "join before publishing")
            return
        room = await self.store.get_room(room_id)
        if not room:
            await self._send_err(ws, "unknown_room", "no such room")
            return
        msg = frame.get("msg")
        if not isinstance(msg, dict) or "msg_id" not in msg:
            await self._send_err(ws, "bad_msg", "msg must be dict with msg_id")
            return
        tid = str(frame.get("tid", ""))
        msg = {**msg, "tid": tid, "room": room_id}
        cursor = await self.store.append_event(room_id, room["ttl_seconds"], msg)
        await self.store.set_cursor(room_id, sub.sid, cursor)
        await self._broadcast(room_id, cursor, msg, skip_sid=sub.sid)
        await ws.send_str(json.dumps({"op": "ack", "cursor": cursor, "msg_id": msg["msg_id"]}))

    async def _handle_ack(self, frame: dict[str, Any], subs: list[_Subscription]) -> None:
        cursor = int(frame.get("cursor", 0))
        room_id = str(frame.get("room", ""))
        sub = next((s for s in subs if s.room_id == room_id), None)
        if sub and cursor > sub.cursor:
            sub.cursor = cursor
            await self.store.set_cursor(sub.room_id, sub.sid, cursor)


# Try defining AppKey if available, else fall back to str:
try:
    RELAY_KEY = web.AppKey("relay", Relay)
    STORE_KEY = web.AppKey("store", RelayStore)
    CLEANUP_KEY = web.AppKey("cleanup_task", asyncio.Task)
except AttributeError:
    RELAY_KEY = "relay"  # type: ignore[assignment]
    STORE_KEY = "store"  # type: ignore[assignment]
    CLEANUP_KEY = "cleanup_task"  # type: ignore[assignment]


# ---------- HTTP endpoints ----------

async def _health(request: web.Request) -> web.Response:
    relay: Relay = request.app[RELAY_KEY]
    return web.json_response({
        "ok": True,
        "version": RELAY_VERSION,
        "protocol": PROTOCOL_VERSION,
        "rooms": await relay.store.count_rooms(),
        "events_pending": await relay.store.count_pending(),
    })


async def _version(request: web.Request) -> web.Response:
    return web.json_response({"relay": RELAY_VERSION, "protocol": PROTOCOL_VERSION})


async def _register_room(request: web.Request) -> web.Response:
    """POST /rooms — bootstrap a room on the relay before invites go out.

    Body: {room_id, name?, invite_verifier, ttl_seconds?, e2ee?}
    """
    relay: Relay = request.app[RELAY_KEY]
    if await relay.store.count_rooms() >= MAX_ROOMS:
        return web.json_response({"ok": False, "error": "room_limit_reached"}, status=429)
    body = await request.json()
    room_id = str(body["room_id"])
    await relay.store.upsert_room(
        room_id,
        str(body.get("name", room_id)),
        int(body.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
        str(body["invite_verifier"]),
        bool(body.get("e2ee", False)),
    )
    return web.json_response({"ok": True, "room": room_id})


# ---------- app + cleanup task ----------

async def _cleanup_loop(store: RelayStore, interval: float) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            purged = await store.purge_expired()
            if purged:
                # tiny stderr log; relay is expected to run behind supervisor
                print(f"[xtalk-relay] purged {purged} expired events", flush=True)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # pragma: no cover
            print(f"[xtalk-relay] cleanup error: {exc}", flush=True)


def build_app(db_path: Path, cleanup_interval: float = CLEANUP_INTERVAL_SECONDS) -> web.Application:
    store = RelayStore(db_path)
    relay = Relay(store)
    app = web.Application()
    app[RELAY_KEY] = relay
    app[STORE_KEY] = store
    app.add_routes([
        web.get("/health", _health),
        web.get("/version", _version),
        web.post("/rooms", _register_room),
        web.get("/ws", relay.ws_handler),
    ])

    async def _on_startup(_app: web.Application) -> None:
        _app[CLEANUP_KEY] = asyncio.create_task(_cleanup_loop(store, cleanup_interval))

    async def _on_cleanup(_app: web.Application) -> None:
        task = _app.get(CLEANUP_KEY)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await store.close()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def run(db_path: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    web.run_app(build_app(db_path), host=host, port=port, print=None)
