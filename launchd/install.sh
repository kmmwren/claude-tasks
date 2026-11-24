#!/usr/bin/env bash
# Renders the maintain LaunchAgent; only the non-print modes write to ~/Library or call launchctl.
# Environment: QUEUE (default $CLAUDE_TASKS_DIR, else ~/tasks, as scripts/_paths.py resolves it), LAUNCHER (default: the queue's [rank] launcher), PYTHON.
set -euo pipefail

LABEL="com.claude-tasks.maintain"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
TEMPLATE="$HERE/$LABEL.plist.template"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/claude-tasks-maintain.log"
DOMAIN="gui/$(id -u)"
USAGE="usage: install.sh [--print | --uninstall | --queue DIR]"

# The system python3 is older than the scripts allow, so launchd gets an absolute path to a newer one.
resolve_python() {
    local candidate
    for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
        [ -n "$candidate" ] || continue
        candidate="$(command -v "$candidate" 2>/dev/null || true)"
        [ -n "$candidate" ] || continue
        if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done
    echo "install.sh: no python >= 3.11 found; set PYTHON to one" >&2
    return 1
}

render() {
    local python queue launcher
    python="$(resolve_python)"
    queue="${QUEUE:-${CLAUDE_TASKS_DIR:-$HOME/tasks}}"
    launcher="${LAUNCHER:-}"
    sed \
        -e "s|@PYTHON@|$python|g" \
        -e "s|@MAINTAIN@|$REPO/scripts/maintain.py|g" \
        -e "s|@QUEUE@|$queue|g" \
        -e "s|@LAUNCHER@|$launcher|g" \
        -e "s|@LOG@|$LOG|g" \
        "$TEMPLATE"
}

install_agent() {
    mkdir -p "$(dirname "$TARGET")" "$(dirname "$LOG")"
    render >"$TARGET"
    plutil -lint "$TARGET" >/dev/null
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$TARGET"
    echo "installed $TARGET (logs: $LOG)"
}

uninstall_agent() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$TARGET"
    echo "removed $TARGET"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --print) render; exit 0 ;;
        --uninstall) uninstall_agent; exit 0 ;;
        --queue) QUEUE="$2"; shift ;;
        *) echo "$USAGE" >&2; exit 2 ;;
    esac
    shift
done
install_agent
