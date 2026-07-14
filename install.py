#!/usr/bin/env python3
"""Cross-platform xtalk installer for Windows, macOS, and Linux."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPOSITORY = "8w6s/xtalk"
DEFAULT_CLIENTS = ["claude-code", "codex", "cursor", "antigravity"]
SKILL_CLIENTS = {"antigravity": "antigravity-cli"}


def data_home() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "xtalk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "xtalk"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "xtalk"


RUNTIME = data_home()
VENV = RUNTIME / "venv"


def venv_bin(name: str, *, windows: bool | None = None) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    suffix = ".exe" if is_windows else ""
    return VENV / ("Scripts" if is_windows else "bin") / f"{name}{suffix}"


def run(*args: str | Path, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    command = [str(arg) for arg in args]
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, check=check, text=True, capture_output=capture)


def selected_clients(clients: list[str] | None) -> list[str]:
    return clients or DEFAULT_CLIENTS.copy()


def install_mcp(*, apply_config: bool, clients: list[str] | None, quiet_pip: bool = False) -> None:
    if not venv_bin("python").exists():
        RUNTIME.mkdir(parents=True, exist_ok=True)
        print(f"Creating stable runtime: {VENV}", flush=True)
        run(sys.executable, "-m", "venv", VENV)

    python = venv_bin("python")
    pip_flags = ["--quiet", "--disable-pip-version-check"] if quiet_pip else []
    run(python, "-m", "pip", "install", *pip_flags, "--upgrade", "pip")
    # A regular install copies the package into the stable runtime. It must not
    # retain a dependency on the cloned repository.
    run(python, "-m", "pip", "install", *pip_flags, "--upgrade", ROOT)

    xtalk = venv_bin("xtalk")
    server = venv_bin("xtalk-mcp")
    daemon_pid = Path(os.environ.get("XTALK_HOME", Path.home() / ".xtalk")) / "daemon" / "daemon.pid"
    if daemon_pid.exists():
        print("Restarting existing xtalk daemon with the new runtime...", flush=True)
        run(xtalk, "daemon", "stop", check=False)
        for _ in range(50):
            if not daemon_pid.exists():
                break
            time.sleep(0.1)
        # `subprocess.CREATE_NEW_PROCESS_GROUP` only exists on Windows —
        # referencing it inside the ternary would evaluate both branches on
        # POSIX and raise AttributeError before the daemon can even restart.
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        subprocess.Popen(
            [str(xtalk), "daemon", "start"], cwd=RUNTIME,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt", creationflags=creationflags,
        )
        for _ in range(50):
            if daemon_pid.exists():
                break
            time.sleep(0.1)
    client_args = [item for client in selected_clients(clients) for item in ("--client", client)]
    run(xtalk, "install", "--server", server, *client_args, "--dry-run")
    if not apply_config:
        # Non-interactive shells (CI, docker exec, piped stdin) would raise
        # EOFError on input(); refuse rather than crash so the caller sees a
        # clear message and can rerun with --yes.
        if not sys.stdin.isatty():
            print("Non-interactive shell; skipping config changes. Re-run with --yes to apply.")
            apply_config = False
        else:
            answer = input("Apply these MCP client configuration changes? [y/N] ").strip().lower()
            apply_config = answer in {"y", "yes"}
    if apply_config:
        run(xtalk, "install", "--server", server, *client_args)
    else:
        print("Skipped MCP config changes. Re-run with --yes to apply them.")
    run(xtalk, "doctor")


def install_skills(*, clients: list[str] | None) -> None:
    npx = shutil.which("npx.cmd" if os.name == "nt" else "npx") or shutil.which("npx")
    if not npx:
        raise RuntimeError("npx is required for agent skill installation; install Node.js and retry")
    command = [npx, "--yes", "skills", "add", REPOSITORY, "--skill", "xtalk", "--global", "--yes"]
    for client in selected_clients(clients):
        command.extend(["--agent", SKILL_CLIENTS.get(client, client)])
    result = run(*command, check=False, capture=True)
    output = (result.stdout or "") + (result.stderr or "")
    print(output, end="")
    if result.returncode or "Failed to install" in output:
        raise RuntimeError("agent skill installation failed or was only partially completed")


def install(*, apply_config: bool, clients: list[str] | None, quiet_pip: bool = False,
            mcp: bool = True, skills: bool = True) -> int:
    try:
        if mcp:
            install_mcp(apply_config=apply_config, clients=clients, quiet_pip=quiet_pip)
        if skills:
            install_skills(clients=clients)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    print("Installation complete. Restart configured agents to load xtalk.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-y", "--yes", action="store_true", help="Apply MCP config changes without prompting")
    parser.add_argument("--quiet-pip", action="store_true", help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mcp-only", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--skill-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--client", action="append", choices=DEFAULT_CLIENTS,
        help="Configure only this client (repeatable)",
    )
    args = parser.parse_args(argv)
    return install(
        apply_config=args.yes,
        clients=args.client,
        quiet_pip=args.quiet_pip,
        mcp=not args.skill_only,
        skills=not args.mcp_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
