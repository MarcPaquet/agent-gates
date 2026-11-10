# agent-gates

A small, tool-agnostic kit that puts gates around coding agents: who plans, who builds and who reviews are
separate roles; a reviewer's verdict is a file the state machine requires before a task's pull request may be
recorded as open; and the
spec, merge and escalation decisions can only be passed by a human at a real terminal.

## Layout

- `gates/` — the core: `machine.py` (states, transitions, refusals; pure functions), `store.py` (JSON store, lock,
  atomic write), `cli.py` (agents' commands), `approve.py` (human only), `tests/`.
- `contracts/`, `progress/`, `reviews/`, `gates.json` — the workflow's own records, committed.
- `docs/reference.md` — the roles table, the state machine's tables, the command line, the walls, the example runs
  step by step, the layout; `gates/tests/test_reference.py` keeps its tables equal to `gates/machine.py`.
- `roles/` — planner, worker, reviewer, in harness-neutral prose.
- `.claude/` — the Claude Code adapter used in this repo: deny tiers, `hooks/gate-guard.sh`, agent files.
- `example/linkcheck/` — the toy app built through the gate (tasks T-001 and T-002).

## Rules

- Type hints, a docstring on every public function, one module per concern.
- Tests read as a spec: one test per rule, named after the rule.
- Python ≥ 3.11, standard library only. Tests: `python -m unittest discover -s gates/tests`, and
  `python -m unittest discover -s example/linkcheck/tests` for the example app.
- English only. No emoji, no marketing adjectives, no private jargon or abbreviations a reader has to decode.
- Never edit `gates.json`, a verdict or a progress log by hand; they change only through `gates/cli.py` and
  `gates/approve.py`.
- Never run `gates/approve.py` from an agent session; it is the human's command.
- Never push, merge or rebase on `main` directly; every change goes through a pull request.
