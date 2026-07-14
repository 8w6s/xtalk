#!/usr/bin/env bash
# xtalk installer — sets up venv, wires MCP into supported clients, prints next steps.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -x "$HERE/.venv/bin/python" ]]; then
    echo "==> Creating venv"
    python3 -m venv "$HERE/.venv"
fi
echo "==> Installing xtalk-mcp"
"$HERE/.venv/bin/pip" install -q --upgrade pip
"$HERE/.venv/bin/pip" install -q -e "$HERE"

SERVER_BIN="$HERE/.venv/bin/xtalk-mcp"
XTALK_CLI="$HERE/.venv/bin/xtalk"

echo "==> Copying skill into ~/.claude/skills/xtalk"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_DIR="$CLAUDE_DIR/skills/xtalk"
mkdir -p "$CLAUDE_DIR/skills"
if [[ -e "$SKILL_DIR" ]]; then
    rm -rf "$SKILL_DIR"
fi
cp -r "$HERE/skills/xtalk" "$SKILL_DIR"

echo "==> Copying skill into ~/.codex/skills/xtalk"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_SKILL_DIR="$CODEX_DIR/skills/xtalk"
mkdir -p "$CODEX_DIR/skills"
if [[ -e "$CODEX_SKILL_DIR" ]]; then
    rm -rf "$CODEX_SKILL_DIR"
fi
cp -r "$HERE/skills/xtalk" "$CODEX_SKILL_DIR"

echo "==> Wiring xtalk into MCP clients (dry-run first)"
"$XTALK_CLI" install --server "$SERVER_BIN" --dry-run || true
echo
read -r -p "Apply the changes above to your MCP client configs? [y/N] " ans
case "$ans" in
    y|Y|yes) "$XTALK_CLI" install --server "$SERVER_BIN" ;;
    *) echo "Skipped. Run manually: $XTALK_CLI install --server $SERVER_BIN" ;;
esac

echo
echo "==> Running doctor"
"$XTALK_CLI" doctor || true

cat <<EOF

Next steps:
  - Restart any running Claude Code / Codex / Cursor / Antigravity sessions.
  - In a new session, ask '/mcp' (Claude Code) and confirm the xtalk server lists 19 tools.
  - For remote/multi-machine use: run '$XTALK_CLI relay --host 0.0.0.0 --port 7889'
    on a machine you control, and use --e2ee when creating rooms.
EOF
