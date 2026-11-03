# Verdict — T-002, cycle 2

## Reviewed

- Pull request: none yet (review cycle 2 runs before the pull request is opened)
- Head commit: 697d8b2bd774a926c4ee72ee1cd7aec9c6da68f9 (work 7a75d7c; 697d8b2 only records the in_review move in `gates.json`)
- Contract: contracts/T-002.md

Checks run by the reviewer on that head, Python 3.12.7:

- `python -m unittest discover -s example/linkcheck/tests`: Ran 12 tests, OK
- `python -m unittest discover -s gates/tests`: Ran 239 tests, OK

The test file at that head was run against mutated copies of `linkcheck.py`, made in a scratch folder outside the
repository, with the test file unchanged. The unmutated copy passes all 12 tests. Each mutant fails:

- The skip line naming the file by `{path}` (linkcheck.py:61): fails `test_file_that_is_not_utf8_is_skipped_and_named`
  only (caught by the `./docs/a.md` assertion, test_linkcheck.py:131-134). This mutant survived cycle 1.
- The skip line naming the file by `{path.resolve()}`: fails the AC4 test only (caught at test_linkcheck.py:116-123).
  This mutant survived cycle 1.
- The skip line naming the file by `{path.name}` or by `{arg}`: fail the AC4 test only.
- Without `sorted(...)` (linkcheck.py:37): fails `test_directory_files_are_reported_in_sorted_order` only.
- `sorted(..., reverse=True)`: fails the sort test and the T-001 test `test_directory_files_are_named_from_the_argument_and_sorted`.
- Links of one line in reverse order (linkcheck.py:18): fails `test_links_on_one_line_follow_their_position` only.
- Exit status 1 instead of 2 when a file is skipped and links are also broken (linkcheck.py:67): fails the AC4 test.
- Plain `return status` (a folder holding only a bad file exits 0): fails the AC4 test.
- No working `except UnicodeDecodeError` (the checker crashes): errors in the AC4 test.
- The skip line on standard output instead of standard error: fails the AC4 test.
- `break` instead of `continue` after the skip (linkcheck.py:63): fails the AC4 test.

## Acceptance criteria

Result is PASS or FAIL. Evidence is the name of a test or a file:line.

| AC | Result | Evidence |
|---|---|---|
| AC4 | PASS | linkcheck.py:58-63 catches UnicodeDecodeError per file, prints `linkcheck: {name}: not valid UTF-8, skipped` to standard error and goes on; :67 returns 2 over 1. `test_file_that_is_not_utf8_is_skipped_and_named` (test_linkcheck.py:108-134) now changes to the fixture root and asserts SPEC AC4's exact triple for PATH `docs`: `(2, "docs/b.md:1: missing.md\n", "linkcheck: docs/a.md: not valid UTF-8, skipped\n")`. It also asserts the edge cases: a folder holding only a bad file gives `(2, "", "linkcheck: only/a.md: ...")`, and `./docs/a.md` passed directly is named `./docs/a.md`, as given. Every naming mutant above fails it. |
| AC5a | PASS | `test_links_on_one_line_follow_their_position` (test_linkcheck.py:136-140): three links on one line, not in alphabetical order. The reverse mutant is caught. |
| AC5b | PASS | `test_directory_files_are_reported_in_sorted_order` (test_linkcheck.py:142-150) now patches `Path.rglob` to return the ten files in the fixed order m q c z a k t e x b. So the order no longer depends on the filesystem's listing, and a checker without the sort fails on any filesystem. The no-sort mutant is caught. Progress note 17:14:12Z records the same scratch check. |
| AC1-AC3 and edge case | PASS | All nine T-001 tests pass unchanged. `git diff main...t-002 -- example/linkcheck/tests/` removes no line; it only appends the three T-002 tests. |

## Concerns

One row per row of the contract's Concerns table. Say whether you agree with the status the worker wrote.

| Concern | Worker status | Reviewer agrees | Note |
|---|---|---|---|
| Every acceptance criterion has a test | addressed | yes | All three named tests exist, pass, and each fails against its mutant. |
| No file outside Owns changed | addressed | yes | Worker commits 0caa261 and 7a75d7c change example/linkcheck/linkcheck.py and tests/test_linkcheck.py, only the Concerns cells of contracts/T-002.md, gates.json and progress/T-002.md. SPEC.md and the rest of the contract changed only in the planner's commit ffc097f. reviews/T-002-verdict-c1.md was added by the reviewer's commit 3c91632. |
| Edge cases from the spec handled | addressed | yes | Both contract edge cases are asserted with relative names (test_linkcheck.py:124-134). The `{path}` and `{path.resolve()}` mutants now fail. The cycle 1 disagreement is resolved. |
| No existing test or assertion weakened | addressed | yes | No T-001 test changed. The retry keeps every T-002 assertion and makes each one stricter: the three AC4 assertions compare the same full triples, but with relative names that tell "as given" apart from a normalised name, and the sort test compares the same expected text in a deterministic order. Nothing under gates/tests changed. |
| Errors give a message a user can act on | addressed | yes | The stderr line names the file as given and says why it was skipped. |
| Existing tests pass unchanged | addressed | yes | 12 and 239 tests OK on head 697d8b2, run by the reviewer. |
| A test that names an order fails against a checker without that order | addressed | yes | Reproduced: the no-sort mutant fails the sort test and the reverse-in-line mutant fails the line test. Because of the `Path.rglob` patch, the sort result no longer depends on the filesystem. |

## Findings

Rank each finding as blocking, should-fix or note. Give a file:line and a concrete failure case: an input and
what goes wrong.

- note: example/linkcheck/tests/test_linkcheck.py:113-115 and :149. The chdir and the patch are both cleaned up. `self.addCleanup(os.chdir, old)` is registered before `os.chdir`. Cleanups run last in, first out, so the old directory is restored before the TemporaryDirectory cleanup from setUp deletes the fixture, and that happens even when an assertion fails. The `mock.patch.object(Path, "rglob", ...)` is a `with` block, so it is undone on exit or on failure. In the mutant runs, a failing AC4 or sort test did not make any other test fail.
- note: example/linkcheck/tests/test_linkcheck.py:149. The patched `rglob` ignores its pattern, so this test no longer exercises the real `Path.rglob("*.md")`. That is intended: it pins the sort only. The real recursive search is still covered by the T-001 tests `test_directory_files_are_named_from_the_argument_and_sorted` and `test_all_links_resolve_exits_zero_without_output`.
- note: example/linkcheck/tests/test_linkcheck.py:131-134. A `{path}` naming mutant is caught only by the `./docs/a.md` assertion, because `str(Path("docs") / "a.md")` equals the given name for PATH `docs`. One assertion is enough to catch it. No change is required.

## Overall

Overall: PASS
