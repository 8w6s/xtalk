"""xtalk daemon: bridges remote relay events into local inbox files.

A single daemon per user watches its subscriptions file
(`$XTALK_HOME/daemon/subscriptions.json`) and maintains one WebSocket per
distinct relay URL. Every relay event it receives is appended to the target
session's inbox JSONL under `$XTALK_HOME/rooms/<room_id>/inbox/<sid>.jsonl`,
which the local xtalk_wait/monitor pipeline already knows how to consume.

Reconnects use exponential backoff (up to 60s) with cursor resume so the same
event is never delivered twice to a given session.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from . import storage
from .storage import Room, XTALK_ROOT, atomic_append, atomic_json


BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
STATE_FILE = "daemon.state.json"
SUBSCRIPTIONS_FILE = "subscriptions.json"
PID_FILE = "daemon.pid"


def _daemon_dir() -> Path:
    d = XTALK_ROOT / "daemon"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _subscriptions_path() -> Path:
    return _daemon_dir() / SUBSCRIPTIONS_FILE


def _state_path() -> Path:
    return _daemon_dir() / STATE_FILE


def _pid_path() -> Path:
    return _daemon_dir() / PID_FILE


@dataclass
class Subscription:
    """One (relay, room, session) triple the daemon must service."""
    relay_url: str
    room_id: str
    sid: str
    alias: str
    verifier: str
    resume_cursor: int = 0
    daemon_id: str = ""

    def __post_init__(self):
        if not self.daemon_id:
            import uuid
            self.daemon_id = "did-" + uuid.uuid4().hex[:16]


@dataclass
class RelayLink:
    relay_url: str
    subs: list[Subscription] = field(default_factory=list)


def load_subscriptions() -> list[Subscription]:
    path = _subscriptions_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    result: list[Subscription] = []
    for entry in raw:
        try:
            result.append(Subscription(
                relay_url=entry["relay_url"],
                room_id=entry["room_id"],
                sid=entry["sid"],
                alias=entry.get("alias", ""),
                verifier=entry["verifier"],
                resume_cursor=int(entry.get("resume_cursor", 0)),
                daemon_id=entry.get("daemon_id", ""),
            ))
        except (KeyError, TypeError):
            continue
    return result


def add_subscription(sub: Subscription) -> None:
    path = _subscriptions_path()
    existing = load_subscriptions()
    for i, s in enumerate(existing):
        if s.relay_url == sub.relay_url and s.room_id == sub.room_id and s.sid == sub.sid:
            existing[i] = sub
            break
    else:
        existing.append(sub)
    atomic_json(path, [_sub_to_dict(s) for s in existing])  # type: ignore[arg-type]


def remove_subscription(relay_url: str, room_id: str, sid: str) -> None:
    path = _subscriptions_path()
    existing = load_subscriptions()
    filtered = [s for s in existing if not (s.relay_url == relay_url and s.room_id == room_id and s.sid == sid)]
    atomic_json(path, [_sub_to_dict(s) for s in filtered])  # type: ignore[arg-type]


def _sub_to_dict(sub: Subscription) -> dict[str, Any]:
    return {
        "relay_url": sub.relay_url,
        "room_id": sub.room_id,
        "sid": sub.sid,
        "alias": sub.alias,
        "verifier": sub.verifier,
        "resume_cursor": sub.resume_cursor,
        "daemon_id": sub.daemon_id,
    }


def _save_cursor(sub: Subscription) -> None:
    """Persist the daemon's advance cursor so restarts resume without duplicates."""
    existing = load_subscriptions()
    for s in existing:
        if s.relay_url == sub.relay_url and s.room_id == sub.room_id and s.sid == sub.sid:
            s.resume_cursor = sub.resume_cursor
            break
    atomic_json(_subscriptions_path(), [_sub_to_dict(s) for s in existing])  # type: ignore[arg-type]


def _deliver_to_inbox(room_id: str, sid: str, event: dict[str, Any]) -> None:
    """Write an event line into the recipient's local inbox JSONL."""
    room = Room(room_id)
    inbox = room.inbox_path(sid)
    entry = {
        "msg_id": event.get("msg_id", ""),
        "tid": event.get("tid", ""),
        "room": room_id,
        "from": event.get("from", ""),
        "kind": event.get("kind", ""),
        "ts": event.get("ts", ""),
    }
    atomic_append(inbox, json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


class DaemonSupervisor:
    """Owns one asyncio task per (relay_url, room_id, sid) subscription.

    On subscriptions file change (checked every few seconds) it adds/removes
    tasks without disturbing existing connections.
    """

    def __init__(self, poll_interval: float = 5.0):
        self.poll_interval = poll_interval
        self._tasks: dict[tuple[str, str, str], asyncio.Task] = {}
        self._stopped = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None

    async def run(self) -> None:
        _pid_path().write_text(str(__import__("os").getpid()))
        self._session = aiohttp.ClientSession()
        try:
            while not self._stopped.is_set():
                await self._reconcile()
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in list(self._tasks.values()):
                task.cancel()
            for task in list(self._tasks.values()):
                with contextlib.suppress(BaseException):
                    await task
            if self._session:
                await self._session.close()
            with contextlib.suppress(FileNotFoundError):
                _pid_path().unlink()

    def stop(self) -> None:
        self._stopped.set()

    async def _reconcile(self) -> None:
        subs = await asyncio.to_thread(load_subscriptions)
        current = {(s.relay_url, s.room_id, s.sid): s for s in subs}
        # Remove tasks that no longer have a subscription
        for key in list(self._tasks):
            if key not in current:
                self._tasks[key].cancel()
                self._tasks.pop(key)
        # Start tasks for new subscriptions
        for key, sub in current.items():
            if key not in self._tasks or self._tasks[key].done():
                self._tasks[key] = asyncio.create_task(self._service(sub))

    async def _service(self, sub: Subscription) -> None:
        assert self._session is not None
        backoff = BACKOFF_START
        while not self._stopped.is_set():
            try:
                async with self._session.ws_connect(sub.relay_url) as ws:
                    await ws.send_json({
                        "op": "join",
                        "room": sub.room_id,
                        "verifier": sub.verifier,
                        "sid": sub.sid,
                        "alias": sub.alias,
                        "resume_cursor": sub.resume_cursor,
                    })
                    backoff = BACKOFF_START
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            frame = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        op = frame.get("op")
                        if op == "joined":
                            for entry in frame.get("backfill", []):
                                await self._on_event(sub, entry.get("cursor", 0), entry.get("event", {}))
                        elif op == "event":
                            await self._on_event(sub, int(frame.get("cursor", 0)), frame.get("event", {}))
                            await ws.send_json({"op": "ack", "cursor": frame.get("cursor", 0), "room": sub.room_id})
                        elif op == "error":
                            # non-recoverable errors (bad verifier, unknown room) → drop subscription
                            code = frame.get("code", "")
                            if code in {"bad_verifier", "unknown_room"}:
                                await asyncio.to_thread(remove_subscription, sub.relay_url, sub.room_id, sub.sid)
                                return
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _on_event(self, sub: Subscription, cursor: int, event: dict[str, Any]) -> None:
        if not event:
            return
        await asyncio.to_thread(_deliver_to_inbox, sub.room_id, sub.sid, event)
        if cursor > sub.resume_cursor:
            sub.resume_cursor = cursor
            await asyncio.to_thread(_save_cursor, sub)


def is_running() -> tuple[bool, int | None]:
    path = _pid_path()
    if not path.exists():
        return False, None
    try:
        pid = int(path.read_text())
    except (ValueError, FileNotFoundError):
        return False, None
    try:
        __import__("os").kill(pid, 0)
        return True, pid
    except OSError:
        return False, pid


def start() -> None:
    running, pid = is_running()
    if running:
        print(f"daemon already running (pid={pid})")
        return
    supervisor = DaemonSupervisor()

    def _stop(*_: Any) -> None:
        supervisor.stop()

    loop = asyncio.new_event_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(s, _stop)
    try:
        loop.run_until_complete(supervisor.run())
    finally:
        loop.close()


def stop() -> bool:
    import os
    running, pid = is_running()
    if not running or pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


def status() -> dict[str, Any]:
    running, pid = is_running()
    subs = load_subscriptions()
    return {
        "running": running,
        "pid": pid,
        "subscriptions": len(subs),
        "state_file": str(_subscriptions_path()),
        "root": str(storage.XTALK_ROOT),
    }
