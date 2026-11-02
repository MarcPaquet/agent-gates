"""The task state machine: states, who may move a task between them, and why a move is refused.

This module is pure. It never reads files, the clock, the environment or other processes, and it never
mutates its inputs: callers read the facts from disk (as an `Evidence`), pass the time as `at`, and receive
a new store back. Every refusal is a `Refusal` whose message comes from the `REFUSALS` table.

A store is a mapping `{"version": 1, "items": {<task id>: <item>}}`; see `gates/store.py` for the item shape.
"""

from __future__ import annotations

import copy
import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

STATES: tuple[str, ...] = (
    "draft",
    "ready",
    "in_progress",
    "in_review",
    "changes_requested",
    "pr_open",
    "escalated",
    "done",
    "cancelled",
)
"""Every state a task can be in."""

TERMINAL_STATES: tuple[str, ...] = ("done", "cancelled")
"""States no transition leaves."""

ESCALATABLE_STATES: tuple[str, ...] = ("ready", "in_progress", "in_review", "changes_requested", "pr_open")
"""States from which an agent may escalate a task to the human."""

DISPATCHED_STATES: tuple[str, ...] = ("in_progress", "in_review", "changes_requested", "pr_open", "escalated")
"""States in which a task holds its `owns` globs: no other task overlapping them may be dispatched."""

ACTORS: tuple[str, ...] = ("planner", "worker", "reviewer", "human")
"""Every actor that can appear in a task's history."""

AGENT_ACTORS: tuple[str, ...] = ("planner", "worker", "reviewer")
"""The actors that are agents. Only `gates/approve.py` acts as the human."""

REVIEW_CYCLE_LIMIT = 2
"""Failed reviews after which a task may not be retried and must be escalated instead."""

TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("draft", "ready"): ("human",),
    ("ready", "in_progress"): ("planner",),
    ("in_progress", "in_review"): ("worker",),
    ("in_review", "changes_requested"): ("reviewer",),
    ("in_review", "pr_open"): ("reviewer",),
    ("changes_requested", "in_progress"): ("planner",),
    ("pr_open", "in_review"): ("planner",),
    ("pr_open", "done"): ("planner", "human"),
    ("ready", "escalated"): AGENT_ACTORS,
    ("in_progress", "escalated"): AGENT_ACTORS,
    ("in_review", "escalated"): AGENT_ACTORS,
    ("changes_requested", "escalated"): AGENT_ACTORS,
    ("pr_open", "escalated"): AGENT_ACTORS,
    ("escalated", "ready"): ("human",),
    ("escalated", "cancelled"): ("human",),
}
"""Every legal transition `(from state, to state)` and the actors allowed to make it."""

ESCALATION_FIELDS: tuple[str, ...] = ("reason", "tried", "options", "recommends")
"""The fields an escalation must fill, in the order they are reported when missing."""

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")

REFUSALS: dict[str, str] = {
    # Check order of a transition: unknown task, unknown state, terminal state, no such transition, actor.
    "unknown_task": (
        "there is no task {task} in the store. Run `python gates/cli.py board` to list the tasks."
    ),
    "unknown_state": "'{state}' is not a state. The states are: {states}.",
    "terminal_state": (
        "{task} is {state}, a terminal state, and no transition leaves it. "
        "If more work is needed, create a new task."
    ),
    "no_such_transition": (
        "{task} cannot go from {from_state} to {to_state}. "
        "The legal next states from {from_state} are: {targets}."
    ),
    "human_only": (
        "{task}: {from_state} -> {to_state} is a human decision and {actor} cannot make it. "
        "The human runs `python gates/approve.py {task} {to_state}` at a terminal."
    ),
    "wrong_actor": (
        "{task}: {from_state} -> {to_state} is made by {allowed}, not by {actor}."
    ),
    # Dispatch: ready -> in_progress, and the retry changes_requested -> in_progress.
    "contract_missing": (
        "{task} cannot be dispatched: its contract file {contract} does not exist. "
        "The planner writes the contract before dispatching."
    ),
    "owns_empty": (
        "{task} cannot be dispatched: it owns no paths. "
        "Give the task its `owns` globs so overlapping work can be detected."
    ),
    "owns_overlap": (
        "{task} cannot be dispatched: its `owns` globs overlap with {others}, still dispatched. "
        "Wait until they are done or cancelled, or narrow the globs so the tasks touch different paths."
    ),
    "review_cycle_limit": (
        "{task} has failed review {cycles} times, the limit is {limit}, so it may not be retried. "
        "The planner escalates it to the human instead: "
        "`python gates/cli.py transition {task} escalated --actor planner "
        "--reason ... --tried ... --options ... --recommends ...`."
    ),
    # Submission for review: in_progress -> in_review.
    "concerns_missing": (
        "{task} cannot go to review: its contract {contract} has no Concerns table with at least one row. "
        "Add a `## Concerns` section with a `Concern | Status | Note` table and one row per concern."
    ),
    "concerns_unfilled": (
        "{task} cannot go to review: these concerns have no status: {concerns}. "
        "Set each Status to addressed, not applicable or deferred."
    ),
    "no_progress_note": (
        "{task} cannot go to review: no progress note was written since the dispatch at {dispatched_at}. "
        "Write one with `python gates/cli.py note {task} --actor worker TEXT`."
    ),
    # Review: in_review -> changes_requested and in_review -> pr_open.
    "verdict_path_missing": (
        "{task}: {to_state} records a review, which needs the reviewer's verdict file. "
        "Pass --verdict PATH."
    ),
    "verdict_file_missing": (
        "{task}: the verdict file {path} does not exist. The reviewer writes it before recording the review."
    ),
    "verdict_overall_missing": (
        "{task}: the verdict file {path} has no single `Overall: PASS` or `Overall: FAIL` line. "
        "Write exactly one."
    ),
    "verdict_overall_mismatch": (
        "{task}: the verdict file {path} says `Overall: {found}`, "
        "but moving to {to_state} needs `Overall: {expected}`."
    ),
    "verdict_reused": (
        "{task}: the verdict file {path} was already recorded for an earlier review. "
        "Each review writes a new verdict file."
    ),
    "pr_missing": (
        "{task}: moving to pr_open needs the pull request number. Pass --pr N with a positive integer."
    ),
    # New commits on an open pull request: pr_open -> in_review, and the merge approval.
    "head_invalid": (
        "{task}: '{head}' is not a commit SHA. "
        "Pass --head with the full 40-character lowercase hexadecimal SHA of the pull request head."
    ),
    # Escalation: any state in ESCALATABLE_STATES -> escalated.
    "escalation_incomplete": (
        "{task}: an escalation needs --reason, --tried, --options and --recommends, "
        "so the human can decide without asking. Missing: {missing}."
    ),
    # Merge approval (gates/approve.py --merge).
    "merge_wrong_state": (
        "{task} is {state}. A merge can only be approved while its pull request is open (pr_open)."
    ),
    "merge_no_pr": (
        "{task} has no pull request number recorded, so there is nothing to approve for merge."
    ),
    # Refusals raised outside this module, kept here so every message lives in one table.
    "human_actor_in_cli": (
        "--actor human is refused: an agent cannot act as the human. "
        "Human decisions are made with `python gates/approve.py` at a terminal."
    ),
    "agent_environment": (
        "approve.py is the human's command and refuses to run in an agent session ({variable} is set). "
        "Run it yourself at a terminal."
    ),
    "not_interactive": (
        "approve.py needs an interactive terminal and its standard input is not one. "
        "Run it yourself at a terminal."
    ),
    "pr_head_unavailable": (
        "could not read the head commit of pull request #{pr} with gh: {detail}"
    ),
    "pr_head_invalid": (
        "gh returned '{value}' as the head of pull request #{pr}, which is not a 40-character commit SHA."
    ),
    "merge_state_changed": (
        "{task} changed while its pull request head was being read (state or pull request number differ). "
        "Run the merge approval again."
    ),
    # Refusals of gates/cli.py that are not transitions.
    "new_planner_only": (
        "only the planner creates tasks, not {actor}. "
        "Ask the planner to run `python gates/cli.py new --actor planner ...`."
    ),
    "path_outside_root": (
        "the path {path} is outside the project root {root}. "
        "Give a path inside the project, relative to the directory that holds the store."
    ),
    "note_empty": (
        "{task}: the note is empty. Write what was done, what is left and what is uncertain."
    ),
    "note_heading": (
        "{task}: the note contains a line starting with '## ', which would read as a new progress entry. "
        "Use '###' or plain text."
    ),
    "file_unreadable": (
        "the file {path} cannot be read ({detail}). Check that it is a readable file inside the project."
    ),
}
"""Every refusal message in the kit: rule name -> message template, filled with `str.format`."""


class GateError(Exception):
    """Base class of every refusal in the kit. `str(error)` is the exact text shown to the user."""


class Refusal(GateError):
    """A refused request. `rule` is the `REFUSALS` key the message was built from."""

    rule: str
    fields: dict[str, Any]

    def __init__(self, rule: str, **fields: Any) -> None:
        """Build the message from `REFUSALS[rule]` filled with `fields`."""
        self.rule = rule
        self.fields = fields
        super().__init__(REFUSALS[rule].format(**fields))


@dataclass(frozen=True)
class Evidence:
    """Facts the caller read from disk or from command-line flags. The machine never reads files itself."""

    contract_exists: bool = False
    concerns_found: bool = False
    """A Concerns table was found in the contract."""
    unfilled_concerns: tuple[str, ...] = ()
    progress_after_dispatch: bool = False
    """The progress log has an entry stamped at or after the task's `dispatched_at`."""
    verdict_path: str | None = None
    verdict_exists: bool = False
    verdict_overall: str | None = None
    """Either "PASS" or "FAIL", or None when the verdict has no single Overall line."""
    pr: int | None = None
    head: str | None = None
    reason: str = ""
    """Why the escalation; for any other transition, an optional comment recorded in the history."""
    tried: str = ""
    options: str = ""
    recommends: str = ""
    dispatch_to: str = "worker"


def legal_targets(state: str) -> tuple[str, ...]:
    """Return the states a task in `state` may move to, in table order (empty for terminal or unknown states)."""
    return tuple(to_state for (from_state, to_state) in TRANSITIONS if from_state == state)


def allowed_actors(from_state: str, to_state: str) -> tuple[str, ...]:
    """Return the actors allowed to move a task from `from_state` to `to_state`, or () when no such transition."""
    return TRANSITIONS.get((from_state, to_state), ())


def is_commit_sha(value: object) -> bool:
    """Return True when `value` is a full commit SHA: exactly 40 lowercase hexadecimal characters."""
    return isinstance(value, str) and _COMMIT_SHA.fullmatch(value) is not None


def globs_overlap(a: str, b: str) -> bool:
    """Return True when some path could be owned by both globs. Conservative: when unsure, return True.

    The rule, applied to the `/`-separated segments of both globs from left to right:

    - Leading `./` and `/`, empty segments and a trailing `/` are ignored.
    - A segment `**` matches any number of segments. When either glob reaches a `**`, the globs overlap.
    - Two literal segments (no `*`, `?` or `[`) must be equal.
    - A literal segment and a wildcard segment must match with `fnmatch` (`*` and `?` stay inside a segment).
    - Two wildcard segments overlap unless their literal starts or literal ends rule it out
      (`*.py` and `*.md` do not overlap). A segment containing `[` is assumed to overlap.
    - Owning a path owns everything below it, so when one glob runs out of segments first, they overlap.

    Examples: `example/linkcheck/**` overlaps `example/**` and `example/linkcheck/cli.py`, not `docs/**`.

    Limit: globs are read as plain, normalised, case-sensitive paths relative to the project root. Brace
    expansion (`{a,b}`) is not supported and `..` is not resolved, so write `owns` globs without them.
    """
    segments_a = _segments(a)
    segments_b = _segments(b)
    for segment_a, segment_b in zip(segments_a, segments_b):
        if segment_a == "**" or segment_b == "**":
            return True
        if not _segments_may_match(segment_a, segment_b):
            return False
    return True


def overlapping_tasks(store: Mapping, item_id: str) -> list[str]:
    """Return the ids of the other dispatched tasks whose `owns` globs overlap those of `item_id`, in store order."""
    items = store["items"]
    if item_id not in items:
        raise Refusal("unknown_task", task=item_id)
    own_globs = items[item_id]["owns"]
    overlapping = []
    for other_id, other in items.items():
        if other_id == item_id or other["state"] not in DISPATCHED_STATES:
            continue
        if any(globs_overlap(mine, theirs) for mine in own_globs for theirs in other["owns"]):
            overlapping.append(other_id)
    return overlapping


def check_transition(store: Mapping, item_id: str, to_state: str, *, actor: str, evidence: Evidence) -> None:
    """Raise `Refusal` unless `actor` may move `item_id` to `to_state` with this `evidence`.

    Checks run in a fixed order so the message is deterministic: unknown task, unknown state, terminal state,
    no such transition, wrong actor, then the guards of the transition in table order.
    """
    items = store["items"]
    if item_id not in items:
        raise Refusal("unknown_task", task=item_id)
    if to_state not in STATES:
        raise Refusal("unknown_state", state=to_state, states=", ".join(STATES))
    item = items[item_id]
    from_state = item["state"]
    if from_state in TERMINAL_STATES:
        raise Refusal("terminal_state", task=item_id, state=from_state)
    actors = allowed_actors(from_state, to_state)
    if not actors:
        targets = legal_targets(from_state)
        raise Refusal(
            "no_such_transition",
            task=item_id,
            from_state=from_state,
            to_state=to_state,
            targets=", ".join(targets) if targets else "none",
        )
    if actor not in actors:
        rule = "human_only" if actors == ("human",) else "wrong_actor"
        raise Refusal(
            rule,
            task=item_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            allowed=" or ".join(actors),
        )
    _check_guards(store, item_id, from_state, to_state, evidence)


def apply_transition(
    store: Mapping, item_id: str, to_state: str, *, actor: str, evidence: Evidence, at: str
) -> dict:
    """Check the transition, then return a new store with the task moved, its side effects and a history event.

    The input store is not changed. `at` is the UTC ISO 8601 time of the transition.
    """
    check_transition(store, item_id, to_state, actor=actor, evidence=evidence)
    new_store = copy.deepcopy(dict(store))
    item = new_store["items"][item_id]
    from_state = item["state"]
    event: dict[str, Any] = {
        "at": at,
        "from": from_state,
        "to": to_state,
        "actor": actor,
        "reason": evidence.reason.strip() or _default_reason(from_state, to_state, evidence),
    }

    if to_state == "in_progress" and from_state == "ready":
        item["dispatched_to"] = evidence.dispatch_to or "worker"
        item["dispatched_at"] = at
    elif to_state == "in_progress" and from_state == "changes_requested":
        item["dispatched_at"] = at
    elif to_state == "changes_requested":
        item["verdicts"].append(evidence.verdict_path)
        item["review_cycles"] += 1
        event["verdict"] = evidence.verdict_path
    elif to_state == "pr_open":
        item["verdicts"].append(evidence.verdict_path)
        item["pr"] = evidence.pr
        event["verdict"] = evidence.verdict_path
        event["pr"] = evidence.pr
    elif to_state == "in_review" and from_state == "pr_open":
        item["merge_approved_head"] = None
        item["merge_approved_at"] = None
        event["head"] = evidence.head
    elif to_state == "escalated":
        item["escalation"] = {
            "reason": evidence.reason.strip(),
            "tried": evidence.tried.strip(),
            "options": evidence.options.strip(),
            "recommends": evidence.recommends.strip(),
            "from_state": from_state,
            "by": actor,
            "at": at,
        }
    elif from_state == "escalated":
        item["escalation"] = None

    item["state"] = to_state
    item["history"].append(event)
    return new_store


def check_merge_approval(store: Mapping, item_id: str, *, head: str) -> None:
    """Raise `Refusal` unless the task's pull request may be approved for merge at commit `head`."""
    items = store["items"]
    if item_id not in items:
        raise Refusal("unknown_task", task=item_id)
    item = items[item_id]
    if item["state"] != "pr_open":
        raise Refusal("merge_wrong_state", task=item_id, state=item["state"])
    if item["pr"] is None:
        raise Refusal("merge_no_pr", task=item_id)
    if not is_commit_sha(head):
        raise Refusal("head_invalid", task=item_id, head=head)


def record_merge_approval(store: Mapping, item_id: str, *, head: str, at: str) -> dict:
    """Check the merge approval, then return a new store recording it. The task stays in pr_open.

    The input store is not changed. Any later move back to in_review (a new push) clears the approval.
    """
    check_merge_approval(store, item_id, head=head)
    new_store = copy.deepcopy(dict(store))
    item = new_store["items"][item_id]
    item["merge_approved_head"] = head
    item["merge_approved_at"] = at
    item["history"].append(
        {
            "at": at,
            "from": "pr_open",
            "to": "pr_open",
            "actor": "human",
            "reason": f"merge approved at head {head}",
            "head": head,
        }
    )
    return new_store


def _check_guards(store: Mapping, item_id: str, from_state: str, to_state: str, evidence: Evidence) -> None:
    """Raise `Refusal` for the first guard of the transition row that fails."""
    item = store["items"][item_id]
    if from_state == "ready" and to_state == "in_progress":
        _check_dispatch(store, item_id, evidence)
    elif from_state == "changes_requested" and to_state == "in_progress":
        if item["review_cycles"] >= REVIEW_CYCLE_LIMIT:
            raise Refusal("review_cycle_limit", task=item_id, cycles=item["review_cycles"], limit=REVIEW_CYCLE_LIMIT)
        _check_dispatch(store, item_id, evidence)
    elif from_state == "in_progress" and to_state == "in_review":
        _check_submission(item, evidence)
    elif from_state == "in_review" and to_state == "changes_requested":
        _check_verdict(item, to_state, evidence, expected="FAIL")
    elif from_state == "in_review" and to_state == "pr_open":
        _check_verdict(item, to_state, evidence, expected="PASS")
        if not _is_pr_number(evidence.pr):
            raise Refusal("pr_missing", task=item_id)
    elif from_state == "pr_open" and to_state == "in_review":
        if not is_commit_sha(evidence.head):
            raise Refusal("head_invalid", task=item_id, head=evidence.head)
    elif to_state == "escalated":
        missing = [name for name in ESCALATION_FIELDS if not getattr(evidence, name).strip()]
        if missing:
            raise Refusal("escalation_incomplete", task=item_id, missing=", ".join(f"--{name}" for name in missing))


def _check_dispatch(store: Mapping, item_id: str, evidence: Evidence) -> None:
    """Guards shared by the first dispatch and the retry: contract file, non-empty `owns`, no overlap."""
    item = store["items"][item_id]
    if not evidence.contract_exists:
        raise Refusal("contract_missing", task=item_id, contract=item["contract"])
    if not item["owns"]:
        raise Refusal("owns_empty", task=item_id)
    others = overlapping_tasks(store, item_id)
    if others:
        raise Refusal("owns_overlap", task=item_id, others=", ".join(others))


def _check_submission(item: Mapping, evidence: Evidence) -> None:
    """Guards of in_progress -> in_review: a filled Concerns table and a progress note since the dispatch."""
    if not evidence.concerns_found:
        raise Refusal("concerns_missing", task=item["id"], contract=item["contract"])
    if evidence.unfilled_concerns:
        raise Refusal("concerns_unfilled", task=item["id"], concerns=", ".join(evidence.unfilled_concerns))
    if not evidence.progress_after_dispatch:
        raise Refusal("no_progress_note", task=item["id"], dispatched_at=item["dispatched_at"])


def _check_verdict(item: Mapping, to_state: str, evidence: Evidence, *, expected: str) -> None:
    """Guards of a review: a verdict path, an existing file, the expected Overall line, a path not used before."""
    path = evidence.verdict_path
    if not path:
        raise Refusal("verdict_path_missing", task=item["id"], to_state=to_state)
    if not evidence.verdict_exists:
        raise Refusal("verdict_file_missing", task=item["id"], path=path)
    if evidence.verdict_overall is None:
        raise Refusal("verdict_overall_missing", task=item["id"], path=path)
    if evidence.verdict_overall != expected:
        raise Refusal(
            "verdict_overall_mismatch",
            task=item["id"],
            path=path,
            found=evidence.verdict_overall,
            expected=expected,
            to_state=to_state,
        )
    if path in item["verdicts"]:
        raise Refusal("verdict_reused", task=item["id"], path=path)


def _is_pr_number(value: object) -> bool:
    """Return True when `value` is a pull request number: an int greater than zero (a bool is not one)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _default_reason(from_state: str, to_state: str, evidence: Evidence) -> str:
    """Return the history reason recorded when the caller gave none."""
    if to_state == "in_progress" and from_state == "ready":
        return f"dispatched to {evidence.dispatch_to or 'worker'}"
    if to_state == "in_progress":
        return "dispatched again after a failed review"
    if to_state == "in_review" and from_state == "pr_open":
        return f"new commits on the pull request, head {evidence.head}"
    if to_state == "in_review":
        return "submitted for review"
    if to_state == "changes_requested":
        return f"review failed: {evidence.verdict_path}"
    if to_state == "pr_open":
        return f"review passed: {evidence.verdict_path}, pull request #{evidence.pr}"
    if to_state == "done":
        return "pull request merged"
    if to_state == "ready" and from_state == "draft":
        return "spec approved"
    if to_state == "ready":
        return "escalation resolved: back to ready"
    if to_state == "cancelled":
        return "escalation resolved: cancelled"
    return f"{from_state} -> {to_state}"


def _segments(glob: str) -> list[str]:
    """Split a glob into its path segments, dropping leading `./` and `/`, empty segments and `.` segments."""
    return [segment for segment in glob.split("/") if segment not in ("", ".")]


def _has_wildcard(segment: str) -> bool:
    """Return True when the segment contains a glob wildcard character."""
    return any(character in segment for character in "*?[")


def _segments_may_match(a: str, b: str) -> bool:
    """Return True when some single path segment could match both glob segments. Conservative."""
    a_wild = _has_wildcard(a)
    b_wild = _has_wildcard(b)
    if not a_wild and not b_wild:
        return a == b
    if not a_wild:
        return fnmatch.fnmatchcase(a, b)
    if not b_wild:
        return fnmatch.fnmatchcase(b, a)
    if "[" in a or "[" in b:
        return True
    prefix_a, suffix_a = _literal_ends(a)
    prefix_b, suffix_b = _literal_ends(b)
    prefixes_agree = prefix_a.startswith(prefix_b) or prefix_b.startswith(prefix_a)
    suffixes_agree = suffix_a.endswith(suffix_b) or suffix_b.endswith(suffix_a)
    return prefixes_agree and suffixes_agree


def _literal_ends(segment: str) -> tuple[str, str]:
    """Return the literal text before the first wildcard and after the last wildcard of a segment."""
    wildcard_positions = [index for index, character in enumerate(segment) if character in "*?"]
    return segment[: wildcard_positions[0]], segment[wildcard_positions[-1] + 1 :]
