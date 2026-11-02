"""The agents' command line: create, dispatch, note, move and inspect tasks.

Usage, from the repository root (`python gates/cli.py` and `python -m gates.cli` are equivalent; every command
also takes `--store PATH` before the command name):

    python gates/cli.py new --actor planner --title T --contract contracts/T-001.md --owns 'src/**' [--id T-001]
    python gates/cli.py dispatch T-001 --actor planner [--to worker]
    python gates/cli.py note T-001 --actor worker "what was done, what is left"
    python gates/cli.py transition T-001 in_review --actor worker
    python gates/cli.py transition T-001 pr_open --actor reviewer --verdict reviews/T-001-1.md --pr 7
    python gates/cli.py transition T-001 escalated --actor worker --reason R --tried T --options O --recommends X
    python gates/cli.py show T-001
    python gates/cli.py board
    python gates/cli.py history T-001

The project root is the directory that holds the store file (`--store`, default `gates.json` at the repository
root). Contract, progress and verdict paths are relative to it.

This module gathers facts and reports results; it decides nothing. Every rule about which move is allowed lives in
`gates/machine.py`: the command line reads the files the rules need (contract, progress log, verdict), passes them
as an `Evidence`, and prints the machine's refusal when there is one. Writing commands hold the store lock from
reading the store to writing it back. The human's decisions are not here: they are made with `gates/approve.py`.

Exit status: 0 on success, 1 when the request is refused (`refused: <message>` on standard error), 2 on a usage
error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Run as a script (`python gates/cli.py`): make the `gates` package importable from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gates import machine, store  # noqa: E402

CONCERN_STATUSES: tuple[str, ...] = ("addressed", "not applicable", "deferred")
"""Statuses that count as filled in a contract's Concerns table (compared case-insensitively)."""

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""The kit's time format: UTC ISO 8601 to the second."""

_CONCERNS_HEADING = re.compile(r"##\s+Concerns\s*")
_SECTION_END = re.compile(r"#{1,2}\s")
_TABLE_SEPARATOR_CELL = re.compile(r":?-+:?")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_OVERALL_LINE = re.compile(r"Overall:\s*(PASS|FAIL)\s*")
_PROGRESS_HEADING = re.compile(r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) · (\S+)[ \t]*")
_FENCE_MARKERS: tuple[str, ...] = ("```", "~~~")

BOARD_WIDTH = 80
"""The board fits this many columns; longer titles are cut."""


# --- Parsers: pure functions over file text -------------------------------------------------------------------


def lines_outside_fences(text: str) -> list[str]:
    """Return the lines of `text` that are not inside a fenced code block, fence lines excluded.

    A line whose stripped text starts with three backticks or three tildes opens a fence; the next such line
    closes it. An unclosed fence runs to the end of the text. The three parsers read only these lines, so an
    example quoted in a code block is never taken for the real thing.
    """
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip().startswith(_FENCE_MARKERS):
            inside = not inside
        elif not inside:
            kept.append(line)
    return kept


def parse_concerns(text: str) -> tuple[bool, tuple[str, ...]]:
    """Read the Concerns table of a contract; return (table found, names of the concerns with no status).

    The table is the first Markdown table after a heading line `## Concerns`, before the next heading of level
    one or two. Its header row must have a `Concern` and a `Status` column (case-insensitive) and be followed by a
    separator row (`|---|---|`). A row is filled when its Status, stripped and lower-cased, is one of
    CONCERN_STATUSES. An unfilled row is reported by its Concern text, or as `row N` when that cell is empty.
    A table with no data rows is reported as not found. Lines inside fenced code blocks are ignored.
    """
    lines = lines_outside_fences(text)
    heading_index = next(
        (index for index, line in enumerate(lines) if _CONCERNS_HEADING.fullmatch(line.rstrip())), None
    )
    if heading_index is None:
        return False, ()

    table_lines: list[str] = []
    for line in lines[heading_index + 1:]:
        stripped = line.strip()
        if _SECTION_END.match(stripped):
            break
        if stripped.startswith("|"):
            table_lines.append(stripped)
        elif table_lines:
            break
    if len(table_lines) < 3:
        return False, ()

    header = [cell.lower() for cell in _table_cells(table_lines[0])]
    separator = _table_cells(table_lines[1])
    if not all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in separator):
        return False, ()
    if "concern" not in header or "status" not in header:
        return False, ()
    concern_column = header.index("concern")
    status_column = header.index("status")

    unfilled: list[str] = []
    for row_number, row in enumerate(table_lines[2:], start=1):
        cells = _table_cells(row)
        concern = cells[concern_column] if concern_column < len(cells) else ""
        status = cells[status_column] if status_column < len(cells) else ""
        if status.lower() not in CONCERN_STATUSES:
            unfilled.append(concern or f"row {row_number}")
    return True, tuple(unfilled)


def parse_verdict_overall(text: str) -> str | None:
    """Return "PASS" or "FAIL" from the verdict's single `Overall:` line; None when there are zero or several.

    A line counts when it is exactly `Overall:` followed by PASS or FAIL, with optional spaces around the word.
    Lines inside fenced code blocks are ignored.
    """
    found = [match.group(1) for line in lines_outside_fences(text) if (match := _OVERALL_LINE.fullmatch(line))]
    return found[0] if len(found) == 1 else None


def parse_progress_times(text: str) -> list[datetime]:
    """Return the times of the entries of a progress log, in file order, as UTC datetimes.

    An entry starts with a heading line `## <time> · <actor>`, where the time is UTC ISO 8601 to the second
    (`2026-11-02T18:27:37Z`). Headings in any other form, or inside a fenced code block, are not entries.
    """
    times: list[datetime] = []
    for line in lines_outside_fences(text):
        match = _PROGRESS_HEADING.fullmatch(line)
        parsed = _parse_time(match.group(1)) if match else None
        if parsed is not None:
            times.append(parsed)
    return times


def _table_cells(line: str) -> list[str]:
    """Split a Markdown table row into stripped cells, ignoring the outer pipes and escaped pipes (`\\|`)."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [cell.strip() for cell in _UNESCAPED_PIPE.split(inner)]


def _parse_time(value: str) -> datetime | None:
    """Parse a time in ISO_FORMAT as a UTC datetime; None when it is not a valid time in that format."""
    try:
        return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --- Facts gathered from disk and flags -----------------------------------------------------------------------


def project_root(store_path: Path) -> Path:
    """Return the project root: the absolute directory that holds the store file."""
    return Path(os.path.abspath(store_path)).parent


def project_path(root: Path, given: str) -> str:
    """Return `given` as a POSIX path relative to `root`; refuse a path that leads outside the root.

    A relative path is read relative to the root, not to the current directory. `.` and `..` segments are
    resolved without touching the disk; the stored form is that lexical path. The path with symbolic links
    followed must also lie inside the root, so a link cannot lead out of it.
    """
    candidate = Path(given)
    if not candidate.is_absolute():
        candidate = root / candidate
    normalised = Path(os.path.normpath(candidate))
    try:
        relative = normalised.relative_to(root)
    except ValueError:
        raise machine.Refusal("path_outside_root", path=given, root=root) from None
    if not (root / relative).resolve().is_relative_to(root.resolve()):
        raise machine.Refusal("path_outside_root", path=given, root=root)
    return relative.as_posix()


def progress_path(root: Path, item_id: str) -> Path:
    """Return the progress log of a task: `progress/<ID>.md` under the project root."""
    return root / "progress" / f"{item_id}.md"


def gather_evidence(root: Path, item: Mapping[str, Any] | None, flags: argparse.Namespace) -> machine.Evidence:
    """Read the facts the machine's guards need from disk and from the command-line flags.

    Every fact is gathered whatever the target state; the machine decides which ones matter. When the task does
    not exist (`item` is None) only the flags are used, and the machine refuses the unknown task.
    """
    contract_exists = False
    concerns_found = False
    unfilled_concerns: tuple[str, ...] = ()
    progress_after_dispatch = False
    if item is not None:
        contract_file = root / item["contract"]
        contract_exists = contract_file.exists()
        if contract_exists:
            concerns_found, unfilled_concerns = parse_concerns(_read_text(root, contract_file))
        dispatched_at = _parse_time(item["dispatched_at"]) if item["dispatched_at"] else None
        log_file = progress_path(root, item["id"])
        if dispatched_at is not None and log_file.exists():
            entry_times = parse_progress_times(_read_text(root, log_file))
            progress_after_dispatch = any(entry_time >= dispatched_at for entry_time in entry_times)

    verdict_path: str | None = flags.verdict
    verdict_exists = False
    verdict_overall: str | None = None
    if verdict_path is not None:
        verdict_file = root / verdict_path
        verdict_exists = verdict_file.exists()
        if verdict_exists:
            verdict_overall = parse_verdict_overall(_read_text(root, verdict_file))

    return machine.Evidence(
        contract_exists=contract_exists,
        concerns_found=concerns_found,
        unfilled_concerns=unfilled_concerns,
        progress_after_dispatch=progress_after_dispatch,
        verdict_path=verdict_path,
        verdict_exists=verdict_exists,
        verdict_overall=verdict_overall,
        pr=flags.pr,
        head=flags.head,
        reason=flags.reason,
        tried=flags.tried,
        options=flags.options,
        recommends=flags.recommends,
        dispatch_to=flags.dispatch_to,
    )


def _read_text(root: Path, path: Path) -> str:
    """Read a text file as UTF-8, replacing bytes that are not valid UTF-8; refuse a file that cannot be read."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise machine.Refusal(
            "file_unreadable", path=os.path.relpath(path, root).replace(os.sep, "/"),
            detail=error.strerror or str(error),
        ) from None


def _require_agent(actor: str) -> None:
    """Refuse `--actor human`: the command line is for agents, the human uses approve.py."""
    if actor not in machine.AGENT_ACTORS:
        raise machine.Refusal("human_actor_in_cli")


def _find_item(data: Mapping[str, Any], item_id: str) -> dict[str, Any]:
    """Return the task `item_id`, or refuse it as unknown."""
    if item_id not in data["items"]:
        raise machine.Refusal("unknown_task", task=item_id)
    return data["items"][item_id]


# --- Writing commands -----------------------------------------------------------------------------------------


def command_new(args: argparse.Namespace) -> str:
    """Create a task in draft. Planner only. Returns the success line."""
    _require_agent(args.actor)
    if args.actor != "planner":
        raise machine.Refusal("new_planner_only", actor=args.actor)
    contract = project_path(project_root(args.store), args.contract)
    with store.locked(args.store) as data:
        updated = store.new_item(
            data, title=args.title, contract=contract, owns=args.owns, actor=args.actor,
            at=store.now_iso(), item_id=args.id,
        )
        created = [item_id for item_id in updated["items"] if item_id not in data["items"]]
        data.clear()
        data.update(updated)
    return f"{created[0]}: created in draft ({args.actor})"


def command_transition(args: argparse.Namespace) -> str:
    """Move a task to another state, with the evidence read from disk and flags. Returns the success line."""
    _require_agent(args.actor)
    root = project_root(args.store)
    if args.verdict is not None:
        args.verdict = project_path(root, args.verdict)
    with store.locked(args.store) as data:
        item = data["items"].get(args.id)
        from_state = item["state"] if item is not None else None
        evidence = gather_evidence(root, item, args)
        updated = machine.apply_transition(
            data, args.id, args.state, actor=args.actor, evidence=evidence, at=store.now_iso()
        )
        data.clear()
        data.update(updated)
    return f"{args.id}: {from_state} -> {args.state} ({args.actor})"


def command_dispatch(args: argparse.Namespace) -> str:
    """Dispatch a task: the same as `transition ID in_progress`, with `--to` as the owner. Returns the line."""
    args.state = "in_progress"
    return command_transition(args)


def command_note(args: argparse.Namespace) -> str:
    """Append an entry to the task's progress log. The store is locked but its content does not change."""
    _require_agent(args.actor)
    root = project_root(args.store)
    with store.locked(args.store) as data:
        item = _find_item(data, args.id)
        if item["state"] in machine.TERMINAL_STATES:
            raise machine.Refusal("terminal_state", task=args.id, state=item["state"])
        text = args.text.strip()
        if not text:
            raise machine.Refusal("note_empty", task=args.id)
        if any(line.strip().startswith("## ") for line in text.splitlines()):
            raise machine.Refusal("note_heading", task=args.id)
        at = store.now_iso()
        log_file = progress_path(root, args.id)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        is_new = not log_file.exists()
        with open(log_file, "a", encoding="utf-8") as log:
            if is_new:
                log.write(f"# Progress — {args.id}\n\n")
            log.write(f"## {at} · {args.actor}\n\n{text}\n\n")
    return f"{args.id}: note added to {log_file.relative_to(root).as_posix()} ({args.actor})"


# --- Reading commands -----------------------------------------------------------------------------------------


def format_show(item: Mapping[str, Any]) -> str:
    """Return a task's fields as aligned text, ending with the legal next states and who may make each move."""
    dispatched = "-"
    if item["dispatched_to"] is not None:
        dispatched = f"{item['dispatched_to']} at {item['dispatched_at']}"
    merge = "-"
    if item["merge_approved_head"] is not None:
        merge = f"head {item['merge_approved_head']} at {item['merge_approved_at']}"
    rows: list[tuple[str, str]] = [
        ("state", item["state"]),
        ("contract", item["contract"]),
        ("owns", ", ".join(item["owns"]) or "-"),
        ("dispatched to", dispatched),
        ("pr", f"#{item['pr']}" if item["pr"] is not None else "-"),
        ("review cycles", f"{item['review_cycles']} of {machine.REVIEW_CYCLE_LIMIT}"),
        ("verdicts", ", ".join(item["verdicts"]) or "-"),
        ("merge approved", merge),
    ]
    escalation = item["escalation"]
    if escalation is None:
        rows.append(("escalation", "-"))
    else:
        rows.append(("escalation", f"from {escalation['from_state']} by {escalation['by']} at {escalation['at']}"))
        for name in machine.ESCALATION_FIELDS:
            rows.append((f"  {name}", escalation[name]))

    label_width = max(len(label) for label, _ in rows)
    lines = [f"{item['id']}  {item['title']}"]
    lines += [f"  {label.ljust(label_width)}  {text}" for label, text in rows]

    targets = machine.legal_targets(item["state"])
    if not targets:
        lines.append("next states: none, this state is terminal")
    else:
        lines.append("next states:")
        target_width = max(len(target) for target in targets)
        for target in targets:
            actors = ", ".join(machine.allowed_actors(item["state"], target))
            lines.append(f"  {target.ljust(target_width)}  by {actors}")
    return "\n".join(lines)


def format_board(data: Mapping[str, Any], store_path: Path) -> str:
    """Return one aligned row per task (id, state, owner, cycles, pr, title), cut to BOARD_WIDTH columns."""
    items = list(data["items"].values())
    if not items:
        return f"No tasks in {store_path}."
    header = ("ID", "STATE", "OWNER", "CYCLES", "PR", "TITLE")
    rows = [
        (
            item["id"],
            item["state"],
            item["dispatched_to"] or "-",
            str(item["review_cycles"]),
            f"#{item['pr']}" if item["pr"] is not None else "-",
            item["title"],
        )
        for item in items
    ]
    widths = [max(len(row[column]) for row in [header, *rows]) for column in range(len(header) - 1)]
    lines = []
    for row in [header, *rows]:
        line = "  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[-1]
        if len(line) > BOARD_WIDTH:
            line = line[: BOARD_WIDTH - 3] + "..."
        lines.append(line.rstrip())
    return "\n".join(lines)


def format_history(item: Mapping[str, Any]) -> str:
    """Return one aligned line per history event: time, move, actor, reason, and any verdict, pr or head."""
    rows = []
    for event in item["history"]:
        move = f"{event['from'] or 'created'} -> {event['to']}"
        extras = [f"{key} {event[key]}" for key in ("verdict", "pr", "head") if event.get(key) is not None]
        reason = event["reason"] + (f" [{', '.join(extras)}]" if extras else "")
        rows.append((event["at"], move, event["actor"], reason))
    move_width = max(len(row[1]) for row in rows)
    actor_width = max(len(row[2]) for row in rows)
    lines = [f"{item['id']}  {item['title']}"]
    for at, move, actor, reason in rows:
        lines.append(f"  {at}  {move.ljust(move_width)}  {actor.ljust(actor_width)}  {reason}")
    return "\n".join(lines)


def command_show(args: argparse.Namespace) -> str:
    """Print a task's fields and its legal next states."""
    return format_show(_find_item(store.load(args.store), args.id))


def command_board(args: argparse.Namespace) -> str:
    """Print one row per task."""
    return format_board(store.load(args.store), args.store)


def command_history(args: argparse.Namespace) -> str:
    """Print a task's history, one line per event."""
    return format_history(_find_item(store.load(args.store), args.id))


# --- Argument parsing -----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser with its seven commands."""
    parser = argparse.ArgumentParser(
        prog="gates/cli.py",
        description="The agents' commands for the task gates. Human decisions are made with gates/approve.py.",
    )
    parser.add_argument(
        "--store", type=Path, default=store.default_store_path(), metavar="PATH",
        help="the store file; its directory is the project root (default: gates.json at the repository root)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add_actor(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--actor", required=True, choices=machine.ACTORS,
            help="who runs the command; human is refused here (the human uses gates/approve.py)",
        )

    new = commands.add_parser("new", help="create a task in draft (planner only)")
    add_actor(new)
    new.add_argument("--title", required=True, help="what the task delivers")
    new.add_argument("--contract", required=True, help="the contract file, relative to the project root")
    new.add_argument(
        "--owns", required=True, action="append", metavar="GLOB",
        help="a path glob the task owns; repeat for several",
    )
    new.add_argument("--id", help="the task id, for example T-001 (default: the next free id)")
    new.set_defaults(handler=command_new)

    dispatch = commands.add_parser(
        "dispatch",
        help="dispatch a ready task, or retry one after a failed review (planner)",
        description=(
            "Move a task to in_progress: the same as `transition ID in_progress`. On a first dispatch (from ready) "
            "--to sets who owns the work. On a retry (from changes_requested) --to is ignored: the owner stays the "
            "same and only the dispatch time is reset, so the worker must write a new note before review."
        ),
    )
    dispatch.add_argument("id", help="the task id")
    add_actor(dispatch)
    dispatch.add_argument(
        "--to", dest="dispatch_to", default="worker", metavar="NAME", help="who does the work (default: worker)"
    )
    dispatch.add_argument("--reason", default="", help="an optional comment recorded in the history")
    dispatch.set_defaults(
        handler=command_dispatch, verdict=None, pr=None, head=None, tried="", options="", recommends=""
    )

    note = commands.add_parser("note", help="append an entry to the task's progress log")
    note.add_argument("id", help="the task id")
    add_actor(note)
    note.add_argument("text", help="the note: what was done, what is left, what is uncertain")
    note.set_defaults(handler=command_note)

    transition = commands.add_parser(
        "transition",
        help="move a task to another state",
        description="Move a task to another state. The guards read the contract, progress log and verdict on disk.",
    )
    transition.add_argument("id", help="the task id")
    transition.add_argument("state", help="the target state")
    add_actor(transition)
    _add_evidence_flags(transition)
    transition.set_defaults(handler=command_transition, dispatch_to="worker")

    show = commands.add_parser("show", help="print a task and its legal next states")
    show.add_argument("id", help="the task id")
    show.set_defaults(handler=command_show)

    board = commands.add_parser("board", help="print one row per task")
    board.set_defaults(handler=command_board)

    history = commands.add_parser("history", help="print a task's history")
    history.add_argument("id", help="the task id")
    history.set_defaults(handler=command_history)
    return parser


def _add_evidence_flags(command: argparse.ArgumentParser) -> None:
    """Add the flags that carry evidence for a transition's guards."""
    command.add_argument("--verdict", help="the reviewer's verdict file, relative to the project root")
    command.add_argument("--pr", type=int, help="the pull request number (to pr_open)")
    command.add_argument("--head", help="the 40-character SHA of the pull request head (pr_open to in_review)")
    command.add_argument(
        "--reason", default="",
        help="why the escalation; for any other move, an optional comment recorded in the history",
    )
    command.add_argument("--tried", default="", help="escalation: what was tried")
    command.add_argument("--options", default="", help="escalation: the options the human can choose from")
    command.add_argument("--recommends", default="", help="escalation: the option the agent recommends")


def main(argv: list[str] | None = None) -> int:
    """Run one command. Returns 0 on success and 1 on a refusal; a usage error exits with status 2."""
    args = build_parser().parse_args(argv)
    try:
        output = args.handler(args)
    except machine.GateError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
