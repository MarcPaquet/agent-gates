"""gate-guard: the rules of the Claude Code PreToolUse hook that keeps agents inside the gates.

This is wall 1b of the three walls described in gates/approve.py. gate-guard.sh, next to this file, runs it with
the hook event as JSON on standard input. The guard gives one of three outcomes:

- deny: exit 2 and one line on standard error, `gate-guard: refused: <what> — <what to do instead>`. Claude Code
  blocks the call and shows the line to the agent.
- ask: exit 0 and one JSON object on standard output whose permissionDecision is "ask" and whose reason is
  `gate-guard: cannot read this command: <why>`. Claude Code asks the human to confirm the call.
- pass: exit 0 and no output. The user's own permission rules decide, as if the guard were not there.

The guard never answers "allow": that would skip the user's own permission prompts. Any fault inside this program
denies (fail closed). The guard stops accidental and obvious routes; it is not a sandbox against an adversarial
agent. .claude/README.md lists the rules with examples, and the limits.

A shell command is split into parts on ; && || | & ( ) and newlines with shlex, quotes respected. A command passes
only when every part runs a program on the allowlist (ALLOWLIST, the first table below), compared without case,
with arguments that pass that program's checks; any other part asks. The deny rules look at every part, on the
allowlist or not, and the strictest outcome over all rules and parts wins.

Standard library only; Python 3.11 or later.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable

# --- Rule 8: the allowlist ------------------------------------------------------------------------------------

READ_ONLY = "read-only"
FILE_WRITER = "file writer"
FOLDER_CHANGE = "folder change"
GIT = "git"
GITHUB_CLI = "gh"
PYTHON = "python"

ALLOWLIST: dict[str, str] = {
    # Programs that only read files and print. sort asks on -o, -T and --compress-program; uniq asks on a
    # second operand, which it writes.
    "ls": READ_ONLY, "cat": READ_ONLY, "head": READ_ONLY, "tail": READ_ONLY, "wc": READ_ONLY,
    "grep": READ_ONLY, "diff": READ_ONLY, "pwd": READ_ONLY, "echo": READ_ONLY, "printf": READ_ONLY,
    "true": READ_ONLY, "test": READ_ONLY, "sort": READ_ONLY, "uniq": READ_ONLY, "cut": READ_ONLY,
    "tr": READ_ONLY, "stat": READ_ONLY, "du": READ_ONLY, "date": READ_ONLY, "basename": READ_ONLY,
    "dirname": READ_ONLY, "which": READ_ONLY,
    # Programs that write the files they name: rules 3, 4 and 7 check every operand, everything inside a folder
    # operand, and folder/name for each source copied, moved or linked into a folder. -t asks.
    "mkdir": FILE_WRITER, "touch": FILE_WRITER, "cp": FILE_WRITER, "mv": FILE_WRITER, "rm": FILE_WRITER,
    "ln": FILE_WRITER,
    # One plain folder; later parts resolve their paths against it.
    "cd": FOLDER_CHANGE,
    # The subcommands in GIT_SUBCOMMANDS; rule 5 checks push, merge, rebase and pull.
    "git": GIT,
    # The commands in GH_SUBCOMMANDS; rule 6 checks gh pr merge and gh api.
    "gh": GITHUB_CLI,
    # python -m unittest, python -m gates.cli, or python FILE.py with FILE.py an existing file in the project.
    "python": PYTHON, "python3": PYTHON,
}
"""The programs a shell command may run without a prompt from the guard, by name in lower case, with the kind of
check their arguments get. Every other program asks; .claude/README.md lists this table."""

GIT_SUBCOMMANDS = (
    "status", "log", "diff", "show", "add", "commit", "checkout", "switch", "branch", "fetch", "pull", "push",
    "merge", "rebase", "restore", "rev-parse", "merge-base", "remote", "stash",
)
"""git subcommands that pass, with these exceptions, which ask: git remote other than `git remote [-v]`, git stash
other than `git stash list`; git branch -m -M -c -C -u -t; git checkout or switch to another commit (creating a
branch where you stand, or git checkout ... -- PATHS, passes); a pathspec pattern or magic in checkout or restore;
--pathspec-from-file; git rebase -x, --onto or with a branch argument; fetch and pull --upload-pack; push
--receive-pack and --exec."""
GIT_ALLOWED_OPTIONS = ("--no-pager", "-P")
"""git's own options, before the subcommand, that pass. Any other (-C, -c, --git-dir, --work-tree, ...) asks."""
GH_SUBCOMMANDS = {
    "pr": ("view", "diff", "list", "checks", "status", "create", "merge"),
    "repo": ("view",),
    "run": ("view", "list", "watch"),
}
"""gh commands that pass, by group. gh api passes too, with no second word, and only as a GET (rule 6)."""
PYTHON_MODULES = ("unittest", "gates.cli")
"""The modules python -m may run."""

# Argument checks of the allowlist. Long options also match an abbreviation (git and getopt_long accept --mir
# for --mirror); short letters match inside a cluster (-fM holds M).
SORT_WRITE_OPTIONS = ("--output", "--temporary-directory", "--compress-program")
SORT_WRITE_LETTERS = "oT"
UNIQ_OPTIONS_WITH_VALUE = ("-f", "-s", "--skip-fields", "--skip-chars")
TARGET_FOLDER_OPTIONS = ("--target-directory",)
TARGET_FOLDER_LETTERS = "t"
COPYING_PROGRAMS = ("cp", "mv", "ln")
CD_OPTIONS = ("-L", "-P", "-e", "-@", "-q", "-s")
GIT_BRANCH_RENAME_OPTIONS = ("--move", "--copy")
GIT_BRANCH_RENAME_LETTERS = "mMcC"
GIT_UPSTREAM_OPTIONS = ("--set-upstream-to", "--set-upstream", "--track")
GIT_UPSTREAM_LETTERS = "ut"
GIT_RUN_OPTIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "rebase": (("--exec",), "x"),
    "fetch": (("--upload-pack",), ""),
    "pull": (("--upload-pack",), ""),
    "push": (("--receive-pack", "--exec"), ""),
}
"""Options of a git subcommand that run a command the guard does not read: long names, short letters."""
GIT_CREATE_BRANCH_OPTIONS = ("-b", "-B", "-c", "-C", "--orphan", "--create", "--force-create")
"""Options of git checkout and git switch that create a branch; each takes the new branch's name."""
GIT_PATHSPEC_PATTERN_CHARACTERS = "*?["
"""git reads a pathspec as a pattern, even inside quotes, where the shell does not expand it."""

# --- Names the rules use --------------------------------------------------------------------------------------

STORE_NAME = "gates.json"
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
"""Tools that must carry a command. Any other tool whose input has a `command` string (Monitor, for one) is read
as shell too; Monitor can also watch a WebSocket, with no command."""

GIT_TIMEOUT_SECONDS = 3
"""The guard runs git at most once per event (to read the branch), so it stays far below the hook timeout."""
NESTING_LIMIT = 3
"""How deep the guard reads commands nested in bash -c '...' or eval before it stops and asks."""
HOOK_SETUP = "check the PreToolUse hook entry in .claude/settings.json"

# What each protected path is, and what the agent should do instead of writing it.
STORE = "the store"
PROJECT_FOLDER = "a folder that holds the store"
PROGRESS = "a progress log"
VERDICT = "a recorded verdict"
ADAPTER = "the Claude Code adapter or settings"
GIT_SETUP = "git's own files or configuration (hooks, config)"
STORE_UNREADABLE = "a project file while the store cannot be read"
INSTEAD = {
    STORE: "change tasks with python gates/cli.py; the human runs gates/approve.py",
    PROJECT_FOLDER: "name the files you mean, outside gates.json, progress/ and recorded verdicts",
    PROGRESS: "add an entry with python gates/cli.py note ID --actor ROLE TEXT",
    VERDICT: "write the next cycle's verdict as a new file, reviews/T-NNN-verdict-cN.md with the next N",
    ADAPTER: "the human changes .claude/ and the Claude Code settings outside the agent session",
    GIT_SETUP: "use git commands; the human changes git hooks and configuration outside the agent session",
    STORE_UNREADABLE: "ask the human to restore gates.json from git; the guard reads the recorded verdicts from it",
}

# Shell reading.
OPERATOR_CHARACTERS = ";&|()<>\n"
"""Characters shlex splits off as operators: the separators ; & | ( ) and newline, and the redirections < >."""
PLAIN_WRITE_REDIRECTIONS = (">", ">>")
PLAIN_READ_REDIRECTIONS = ("<",)
DUPLICATIONS = (">&", "<&")
"""With a file descriptor number or - after them they copy or close a descriptor; with a word they name a file."""
TEXT_REDIRECTIONS = ("<<", "<<<")
"""After << and <<< comes a here-document delimiter or a string, not a file."""
OTHER_REDIRECTIONS = (">|", ">>|", "&>", "&>>", "<>")
"""Redirections the guard does not read (with >&WORD, <&WORD and zsh's >! and >>!): they ask, and their target is
still checked as a write."""
REDIRECTIONS = (
    PLAIN_WRITE_REDIRECTIONS + PLAIN_READ_REDIRECTIONS + DUPLICATIONS + TEXT_REDIRECTIONS + OTHER_REDIRECTIONS
)
GLOB_AND_COMMENT_CHARACTERS = "*?[]{}#"
SHELL_KEYWORDS = ("if", "then", "else", "elif", "do", "while", "until", "!", "time")
"""Words that only introduce the command after them; the guard drops them (and time's options) and reads that
command."""
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "fish")

# Rules 3, 4 and 7: shell commands that write the paths they name. Programs off the allowlist ask anyway; these
# are read so that a write of a protected path denies.
WRITING_PROGRAMS = (
    "rm", "rmdir", "unlink", "shred", "cp", "mv", "ln", "mkdir", "touch", "tee", "truncate", "install", "rsync",
    "dd", "chmod", "chown",
)
IN_PLACE_EDITORS = ("sed", "perl", "ruby")
GIT_WRITE_SUBCOMMANDS = ("checkout", "restore", "rm", "mv")
GIT_READ_SUBCOMMANDS = ("add", "diff", "show", "log", "status")

# Rule 5: git.
GIT_OPTIONS_WITH_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix")
GIT_DIRECTORY_OPTIONS = ("-C", "--git-dir", "--work-tree")
GIT_CONFIG_OPTIONS = ("-c", "--config-env")
GIT_BRANCH_CHANGES = ("checkout", "switch", "rebase", "bisect", "symbolic-ref", "update-ref", "worktree")
"""Subcommands that can change which branch is checked out (git branch -m/-M/-c/-C too)."""
GIT_CONFIG_CHANGES = ("config", "remote")
GIT_MERGING_SUBCOMMANDS = ("merge", "rebase", "pull")
PUSH_OPTIONS_WITH_VALUE = ("-o", "--push-option", "--repo", "--receive-pack", "--exec")
PUSH_ALL_BRANCHES = ("--all", "--mirror", "--branches")
REBASE_OPTIONS_WITH_VALUE = ("--onto", "-s", "--strategy", "-X", "--strategy-option", "-x", "--exec")
ASK_OTHER_REPOSITORY = (
    "git -C, --git-dir or --work-tree points git at another repository, and the guard reads the branch only in "
    "the working folder"
)

# Rule 6: GitHub.
MERGE_FLAGS = ("--squash", "-s", "--merge", "-m", "--rebase", "-r", "--delete-branch", "-d", "--auto")
MERGE_FORM = "use gh pr merge N [--squash|--merge|--rebase] [--delete-branch] [--auto] --match-head-commit SHA"
MERGE_INSTEAD = "merge with gh pr merge N --squash --match-head-commit SHA after the human's go"
API_MERGE = re.compile(
    r"pulls/[^\s/]+/merge\b|/merges\b|mergepullrequest|automerge|git/refs/heads/main\b"
)
"""Merges and changes to main through the GitHub API, matched in lower case."""
HEAD_SHA = re.compile(r"[0-9a-f]{40}")
GH_API_OPTIONS_WITH_VALUE = (
    "-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field", "--input",
    "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview",
)
GH_API_BODY_OPTIONS = ("-f", "--raw-field", "-F", "--field", "--input")
"""gh api sends POST when one of these is present and no method is given."""


# --- Outcomes -------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Outcome:
    """One answer of the guard: `kind` is "pass", "ask" or "deny"; `message` is the line shown for ask and deny."""

    kind: str
    message: str = ""


PASS = Outcome("pass")
SEVERITY = {"pass": 0, "ask": 1, "deny": 2}


def one_line(text: str) -> str:
    """Return `text` with every run of whitespace, newlines included, turned into one space."""
    return " ".join(text.split())


def deny(what: str, instead: str) -> Outcome:
    """Return a deny outcome: `what` is refused, `instead` says what to do."""
    return Outcome("deny", one_line(f"gate-guard: refused: {what} — {instead}"))


def ask(why: str) -> Outcome:
    """Return an ask outcome: the guard cannot read the command, for the reason `why`."""
    return Outcome("ask", one_line(f"gate-guard: cannot read this command: {why}"))


def strictest(outcomes: Iterable[Outcome]) -> Outcome:
    """Return the strictest outcome (deny, then ask, then pass); among equals, the first one."""
    result = PASS
    for outcome in outcomes:
        if SEVERITY[outcome.kind] > SEVERITY[result.kind]:
            result = outcome
    return result


# --- Paths ----------------------------------------------------------------------------------------------------


def resolve(path: str, base: str) -> str:
    """Return `path` made absolute against `base`, with ~ expanded, .. resolved and symbolic links followed, the
    way a program (not the shell's cd) reaches the file."""
    return os.path.realpath(os.path.join(base, os.path.expanduser(path)))


def same_path(first: str, second: str) -> bool:
    """True when two resolved paths are equal, ignoring case (default macOS disks ignore case)."""
    return first.casefold() == second.casefold()


def same_or_inside(path: str, folder: str) -> bool:
    """True when the resolved `path` is `folder` or lies inside it, ignoring case."""
    path, folder = path.casefold(), folder.casefold().rstrip(os.sep)
    return path == folder or path.startswith(folder + os.sep)


def user_settings_path() -> str:
    """Return the user's Claude Code settings file, ~/.claude/settings.json, resolved."""
    return resolve("~/.claude/settings.json", os.sep)


def user_git_configuration() -> tuple[str, ...]:
    """Return the user's git configuration paths, ~/.gitconfig and ~/.config/git, resolved."""
    return resolve("~/.gitconfig", os.sep), resolve("~/.config/git", os.sep)


# --- The project: root, working folder, store and branch -----------------------------------------------------


class StoreUnreadable(Exception):
    """The store exists but is not a JSON object with an `items` object."""


class Project:
    """The project an event runs in: its root and working folder, with the store and the branch read once.

    `cwd` is the working folder resolved (for git); `shell_cwd` is the folder as the shell holds it, with symbolic
    links kept, so that a later cd .. goes where the shell goes.
    """

    def __init__(self, root: str, cwd: str, shell_cwd: str | None = None) -> None:
        self.root = root
        self.cwd = cwd
        self.shell_cwd = shell_cwd or cwd
        self.store_path = os.path.join(root, STORE_NAME)
        self._verdicts: set[str] | None = None
        self._branch_read = False
        self._branch: str | None = None

    def load_store(self) -> dict | None:
        """Return the store's items, or None when there is no store; raise StoreUnreadable when it cannot be read."""
        if not os.path.exists(self.store_path):
            return None
        try:
            with open(self.store_path, encoding="utf-8") as handle:
                items = json.load(handle)["items"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise StoreUnreadable(f"{self.store_path}: {error}") from None
        if not isinstance(items, dict):
            raise StoreUnreadable(f"{self.store_path}: items is not an object")
        return items

    def recorded_verdicts(self) -> set[str]:
        """Return every verdict path recorded in the store, resolved and in lower case; empty with no store."""
        if self._verdicts is None:
            verdicts = set()
            for item in (self.load_store() or {}).values():
                paths = item.get("verdicts") if isinstance(item, dict) else None
                for path in paths or []:
                    if isinstance(path, str):
                        verdicts.add(resolve(path, self.root).casefold())
            self._verdicts = verdicts
        return self._verdicts

    def current_branch(self) -> str | None:
        """Return the branch checked out in the working folder, read once with git; None when it is unknown."""
        if not self._branch_read:
            self._branch_read = True
            self._branch = read_branch(self.cwd)
        return self._branch


def read_branch(folder: str) -> str | None:
    """Return the branch checked out in the repository at `folder`, or None when git cannot tell in time."""
    try:
        result = subprocess.run(
            ["git", "-C", folder, "branch", "--show-current"],
            capture_output=True, text=True, errors="replace", timeout=GIT_TIMEOUT_SECONDS, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch else None


def project_of(event: dict) -> Project:
    """Return the event's project: the root is $CLAUDE_PROJECT_DIR, or the event's cwd when that is unset."""
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()
    root = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    return Project(root=os.path.realpath(root), cwd=os.path.realpath(cwd), shell_cwd=os.path.abspath(cwd))


def protected_kind(path: str, project: Project) -> str | None:
    """Name what the resolved `path` is when an agent may not write it by hand, or return None.

    Protected: any file whose name starts with gates.json (the store, its lock, a temporary copy), anything in
    progress/ at the project root, the adapter folder .claude/ and the user's ~/.claude/settings.json, anything in
    a .git folder or file and the user's git configuration, and every verdict recorded in the store. While the
    store cannot be read, every path in the project counts as protected, because any of them may be a recorded
    verdict.
    """
    if os.path.basename(path).casefold().startswith(STORE_NAME):
        return STORE
    if same_or_inside(path, resolve("progress", project.root)):
        return PROGRESS
    if same_or_inside(path, resolve(".claude", project.root)) or same_path(path, user_settings_path()):
        return ADAPTER
    if ".git" in path.casefold().split(os.sep) or any(
        same_or_inside(path, configuration) for configuration in user_git_configuration()
    ):
        return GIT_SETUP
    try:
        verdicts = project.recorded_verdicts()
    except StoreUnreadable:
        return STORE_UNREADABLE if same_or_inside(path, project.root) else None
    return VERDICT if path.casefold() in verdicts else None


def protected_kind_within(path: str, project: Project) -> str | None:
    """Like protected_kind, and also when `path` is a folder that holds a protected path (for rm -r, cp -R)."""
    kind = protected_kind(path, project)
    if kind:
        return kind
    if same_or_inside(project.root, path):
        return PROJECT_FOLDER
    if same_or_inside(user_settings_path(), path):
        return ADAPTER
    if any(same_or_inside(configuration, path) for configuration in user_git_configuration()):
        return GIT_SETUP
    try:
        verdicts = project.recorded_verdicts()
    except StoreUnreadable:
        return None
    if any(same_or_inside(verdict, path) for verdict in verdicts):
        return VERDICT
    return None


# --- Reading shell text ---------------------------------------------------------------------------------------


@dataclasses.dataclass
class Part:
    """One simple command of a shell text: its words without redirections, the files it redirects to and from,
    and any operator or redirection form the guard does not read."""

    words: list[str] = dataclasses.field(default_factory=list)
    writes: list[str] = dataclasses.field(default_factory=list)
    reads: list[str] = dataclasses.field(default_factory=list)
    unknown_operators: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ShellState:
    """What earlier parts of the same command changed for the parts after them.

    `cwd` is the working folder as the shell holds it (symbolic links kept) after literal cd commands, or None
    once it cannot be known (cd -, pushd).
    """

    cwd: str | None
    directory_changed: bool = False
    branch_changed: bool = False
    git_config_changed: bool = False


def join_continued_lines(command: str) -> str:
    """Return `command` with each line continuation removed, as the shell reads it: a backslash-newline outside
    single quotes, where the backslash is not itself escaped (`\\\\` then a newline is a backslash, then a real
    newline)."""
    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            result.append(character)
            if character == "'":
                quote = None
        elif character == "\\":
            following = command[index + 1:index + 2]
            if following != "\n":
                result.append(character + following)
            index += 2
            continue
        else:
            result.append(character)
            if quote is None and character in "'\"":
                quote = character
            elif character == quote:
                quote = None
        index += 1
    return "".join(result)


def unquoted_text(command: str) -> str | None:
    """Return the characters of `command` outside quotes and not escaped by a backslash; None when a quote is open."""
    outside: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
        elif quote == '"':
            if character == "\\":
                index += 1
            elif character == '"':
                quote = None
        elif character == "\\":
            index += 1
        elif character in "'\"":
            quote = character
        else:
            outside.append(character)
        index += 1
    return None if quote else "".join(outside)


def shell_tokens(command: str) -> list[str] | None:
    """Split shell text into words and operator tokens with shlex, quotes respected; None when a quote is open."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=OPERATOR_CHARACTERS)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def is_operator(token: str) -> bool:
    """True for a token made only of operator characters (see OPERATOR_CHARACTERS)."""
    return token != "" and all(character in OPERATOR_CHARACTERS for character in token)


def add_redirection(part: Part, redirection: str, word: str) -> None:
    """Record the file `word` that `redirection` reads or writes in `part`.

    > and >> write, < reads; >& and <& with a descriptor number or - copy a descriptor; << and <<< take text.
    Every other form (>| >>| &> &>> <> >&WORD <&WORD, zsh's >! and >>!) is recorded as an operator the guard does
    not read, and its file is still checked: written for a form with >, read otherwise.
    """
    if redirection in TEXT_REDIRECTIONS:
        return
    if redirection in DUPLICATIONS and (word.isdigit() or word == "-"):
        return
    if redirection in PLAIN_WRITE_REDIRECTIONS and word.startswith("!"):
        redirection, word = redirection + "!", word[1:]
    if redirection in PLAIN_WRITE_REDIRECTIONS:
        part.writes.append(word)
        return
    if redirection in PLAIN_READ_REDIRECTIONS:
        part.reads.append(word)
        return
    part.unknown_operators.append(f"{redirection} (a redirection form the guard does not read)")
    (part.writes if ">" in redirection else part.reads).append(word)


def parts_of(tokens: list[str]) -> list[Part]:
    """Group tokens into parts: a separator ends a part, and a redirection takes the next word as its file."""
    parts = [Part()]
    pending: str | None = None
    for token in tokens:
        part = parts[-1]
        if is_operator(token):
            if pending is not None:
                part.unknown_operators.append(f"{pending} with no file after it")
                pending = None
            if "<" not in token and ">" not in token:
                parts.append(Part())
            elif token in REDIRECTIONS:
                if part.words and part.words[-1].isdigit():
                    part.words.pop()  # the file descriptor in 2>file
                pending = token
            else:
                part.unknown_operators.append(token)
            continue
        if pending is None:
            part.words.append(token)
        else:
            add_redirection(part, pending, token)
        pending = None
    if pending is not None:
        parts[-1].unknown_operators.append(f"{pending} with no file after it")
    return [part for part in parts if part.words or part.writes or part.reads or part.unknown_operators]


def command_words(part: Part) -> tuple[list[str], list[str]]:
    """Split a part's words into its leading variable assignments and the command after them.

    Shell keywords that only introduce a command (if, then, do, while, !, time and its options) are dropped first,
    so `if git push origin main; then` and `time -p git push origin main` are read as the git command.
    """
    words = list(part.words)
    while words and words[0] in SHELL_KEYWORDS:
        keyword = words.pop(0)
        while keyword == "time" and words and words[0].startswith("-"):
            words.pop(0)
    assignments = []
    while words and ASSIGNMENT.match(words[0]):
        assignments.append(words.pop(0))
    return assignments, words


def program_name(word: str) -> str:
    """Return the program name of a command word as the rules compare it: its last path segment, without zsh's
    leading = (=git runs git), in lower case (default macOS disks ignore case, so GIT runs git)."""
    return word.rsplit("/", 1)[-1].removeprefix("=").casefold()


def path_candidates(arguments: Iterable[str]) -> list[str]:
    """Return the arguments that may name a file: every word that is not an option, and the value of a word
    written NAME=VALUE (--output=FILE, of=FILE). Empty words are left out."""
    result = []
    for word in arguments:
        if not word.startswith("-"):
            result.append(word)
        if "=" in word:
            result.append(word.partition("=")[2])
    return [word for word in result if word]


def abbreviates(word: str, long_names: Iterable[str]) -> bool:
    """True when `word` (without any =VALUE) is one of `long_names` or the start of one: git and getopt_long
    accept an unambiguous abbreviation, --mir for --mirror."""
    name = word.partition("=")[0]
    return name.startswith("--") and len(name) > 2 and any(full.startswith(name) for full in long_names)


def option_matches(word: str, long_names: tuple[str, ...], short_letters: str = "") -> bool:
    """True when `word` is one of the long options `long_names` (whole, abbreviated, or with =VALUE), or a cluster
    of short options holding one of `short_letters` (-fM holds M)."""
    if word.startswith("--"):
        return abbreviates(word, long_names)
    return word.startswith("-") and any(letter in word[1:] for letter in short_letters)


def positionals(arguments: Iterable[str], options_with_value: tuple[str, ...]) -> list[str]:
    """Return the arguments that are not options, skipping the value of each option in `options_with_value`
    (written whole or, for a long option, abbreviated)."""
    arguments = list(arguments)
    long_names = tuple(name for name in options_with_value if name.startswith("--"))
    result = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in options_with_value or ("=" not in word and abbreviates(word, long_names)):
            index += 2
            continue
        if not word.startswith("-"):
            result.append(word)
        index += 1
    return result


def split_option(word: str) -> tuple[str, str | None]:
    """Split an option word into its name and attached value: --method=PUT and -XPUT give the value PUT."""
    if word.startswith("--"):
        name, equals, value = word.partition("=")
        return name, (value if equals else None)
    if word.startswith("-") and len(word) > 2:
        return word[:2], word[2:]
    return word, None


def has_option(words: Iterable[str], names: tuple[str, ...]) -> bool:
    """True when one of `words` is an option in `names`, alone, as NAME=VALUE, or as a short option with its
    value attached (-Cdir)."""
    for word in words:
        for name in names:
            if word == name or word.startswith(name + "="):
                return True
            if len(name) == 2 and word.startswith(name):
                return True
    return False


@dataclasses.dataclass(frozen=True)
class GitCall:
    """A git command line split into git's own options (with their values), the subcommand in lower case and its
    arguments."""

    options: tuple[str, ...]
    subcommand: str | None
    arguments: tuple[str, ...]


def parse_git(arguments: list[str]) -> GitCall:
    """Split the words after `git` into git's own options, the subcommand and the subcommand's arguments."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in GIT_OPTIONS_WITH_VALUE:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            return GitCall(tuple(arguments[:index]), word.casefold(), tuple(arguments[index + 1:]))
    return GitCall(tuple(arguments), None, ())


def is_main(name: str) -> bool:
    """True when a branch or ref name means main: main, heads/main or refs/heads/main, in any case."""
    return name.casefold().removeprefix("refs/").removeprefix("heads/") == "main"


def cd_target(arguments: list[str]) -> str | None:
    """Return the one plain folder a cd command names, or None: cd -, cd +N, cd -N (the folder stack), and cd
    with no folder or two folders, do not name one."""
    targets = [word for word in arguments if word not in CD_OPTIONS]
    if len(targets) != 1 or targets[0][:1] in ("-", "+"):
        return None
    return targets[0]


def cd_destination(arguments: list[str], cwd: str) -> str | None:
    """Return the folder a cd with these arguments moves to, as the shell holds it, or None when it is unknown.

    The shell's cd is logical: `a/../b` is `b`, whether or not `a` is a symbolic link. cd -P follows links first.
    """
    target = cd_target(arguments)
    if target is None:
        return None
    if "-P" in arguments:
        return resolve(target, cwd)
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(target)))


# --- Rule 1: permission mode ----------------------------------------------------------------------------------


def rule_permission_mode(event: dict) -> Outcome:
    """Rule 1: deny every call in bypassPermissions mode, where no permission prompt stands before a tool."""
    if event.get("permission_mode") == "bypassPermissions":
        return deny(
            "the session runs in bypassPermissions mode",
            "restart Claude Code in another permission mode; the gates need the permission rules",
        )
    return PASS


# --- Rule 2: approve.py is the human's command ----------------------------------------------------------------


def rule_approve_is_the_humans(command: str) -> Outcome:
    """Rule 2: deny any shell command whose text, with quotes and backslashes removed, contains approve, in any case."""
    if "approve" in re.sub(r"[\"'\\]", "", command).casefold():
        return deny(
            "a shell command containing the word approve (gates/approve.py is the human's command)",
            "read files with the Read tool, ask the human to run approve.py, "
            "and put a commit message that names it in a file for git commit -F",
        )
    return PASS


# --- Rules 3, 4 and 7: records and the adapter, by file tool and by shell -------------------------------------


def rule_file_tool_records(tool_input: dict, project: Project) -> Outcome:
    """Rules 3, 4 and 7 for Edit, Write, MultiEdit and NotebookEdit: deny a write of a protected path."""
    given = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(given, str) or not given:
        return deny("a file tool call with no file path", "give the tool a file path")
    kind = protected_kind(resolve(given, project.shell_cwd), project)
    if kind:
        return deny(f"writing {kind} ({given}) by hand", INSTEAD[kind])
    return PASS


def is_in_place_option(word: str) -> bool:
    """True for the in-place option of sed, perl or ruby: -i, -i.bak, -pi, -Ei, --in-place."""
    if word == "--in-place" or word.startswith("--in-place="):
        return True
    return word.startswith("-") and not word.startswith("--") and "i" in word[1:]


def output_files(arguments: list[str]) -> list[str]:
    """Return the files a git command writes with --output FILE or --output=FILE (or an abbreviation, --out)."""
    result = []
    for index, word in enumerate(arguments):
        if not abbreviates(word, ("--output",)):
            continue
        if "=" in word:
            result.append(word.partition("=")[2])
        elif index + 1 < len(arguments):
            result.append(arguments[index + 1])
    return result


def copied_names(source: str, cwd: str) -> list[str]:
    """Return the names a copy, move or link of `source` creates inside a destination folder: the source's own
    name and, for a folder written with a trailing / or /. (whose contents cp -R copies), every entry in it."""
    names = [os.path.basename(os.path.normpath(source))]
    path = resolve(source, cwd)
    if (source.endswith("/") or source.endswith("/.") or source == ".") and os.path.isdir(path):
        try:
            names.extend(sorted(os.listdir(path)))
        except OSError:
            pass
    return names


def copy_destinations(arguments: list[str], cwd: str) -> list[tuple[str, str]]:
    """For cp, mv and ln into a folder (one that exists, or a last operand written with a trailing /): return each
    path written inside it, folder/name for every name a source creates, as (word shown, resolved path)."""
    operands = [word for word in arguments if not word.startswith("-")]
    if len(operands) < 2:
        return []
    destination = operands[-1]
    folder = resolve(destination, cwd)
    if not (destination.endswith("/") or os.path.isdir(folder)):
        return []
    result = []
    for source in operands[:-1]:
        for name in copied_names(source, cwd):
            result.append((f"{destination.rstrip('/')}/{name}", resolve(name, folder)))
    return result


def write_targets(words: list[str], cwd: str) -> list[tuple[str, str]]:
    """Return the paths a command writes, as (word shown, resolved path); each one counts with everything in it.

    Read as writes: every path named by rm rmdir unlink shred cp mv ln mkdir touch tee truncate install rsync dd
    chmod chown, by sed/perl/ruby with -i, by git checkout/restore/rm/mv (resolved both as a program reaches it and
    as git reads a pathspec), a git --output file, and for cp, mv and ln into a folder, the paths they create
    inside it.
    """
    name = program_name(words[0])
    arguments = words[1:]
    in_place = name in IN_PLACE_EDITORS and any(is_in_place_option(word) for word in arguments)
    if name in WRITING_PROGRAMS or in_place:
        named = path_candidates(arguments)
    elif name == "git":
        call = parse_git(arguments)
        if call.subcommand not in GIT_WRITE_SUBCOMMANDS:
            return [(word, resolve(word, cwd)) for word in output_files(list(call.arguments))]
        # git reads .. in a pathspec without following links (link/../progress is progress), so both readings count.
        physical_cwd = os.path.realpath(cwd)
        targets = []
        for word in path_candidates(call.arguments):
            lexical = os.path.normpath(os.path.join(physical_cwd, os.path.expanduser(word)))
            targets += [(word, resolve(word, cwd)), (word, os.path.realpath(lexical))]
        return targets
    else:
        return []
    targets = [(word, resolve(word, cwd)) for word in named]
    if name in COPYING_PROGRAMS:
        targets.extend(copy_destinations(arguments, cwd))
    return targets


def is_cli_call(words: list[str], project: Project, state: ShellState) -> bool:
    """True for python|python3 gates/cli.py ... or python|python3 -m gates.cli ..., run from the project root."""
    if program_name(words[0]) not in ("python", "python3") or state.cwd is None:
        return False
    script = words[1:2] == ["gates/cli.py"] or words[1:3] == ["-m", "gates.cli"]
    return script and same_path(os.path.realpath(state.cwd), project.root)


def is_read_only_git(words: list[str]) -> bool:
    """True for git add, diff, show, log or status (their --output is checked as a write before this)."""
    return program_name(words[0]) == "git" and parse_git(words[1:]).subcommand in GIT_READ_SUBCOMMANDS


def rule_shell_records(part: Part, project: Project, state: ShellState) -> Outcome:
    """Rules 3, 4 and 7 in a shell command: deny a write of a protected path; ask when one is named otherwise.

    Writes the guard reads: a redirection, and the commands listed in write_targets. A folder written counts with
    everything in it (rm -r reviews, cp -R stage/. .). Naming a protected path passes only in python gates/cli.py
    ... from the project root and in git add|diff|show|log|status.
    """
    _, words = command_words(part)
    if not (words or part.writes or part.reads):
        return PASS
    if state.cwd is None:
        return ask("an earlier cd, pushd or popd leaves the working folder unknown, so paths cannot be resolved")
    for target in part.writes:
        kind = protected_kind(resolve(target, state.cwd), project)
        if kind:
            return deny(f"a redirection that writes {kind} ({target})", INSTEAD[kind])
    if not words:
        return PASS
    name = program_name(words[0])
    for shown, path in write_targets(words, state.cwd):
        kind = protected_kind_within(path, project)
        if kind:
            return deny(f"{name} writes {kind} ({shown})", INSTEAD[kind])
    if is_cli_call(words, project, state) or is_read_only_git(words):
        return PASS
    named = [(word, resolve(word, state.cwd)) for word in path_candidates(words[1:]) + part.reads]
    if name == "cd":
        destination = cd_destination(words[1:], state.cwd)
        if destination is not None:
            named.append((cd_target(words[1:]) or "", os.path.realpath(destination)))
    for word, path in named:
        kind = protected_kind(path, project)
        if kind:
            return ask(
                f"it names {kind} ({word}) in a form the guard does not classify; use python gates/cli.py from "
                "the project root, git add|diff|show|log|status, or the Read tool"
            )
    return PASS


# --- Rule 5: main changes only through pull requests ----------------------------------------------------------


def checked_out_branch(call: GitCall, project: Project, state: ShellState) -> tuple[str | None, Outcome]:
    """Return the branch a git call works on and PASS, or None and the ask outcome that says why it is unknown."""
    if has_option(call.options, GIT_DIRECTORY_OPTIONS):
        return None, ask(ASK_OTHER_REPOSITORY)
    if state.directory_changed:
        return None, ask("an earlier cd in this command moves git to another folder; run the git command alone")
    if state.branch_changed:
        return None, ask("an earlier git command in this command can change the checked-out branch "
                         "(checkout, switch, branch -m, rebase); run the next git command alone")
    branch = project.current_branch()
    if branch is None:
        return None, ask("the guard cannot read which branch is checked out (no repository, a detached HEAD, "
                         "or git did not answer in time)")
    return branch, PASS


def push_target(refspec: str) -> str | None:
    """Return the branch a push refspec updates, or None when it is HEAD or @ (the checked-out branch)."""
    source, colon, destination = refspec.removeprefix("+").partition(":")
    if colon:
        return destination
    if source in ("HEAD", "@"):
        return None
    return source


def check_push(call: GitCall, project: Project, state: ShellState) -> Outcome:
    """Rule 5 for git push: deny a target of main, every-branch pushes, and a push of the branch while on main.

    Every plain word but the remote is read as a refspec, so a word the guard took for an option's value still
    denies when it names main.
    """
    arguments = list(call.arguments)
    if any(option_matches(word, PUSH_ALL_BRANCHES) for word in arguments):
        return deny("git push of every branch, which includes main", "push your branch by name")
    names = positionals(arguments, PUSH_OPTIONS_WITH_VALUE)
    plain_words = [word for word in arguments if not word.startswith("-")]
    if names:
        plain_words.remove(names[0])
    for word in plain_words:
        target = push_target(word)
        if target is None:
            continue
        if is_main(target):
            return deny(f"git push to main ({word})", "push a feature branch and open a pull request")
        if "*" in target:
            return deny(f"git push of a pattern refspec ({word}), which can include main",
                        "push your branch by name")
    refspecs = names[1:]
    needs_branch = not refspecs or any(push_target(refspec) is None for refspec in refspecs)
    if state.git_config_changed and (needs_branch or any(":" not in refspec for refspec in refspecs)):
        return ask("an earlier git config, git remote or git branch -u in this command can change where this push goes")
    if not needs_branch:
        return PASS
    branch, unknown = checked_out_branch(call, project, state)
    if branch is None:
        return unknown
    if is_main(branch):
        return deny("git push while main is checked out", "push a feature branch and open a pull request")
    return PASS


def check_merge_or_rebase(call: GitCall, project: Project, state: ShellState) -> Outcome:
    """Rule 5 for git merge, rebase and pull: deny while main is checked out, and a rebase of main by name."""
    if call.subcommand == "rebase":
        names = positionals(call.arguments, REBASE_OPTIONS_WITH_VALUE)
        if any(is_main(name) for name in names[1:]):
            return deny("git rebase of main", "rebase a feature branch; main changes only through pull requests")
    branch, unknown = checked_out_branch(call, project, state)
    if branch is None:
        return unknown
    if is_main(branch):
        return deny(
            f"git {call.subcommand} while main is checked out",
            "switch to a feature branch; main changes only through pull requests",
        )
    return PASS


def rule_main_through_pull_requests(part: Part, project: Project, state: ShellState) -> Outcome:
    """Rule 5: no push to main, and no merge, rebase or pull while main is checked out."""
    _, words = command_words(part)
    if not words or program_name(words[0]) != "git":
        return PASS
    call = parse_git(words[1:])
    outcomes = []
    if has_option(call.options, GIT_CONFIG_OPTIONS):
        outcomes.append(ask("git -c or --config-env sets configuration for this call, which can change what it does"))
    if call.subcommand in ("push",) + GIT_MERGING_SUBCOMMANDS and has_option(call.options, GIT_DIRECTORY_OPTIONS):
        outcomes.append(ask(ASK_OTHER_REPOSITORY))
    if call.subcommand == "push":
        outcomes.append(check_push(call, project, state))
    elif call.subcommand in GIT_MERGING_SUBCOMMANDS:
        outcomes.append(check_merge_or_rebase(call, project, state))
    return strictest(outcomes)


# --- Rule 6: a pull request merges only after the human's go --------------------------------------------------


def check_pr_merge(arguments: list[str], project: Project) -> Outcome:
    """Rule 6 for gh pr merge: only the allowed form, with the head the human approved for that pull request."""
    numbers, head, index = [], None, 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--match-head-commit" and index + 1 < len(arguments):
            head = arguments[index + 1]
            index += 2
            continue
        if word.startswith("--match-head-commit="):
            head = word.partition("=")[2]
        elif re.fullmatch(r"[0-9]+", word):
            numbers.append(int(word))
        elif word not in MERGE_FLAGS:
            return deny(f"gh pr merge with the argument {word}", MERGE_FORM)
        index += 1
    if len(numbers) != 1:
        return deny("gh pr merge without exactly one pull request number", MERGE_FORM)
    if head is None:
        return deny("gh pr merge without --match-head-commit", MERGE_FORM)
    pr = numbers[0]
    try:
        items = project.load_store()
    except StoreUnreadable as error:
        return deny(f"the store cannot be read ({error})", "ask the human to restore gates.json from git")
    if items is None:
        return deny(f"no store at {project.store_path}", "run the command from the project root that holds gates.json")
    tasks = [item for item in items.values() if isinstance(item, dict) and item.get("pr") == pr]
    if not tasks:
        return deny(f"gh pr merge {pr}: no task in the store has pull request {pr}",
                    "check the number with python gates/cli.py board")
    open_tasks = [item for item in tasks if item.get("state") == "pr_open"]
    if not open_tasks:
        return deny(f"gh pr merge {pr}: its task is not in pr_open",
                    "a pull request merges only from pr_open, after the human's merge go")
    approved = [item.get("merge_approved_head") for item in open_tasks if item.get("merge_approved_head")]
    if not approved:
        return deny(f"gh pr merge {pr}: the human has not approved the merge",
                    "ask the human to run gates/approve.py ID --merge")
    if head not in approved or not HEAD_SHA.fullmatch(head):
        return deny(f"gh pr merge {pr}: --match-head-commit is not the head the human approved",
                    "use the exact command gates/approve.py printed, or ask for a new go")
    return PASS


def check_gh_api(words: list[str]) -> Outcome:
    """Rule 6 for gh api: deny a merge or a change to main; ask on any method but GET, and on graphql."""
    if API_MERGE.search(" ".join(words).casefold()):
        return deny("a merge or a change to main through the GitHub API", MERGE_INSTEAD)
    arguments = words[2:]
    method, endpoint, has_body = "", "", False
    index = 0
    while index < len(arguments):
        word = arguments[index]
        index += 1
        if not word.startswith("-"):
            endpoint = endpoint or word
            continue
        name, value = split_option(word)
        if value is None and name in GH_API_OPTIONS_WITH_VALUE and index < len(arguments):
            value = arguments[index]
            index += 1
        if name in ("-X", "--method"):
            method = (value or "").upper()
        if name in GH_API_BODY_OPTIONS:
            has_body = True
    method = method or ("POST" if has_body else "GET")
    if endpoint.casefold() == "graphql":
        return ask("gh api graphql can run any mutation, and the guard does not read the query")
    if method != "GET":
        return ask(f"gh api with method {method} changes GitHub, and the guard reads only merges")
    return PASS


def check_github_api_by_url(words: list[str]) -> Outcome:
    """Rule 6 for curl and wget (both off the allowlist, so they ask): deny a merge through api.github.com."""
    if "api.github.com" in " ".join(words).casefold() and API_MERGE.search(" ".join(words).casefold()):
        return deny("a merge or a change to main through the GitHub API", MERGE_INSTEAD)
    return PASS


def rule_merge_after_human_go(part: Part, project: Project) -> Outcome:
    """Rule 6: a pull request merges only as gh pr merge N --match-head-commit <the head the human approved>."""
    _, words = command_words(part)
    if not words:
        return PASS
    name = program_name(words[0])
    arguments = words[1:]
    if name in ("curl", "wget"):
        return check_github_api_by_url(words)
    if name != "gh":
        return PASS
    first_two = [word.casefold() for word in arguments[:2]]
    if first_two[:1] == ["api"]:
        return check_gh_api(words)
    if first_two == ["pr", "merge"]:
        return check_pr_merge(arguments[2:], project)
    if first_two[:1] == ["alias"]:
        return ask("gh alias defines a command the guard does not read")
    folded = [word.casefold() for word in arguments]
    if "pr" in folded and "merge" in folded:
        return ask("gh pr merge written in an order the guard does not read")
    return PASS


# --- Rule 8: the guard passes only commands on its allowlist ---------------------------------------------------


def rule_readable_text(command: str) -> Outcome:
    """Rule 8 on the whole text: ask on expansions, patterns and comments, which the guard does not evaluate."""
    if "$" in command:
        return ask("it holds a $ expansion (a variable, $(...) or $'...'), which the guard does not evaluate")
    if "`" in command:
        return ask("it holds a backtick command substitution")
    outside = unquoted_text(command)
    if outside is None:
        return ask("a quote is left open")
    for character in GLOB_AND_COMMENT_CHARACTERS:
        if character in outside:
            return ask(f"it holds {character} outside quotes (a pattern, a brace expansion or a comment), "
                       "which the guard does not expand")
    return PASS


def rule_operators_are_unquoted(command: str, tokens: list[str]) -> Outcome:
    """Rule 8 on operators: ask when an operator token came from a quoted or escaped character, such as ';' or \\;.

    shlex returns a quoted ; as the same token as a real one. Every operator character outside quotes becomes
    part of an operator token, so when the tokens hold more operator characters than the text holds outside
    quotes, one of them was quoted, and the guard cannot tell where the parts really end.
    """
    outside = unquoted_text(command) or ""
    outside_count = sum(1 for character in outside if character in OPERATOR_CHARACTERS)
    token_count = sum(len(token) for token in tokens if is_operator(token))
    if outside_count != token_count:
        return ask("an operator character (; & | < > or a parenthesis) is quoted or escaped where a word is expected")
    return PASS


def check_read_only(name: str, arguments: list[str]) -> Outcome:
    """Allowlist check for the read-only programs: sort asks on -o, -T, --compress-program; uniq on a second
    operand (it writes it)."""
    if name == "sort" and any(option_matches(word, SORT_WRITE_OPTIONS, SORT_WRITE_LETTERS) for word in arguments):
        return ask("sort -o, -T and --compress-program write files or run a program")
    if name == "uniq" and len(positionals(arguments, UNIQ_OPTIONS_WITH_VALUE)) > 1:
        return ask("uniq writes its second operand")
    return PASS


def check_file_writer(name: str, arguments: list[str]) -> Outcome:
    """Allowlist check for mkdir, touch, cp, mv, rm and ln: cp, mv and ln ask on -t (the destination first).
    Rules 3, 4 and 7 check the paths."""
    if name in COPYING_PROGRAMS and any(
        option_matches(word, TARGET_FOLDER_OPTIONS, TARGET_FOLDER_LETTERS) for word in arguments
    ):
        return ask(f"{name} -t names the destination folder first, which the guard does not read")
    return PASS


def check_cd(arguments: list[str]) -> Outcome:
    """Allowlist check for cd: exactly one plain folder (not -, +N or -N)."""
    if cd_target(arguments) is None:
        return ask("cd without exactly one plain folder (not -, +N or -N) leaves the working folder unknown")
    return PASS


def check_git(arguments: list[str]) -> Outcome:
    """Allowlist check for git: no git option but --no-pager and -P, a subcommand in GIT_SUBCOMMANDS, and none of
    the forms that rename a branch, change where it pushes, or run a command."""
    call = parse_git(arguments)
    for word in call.options:
        if word not in GIT_ALLOWED_OPTIONS:
            return ask(f"git with the option {word} before the subcommand, which the guard does not read")
    subcommand, rest = call.subcommand, list(call.arguments)
    if subcommand is None:
        return ask("git without a subcommand")
    if subcommand not in GIT_SUBCOMMANDS:
        return ask(f"git {subcommand} is not on the guard's allowlist (.claude/README.md lists it)")
    if subcommand == "remote" and rest not in ([], ["-v"], ["--verbose"]):
        return ask("git remote passes only as git remote -v; its other forms change where pushes go")
    if subcommand == "stash" and rest[:1] != ["list"]:
        return ask("git stash passes only as git stash list; its other forms change files without naming them")
    if subcommand == "branch" and any(
        option_matches(word, GIT_BRANCH_RENAME_OPTIONS + GIT_UPSTREAM_OPTIONS,
                       GIT_BRANCH_RENAME_LETTERS + GIT_UPSTREAM_LETTERS)
        for word in rest
    ):
        return ask("git branch -m, -M, -c, -C, -u and -t rename a branch or change where it pushes")
    if subcommand in GIT_RUN_OPTIONS:
        long_names, letters = GIT_RUN_OPTIONS[subcommand]
        if any(option_matches(word, long_names, letters) for word in rest):
            return ask(f"git {subcommand} with an option that runs a command the guard does not read")
    if any(abbreviates(word, ("--pathspec-from-file",)) for word in rest):
        return ask("git --pathspec-from-file reads its paths from a file the guard does not read")
    if subcommand in ("checkout", "restore") and any(
        word.startswith(":") or any(character in word for character in GIT_PATHSPEC_PATTERN_CHARACTERS)
        for word in path_candidates(rest)
    ):
        return ask(f"git {subcommand} with a pathspec pattern or magic (* ? [ or a leading :), which git expands "
                   "and the guard does not")
    if subcommand in ("checkout", "switch") and moves_to_another_commit(rest):
        return ask(f"git {subcommand} to another branch or commit replaces the store, the progress logs and the "
                   "verdicts with that commit's versions; only creating a branch where you stand (-b NAME) passes")
    if subcommand == "rebase":
        names = positionals(rest, REBASE_OPTIONS_WITH_VALUE)
        if len(names) > 1 or any(abbreviates(word, ("--onto",)) for word in rest):
            return ask("git rebase --onto, or with a branch to check out first, can move the branch to another "
                       "commit and replace the records with that commit's versions")
    return PASS


def moves_to_another_commit(arguments: list[str]) -> bool:
    """True when git checkout or git switch with these arguments moves the working folder to another commit.

    It does not when it creates a branch at the current commit (-b NAME, -c NAME, with no start point), or when
    git checkout restores paths named after -- (rules 3, 4 and 7 check those paths).
    """
    if "--" in arguments and arguments.index("--") < len(arguments) - 1:
        return False
    long_names = tuple(name for name in GIT_CREATE_BRANCH_OPTIONS if name.startswith("--"))
    creates = any(word in GIT_CREATE_BRANCH_OPTIONS or abbreviates(word, long_names) for word in arguments)
    return not creates or bool(positionals(arguments, GIT_CREATE_BRANCH_OPTIONS))


def check_github_cli(arguments: list[str]) -> Outcome:
    """Allowlist check for gh: gh api, or a group and command in GH_SUBCOMMANDS."""
    group = arguments[0].casefold() if arguments else ""
    if group == "api":
        return PASS
    commands = GH_SUBCOMMANDS.get(group)
    if commands is None:
        return ask(f"gh {group or 'without a command'} is not on the guard's allowlist (.claude/README.md lists it)")
    command = arguments[1].casefold() if len(arguments) > 1 else ""
    if command not in commands:
        return ask(f"gh {group} {command or 'without a command'} is not on the guard's allowlist")
    return PASS


def check_python(arguments: list[str], project: Project, state: ShellState) -> Outcome:
    """Allowlist check for python and python3: -m unittest, -m gates.cli, or a first argument that is an existing
    .py file inside the project. Anything before it (-c, -I, -, no script, standard input) asks."""
    if arguments[:1] == ["-m"] and arguments[1:2] and arguments[1] in PYTHON_MODULES:
        return PASS
    script = arguments[0] if arguments else ""
    if script.casefold().endswith(".py") and not script.startswith("-") and state.cwd is not None:
        path = resolve(script, state.cwd)
        if os.path.isfile(path) and same_or_inside(path, project.root):
            return PASS
    return ask("python passes only as python -m unittest, python -m gates.cli, or python FILE.py with an existing "
               "file in the project; an option before them, -c, - and standard input are not read")


def check_arguments(kind: str, name: str, arguments: list[str], project: Project, state: ShellState) -> Outcome:
    """Run the allowlist check of `kind` (a value of ALLOWLIST) on a program's arguments."""
    if kind == READ_ONLY:
        return check_read_only(name, arguments)
    if kind == FILE_WRITER:
        return check_file_writer(name, arguments)
    if kind == FOLDER_CHANGE:
        return check_cd(arguments)
    if kind == GIT:
        return check_git(arguments)
    if kind == GITHUB_CLI:
        return check_github_cli(arguments)
    return check_python(arguments, project, state)


def shell_command_string(words: list[str]) -> str | None:
    """Return the command string a shell runs with -c (bash -c 'CODE', bash -ec 'CODE'), or None without -c."""
    for index, word in enumerate(words[1:], start=1):
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            rest = [later for later in words[index + 1:] if not later.startswith("-")]
            return rest[0] if rest else ""
    return None


def nested(code: str, project: Project, state: ShellState, depth: int) -> Outcome:
    """Read the code a bash -c or an eval runs as a command of its own, up to NESTING_LIMIT deep."""
    if depth + 1 > NESTING_LIMIT:
        return ask(f"commands nested more than {NESTING_LIMIT} deep in bash -c or eval")
    return check_shell(code, project, dataclasses.replace(state), depth + 1)


def rule_allowlist(part: Part, project: Project, state: ShellState, depth: int) -> Outcome:
    """Rule 8 on one part: pass only a program on ALLOWLIST whose arguments pass its check; ask otherwise.

    Also asks on a leading variable assignment, a program run by path or as zsh's =NAME, and a redirection form
    the guard does not read. The code of bash -c '...' and eval is read as a command too, so a deny inside it
    still denies.
    """
    outcomes = [ask(f"it uses the shell form {operator}, which the guard does not read")
                for operator in part.unknown_operators]
    assignments, words = command_words(part)
    if assignments:
        variable = assignments[0].partition("=")[0]
        outcomes.append(ask(f"it sets the variable {variable}, which can change what the command does"))
    if not words:
        return strictest(outcomes)
    program, name = words[0], program_name(words[0])
    if "/" in program:
        outcomes.append(ask(f"it runs a program by path ({program}); run it by name"))
    elif program.startswith("="):
        outcomes.append(ask(f"{program} is zsh's form for a program's path; run it by name"))
    kind = ALLOWLIST.get(name)
    if kind is not None:
        outcomes.append(check_arguments(kind, name, words[1:], project, state))
        return strictest(outcomes)
    outcomes.append(ask(f"{name} is not on the guard's allowlist (.claude/README.md lists it)"))
    if name == "eval":
        outcomes.append(nested(" ".join(words[1:]), project, state, depth))
    elif name in SHELLS:
        code = shell_command_string(words)
        if code is not None:
            outcomes.append(nested(code, project, state, depth))
    return strictest(outcomes)


def advance_state(part: Part, state: ShellState) -> None:
    """Record what a part changes for the parts after it: the folder (cd), the branch (see GIT_BRANCH_CHANGES,
    git branch -m/-M/-c/-C) and git's configuration (git config, git remote, git branch -u/-t)."""
    _, words = command_words(part)
    if not words:
        return
    name, arguments = program_name(words[0]), words[1:]
    if name == "cd":
        state.directory_changed = True
        state.cwd = cd_destination(arguments, state.cwd) if state.cwd is not None else None
    elif name in ("pushd", "popd"):
        state.directory_changed = True
        state.cwd = None
    elif name == "git":
        call = parse_git(arguments)
        is_branch = call.subcommand == "branch"
        if call.subcommand in GIT_BRANCH_CHANGES or (is_branch and any(
            option_matches(word, GIT_BRANCH_RENAME_OPTIONS, GIT_BRANCH_RENAME_LETTERS) for word in call.arguments
        )):
            state.branch_changed = True
        if call.subcommand in GIT_CONFIG_CHANGES or (is_branch and any(
            option_matches(word, GIT_UPSTREAM_OPTIONS, GIT_UPSTREAM_LETTERS) for word in call.arguments
        )):
            state.git_config_changed = True


# --- Entry point ----------------------------------------------------------------------------------------------


def check_shell(command: str, project: Project, state: ShellState, depth: int = 0) -> Outcome:
    """Apply every shell rule to `command` and return the strictest outcome over the rules and the parts.

    A backslash at the end of a line asks; the rules read the text with those lines joined, as the shell does.
    """
    outcomes = []
    if "\\\n" in command:
        outcomes.append(ask("a backslash at the end of a line joins it to the next one, "
                            "which the guard does not read"))
    joined = join_continued_lines(command)
    outcomes += [rule_approve_is_the_humans(command), rule_approve_is_the_humans(joined),
                 rule_readable_text(joined)]
    tokens = shell_tokens(joined)
    if tokens is None:
        outcomes.append(ask("a quote is left open"))
        return strictest(outcomes)
    outcomes.append(rule_operators_are_unquoted(joined, tokens))
    for part in parts_of(tokens):
        outcomes.append(rule_allowlist(part, project, state, depth))
        outcomes.append(rule_shell_records(part, project, state))
        outcomes.append(rule_main_through_pull_requests(part, project, state))
        outcomes.append(rule_merge_after_human_go(part, project))
        advance_state(part, state)
    return strictest(outcomes)


def check_event(event: object) -> Outcome:
    """Apply every rule to one hook event and return the outcome."""
    if not isinstance(event, dict):
        return deny("a hook event that is not a JSON object", HOOK_SETUP)
    mode = rule_permission_mode(event)
    if mode.kind != "pass":
        return mode
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return deny("a hook event with no tool_input object", HOOK_SETUP)
    project = project_of(event)
    tool_name = event.get("tool_name")
    command = tool_input.get("command")
    if tool_name in WRITE_TOOLS:
        return rule_file_tool_records(tool_input, project)
    if isinstance(command, str):
        return check_shell(command, project, ShellState(cwd=project.shell_cwd))
    if tool_name in SHELL_TOOLS:
        return deny(f"a {tool_name} call with no command", f"give the {tool_name} tool a command")
    return PASS


def emit(outcome: Outcome) -> int:
    """Print the outcome the way Claude Code reads it and return the exit status: 2 for deny, 0 otherwise."""
    if outcome.kind == "deny":
        print(outcome.message, file=sys.stderr)
        return 2
    if outcome.kind == "ask":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": outcome.message,
            }
        }))
    return 0


def main() -> int:
    """Read the event from standard input, apply the rules, print the outcome; any fault denies."""
    try:
        try:
            event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        except ValueError:
            outcome = deny("a hook event that is not JSON", HOOK_SETUP)
        else:
            outcome = check_event(event)
    except Exception as error:  # Any fault denies: the guard fails closed.
        outcome = deny(f"the guard failed ({type(error).__name__}: {error})",
                       "fix the guard, then run test-gate-guard.sh next to it")
    return emit(outcome)


if __name__ == "__main__":
    sys.exit(main())
