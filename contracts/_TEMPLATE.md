# T-NNN — <title>

The planner copies this file to `contracts/T-NNN.md` and replaces every angle-bracket placeholder.

## Goal

<One paragraph: what changes for a user or a caller when this task is done, and why it is wanted.>

## Spec

<Link to the spec file, for example `specs/T-NNN.md`.>

## Owns

The path globs the worker may change. They must equal the `owns` the task was created with in the store.

- <glob, for example `example/linkcheck/**`>
- <glob>

## Acceptance criteria

Number each criterion and say how it is checked, by a named test or by a command and its expected output.

1. AC1: <observable behaviour>. Checked by: <test name or command>.
2. AC2: <observable behaviour>. Checked by: <test name or command>.

## Edge cases

- <Input or situation that is easy to get wrong, and the expected result.>

## Out of scope

- <What this task must not do, even if it looks related.>

## Checks

The exact commands the worker runs, and passes, before moving the task to review.

- `<command, for example python -m unittest discover -s gates/tests>`

## Escalate when

Stop and escalate, with `transition <id> escalated`, when any of these happens:

- A decision is needed that this contract did not make.
- A change is needed outside the paths in Owns.
- A check cannot pass without weakening a test or an assertion.

## Concerns

The worker fills the Status and Note cells of every row. The allowed statuses are exactly `addressed`,
`not applicable` and `deferred` (case-insensitive). A `deferred` row needs a note that says what is left and why.
The planner may add rows; nobody removes one.

| Concern | Status | Note |
|---|---|---|
| Every acceptance criterion has a test | | |
| No file outside Owns changed | | |
| Edge cases from the spec handled | | |
| No existing test or assertion weakened | | |
| Errors give a message a user can act on | | |
