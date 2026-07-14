#!/usr/bin/env sh
# Guided macOS/Linux installer. install.py remains the cross-platform core.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO="8w6s/xtalk"

# Agent shells often capture stdout through a pipe even though their UI renders ANSI.
# Keep colors enabled there; honor the standard NO_COLOR opt-out for plain logs.
if [ "${NO_COLOR:-}" = "" ]; then
    ESC=$(printf '\033')
    BOLD="${ESC}[1m"; DIM="${ESC}[2m"; BLUE="${ESC}[36m"; GREEN="${ESC}[32m"; YELLOW="${ESC}[33m"; RED="${ESC}[31m"; RESET="${ESC}[0m"
else
    BOLD=''; DIM=''; BLUE=''; GREEN=''; YELLOW=''; RED=''; RESET=''
fi

line() { printf '%s\n' "+------------------------------------------------------------+"; }
step() { printf '\n%s[%s/%s]%s %s%s%s\n' "$BLUE" "$1" "$2" "$RESET" "$BOLD" "$3" "$RESET"; }
ok() { printf '%s  OK%s  %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '%s  !!%s  %s\n' "$YELLOW" "$RESET" "$1"; }
fail() { printf '%s  ERROR%s  %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }

line
printf '| %-58s |\n' "xtalk setup"
printf '| %-58s |\n' "Cross-agent rooms for Claude, Codex and Antigravity"
line
printf '%sProject:%s %s\n' "$DIM" "$RESET" "$HERE"
printf '%sSource: %s https://github.com/%s\n' "$DIM" "$RESET" "$REPO"

step 1 4 "Environment checks"
command -v python3 >/dev/null 2>&1 || fail "Python 3.10+ is required."
ok "Python: $(python3 --version 2>&1)"
if command -v npx >/dev/null 2>&1; then
    HAVE_NPX=1
    ok "Skills CLI runner: $(command -v npx)"
else
    HAVE_NPX=0
    warn "npx not found; install Node.js, then run the skill command shown below."
fi

step 2 4 "Install xtalk MCP server"
python3 "$HERE/install.py" --skip-skill-copy "$@"
ok "Virtual environment, MCP server, client config and doctor completed."

step 3 4 "Install the xtalk agent skill"
install_skill() {
    # Build the official skills CLI agent list from install.py client names.
    selected=""
    expect_client=0
    for arg do
        if [ "$expect_client" -eq 1 ]; then
            selected="$selected $arg"
            expect_client=0
            continue
        fi
        case "$arg" in
            --client) expect_client=1 ;;
            --client=*) selected="$selected ${arg#--client=}" ;;
        esac
    done

    set -- npx --yes skills add "$REPO" --skill xtalk --global --yes
    for client in $selected; do
        case "$client" in
            antigravity) agent=antigravity-cli ;;
            *) agent=$client ;;
        esac
        set -- "$@" --agent "$agent"
    done
    printf '%s  ->%s %s\n' "$DIM" "$RESET" "$*"
    "$@"
}

if [ "$HAVE_NPX" -eq 1 ]; then
    install_skill "$@"
    ok "Skill installed from github.com/$REPO for the selected/detected agents."
else
    warn "Skill installation skipped. After installing Node.js, run:"
    printf '     npx skills add %s --skill xtalk -g\n' "$REPO"
fi

step 4 4 "Finish"
printf '%sSetup complete.%s Restart each configured agent, then ask it to register with xtalk.\n' "$GREEN$BOLD" "$RESET"
printf '%sThe skill keeps agents registered across timeouts and closed threads.%s\n' "$DIM" "$RESET"
