# Verdict — T-NNN, cycle N

The reviewer copies this file to `reviews/T-NNN-verdict-cN.md`, one file per review cycle. Once a transition cites
the file, it is never edited; the next cycle gets a new file.

## Reviewed

- Pull request: <number, or "none yet" when the review runs before the pull request is opened>
- Head commit: <the full 40-character SHA of the commit reviewed; a comment may follow it>
- Contract: <contracts/T-NNN.md>

The command line reads the Head commit and Contract lines: each must appear once, the head as a full SHA and the
contract as the task's own contract path. A passing review records the head; the merge go is refused if the pull
request later changes anything but records.

## Acceptance criteria

Result is PASS or FAIL. Evidence is the name of a test or a file:line.

| AC | Result | Evidence |
|---|---|---|
| AC1 | <PASS or FAIL> | <test name or file:line> |
| AC2 | <PASS or FAIL> | <test name or file:line> |

## Concerns

One row per row of the contract's Concerns table. Say whether you agree with the status the worker wrote.

| Concern | Worker status | Reviewer agrees | Note |
|---|---|---|---|
| <concern text> | <status> | <yes or no> | <why, if no> |

## Findings

Rank each finding as blocking, should-fix or note. Give a file:line and a concrete failure case: an input and
what goes wrong.

- blocking: <file:line> — <failure case>
- should-fix: <file:line> — <failure case>
- note: <file:line> — <observation>

## Overall

Replace the line below with exactly `Overall: PASS` or `Overall: FAIL`, alone on its line, once in this file.
Any blocking finding means FAIL. While the placeholder is in place the file is refused.

Overall: <PASS or FAIL>
