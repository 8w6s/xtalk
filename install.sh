#!/usr/bin/env sh
# Guided macOS/Linux installer. install.py remains the cross-platform core.
set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO="8w6s/xtalk"

# Agent shells often capture stdout through a pipe even though their UI renders ANSI.
# Keep colors enabled there; honor the standard NO_COLOR opt-out for plain logs.
if [ "${NO_COLOR:-}" = "" ]; then
    ESC=$(printf '\033')
    BOLD="${ESC}[1m"; DIM="${ESC}[2m"; CYAN="${ESC}[36m"; GREEN="${ESC}[32m"; YELLOW="${ESC}[33m"; RED="${ESC}[31m"; MAGENTA="${ESC}[35m"; WHITE="${ESC}[97m"; RESET="${ESC}[0m"
else
    BOLD=''; DIM=''; CYAN=''; GREEN=''; YELLOW=''; RED=''; MAGENTA=''; WHITE=''; RESET=''
fi

line() { printf '%s%s%s\n' "$CYAN" "+------------------------------------------------------------+" "$RESET"; }
step() { printf '\n%s%s[%s/%s]%s %s%s%s\n' "$CYAN" "$BOLD" "$1" "$2" "$RESET" "$WHITE$BOLD" "$3" "$RESET"; }
ok() { printf '%s%s  OK%s  %s\n' "$GREEN" "$BOLD" "$RESET" "$1"; }
warn() { printf '%s%s  WARN%s  %s\n' "$YELLOW" "$BOLD" "$RESET" "$1"; }
fail() { printf '%s%s  ERROR%s  %s\n' "$RED" "$BOLD" "$RESET" "$1" >&2; exit 1; }

line
printf '%s| %s%-58s%s%s |%s\n' "$CYAN" "$WHITE$BOLD" "xtalk setup" "$RESET" "$CYAN" "$RESET"
printf '%s| %s%-58s%s%s |%s\n' "$CYAN" "$DIM" "Cross-agent rooms for Claude, Codex and Antigravity" "$RESET" "$CYAN" "$RESET"
line
printf '%sProject:%s %s%s%s\n' "$MAGENTA" "$RESET" "$BOLD" "$HERE" "$RESET"
printf '%sSource: %s %shttps://github.com/%s%s\n' "$MAGENTA" "$RESET" "$DIM" "$REPO" "$RESET"

step 1 4 "Environment checks"
command -v python3 >/dev/null 2>&1 || fail "Python 3.10+ is required."
ok "Python: $(python3 --version 2>&1)"
if command -v npx >/dev/null 2>&1; then
    ok "Skills CLI runner: $(command -v npx)"
else
    fail "npx not found. Install Node.js before running the full setup."
fi

step 2 4 "Install xtalk MCP server"
python3 "$HERE/install.py" --mcp-only --quiet-pip "$@"
ok "Virtual environment, MCP server, client config and doctor completed."

step 3 4 "Install the xtalk agent skill"
install_skill() {
    printf '%s  RUN%s %sagent skill installer%s\n' "$MAGENTA$BOLD" "$RESET" "$DIM" "$RESET"
    python3 "$HERE/install.py" --skill-only "$@"
}

install_skill "$@"
ok "Skill installed from github.com/$REPO for the selected/detected agents."

step 4 4 "Finish"
printf '%sSetup complete.%s Restart each configured agent, then ask it to register with xtalk.\n' "$GREEN$BOLD" "$RESET"
printf '%sSkill scope:%s Claude Code, Codex, Cursor, Antigravity CLI\n' "$MAGENTA" "$RESET"
printf '%sThe skill keeps agents registered across timeouts and closed threads.%s\n' "$DIM" "$RESET"
