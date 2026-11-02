"""Specification of gates/approve.py: the human checks, the human's transitions and the merge go."""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from gates import approve, cli, machine, store

REPO_ROOT = Path(__file__).resolve().parents[2]
HEAD = "0123456789abcdef0123456789abcdef01234567"
NEW_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
HUMAN_ENVIRON: dict[str, str] = {}


class FakeStdin:
    """A standard input whose `isatty()` answers what the test says."""

    def __init__(self, interactive: bool) -> None:
        """Remember whether this input is an interactive terminal."""
        self.interactive = interactive

    def isatty(self) -> bool:
        """Return whether this input is an interactive terminal."""
        return self.interactive


class FakeGh:
    """A stand-in for `subprocess.run` that records each call and answers like `gh pr view` would."""

    def __init__(self, stdout: str = HEAD + "\n", returncode: int = 0, stderr: str = "") -> None:
        """Set the answer every call returns."""
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **options: Any) -> subprocess.CompletedProcess:
        """Record the command and return the set answer."""
        self.calls.append(command)
        return subprocess.CompletedProcess(command, self.returncode, stdout=self.stdout, stderr=self.stderr)


class ApproveTestCase(unittest.TestCase):
    """Gives each test a store in a temporary directory, and runs approve.py against it as the human."""

    def setUp(self) -> None:
        """Create the store directory and remove it when the test ends."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store_path = self.root / "gates.json"

    def run_approve(
        self,
        *args: str,
        environ: dict[str, str] | None = None,
        interactive: bool = True,
        runner: Any = None,
    ) -> tuple[int, str, str]:
        """Run `approve.main` with `--store` set; return the exit status, standard output and standard error."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = approve.main(
                ["--store", str(self.store_path), *args],
                environ=HUMAN_ENVIRON if environ is None else environ,
                stdin=FakeStdin(interactive),
                runner=FakeGh() if runner is None else runner,
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def assert_ok(self, *args: str, **options: Any) -> str:
        """Assert the command succeeds with no error output; return its standard output."""
        status, out, err = self.run_approve(*args, **options)
        self.assertEqual((status, err), (0, ""), f"{args}: {err}")
        return out

    def assert_refused(self, expected: str, *args: str, **options: Any) -> str:
        """Assert the command is refused (exit 1, nothing on stdout) with `expected` in the message."""
        status, out, err = self.run_approve(*args, **options)
        self.assertEqual(status, 1, f"{args}: {out}")
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("refused: "), err)
        self.assertIn(expected, err)
        return err

    def task(self, **fields: object) -> None:
        """Create T-001 in the store, then set the given fields directly, as a test fixture."""
        data = store.new_item(
            store.empty_store(), title="Check links", contract="contracts/T-001.md",
            owns=["example/linkcheck/**"], actor="planner", at="2026-11-02T10:00:00Z",
        )
        data["items"]["T-001"].update(fields)
        store.save(self.store_path, data)

    def pr_open_task(self, pr: int | None = 7) -> None:
        """Create T-001 with its pull request open."""
        self.task(state="pr_open", pr=pr, dispatched_to="worker", dispatched_at="2026-11-02T10:05:00Z")

    def item(self) -> dict:
        """Return T-001 as it is in the store file."""
        return store.load(self.store_path)["items"]["T-001"]


class HumanCheckTest(ApproveTestCase):
    """The command refuses to run in an agent session or without an interactive terminal."""

    def test_refused_when_an_agent_variable_is_set_before_the_store_or_gh_is_touched(self) -> None:
        self.pr_open_task()
        before = self.store_path.read_bytes()
        gh = FakeGh()
        for args in (("T-001", "--merge"), ("T-001", "done")):
            self.assert_refused("CLAUDECODE is set", *args, environ={"CLAUDECODE": "1"}, runner=gh)
        self.assertEqual(gh.calls, [])
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertFalse(self.store_path.with_name("gates.json.lock").exists())

    def test_refused_when_standard_input_is_not_a_terminal(self) -> None:
        self.task()
        before = self.store_path.read_bytes()
        gh = FakeGh()
        self.assert_refused("needs an interactive terminal", "T-001", "ready", interactive=False, runner=gh)
        self.assertEqual(gh.calls, [])
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertFalse(self.store_path.with_name("gates.json.lock").exists())

    def test_an_empty_agent_variable_does_not_count(self) -> None:
        self.task()
        self.assert_ok("T-001", "ready", environ={"CLAUDECODE": ""})

    def test_require_human_checks_every_agent_variable(self) -> None:
        for variable in approve.AGENT_ENV_VARS:
            with self.assertRaises(machine.Refusal) as caught:
                approve.require_human({variable: "1"}, FakeStdin(True))
            self.assertEqual(caught.exception.rule, "agent_environment")

    def test_no_standard_input_at_all_is_not_interactive(self) -> None:
        with self.assertRaises(machine.Refusal) as caught:
            approve.require_human({}, None)
        self.assertEqual(caught.exception.rule, "not_interactive")


class TransitionTest(ApproveTestCase):
    """The human's transitions go through the state machine with actor human."""

    def test_the_human_approves_a_draft_spec(self) -> None:
        self.task()
        out = self.assert_ok("T-001", "ready")
        self.assertEqual(out, "T-001: draft -> ready (human)\n")
        self.assertEqual(self.item()["state"], "ready")

    def test_the_human_resolves_an_escalation_back_to_ready(self) -> None:
        self.task(state="escalated", escalation={"reason": "stuck", "from_state": "in_progress"})
        self.assert_ok("T-001", "ready")
        self.assertEqual((self.item()["state"], self.item()["escalation"]), ("ready", None))

    def test_the_human_resolves_an_escalation_by_cancelling(self) -> None:
        self.task(state="escalated", escalation={"reason": "stuck", "from_state": "in_progress"})
        out = self.assert_ok("T-001", "cancelled")
        self.assertEqual(out, "T-001: escalated -> cancelled (human)\n")
        self.assertEqual((self.item()["state"], self.item()["escalation"]), ("cancelled", None))

    def test_the_human_closes_a_task_whose_pull_request_was_merged(self) -> None:
        self.pr_open_task()
        self.assert_ok("T-001", "done")
        self.assertEqual(self.item()["state"], "done")

    def test_the_history_event_names_the_human_as_actor(self) -> None:
        self.task()
        self.assert_ok("T-001", "ready")
        event = self.item()["history"][-1]
        self.assertEqual((event["from"], event["to"], event["actor"]), ("draft", "ready", "human"))

    def test_the_comment_becomes_the_history_reason(self) -> None:
        self.task()
        self.assert_ok("T-001", "ready", "--comment", "spec read, scope agreed")
        self.assertEqual(self.item()["history"][-1]["reason"], "spec read, scope agreed")

    def test_a_transition_the_human_may_not_take_is_refused_and_the_store_is_unchanged(self) -> None:
        self.task(state="ready")
        before = self.store_path.read_bytes()
        self.assert_refused("is made by planner, not by human", "T-001", "in_progress")
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_a_target_that_is_not_a_state_is_refused_and_the_store_is_unchanged(self) -> None:
        self.task()
        before = self.store_path.read_bytes()
        status, out, err = self.run_approve("T-001", "flying")
        self.assertEqual((status, out), (1, ""))
        self.assertTrue(err.startswith("refused: "), err)
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_a_transition_on_an_unknown_task_is_refused(self) -> None:
        self.task()
        before = self.store_path.read_bytes()
        self.assert_refused("there is no task T-404", "T-404", "ready")
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_target_and_merge_together_are_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            approve.main(["T-001", "ready", "--merge"], environ=HUMAN_ENVIRON, stdin=FakeStdin(True))
        self.assertEqual(caught.exception.code, 2)

    def test_neither_target_nor_merge_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            approve.main(["T-001"], environ=HUMAN_ENVIRON, stdin=FakeStdin(True))
        self.assertEqual(caught.exception.code, 2)


class PrHeadShaTest(unittest.TestCase):
    """Reading the pull request head with gh."""

    def test_gh_is_asked_for_the_head_commit_of_the_pull_request(self) -> None:
        gh = FakeGh()
        self.assertEqual(approve.pr_head_sha(7, runner=gh), HEAD)
        self.assertEqual(gh.calls, [["gh", "pr", "view", "7", "--json", "headRefOid", "--jq", ".headRefOid"]])

    def test_refused_when_gh_fails(self) -> None:
        gh = FakeGh(stdout="", returncode=1, stderr="no pull requests found\n")
        with self.assertRaises(machine.Refusal) as caught:
            approve.pr_head_sha(7, runner=gh)
        self.assertEqual(caught.exception.rule, "pr_head_unavailable")
        self.assertIn("no pull requests found", str(caught.exception))

    def test_refused_when_gh_is_not_installed(self) -> None:
        def missing_gh(command: list[str], **options: Any) -> subprocess.CompletedProcess:
            raise FileNotFoundError(2, "No such file or directory", "gh")

        with self.assertRaises(machine.Refusal) as caught:
            approve.pr_head_sha(7, runner=missing_gh)
        self.assertEqual(caught.exception.rule, "pr_head_unavailable")
        self.assertIn("gh could not be run", str(caught.exception))

    def test_refused_when_gh_cannot_be_executed(self) -> None:
        def not_executable(command: list[str], **options: Any) -> subprocess.CompletedProcess:
            raise PermissionError(13, "Permission denied", "gh")

        with self.assertRaises(machine.Refusal) as caught:
            approve.pr_head_sha(7, runner=not_executable)
        self.assertEqual(caught.exception.rule, "pr_head_unavailable")
        self.assertIn("Permission denied", str(caught.exception))

    def test_refused_when_gh_output_cannot_be_decoded(self) -> None:
        def undecodable(command: list[str], **options: Any) -> subprocess.CompletedProcess:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        with self.assertRaises(machine.Refusal) as caught:
            approve.pr_head_sha(7, runner=undecodable)
        self.assertEqual(caught.exception.rule, "pr_head_unavailable")

    def test_refused_when_gh_returns_something_other_than_a_commit_sha(self) -> None:
        for output in ("", "null\n", HEAD.upper(), HEAD[:7]):
            with self.assertRaises(machine.Refusal) as caught:
                approve.pr_head_sha(7, runner=FakeGh(stdout=output))
            self.assertEqual(caught.exception.rule, "pr_head_invalid", output)


class MergeTest(ApproveTestCase):
    """The human's go to merge an open pull request at its current head."""

    def test_the_merge_go_records_the_head_and_time_and_a_history_event(self) -> None:
        self.pr_open_task()
        out = self.assert_ok("T-001", "--merge")
        item = self.item()
        self.assertEqual(item["state"], "pr_open")
        self.assertEqual(item["merge_approved_head"], HEAD)
        self.assertRegex(item["merge_approved_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        event = item["history"][-1]
        self.assertEqual(
            (event["from"], event["to"], event["actor"], event["head"], event["at"]),
            ("pr_open", "pr_open", "human", HEAD, item["merge_approved_at"]),
        )
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("#7", lines[0])
        self.assertIn(HEAD[:7], lines[0])
        self.assertIn(item["merge_approved_at"], lines[0])
        self.assertIn(f"gh pr merge 7 --squash --match-head-commit {HEAD}", lines[1])
        self.assertIn("refuses this merge if the pull request head has moved", lines[2])
        self.assertIn("voids the go", lines[3])

    def test_the_merge_comment_is_added_to_the_history_reason(self) -> None:
        self.pr_open_task()
        self.assert_ok("T-001", "--merge", "--comment", "diff read")
        self.assertEqual(self.item()["history"][-1]["reason"], f"merge approved at head {HEAD}: diff read")

    def test_merge_is_refused_when_the_task_is_not_pr_open(self) -> None:
        self.task(state="in_review", pr=7)
        gh = FakeGh()
        self.assert_refused("can only be approved while its pull request is open", "T-001", "--merge", runner=gh)
        self.assertEqual(gh.calls, [])

    def test_merge_is_refused_when_no_pull_request_is_recorded(self) -> None:
        self.pr_open_task(pr=None)
        gh = FakeGh()
        self.assert_refused("has no pull request number recorded", "T-001", "--merge", runner=gh)
        self.assertEqual(gh.calls, [])

    def test_merge_is_refused_for_an_unknown_task(self) -> None:
        self.assert_refused("there is no task T-404", "T-404", "--merge")

    def test_merge_is_refused_when_gh_fails(self) -> None:
        self.pr_open_task()
        before = self.store_path.read_bytes()
        gh = FakeGh(stdout="", returncode=1, stderr="HTTP 401: Bad credentials")
        self.assert_refused("could not read the head commit of pull request #7", "T-001", "--merge", runner=gh)
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_merge_is_refused_when_gh_is_missing(self) -> None:
        self.pr_open_task()

        def missing_gh(command: list[str], **options: Any) -> subprocess.CompletedProcess:
            raise FileNotFoundError(2, "No such file or directory", "gh")

        self.assert_refused("gh could not be run", "T-001", "--merge", runner=missing_gh)
        self.assertIsNone(self.item()["merge_approved_head"])

    def test_merge_is_refused_when_gh_returns_a_non_sha(self) -> None:
        self.pr_open_task()
        self.assert_refused("not a 40-character commit SHA", "T-001", "--merge", runner=FakeGh(stdout="main\n"))
        self.assertIsNone(self.item()["merge_approved_head"])

    def test_merge_is_refused_when_the_task_changes_while_gh_runs(self) -> None:
        self.pr_open_task()
        store_path = self.store_path

        def gh_while_the_planner_moves_the_task(command: list[str], **options: Any) -> subprocess.CompletedProcess:
            with store.locked(store_path) as data:
                data["items"]["T-001"]["state"] = "in_review"
            return subprocess.CompletedProcess(command, 0, stdout=HEAD + "\n", stderr="")

        self.assert_refused(
            "changed while its pull request head was being read", "T-001", "--merge",
            runner=gh_while_the_planner_moves_the_task,
        )
        item = self.item()
        self.assertEqual((item["state"], item["merge_approved_head"]), ("in_review", None))

    def test_merge_is_refused_when_the_task_leaves_pr_open_and_returns_while_gh_runs(self) -> None:
        self.pr_open_task()
        store_path = self.store_path

        def gh_while_the_task_goes_to_review_and_back(
            command: list[str], **options: Any
        ) -> subprocess.CompletedProcess:
            with store.locked(store_path) as data:
                item = data["items"]["T-001"]
                item["history"].append(
                    {"from": "pr_open", "to": "in_review", "actor": "planner", "head": NEW_HEAD,
                     "at": "2026-11-02T11:00:00Z"}
                )
                item["history"].append(
                    {"from": "in_review", "to": "pr_open", "actor": "planner", "head": NEW_HEAD,
                     "at": "2026-11-02T11:01:00Z"}
                )
            return subprocess.CompletedProcess(command, 0, stdout=HEAD + "\n", stderr="")

        self.assert_refused(
            "changed while its pull request head was being read", "T-001", "--merge",
            runner=gh_while_the_task_goes_to_review_and_back,
        )
        item = self.item()
        self.assertEqual((item["state"], item["pr"], item["merge_approved_head"]), ("pr_open", 7, None))
        self.assertEqual(item["merge_approved_at"], None)
        self.assertEqual(item["history"][-1]["to"], "pr_open")
        self.assertNotIn("merge approved", item["history"][-1].get("reason", ""))

    def test_a_new_head_moved_back_to_review_clears_the_merge_go(self) -> None:
        self.pr_open_task()
        self.assert_ok("T-001", "--merge")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = cli.main(
                ["--store", str(self.store_path), "transition", "T-001", "in_review", "--head", NEW_HEAD,
                 "--actor", "planner"]
            )
        self.assertEqual(status, 0)
        item = self.item()
        self.assertEqual(
            (item["state"], item["merge_approved_head"], item["merge_approved_at"]), ("in_review", None, None)
        )
        self.assertEqual(item["history"][-1]["head"], NEW_HEAD)

    @unittest.skipIf(sys.platform == "win32", "the fake gh is a POSIX shell script")
    def test_the_merge_go_reads_the_head_from_the_gh_found_on_path(self) -> None:
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake_gh = bin_dir / "gh"
        fake_gh.write_text(f"#!/bin/sh\necho {HEAD}\n", encoding="utf-8")
        fake_gh.chmod(0o755)
        self.pr_open_task()
        self.assert_ok("T-001", "--merge", environ={"PATH": str(bin_dir)}, runner=subprocess.run)
        self.assertEqual(self.item()["merge_approved_head"], HEAD)


class EntryPointTest(unittest.TestCase):
    """The command runs as a script."""

    def test_the_script_help_runs_from_the_repository_root(self) -> None:
        result = subprocess.run(
            [sys.executable, "gates/approve.py", "--help"], cwd=REPO_ROOT, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--merge", result.stdout)


if __name__ == "__main__":
    unittest.main()
