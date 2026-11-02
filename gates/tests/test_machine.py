"""Specification of gates/machine.py: one test per rule of the state machine."""

import copy
import unittest
from typing import Any

from gates import machine
from gates.machine import Evidence, Refusal

AT = "2026-11-02T18:27:37Z"
LATER = "2026-11-02T19:00:00Z"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"

DISPATCH = Evidence(contract_exists=True)
SUBMIT = Evidence(concerns_found=True, progress_after_dispatch=True)
FAILED_REVIEW = Evidence(verdict_path="reviews/T-001-1.md", verdict_exists=True, verdict_overall="FAIL")
PASSED_REVIEW = Evidence(verdict_path="reviews/T-001-2.md", verdict_exists=True, verdict_overall="PASS", pr=7)
NEW_HEAD = Evidence(head=SHA)
ESCALATION = Evidence(reason="the API changed", tried="two fixes", options="pin or port", recommends="port")
NO_EVIDENCE = Evidence()


def make_item(item_id: str = "T-001", state: str = "draft", **fields: Any) -> dict:
    """Return an item in the store's shape, with `fields` overriding the defaults."""
    item = {
        "id": item_id,
        "title": f"task {item_id}",
        "state": state,
        "contract": f"contracts/{item_id}.md",
        "owns": ["example/linkcheck/**"],
        "dispatched_to": None,
        "dispatched_at": None,
        "pr": None,
        "review_cycles": 0,
        "verdicts": [],
        "merge_approved_head": None,
        "merge_approved_at": None,
        "escalation": None,
        "history": [{"at": AT, "from": None, "to": "draft", "actor": "planner", "reason": "created"}],
    }
    item.update(fields)
    return item


def make_store(*items: dict) -> dict:
    """Return a store holding `items`, in order."""
    return {"version": 1, "items": {item["id"]: item for item in items}}


def single(state: str, **fields: Any) -> dict:
    """Return a store holding one task T-001 in `state`."""
    return make_store(make_item("T-001", state, **fields))


class RefusalAssertions(unittest.TestCase):
    """Helpers that assert a request is refused for a given rule."""

    def assert_refused(self, rule: str, store: dict, to_state: str, actor: str, evidence: Evidence) -> Refusal:
        """Assert that the transition is refused for `rule`, and return the refusal."""
        with self.assertRaises(Refusal) as caught:
            machine.check_transition(store, "T-001", to_state, actor=actor, evidence=evidence)
        self.assertEqual(caught.exception.rule, rule, str(caught.exception))
        return caught.exception

    def assert_allowed(self, store: dict, to_state: str, actor: str, evidence: Evidence) -> dict:
        """Assert that the transition is allowed, and return the new store."""
        machine.check_transition(store, "T-001", to_state, actor=actor, evidence=evidence)
        return machine.apply_transition(store, "T-001", to_state, actor=actor, evidence=evidence, at=LATER)


class TablesTest(unittest.TestCase):
    """The states, actors and transition table."""

    def test_there_are_nine_states(self) -> None:
        self.assertEqual(len(machine.STATES), 9)
        self.assertEqual(len(set(machine.STATES)), 9)

    def test_terminal_states_are_done_and_cancelled(self) -> None:
        self.assertEqual(set(machine.TERMINAL_STATES), {"done", "cancelled"})

    def test_no_transition_leaves_a_terminal_state(self) -> None:
        for from_state, _ in machine.TRANSITIONS:
            self.assertNotIn(from_state, machine.TERMINAL_STATES)
        for state in machine.TERMINAL_STATES:
            self.assertEqual(machine.legal_targets(state), ())

    def test_transition_table_matches_the_plan(self) -> None:
        agents = ("planner", "worker", "reviewer")
        expected = {
            ("draft", "ready"): {"human"},
            ("ready", "in_progress"): {"planner"},
            ("in_progress", "in_review"): {"worker"},
            ("in_review", "changes_requested"): {"reviewer"},
            ("in_review", "pr_open"): {"reviewer"},
            ("changes_requested", "in_progress"): {"planner"},
            ("pr_open", "in_review"): {"planner"},
            ("pr_open", "done"): {"planner", "human"},
            ("escalated", "ready"): {"human"},
            ("escalated", "cancelled"): {"human"},
        }
        for state in machine.ESCALATABLE_STATES:
            expected[(state, "escalated")] = set(agents)
        actual = {key: set(actors) for key, actors in machine.TRANSITIONS.items()}
        self.assertEqual(actual, expected)

    def test_every_state_in_the_table_is_a_known_state(self) -> None:
        for from_state, to_state in machine.TRANSITIONS:
            self.assertIn(from_state, machine.STATES)
            self.assertIn(to_state, machine.STATES)

    def test_every_actor_in_the_table_is_a_known_actor(self) -> None:
        for actors in machine.TRANSITIONS.values():
            for actor in actors:
                self.assertIn(actor, machine.ACTORS)

    def test_escalatable_states_are_the_five_working_states(self) -> None:
        self.assertEqual(
            machine.ESCALATABLE_STATES, ("ready", "in_progress", "in_review", "changes_requested", "pr_open")
        )

    def test_dispatched_states_hold_their_owns_globs(self) -> None:
        self.assertEqual(
            machine.DISPATCHED_STATES, ("in_progress", "in_review", "changes_requested", "pr_open", "escalated")
        )

    def test_review_cycle_limit_is_two(self) -> None:
        self.assertEqual(machine.REVIEW_CYCLE_LIMIT, 2)

    def test_legal_targets_lists_the_states_reachable_from_a_state(self) -> None:
        self.assertEqual(machine.legal_targets("pr_open"), ("in_review", "done", "escalated"))
        self.assertEqual(machine.legal_targets("draft"), ("ready",))

    def test_allowed_actors_is_empty_for_a_missing_transition(self) -> None:
        self.assertEqual(machine.allowed_actors("draft", "done"), ())
        self.assertEqual(machine.allowed_actors("pr_open", "done"), ("planner", "human"))


class RefusalTest(unittest.TestCase):
    """How refusals are built."""

    def test_refusal_message_comes_from_the_refusals_table(self) -> None:
        refusal = Refusal("unknown_task", task="T-404")
        self.assertEqual(refusal.rule, "unknown_task")
        self.assertEqual(str(refusal), machine.REFUSALS["unknown_task"].format(task="T-404"))

    def test_refusal_is_a_gate_error(self) -> None:
        self.assertIsInstance(Refusal("unknown_task", task="T-404"), machine.GateError)

    def test_every_refusal_template_is_a_non_empty_message(self) -> None:
        for rule, template in machine.REFUSALS.items():
            self.assertTrue(template.strip(), rule)

    def test_cli_refusal_for_the_human_actor_points_to_approve(self) -> None:
        self.assertIn("approve.py", machine.REFUSALS["human_actor_in_cli"])


class CheckOrderTest(RefusalAssertions):
    """Refusals are checked in a fixed order so the message is deterministic."""

    def test_unknown_task_is_refused(self) -> None:
        with self.assertRaises(Refusal) as caught:
            machine.check_transition(single("ready"), "T-404", "in_progress", actor="planner", evidence=DISPATCH)
        self.assertEqual(caught.exception.rule, "unknown_task")
        self.assertIn("T-404", str(caught.exception))

    def test_unknown_target_state_is_refused(self) -> None:
        refusal = self.assert_refused("unknown_state", single("ready"), "started", "planner", DISPATCH)
        self.assertIn("started", str(refusal))

    def test_terminal_state_is_refused_before_the_actor_is_checked(self) -> None:
        refusal = self.assert_refused("terminal_state", single("done"), "in_progress", "worker", NO_EVIDENCE)
        self.assertIn("T-001", str(refusal))
        self.assertIn("done", str(refusal))

    def test_cancelled_task_is_refused_as_terminal(self) -> None:
        self.assert_refused("terminal_state", single("cancelled"), "ready", "human", NO_EVIDENCE)

    def test_missing_transition_is_refused_with_the_legal_targets(self) -> None:
        refusal = self.assert_refused("no_such_transition", single("draft"), "in_progress", "planner", DISPATCH)
        self.assertIn("ready", str(refusal))
        self.assertIn("draft", str(refusal))

    def test_missing_transition_is_checked_before_the_actor(self) -> None:
        self.assert_refused("no_such_transition", single("draft"), "done", "worker", NO_EVIDENCE)

    def test_wrong_actor_is_checked_before_the_guards(self) -> None:
        self.assert_refused("wrong_actor", single("ready"), "in_progress", "worker", NO_EVIDENCE)

    def test_wrong_actor_names_the_allowed_actors(self) -> None:
        refusal = self.assert_refused("wrong_actor", single("pr_open", pr=7), "done", "reviewer", NO_EVIDENCE)
        self.assertIn("planner or human", str(refusal))
        self.assertIn("reviewer", str(refusal))

    def test_human_only_transition_tells_the_agent_to_run_approve(self) -> None:
        refusal = self.assert_refused("human_only", single("draft"), "ready", "planner", NO_EVIDENCE)
        self.assertIn("python gates/approve.py T-001 ready", str(refusal))
        self.assertIn("terminal", str(refusal))

    def test_every_human_only_transition_is_refused_for_every_agent(self) -> None:
        cases = [("draft", "ready"), ("escalated", "ready"), ("escalated", "cancelled")]
        for from_state, to_state in cases:
            for actor in machine.AGENT_ACTORS:
                with self.subTest(from_state=from_state, to_state=to_state, actor=actor):
                    self.assert_refused("human_only", single(from_state), to_state, actor, NO_EVIDENCE)

    def test_unknown_actor_is_refused_as_wrong_actor(self) -> None:
        self.assert_refused("wrong_actor", single("ready"), "in_progress", "someone", DISPATCH)


class SpecApprovalTest(RefusalAssertions):
    """draft -> ready."""

    def test_draft_to_ready_is_allowed_for_the_human(self) -> None:
        new_store = self.assert_allowed(single("draft"), "ready", "human", NO_EVIDENCE)
        self.assertEqual(new_store["items"]["T-001"]["state"], "ready")


class DispatchTest(RefusalAssertions):
    """ready -> in_progress."""

    def test_dispatch_is_allowed_for_the_planner(self) -> None:
        new_store = self.assert_allowed(single("ready"), "in_progress", "planner", DISPATCH)
        self.assertEqual(new_store["items"]["T-001"]["state"], "in_progress")

    def test_dispatch_is_refused_for_every_other_actor(self) -> None:
        for actor in ("worker", "reviewer", "human"):
            with self.subTest(actor=actor):
                self.assert_refused("wrong_actor", single("ready"), "in_progress", actor, DISPATCH)

    def test_dispatch_is_refused_without_a_contract_file(self) -> None:
        refusal = self.assert_refused("contract_missing", single("ready"), "in_progress", "planner", NO_EVIDENCE)
        self.assertIn("contracts/T-001.md", str(refusal))

    def test_dispatch_is_refused_when_owns_is_empty(self) -> None:
        self.assert_refused("owns_empty", single("ready", owns=[]), "in_progress", "planner", DISPATCH)

    def test_dispatch_is_refused_when_owns_overlaps_a_dispatched_task(self) -> None:
        for state in machine.DISPATCHED_STATES:
            with self.subTest(state=state):
                store = make_store(
                    make_item("T-001", "ready", owns=["example/linkcheck/cli.py"]),
                    make_item("T-002", state, owns=["example/**"]),
                )
                refusal = self.assert_refused("owns_overlap", store, "in_progress", "planner", DISPATCH)
                self.assertIn("T-002", str(refusal))

    def test_dispatch_ignores_tasks_that_are_not_dispatched(self) -> None:
        for state in ("draft", "ready", "done", "cancelled"):
            with self.subTest(state=state):
                store = make_store(make_item("T-001", "ready"), make_item("T-002", state, owns=["example/**"]))
                self.assert_allowed(store, "in_progress", "planner", DISPATCH)

    def test_dispatch_guards_run_in_table_order(self) -> None:
        store = make_store(make_item("T-001", "ready", owns=[]), make_item("T-002", "in_progress"))
        self.assert_refused("contract_missing", store, "in_progress", "planner", NO_EVIDENCE)

    def test_dispatch_records_who_and_when(self) -> None:
        evidence = Evidence(contract_exists=True, dispatch_to="worker-2")
        new_store = self.assert_allowed(single("ready"), "in_progress", "planner", evidence)
        item = new_store["items"]["T-001"]
        self.assertEqual(item["dispatched_to"], "worker-2")
        self.assertEqual(item["dispatched_at"], LATER)

    def test_dispatch_goes_to_the_worker_by_default(self) -> None:
        new_store = self.assert_allowed(single("ready"), "in_progress", "planner", DISPATCH)
        self.assertEqual(new_store["items"]["T-001"]["dispatched_to"], "worker")


class SubmissionTest(RefusalAssertions):
    """in_progress -> in_review."""

    def store(self) -> dict:
        """Return a store with T-001 dispatched."""
        return single("in_progress", dispatched_to="worker", dispatched_at=AT)

    def test_submission_is_allowed_for_the_worker(self) -> None:
        new_store = self.assert_allowed(self.store(), "in_review", "worker", SUBMIT)
        self.assertEqual(new_store["items"]["T-001"]["state"], "in_review")

    def test_submission_is_refused_for_every_other_actor(self) -> None:
        for actor in ("planner", "reviewer", "human"):
            with self.subTest(actor=actor):
                self.assert_refused("wrong_actor", self.store(), "in_review", actor, SUBMIT)

    def test_submission_is_refused_without_a_concerns_table(self) -> None:
        evidence = Evidence(concerns_found=False, progress_after_dispatch=True)
        self.assert_refused("concerns_missing", self.store(), "in_review", "worker", evidence)

    def test_submission_is_refused_while_a_concern_is_unfilled(self) -> None:
        evidence = Evidence(concerns_found=True, unfilled_concerns=("Security", "Tests"), progress_after_dispatch=True)
        refusal = self.assert_refused("concerns_unfilled", self.store(), "in_review", "worker", evidence)
        self.assertIn("Security, Tests", str(refusal))

    def test_submission_is_refused_without_a_progress_note_since_the_dispatch(self) -> None:
        evidence = Evidence(concerns_found=True, progress_after_dispatch=False)
        refusal = self.assert_refused("no_progress_note", self.store(), "in_review", "worker", evidence)
        self.assertIn(AT, str(refusal))

    def test_submission_guards_run_in_table_order(self) -> None:
        self.assert_refused("concerns_missing", self.store(), "in_review", "worker", NO_EVIDENCE)
        evidence = Evidence(concerns_found=True, unfilled_concerns=("Security",))
        self.assert_refused("concerns_unfilled", self.store(), "in_review", "worker", evidence)


class ReviewTest(RefusalAssertions):
    """in_review -> changes_requested and in_review -> pr_open."""

    REVIEWS = (("changes_requested", FAILED_REVIEW), ("pr_open", PASSED_REVIEW))

    def test_failed_review_is_allowed_for_the_reviewer(self) -> None:
        new_store = self.assert_allowed(single("in_review"), "changes_requested", "reviewer", FAILED_REVIEW)
        self.assertEqual(new_store["items"]["T-001"]["state"], "changes_requested")

    def test_failed_review_counts_a_cycle_and_records_the_verdict(self) -> None:
        new_store = self.assert_allowed(single("in_review"), "changes_requested", "reviewer", FAILED_REVIEW)
        item = new_store["items"]["T-001"]
        self.assertEqual(item["review_cycles"], 1)
        self.assertEqual(item["verdicts"], ["reviews/T-001-1.md"])
        self.assertEqual(item["history"][-1]["verdict"], "reviews/T-001-1.md")

    def test_passed_review_is_allowed_for_the_reviewer(self) -> None:
        new_store = self.assert_allowed(single("in_review"), "pr_open", "reviewer", PASSED_REVIEW)
        self.assertEqual(new_store["items"]["T-001"]["state"], "pr_open")

    def test_passed_review_records_the_pr_and_the_verdict_without_counting_a_cycle(self) -> None:
        new_store = self.assert_allowed(single("in_review"), "pr_open", "reviewer", PASSED_REVIEW)
        item = new_store["items"]["T-001"]
        self.assertEqual(item["pr"], 7)
        self.assertEqual(item["verdicts"], ["reviews/T-001-2.md"])
        self.assertEqual(item["review_cycles"], 0)
        self.assertEqual(item["history"][-1]["pr"], 7)
        self.assertEqual(item["history"][-1]["verdict"], "reviews/T-001-2.md")

    def test_review_is_refused_for_every_other_actor(self) -> None:
        for to_state, evidence in self.REVIEWS:
            for actor in ("planner", "worker", "human"):
                with self.subTest(to_state=to_state, actor=actor):
                    self.assert_refused("wrong_actor", single("in_review"), to_state, actor, evidence)

    def test_review_is_refused_without_a_verdict_path(self) -> None:
        for to_state, _ in self.REVIEWS:
            with self.subTest(to_state=to_state):
                evidence = Evidence(verdict_exists=True, verdict_overall="PASS", pr=7)
                self.assert_refused("verdict_path_missing", single("in_review"), to_state, "reviewer", evidence)

    def test_review_is_refused_when_the_verdict_file_does_not_exist(self) -> None:
        for to_state, _ in self.REVIEWS:
            with self.subTest(to_state=to_state):
                evidence = Evidence(verdict_path="reviews/x.md", verdict_exists=False, pr=7)
                store = single("in_review")
                refusal = self.assert_refused("verdict_file_missing", store, to_state, "reviewer", evidence)
                self.assertIn("reviews/x.md", str(refusal))

    def test_review_is_refused_when_the_verdict_has_no_overall_line(self) -> None:
        for to_state, _ in self.REVIEWS:
            with self.subTest(to_state=to_state):
                evidence = Evidence(verdict_path="reviews/x.md", verdict_exists=True, verdict_overall=None, pr=7)
                self.assert_refused("verdict_overall_missing", single("in_review"), to_state, "reviewer", evidence)

    def test_review_is_refused_when_the_overall_line_does_not_match_the_target(self) -> None:
        cases = (("changes_requested", "PASS"), ("pr_open", "FAIL"))
        for to_state, found in cases:
            with self.subTest(to_state=to_state):
                evidence = Evidence(verdict_path="reviews/x.md", verdict_exists=True, verdict_overall=found, pr=7)
                store = single("in_review")
                refusal = self.assert_refused("verdict_overall_mismatch", store, to_state, "reviewer", evidence)
                self.assertIn(f"Overall: {found}", str(refusal))

    def test_review_is_refused_when_the_verdict_was_already_recorded(self) -> None:
        for to_state, evidence in self.REVIEWS:
            with self.subTest(to_state=to_state):
                store = single("in_review", verdicts=[evidence.verdict_path])
                self.assert_refused("verdict_reused", store, to_state, "reviewer", evidence)

    def test_passed_review_is_refused_without_a_pr_number(self) -> None:
        for pr in (None, 0, -3, True):
            with self.subTest(pr=pr):
                evidence = Evidence(verdict_path="reviews/x.md", verdict_exists=True, verdict_overall="PASS", pr=pr)
                self.assert_refused("pr_missing", single("in_review"), "pr_open", "reviewer", evidence)

    def test_review_guards_run_in_table_order(self) -> None:
        evidence = Evidence(verdict_path="reviews/x.md", verdict_exists=False, verdict_overall="FAIL")
        self.assert_refused("verdict_file_missing", single("in_review"), "pr_open", "reviewer", evidence)


class RetryTest(RefusalAssertions):
    """changes_requested -> in_progress."""

    def store(self, cycles: int = 1, **fields: Any) -> dict:
        """Return a store with T-001 sent back after `cycles` failed reviews."""
        return single("changes_requested", review_cycles=cycles, dispatched_to="worker", dispatched_at=AT, **fields)

    def test_retry_is_allowed_for_the_planner_below_the_review_cycle_limit(self) -> None:
        new_store = self.assert_allowed(self.store(cycles=1), "in_progress", "planner", DISPATCH)
        self.assertEqual(new_store["items"]["T-001"]["state"], "in_progress")

    def test_retry_is_refused_for_every_other_actor(self) -> None:
        for actor in ("worker", "reviewer", "human"):
            with self.subTest(actor=actor):
                self.assert_refused("wrong_actor", self.store(), "in_progress", actor, DISPATCH)

    def test_retry_is_refused_after_two_failed_reviews(self) -> None:
        refusal = self.assert_refused("review_cycle_limit", self.store(cycles=2), "in_progress", "planner", DISPATCH)
        message = str(refusal)
        self.assertIn("escalated", message)
        for flag in ("--reason", "--tried", "--options", "--recommends"):
            self.assertIn(flag, message)

    def test_retry_limit_is_checked_before_the_dispatch_guards(self) -> None:
        self.assert_refused("review_cycle_limit", self.store(cycles=2), "in_progress", "planner", NO_EVIDENCE)

    def test_retry_runs_the_dispatch_guards(self) -> None:
        self.assert_refused("contract_missing", self.store(), "in_progress", "planner", NO_EVIDENCE)
        self.assert_refused("owns_empty", self.store(owns=[]), "in_progress", "planner", DISPATCH)
        store = make_store(
            make_item("T-001", "changes_requested", review_cycles=1),
            make_item("T-002", "in_review", owns=["example/linkcheck/cli.py"]),
        )
        self.assert_refused("owns_overlap", store, "in_progress", "planner", DISPATCH)

    def test_retry_does_not_count_the_task_itself_as_an_overlap(self) -> None:
        self.assert_allowed(self.store(), "in_progress", "planner", DISPATCH)

    def test_retry_restamps_the_dispatch_so_the_worker_writes_a_new_note(self) -> None:
        new_store = self.assert_allowed(self.store(), "in_progress", "planner", DISPATCH)
        item = new_store["items"]["T-001"]
        self.assertEqual(item["dispatched_at"], LATER)
        self.assertEqual(item["dispatched_to"], "worker")
        self.assertEqual(item["review_cycles"], 1)

    def test_second_failed_review_reaches_the_limit(self) -> None:
        store = single("in_review", review_cycles=1, verdicts=["reviews/T-001-0.md"])
        new_store = self.assert_allowed(store, "changes_requested", "reviewer", FAILED_REVIEW)
        self.assertEqual(new_store["items"]["T-001"]["review_cycles"], machine.REVIEW_CYCLE_LIMIT)
        with self.assertRaises(Refusal) as caught:
            machine.check_transition(new_store, "T-001", "in_progress", actor="planner", evidence=DISPATCH)
        self.assertEqual(caught.exception.rule, "review_cycle_limit")


class NewHeadTest(RefusalAssertions):
    """pr_open -> in_review, after new commits on the pull request."""

    def store(self) -> dict:
        """Return a store with T-001's pull request open and approved for merge."""
        return single("pr_open", pr=7, review_cycles=1, merge_approved_head=OTHER_SHA, merge_approved_at=AT)

    def test_new_head_is_allowed_for_the_planner(self) -> None:
        new_store = self.assert_allowed(self.store(), "in_review", "planner", NEW_HEAD)
        self.assertEqual(new_store["items"]["T-001"]["state"], "in_review")

    def test_new_head_is_refused_for_every_other_actor(self) -> None:
        for actor in ("worker", "reviewer", "human"):
            with self.subTest(actor=actor):
                self.assert_refused("wrong_actor", self.store(), "in_review", actor, NEW_HEAD)

    def test_new_head_is_refused_unless_it_is_a_full_lowercase_sha(self) -> None:
        for head in (None, "", SHA[:7], SHA.upper(), SHA + "0", "g" * 40):
            with self.subTest(head=head):
                self.assert_refused("head_invalid", self.store(), "in_review", "planner", Evidence(head=head))

    def test_new_head_clears_the_merge_approval(self) -> None:
        item = self.assert_allowed(self.store(), "in_review", "planner", NEW_HEAD)["items"]["T-001"]
        self.assertIsNone(item["merge_approved_head"])
        self.assertIsNone(item["merge_approved_at"])

    def test_new_head_does_not_spend_a_review_cycle(self) -> None:
        item = self.assert_allowed(self.store(), "in_review", "planner", NEW_HEAD)["items"]["T-001"]
        self.assertEqual(item["review_cycles"], 1)
        self.assertEqual(item["pr"], 7)

    def test_new_head_is_recorded_in_the_history(self) -> None:
        item = self.assert_allowed(self.store(), "in_review", "planner", NEW_HEAD)["items"]["T-001"]
        self.assertEqual(item["history"][-1]["head"], SHA)


class DoneTest(RefusalAssertions):
    """pr_open -> done."""

    def test_done_is_allowed_for_the_planner_and_the_human(self) -> None:
        for actor in ("planner", "human"):
            with self.subTest(actor=actor):
                new_store = self.assert_allowed(single("pr_open", pr=7), "done", actor, NO_EVIDENCE)
                self.assertEqual(new_store["items"]["T-001"]["state"], "done")

    def test_done_is_refused_for_the_worker_and_the_reviewer(self) -> None:
        for actor in ("worker", "reviewer"):
            with self.subTest(actor=actor):
                self.assert_refused("wrong_actor", single("pr_open", pr=7), "done", actor, NO_EVIDENCE)


class EscalationTest(RefusalAssertions):
    """ESCALATABLE_STATES -> escalated, and back out through the human."""

    def test_escalation_is_allowed_from_every_working_state_for_every_agent(self) -> None:
        for state in machine.ESCALATABLE_STATES:
            for actor in machine.AGENT_ACTORS:
                with self.subTest(state=state, actor=actor):
                    new_store = self.assert_allowed(single(state), "escalated", actor, ESCALATION)
                    self.assertEqual(new_store["items"]["T-001"]["state"], "escalated")

    def test_escalation_is_refused_for_the_human(self) -> None:
        self.assert_refused("wrong_actor", single("in_review"), "escalated", "human", ESCALATION)

    def test_escalation_is_refused_from_draft(self) -> None:
        self.assert_refused("no_such_transition", single("draft"), "escalated", "planner", ESCALATION)

    def test_escalation_is_refused_unless_every_field_is_filled(self) -> None:
        evidence = Evidence(reason="stuck", tried="  ", options="", recommends="ask")
        refusal = self.assert_refused("escalation_incomplete", single("in_progress"), "escalated", "worker", evidence)
        self.assertIn("--tried, --options", str(refusal))
        self.assertNotIn("--reason,", str(refusal).split("Missing:")[1])

    def test_escalation_records_the_decision_request(self) -> None:
        new_store = self.assert_allowed(single("pr_open", pr=7), "escalated", "planner", ESCALATION)
        self.assertEqual(
            new_store["items"]["T-001"]["escalation"],
            {
                "reason": "the API changed",
                "tried": "two fixes",
                "options": "pin or port",
                "recommends": "port",
                "from_state": "pr_open",
                "by": "planner",
                "at": LATER,
            },
        )

    def test_escalation_reason_is_the_history_reason(self) -> None:
        new_store = self.assert_allowed(single("in_progress"), "escalated", "worker", ESCALATION)
        self.assertEqual(new_store["items"]["T-001"]["history"][-1]["reason"], "the API changed")

    def test_human_returning_an_escalated_task_to_ready_clears_the_escalation(self) -> None:
        store = self.assert_allowed(single("in_progress"), "escalated", "worker", ESCALATION)
        new_store = machine.apply_transition(store, "T-001", "ready", actor="human", evidence=NO_EVIDENCE, at=LATER)
        self.assertEqual(new_store["items"]["T-001"]["state"], "ready")
        self.assertIsNone(new_store["items"]["T-001"]["escalation"])

    def test_human_cancelling_an_escalated_task_clears_the_escalation(self) -> None:
        store = self.assert_allowed(single("in_progress"), "escalated", "worker", ESCALATION)
        new_store = machine.apply_transition(store, "T-001", "cancelled", actor="human", evidence=NO_EVIDENCE, at=LATER)
        self.assertEqual(new_store["items"]["T-001"]["state"], "cancelled")
        self.assertIsNone(new_store["items"]["T-001"]["escalation"])


class HistoryAndPurityTest(RefusalAssertions):
    """Every transition is recorded, and the input store is never changed."""

    def test_every_transition_appends_a_history_event(self) -> None:
        new_store = self.assert_allowed(single("ready"), "in_progress", "planner", DISPATCH)
        history = new_store["items"]["T-001"]["history"]
        self.assertEqual(len(history), 2)
        event = history[-1]
        self.assertEqual(
            {key: event[key] for key in ("at", "from", "to", "actor")},
            {"at": LATER, "from": "ready", "to": "in_progress", "actor": "planner"},
        )
        self.assertTrue(event["reason"])

    def test_a_given_reason_is_recorded_in_the_history(self) -> None:
        new_store = self.assert_allowed(single("draft"), "ready", "human", Evidence(reason="spec reviewed"))
        self.assertEqual(new_store["items"]["T-001"]["history"][-1]["reason"], "spec reviewed")

    def test_apply_transition_does_not_mutate_its_input(self) -> None:
        store = single("in_review", verdicts=["reviews/T-001-0.md"])
        before = copy.deepcopy(store)
        machine.apply_transition(
            store, "T-001", "changes_requested", actor="reviewer", evidence=FAILED_REVIEW, at=LATER
        )
        self.assertEqual(store, before)

    def test_apply_transition_returns_a_store_that_shares_nothing_with_its_input(self) -> None:
        store = single("ready")
        new_store = self.assert_allowed(store, "in_progress", "planner", DISPATCH)
        new_store["items"]["T-001"]["owns"].append("docs/**")
        self.assertEqual(store["items"]["T-001"]["owns"], ["example/linkcheck/**"])

    def test_apply_transition_refuses_what_check_transition_refuses(self) -> None:
        with self.assertRaises(Refusal) as caught:
            machine.apply_transition(
                single("done"), "T-001", "in_progress", actor="worker", evidence=NO_EVIDENCE, at=LATER
            )
        self.assertEqual(caught.exception.rule, "terminal_state")

    def test_a_task_goes_from_draft_to_done_through_the_gates(self) -> None:
        store = single("draft")
        steps = [
            ("ready", "human", NO_EVIDENCE),
            ("in_progress", "planner", DISPATCH),
            ("in_review", "worker", SUBMIT),
            ("changes_requested", "reviewer", FAILED_REVIEW),
            ("in_progress", "planner", DISPATCH),
            ("in_review", "worker", SUBMIT),
            ("pr_open", "reviewer", PASSED_REVIEW),
            ("done", "planner", NO_EVIDENCE),
        ]
        for to_state, actor, evidence in steps:
            store = machine.apply_transition(store, "T-001", to_state, actor=actor, evidence=evidence, at=LATER)
        item = store["items"]["T-001"]
        self.assertEqual(item["state"], "done")
        self.assertEqual(item["review_cycles"], 1)
        self.assertEqual(item["verdicts"], ["reviews/T-001-1.md", "reviews/T-001-2.md"])
        self.assertEqual([event["to"] for event in item["history"]], ["draft"] + [step[0] for step in steps])


class MergeApprovalTest(unittest.TestCase):
    """The human's merge go: recorded on an open pull request at a given head."""

    def store(self) -> dict:
        """Return a store with T-001's pull request open."""
        return single("pr_open", pr=7)

    def assert_merge_refused(self, rule: str, store: dict, item_id: str = "T-001", head: str = SHA) -> None:
        """Assert that the merge approval is refused for `rule`."""
        with self.assertRaises(Refusal) as caught:
            machine.check_merge_approval(store, item_id, head=head)
        self.assertEqual(caught.exception.rule, rule, str(caught.exception))

    def test_merge_approval_records_the_head_and_the_time_and_keeps_the_state(self) -> None:
        item = machine.record_merge_approval(self.store(), "T-001", head=SHA, at=LATER)["items"]["T-001"]
        self.assertEqual(item["state"], "pr_open")
        self.assertEqual(item["merge_approved_head"], SHA)
        self.assertEqual(item["merge_approved_at"], LATER)

    def test_merge_approval_appends_a_human_history_event(self) -> None:
        item = machine.record_merge_approval(self.store(), "T-001", head=SHA, at=LATER)["items"]["T-001"]
        self.assertEqual(
            item["history"][-1],
            {
                "at": LATER,
                "from": "pr_open",
                "to": "pr_open",
                "actor": "human",
                "reason": f"merge approved at head {SHA}",
                "head": SHA,
            },
        )

    def test_merge_approval_is_refused_for_an_unknown_task(self) -> None:
        self.assert_merge_refused("unknown_task", self.store(), item_id="T-404")

    def test_merge_approval_is_refused_outside_pr_open(self) -> None:
        for state in ("in_review", "done", "escalated"):
            with self.subTest(state=state):
                self.assert_merge_refused("merge_wrong_state", single(state, pr=7))

    def test_merge_approval_is_refused_without_a_pr(self) -> None:
        self.assert_merge_refused("merge_no_pr", single("pr_open"))

    def test_merge_approval_is_refused_unless_the_head_is_a_full_lowercase_sha(self) -> None:
        for head in ("", SHA[:12], SHA.upper()):
            with self.subTest(head=head):
                self.assert_merge_refused("head_invalid", self.store(), head=head)

    def test_record_merge_approval_does_not_mutate_its_input(self) -> None:
        store = self.store()
        before = copy.deepcopy(store)
        machine.record_merge_approval(store, "T-001", head=SHA, at=LATER)
        self.assertEqual(store, before)

    def test_a_new_head_voids_the_merge_approval(self) -> None:
        store = machine.record_merge_approval(self.store(), "T-001", head=OTHER_SHA, at=AT)
        store = machine.apply_transition(store, "T-001", "in_review", actor="planner", evidence=NEW_HEAD, at=LATER)
        self.assertIsNone(store["items"]["T-001"]["merge_approved_head"])


class GlobOverlapTest(unittest.TestCase):
    """globs_overlap: conservative, symmetric, segment by segment."""

    def assert_overlap(self, a: str, b: str, expected: bool) -> None:
        """Assert the overlap of `a` and `b` in both orders."""
        self.assertEqual(machine.globs_overlap(a, b), expected, f"{a} vs {b}")
        self.assertEqual(machine.globs_overlap(b, a), expected, f"{b} vs {a}")

    def test_a_glob_overlaps_the_glob_of_its_parent_directory(self) -> None:
        self.assert_overlap("example/linkcheck/**", "example/**", True)

    def test_a_glob_overlaps_a_file_it_matches(self) -> None:
        self.assert_overlap("example/linkcheck/**", "example/linkcheck/cli.py", True)

    def test_globs_in_different_directories_do_not_overlap(self) -> None:
        self.assert_overlap("example/linkcheck/**", "docs/**", False)
        self.assert_overlap("example/a/**", "example/b/**", False)

    def test_identical_paths_overlap(self) -> None:
        self.assert_overlap("gates/machine.py", "gates/machine.py", True)

    def test_different_files_do_not_overlap(self) -> None:
        self.assert_overlap("gates/machine.py", "gates/store.py", False)

    def test_a_wildcard_segment_overlaps_only_the_names_it_matches(self) -> None:
        self.assert_overlap("gates/*.py", "gates/cli.py", True)
        self.assert_overlap("gates/*.py", "gates/README.md", False)

    def test_two_wildcard_segments_overlap_unless_their_literal_ends_differ(self) -> None:
        self.assert_overlap("gates/*.py", "gates/test_*", True)
        self.assert_overlap("gates/*.py", "gates/*.md", False)
        self.assert_overlap("gates/a*", "gates/b*", False)

    def test_a_bracket_segment_is_assumed_to_overlap_another_wildcard(self) -> None:
        self.assert_overlap("gates/[ab].py", "gates/*.md", True)

    def test_owning_a_path_owns_everything_below_it(self) -> None:
        self.assert_overlap("example", "example/linkcheck/cli.py", True)
        self.assert_overlap("example/*", "example/linkcheck/cli.py", True)

    def test_a_double_star_in_the_middle_is_assumed_to_overlap(self) -> None:
        self.assert_overlap("example/**/cli.py", "example/linkcheck/report.py", True)

    def test_leading_dot_slash_and_trailing_slash_are_ignored(self) -> None:
        self.assert_overlap("./example/linkcheck/", "example/linkcheck/cli.py", True)
        self.assert_overlap("/docs/", "example/**", False)


class OverlappingTasksTest(unittest.TestCase):
    """overlapping_tasks: which dispatched tasks hold a path this task wants."""

    def test_overlapping_tasks_lists_only_other_dispatched_tasks_in_store_order(self) -> None:
        store = make_store(
            make_item("T-001", "ready", owns=["example/linkcheck/**"]),
            make_item("T-002", "pr_open", owns=["example/**"]),
            make_item("T-003", "done", owns=["example/**"]),
            make_item("T-004", "in_progress", owns=["docs/**"]),
            make_item("T-005", "escalated", owns=["example/linkcheck/cli.py"]),
        )
        self.assertEqual(machine.overlapping_tasks(store, "T-001"), ["T-002", "T-005"])

    def test_overlapping_tasks_skips_the_task_itself(self) -> None:
        store = make_store(make_item("T-001", "in_progress"))
        self.assertEqual(machine.overlapping_tasks(store, "T-001"), [])

    def test_overlapping_tasks_refuses_an_unknown_task(self) -> None:
        with self.assertRaises(Refusal) as caught:
            machine.overlapping_tasks(make_store(), "T-404")
        self.assertEqual(caught.exception.rule, "unknown_task")


class CommitShaTest(unittest.TestCase):
    """is_commit_sha."""

    def test_a_commit_sha_is_forty_lowercase_hexadecimal_characters(self) -> None:
        self.assertTrue(machine.is_commit_sha(SHA))
        for value in (None, 7, SHA.upper(), SHA[:39], SHA + "a", "z" * 40):
            with self.subTest(value=value):
                self.assertFalse(machine.is_commit_sha(value))


if __name__ == "__main__":
    unittest.main()
