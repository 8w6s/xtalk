"""Portable append-only storage primitives for xtalk."""
from __future__ import annotations

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
    loss. FileExistsError makes concurrent first-start migration race-safe.
    """
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
        modern.parent.mkdir(parents=True, exist_ok=True)
        try:
            modern.symlink_to(legacy, target_is_directory=True)
        except FileExistsError:  # another MCP process migrated first
            if not modern.samefile(legacy):
                raise RuntimeError(
                    f"xtalk migration race produced conflicting stores: {modern} and {legacy}"
                )
    return modern


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
    return hashlib.sha1(str(workspace_root(path)).encode()).hexdigest()[:16]


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

    # Keep the legacy deterministic id for seamless adoption by existing
    # workspaces; the manifest makes it stable after the project is moved.
    room_id = workspace_hash(root)
    data = {
        "version": PROJECT_MANIFEST_VERSION,
        "project_id": "project-" + hashlib.sha256(room_id.encode()).hexdigest()[:32],
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
    with _locked_file(path, "ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


_ROOM_ID_RE = re.compile(r"^(room-[A-Za-z0-9_-]{4,64}|[0-9a-f]{16})$")


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

    def current_members(self, include_stale: bool = False) -> list[dict[str, Any]]:
        try:
            lines = self.members_path().read_text(encoding="utf-8").splitlines()
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
        tail = self.read_thread(system_tid, count=5) if self.thread_path(system_tid).exists() else []
        waiters_key = ",".join(sorted(waiters))
        for msg in tail:
            if msg.kind == "deadlock_hint" and msg.meta.get("waiters_key") == waiters_key:
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
        event = json.dumps({"msg_id": msg.msg_id, "tid": tid, "room": self.id, "from": msg.from_sid, "kind": msg.kind, "ts": msg.ts}, separators=(",", ":"))
        for sid in recipients:
            atomic_append(self.inbox_path(sid), event)

    def read_thread(self, tid: str, count: int = DEFAULT_READ_COUNT) -> list[Message]:
        count = max(1, min(int(count), MAX_READ_COUNT))
        try:
            lines = self.thread_path(tid).read_text(encoding="utf-8").splitlines()[-count:]
        except FileNotFoundError:
            return []
        result: list[Message] = []
        for line in lines:
            try:
                result.append(Message.from_json(line))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return result

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
