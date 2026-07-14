"""xtalk MCP server: portable rooms and cross-agent messaging."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Callable

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import crypto, storage, daemon
from .storage import DEFAULT_READ_COUNT, MAX_READ_COUNT, Message, Room, ensure_project_binding, new_msg_id, new_room_id, new_thread_id, now_iso, project_manifest_path, resolve_alias, save_session, workspace_hash


class SessionCtx:
    def __init__(self) -> None:
        self.sid: str | None = None
        self.client = "other"
        self.capabilities: set[str] = set()
        self.memberships: dict[str, str] = {}
        self.active_room: str | None = None
        self.cursors: dict[str, int] = {}
        # room_id -> encryption key (only in memory; never persisted to disk)
        self.room_keys: dict[str, bytes] = {}

    @property
    def alias(self) -> str | None:  # v0.1 compatibility
        return self.memberships.get(self.active_room or "")

    @alias.setter
    def alias(self, value: str | None) -> None:
        if self.active_room and value:
            self.memberships[self.active_room] = value

    @property
    def workspace_hash(self) -> str | None:
        return self.active_room

    @workspace_hash.setter
    def workspace_hash(self, value: str | None) -> None:
        self.active_room = value

    @property
    def workspace_path(self) -> str | None:
        return Room(self.active_room).metadata().get("workspace_path") if self.active_room else None

    @workspace_path.setter
    def workspace_path(self, value: str | None) -> None:
        pass

    def require_registered(self) -> None:
        if not self.sid:
            raise RuntimeError("session not registered — call xtalk_register first")

    def room(self, room: str | None = None) -> Room:
        self.require_registered()
        rid = room or self.active_room
        if not rid or rid not in self.memberships:
            raise ValueError("not a member of the requested room")
        return Room(rid)

    def persist(self) -> None:
        if self.sid:
            save_session(self.sid, {"sid": self.sid, "client": self.client, "capabilities": sorted(self.capabilities), "memberships": self.memberships, "active_room": self.active_room, "cursors": self.cursors})


CTX = SessionCtx()

DEADLOCK_GRACE_SECONDS = 60.0
HEARTBEAT_INTERVAL_SECONDS = 20.0
_WATCHDOG_TIMERS: dict[str, threading.Timer] = {}
_WATCHDOG_LOCK = threading.Lock()
_HEARTBEAT_TIMERS: dict[tuple[str, str], threading.Timer] = {}
_HEARTBEAT_LOCK = threading.Lock()


def _heartbeat_tick(room_id: str, sid: str, alias: str, client: str, pid: int) -> None:
    key = (room_id, sid)
    try:
        Room(room_id).append_member_event({
            "event": "heartbeat", "sid": sid, "alias": alias,
            "epoch": time.time(), "ts": now_iso(), "client": client, "pid": pid,
        })
    except Exception:
        pass
    with _HEARTBEAT_LOCK:
        if key not in _HEARTBEAT_TIMERS:
            return
        timer = threading.Timer(
            HEARTBEAT_INTERVAL_SECONDS, _heartbeat_tick,
            args=(room_id, sid, alias, client, pid),
        )
        timer.daemon = True
        _HEARTBEAT_TIMERS[key] = timer
        timer.start()


def _arm_heartbeat(room_id: str, sid: str, alias: str) -> None:
    key = (room_id, sid)
    with _HEARTBEAT_LOCK:
        previous = _HEARTBEAT_TIMERS.pop(key, None)
        if previous:
            previous.cancel()
        timer = threading.Timer(
            HEARTBEAT_INTERVAL_SECONDS, _heartbeat_tick,
            args=(room_id, sid, alias, CTX.client, os.getppid()),
        )
        timer.daemon = True
        _HEARTBEAT_TIMERS[key] = timer
        timer.start()


def _cancel_heartbeat(room_id: str, sid: str) -> None:
    with _HEARTBEAT_LOCK:
        timer = _HEARTBEAT_TIMERS.pop((room_id, sid), None)
        if timer:
            timer.cancel()


def _cancel_all_heartbeats() -> None:
    with _HEARTBEAT_LOCK:
        timers = list(_HEARTBEAT_TIMERS.values())
        _HEARTBEAT_TIMERS.clear()
    for timer in timers:
        timer.cancel()


def _watchdog_check(room_id: str) -> None:
    """Called on a background thread ~grace seconds after ask/listen.

    Reads the members file, checks for mutual-wait deadlock, and emits a
    `deadlock_hint` into the shared system thread + each waiter's inbox so
    Monitor and xtalk_wait can exit. Idempotent-safe via the tail check in
    `Room.emit_deadlock_hint`.
    """
    try:
        room = Room(room_id)
        waiters = room.check_deadlock()
        if waiters:
            room.emit_deadlock_hint(waiters)
    except Exception:
        # Watchdog is best-effort; never crash the MCP process.
        pass
    finally:
        with _WATCHDOG_LOCK:
            _WATCHDOG_TIMERS.pop(room_id, None)


def _arm_watchdog(room_id: str) -> None:
    """Ensure exactly one watchdog timer per room is armed."""
    with _WATCHDOG_LOCK:
        existing = _WATCHDOG_TIMERS.get(room_id)
        if existing and existing.is_alive():
            return
        timer = threading.Timer(DEADLOCK_GRACE_SECONDS + 5, _watchdog_check, args=(room_id,))
        timer.daemon = True
        timer.start()
        _WATCHDOG_TIMERS[room_id] = timer


def _tool(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> Tool:
    return Tool(name=name, description=description, inputSchema={"type": "object", "properties": properties or {}, **({"required": required} if required else {})})


ROOM = {"room": {"type": "string", "description": "Room id; defaults to active room"}}
TOOLS = [
    _tool("xtalk_register", "Register this agent and join its workspace room.", {"alias": {"type": "string"}, "workspace": {"type": "string"}, "client": {"type": "string"}, "capabilities": {"type": "array", "items": {"type": "string"}}, "workspace_room": {"type": "boolean"}}, ["alias"]),
    _tool("xtalk_discover", "Discover members in a room or workspace.", {"workspace": {"type": "string"}, **ROOM}),
    _tool("xtalk_listen", "Return a platform-appropriate inbox monitor command.", ROOM),
    _tool("xtalk_ask", "Ask a room member and return a wait condition.", {"to": {"type": "string"}, "body": {"type": "string"}, "thread": {"type": "string"}, **ROOM}, ["to", "body"]),
    _tool("xtalk_read", "Read the last messages in a thread.", {"thread": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": MAX_READ_COUNT}, **ROOM}, ["thread"]),
    _tool("xtalk_reply", "Reply to an ask in a thread.", {"thread": {"type": "string"}, "body": {"type": "string"}, "in_reply_to": {"type": "string"}, **ROOM}, ["thread", "body"]),
    _tool("xtalk_close", "Close a thread and choose its reporting agent.", {"thread": {"type": "string"}, "summary": {"type": "string"}, "report_to": {"type": "string"}, **ROOM}, ["thread", "summary", "report_to"]),
    _tool("xtalk_leave", "Leave the active or selected room.", ROOM),
    _tool("xtalk_room_create", "Create and join a room; returns an invite URI.", {"name": {"type": "string"}, "alias": {"type": "string"}, "visibility": {"type": "string", "enum": ["private", "local"]}, "transport": {"type": "string", "enum": ["local", "relay"]}, "e2ee": {"type": "boolean"}, "ttl_seconds": {"type": "integer"}}, ["name"]),
    _tool("xtalk_room_join", "Join a room using an invite URI.", {"invite": {"type": "string"}, "alias": {"type": "string"}}, ["invite", "alias"]),
    _tool("xtalk_room_list", "List rooms joined by this session."),
    _tool("xtalk_room_use", "Set the active room without leaving other rooms.", {"room": {"type": "string"}}, ["room"]),
    _tool("xtalk_room_leave", "Leave one room.", ROOM, ["room"]),
    _tool("xtalk_wait", "Wait for a matching message; portable fallback when no Monitor exists.", {"room": {"type": "string"}, "thread": {"type": "string"}, "in_reply_to": {"type": "string"}, "kinds": {"type": "array", "items": {"type": "string"}}, "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 1800000}}),
    _tool("xtalk_status", "Return session, room, transport and resume-strategy status."),
    _tool(
        "xtalk_presence",
        "Set your presence mode manually. Use `listening` before entering Monitor listen loops, `waiting_reply` before entering wait-for-reply loops, `idle` when neither. The deadlock detector uses this signal — mis-declaring it will break auto-recovery.",
        {"mode": {"type": "string", "enum": ["idle", "listening", "waiting_reply"]}, "target_msg_id": {"type": "string"}, "room": {"type": "string"}},
        ["mode"],
    ),
    _tool("xtalk_thread_list", "List all open/closed threads in the current or specified room.", ROOM),
    _tool("xtalk_broadcast", "Broadcast an informational message to all room members without entering a waiting state.", {"body": {"type": "string"}, **ROOM}, ["body"]),
    _tool("xtalk_daemon_control", "Manage the background daemon process and its room subscriptions.", {"action": {"type": "string", "enum": ["start", "stop", "status", "subscribe", "unsubscribe"]}, "room": {"type": "string"}, "relay_url": {"type": "string"}}, ["action"]),
]


def _strategy() -> str:
    if "monitor" in CTX.capabilities:
        return "monitor"
    if "background_process" in CTX.capabilities:
        return "daemon"
    return "long_poll"


def _join(room: Room, alias: str) -> None:
    alias = alias.strip()
    if not alias or len(alias) > 64:
        raise ValueError("alias must be 1..64 characters")
    existing = room.current_members()
    if any(m.get("alias") == alias and m.get("sid") != CTX.sid for m in existing):
        raise ValueError(f"alias already in use in room: {alias}")
    room.append_member_event({"event": "join", "sid": CTX.sid, "alias": alias, "epoch": time.time(), "ts": now_iso(), "client": CTX.client, "pid": os.getppid()})
    CTX.memberships[room.id] = alias
    CTX.active_room = room.id
    CTX.persist()
    _arm_heartbeat(room.id, CTX.sid or "", alias)
    room.notify_membership("member_joined", CTX.sid or "", alias, [m["sid"] for m in existing])


def handle_register(args: dict[str, Any]) -> dict[str, Any]:
    global CTX
    if CTX.sid:
        return {"sid": CTX.sid, "alias": CTX.alias, "room_id": CTX.active_room, "other_members": [m for m in CTX.room().current_members() if m["sid"] != CTX.sid], "recommended_resume_strategy": _strategy()}
    CTX.sid = storage.session_id()
    CTX.client = args.get("client", "other")
    CTX.capabilities = set(args.get("capabilities", []))
    workspace = str(storage.workspace_root(args.get("workspace") or os.getcwd()))
    binding = ensure_project_binding(workspace)
    rid = binding["default_room"]
    room = Room(rid)
    room_restored = (room.root / "meta.json").exists()
    room.ensure(workspace, name=Path(workspace).name, transport="local", workspace_room=True, project_id=binding["project_id"], persistent=True)
    if room_restored:
        room.update_metadata(workspace_path=workspace, project_id=binding["project_id"], persistent=True)
    _join(room, args["alias"])
    others = [m for m in room.current_members() if m["sid"] != CTX.sid]
    return {"sid": CTX.sid, "alias": CTX.alias, "room_id": rid, "workspace": workspace, "project_id": binding["project_id"], "project_manifest": str(project_manifest_path(workspace)), "room_restored": room_restored, "other_members": others, "recommended_resume_strategy": _strategy(), "hint": f"Registered as '{CTX.alias}' in persistent project room '{rid}'."}


def handle_discover(args: dict[str, Any]) -> dict[str, Any]:
    workspace = args.get("workspace") or os.getcwd()
    binding = ensure_project_binding(workspace) if not args.get("room") and not CTX.active_room else None
    rid = args.get("room") or (binding["default_room"] if binding else CTX.active_room)
    room = Room(rid)
    exists = (room.root / "meta.json").exists()
    return {"room_id": rid, "workspace": room.metadata().get("workspace_path"), "exists": exists, "members": room.current_members() if exists else [], "open_threads": len(list(room.threads_dir.glob("*.jsonl"))) if room.threads_dir.exists() else 0}


def handle_listen(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    inbox = room.inbox_path(CTX.sid or "")
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.touch(exist_ok=True)
    room.append_presence(CTX.sid or "", CTX.memberships[room.id], "listening")
    if os.name == "nt":
        command = f'powershell -NoProfile -Command "Get-Content -Path \'{inbox}\' -Wait -Tail 0 | ForEach-Object {{ \'[xtalk] \' + $_ }}"'
    else:
        command = f"tail -F -n 0 {shlex.quote(str(inbox))} 2>/dev/null | while IFS= read -r line; do echo \"[xtalk] $line\"; done"
    _arm_watchdog(room.id)
    return {
        "monitor_command": command,
        "inbox_path": str(inbox),
        "recommended_resume_strategy": _strategy(),
        "presence": "listening",
        "warning": "A persistent monitor temporarily occupies clients that cannot accept prompts concurrently.",
    }


def handle_ask(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    target = args["to"].strip()
    to = ["*"] if target == "*" else [resolve_alias(room, target) or ""]
    if to == [""]:
        raise ValueError(f"unknown alias or sid: {target}")

    # Pre-flight deadlock check: if all intended targets are already stuck in
    # waiting_reply, warn the caller before they enter waiting_reply too.
    members = room.current_members()
    target_sids = set(to) if to != ["*"] else {m["sid"] for m in members if m["sid"] != CTX.sid}
    relevant = [m for m in members if m["sid"] in target_sids]
    all_waiting = bool(relevant) and all(m.get("mode") == "waiting_reply" for m in relevant)
    any_listening = any(m.get("mode") == "listening" for m in relevant)
    warning: str | None = None
    if all_waiting:
        warning = f"all targets are in waiting_reply mode; entering waiting_reply yourself risks deadlock"
    elif not relevant:
        warning = "no live members to receive this ask"
    elif not any_listening:
        warning = "no target is in `listening` mode; reply may be delayed"

    tid = args.get("thread") or new_thread_id()
    msg = Message(new_msg_id(), now_iso(), CTX.sid or "", CTX.memberships[room.id], to, "ask", args["body"])
    _encrypt_outgoing(room, msg)
    room.append_message(tid, msg)

    deadline = time.time() + DEADLOCK_GRACE_SECONDS
    room.append_presence(
        CTX.sid or "", CTX.memberships[room.id], "waiting_reply",
        target_msg_id=msg.msg_id, deadline_ts=deadline,
    )

    thread_file = shlex.quote(str(room.thread_path(tid)))
    reply_pattern = f'"in_reply_to":"{msg.msg_id}"'
    hint_pattern = '"kind":"deadlock_hint"'
    # Also tail the shared system thread so a deadlock hint written there
    # unblocks Monitor. `tail -F` on multiple files interleaves lines and each
    # match still trips `grep -m 1`.
    system_tid = f"tid-system-{room.id[:16]}"
    system_file = shlex.quote(str(room.thread_path(system_tid)))
    wait = (
        f"tail -F -n 0 {thread_file} {system_file} 2>/dev/null | "
        f"grep --line-buffered -m 1 -F -e {shlex.quote(reply_pattern)} -e {shlex.quote(hint_pattern)}"
    )
    _arm_watchdog(room.id)

    response: dict[str, Any] = {
        "thread_id": tid,
        "msg_id": msg.msg_id,
        "to": to,
        "room": room.id,
        "wait_command": wait,
        "recommended_resume_strategy": _strategy(),
        "presence": "waiting_reply",
        "deadline_ts": deadline,
    }
    if warning:
        response["warning"] = warning
        response["deadlock_risk"] = all_waiting
    return response


def _message_dict(m: Message) -> dict[str, Any]:
    return {"msg_id": m.msg_id, "ts": m.ts, "from": m.from_sid, "from_alias": m.from_alias, "to": m.to, "kind": m.kind, "in_reply_to": m.in_reply_to, "body": m.body, "meta": m.meta}


def handle_read(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    raw = room.read_thread(args["thread"], args.get("count", DEFAULT_READ_COUNT))
    messages = [_decrypt_incoming(room, m) for m in raw]
    return {"thread_id": args["thread"], "room": room.id, "count": len(messages), "messages": [_message_dict(m) for m in messages]}


def handle_reply(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    tail = room.read_thread(args["thread"], 100)
    ref = args.get("in_reply_to")
    origin = next((m for m in tail if m.msg_id == ref), None) if ref else next((m for m in reversed(tail) if m.kind == "ask" and (CTX.sid in m.to or m.to == ["*"])), None)
    to = [origin.from_sid] if origin else ["*"]
    msg = Message(new_msg_id(), now_iso(), CTX.sid or "", CTX.memberships[room.id], to, "reply", args["body"], ref or (origin.msg_id if origin else None))
    _encrypt_outgoing(room, msg)
    room.append_message(args["thread"], msg)
    room.append_presence(CTX.sid or "", CTX.memberships[room.id], "idle")
    return {"msg_id": msg.msg_id, "to": to, "room": room.id, "presence": "idle"}


def handle_close(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    report_to = resolve_alias(room, args["report_to"]) or args["report_to"]
    others = list({m.from_sid for m in room.read_thread(args["thread"], 100) if m.from_sid != CTX.sid}) or ["*"]
    msg = Message(new_msg_id(), now_iso(), CTX.sid or "", CTX.memberships[room.id], others, "done", args["summary"], meta={"report_to": report_to})
    _encrypt_outgoing(room, msg)
    room.append_message(args["thread"], msg)
    room.append_presence(CTX.sid or "", CTX.memberships[room.id], "idle")
    return {"msg_id": msg.msg_id, "report_to": report_to, "room": room.id, "presence": "idle"}


def _leave_room(rid: str) -> dict[str, Any]:
    room = CTX.room(rid)
    alias = CTX.memberships[rid]
    _cancel_heartbeat(rid, CTX.sid or "")
    room.append_member_event({"event": "leave", "sid": CTX.sid, "alias": alias, "epoch": time.time(), "ts": now_iso()})
    room.notify_membership(
        "member_left", CTX.sid or "", alias,
        [m["sid"] for m in room.current_members()],
    )
    del CTX.memberships[rid]
    CTX.active_room = next(iter(CTX.memberships), None)
    if not CTX.memberships:
        try:
            storage.session_path(CTX.sid or "").unlink()
        except FileNotFoundError:
            pass
        CTX.sid = None
    else:
        CTX.persist()
    return {"ok": True, "left_as": alias, "room": rid, "active_room": CTX.active_room}


def handle_leave(args: dict[str, Any]) -> dict[str, Any]:
    if not CTX.sid:
        return {"ok": True, "note": "not registered"}
    rid = args.get("room") or CTX.active_room
    if not rid:
        return {"ok": True, "note": "no active room"}
    return _leave_room(rid)


def _encrypt_outgoing(room: Room, msg: Message) -> Message:
    key = CTX.room_keys.get(room.id)
    if not key:
        return msg
    envelope = crypto.encrypt_body(key, room.id, msg.msg_id, msg.kind, msg.body)
    msg.body = ""
    msg.meta = {**(msg.meta or {}), "enc": envelope}
    return msg


def _decrypt_incoming(room: Room, msg: Message) -> Message:
    envelope = (msg.meta or {}).get("enc")
    if not envelope:
        return msg
    key = CTX.room_keys.get(room.id)
    if not key:
        msg.body = "[encrypted — missing key]"
        return msg
    try:
        msg.body = crypto.decrypt_body(key, room.id, msg.msg_id, msg.kind, envelope)
    except Exception as exc:
        msg.body = f"[decryption failed: {exc}]"
    return msg


def handle_room_create(args: dict[str, Any]) -> dict[str, Any]:
    CTX.require_registered()
    rid = new_room_id()
    secret = secrets.token_urlsafe(32)
    is_e2ee = bool(args.get("e2ee", False))
    if is_e2ee:
        enc_key, auth_key = crypto.derive_keys(secret)
        verifier = crypto.invite_verifier(auth_key)
        CTX.room_keys[rid] = enc_key
    else:
        verifier = hashlib.sha256(secret.encode()).hexdigest()
    room = Room(rid)
    room.ensure(
        "",
        name=args["name"],
        visibility=args.get("visibility", "private"),
        transport=args.get("transport", "local"),
        e2ee=is_e2ee,
        ttl_seconds=int(args.get("ttl_seconds", 86400)),
        invite_verifier=verifier,
    )
    _join(room, args.get("alias") or CTX.alias or "agent")
    payload = {"room": rid, "transport": args.get("transport", "local"), "e2ee": is_e2ee}
    return {
        "room": rid,
        "name": args["name"],
        "invite": crypto.encode_invite(payload, secret),
        "e2ee": is_e2ee,
        "active_room": rid,
    }


def handle_room_join(args: dict[str, Any]) -> dict[str, Any]:
    CTX.require_registered()
    payload, secret = crypto.decode_invite(args["invite"])
    if not secret:
        raise ValueError("invite missing secret")
    room = Room(payload["room"])
    meta = room.metadata()
    if not meta:
        raise ValueError("room does not exist locally; start/connect the relay first")
    is_e2ee = bool(meta.get("e2ee")) or bool(payload.get("e2ee"))
    stored = meta.get("invite_verifier") or meta.get("invite_hash", "")
    if is_e2ee:
        enc_key, auth_key = crypto.derive_keys(secret)
        if not secrets.compare_digest(stored, crypto.invite_verifier(auth_key)):
            raise ValueError("invalid invite secret")
        CTX.room_keys[room.id] = enc_key
    else:
        if not secrets.compare_digest(stored, hashlib.sha256(secret.encode()).hexdigest()):
            raise ValueError("invalid invite secret")
    _join(room, args["alias"])
    return {"room": room.id, "alias": args["alias"], "active_room": room.id, "e2ee": is_e2ee}


def handle_room_list(args: dict[str, Any]) -> dict[str, Any]:
    CTX.require_registered()
    return {"active_room": CTX.active_room, "rooms": [{"room": rid, "alias": alias, **Room(rid).metadata()} for rid, alias in CTX.memberships.items()]}


def handle_room_use(args: dict[str, Any]) -> dict[str, Any]:
    CTX.room(args["room"])
    CTX.active_room = args["room"]
    CTX.persist()
    return {"active_room": CTX.active_room, "alias": CTX.alias}


def handle_wait(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    inbox = room.inbox_path(CTX.sid or "")
    timeout = min(max(int(args.get("timeout_ms", 30000)), 0), 1800000) / 1000
    deadline_mono = time.monotonic() + timeout
    cursor = CTX.cursors.get(room.id, 0)
    kinds = set(args.get("kinds", []))

    # Announce presence for the deadlock detector — waiting_reply if we're
    # gated on a specific reply, else listening.
    mode = "waiting_reply" if args.get("in_reply_to") else "listening"
    deadlock_deadline = time.time() + DEADLOCK_GRACE_SECONDS
    presence_extra: dict[str, Any] = {"deadline_ts": deadlock_deadline}
    if args.get("in_reply_to"):
        presence_extra["target_msg_id"] = args["in_reply_to"]
    room.append_presence(CTX.sid or "", CTX.memberships[room.id], mode, **presence_extra)
    _arm_watchdog(room.id)

    def _idle() -> None:
        try:
            room.append_presence(CTX.sid or "", CTX.memberships[room.id], "idle")
        except Exception:
            pass

    last_deadlock_check = 0.0
    try:
        while True:
            try:
                lines = inbox.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            for index, line in enumerate(lines[cursor:], cursor + 1):
                cursor = index
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # deadlock_hint always short-circuits regardless of filters.
                if event.get("kind") == "deadlock_hint":
                    CTX.cursors[room.id] = cursor
                    CTX.persist()
                    return {"timed_out": False, "event": event, "cursor": cursor, "deadlock_hint": True}
                if args.get("thread") and event.get("tid") != args["thread"]:
                    continue
                if kinds and event.get("kind") not in kinds:
                    continue
                if args.get("in_reply_to"):
                    if not event.get("tid"):
                        continue
                    messages = room.read_thread(event["tid"], 100)
                    if not any(m.msg_id == event["msg_id"] and m.in_reply_to == args["in_reply_to"] for m in messages):
                        continue
                CTX.cursors[room.id] = cursor
                CTX.persist()
                return {"timed_out": False, "event": event, "cursor": cursor}
            CTX.cursors[room.id] = len(lines)
            cursor = len(lines)
            # Opportunistic deadlock check every 5s of wall time — no separate thread needed.
            if time.monotonic() - last_deadlock_check > 5.0:
                last_deadlock_check = time.monotonic()
                waiters = room.check_deadlock()
                if waiters:
                    room.emit_deadlock_hint(waiters)
                    # Loop will pick it up on the next inbox read.
            if time.monotonic() >= deadline_mono:
                CTX.persist()
                return {"timed_out": True, "cursor": cursor}
            time.sleep(min(0.2, max(0.05, deadline_mono - time.monotonic())))
    finally:
        _idle()


def handle_status(args: dict[str, Any]) -> dict[str, Any]:
    return {"registered": bool(CTX.sid), "sid": CTX.sid, "client": CTX.client, "capabilities": sorted(CTX.capabilities), "active_room": CTX.active_room, "rooms": len(CTX.memberships), "recommended_resume_strategy": _strategy(), "root": str(storage.XTALK_ROOT), "version": "0.2.4"}


def handle_presence(args: dict[str, Any]) -> dict[str, Any]:
    CTX.require_registered()
    room = CTX.room(args.get("room"))
    mode = args["mode"]
    extra: dict[str, Any] = {}
    if mode == "waiting_reply":
        extra["deadline_ts"] = time.time() + DEADLOCK_GRACE_SECONDS
        if args.get("target_msg_id"):
            extra["target_msg_id"] = args["target_msg_id"]
        _arm_watchdog(room.id)
    room.append_presence(CTX.sid or "", CTX.memberships[room.id], mode, **extra)
    return {"presence": mode, "room": room.id, **({"deadline_ts": extra["deadline_ts"]} if "deadline_ts" in extra else {})}


def handle_thread_list(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    threads = room.list_threads()
    for t in threads:
        last = t["last_message"]
        msg = Message(
            msg_id=last["msg_id"],
            ts=last["ts"],
            from_sid=last["from"],
            from_alias=last["from_alias"],
            to=[],
            kind=last["kind"],
            body=""
        )
        raw_msgs = room.read_thread(t["thread_id"], count=1)
        if raw_msgs:
            msg.meta = raw_msgs[0].meta
            msg.body = raw_msgs[0].body
            decrypted = _decrypt_incoming(room, msg)
            t["last_message"]["body"] = decrypted.body
            if t["closed"]:
                t["summary"] = decrypted.body
        else:
            t["last_message"]["body"] = ""
    return {"room": room.id, "threads": threads}


def handle_broadcast(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    tid = new_thread_id()
    msg = Message(new_msg_id(), now_iso(), CTX.sid or "", CTX.memberships[room.id], ["*"], "broadcast", args["body"])
    _encrypt_outgoing(room, msg)
    room.append_message(tid, msg)
    room.append_presence(CTX.sid or "", CTX.memberships[room.id], "idle")
    return {"thread_id": tid, "msg_id": msg.msg_id, "to": ["*"], "room": room.id, "presence": "idle"}


def handle_daemon_control(args: dict[str, Any]) -> dict[str, Any]:
    action = args["action"]
    room_id = args.get("room") or CTX.active_room
    relay_url = args.get("relay_url")

    if action in {"subscribe", "unsubscribe"} and not room_id:
        raise ValueError("room is required for subscribe/unsubscribe actions")

    if action == "start":
        import subprocess
        import sys
        running, pid = daemon.is_running()
        if not running:
            subprocess.Popen(
                [sys.executable, "-m", "xtalk.cli", "daemon", "start"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.2)
        running, pid = daemon.is_running()
        return {"status": "running" if running else "failed_to_start", "pid": pid}

    if action == "stop":
        stopped = daemon.stop()
        return {"stopped": stopped, "status": "stopped" if stopped else "not_running"}

    if action == "status":
        return daemon.status()

    if action == "subscribe":
        if not relay_url:
            raise ValueError("relay_url is required to subscribe")
        room = Room(room_id)
        meta = room.metadata()
        if not meta:
            raise ValueError(f"room {room_id} not found locally")

        verifier = meta.get("invite_verifier") or ""
        alias = CTX.memberships.get(room_id, CTX.alias or "agent")

        sub = daemon.Subscription(
            relay_url=relay_url,
            room_id=room_id,
            sid=CTX.sid or "",
            alias=alias,
            verifier=verifier,
            resume_cursor=CTX.cursors.get(room_id, 0),
        )
        daemon.add_subscription(sub)

        running, pid = daemon.is_running()
        if not running:
            import subprocess
            import sys
            subprocess.Popen(
                [sys.executable, "-m", "xtalk.cli", "daemon", "start"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.2)
            running, pid = daemon.is_running()

        return {
            "subscribed": True,
            "room_id": room_id,
            "daemon_id": sub.daemon_id,
            "daemon_running": running,
            "pid": pid,
        }

    if action == "unsubscribe":
        if not relay_url:
            subs = daemon.load_subscriptions()
            match = next((s for s in subs if s.room_id == room_id and s.sid == CTX.sid), None)
            if not match:
                return {"unsubscribed": False, "note": "no active subscription found for room"}
            relay_url = match.relay_url

        daemon.remove_subscription(relay_url, room_id, CTX.sid or "")
        return {"unsubscribed": True, "room_id": room_id}

    raise ValueError(f"unsupported action: {action}")


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "xtalk_register": handle_register, "xtalk_discover": handle_discover, "xtalk_listen": handle_listen, "xtalk_ask": handle_ask, "xtalk_read": handle_read, "xtalk_reply": handle_reply, "xtalk_close": handle_close, "xtalk_leave": handle_leave,
    "xtalk_room_create": handle_room_create, "xtalk_room_join": handle_room_join, "xtalk_room_list": handle_room_list, "xtalk_room_use": handle_room_use, "xtalk_room_leave": lambda a: _leave_room(a["room"]), "xtalk_wait": handle_wait, "xtalk_status": handle_status, "xtalk_presence": handle_presence,
    "xtalk_thread_list": handle_thread_list, "xtalk_broadcast": handle_broadcast, "xtalk_daemon_control": handle_daemon_control,
}


def initialize_project_asset(workspace: str | Path | None = None) -> dict[str, Any]:
    """Create/load the project-local room pointer when the MCP starts."""
    return ensure_project_binding(workspace or os.getcwd())


async def _run() -> None:
    initialize_project_asset()
    server: Server = Server("xtalk")
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS
    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            result = HANDLERS[name](arguments or {})
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": {"code": type(exc).__name__, "message": str(exc), "retryable": False}, "error_type": type(exc).__name__}))]
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
