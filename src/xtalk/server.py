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
from . import __version__

from . import crypto, storage, daemon
from .storage import DEFAULT_READ_COUNT, MAX_READ_COUNT, TASK_PRIORITIES, TASK_STATUSES, Message, Room, ensure_project_binding, new_msg_id, new_room_id, new_task_id, new_thread_id, now_iso, project_manifest_path, resolve_alias, save_session, workspace_hash


class SessionCtx:
    def __init__(self) -> None:
        self.sid: str | None = None
        self.client = "other"
        self.capabilities: set[str] = set()
        self.memberships: dict[str, str] = {}
        self.active_room: str | None = None
        self.cursors: dict[str, int] = {}
        # Fingerprint of the last consumed inbox line per room. Used by
        # `handle_wait` to detect an external truncate+rewrite of the inbox
        # (rewind) that a pure length compare would miss. Session-local; not
        # persisted — a fresh session simply starts without a mark.
        self.cursor_marks: dict[str, str] = {}
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
    # Check cancellation under lock BEFORE writing — avoids a late heartbeat
    # event after `_cancel_heartbeat` (e.g. after leave) that would keep the
    # departed session visible for another lease window.
    with _HEARTBEAT_LOCK:
        if key not in _HEARTBEAT_TIMERS:
            return
    # If the room storage disappeared (deleted on disk, permissions lost),
    # stop rearming; otherwise the timer spins forever on an unreachable path.
    try:
        Room(room_id).append_member_event({
            "event": "heartbeat", "sid": sid, "alias": alias,
            "epoch": time.time(), "ts": now_iso(), "client": client, "pid": pid,
        })
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        with _HEARTBEAT_LOCK:
            _HEARTBEAT_TIMERS.pop(key, None)
        return
    except Exception:
        # Transient errors (e.g. brief Windows sharing violation) — reschedule.
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
            args=(room_id, sid, alias, CTX.client, os.getpid()),
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
    _tool(
        "xtalk_unregister",
        "Fully tear down this session: leave every room, cancel heartbeats, delete the on-disk session file. Use when the agent is shutting down for real, not between tasks — a plain reply/close does not require this.",
        {},
    ),
    _tool("xtalk_room_create", "Create and join a room; returns an invite URI.", {"name": {"type": "string"}, "alias": {"type": "string"}, "visibility": {"type": "string", "enum": ["private", "local"]}, "transport": {"type": "string", "enum": ["local", "relay"]}, "e2ee": {"type": "boolean"}, "ttl_seconds": {"type": "integer"}}, ["name"]),
    _tool("xtalk_room_join", "Join a room using an invite URI.", {"invite": {"type": "string"}, "alias": {"type": "string"}}, ["invite", "alias"]),
    _tool("xtalk_room_list", "List rooms joined by this session."),
    _tool("xtalk_room_use", "Set the active room without leaving other rooms.", {"room": {"type": "string"}}, ["room"]),
    _tool("xtalk_room_leave", "Leave one room.", ROOM, ["room"]),
    _tool(
        "xtalk_wait",
        "Wait until a matching event lands in this session's inbox. Unbounded by default — the call only returns when a message, mention, or deadlock_hint arrives. Mentions (`@alias` in another agent's body) and deadlock_hint always short-circuit filters. Pass `timeout_ms` only when the MCP host enforces a hard tool timeout; otherwise omit it.",
        {
            "room": {"type": "string", "description": "Room id; defaults to active room"},
            "thread": {"type": "string", "description": "Only match events in this thread"},
            "in_reply_to": {"type": "string", "description": "Only match a reply to this msg_id"},
            "kinds": {"type": "array", "items": {"type": "string"}, "description": "Whitelist of event kinds (mention/deadlock_hint always pass)"},
            "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 1800000, "description": "Optional hard cap in ms. Omit or set 0 for unbounded wait."},
        },
    ),
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
    _tool(
        "xtalk_assign",
        "Assign a task to another member. The assignee sees a `mention`-style wake event in their inbox and can respond with xtalk_ack. Use when acting as coordinator/lead — the tool doesn't enforce a role, but a task creates a shared ledger both sides can track.",
        {
            "to": {"type": "string", "description": "Assignee alias or sid"},
            "title": {"type": "string", "description": "Short one-line task title"},
            "description": {"type": "string", "description": "Full task description (up to 8 KiB)"},
            "priority": {"type": "string", "enum": list(TASK_PRIORITIES)},
            "room": {"type": "string"},
        },
        ["to", "title"],
    ),
    _tool(
        "xtalk_ack",
        "Update the status of a task previously assigned via xtalk_assign. Assignees walk their task through pending → in_progress → done (or blocked/cancelled). Assigners can also call it to cancel/close.",
        {
            "task_id": {"type": "string"},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
            "note": {"type": "string", "description": "Optional progress note or blocker reason"},
            "room": {"type": "string"},
        },
        ["task_id", "status"],
    ),
    _tool(
        "xtalk_tasks",
        "List tasks in the current room with their live status. Use before starting work to see what's already assigned to you, or as a coordinator to check progress.",
        {
            "assignee": {"type": "string", "description": "Filter by assignee alias/sid ('me' = self)"},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
            "room": {"type": "string"},
        },
    ),
    _tool(
        "xtalk_stream",
        "Return a live snapshot of the room: current members (with mode), open tasks, and every inbox event newer than `cursor`. Use this as a non-blocking peek — unlike xtalk_wait it never sleeps and always returns immediately. Pair with the returned `next_cursor` for successive delta calls, or with a periodic loop to build a lightweight dashboard.",
        {
            "cursor": {"type": "integer", "minimum": 0, "description": "Inbox line offset. Pass the `next_cursor` from the previous call; omit or 0 for a full head start (returns members + last recent_limit events)."},
            "recent_limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Cap on recent events when cursor is 0 (default 20). Ignored once cursor > 0."},
            "include_tasks": {"type": "boolean", "description": "Include task ledger snapshot (default true)."},
            "room": {"type": "string"},
        },
    ),
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
    # Atomic alias check + append under the members file lock; prevents two
    # concurrent registers with the same alias from both succeeding.
    existing = room.join_with_alias_check({
        "event": "join", "sid": CTX.sid, "alias": alias,
        "epoch": time.time(), "ts": now_iso(),
        "client": CTX.client, "pid": os.getpid(),
    })
    CTX.memberships[room.id] = alias
    CTX.active_room = room.id
    CTX.persist()
    _arm_heartbeat(room.id, CTX.sid or "", alias)
    room.notify_membership("member_joined", CTX.sid or "", alias, [m["sid"] for m in existing])


def handle_register(args: dict[str, Any]) -> dict[str, Any]:
    global CTX
    requested_alias = args["alias"].strip()
    requested_workspace = args.get("workspace")
    if CTX.sid:
        # Re-registering an existing session. Three cases:
        #   1. Same alias, same/no workspace → idempotent: re-arm heartbeat.
        #   2. New alias, same workspace → the caller wants to rename in this
        #      room. Emit a leave + fresh join so peers observe the change.
        #   3. Different workspace → the caller has moved projects. Refuse
        #      and tell them to xtalk_leave first, otherwise we'd silently
        #      keep them attached to the old room.
        current_alias = CTX.alias
        current_room = CTX.active_room
        if requested_workspace:
            resolved_ws = str(storage.workspace_root(requested_workspace))
            current_ws = None
            if current_room:
                current_ws = Room(current_room).metadata().get("workspace_path")
            if current_ws and resolved_ws != current_ws:
                raise ValueError(
                    "already registered in a different workspace; call xtalk_leave first"
                )
        if requested_alias and requested_alias != current_alias and current_room:
            room = Room(current_room)
            # Rename = leave + join in one call.
            _cancel_heartbeat(current_room, CTX.sid or "")
            room.append_member_event({
                "event": "leave", "sid": CTX.sid, "alias": current_alias,
                "epoch": time.time(), "ts": now_iso(),
            })
            del CTX.memberships[current_room]
            CTX.active_room = None
            _join(room, requested_alias)
        elif current_room and current_alias:
            _arm_heartbeat(current_room, CTX.sid or "", current_alias)
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
    # Validate body size before stamping presence: otherwise a body that
    # exceeds MAX_BODY_BYTES leaves the caller advertised as `waiting_reply`
    # with a watchdog armed, and the deadlock detector sees phantom waiters.
    body = args["body"]
    if len(body.encode("utf-8")) > storage.MAX_BODY_BYTES:
        raise ValueError(f"body exceeds {storage.MAX_BODY_BYTES} bytes")
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
    inbox_file = shlex.quote(str(room.inbox_path(CTX.sid or "")))
    reply_pattern = f'"in_reply_to":"{msg.msg_id}"'
    hint_pattern = '"kind":"deadlock_hint"'
    mention_pattern = '"kind":"mention"'
    # Also tail the shared system thread + our own inbox so a deadlock hint
    # or an @-mention unblocks Monitor even when the underlying message is
    # in a different thread. `tail -F` on multiple files interleaves lines
    # and each match still trips `grep -m 1`.
    system_tid = f"tid-system-{room.id[:16]}"
    system_file = shlex.quote(str(room.thread_path(system_tid)))
    wait = (
        f"tail -F -n 0 {thread_file} {system_file} {inbox_file} 2>/dev/null | "
        f"grep --line-buffered -m 1 -F "
        f"-e {shlex.quote(reply_pattern)} "
        f"-e {shlex.quote(hint_pattern)} "
        f"-e {shlex.quote(mention_pattern)}"
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
    raw_report_to = args["report_to"]
    report_to = resolve_alias(room, raw_report_to)
    if report_to is None:
        # A typo'd or already-departed reporter used to silently fall back to
        # the raw string, so the `done` event went nowhere. Fail loudly so
        # the caller can pick a real member.
        if raw_report_to.startswith("sid-"):
            report_to = raw_report_to
        else:
            raise ValueError(f"unknown report_to alias: {raw_report_to}")
    others = {m.from_sid for m in room.read_thread(args["thread"], 100) if m.from_sid != CTX.sid}
    # Include the reporter even if they never posted in the thread — a lurker
    # can be nominated to report, and they still need the `done` notification.
    if report_to and report_to != CTX.sid and report_to.startswith("sid-"):
        others.add(report_to)
    recipients = list(others) or ["*"]
    msg = Message(new_msg_id(), now_iso(), CTX.sid or "", CTX.memberships[room.id], recipients, "done", args["summary"], meta={"report_to": report_to})
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


def handle_unregister(args: dict[str, Any]) -> dict[str, Any]:
    """Full session teardown — leave every room, cancel all heartbeats,
    delete the on-disk session file. Idempotent when already unregistered."""
    global CTX
    if not CTX.sid:
        return {"ok": True, "note": "not registered"}
    left: list[str] = []
    for rid in list(CTX.memberships.keys()):
        try:
            _leave_room(rid)
            left.append(rid)
        except Exception:
            pass
    _cancel_all_heartbeats()
    # _leave_room already removes the session file when the last membership
    # goes; belt-and-braces for the case where memberships was already empty.
    if CTX.sid:
        try:
            storage.session_path(CTX.sid).unlink()
        except FileNotFoundError:
            pass
        CTX = SessionCtx()
    return {"ok": True, "left_rooms": left}


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
    # `timeout_ms=0` (or omitted) is now the default: wait unbounded until
    # something actually happens. Callers on hosts with a hard tool timeout
    # can still pass a bounded value; anything <= 0 disables the deadline.
    raw_timeout = args.get("timeout_ms")
    if raw_timeout is None or int(raw_timeout) <= 0:
        deadline_mono: float | None = None
    else:
        deadline_mono = time.monotonic() + min(int(raw_timeout), 1800000) / 1000
    cursor = CTX.cursors.get(room.id, 0)
    kinds = set(args.get("kinds", []))

    def _rewind_detected(current_lines: list[str]) -> bool:
        """Return True when the inbox has been truncated/rotated under us.

        Simple len-based check misses the case where the file is truncated
        and then rewritten to a length ≥ old cursor: the new prefix looks
        like the old prefix from an offset alone, but the sentinel line the
        cursor was pointing at has changed. Compare the last-consumed line
        (index cursor-1) against the fingerprint we stored when we advanced
        past it.
        """
        if cursor <= 0:
            return False
        if cursor > len(current_lines):
            return True
        stored = CTX.cursor_marks.get(room.id, "")
        if not stored:
            return False
        # `cursor` points *after* the last consumed line, so its content is at
        # index cursor - 1.
        return current_lines[cursor - 1] != stored

    # Announce presence for the deadlock detector — waiting_reply if we're
    # gated on a specific reply, else listening.
    mode = "waiting_reply" if args.get("in_reply_to") else "listening"
    presence_extra: dict[str, Any] = {}
    if args.get("in_reply_to"):
        # If the caller is retrying `xtalk_wait` on the same reply target,
        # preserve their original grace deadline. Otherwise a client that
        # polls every 30s indefinitely pushes deadline_ts forward on every
        # tick and the watchdog never observes the mutual-wait window.
        existing = next(
            (m for m in room.current_members()
             if m.get("sid") == CTX.sid and m.get("mode") == "waiting_reply"
             and m.get("target_msg_id") == args["in_reply_to"]
             and m.get("deadline_ts") is not None),
            None,
        )
        deadlock_deadline = float(existing["deadline_ts"]) if existing else time.time() + DEADLOCK_GRACE_SECONDS
        presence_extra["target_msg_id"] = args["in_reply_to"]
        presence_extra["deadline_ts"] = deadlock_deadline
    else:
        presence_extra["deadline_ts"] = time.time() + DEADLOCK_GRACE_SECONDS
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
                lines = storage.concurrent_read_text(inbox).splitlines()
            except FileNotFoundError:
                lines = []
            if _rewind_detected(lines):
                cursor = 0
                CTX.cursors[room.id] = 0
                CTX.cursor_marks.pop(room.id, None)
            for index, line in enumerate(lines[cursor:], cursor + 1):
                cursor = index
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # deadlock_hint always short-circuits regardless of filters.
                if event.get("kind") == "deadlock_hint":
                    CTX.cursors[room.id] = cursor
                    CTX.cursor_marks[room.id] = line
                    CTX.persist()
                    return {"timed_out": False, "event": event, "cursor": cursor, "deadlock_hint": True}
                # Mentions and task lifecycle events short-circuit filters —
                # someone called our alias or handed us a task; that's
                # always worth surfacing regardless of what thread/kind the
                # caller was originally waiting on.
                event_kind = event.get("kind")
                if event_kind in {"mention", "task_assigned", "task_update"}:
                    CTX.cursors[room.id] = cursor
                    CTX.cursor_marks[room.id] = line
                    CTX.persist()
                    flag_key = "mention" if event_kind == "mention" else "task_event"
                    return {"timed_out": False, "event": event, "cursor": cursor, flag_key: True}
                if args.get("thread") and event.get("tid") != args["thread"]:
                    continue
                if kinds and event.get("kind") not in kinds:
                    continue
                if args.get("in_reply_to"):
                    if not event.get("tid"):
                        continue
                    # Fast path: the inbox event now carries `in_reply_to`
                    # directly. Fall back to a full thread scan for legacy
                    # events written before the field was added.
                    if "in_reply_to" in event:
                        if event["in_reply_to"] != args["in_reply_to"]:
                            continue
                    else:
                        messages = room.read_thread(event["tid"], storage.MAX_READ_COUNT)
                        if not any(m.msg_id == event["msg_id"] and m.in_reply_to == args["in_reply_to"] for m in messages):
                            continue
                CTX.cursors[room.id] = cursor
                CTX.cursor_marks[room.id] = line
                CTX.persist()
                return {"timed_out": False, "event": event, "cursor": cursor}
            CTX.cursors[room.id] = len(lines)
            if lines:
                CTX.cursor_marks[room.id] = lines[-1]
            cursor = len(lines)
            # Opportunistic deadlock check every 5s of wall time — no separate thread needed.
            if time.monotonic() - last_deadlock_check > 5.0:
                last_deadlock_check = time.monotonic()
                waiters = room.check_deadlock()
                if waiters:
                    room.emit_deadlock_hint(waiters)
                    # Loop will pick it up on the next inbox read.
            if deadline_mono is not None and time.monotonic() >= deadline_mono:
                CTX.persist()
                return {"timed_out": True, "cursor": cursor}
            if deadline_mono is None:
                # Unbounded wait: settle on a modest poll cadence so we can
                # still tick the deadlock check every 5s and pick up new
                # inbox lines quickly. `time.sleep(0.2)` is our upper bound.
                time.sleep(0.2)
            else:
                time.sleep(min(0.2, max(0.05, deadline_mono - time.monotonic())))
    finally:
        _idle()


def handle_status(args: dict[str, Any]) -> dict[str, Any]:
    return {"registered": bool(CTX.sid), "sid": CTX.sid, "client": CTX.client, "capabilities": sorted(CTX.capabilities), "active_room": CTX.active_room, "rooms": len(CTX.memberships), "recommended_resume_strategy": _strategy(), "root": str(storage.XTALK_ROOT), "version": __version__}


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


def _resolve_room_member(room: Room, alias_or_sid: str) -> tuple[str, str]:
    """Return (sid, alias) for a room member referenced by alias or sid.

    Rejects unknown targets loudly rather than silently degrading — the
    caller almost always wants to know they mistyped a name.
    """
    if alias_or_sid == "me":
        sid = CTX.sid or ""
        alias = CTX.memberships.get(room.id, "")
        if not sid:
            raise ValueError("cannot resolve 'me' — not registered")
        return sid, alias
    sid = resolve_alias(room, alias_or_sid)
    if not sid:
        raise ValueError(f"unknown member: {alias_or_sid}")
    member = next((m for m in room.current_members() if m.get("sid") == sid), None)
    alias = (member or {}).get("alias", alias_or_sid)
    return sid, alias


def handle_assign(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    title = str(args["title"]).strip()
    if not title or len(title) > 200:
        raise ValueError("title must be 1..200 characters")
    description = str(args.get("description", ""))
    if len(description.encode("utf-8")) > storage.MAX_BODY_BYTES:
        raise ValueError(f"description exceeds {storage.MAX_BODY_BYTES} bytes")
    priority = args.get("priority", "normal")
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"priority must be one of {TASK_PRIORITIES}")
    assignee_sid, assignee_alias = _resolve_room_member(room, str(args["to"]))
    if assignee_sid == CTX.sid:
        raise ValueError("cannot assign a task to yourself")

    task_id = new_task_id()
    now = now_iso()
    event = {
        "event": "open", "task_id": task_id, "ts": now,
        "title": title, "description": description, "priority": priority,
        "assignee_sid": assignee_sid, "assignee_alias": assignee_alias,
        "assigned_by_sid": CTX.sid or "", "assigned_by_alias": CTX.memberships[room.id],
    }
    room.append_task_event(event)

    # Wake the assignee: a task_assigned event goes into their inbox,
    # matching the mention pattern so xtalk_wait short-circuits on it.
    # `msg_id` is per-event so a task with a full lifecycle (open + acks)
    # keeps distinct identifiers per inbox line. Include a truncated
    # description directly so the assignee can decide whether to start
    # without a second xtalk_tasks roundtrip.
    _INLINE_DESC_LIMIT = 500
    description_snippet = description[:_INLINE_DESC_LIMIT] if description else ""
    truncated = bool(description) and len(description) > _INLINE_DESC_LIMIT
    inbox_event_data: dict[str, Any] = {
        "msg_id": new_msg_id(), "task_id": task_id, "room": room.id,
        "from": CTX.sid or "", "from_alias": CTX.memberships[room.id],
        "kind": "task_assigned", "title": title, "priority": priority,
        "ts": now,
    }
    if description_snippet:
        inbox_event_data["description"] = description_snippet
        if truncated:
            inbox_event_data["description_truncated"] = True
    inbox_event = json.dumps(inbox_event_data, separators=(",", ":"))
    storage.atomic_append(room.inbox_path(assignee_sid), inbox_event)
    # Mirror the event into the assigner's inbox too — otherwise a
    # coordinator has to poll xtalk_tasks to know their assign call
    # actually landed. Keep it as a distinct msg_id so downstream logic
    # doesn't dedup with the assignee copy.
    assigner_event = json.dumps({
        "msg_id": new_msg_id(), "task_id": task_id, "room": room.id,
        "from": CTX.sid or "", "from_alias": CTX.memberships[room.id],
        "kind": "task_assigned_ack", "title": title, "priority": priority,
        "assignee_sid": assignee_sid, "assignee_alias": assignee_alias,
        "ts": now,
    }, separators=(",", ":"))
    storage.atomic_append(room.inbox_path(CTX.sid or ""), assigner_event)

    return {
        "task_id": task_id, "assignee_sid": assignee_sid,
        "assignee_alias": assignee_alias, "priority": priority,
        "status": "pending", "room": room.id,
    }


def handle_ack(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    task_id = str(args["task_id"])
    status = args["status"]
    if status not in TASK_STATUSES:
        raise ValueError(f"status must be one of {TASK_STATUSES}")

    # Serialize the whole find→check→append sequence on this task so two
    # concurrent acks (assignee=done, assigner=cancelled) resolve to a
    # deterministic order instead of both landing and clobbering.
    with room.task_lock(task_id):
        task = room.find_task(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")

        expected = args.get("expected_status")
        if expected and task["status"] != expected:
            raise ValueError(
                f"task status is {task['status']!r}, not {expected!r}; refresh and retry"
            )

        # Only the assignee or the assigner may transition a task. Any other
        # room member calling xtalk_ack is almost certainly a mistake.
        caller_sid = CTX.sid or ""
        if caller_sid not in {task["assignee_sid"], task["assigned_by_sid"]}:
            raise ValueError("only the assignee or assigner may update this task")

        now = now_iso()
        event = {
            "event": "ack", "task_id": task_id, "ts": now, "status": status,
            "by_sid": caller_sid, "by_alias": CTX.memberships[room.id],
        }
        if "note" in args:
            event["note"] = str(args["note"])[: storage.MAX_BODY_BYTES]
        room.append_task_event(event)

    # Notify the counterparty (assigner ↔ assignee) so they don't have to
    # poll xtalk_tasks. Broadcast-style: only the other party gets it.
    other_sid = task["assigned_by_sid"] if caller_sid == task["assignee_sid"] else task["assignee_sid"]
    if other_sid and other_sid != caller_sid:
        inbox_event = json.dumps({
            "msg_id": new_msg_id(), "task_id": task_id, "room": room.id,
            "from": caller_sid, "from_alias": CTX.memberships[room.id],
            "kind": "task_update", "status": status, "ts": now,
        }, separators=(",", ":"))
        storage.atomic_append(room.inbox_path(other_sid), inbox_event)

    return {"task_id": task_id, "status": status, "room": room.id}


def handle_stream(args: dict[str, Any]) -> dict[str, Any]:
    """Non-blocking snapshot + delta feed for a room.

    Returns three views useful for building an ambient dashboard:
    - `members`: current membership with mode + last presence timestamp
    - `tasks`: open task ledger snapshot (title/status/assignee)
    - `events`: recent inbox lines since `cursor`

    Callers keep the returned `next_cursor` and pass it back to see only
    what happened between calls. This is the pull-mode complement of
    `xtalk_wait` — no sleep, always returns immediately.
    """
    room = CTX.room(args.get("room"))
    inbox = room.inbox_path(CTX.sid or "")
    include_tasks = bool(args.get("include_tasks", True))
    recent_limit = min(max(int(args.get("recent_limit", 20)), 1), 200)
    cursor = int(args.get("cursor", 0))

    try:
        lines = storage.concurrent_read_text(inbox).splitlines()
    except FileNotFoundError:
        lines = []

    if cursor <= 0:
        selected = lines[-recent_limit:] if lines else []
        base_offset = len(lines) - len(selected)
    elif cursor >= len(lines):
        selected = []
        base_offset = cursor
    else:
        selected = lines[cursor:]
        base_offset = cursor

    events: list[dict[str, Any]] = []
    for line in selected:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    members = [
        {
            "sid": m.get("sid"),
            "alias": m.get("alias"),
            "mode": m.get("mode", "idle"),
            "client": m.get("client"),
            "presence_ts": m.get("presence_ts"),
            "epoch": m.get("epoch"),
        }
        for m in room.current_members()
    ]

    result: dict[str, Any] = {
        "room": room.id,
        "cursor": cursor,
        "next_cursor": len(lines),
        "members": members,
        "events": events,
        "event_count": len(events),
    }
    if include_tasks:
        result["tasks"] = [
            {k: v for k, v in t.items() if k != "history"}
            for t in room.load_tasks()
            if t.get("status") in {"pending", "in_progress", "blocked"}
        ]
    return result


def handle_tasks(args: dict[str, Any]) -> dict[str, Any]:
    room = CTX.room(args.get("room"))
    tasks = room.load_tasks()
    filter_assignee = args.get("assignee")
    if filter_assignee == "me":
        filter_assignee_sid: str | None = CTX.sid
    elif filter_assignee:
        filter_assignee_sid = resolve_alias(room, str(filter_assignee)) or str(filter_assignee)
    else:
        filter_assignee_sid = None
    filter_status = args.get("status")
    if filter_status and filter_status not in TASK_STATUSES:
        raise ValueError(f"status must be one of {TASK_STATUSES}")

    def _keep(task: dict[str, Any]) -> bool:
        if filter_assignee_sid and task.get("assignee_sid") != filter_assignee_sid:
            return False
        if filter_status and task.get("status") != filter_status:
            return False
        return True

    filtered = [
        {k: v for k, v in t.items() if k != "history"}
        for t in tasks if _keep(t)
    ]
    filtered.sort(key=lambda t: t.get("opened_ts", ""), reverse=True)
    return {"room": room.id, "count": len(filtered), "tasks": filtered}


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "xtalk_register": handle_register, "xtalk_discover": handle_discover, "xtalk_listen": handle_listen, "xtalk_ask": handle_ask, "xtalk_read": handle_read, "xtalk_reply": handle_reply, "xtalk_close": handle_close, "xtalk_leave": handle_leave, "xtalk_unregister": handle_unregister,
    "xtalk_room_create": handle_room_create, "xtalk_room_join": handle_room_join, "xtalk_room_list": handle_room_list, "xtalk_room_use": handle_room_use, "xtalk_room_leave": lambda a: _leave_room(a["room"]), "xtalk_wait": handle_wait, "xtalk_status": handle_status, "xtalk_presence": handle_presence,
    "xtalk_thread_list": handle_thread_list, "xtalk_broadcast": handle_broadcast, "xtalk_daemon_control": handle_daemon_control,
    "xtalk_assign": handle_assign, "xtalk_ack": handle_ack, "xtalk_tasks": handle_tasks,
    "xtalk_stream": handle_stream,
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
