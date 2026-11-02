#!/usr/bin/env bash
# Runs every case in fixtures/cases.txt through gate-guard.sh and checks the outcome the case expects.
#
# A case is one line, NAME EXPECTED EVENT, separated by spaces; EVENT is the rest of the line (one hook event,
# normally a JSON object). Blank lines and lines starting with # are skipped. EXPECTED is one of:
#   deny  exit 2, nothing on standard output, and one line on standard error,
#         `gate-guard: refused: <what> — <what to do instead>`;
#   ask   exit 0, nothing on standard error, and one line on standard output: a JSON object whose
#         permissionDecision is "ask" and whose reason starts with `gate-guard: cannot read this command: `;
#   pass  exit 0 and no output at all.
# Placeholders in EVENT are filled in before the run:
#   @FIXTURES@      this fixtures folder (fixture stores live in fixtures/stores/<name>/gates.json);
#   @MAIN_REPO@     a temporary git repository with main checked out;
#   @FEATURE_REPO@  a temporary git repository with the branch feature checked out;
#   @WORK@          the temporary folder; it holds store-link.json, a symbolic link to the approved fixture store,
#                   and link-to-approved, a symbolic link to that store's folder.
# CLAUDE_PROJECT_DIR is empty for every case, so the guard takes the event's cwd as the project root, except for a
# case whose name holds -project-dir-: it runs with CLAUDE_PROJECT_DIR set to fixtures/stores/approved. A case whose
# name ends in -without-python3 runs with a PATH that holds no python3.
#
# Usage, from the repository root: bash .claude/hooks/test-gate-guard.sh, or /bin/bash for macOS bash 3.2 (the
# guard runs under the same bash). Prints `ok NAME` or `FAIL NAME (...)` per case, then `N passed, M failed`, and
# exits 1 on any failure.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
guard="$here/gate-guard.sh"
fixtures="$here/fixtures"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

passed=0
failed=0

# make_repo DIR BRANCH: a git repository with one empty commit and BRANCH checked out.
make_repo() {
  local directory=$1 branch=$2
  git init -q "$directory"
  git -C "$directory" -c user.name=test -c user.email=test@example.invalid -c commit.gpgsign=false \
    commit -q --no-verify --allow-empty -m "initial commit"
  git -C "$directory" branch -q -M main
  if [ "$branch" != main ]; then
    git -C "$directory" checkout -q -b "$branch"
  fi
}

# fill_placeholders TEXT: print TEXT with the placeholders replaced by this run's paths.
fill_placeholders() {
  local text=$1
  text="${text//@FIXTURES@/$fixtures}"
  text="${text//@MAIN_REPO@/$work/main-repo}"
  text="${text//@FEATURE_REPO@/$work/feature-repo}"
  text="${text//@WORK@/$work}"
  printf '%s\n' "$text"
}

# line_count FILE: print the number of lines in FILE.
line_count() {
  wc -l < "$1" | tr -d ' '
}

# outcome_matches EXPECTED STATUS STDOUT_FILE STDERR_FILE: true when the guard's answer is the expected one.
outcome_matches() {
  local expected=$1 status=$2 out=$3 err=$4
  case "$expected" in
    pass)
      [ "$status" -eq 0 ] && [ ! -s "$out" ] && [ ! -s "$err" ]
      ;;
    ask)
      [ "$status" -eq 0 ] && [ ! -s "$err" ] && [ "$(line_count "$out")" -eq 1 ] \
        && grep -q '"permissionDecision": "ask"' "$out" \
        && grep -q '"permissionDecisionReason": "gate-guard: cannot read this command: ' "$out"
      ;;
    deny)
      [ "$status" -eq 2 ] && [ ! -s "$out" ] && [ "$(line_count "$err")" -eq 1 ] \
        && grep -q '^gate-guard: refused: .* — .' "$err"
      ;;
    *)
      return 1
      ;;
  esac
}

# run_case NAME EXPECTED EVENT: run one case through the guard and print its result line.
run_case() {
  local name=$1 expected=$2 event=$3
  local out="$work/stdout" err="$work/stderr" status=0 project_dir="" search_path="$PATH"
  case "$name" in
    *-project-dir-*) project_dir="$fixtures/stores/approved" ;;
  esac
  case "$name" in
    *-without-python3) search_path="$work/empty-path" ;;
  esac
  fill_placeholders "$event" \
    | PATH="$search_path" CLAUDE_PROJECT_DIR="$project_dir" "$BASH" "$guard" > "$out" 2> "$err" || status=$?
  if outcome_matches "$expected" "$status" "$out" "$err"; then
    echo "ok $name"
    passed=$((passed + 1))
  else
    echo "FAIL $name (expected $expected; exit $status; stdout: $(tr '\n' ' ' < "$out"); stderr: $(tr '\n' ' ' < "$err"))"
    failed=$((failed + 1))
  fi
}

make_repo "$work/main-repo" main
make_repo "$work/feature-repo" feature
ln -s "$fixtures/stores/approved/gates.json" "$work/store-link.json"
ln -s "$fixtures/stores/approved" "$work/link-to-approved"

while IFS= read -r line <&3 || [ -n "$line" ]; do
  case "$line" in
    '' | '#'*) continue ;;
  esac
  name="${line%% *}"
  rest="${line#* }"
  expected="${rest%% *}"
  event="${rest#* }"
  run_case "$name" "$expected" "$event"
done 3< "$fixtures/cases.txt"

echo "$passed passed, $failed failed"
[ "$failed" -eq 0 ]
