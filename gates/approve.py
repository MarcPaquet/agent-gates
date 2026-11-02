"""The human's command: approve a spec, resolve an escalation, close a task, or give the go to merge.

Usage, from the repository root (`--store PATH` comes before the task id):

    python gates/approve.py T-001 ready                  # approve the spec (draft), or resolve an escalation
    python gates/approve.py T-001 cancelled              # resolve an escalation by cancelling the task
    python gates/approve.py T-001 done                   # close a task whose pull request was merged
    python gates/approve.py T-001 --merge                # approve the open pull request for merge at its head
    python gates/approve.py T-001 ready --comment TEXT   # any form takes a comment for the history

Every transition the human may make is recorded with actor `human`; the state machine in `gates/machine.py`
refuses any other move. A merge go records the pull request's head commit, read with `gh`; a later push to the
pull request moves the task back to in_review and clears the go, so the human approves the new head again.

Three walls keep an agent from running this command as the human:

1. The harness's deny rule and hook (the Claude Code adapter in `.claude/`) refuse the command before it runs.
   They live outside this module.
2. The agent-variable check: the command refuses to run when a variable in `AGENT_ENV_VARS` is set, as a
   coding-agent harness does in the shells it runs.
3. The terminal check: the command refuses to run when its standard input is not an interactive terminal, as it
   is not for a command an agent runs through a tool call.

What walls 2 and 3 do not prove: a process that clears the variable and allocates a pseudo-terminal passes
both. Wall 1 is what stops that, and the human's review of what an agent ran is the last.

Exit status: 0 on success, 1 when the request is refused (`refused: <message>` on standard error), 2 on a usage
error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

if __package__ in (None, ""):
    # Run as a script (`python gates/approve.py`): make the `gates` package importable from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gates import machine, store  # noqa: E402

AGENT_ENV_VARS: tuple[str, ...] = ("CLAUDECODE",)
"""Variables a coding-agent harness sets in the shells it runs. Extend this list for each harness you use."""

SHORT_HEAD_LENGTH = 7
"""How many characters of the head commit the merge go prints."""

Runner = Callable[..., subprocess.CompletedProcess]
"""The signature of `subprocess.run`, which tests replace with a fake."""


def require_human(environ: Mapping[str, str], stdin: TextIO | None) -> None:
    """Refuse unless the command looks like it runs in a human's terminal session.

    Refuses with `agent_environment` when a variable in `AGENT_ENV_VARS` is set to a non-empty value, then with
    `not_interactive` when `stdin` is missing or is not an interactive terminal.

    What this does not prove: a process that clears the variable and allocates a pseudo-terminal passes. The
    harness's deny rules and hooks are the other walls; this check only stops the ordinary case of an agent
    running the command through its shell tool.
    """
    for variable in AGENT_ENV_VARS:
        if environ.get(variable):
            raise machine.Refusal("agent_environment", variable=variable)
    if stdin is None or not stdin.isatty():
        raise machine.Refusal("not_interactive")


def pr_head_sha(pr: int, *, runner: Runner = subprocess.run, env: Mapping[str, str] | None = None) -> str:
    """Return the head commit SHA of pull request `pr`, read with `gh pr view`.

    `env` is the environment `gh` runs with (None: this process's environment). Refuses with
    `pr_head_unavailable` when `gh` cannot be run (not installed, not executable, undecodable output) or exits
    with an error, and with `pr_head_invalid` when
    its output is not a 40-character lowercase hexadecimal SHA.
    """
    command = ["gh", "pr", "view", str(pr), "--json", "headRefOid", "--jq", ".headRefOid"]
    process_env = dict(env) if env is not None else None
    try:
        result = runner(command, capture_output=True, text=True, check=False, env=process_env)
    except (OSError, UnicodeDecodeError) as error:
        raise machine.Refusal("pr_head_unavailable", pr=pr, detail=f"gh could not be run ({error})") from error
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"gh exited with status {result.returncode}"
        raise machine.Refusal("pr_head_unavailable", pr=pr, detail=detail)
    value = (result.stdout or "").strip()
    if not machine.is_commit_sha(value):
        raise machine.Refusal("pr_head_invalid", pr=pr, value=value)
    return value


def approve_transition(store_path: Path, item_id: str, target: str, *, comment: str) -> str:
    """Move a task to `target` as the human, under the store lock. Returns the success line."""
    with store.locked(store_path) as data:
        item = data["items"].get(item_id)
        from_state = item["state"] if item is not None else None
        updated = machine.apply_transition(
            data, item_id, target, actor="human", evidence=machine.Evidence(reason=comment), at=store.now_iso()
        )
        data.clear()
        data.update(updated)
    return f"{item_id}: {from_state} -> {target} (human)"


def approve_merge(
    store_path: Path,
    item_id: str,
    *,
    comment: str,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> str:
    """Record the human's go to merge the task's pull request at its current head. Returns the output lines.

    The head is read with `gh` before the store lock is taken, because it is a network call. Under the lock the
    task is read again; unless it is equal to the task read before the `gh` call, the go is refused. Any change
    counts, including a move out of pr_open and back on the same pull request.
    """
    item = _merge_candidate(store.load(store_path), item_id)
    pr = item["pr"]
    head = pr_head_sha(pr, runner=runner, env=env)
    with store.locked(store_path) as data:
        current = data["items"].get(item_id)
        if current != item:
            raise machine.Refusal("merge_state_changed", task=item_id)
        at = store.now_iso()
        updated = machine.record_merge_approval(data, item_id, head=head, at=at)
        if comment.strip():
            event = updated["items"][item_id]["history"][-1]
            event["reason"] = f"{event['reason']}: {comment.strip()}"
        data.clear()
        data.update(updated)
    return "\n".join(
        [
            f"{item_id}: merge approved for pull request #{pr} at head {head[:SHORT_HEAD_LENGTH]} (human, {at})",
            f"An agent may now run: gh pr merge {pr} --squash --match-head-commit {head}",
            "gh refuses this merge if the pull request head has moved since the go.",
            "A push to the pull request after this voids the go: the planner moves the task back to in_review "
            "with the new head, and the human approves again.",
        ]
    )


def _merge_candidate(data: Mapping[str, Any], item_id: str) -> Mapping[str, Any]:
    """Return the task if its pull request may be approved for merge; refuse it as the machine would.

    These are the checks of `machine.check_merge_approval` that do not need the head, which is not known yet.
    """
    if item_id not in data["items"]:
        raise machine.Refusal("unknown_task", task=item_id)
    item = data["items"][item_id]
    if item["state"] != "pr_open":
        raise machine.Refusal("merge_wrong_state", task=item_id, state=item["state"])
    if item["pr"] is None:
        raise machine.Refusal("merge_no_pr", task=item_id)
    return item


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser: a task id, then either a target state or `--merge`."""
    parser = argparse.ArgumentParser(
        prog="gates/approve.py",
        description=(
            "The human's decisions on a task: approve a spec, resolve an escalation, close a task, or approve a "
            "pull request for merge. Refuses to run in an agent session or without an interactive terminal."
        ),
    )
    parser.add_argument(
        "--store", type=Path, default=store.default_store_path(), metavar="PATH",
        help="the store file (default: gates.json at the repository root)",
    )
    parser.add_argument("id", help="the task id, for example T-001")
    decision = parser.add_mutually_exclusive_group(required=True)
    decision.add_argument(
        "target", nargs="?", metavar="STATE",
        help="the state to move the task to: ready, cancelled or done (the state machine refuses the rest)",
    )
    decision.add_argument(
        "--merge", action="store_true", help="approve the task's open pull request for merge at its current head"
    )
    parser.add_argument("--comment", default="", metavar="TEXT", help="a comment recorded in the task's history")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    runner: Runner = subprocess.run,
) -> int:
    """Run one human decision. Returns 0 on success and 1 on a refusal; a usage error exits with status 2.

    `environ` and `stdin` default to this process's environment and standard input; `runner` runs `gh`.
    """
    args = build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    try:
        require_human(environ, stdin)
        if args.merge:
            output = approve_merge(args.store, args.id, comment=args.comment, runner=runner, env=environ)
        else:
            output = approve_transition(args.store, args.id, args.target, comment=args.comment)
    except machine.GateError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
