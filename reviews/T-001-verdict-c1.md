# Verdict — T-001, cycle 1

## Reviewed

- Pull request: none yet (review cycle 1 runs before the pull request is opened)
- Head commit: 2b2963d7ac93a3fff675f59a538ccb37cf2458e3 (build 66bbc7e071f0b5c7165eacee7dae5c4fb8e19a97; 2b2963d only records the in_review move in `gates.json`)
- Contract: contracts/T-001.md

Checks run by the reviewer on that head, Python 3.12.7:

- `python -m unittest discover -s example/linkcheck/tests`: Ran 8 tests, OK
- `python -m unittest discover -s gates/tests`: Ran 239 tests, OK

Each test was also run against mutated copies of `linkcheck.py`, made in a scratch folder outside the repository:

- Resolving against the current directory fails 3 tests, including `test_target_resolves_relative_to_linking_file`.
- Keeping the fragment fails `test_fragment_is_ignored_when_resolving`.
- Fetching each http(s) target and ignoring the error passes all 8 tests (see the blocking finding).

## Acceptance criteria

Result is PASS or FAIL. Evidence is the name of a test or a file:line.

| AC | Result | Evidence |
|---|---|---|
| AC1 | PASS | `test_broken_relative_link_is_reported_with_file_and_line`, `test_fragment_is_ignored_when_resolving`, `test_output_follows_argument_then_line_order`; example/linkcheck/linkcheck.py:22-24, :52-56. The no-fragment-stripping mutant is caught. |
| AC2 | FAIL | The behavior is correct: example/linkcheck/linkcheck.py:5-7 imports only re, sys and pathlib, and :20-21 skips the four prefixes. The named check, `test_skipped_targets_are_not_reported_or_fetched` (example/linkcheck/tests/test_linkcheck.py:52-61), cannot see a fetch, so the "nothing is fetched" half of AC2 is not verified. See the blocking finding. |
| AC3 | PASS | `test_all_links_resolve_exits_zero_without_output`, `test_no_path_is_a_usage_error`, `test_missing_path_is_a_usage_error`; example/linkcheck/linkcheck.py:44-50. |
| Edge case | PASS | `test_target_resolves_relative_to_linking_file` changes the current directory to a folder holding `README.md` (test_linkcheck.py:85) and then to the fixture root (:88). The cwd-resolution mutant is caught. |

## Concerns

One row per row of the contract's Concerns table. Say whether you agree with the status the worker wrote.

| Concern | Worker status | Reviewer agrees | Note |
|---|---|---|---|
| Every acceptance criterion has a test | addressed | no | AC2's test does not detect a fetch (blocking finding). AC1, AC3 and the edge case do have working tests. |
| No file outside Owns changed | addressed | yes | `git diff main...t-001` touches `example/linkcheck/**`. Outside it, only the Concerns cells of contracts/T-001.md, `gates.json` and progress/T-001.md change. See the note on `gates.json`. |
| Edge cases from the spec handled | addressed | yes | linkcheck.py:23 resolves against `path.parent`. The test runs from two current directories. |
| No existing test or assertion weakened | addressed | yes | No file under gates/tests changed. 239 tests pass. |
| Errors give a message a user can act on | addressed | yes | linkcheck.py:45 prints usage and :49 names the missing path. A non-UTF-8 file is outside the spec (see the note). |
| No network access in the checker or its tests | addressed | yes | True of the code as written. The note's claim that the stub would fail the test is not true; that is the blocking finding. |
| Standard library only, Python 3.11 or later | addressed | yes | Imports re, sys, pathlib. Nothing newer than 3.11 is used. |
| Tests build their fixtures in a temporary directory and leave nothing behind | addressed | yes | TemporaryDirectory with addCleanup (test_linkcheck.py:25-26). The chdir cleanup is registered later, so it runs first (:84). |

## Findings

Rank each finding as blocking, should-fix or note. Give a file:line and a concrete failure case: an input and
what goes wrong.

- blocking: example/linkcheck/tests/test_linkcheck.py:58-61. The no-network test passes against a checker that fetches URLs, for two reasons:
  - Fetching `https://example.invalid/x` calls `socket.getaddrinfo` before `socket.socket`. The lookup fails there, so the stub is never called.
  - The stub reports by raising `self.fail(...)`. That raises AssertionError, which a checker's `except Exception` swallows.

  Failure cases, both run in a scratch copy with the unchanged test file, all 8 tests OK:
  - Before linkcheck.py:20, add `try: urllib.request.urlopen(target, timeout=2)` / `except Exception: pass` for http(s) targets.
  - Same insertion with `socket.create_connection(("127.0.0.1", 9))` instead.

  A fix within Owns that keeps the spec's method:
  - Stub both `socket.socket` and `socket.getaddrinfo` with a function that records the call and raises OSError.
  - After `run(f)`, assert that the list of recorded calls is empty.

  This version, tried in scratch, fails on both mutants with `['getaddrinfo', 'getaddrinfo'] != []` and passes on the current linkcheck.py.
- should-fix: example/linkcheck/linkcheck.py:33-37. No test checks the spec's directory output rules:
  - the display name: PATH `docs/` gives `docs/sub/a.md`, with one `/`;
  - files of one directory sorted by path.

  Failure case: replace `arg.rstrip("/")` with `arg` at :33. PATH `docs/` then prints `docs//sub/a.md`, and all 8 tests still pass. The only directory test (test_linkcheck.py:63-66) expects no output.

  The current code is correct: run as a script on a scratch fixture, `docs/` printed `docs/b.md:1: x.md`, then `docs/sub/a.md:1: nope.md`, then `docs/sub/a.md:1: gone.md#x`, with exit status 1. Add one test with a broken link in a nested file, passed as `docs/`, with two files to check the sort order.
- note: example/linkcheck/tests/test_linkcheck.py. No test checks "position in the line" ordering, which needs two links on one line. The code is correct (linkcheck.py:18 uses `finditer`). The spec does not name a test for it.
- note: example/linkcheck/linkcheck.py:16. A file that is not valid UTF-8 raises UnicodeDecodeError. Run as a script, that prints a traceback and exits 1, the same status as "broken links found". The spec does not define this case.
- note: gates.json. On main (8a7dcdd) T-001 is in `draft`. The human's `draft -> ready` record (2026-11-02T23:32:40Z, actor human) first enters git in the worker's build commit 66bbc7e, together with the worker's own `in_progress` records. This is not a worker change outside Owns. Whoever merges should know that the pull request also carries the spec approval record.

## Overall

Overall: FAIL
