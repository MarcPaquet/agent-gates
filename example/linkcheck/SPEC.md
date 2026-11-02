# linkcheck: a Markdown link checker

Reports inline links in Markdown files whose relative target does not exist. Python 3.11 or later, standard
library only, no network access.

## Interface

- Module `example/linkcheck/linkcheck.py`, run as `python example/linkcheck/linkcheck.py PATH...`. A PATH is a
  file, checked whatever its extension, or a directory searched recursively for `*.md` files.
- The module exposes `main(argv: list[str]) -> int`: `argv` is the list of PATH arguments (no program name), the
  return value is the exit status, and output goes to `sys.stdout` and `sys.stderr`. Run as a script, it calls
  `sys.exit(main(sys.argv[1:]))`.
- A link is an inline link `[text](target)`; its target is the text between `(` and the next `)`. Files are read
  as UTF-8; line numbers start at 1.
- The file part of a target is the text before the first `#` (the whole target when there is none). A link is
  broken when its file part, resolved relative to the directory of the file that contains the link, exists
  neither as a file nor as a directory.
- Targets that start with `http://`, `https://`, `mailto:` or `#` are skipped: never reported, never fetched.
- Output: one line per broken link, `<file>:<line>: <target>`, on standard output. `<file>` is the PATH as given,
  or, for a file found in a directory, the directory as given joined with the file's path below it with one `/`
  (PATH `docs` or `docs/` gives `docs/sub/a.md`). `<target>` is the target as written, fragment included. Order: PATH arguments in the
  order given, files of one directory sorted by path, then line number, then position in the line.
- Exit status: 0 when no link is broken, 1 when at least one is, 2 on a usage error.
- Tests live in `example/linkcheck/tests/test_*.py` and run from the repository root with
  `python -m unittest discover -s example/linkcheck/tests`. They import the module as
  `from example.linkcheck import linkcheck` (a namespace package; no `__init__.py`) and build their fixtures in a
  temporary directory.

## Acceptance criteria

1. AC1: Broken relative links are reported. For a file with `[a](missing.md)` on line 3 and
   `[b](missing.md#intro)` on line 5, the output is exactly `<file>:3: missing.md` then `<file>:5: missing.md#intro`
   and the exit status is 1. A link `[c](present.md#nowhere)` to an existing `present.md` is not reported. Across
   two PATH arguments, lines follow the order of the arguments.
2. AC2: Skipped targets are never reported and nothing is fetched. A file holding only `[w](https://example.invalid/x)`,
   `[h](http://example.invalid/y)`, `[m](mailto:someone@example.invalid)` and `[s](#section)` gives no output and
   exit status 0, and no network connection is attempted (the test replaces `socket.socket` with a stub that fails
   the test when called).
3. AC3: Clean runs and usage errors. A directory whose links all resolve, or that holds no links, gives no output
   and exit status 0. No PATH, or a PATH that does not exist, gives exit status 2, nothing on standard output, and a
   message on standard error (naming the missing PATH when there is one).

## Edge case

- A target is resolved relative to the directory of the linking file, not the current directory. With
  `docs/guide.md` holding `[up](../README.md)` and `README.md` at the fixture root, nothing is reported; a link
  `[x](README.md)` in `docs/guide.md` is reported even when the current directory holds a `README.md`. The result
  is the same whatever the current directory is.

## Out of scope

- Reference-style links (`[text][label]`), images (`![alt](path)`; tests must not depend on how they are
  treated), autolinks and HTML links (`<a href>`).
- Checking anchors inside files: `present.md#nowhere` passes when `present.md` exists.
- Ignoring links inside fenced code blocks or inline code: they are checked like any other line.
- Absolute paths, other URL schemes, link titles (`[a](b "title")`), angle-bracket targets, percent-encoding.
