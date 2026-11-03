# Verdict — T-002, cycle 1

## Reviewed

- Pull request: none yet (review cycle 1 runs before the pull request is opened)
- Head commit: 0960619e3675772a47e03669f8fc5b11106bf247 (work 0caa26119fa9004ccfbecfa5f46942039a64abdf; 0960619 only records the in_review move in `gates.json`)
- Contract: contracts/T-002.md

Checks run by the reviewer on that head, Python 3.12.7:

- `python -m unittest discover -s example/linkcheck/tests`: Ran 12 tests, OK
- `python -m unittest discover -s gates/tests`: Ran 239 tests, OK

Each test was also run against mutated copies of `linkcheck.py`, made in a scratch folder outside the repository,
with the unchanged test file:

- Without `sorted(...)` at linkcheck.py:37: fails `test_directory_files_are_reported_in_sorted_order` only.
- Links of one line in reverse order (linkcheck.py:18): fails `test_links_on_one_line_follow_their_position` only.
- Exit status 1 instead of 2 when a file is skipped and links are also broken (linkcheck.py:67): fails
  `test_file_that_is_not_utf8_is_skipped_and_named`.
- No `except UnicodeDecodeError` (the checker crashes): errors in `test_file_that_is_not_utf8_is_skipped_and_named`.
- `break` instead of `continue` after the skip (linkcheck.py:63), and the skip line on standard output: both fail the
  AC4 test.
- The skip line naming the file by `path.name` or by `arg`: both fail the AC4 test.
- The skip line naming the file by `str(path)` or by `path.resolve()` instead of `name` (linkcheck.py:61): all 12
  tests pass. See the blocking finding.

## Acceptance criteria

Result is PASS or FAIL. Evidence is the name of a test or a file:line.

| AC | Result | Evidence |
|---|---|---|
| AC4 | FAIL | The behaviour is correct: example/linkcheck/linkcheck.py:58-63 catches UnicodeDecodeError per file, prints `linkcheck: {name}: not valid UTF-8, skipped` to standard error and goes on; :67 returns 2 over 1. `test_file_that_is_not_utf8_is_skipped_and_named` (test_linkcheck.py:108-132) checks the skip, the other file's report, exit 2, the folder with only a bad file, and a bad file passed directly. It does not use the PATH and exact outputs of SPEC AC4 that contract AC1 requires (`docs` giving `docs/b.md:1: missing.md` and `linkcheck: docs/a.md: ...`): every PATH is an absolute resolved path (:113, :130). So the "named as given" half is not verified. See the blocking finding. |
| AC5a | PASS | `test_links_on_one_line_follow_their_position` (test_linkcheck.py:134-138): three links on one line, not in alphabetical order. The reverse mutant is caught. |
| AC5b | PASS | `test_directory_files_are_reported_in_sorted_order` (test_linkcheck.py:140-146): ten files written in the order m q c z a k t e x b. The no-sort mutant is caught on this machine (APFS), and the worker's progress note reports the same. Linux CI is covered in a note below. |
| AC1-AC3 and edge case | PASS | All nine T-001 tests pass unchanged. `git diff main...t-002 -- example/linkcheck/tests/` adds 40 lines and removes none. |

## Concerns

One row per row of the contract's Concerns table. Say whether you agree with the status the worker wrote.

| Concern | Worker status | Reviewer agrees | Note |
|---|---|---|---|
| Every acceptance criterion has a test | addressed | yes | Each named test exists and passes. The AC4 test is too weak on naming (blocking finding), but it is there. |
| No file outside Owns changed | addressed | yes | The worker's commit 0caa261 changes example/linkcheck/linkcheck.py and tests/test_linkcheck.py, the Concerns cells of contracts/T-002.md, gates.json and progress/T-002.md. SPEC.md and the rest of the contract changed only in the planner's commit ffc097f. |
| Edge cases from the spec handled | addressed | no | Handled in code, yes. "Asserted" is only partly true: the bad file passed directly is an absolute resolved path (test_linkcheck.py:130), so `arg`, `str(Path(arg))` and `Path(arg).resolve()` all print the same text. The test cannot tell "named as given" apart from a normalised name. |
| No existing test or assertion weakened | addressed | yes | The test diff adds lines only. No file under gates/tests changed. |
| Errors give a message a user can act on | addressed | yes | The stderr line names the file and says why it was skipped. |
| Existing tests pass unchanged | addressed | yes | 12 and 239 tests OK on head 0960619, run by the reviewer. |
| A test that names an order fails against a checker without that order | addressed | yes | Reproduced for both orders: the no-sort and reverse-in-line mutants each fail their named test. See the Linux note. |

## Findings

Rank each finding as blocking, should-fix or note. Give a file:line and a concrete failure case: an input and
what goes wrong.

- blocking: example/linkcheck/tests/test_linkcheck.py:113-132. The AC4 test passes every PATH as an absolute resolved
  path, not SPEC AC4's `docs`. Contract AC1 asks for "the fixture and exact outputs of SPEC AC4". The contract's edge
  case also asks that a file passed directly be "named as given". Neither is verified.

  Failure case: in a scratch copy, change linkcheck.py:61 to print `{path}` or `{path.resolve()}` instead of
  `{name}`. All 12 tests pass. Run as a script from the fixture root:
  - `{path}` mutant, PATH `./docs`: stdout says `./docs/b.md:1: missing.md`, but stderr says `linkcheck: docs/a.md: ...`.
  - `{path}` mutant, PATH `./docs/a.md`: stderr says `docs/a.md`.
  - `{path.resolve()}` mutant, PATH `docs`: stderr names the absolute `/private/var/.../docs/a.md`.

  The current code is correct for all three PATHs. Fix within Owns: in the AC4 test, change to the fixture root (with
  `addCleanup(os.chdir, old)`, as at test_linkcheck.py:101-103). Then pass `docs` and assert exactly
  `(2, "docs/b.md:1: missing.md\n", "linkcheck: docs/a.md: not valid UTF-8, skipped\n")`. Then pass `./docs/a.md`
  and assert the stderr names `./docs/a.md`. Tried in scratch, this fails both surviving mutants and passes on the
  current linkcheck.py.
- note: example/linkcheck/tests/test_linkcheck.py:140-146. The sort test depends on directory listing order. It catches
  a checker without the sort only when `Path.rglob` returns these ten names unsorted. On Linux CI (ubuntu-latest,
  `/tmp` on ext4 with dir_index), a one-block directory is read in hash order, and the hash is seeded per
  filesystem. The chance that this order is sorted is 1 in 10! (about 3e-7). tmpfs returns reverse creation order
  (b x e t k a z c q m), and btrfs and XFS short-form directories return creation order. Neither is sorted. So
  the test is sound on CI in practice. It is not deterministic in principle: on HFS+, which lists names sorted, the
  no-sort mutant would pass. A deterministic form would patch `Path.rglob` in the test to return the names in a fixed
  unsorted order.
- note: gates.json. As in T-001, the human's `draft -> ready` record (2026-11-03T17:05:09Z) first enters git in the
  worker's commit 0caa261, together with the planner's dispatch record. This is not a worker change outside Owns.

## Overall

Overall: FAIL
