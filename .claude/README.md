# The Claude Code adapter

This folder fits the kit to Claude Code. It is wall 1 of the three walls described in `gates/approve.py`:
`settings.json` holds the permission rules (wall 1a), and `hooks/gate-guard.sh` is a PreToolUse hook (wall 1b) that
must hold on its own, because permission rules are prefix matches an agent can step around. `agents/` holds the
planner, worker and reviewer agent files; each one points at its role in `roles/` and adds only what is specific to
this harness.

## Permission rules (`settings.json`)

| Tier | What it covers |
|---|---|
| deny | Running `gates/approve.py` (python, python3, `-m gates.approve`, `./` and bare paths, and any command containing `gates/approve.py` or `gates.approve`); `Edit` of `gates.json`, `gates.json.lock` and `progress/**` |
| ask | `git push`, `gh pr merge`, `gh pr create` (outward-facing: the human sees each one) |

There is no allow tier: every command the agent runs goes through the user's own permission prompts.
`disableBypassPermissionsMode: "disable"` stops a session from starting in bypassPermissions mode. A file path rule
is written as `Edit(...)` only: Claude Code applies `Edit` rules to every file-writing tool.

The hook runs on every tool (`"matcher": "*"`) as `bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/gate-guard.sh`, with a
30-second timeout and `"onFailure": "block"` (Claude Code v2.1.295 or later), so a hook that fails or times out
blocks the call instead of letting it through. The guard runs git at most once per call, with a 3-second limit, so
it stays far below that. Running on every tool costs one Python start per tool call.

## The guard (`hooks/gate-guard.sh`, `hooks/gate_guard.py`)

`gate-guard.sh` is a short wrapper: it denies when `python3` is missing, runs `gate_guard.py` with the hook event on
standard input, and turns any exit status other than 0 or 2 into a deny. `gate_guard.py` (standard library only)
holds the rules, one function per rule, named after the rule. Each call gets one of three outcomes:

| Outcome | When | What the guard prints | What Claude Code does |
|---|---|---|---|
| deny | the call does one of the refused actions below (rules 1 to 7) | exit 2, one line on standard error: `gate-guard: refused: <what> — <what to do instead>` | blocks the call and shows the line to the agent |
| ask | a shell command with a part that is not on the allowlist, or that the guard cannot read (rule 8) | exit 0, a JSON object with `permissionDecision: "ask"` and the reason `gate-guard: cannot read this command: <why>` | asks the human to confirm the call |
| pass | every part of the command is on the allowlist, and no rule denies | exit 0, no output | the user's own permission rules decide |

The guard never answers `allow`, which would skip the user's permission prompts. A fault inside `gate_guard.py`
denies (fail closed). The project root is `$CLAUDE_PROJECT_DIR`, or the event's `cwd` when that variable is unset;
the store is `gates.json` at the root.

Shell commands are read from any tool whose input carries a `command` string (Bash, Monitor, PowerShell, and any
other). A command is split into parts on `;`, `&&`, `||`, `|`, `&`, parentheses and newlines with `shlex`, quotes
respected; every rule looks at every part, and the strictest outcome wins, so a deny inside an ask still denies.
The code inside `bash -c '...'` and `eval ...` is read as a command too. Paths, from the file tools and from shell
words, are resolved against the working folder, with `..` resolved and symbolic links followed, and compared
without case, as default macOS disks ignore case. A `cd` earlier in the command moves the working folder the way
the shell does: logically, so `a/../b` is `b` even when `a` is a symbolic link.

### The allowlist (rule 8)

A shell command passes only when every part runs one of these programs, its name compared without case (`GIT`
runs git on a default macOS disk), with arguments that pass the program's check. The table is `ALLOWLIST` at the
top of `gate_guard.py`.

| Programs | Passes | Asks |
|---|---|---|
| `ls` `cat` `head` `tail` `wc` `grep` `diff` `pwd` `echo` `printf` `true` `test` `sort` `uniq` `cut` `tr` `stat` `du` `date` `basename` `dirname` `which` | any arguments (they only read and print) | `sort -o`, `-T`, `--compress-program`; `uniq` with a second operand, which it writes |
| `mkdir` `touch` `cp` `mv` `rm` `ln` | when no operand is protected (rules 3, 4, 7): every operand, everything inside a folder operand, and for `cp`, `mv` and `ln` into a folder, `folder/name` for each source (every entry of a source written `dir/` or `dir/.`) | `cp`, `mv`, `ln` with `-t` |
| `cd` | exactly one plain folder | `cd`, `cd -`, `cd +N`, `cd -N`, two folders |
| `git` | `status` `log` `diff` `show` `add` `commit` `checkout` `switch` `branch` `fetch` `pull` `push` `merge` `rebase` `restore` `rev-parse` `merge-base`, `remote -v`, `stash list`; git options `--no-pager` and `-P` only; rule 5 checks `push`, `merge`, `rebase`, `pull` | any other subcommand (`config`, `reset`, `stash pop`, `cherry-pick`, `apply`, `bisect`, `submodule`, `filter-branch`, `worktree`, `symbolic-ref`, `update-ref`, aliases); any other git option (`-C`, `-c`, `--git-dir`); `branch -m -M -c -C -u -t`; `checkout` or `switch` to another branch or commit (only `-b NAME` / `-c NAME` with no start point, or `checkout ... -- PATHS`, pass); a pathspec pattern or magic (`'*.json'`, `':!x'`) in `checkout` or `restore`; `--pathspec-from-file`; `rebase -x`, `--exec`, `--onto` or with a branch argument; `fetch`/`pull --upload-pack`; `push --receive-pack`/`--exec` |
| `gh` | `pr view` `pr diff` `pr list` `pr checks` `pr status` `pr create`, `pr merge` (rule 6), `repo view`, `run view` `run list` `run watch`, `api` as a GET (rule 6) | any other command (`pr checkout`, `run download`, `alias`, `repo clone`) |
| `python` `python3` | `-m unittest ...`, `-m gates.cli ...`, or a first argument that is an existing `.py` file inside the project | `-c`, `-I` or any option before the script, `-`, no script (standard input), a file outside the project or not there yet |

Everything else asks, among them `awk`, `sed`, `perl`, `node`, `ruby`, `find`, `tar`, `unzip`, `tee`, `curl`,
`wget`, `http`, `env`, `xargs`, `sudo`, `bash`, `sh`, `zsh`, `eval`, `source`, `exec`, `caffeinate`, `xcrun`,
`arch`, `uv`, `noglob`, `repeat`, `coproc`, `pushd`, `popd`, a program run by path (`/usr/bin/git`) or as zsh's
`=git`, and a part that sets a variable first (`FOO=1 ls`). `tree` is left out: `tree -o` and `tree -R` write files.

The guard also asks, whatever the program, on: `$` anywhere (variables, `$(...)`, `$'...'`); backticks;
`* ? [ ] { } #` outside quotes; an open quote; a quoted or escaped operator (`';'`, `\;`); `<(...)`; a backslash at
the end of a line (the rules still read the joined lines, as the shell does, so a deny there still denies); and
the redirections `>|`, `>>|`, `&>`, `&>>`, `<>`, `>&FILE`, `<&FILE` and zsh's `>!`, `>>!` (their target is still
checked, so `echo x >!gates.json` denies). Shell keywords that only introduce a command (`if`, `then`, `do`,
`while`, `!`, `time` and `time -p`) are dropped and the command after them is read.

### The deny rules

| Rule | Outcome | Example | Allowed neighbour |
|---|---|---|---|
| 1. Permission mode | deny | any call with `permission_mode` = `bypassPermissions` | the same call in `default` or `acceptEdits` |
| 2. approve.py is the human's | deny | any shell command whose text, with quotes and backslashes removed, contains `approve` in any case: `cat gates/approve.py`, `python gates/appr""ove.py`, `git commit -m "ask the human to approve"` | `python gates/cli.py board`; read the file with the Read tool; `git commit -F message.txt` |
| 3. The store by hand | deny | Edit/Write/MultiEdit/NotebookEdit of a file whose name starts with `gates.json` (`GATES.json`, `sub/../gates.json`, a symbolic link to it); a shell write of it: `echo x > gates.json`, `echo x &> gates.json`, `cp`, `mv`, `rm`, `tee`, `sed -i`, `git checkout HEAD -- gates.json`, `git restore gates.json`, `git diff --output=gates.json`, `rm -rf .`, `cp -R stage/. .`, `cp stage/gates.json example/` | `python gates/cli.py --store gates.json board` from the project root; `git add\|diff\|show\|log\|status gates.json` |
| 3. The store by hand | ask | a command that names the store in any other form: `cat gates.json`, `cd example && python gates/cli.py --store ../gates.json board` | the Read tool on `gates.json` |
| 4. Logs and verdicts | deny | a write in `progress/` at the project root (`Progress/T-001.md`, `cd progress && echo x >> T-001.md`, `echo x >>!progress/T-001.md`); a write of a verdict path recorded in the store (`Reviews/T-001-verdict-c1.md`), or of a folder that holds one (`rm -r reviews`, `mv reviews old`, `cp x.md reviews/`); any write in the project while the store cannot be read | Write of the next cycle's verdict, `reviews/T-001-verdict-c2.md`; `cp reviews/_VERDICT_TEMPLATE.md reviews/T-001-verdict-c2.md`; `git log progress/` |
| 4. Logs and verdicts | ask | a command that names them in any other form: `cd progress`, `ls progress` | `python gates/cli.py note T-001 --actor worker TEXT` |
| 5. Main through pull requests | deny | `git push` whose target is main (`main`, `MAIN`, `+main`, `HEAD:main`, `x:refs/heads/main`, `:main`), `--all`, `--mirror`, `--branches` or an abbreviation (`--mir`), a pattern refspec; `git push`, `git push origin` or `git push origin HEAD` while main is checked out; `git merge`, `git rebase` or `git pull` while main is checked out; `git rebase feature main` | `git push -u origin feature`, `git merge main` and `git pull` on a feature branch, `git merge-base main HEAD` |
| 5. Main through pull requests | ask | the branch cannot be read: after `cd`, `git checkout`, `git switch`, `git branch -m` or `git rebase` in the same command, outside a repository; a push after `git config`, `git remote` or `git branch -u` in the same command | `git push` alone on a feature branch |
| 6. Merges after the human's go | deny | `gh pr merge` in any form but `gh pr merge N [--squash\|--merge\|--rebase] [--delete-branch] [--auto] --match-head-commit SHA`, or that form unless a task has `pr` N, state `pr_open` and `merge_approved_head` equal to SHA; a missing or unreadable store; `.../pulls/N/merge`, `.../merges`, `mergePullRequest`, `automerge` or `git/refs/heads/main` in `gh api`, `curl` or `wget` | `gh pr merge 7 --squash --match-head-commit <the head approve.py printed>`, `gh pr view 7` |
| 6. Merges after the human's go | ask | `gh api` with a method other than GET (`-X PUT`, or `-f`/`-F`/`--input`, which imply POST), `gh api graphql`; `gh alias`; `pr` and `merge` in another order | `gh api repos/o/r/pulls/7` |
| 7. The adapter protects itself | deny | Edit/Write of anything in `.claude/` or of `~/.claude/settings.json`; anything in a `.git` folder or file (`.git/hooks/pre-commit`, `.git/config`) or the user's git configuration (`~/.gitconfig`, `~/.config/git/`); a shell write of them (`cp x.json .claude/settings.json`, `cp settings.json ~/.claude/`, `echo x >> .git/config`) | the Read tool on `.claude/settings.json`; `git add .claude` |
| 7. The adapter protects itself | ask | a command that names them in any other form: `cat .claude/settings.json`, `bash .claude/hooks/test-gate-guard.sh` | the human runs the guard's tests |
| Event | deny | an event that is not JSON, has no `tool_input`, a Bash or PowerShell call with no command, `python3` missing | any other tool with no `command`: Read, Grep, Monitor watching a socket |

## Running the tests

From the repository root:

    bash .claude/hooks/test-gate-guard.sh        # the bash on PATH
    /bin/bash .claude/hooks/test-gate-guard.sh   # macOS bash 3.2

Each line of `hooks/fixtures/cases.txt` is one case, `NAME EXPECTED EVENT`, where EXPECTED is `deny`, `ask` or
`pass` and EVENT is one hook event. Fixture stores live in `hooks/fixtures/stores/`, with stand-in `.py` files
where a case runs `python FILE.py`; the git cases run against two temporary repositories the script creates and
deletes. The script prints `ok` or `FAIL` per case, then `N passed, M failed`.

## Limits

The guard stops accidental and obvious routes. It is not a sandbox against an adversarial agent.

- **An allowed program that runs code the agent wrote is not inspected.** `python tool.py` passes once `tool.py`
  exists in the project, and `python -m unittest` runs the test files it finds (and a `unittest.py` in the working
  folder in place of the standard one); git runs hooks and configured programs (diff drivers, filters, merge
  strategies) on `commit`, `merge`, `pull`, `checkout`. Writes into `.git/` and the user's git configuration are
  denied, but git also reads a system configuration (Homebrew's `etc/gitconfig` is writable by the user) and
  `core.hooksPath` set there. Such a program can forge the store (including a `merge_approved_head` that then
  lets `gh pr merge` pass rule 6), push to main or run `gates/approve.py` with the agent variable cleared under a
  pseudo-terminal, which passes walls 2 and 3.
- **The shell is read with POSIX rules, and the Bash tool may run zsh.** The guard reads words with `shlex` and
  asks on the zsh forms it knows (`=cmd`, `>!`, `noglob`, `repeat`), but a zsh-only form it does not know is read
  as POSIX text. PowerShell commands are read with the same rules, so most of them ask. Functions and aliases from
  the user's shell profile (the Bash tool loads a snapshot of it), a program earlier on `PATH` with an allowlisted
  name, and a `CDPATH` set in the user's environment, change what an allowlisted word runs or where `cd` goes, and
  leave no trace in the command.
- **Git and GitHub state set before the call.** git and gh aliases in configuration; a push target set by
  configuration (`remote.<name>.push`, `push.default`, a branch's upstream), so `git push` or
  `git push origin feature` can still update main (the settings ask tier prompts on every `git push`). `git merge`,
  `git rebase` and `git pull` bring in the store, progress logs and verdicts of the commits they merge, as git
  history shows. A `gh api` GET is never checked.
- **Commands that change files without naming them** ask, except the ones above; here-document bodies are read as
  commands, which can deny or ask on a harmless text. Only the Edit, Write, MultiEdit and NotebookEdit tools are
  checked as file writers; an MCP tool that writes files through other input fields is not.
- **Paths.** Symbolic links are followed for paths that exist when the call is checked; a link or hard link made
  later, or one that existed before the session, can still reach a protected file.
- **The guard holds only while the harness runs hooks.** Without Claude Code, with project settings that are not
  loaded, with `disableAllHooks`, or in another harness, nothing in this folder runs. In auto mode a pass goes to
  the classifier with no human, so a route the guard passes by mistake matters most there.
- **Fail-open cases.** Per the Claude Code hooks reference, a hook that fails (an exit status other than 0 or 2)
  or times out lets the call go ahead unless the hook entry sets `"onFailure": "block"`. This settings file sets
  it, so a missing `gate-guard.sh`, a crash or a timeout blocks the call, but only on Claude Code v2.1.295 or
  later; an older version ignores the field and lets those calls through. Faults inside `gate_guard.py` are
  turned into denies by the wrapper on every version.
- **The real wall against an adversarial agent** is outside this folder: OS-level isolation (a sandbox or a
  separate user that cannot write the store, the hook or the credentials) and GitHub branch protection on main
  (no direct pushes, required reviews, merge rights the agent's token does not hold).
