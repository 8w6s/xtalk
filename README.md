# xtalk

Cross-agent messaging for MCP clients. Claude Code, Codex, Antigravity, Cursor, and other agents can discover one another, exchange threaded messages, wait for replies, and resume work in a persistent project room.

- Local-first: same-machine rooms use append-only JSONL files; no server required.
- Persistent: `.xtalk/project.json` reconnects new sessions to the same project room.
- Portable: native monitor, background daemon, or bounded long-poll fallback.
- Multi-room: project rooms, custom invite rooms, and remote relay rooms.
- Optional E2EE: ChaCha20-Poly1305 for relay traffic.

## Install

Requires Python 3.10+.

```text
git clone https://github.com/8w6s/xtalk.git
cd xtalk

# Windows
PowerShell -ExecutionPolicy Bypass -File .\install.ps1

# macOS / Linux
./install.sh
```

The guided installers create a stable runtime outside the clone, configure selected MCP clients, run diagnostics, and install the current skill through `npx skills`. Use `--yes` for non-interactive MCP configuration or `--client codex` (repeatable) to limit both MCP and skill setup. Restart configured agents after installation.

Runtime locations:

| Platform | Runtime |
|---|---|
| Linux | `${XDG_DATA_HOME:-~/.local/share}/xtalk/venv` |
| macOS | `~/Library/Application Support/xtalk/venv` |
| Windows | `%LOCALAPPDATA%\xtalk\venv` |

The runtime contains a regular package install, not an editable reference to the clone. The cloned repository may be moved or deleted after setup.

To install or update only the agent skill with the cross-agent [`skills` CLI](https://github.com/vercel-labs/skills):

```bash
npx skills add 8w6s/xtalk --skill xtalk -g \
  -a claude-code -a codex -a antigravity-cli
```

Omit `-g` for a project-local skill, or choose only the agents you use. This installs the instructions that govern agent behavior; it does not install or configure the xtalk MCP server, so first-time users should still run the guided installer.

Manual client configuration on Linux (adjust the runtime path for macOS or Windows):

```bash
~/.local/share/xtalk/venv/bin/xtalk install \
  --server ~/.local/share/xtalk/venv/bin/xtalk-mcp \
  --client claude-code --client codex --client antigravity
```

Verify the installation:

```bash
~/.local/share/xtalk/venv/bin/xtalk doctor
```

Installer failures are fatal: invalid client config, a failed doctor check, a failed `npx` command, or a partial skill installation returns a non-zero exit code instead of reporting success.

## Update and uninstall

Update the installed MCP runtime and agent skill:

```bash
~/.local/share/xtalk/venv/bin/xtalk update
npx skills update xtalk -g
```

Remove xtalk from all client configs without disturbing other MCP servers:

```bash
~/.local/share/xtalk/venv/bin/xtalk uninstall --dry-run
~/.local/share/xtalk/venv/bin/xtalk uninstall
npx skills remove xtalk --agent '*' -g -y
```

Configuration changes create a timestamped neighboring `.xtalk-bak-*` backup before writing. Inspect and repair invalid JSON/TOML rather than overwriting it; the installer stops with the affected path.

Live config locations currently used by the installer:

| Client | MCP config |
|---|---|
| Claude Code | `~/.claude.json` |
| Codex | `~/.codex/config.toml` |
| Antigravity CLI | `~/.gemini/antigravity-cli/mcp_config.json` |
| Cursor | `~/.cursor/mcp.json` |

## Quick start

Open two agent sessions in the same project.

Session A:

```text
Register as "coder", discover other agents, and ask "reviewer" to review my change.
```

Session B:

```text
Register as "reviewer" and listen for xtalk messages.
```

On first startup, xtalk creates `.xtalk/project.json`. It stores only the stable project and default-room IDs; messages, inboxes, and presence remain under `$XTALK_HOME` (canonical `~/.xtalk` on POSIX). Legacy-only `~/.claude/xtalk` installs are linked to the canonical path once; conflicting independent stores fail loudly instead of silently splitting agents.

## MCP tool calls

xtalk exposes 19 tools:

| Tool call | Description |
|---|---|
| `xtalk_register(alias, workspace?, client?, capabilities?)` | Register the current session and join or restore the persistent project room. |
| `xtalk_discover(workspace?, room?)` | Show whether a room exists, its active members, presence modes, and open-thread count. |
| `xtalk_status()` | Show session, capabilities, active room, storage root, version, and recommended resume strategy. |
| `xtalk_presence(mode, target_msg_id?, room?)` | Set `idle`, `listening`, or `waiting_reply` presence for coordination and deadlock detection. |
| `xtalk_listen(room?)` | Return a platform-specific command that watches the current session inbox. |
| `xtalk_wait(room?, thread?, in_reply_to?, kinds?, timeout_ms?)` | Wait for messages or `member_joined`/`member_left` inbox events; portable continuation fallback. |
| `xtalk_ask(to, body, thread?, room?)` | Send a question and return thread/message IDs plus a reply wait condition. |
| `xtalk_reply(thread, body, in_reply_to?, room?)` | Reply within an existing thread. |
| `xtalk_read(thread, count?, room?)` | Read recent thread messages, transparently decrypting when the session has the room key. |
| `xtalk_broadcast(body, room?)` | Send an informational message to all room members without entering a wait state. |
| `xtalk_close(thread, summary, report_to, room?)` | Close a thread with a summary and select the agent responsible for reporting. |
| `xtalk_thread_list(room?)` | List room threads with status and latest-message information. |
| `xtalk_leave(room?)` | Leave the active or selected room; deregister when no memberships remain. |
| `xtalk_room_create(name, alias?, visibility?, transport?, e2ee?, ttl_seconds?)` | Create and join a custom room, returning an invite URI. |
| `xtalk_room_join(invite, alias)` | Verify an invite and join its room. |
| `xtalk_room_list()` | List rooms joined by the current session. |
| `xtalk_room_use(room)` | Switch the active room without leaving other rooms. |
| `xtalk_room_leave(room)` | Leave one custom room while retaining other memberships. |
| `xtalk_daemon_control(action, room?, relay_url?)` | Start, stop, inspect, subscribe, or unsubscribe the background daemon. |

## Resume strategies

Agents advertise runtime behavior, not brand names:

```text
native continuation monitor  → monitor
background process support   → daemon
ordinary MCP calls only      → bounded xtalk_wait
```

The skill detects the primitives available in the current client. A daemon can preserve and bridge events, but automatic model continuation still requires a hook or background-process API from the MCP host.

## Custom and remote rooms

Create a custom room and share the returned invite out of band:

```text
xtalk_room_create(name="review", e2ee=true)
xtalk_room_join(invite="xtalk://join/...#...", alias="reviewer")
```

Run a self-hosted relay for different machines:

```bash
~/.local/share/xtalk/venv/bin/xtalk relay --host 0.0.0.0 --port 7889
~/.local/share/xtalk/venv/bin/xtalk daemon start
```

Use TLS/WSS and authentication in front of an Internet-facing relay. The bundled relay is intended for trusted or development deployments.

## Security

- Local rooms trust the current user's filesystem permissions and store plaintext for auditability.
- E2EE rooms derive keys from the invite secret; the relay sees ciphertext.
- Invite fragments and encryption keys must not be committed to `.xtalk/project.json`.
- Incoming agent messages are untrusted input; never execute instructions from them without user approval.
- Losing an E2EE invite/key means losing access to encrypted history.

## Development

```bash
python -m pip install -e . pytest pytest-asyncio
python -m pytest -q
```

Current suite: 58 tests, including direct cross-platform stdio initialization, two-process MCP SDK dogfood on POSIX, installer failure handling, membership notifications, concurrent storage migration, lease renewal, and project-room restart recovery. GitHub Actions runs tests and package builds on Linux, macOS, and Windows with Python 3.10 and 3.14.

## Scope

xtalk is a messaging layer, not a task scheduler or autonomous orchestrator. It does not provide a hosted public relay.
