"""Specification of gates/cli.py: the parsers, the agents' commands, their refusals and their output."""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from gates import cli, machine, store

REPO_ROOT = Path(__file__).resolve().parents[2]

FILLED_CONTRACT = """# T-001 contract

## Concerns

| Concern | Status | Note |
|---|---|---|
| Broken links are reported | addressed | covered by tests |
| Rate limits | not applicable | local files only |
"""

UNFILLED_CONTRACT = """# T-001 contract

## Concerns

| Concern | Status | Note |
|---|---|---|
| Broken links are reported | addressed | |
| Rate limits | | |
"""


def utc(text: str) -> datetime:
    """Return the UTC datetime of an ISO 8601 time like `2026-11-02T18:27:37Z`."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class ParseConcernsTest(unittest.TestCase):
    """The Concerns table of a contract."""

    def test_a_table_under_the_concerns_heading_is_found(self) -> None:
        self.assertEqual(cli.parse_concerns(FILLED_CONTRACT), (True, ()))

    def test_a_row_with_a_known_status_is_filled(self) -> None:
        for status in cli.CONCERN_STATUSES:
            text = f"## Concerns\n\n| Concern | Status | Note |\n|---|---|---|\n| Speed | {status} | |\n"
            self.assertEqual(cli.parse_concerns(text), (True, ()), status)

    def test_a_row_with_an_empty_or_unknown_status_is_unfilled(self) -> None:
        text = (
            "## Concerns\n\n| Concern | Status | Note |\n|---|---|---|\n"
            "| Speed | | |\n| Memory | todo | |\n| Logs | deferred | |\n"
        )
        self.assertEqual(cli.parse_concerns(text), (True, ("Speed", "Memory")))

    def test_an_unfilled_row_with_no_concern_text_is_named_by_its_row_number(self) -> None:
        text = "## Concerns\n\n| Concern | Status | Note |\n|---|---|---|\n| Speed | addressed | |\n|  |  | |\n"
        self.assertEqual(cli.parse_concerns(text), (True, ("row 2",)))

    def test_the_status_is_compared_case_insensitively(self) -> None:
        text = "## Concerns\n\n| Concern | Status | Note |\n|---|---|---|\n| Speed | Not Applicable | |\n"
        self.assertEqual(cli.parse_concerns(text), (True, ()))

    def test_no_concerns_heading_means_no_table(self) -> None:
        text = "## Scope\n\n| Concern | Status | Note |\n|---|---|---|\n| Speed | addressed | |\n"
        self.assertEqual(cli.parse_concerns(text), (False, ()))

    def test_a_concerns_heading_without_a_table_means_no_table(self) -> None:
        text = "## Concerns\n\nNone yet.\n\n## Scope\n\n| Concern | Status | Note |\n|---|---|---|\n"
        self.assertEqual(cli.parse_concerns(text), (False, ()))

    def test_a_table_without_concern_and_status_columns_is_not_the_concerns_table(self) -> None:
        text = "## Concerns\n\n| Item | Owner |\n|---|---|\n| Speed | worker |\n"
        self.assertEqual(cli.parse_concerns(text), (False, ()))

    def test_a_table_with_no_data_rows_is_not_found(self) -> None:
        text = "## Concerns\n\n| Concern | Status | Note |\n|---|---|---|\n\n## Scope\n"
        self.assertEqual(cli.parse_concerns(text), (False, ()))

    def test_a_concerns_heading_inside_a_fence_is_not_the_section(self) -> None:
        text = "```\n## Concerns\n\n| Concern | Status | Note |\n|---|---|---|\n| Speed | addressed | |\n```\n"
        self.assertEqual(cli.parse_concerns(text), (False, ()))

    def test_a_comment_line_inside_a_fence_does_not_end_the_concerns_section(self) -> None:
        text = (
            "## Concerns\n\n| Concern | Status | Note |\n|---|---|---|\n| Speed | addressed | |\n"
            "```\n# comment\n```\n| Memory | | |\n"
        )
        self.assertEqual(cli.parse_concerns(text), (True, ("Memory",)))

    def test_only_the_first_table_under_the_heading_is_read(self) -> None:
        text = (
            "## Concerns\n\n| Concern | Status | Note |\n|---|---|---|\n| Speed | addressed | |\n\n"
            "| Concern | Status | Note |\n|---|---|---|\n| Memory | | |\n"
        )
        self.assertEqual(cli.parse_concerns(text), (True, ()))


class ParseVerdictOverallTest(unittest.TestCase):
    """The single Overall line of a verdict."""

    def test_one_overall_line_gives_its_value(self) -> None:
        self.assertEqual(cli.parse_verdict_overall("# Review\n\nOverall: PASS\n"), "PASS")
        self.assertEqual(cli.parse_verdict_overall("Overall:FAIL  \n"), "FAIL")

    def test_no_overall_line_gives_none(self) -> None:
        self.assertIsNone(cli.parse_verdict_overall("# Review\n\nLooks good.\n"))

    def test_two_overall_lines_give_none(self) -> None:
        self.assertIsNone(cli.parse_verdict_overall("Overall: PASS\nOverall: FAIL\n"))
        self.assertIsNone(cli.parse_verdict_overall("Overall: PASS\nOverall: PASS\n"))

    def test_an_overall_line_must_be_the_whole_line_in_capitals(self) -> None:
        self.assertIsNone(cli.parse_verdict_overall("The Overall: PASS\nOverall: pass\nOverall: PASS, mostly\n"))

    def test_an_overall_line_inside_a_fence_does_not_count(self) -> None:
        self.assertIsNone(cli.parse_verdict_overall("```\nOverall: PASS\n```\n"))
        self.assertIsNone(cli.parse_verdict_overall("~~~\nOverall: PASS\n~~~\n"))

    def test_an_overall_value_on_the_next_line_does_not_count(self) -> None:
        self.assertIsNone(cli.parse_verdict_overall("Overall:\nPASS\n"))


class ParseProgressTimesTest(unittest.TestCase):
    """The entry headings of a progress log."""

    def test_entry_headings_give_their_times_in_file_order(self) -> None:
        text = (
            "# Progress — T-001\n\n## 2026-11-02T18:27:37Z · worker\n\nstarted\n\n"
            "## 2026-11-02T19:00:00Z · planner\n\nchecked\n\n"
        )
        self.assertEqual(
            cli.parse_progress_times(text), [utc("2026-11-02T18:27:37Z"), utc("2026-11-02T19:00:00Z")]
        )

    def test_headings_in_another_form_are_not_entries(self) -> None:
        text = (
            "## 2026-11-02 · worker\n\n## 2026-11-02T18:27:37 · worker\n\n### 2026-11-02T18:27:37Z · worker\n\n"
            "## 2026-13-02T18:27:37Z · worker\n\nText ## 2026-11-02T18:27:37Z · worker\n"
        )
        self.assertEqual(cli.parse_progress_times(text), [])

    def test_a_heading_inside_a_fence_is_not_an_entry(self) -> None:
        text = "# Progress\n\n```\n## 2026-11-02T18:27:37Z · worker\n```\n"
        self.assertEqual(cli.parse_progress_times(text), [])

    def test_a_log_without_entries_gives_no_times(self) -> None:
        self.assertEqual(cli.parse_progress_times("# Progress — T-001\n\n"), [])


class CliTestCase(unittest.TestCase):
    """Gives each test a project root in a temporary directory, and runs commands against its store."""

    def setUp(self) -> None:
        """Create the project root and remove it when the test ends."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store_path = self.root / "gates.json"

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        """Run `cli.main` with `--store` set; return the exit status, standard output and standard error."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(["--store", str(self.store_path), *args])
        return status, stdout.getvalue(), stderr.getvalue()

    def assert_ok(self, *args: str) -> str:
        """Assert the command succeeds with no error output; return its standard output."""
        status, out, err = self.run_cli(*args)
        self.assertEqual((status, err), (0, ""), f"{args}: {err}")
        return out

    def assert_refused(self, expected: str, *args: str) -> str:
        """Assert the command is refused (exit 1, nothing on stdout) with `expected` in the message."""
        status, out, err = self.run_cli(*args)
        self.assertEqual(status, 1, f"{args}: {out}")
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("refused: "), err)
        self.assertIn(expected, err)
        return err

    def write(self, relative: str, text: str) -> None:
        """Write a file under the project root, creating its directory."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def set_fields(self, item_id: str, **fields: object) -> None:
        """Change fields of a task directly in the store, as approve.py or a test fixture would."""
        with store.locked(self.store_path) as data:
            data["items"][item_id].update(fields)

    def item(self, item_id: str = "T-001") -> dict:
        """Return a task as it is in the store file."""
        return store.load(self.store_path)["items"][item_id]

    def new_task(self, *, title: str = "Check links", contract: str = "contracts/T-001.md") -> None:
        """Create a task in draft through the command line."""
        self.assert_ok(
            "new", "--actor", "planner", "--title", title, "--contract", contract, "--owns", "example/linkcheck/**"
        )

    def ready_task(self, contract_text: str | None = FILLED_CONTRACT) -> None:
        """Create T-001 and put it in ready (the human's step, done directly); write its contract if given."""
        self.new_task()
        self.set_fields("T-001", state="ready")
        if contract_text is not None:
            self.write("contracts/T-001.md", contract_text)

    def in_review_task(self) -> None:
        """Bring T-001 to in_review through the command line."""
        self.ready_task()
        self.assert_ok("dispatch", "T-001", "--actor", "planner")
        self.assert_ok("note", "T-001", "--actor", "worker", "first pass done")
        self.assert_ok("transition", "T-001", "in_review", "--actor", "worker")


class ActorTest(CliTestCase):
    """Who may run the writing commands."""

    def test_the_human_actor_is_refused_on_every_writing_command(self) -> None:
        self.ready_task()
        expected = machine.REFUSALS["human_actor_in_cli"]
        commands = [
            ["new", "--actor", "human", "--title", "x", "--contract", "c.md", "--owns", "a/**"],
            ["dispatch", "T-001", "--actor", "human"],
            ["note", "T-001", "--actor", "human", "text"],
            ["transition", "T-001", "in_progress", "--actor", "human"],
        ]
        for command in commands:
            self.assert_refused(expected, *command)
        self.assertEqual(self.item()["state"], "ready")

    def test_an_actor_that_is_not_a_role_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["--store", str(self.store_path), "note", "T-001", "--actor", "admin", "text"])
        self.assertEqual(raised.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["--store", str(self.store_path), "dispatch", "T-001", "--actor", "admin"])
        self.assertEqual(raised.exception.code, 2)

    def test_only_the_planner_creates_tasks(self) -> None:
        for actor in ("worker", "reviewer"):
            self.assert_refused(
                machine.REFUSALS["new_planner_only"].format(actor=actor),
                "new", "--actor", actor, "--title", "x", "--contract", "c.md", "--owns", "a/**",
            )
        self.assertFalse(self.store_path.exists())


class NewTest(CliTestCase):
    """Creating a task."""

    def test_new_creates_a_draft_task_and_prints_one_line(self) -> None:
        out = self.assert_ok(
            "new", "--actor", "planner", "--title", "Check links", "--contract", "contracts/T-001.md",
            "--owns", "example/**", "--owns", "docs/**",
        )
        self.assertEqual(out, "T-001: created in draft (planner)\n")
        item = self.item()
        self.assertEqual((item["state"], item["owns"]), ("draft", ["example/**", "docs/**"]))

    def test_new_takes_an_explicit_id(self) -> None:
        out = self.assert_ok(
            "new", "--actor", "planner", "--title", "x", "--contract", "c.md", "--owns", "a/**", "--id", "T-010"
        )
        self.assertEqual(out, "T-010: created in draft (planner)\n")

    def test_new_stores_the_contract_path_relative_to_the_root_in_posix_form(self) -> None:
        self.new_task(contract=str(self.root / "contracts" / "." / "T-001.md"))
        self.assertEqual(self.item()["contract"], "contracts/T-001.md")

    def test_a_store_error_is_reported_as_a_refusal(self) -> None:
        self.new_task()
        self.assert_refused(
            "already exists",
            "new", "--actor", "planner", "--title", "x", "--contract", "c.md", "--owns", "a/**", "--id", "T-001",
        )


class HappyPathTest(CliTestCase):
    """A task from creation to an open pull request, with one failed review on the way."""

    def test_a_task_goes_from_new_to_pr_open_and_history_shows_every_step(self) -> None:
        self.ready_task()
        self.assertEqual(
            self.assert_ok("dispatch", "T-001", "--actor", "planner"), "T-001: ready -> in_progress (planner)\n"
        )
        self.assert_ok("note", "T-001", "--actor", "worker", "first pass done")
        self.assertEqual(
            self.assert_ok("transition", "T-001", "in_review", "--actor", "worker"),
            "T-001: in_progress -> in_review (worker)\n",
        )
        self.write("reviews/T-001-c1.md", "# Review 1\n\nOverall: FAIL\n")
        self.assert_ok(
            "transition", "T-001", "changes_requested", "--actor", "reviewer", "--verdict", "reviews/T-001-c1.md"
        )
        self.assertEqual(
            self.assert_ok("dispatch", "T-001", "--actor", "planner"),
            "T-001: changes_requested -> in_progress (planner)\n",
        )
        self.assert_ok("note", "T-001", "--actor", "worker", "fixed the review findings")
        self.assert_ok("transition", "T-001", "in_review", "--actor", "worker")
        self.write("reviews/T-001-c2.md", "# Review 2\n\nOverall: PASS\n")
        self.assertEqual(
            self.assert_ok(
                "transition", "T-001", "pr_open", "--actor", "reviewer",
                "--verdict", "reviews/T-001-c2.md", "--pr", "7",
            ),
            "T-001: in_review -> pr_open (reviewer)\n",
        )

        item = self.item()
        self.assertEqual(item["state"], "pr_open")
        self.assertEqual(item["pr"], 7)
        self.assertEqual(item["review_cycles"], 1)
        self.assertEqual(item["verdicts"], ["reviews/T-001-c1.md", "reviews/T-001-c2.md"])

        history = self.assert_ok("history", "T-001").splitlines()
        steps = [  # draft -> ready is the human's step, set directly by this test, so it is not in the history
            "created -> draft", "ready -> in_progress", "in_progress -> in_review",
            "in_review -> changes_requested", "changes_requested -> in_progress", "in_progress -> in_review",
            "in_review -> pr_open",
        ]
        self.assertEqual(len(history), 1 + len(steps))
        for line, move in zip(history[1:], steps):
            self.assertIn(move, line)
        self.assertIn("verdict reviews/T-001-c2.md", history[-1])
        self.assertIn("pr 7", history[-1])


class GuardRefusalTest(CliTestCase):
    """Each guard of the machine, reached through the command line."""

    def test_dispatch_is_refused_while_the_contract_file_is_missing(self) -> None:
        self.ready_task(contract_text=None)
        self.assert_refused("its contract file contracts/T-001.md does not exist", "dispatch", "T-001",
                            "--actor", "planner")

    def test_review_is_refused_while_a_concern_has_no_status(self) -> None:
        self.ready_task(contract_text=UNFILLED_CONTRACT)
        self.assert_ok("dispatch", "T-001", "--actor", "planner")
        self.assert_ok("note", "T-001", "--actor", "worker", "done")
        self.assert_refused("these concerns have no status: Rate limits",
                            "transition", "T-001", "in_review", "--actor", "worker")

    def test_review_is_refused_without_a_concerns_table(self) -> None:
        self.ready_task(contract_text="# T-001 contract\n\nNo concerns section.\n")
        self.assert_ok("dispatch", "T-001", "--actor", "planner")
        self.assert_ok("note", "T-001", "--actor", "worker", "done")
        self.assert_refused(
            "has no Concerns table with at least one row",
            "transition", "T-001", "in_review", "--actor", "worker",
        )

    def test_review_is_refused_without_a_note_after_the_dispatch(self) -> None:
        self.ready_task()
        self.assert_ok("dispatch", "T-001", "--actor", "planner")
        self.assert_refused("no progress note was written since the dispatch",
                            "transition", "T-001", "in_review", "--actor", "worker")

    def test_a_note_written_before_the_dispatch_does_not_count(self) -> None:
        self.ready_task()
        self.assert_ok("dispatch", "T-001", "--actor", "planner")
        self.write("progress/T-001.md", "# Progress — T-001\n\n## 2020-01-01T00:00:00Z · worker\n\nold\n\n")
        self.assert_refused("no progress note was written since the dispatch",
                            "transition", "T-001", "in_review", "--actor", "worker")

    def test_a_review_is_refused_without_a_verdict_path(self) -> None:
        self.in_review_task()
        self.assert_refused("Pass --verdict PATH", "transition", "T-001", "changes_requested", "--actor", "reviewer")

    def test_a_review_is_refused_when_the_verdict_file_is_missing(self) -> None:
        self.in_review_task()
        self.assert_refused(
            "the verdict file reviews/T-001-c1.md does not exist",
            "transition", "T-001", "changes_requested", "--actor", "reviewer", "--verdict", "reviews/T-001-c1.md",
        )

    def test_a_review_is_refused_when_the_verdict_says_the_other_overall(self) -> None:
        self.in_review_task()
        self.write("reviews/T-001-c1.md", "Overall: PASS\n")
        self.assert_refused(
            "says `Overall: PASS`, but moving to changes_requested needs `Overall: FAIL`",
            "transition", "T-001", "changes_requested", "--actor", "reviewer", "--verdict", "reviews/T-001-c1.md",
        )

    def test_a_review_is_refused_when_the_verdict_has_no_single_overall_line(self) -> None:
        self.in_review_task()
        self.write("reviews/T-001-c1.md", "Overall: FAIL\nOverall: PASS\n")
        self.assert_refused(
            "has no single `Overall: PASS` or `Overall: FAIL` line",
            "transition", "T-001", "changes_requested", "--actor", "reviewer", "--verdict", "reviews/T-001-c1.md",
        )

    def test_a_review_is_refused_when_the_verdict_was_already_recorded(self) -> None:
        self.in_review_task()
        self.write("reviews/T-001-c1.md", "Overall: FAIL\n")
        self.assert_ok(
            "transition", "T-001", "changes_requested", "--actor", "reviewer", "--verdict", "reviews/T-001-c1.md"
        )
        self.assert_ok("dispatch", "T-001", "--actor", "planner")
        self.assert_ok("note", "T-001", "--actor", "worker", "fixed")
        self.assert_ok("transition", "T-001", "in_review", "--actor", "worker")
        self.assert_refused(
            "was already recorded for an earlier review",
            "transition", "T-001", "changes_requested", "--actor", "reviewer",
            "--verdict", "./reviews/../reviews/T-001-c1.md",
        )

    def test_a_verdict_path_outside_the_project_root_is_refused(self) -> None:
        self.in_review_task()
        self.assert_refused(
            "is outside the project root",
            "transition", "T-001", "changes_requested", "--actor", "reviewer", "--verdict", "../T-001-c1.md",
        )

    def test_a_verdict_path_through_a_symlink_to_outside_the_root_is_refused(self) -> None:
        self.in_review_task()
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "lnk").symlink_to(outside, target_is_directory=True)
            self.assert_refused(
                "is outside the project root",
                "transition", "T-001", "changes_requested", "--actor", "reviewer", "--verdict", "lnk/x.md",
            )

    def test_a_contract_that_is_a_directory_is_refused_as_unreadable(self) -> None:
        self.new_task()
        self.set_fields("T-001", state="ready")
        (self.root / "contracts" / "T-001.md").mkdir(parents=True)
        self.assert_refused(
            "the file contracts/T-001.md cannot be read", "dispatch", "T-001", "--actor", "planner"
        )

    def test_pr_open_is_refused_without_a_pull_request_number(self) -> None:
        self.in_review_task()
        self.write("reviews/T-001-c1.md", "Overall: PASS\n")
        self.assert_refused(
            "moving to pr_open needs the pull request number",
            "transition", "T-001", "pr_open", "--actor", "reviewer", "--verdict", "reviews/T-001-c1.md",
        )

    def test_an_escalation_missing_a_field_is_refused(self) -> None:
        self.ready_task()
        self.assert_refused(
            "Missing: --recommends",
            "transition", "T-001", "escalated", "--actor", "worker",
            "--reason", "blocked", "--tried", "two fixes", "--options", "wait or drop",
        )

    def test_a_complete_escalation_is_recorded(self) -> None:
        self.ready_task()
        self.assert_ok(
            "transition", "T-001", "escalated", "--actor", "worker",
            "--reason", "blocked", "--tried", "two fixes", "--options", "wait or drop", "--recommends", "wait",
        )
        self.assertEqual(self.item()["escalation"]["recommends"], "wait")

    def test_a_task_in_a_terminal_state_is_refused_as_terminal_before_the_actor(self) -> None:
        self.new_task()
        self.set_fields("T-001", state="done")
        self.assert_refused("T-001 is done, a terminal state", "transition", "T-001", "in_progress",
                            "--actor", "worker")

    def test_an_unknown_task_is_refused(self) -> None:
        self.assert_refused("there is no task T-404", "transition", "T-404", "in_review", "--actor", "worker")

    def test_a_refused_transition_leaves_the_store_file_byte_identical(self) -> None:
        self.ready_task(contract_text=None)
        before = self.store_path.read_bytes()
        self.assert_refused("does not exist", "dispatch", "T-001", "--actor", "planner")
        self.assertEqual(self.store_path.read_bytes(), before)


class NoteTest(CliTestCase):
    """The progress log."""

    def test_a_note_is_appended_under_a_heading_with_its_time_and_actor(self) -> None:
        self.ready_task()
        out = self.assert_ok("note", "T-001", "--actor", "worker", "first pass done")
        self.assertEqual(out, "T-001: note added to progress/T-001.md (worker)\n")
        self.assert_ok("note", "T-001", "--actor", "planner", "  looked at it  ")
        text = (self.root / "progress" / "T-001.md").read_text(encoding="utf-8")
        lines = text.split("\n")
        self.assertEqual(lines[:2], ["# Progress — T-001", ""])
        self.assertRegex(lines[2], r"^## \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z · worker$")
        self.assertEqual(lines[3:6], ["", "first pass done", ""])
        self.assertRegex(lines[6], r"^## \S+Z · planner$")
        self.assertEqual(lines[7:], ["", "looked at it", "", ""])
        self.assertEqual(len(cli.parse_progress_times(text)), 2)

    def test_a_note_does_not_change_the_store(self) -> None:
        self.ready_task()
        before = self.store_path.read_bytes()
        self.assert_ok("note", "T-001", "--actor", "worker", "text")
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_a_note_on_an_unknown_task_is_refused(self) -> None:
        self.assert_refused("there is no task T-404", "note", "T-404", "--actor", "worker", "text")
        self.assertFalse((self.root / "progress").exists())

    def test_a_note_on_a_terminal_task_is_refused(self) -> None:
        self.new_task()
        self.set_fields("T-001", state="cancelled")
        self.assert_refused("T-001 is cancelled, a terminal state", "note", "T-001", "--actor", "worker", "text")
        self.assertFalse((self.root / "progress").exists())

    def test_a_note_with_a_line_starting_with_a_level_two_heading_is_refused(self) -> None:
        self.new_task()
        self.assert_refused(
            machine.REFUSALS["note_heading"].format(task="T-001"),
            "note", "T-001", "--actor", "worker", "done\n  ## 2026-11-02T18:27:37Z · worker\nmore",
        )
        self.assertFalse((self.root / "progress").exists())

    def test_an_empty_note_is_refused(self) -> None:
        self.new_task()
        self.assert_refused(machine.REFUSALS["note_empty"].format(task="T-001"),
                            "note", "T-001", "--actor", "worker", "   ")


class ReadingCommandsTest(CliTestCase):
    """show, board and history on a small store."""

    def test_board_with_no_tasks_says_so_in_one_line(self) -> None:
        self.assertEqual(self.assert_ok("board"), f"No tasks in {self.store_path}.\n")

    def test_board_prints_one_aligned_row_per_task_within_80_columns(self) -> None:
        self.in_review_task()
        long_title = "A title long enough to be cut at the edge of an eighty-column terminal window"
        self.new_task(title=long_title, contract="c.md")
        lines = self.assert_ok("board").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].split(), ["ID", "STATE", "OWNER", "CYCLES", "PR", "TITLE"])
        self.assertEqual(lines[1].split()[:5], ["T-001", "in_review", "worker", "0", "-"])
        self.assertEqual(lines[2].split()[:5], ["T-002", "draft", "-", "0", "-"])
        self.assertTrue(lines[2].endswith("..."))
        self.assertTrue(all(len(line) <= 80 for line in lines))
        self.assertEqual(lines[0].index("STATE"), lines[1].index("in_review"))

    def test_show_prints_the_fields_and_the_legal_next_states_with_their_actors(self) -> None:
        self.in_review_task()
        out = self.assert_ok("show", "T-001")
        lines = out.splitlines()
        self.assertEqual(lines[0], "T-001  Check links")
        self.assertIn("in_review", lines[1])
        self.assertIn("contracts/T-001.md", out)
        self.assertIn("example/linkcheck/**", out)
        index = lines.index("next states:")
        self.assertEqual(
            [line.split(None, 1) for line in lines[index + 1:]],
            [
                ["changes_requested", "by reviewer"],
                ["pr_open", "by reviewer"],
                ["escalated", "by planner, worker, reviewer"],
            ],
        )

    def test_show_says_a_terminal_task_has_no_next_state(self) -> None:
        self.new_task()
        self.set_fields("T-001", state="done")
        self.assertTrue(self.assert_ok("show", "T-001").endswith("next states: none, this state is terminal\n"))

    def test_history_prints_one_line_per_event(self) -> None:
        self.ready_task()
        self.assert_ok("dispatch", "T-001", "--actor", "planner", "--to", "worker-2")
        lines = self.assert_ok("history", "T-001").splitlines()
        self.assertEqual(lines[0], "T-001  Check links")
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[1], r"^  \S+Z  created -> draft\s+planner\s+created$")
        self.assertRegex(lines[2], r"^  \S+Z  ready -> in_progress\s+planner\s+dispatched to worker-2$")

    def test_show_and_history_refuse_an_unknown_task(self) -> None:
        self.assert_refused("there is no task T-404", "show", "T-404")
        self.assert_refused("there is no task T-404", "history", "T-404")


class EntryPointTest(unittest.TestCase):
    """The command line runs as a script and as a module."""

    def test_the_script_entry_point_runs(self) -> None:
        for command in ([sys.executable, "gates/cli.py", "--help"], [sys.executable, "-m", "gates.cli", "--help"]):
            result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("approve.py", result.stdout)


if __name__ == "__main__":
    unittest.main()
