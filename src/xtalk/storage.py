"""Portable append-only storage primitives for xtalk."""
from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

portalocker: Any = None
_fcntl: Any = None
try:  # Windows and Unix portable lock supplied by the package.
    import portalocker as _portalocker  # type: ignore
    portalocker = _portalocker
    _LOCK_BACKEND = "portalocker"
except ImportError:
    if os.name != "nt":
        import fcntl as _fcntl_mod  # type: ignore
        _fcntl = _fcntl_mod
        _LOCK_BACKEND = "fcntl"
    else:  # pragma: no cover - portalocker is required on Windows
        _LOCK_BACKEND = "none"


def _converge_posix_root(modern: Path, legacy: Path) -> Path:
    """Return one canonical root and migrate a legacy-only install safely.

    Every process returns ``modern``. When only the legacy directory exists,
    a symlink is created once so old data remains available without copying.
    Independent modern and legacy stores are rejected to prevent silent data
    loss.

    Concurrent first-start callers serialize on a lock file in the parent
    directory: the winner does the symlink, the losers wait, then re-check.
    FileExistsError is still handled as a belt-and-braces backstop for
    non-lockable filesystems (some NFS mounts).
    """
    modern.parent.mkdir(parents=True, exist_ok=True)
    lock_path = modern.parent / ".xtalk-migration.lock"

    def _decide() -> Path:
        modern_present = os.path.lexists(modern)
        legacy_present = legacy.exists()

        if modern_present and legacy_present:
            try:
                if modern.samefile(legacy):
                    return modern
            except OSError:
                pass
            raise RuntimeError(
                f"conflicting xtalk stores: {modern} and {legacy}; "
                "merge them or set XTALK_HOME explicitly"
            )

        if legacy_present and not modern_present:
            try:
                modern.symlink_to(legacy, target_is_directory=True)
            except FileExistsError:
                if not modern.samefile(legacy):
                    raise RuntimeError(
                        f"xtalk migration race produced conflicting stores: {modern} and {legacy}"
                    )
        return modern

    # Best-effort file lock. If lock acquisition isn't supported here, fall
    # back to the unlocked path — the FileExistsError branch still keeps
    # simple concurrent creates safe.
    try:
        with lock_path.open("ab") as handle:
            fd = handle.fileno()
            try:
                if os.name != "nt":
                    import fcntl as _fcntl
                    _fcntl.flock(fd, _fcntl.LOCK_EX)
                return _decide()
            finally:
                if os.name != "nt":
                    with contextlib.suppress(OSError):
                        import fcntl as _fcntl
                        _fcntl.flock(fd, _fcntl.LOCK_UN)
    except OSError:
        return _decide()


def _default_root() -> Path:
    configured = os.environ.get("XTALK_HOME")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "xtalk"
    modern = Path.home() / ".xtalk"
    legacy = Path.home() / ".claude" / "xtalk"
    return _converge_posix_root(modern, legacy)


XTALK_ROOT = _default_root()
MAX_BODY_BYTES = 8 * 1024
MAX_READ_COUNT = 100
DEFAULT_READ_COUNT = 20
LEASE_SECONDS = 60
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_local_lock = threading.RLock()


def _ulid() -> str:
    raw = int(time.time() * 1000).to_bytes(6, "big") + secrets.token_bytes(10)
    n = int.from_bytes(raw, "big")
    chars: list[str] = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(chars))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() % 1 * 1000):03d}Z"


def workspace_root(path: str | Path) -> Path:
    p = Path(path).resolve()
    try:
        result = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return p


def workspace_hash(path: str | Path) -> str:
    """Legacy 64-bit workspace fingerprint (kept for read-side compatibility).

    New rooms use `new_room_id()` (128-bit ULID) — see `ensure_project_binding`.
    Existing installs with rooms whose id is a bare 16-hex string will still
    resolve via `_ROOM_ID_RE`, but no new writer produces this format.
    """
    return hashlib.sha1(str(workspace_root(path)).encode()).hexdigest()[:16]


def workspace_project_id(path: str | Path) -> str:
    """Stable 128-bit project id, used as the seed for the default room.

    Independent of the room id so we can rotate rooms without losing the
    project identity for telemetry / migration.
    """
    return "project-" + hashlib.sha256(str(workspace_root(path)).encode()).hexdigest()[:32]


PROJECT_MANIFEST_VERSION = 1


def project_manifest_path(path: str | Path) -> Path:
    """Return the project-local xtalk manifest path for a workspace."""
    return workspace_root(path) / ".xtalk" / "project.json"


def ensure_project_binding(path: str | Path) -> dict[str, Any]:
    """Load or create the durable room binding for a project.

    The manifest is only a discovery pointer. Messages, presence, and other
    runtime data remain under ``XTALK_ROOT``.
    """
    root = workspace_root(path)
    manifest = project_manifest_path(root)
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid xtalk project manifest: {manifest}") from exc
        if data.get("version") != PROJECT_MANIFEST_VERSION:
            raise ValueError(f"unsupported xtalk project manifest version: {data.get('version')!r}")
        room_id = data.get("default_room")
        if not isinstance(room_id, str) or not _ROOM_ID_RE.match(room_id):
            raise ValueError(f"invalid default_room in xtalk project manifest: {room_id!r}")
        return data

    # Legacy layout: an existing 16-hex room directory keyed by workspace_hash
    # gets adopted so upgraders keep their history. New workspaces get a fresh
    # 128-bit ULID room id, sidestepping the 64-bit birthday risk of
    # workspace_hash and matching the format of every other new room.
    legacy_room = workspace_hash(root)
    legacy_room_dir = XTALK_ROOT / "rooms" / legacy_room
    if legacy_room_dir.exists():
        room_id = legacy_room
    else:
        room_id = new_room_id()
    data = {
        "version": PROJECT_MANIFEST_VERSION,
        "project_id": workspace_project_id(root),
        "default_room": room_id,
        "created": now_iso(),
    }
    atomic_json(manifest, data)
    return data


def session_id() -> str:
    return "sid-" + uuid.uuid4().hex[:16]


def new_room_id() -> str:
    return "room-" + _ulid()


def new_thread_id() -> str:
    return "tid-" + _ulid()


def new_msg_id() -> str:
    return "msg-" + _ulid()


def new_task_id() -> str:
    return "task-" + _ulid()


TASK_STATUSES = ("pending", "in_progress", "blocked", "done", "cancelled")
TASK_PRIORITIES = ("low", "normal", "high", "urgent")


def validate_id(value: str, prefixes: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value.startswith(prefixes):
        raise ValueError(f"invalid identifier: {value!r}")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if len(value) > 96 or any(c not in allowed for c in value) or ".." in value:
        raise ValueError(f"invalid identifier: {value!r}")
    return value


@dataclass
class Message:
    msg_id: str
    ts: str
    from_sid: str
    from_alias: str
    to: list[str]
    kind: str
    body: str
    in_reply_to: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        data["from"] = data.pop("from_sid")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "Message":
        data = json.loads(line)
        data["from_sid"] = data.pop("from")
        return cls(**data)


@contextmanager
def _locked_file(path: Path, mode: str) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _local_lock:
        with path.open(mode) as handle:
            fd = handle.fileno()
            if _LOCK_BACKEND == "portalocker":
                portalocker.lock(handle, portalocker.LOCK_EX)
            elif _LOCK_BACKEND == "fcntl":
                _fcntl.flock(fd, _fcntl.LOCK_EX)
            try:
                yield handle
            finally:
                try:
                    if _LOCK_BACKEND == "portalocker":
                        portalocker.unlock(handle)
                    elif _LOCK_BACKEND == "fcntl":
                        _fcntl.flock(fd, _fcntl.LOCK_UN)
                except OSError:
                    pass


def atomic_append(path: Path, line: str) -> None:
    data = (line + "\n").encode("utf-8")
    if len(data) > MAX_BODY_BYTES * 2:
        raise ValueError("message line too large")
    is_new_file = not path.exists()
    with _locked_file(path, "ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    # On ext4/xfs a freshly created file survives crash only after its
    # containing directory has been fsynced too. Skip this on Windows —
    # opening a directory for fsync isn't supported.
    if is_new_file and os.name != "nt":
        with contextlib.suppress(OSError):
            dirfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)


def concurrent_read_text(path: Path, *, encoding: str = "utf-8", attempts: int = 100) -> str:
    """Read through Windows' brief exclusive-write sharing violation."""
    for attempt in range(attempts):
        try:
            return path.read_text(encoding=encoding)
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


def atomic_json(path: Path, data: Any) -> None:
    """Atomic file replace with cross-process serialization.

    A sidecar lock file (`<path>.lock`) is held for the tmp-write + rename so
    concurrent writers on the same path don't clobber each other's payloads.
    On POSIX `replace` is atomic; the lock only orders callers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with _locked_file(lock_path, "ab"):
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


_ROOM_ID_RE = re.compile(r"^(room-[A-Za-z0-9_-]{4,64}|[0-9a-f]{16})$")

# Mention token: `@alias` where alias follows the same character class we
# accept in _join (letters, digits, dot, underscore, dash). The negative
# class in front rejects `email@domain` (word char) AND `@@alias` escape
# (double-@) — both would otherwise slip through and wake the target.
_MENTION_RE = re.compile(r"(?:^|[^A-Za-z0-9_@])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})")


def parse_mentions(body: str) -> list[str]:
    """Return the unique @alias tokens referenced in `body`, in first-seen order.

    Strips fenced code blocks (```...```) and inline code (`...`) so a
    pasted snippet like `@decorator` in Python source doesn't accidentally
    wake a member with that alias. Trailing `.`, `-`, `_` characters are
    not part of the alias.
    """
    if not body:
        return []
    # Drop fenced blocks first, then inline code. Both are best-effort
    # sanitizers — malformed fences fall through to plain-text scanning.
    stripped = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    stripped = re.sub(r"`[^`\n]*`", " ", stripped)
    seen: set[str] = set()
    result: list[str] = []
    for match in _MENTION_RE.finditer(stripped):
        alias = match.group(1).rstrip("._-")
        if not alias or alias in seen:
            continue
        seen.add(alias)
        result.append(alias)
    return result


class Room:
    def __init__(self, room_id: str):
        if not _ROOM_ID_RE.match(room_id) or ".." in room_id:
            raise ValueError(f"invalid room id: {room_id!r}")
        self.id = room_id
        self.hash = room_id  # v0.1 compatibility
        self.root = XTALK_ROOT / "rooms" / room_id
        self.threads_dir = self.root / "threads"
        self.inbox_dir = self.root / "inbox"

    def ensure(self, workspace_path: str = "", **metadata: Any) -> None:
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        meta = self.root / "meta.json"
        if not meta.exists():
            atomic_json(meta, {"room_id": self.id, "workspace_path": workspace_path, "created": now_iso(), **metadata})

    def metadata(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / "meta.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def update_metadata(self, **changes: Any) -> None:
        data = self.metadata()
        if not data:
            raise ValueError(f"room does not exist: {self.id}")
        data.update(changes)
        atomic_json(self.root / "meta.json", data)

    def members_path(self) -> Path:
        return self.root / "members.jsonl"

    def thread_path(self, tid: str) -> Path:
        validate_id(tid, ("tid-",))
        return self.threads_dir / f"{tid}.jsonl"

    def inbox_path(self, sid: str) -> Path:
        validate_id(sid, ("sid-",))
        return self.inbox_dir / f"{sid}.jsonl"

    def append_member_event(self, event: dict[str, Any]) -> None:
        atomic_append(self.members_path(), json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    def join_with_alias_check(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Atomically verify alias uniqueness and append the join event.

        Returns the current members snapshot as it was observed inside the
        lock, so the caller can notify existing peers without a second read
        that could race with heartbeat rotation.

        Raises ValueError if `event["alias"]` is already taken by another sid.
        """
        alias = event["alias"]
        sid = event["sid"]
        path = self.members_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _locked_file(path, "ab") as handle:
            try:
                existing_lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                existing_lines = []
            alive: dict[str, dict[str, Any]] = {}
            for line in existing_lines:
                try:
                    parsed = json.loads(line)
                    other_sid = parsed["sid"]
                except (json.JSONDecodeError, KeyError):
                    continue
                kind = parsed.get("event")
                if kind in {"join", "heartbeat"}:
                    alive[other_sid] = {**alive.get(other_sid, {}), **parsed, "mode": alive.get(other_sid, {}).get("mode", "idle")}
                elif kind == "presence" and other_sid in alive:
                    alive[other_sid].update({k: parsed.get(k) for k in ("epoch", "mode", "target_msg_id", "deadline_ts")})
                    alive[other_sid]["presence_ts"] = parsed.get("ts")
                elif kind == "leave":
                    alive.pop(other_sid, None)
            now = time.time()
            # Alias collision check runs on the full alive set (including
            # zombies whose lease has expired but who never emitted a leave
            # event), so `current_members(include_stale=True)` cannot see two
            # entries with the same alias. The return snapshot still filters
            # to live peers — those are the only ones we notify.
            for m in alive.values():
                if m.get("alias") == alias and m.get("sid") != sid:
                    raise ValueError(f"alias already in use in room: {alias}")
            live_members = [m for m in alive.values() if now - float(m.get("epoch", now)) <= LEASE_SECONDS]
            payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            return live_members

    def current_members(self, include_stale: bool = False) -> list[dict[str, Any]]:
        try:
            lines = concurrent_read_text(self.members_path()).splitlines()
        except FileNotFoundError:
            return []
        alive: dict[str, dict[str, Any]] = {}
        for line in lines:
            try:
                event = json.loads(line)
                sid = event["sid"]
            except (json.JSONDecodeError, KeyError):
                continue
            kind = event.get("event")
            if kind in {"join", "heartbeat"}:
                previous = alive.get(sid, {})
                alive[sid] = {**previous, **event, "mode": previous.get("mode", "idle")}
            elif kind == "presence":
                if sid not in alive:
                    continue  # presence only meaningful for existing members
                alive[sid]["epoch"] = float(event.get("epoch", alive[sid].get("epoch", 0)))
                alive[sid]["mode"] = event.get("mode", "idle")
                alive[sid]["target_msg_id"] = event.get("target_msg_id")
                alive[sid]["deadline_ts"] = event.get("deadline_ts")
                alive[sid]["presence_ts"] = event.get("ts")
            elif kind == "leave":
                alive.pop(sid, None)
        if include_stale:
            return list(alive.values())
        now = time.time()
        return [m for m in alive.values() if now - float(m.get("epoch", now)) <= LEASE_SECONDS]

    def append_presence(self, sid: str, alias: str, mode: str, **extra: Any) -> None:
        """Announce a session's current wait mode.

        `mode` ∈ {"idle", "listening", "waiting_reply"}. Extras go into the
        event verbatim — pass `target_msg_id` / `deadline_ts` when mode is
        `waiting_reply` so the deadlock detector can reason about them.
        """
        event = {
            "event": "presence", "sid": sid, "alias": alias, "mode": mode,
            "epoch": time.time(), "ts": now_iso(), **extra,
        }
        self.append_member_event(event)

    def notify_membership(self, kind: str, sid: str, alias: str, recipients: list[str]) -> dict[str, Any]:
        """Fan out an inbox-only member_joined/member_left event."""
        if kind not in {"member_joined", "member_left"}:
            raise ValueError(f"invalid membership notification kind: {kind}")
        event = {
            "msg_id": new_msg_id(), "room": self.id, "from": sid,
            "from_alias": alias, "kind": kind, "ts": now_iso(),
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        for recipient in recipients:
            if recipient != sid:
                atomic_append(self.inbox_path(recipient), line)
        return event

    def check_deadlock(self, now: float | None = None) -> list[str] | None:
        """Return the sids caught in a mutual-wait deadlock, or None if the room is fine.

        Trigger conditions:
        - two or more members in `waiting_reply` mode,
        - nobody in `listening` mode,
        - every waiter's `deadline_ts` has passed.
        """
        members = self.current_members()
        waiters = [m for m in members if m.get("mode") == "waiting_reply"]
        listeners = [m for m in members if m.get("mode") == "listening"]
        if len(waiters) < 2 or listeners:
            return None
        now = time.time() if now is None else now
        if not all(float(m.get("deadline_ts", now + 1)) <= now for m in waiters):
            return None
        return [m["sid"] for m in waiters]

    def emit_deadlock_hint(self, waiters: list[str]) -> None:
        """Post a `deadlock_hint` message so blocked Monitor/xtalk_wait callers exit.

        Idempotent-ish: if the tail of the system thread already contains an
        unresolved hint for this exact waiter set, skip. Prevents flooding when
        multiple watchdog callbacks fire during the same stuck window.
        """
        system_tid = f"tid-system-{self.id[:16]}"
        # Widen the dedup window: MAX_READ_COUNT covers roughly the last 100
        # system messages, so unrelated activity between two flap cycles
        # won't push the earlier hint out of view and cause a double-emit.
        tail = self.read_thread(system_tid, count=MAX_READ_COUNT) if self.thread_path(system_tid).exists() else []
        waiters_key = ",".join(sorted(waiters))
        now = time.time()
        for msg in tail:
            if msg.kind != "deadlock_hint" or msg.meta.get("waiters_key") != waiters_key:
                continue
            # Only suppress hints emitted within the current grace window; a
            # much older hint for the same waiter set is legitimate to re-emit.
            # `now_iso` writes UTC with a trailing Z; parse as UTC, not local.
            try:
                msg_epoch = calendar.timegm(time.strptime(msg.ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                msg_epoch = 0
            if now - msg_epoch < LEASE_SECONDS:
                return
        hint = Message(
            msg_id=new_msg_id(),
            ts=now_iso(),
            from_sid="sid-system",
            from_alias="system",
            to=list(waiters),
            kind="deadlock_hint",
            body="Mutual-wait deadlock detected; nobody is listening.",
            meta={"reason": "mutual_wait", "waiters": list(waiters), "waiters_key": waiters_key},
        )
        atomic_append(self.thread_path(system_tid), hint.to_json())
        event = json.dumps({
            "msg_id": hint.msg_id, "tid": system_tid, "room": self.id,
            "from": "sid-system", "kind": "deadlock_hint", "ts": hint.ts,
            "waiters": list(waiters),
        }, separators=(",", ":"))
        for sid in waiters:
            atomic_append(self.inbox_path(sid), event)

    def append_message(self, tid: str, msg: Message) -> None:
        if len(msg.body.encode()) > MAX_BODY_BYTES:
            raise ValueError(f"body exceeds {MAX_BODY_BYTES} bytes")
        atomic_append(self.thread_path(tid), msg.to_json())
        recipients = msg.to
        if recipients == ["*"]:
            recipients = [m["sid"] for m in self.current_members() if m["sid"] != msg.from_sid]
        # Include `in_reply_to` in the inbox event so a waiter can match a
        # specific reply without having to re-read the whole thread. The
        # thread lookup fallback is bounded to the last 100 messages, so a
        # long conversation could otherwise push the target reply out of view.
        event_data = {
            "msg_id": msg.msg_id, "tid": tid, "room": self.id,
            "from": msg.from_sid, "kind": msg.kind, "ts": msg.ts,
        }
        if msg.in_reply_to:
            event_data["in_reply_to"] = msg.in_reply_to

        # Parse @alias mentions in the body and fan them out as inbox
        # `mention` events. Mentions are the wake signal: a waiter blocked
        # in xtalk_wait pops out as soon as their alias is called, even if
        # the underlying message was addressed to someone else or was a
        # broadcast to `*`. Encrypted bodies (E2EE rooms) are opaque to us,
        # so mention detection only runs on plaintext.
        mention_targets: list[tuple[str, str]] = []
        if not (msg.meta or {}).get("enc"):
            members = self.current_members()
            alias_to_sid: dict[str, str] = {}
            for m in members:
                alias = m.get("alias")
                sid = m.get("sid")
                if isinstance(alias, str) and isinstance(sid, str) and sid != msg.from_sid:
                    alias_to_sid.setdefault(alias, sid)
            for alias in parse_mentions(msg.body):
                sid = alias_to_sid.get(alias)
                if sid:
                    mention_targets.append((alias, sid))

        recipient_set = set(recipients)
        event = json.dumps(event_data, separators=(",", ":"))
        for sid in recipients:
            atomic_append(self.inbox_path(sid), event)

        # Mentioned members who are ALSO explicit recipients don't need a
        # second inbox line — the base event already carries the message.
        # Instead, mark the underlying event as mentioning them and let
        # xtalk_wait's mention short-circuit still fire. Doing this in a
        # second pass keeps the base event JSON stable for the recipients
        # who aren't mentioned.
        for alias, sid in mention_targets:
            if sid in recipient_set:
                continue
            mention_event = json.dumps({
                "msg_id": new_msg_id(), "tid": tid, "room": self.id,
                "underlying_msg_id": msg.msg_id,
                "from": msg.from_sid, "from_alias": msg.from_alias,
                "kind": "mention", "mentioned_alias": alias,
                "underlying_kind": msg.kind, "ts": msg.ts,
            }, separators=(",", ":"))
            atomic_append(self.inbox_path(sid), mention_event)

    def read_thread(self, tid: str, count: int = DEFAULT_READ_COUNT) -> list[Message]:
        count = max(1, min(int(count), MAX_READ_COUNT))
        try:
            lines = concurrent_read_text(self.thread_path(tid)).splitlines()[-count:]
        except FileNotFoundError:
            return []
        result: list[Message] = []
        for line in lines:
            try:
                result.append(Message.from_json(line))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return result

    # -- Task ledger ------------------------------------------------------

    def tasks_path(self) -> Path:
        return self.root / "tasks.jsonl"

    def task_lock_path(self, task_id: str) -> Path:
        validate_id(task_id, ("task-",))
        return self.root / "tasks" / f"{task_id}.lock"

    def append_task_event(self, event: dict[str, Any]) -> None:
        atomic_append(self.tasks_path(), json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    @contextmanager
    def task_lock(self, task_id: str) -> Iterator[None]:
        """Serialize state transitions on a single task across processes.

        Two agents (typically assignee + assigner) can call xtalk_ack on the
        same task at nearly the same moment; without a per-task lock the
        ledger accepts both events and load_tasks folds the later one over
        the earlier one, so one caller's transition silently wins.
        """
        lock_path = self.task_lock_path(task_id)
        with _locked_file(lock_path, "ab"):
            yield

    def find_task(self, task_id: str) -> dict[str, Any] | None:
        """Return one task's projected state without folding the whole ledger.

        Scans the append-only file linearly; the caller expects one task and
        the ledger is usually much smaller than the room's message volume,
        so keeping the projector shared with `load_tasks` isn't worth the
        cost of building the full snapshot for a single lookup.
        """
        try:
            lines = concurrent_read_text(self.tasks_path()).splitlines()
        except FileNotFoundError:
            return None
        task: dict[str, Any] | None = None
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("task_id") != task_id:
                continue
            kind = event.get("event")
            if kind == "open":
                task = {
                    "task_id": task_id,
                    "title": event.get("title", ""),
                    "description": event.get("description", ""),
                    "assignee_sid": event.get("assignee_sid", ""),
                    "assignee_alias": event.get("assignee_alias", ""),
                    "assigned_by_sid": event.get("assigned_by_sid", ""),
                    "assigned_by_alias": event.get("assigned_by_alias", ""),
                    "priority": event.get("priority", "normal"),
                    "status": "pending",
                    "opened_ts": event.get("ts", ""),
                    "updated_ts": event.get("ts", ""),
                }
            elif kind == "ack" and task is not None:
                task["status"] = event.get("status", task["status"])
                task["updated_ts"] = event.get("ts", task["updated_ts"])
                if "note" in event:
                    task["last_note"] = event["note"]
        return task

    def load_tasks(self) -> list[dict[str, Any]]:
        """Fold the task ledger into a current snapshot.

        The ledger is append-only: `open` events introduce a task,
        `ack` events mutate its state. This projector returns each task's
        latest state without collapsing history — the ledger itself is
        available if a caller needs the audit trail.
        """
        try:
            lines = concurrent_read_text(self.tasks_path()).splitlines()
        except FileNotFoundError:
            return []
        tasks: dict[str, dict[str, Any]] = {}
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            task_id = event.get("task_id")
            if not isinstance(task_id, str):
                continue
            kind = event.get("event")
            if kind == "open":
                tasks[task_id] = {
                    "task_id": task_id,
                    "title": event.get("title", ""),
                    "description": event.get("description", ""),
                    "assignee_sid": event.get("assignee_sid", ""),
                    "assignee_alias": event.get("assignee_alias", ""),
                    "assigned_by_sid": event.get("assigned_by_sid", ""),
                    "assigned_by_alias": event.get("assigned_by_alias", ""),
                    "priority": event.get("priority", "normal"),
                    "status": "pending",
                    "opened_ts": event.get("ts", ""),
                    "updated_ts": event.get("ts", ""),
                    "history": [event],
                }
            elif kind == "ack" and task_id in tasks:
                task = tasks[task_id]
                task["status"] = event.get("status", task["status"])
                task["updated_ts"] = event.get("ts", task["updated_ts"])
                if "note" in event:
                    task["last_note"] = event["note"]
                task["history"].append(event)
        return list(tasks.values())

    def list_threads(self) -> list[dict[str, Any]]:
        if not self.threads_dir.exists():
            return []
        result = []
        for p in self.threads_dir.glob("*.jsonl"):
            if p.name.startswith("tid-system-"):
                continue
            tid = p.stem
            msgs = self.read_thread(tid, count=100)
            if not msgs:
                continue
            last = msgs[-1]
            closed = last.kind == "done"
            summary = last.body if closed else ""
            result.append({
                "thread_id": tid,
                "closed": closed,
                "summary": summary,
                "last_message": {
                    "msg_id": last.msg_id,
                    "ts": last.ts,
                    "from": last.from_sid,
                    "from_alias": last.from_alias,
                    "kind": last.kind,
                }
            })
        result.sort(key=lambda x: x["last_message"]["ts"], reverse=True)
        return result


def session_path(sid: str) -> Path:
    validate_id(sid, ("sid-",))
    return XTALK_ROOT / "sessions" / f"{sid}.json"


def save_session(sid: str, data: dict[str, Any]) -> None:
    atomic_json(session_path(sid), data)


def load_session(sid: str) -> dict[str, Any] | None:
    try:
        return json.loads(session_path(sid).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def list_rooms() -> list[Room]:
    root = XTALK_ROOT / "rooms"
    if not root.exists():
        return []
    return [Room(p.name) for p in root.iterdir() if p.is_dir()]


def resolve_alias(room: Room, alias_or_sid: str) -> str | None:
    if alias_or_sid.startswith("sid-"):
        return alias_or_sid
    matches = [m["sid"] for m in room.current_members() if m.get("alias") == alias_or_sid]
    if len(matches) > 1:
        raise ValueError(f"ambiguous alias: {alias_or_sid}")
    return matches[0] if matches else None
