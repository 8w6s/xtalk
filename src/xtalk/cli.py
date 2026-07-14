"""xtalk CLI: install/doctor/daemon/relay/room."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

from . import crypto, daemon, storage
from .storage import Room, XTALK_ROOT, new_room_id


CLIENT_CONFIGS: dict[str, Path] = {
    "claude-code": Path.home() / ".claude.json",
    "codex": Path.home() / ".codex" / "config.toml",
    "cursor": Path.home() / ".cursor" / "mcp.json",
    "antigravity": Path.home() / ".gemini" / "antigravity-cli" / "mcp_config.json",
}


# ---------- install ----------

def _server_bin() -> str:
    """Best-effort discovery of the installed xtalk-mcp entrypoint."""
    exe = shutil.which("xtalk-mcp")
    if exe:
        return exe
    # Fallback: current venv's bin dir sibling of this executable
    return str(Path(sys.executable).parent / "xtalk-mcp")


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".xtalk-bak")
    backup.write_bytes(path.read_bytes())
    return backup


def _install_claude_code(server: str, *, dry_run: bool) -> str:
    path = CLIENT_CONFIGS["claude-code"]
    settings: dict[str, Any] = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return f"claude-code: {path} is not valid JSON — skipping"
    servers = settings.setdefault("mcpServers", {})
    before = json.dumps(servers.get("xtalk"), sort_keys=True) if "xtalk" in servers else None
    servers["xtalk"] = {"type": "stdio", "command": server, "args": [], "env": {}}
    after = json.dumps(servers["xtalk"], sort_keys=True)
    if before == after:
        return f"claude-code: already configured at {path}"
    if dry_run:
        return f"claude-code: would write xtalk entry to {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"claude-code: wrote {path}"


def _install_codex(server: str, *, dry_run: bool) -> str:
    """Codex config.toml — append [mcp_servers.xtalk] section if missing."""
    path = CLIENT_CONFIGS["codex"]
    snippet = f'\n[mcp_servers.xtalk]\ncommand = "{server}"\n'
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if "[mcp_servers.xtalk]" in text:
            return f"codex: already configured at {path}"
        if dry_run:
            return f"codex: would append to {path}"
        _backup(path)
        path.write_text(text.rstrip() + "\n" + snippet, encoding="utf-8")
        return f"codex: appended to {path}"
    if dry_run:
        return f"codex: would create {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snippet.lstrip(), encoding="utf-8")
    return f"codex: created {path}"


def _install_json_client(name: str, path: Path, server: str, *, dry_run: bool) -> str:
    """Generic MCP JSON config (Cursor, Antigravity, etc.)."""
    config: dict[str, Any] = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return f"{name}: {path} is not valid JSON — skipping"
    servers = config.setdefault("mcpServers", {})
    if servers.get("xtalk", {}).get("command") == server:
        return f"{name}: already configured at {path}"
    servers["xtalk"] = {"command": server}
    if dry_run:
        return f"{name}: would write xtalk entry to {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"{name}: wrote {path}"


def cmd_install(args: argparse.Namespace) -> int:
    server = args.server or _server_bin()
    print(f"xtalk-mcp binary: {server}")
    if not Path(server).exists():
        print(f"warning: {server} does not exist yet — run `pip install xtalk-mcp` first")
    only = set(args.client) if args.client else None

    def _pick(name: str) -> bool:
        return only is None or name in only

    if _pick("claude-code"):
        print(_install_claude_code(server, dry_run=args.dry_run))
    if _pick("codex"):
        print(_install_codex(server, dry_run=args.dry_run))
    for name in ("cursor", "antigravity"):
        if _pick(name):
            print(_install_json_client(name, CLIENT_CONFIGS[name], server, dry_run=args.dry_run))
    return 0


# ---------- doctor ----------

def cmd_doctor(_args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    py_ok = sys.version_info >= (3, 10)
    checks.append(("python>=3.10", py_ok, f"running {sys.version.split()[0]}"))

    try:
        import mcp  # noqa: F401
        checks.append(("mcp installed", True, ""))
    except ImportError:
        checks.append(("mcp installed", False, "pip install mcp"))

    try:
        import portalocker  # noqa: F401
        checks.append(("portalocker installed", True, ""))
    except ImportError:
        checks.append(("portalocker installed", False, "pip install portalocker"))

    try:
        import cryptography  # noqa: F401
        checks.append(("cryptography installed", True, ""))
    except ImportError:
        checks.append(("cryptography installed", False, "pip install cryptography"))

    try:
        import aiohttp  # noqa: F401
        checks.append(("aiohttp installed", True, ""))
    except ImportError:
        checks.append(("aiohttp installed", False, "pip install aiohttp"))

    server = _server_bin()
    checks.append(("xtalk-mcp entrypoint", Path(server).exists(), server))

    root_ok = True
    try:
        XTALK_ROOT.mkdir(parents=True, exist_ok=True)
        (XTALK_ROOT / ".write-check").write_text("ok")
        (XTALK_ROOT / ".write-check").unlink()
    except OSError as exc:
        root_ok = False
        checks.append(("XTALK_HOME writable", False, f"{XTALK_ROOT}: {exc}"))
    if root_ok:
        checks.append(("XTALK_HOME writable", True, str(XTALK_ROOT)))

    running, pid = daemon.is_running()
    checks.append(("daemon (optional)", True, f"pid={pid}" if running else "not running"))

    print(f"{'CHECK':<30} STATUS  DETAIL")
    print("-" * 70)
    all_ok = True
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"{name:<30} {status:<7} {detail}")
    return 0 if all_ok else 1


# ---------- daemon ----------

def cmd_daemon(args: argparse.Namespace) -> int:
    if args.action == "start":
        daemon.start()
        return 0
    if args.action == "stop":
        return 0 if daemon.stop() else 1
    if args.action == "status":
        print(json.dumps(daemon.status(), indent=2))
        return 0
    return 2


# ---------- relay ----------

def cmd_relay(args: argparse.Namespace) -> int:
    from . import relay as relay_mod
    db_path = Path(args.db or XTALK_ROOT / "relay" / "relay.db").expanduser()
    print(f"xtalk relay listening on {args.host}:{args.port} (db={db_path})")
    relay_mod.run(db_path, host=args.host, port=args.port)
    return 0


# ---------- room ----------

def cmd_room(args: argparse.Namespace) -> int:
    if args.action == "create":
        rid = new_room_id()
        secret = secrets.token_urlsafe(32)
        e2ee = bool(args.e2ee)
        if e2ee:
            _, auth_key = crypto.derive_keys(secret)
            verifier = crypto.invite_verifier(auth_key)
        else:
            verifier = hashlib.sha256(secret.encode()).hexdigest()
        room = Room(rid)
        room.ensure(
            "",
            name=args.name,
            transport=args.transport,
            visibility="private",
            e2ee=e2ee,
            ttl_seconds=int(args.ttl),
            invite_verifier=verifier,
        )
        invite = crypto.encode_invite({"room": rid, "transport": args.transport, "e2ee": e2ee}, secret)
        print(json.dumps({"room": rid, "name": args.name, "e2ee": e2ee, "invite": invite}, indent=2))
        return 0
    if args.action == "list":
        rows = []
        for room in storage.list_rooms():
            meta = room.metadata()
            rows.append({"room": room.id, "name": meta.get("name", ""), "transport": meta.get("transport", "local"), "e2ee": meta.get("e2ee", False)})
        print(json.dumps(rows, indent=2))
        return 0
    return 2


# ---------- entry ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xtalk", description="xtalk CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="Wire xtalk-mcp into supported clients")
    p_install.add_argument("--server", help="Path to the xtalk-mcp binary")
    p_install.add_argument("--client", action="append", choices=list(CLIENT_CONFIGS.keys()), help="Only install for these clients (repeatable)")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.set_defaults(func=cmd_install)

    p_doctor = sub.add_parser("doctor", help="Diagnose xtalk installation")
    p_doctor.set_defaults(func=cmd_doctor)

    p_daemon = sub.add_parser("daemon", help="Notification bridge daemon")
    p_daemon.add_argument("action", choices=["start", "stop", "status"])
    p_daemon.set_defaults(func=cmd_daemon)

    p_relay = sub.add_parser("relay", help="Run a self-hosted xtalk relay")
    p_relay.add_argument("--host", default="127.0.0.1")
    p_relay.add_argument("--port", type=int, default=7889)
    p_relay.add_argument("--db")
    p_relay.set_defaults(func=cmd_relay)

    p_room = sub.add_parser("room", help="Room administration")
    room_sub = p_room.add_subparsers(dest="action", required=True)
    p_create = room_sub.add_parser("create", help="Create a room and print invite URI")
    p_create.add_argument("name")
    p_create.add_argument("--e2ee", action="store_true")
    p_create.add_argument("--transport", choices=["local", "relay"], default="local")
    p_create.add_argument("--ttl", type=int, default=86400)
    room_sub.add_parser("list", help="List rooms known to this host")
    p_room.set_defaults(func=cmd_room)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
