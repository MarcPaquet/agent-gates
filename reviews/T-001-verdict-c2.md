# Verdict — T-001, cycle 2

## Reviewed

- Pull request: none yet (review cycle 2 runs before the pull request is opened)
- Head commit: 156518fbb5d1538087a333d2a7dc2eadc02120e2 (retry 2fe757f, which changes only the test file; 156518f only records the in_review move in `gates.json`)
- Contract: contracts/T-001.md

Checks run by the reviewer on that head, Python 3.12.7:

- `python -m unittest discover -s example/linkcheck/tests`: Ran 9 tests, OK
- `python -m unittest discover -s gates/tests`: Ran 239 tests, OK

The test file was also run against mutated copies of `linkcheck.py`, made in a scratch folder outside the repository:

- Fetching each http(s) target with `urllib.request.urlopen` and ignoring the error: `test_skipped_targets_are_not_reported_or_fetched` fails, recorded call `('example.invalid', 443, ...)`.
- Same, with `socket.create_connection(("127.0.0.1", 9))`: the same test fails, recorded call `('127.0.0.1', 9, ...)`.
- `arg.rstrip("/")` replaced with `arg` at linkcheck.py:33: `test_directory_files_are_named_from_the_argument_and_sorted` fails on `docs//b.md`.
- `sorted(..., reverse=True)` at linkcheck.py:37: the directory test fails.
- `sorted(...)` removed at linkcheck.py:37: all 9 tests pass (see the should-fix finding).

## Acceptance criteria

Result is PASS or FAIL. Evidence is the name of a test or a file:line.

| AC | Result | Evidence |
|---|---|---|
| AC1 | PASS | `test_broken_relative_link_is_reported_with_file_and_line`, `test_fragment_is_ignored_when_resolving`, `test_output_follows_argument_then_line_order` (test_linkcheck.py:35-50); example/linkcheck/linkcheck.py:22-24, :52-56. Unchanged since cycle 1, where the fragment and cwd mutants were caught. |
| AC2 | PASS | `test_skipped_targets_are_not_reported_or_fetched` (test_linkcheck.py:52-69) now stubs `socket.socket` and `socket.getaddrinfo` with a recorder that raises OSError, then asserts no call was recorded (:69), outside the patched block so a checker's `except` cannot hide it. Both fetching mutants fail it. linkcheck.py:5-7 imports only re, sys, pathlib; :20-21 skips the four prefixes. |
| AC3 | PASS | `test_all_links_resolve_exits_zero_without_output`, `test_no_path_is_a_usage_error`, `test_missing_path_is_a_usage_error` (test_linkcheck.py:81-95); linkcheck.py:44-50. |
| Edge case | PASS | `test_target_resolves_relative_to_linking_file` (test_linkcheck.py:97-106) runs from a folder holding `README.md` and from the fixture root; linkcheck.py:23 resolves against `path.parent`. |

## Concerns

One row per row of the contract's Concerns table. Say whether you agree with the status the worker wrote.

| Concern | Worker status | Reviewer agrees | Note |
|---|---|---|---|
| Every acceptance criterion has a test | addressed | yes | Each AC and the edge case has a named test that a matching mutant fails. The note's claim that directory "order" has its own test is only partly true: reversed order is caught, no sorting is not (should-fix finding). That gap is a spec rule, not a contract AC. |
| No file outside Owns changed | addressed | yes | `git diff main...t-001` touches `example/linkcheck/**`; outside it only the Concerns cells of contracts/T-001.md, `gates.json`, progress/T-001.md and reviews/T-001-verdict-c1.md. The retry diff `9555550..156518f` changes the test file, one Concerns cell, `gates.json` and the progress log. |
| Edge cases from the spec handled | addressed | yes | linkcheck.py:23; test runs from two current directories. |
| No existing test or assertion weakened | addressed | yes | The retry replaces the AC2 test body with a stricter one (adds `getaddrinfo` and an explicit empty-calls assertion); nothing is removed. No file under gates/tests changed; 239 tests pass. |
| Errors give a message a user can act on | addressed | yes | linkcheck.py:45 prints usage; :49 names the missing path. |
| No network access in the checker or its tests | addressed | yes | True of the code. The note still says the test "stubs socket.socket"; it now stubs `getaddrinfo` too, which is stronger, so the note understates rather than overstates. |
| Standard library only, Python 3.11 or later | addressed | yes | Imports re, sys, pathlib; nothing newer than 3.11. |
| Tests build their fixtures in a temporary directory and leave nothing behind | addressed | yes | TemporaryDirectory with addCleanup (test_linkcheck.py:25-26); chdir cleanup registered later, so it runs first (:102). `git status` was clean after the Checks. |

## Findings

Rank each finding as blocking, should-fix or note. Give a file:line and a concrete failure case: an input and
what goes wrong.

- Cycle 1 blocking finding (AC2 test could not see a fetch): fixed. Both mutants named in cycle 1 now fail the test.
- Cycle 1 should-fix (no test of the directory display name): fixed. The `rstrip` mutant now fails the new test.
- should-fix: example/linkcheck/tests/test_linkcheck.py:71-79. The test named `..._and_sorted` does not detect a checker that does not sort. Its fixture (`docs/b.md`, `docs/sub/a.md`) lists in the same order whether sorted or not, because `rglob` yields a directory's own files before its subfolders. Failure case: remove `sorted(...)` at linkcheck.py:37; all 9 tests pass. On a fixture `docs/a/x.md` + `docs/b.md` that mutant prints `docs/b.md` first, while the current code correctly prints `docs/a/x.md` first. A fixture with a subfolder that sorts before a top-level file would catch it. Not blocking: the current code is correct, and the contract's ACs do not name this rule.
- note: example/linkcheck/tests/test_linkcheck.py. Still no test of "position in the line" order (two broken links on one line). linkcheck.py:18 uses `finditer`, which is correct. Left out of this retry by decision; the spec names no test for it.
- note: example/linkcheck/linkcheck.py:16. A non-UTF-8 file raises UnicodeDecodeError; run as a script it prints a traceback and exits 1, the same status as "broken links found". The spec does not define the case. Left out of this retry by decision.
- note: gates.json. As in cycle 1, the human's `draft -> ready` record (2026-11-02T23:32:40Z) first enters git on this branch. Whoever merges should know the pull request also carries the spec approval record.

## Overall

Overall: PASS
