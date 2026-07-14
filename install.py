#!/usr/bin/env python3
"""Cross-platform xtalk installer for Windows, macOS, and Linux."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def venv_bin(name: str, *, windows: bool | None = None) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    suffix = ".exe" if is_windows else ""
    return VENV / ("Scripts" if is_windows else "bin") / f"{name}{suffix}"


def run(*args: str | Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [str(arg) for arg in args]
    print("+", " ".join(command))
    return subprocess.run(command, cwd=ROOT, check=check, text=True)


def copy_skill(destination: Path) -> None:
    source = ROOT / "skills" / "xtalk"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    print(f"Installed skill: {destination}")


def install(*, apply_config: bool, clients: list[str] | None) -> int:
    if not venv_bin("python").exists():
        print(f"Creating virtual environment: {VENV}")
        run(sys.executable, "-m", "venv", VENV)

    python = venv_bin("python")
    run(python, "-m", "pip", "install", "--upgrade", "pip")
    run(python, "-m", "pip", "install", "-e", ROOT)

    home = Path.home()
    copy_skill(Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "skills" / "xtalk")
    copy_skill(Path(os.environ.get("CODEX_HOME", home / ".codex")) / "skills" / "xtalk")

    xtalk = venv_bin("xtalk")
    server = venv_bin("xtalk-mcp")
    client_args: list[str] = []
    for client in clients or []:
        client_args.extend(["--client", client])

    run(xtalk, "install", "--server", server, *client_args, "--dry-run")
    if not apply_config:
        answer = input("Apply these MCP client configuration changes? [y/N] ").strip().lower()
        apply_config = answer in {"y", "yes"}
    if apply_config:
        run(xtalk, "install", "--server", server, *client_args)
    else:
        print("Skipped MCP config changes. Re-run with --yes to apply them.")

    run(xtalk, "doctor", check=False)
    print("Installation complete. Restart your MCP client to load xtalk.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-y", "--yes", action="store_true", help="Apply MCP config changes without prompting")
    parser.add_argument(
        "--client", action="append",
        choices=["claude-code", "codex", "cursor", "antigravity"],
        help="Configure only this client (repeatable)",
    )
    args = parser.parse_args(argv)
    return install(apply_config=args.yes, clients=args.client)


if __name__ == "__main__":
    raise SystemExit(main())
