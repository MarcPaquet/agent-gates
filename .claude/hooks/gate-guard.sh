#!/usr/bin/env bash
# gate-guard: the Claude Code PreToolUse hook that keeps agents inside the gates (wall 1b, see gates/approve.py).
#
# settings.json runs it on every tool call as: bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/gate-guard.sh
# The hook event arrives as JSON on standard input and goes straight on to gate_guard.py, next to this file, which
# holds the rules and prints the outcome (deny: exit 2 and one line on standard error; ask: exit 0 and a JSON
# object on standard output; pass: exit 0 and nothing).
#
# This wrapper adds two things: it denies when python3 is missing, and it turns any exit status of the Python
# program other than 0 or 2 (a crash before the program's own fault handling) into a deny.

set -u

case "${BASH_SOURCE[0]}" in
  */*) here="${BASH_SOURCE[0]%/*}" ;;
  *) here="." ;;
esac

# refuse WHAT INSTEAD: print the one deny line and exit 2.
refuse() {
  printf 'gate-guard: refused: %s — %s\n' "$1" "$2" >&2
  exit 2
}

if ! command -v python3 > /dev/null 2>&1; then
  refuse "python3 is not on PATH" "install Python 3.11 or later; the guard needs it"
fi

status=0
python3 -I "$here/gate_guard.py" || status=$?

case "$status" in
  0 | 2) exit "$status" ;;
  *) refuse "the guard failed (exit status $status)" "run test-gate-guard.sh, next to this script, to find the fault" ;;
esac
