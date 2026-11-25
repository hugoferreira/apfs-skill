# Claude Code integration: CoW worktrees through the worktree hooks

Claude Code creates git worktrees for `claude --worktree`, the `EnterWorktree` tool, subagents with `isolation: "worktree"`, workflow agents, background sessions and `claude remote-control` in worktree mode. By default it runs `git worktree add` under `REPO/.claude/worktrees/NAME` on branch `worktree-NAME`, which writes every tracked file and starts without `node_modules` or build output.

A `WorktreeCreate` hook replaces that behavior. `scripts/claude-worktree-hook.py` implements it with `apfs-worktree.py`: tracked files are clones, and with `--warm` (the default) ignored paths are clones too. The same script, registered for `WorktreeRemove`, removes those worktrees and deletes their branches once merged.

## Install

Editing `settings.json` changes how every worktree of the user is created: ask first. Add to `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one project):

```json
{
  "hooks": {
    "WorktreeCreate": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/skills/apfs-dev/scripts/claude-worktree-hook.py"
          }
        ]
      }
    ],
    "WorktreeRemove": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/skills/apfs-dev/scripts/claude-worktree-hook.py"
          }
        ]
      }
    ]
  }
}
```

The script acts on `hook_event_name`, so both entries use the same command. The two hooks are independent: `WorktreeCreate` alone is valid (Claude Code then removes worktrees with `git worktree remove --force` and never deletes their branches).

In the `WorktreeCreate` entry, arguments after the script path go to `apfs-worktree.py` and replace the default `--warm`, e.g. `... claude-worktree-hook.py --warm --include node_modules --include target` or `... claude-worktree-hook.py --warm --fast-index` (changes the repository config, see worktrees.md). In the `WorktreeRemove` entry the only argument is `--force` (remove despite uncommitted work).

## Hook contract (Claude Code hooks reference, read 2026-11-22)

- Input on stdin: JSON with the common fields (`session_id`, `cwd`, `hook_event_name`, ...) plus `name`, a slug that is user-specified or generated.
- Output: the worktree path as the last non-empty line of stdout; everything else must go to stderr. Absolute paths containing `.` or `..` and paths through a symlink below the repository root are refused.
- A non-zero exit or a missing path fails the worktree creation.
- No matcher support; the hook fires on every worktree creation. Default command-hook timeout: 600 s.
- With a `WorktreeCreate` hook, `.worktreeinclude` is not processed; the hook must provide ignored files itself (`--warm` does).
- `WorktreeRemove` fires when a worktree that a `WorktreeCreate` hook created is being removed: on exit of an interactive worktree session when removal is chosen, on `ExitWorktree`, when a background session in the worktree is deleted. Input: the common fields plus `worktree_path` (absolute).
- With a `WorktreeRemove` hook, the hook must delete the directory. Exit 0 counts as removed. A non-zero exit fails the removal if the directory still exists, and there is no git fallback. Hook output is discarded.
- Without a `WorktreeRemove` hook, Claude Code runs `git worktree remove --force` on the path when the session exits.
- Claude Code never deletes the branch of a hook-created worktree, and its periodic cleanup sweep does not remove hook-created worktrees. A hook-created worktree of a finished subagent or workflow agent is kept, and no `WorktreeRemove` hook runs for it.
- The docs describe a keep-or-remove prompt for worktrees with work in them only for worktrees Claude Code created with git. For hook-created worktrees no prompt is documented, so the remove hook checks for uncommitted work itself.

## What the hook does on WorktreeCreate

1. Resolves the checkout containing `cwd` (SOURCE) and the main repository root.
2. Target: `ROOT/.claude/worktrees/NAME`, branch `worktree-NAME` (slashes in NAME become dashes in the branch name).
3. An existing worktree at the target is reused. An existing branch `worktree-NAME` is checked out (`--checkout`); otherwise the branch is created at SOURCE's HEAD.
4. Runs `apfs-worktree.py SOURCE TARGET ...` with its output on stderr and prints TARGET.

Differences from the built-in behavior:

- Base commit is HEAD of the session's checkout (equivalent to `worktree.baseRef: "head"`), not the remote default branch. Cloning needs a base whose files are already on disk; a different base would be written in full by git.
- `--warm` clones every ignored path except the default exclusions (`.venv`, CMake caches, ...), a superset of what `.worktreeinclude` selects, at no space cost.
- Add `.claude/worktrees/` to `.gitignore` (or `.git/info/exclude`) so the main checkout's `git status` stays clean. `apfs-worktree.py` never clones that directory's worktrees into new ones.

## What the hook does on WorktreeRemove

1. Refuses, leaving everything in place (exit 1, reason on stderr), when
   - the path is not the root of a linked git worktree (a plain directory or the main checkout is never deleted),
   - `git status` shows modified, staged or untracked files (ignored files do not count),
   - HEAD is detached on commits that no branch, remote-tracking branch or tag contains.
2. Removes the worktree with `git worktree remove`; cloned caches and other ignored files go with it.
3. Deletes the worktree's `worktree-*` branch when its tip is contained in another local branch or in a remote-tracking branch other than its own pushed copy, so no commit becomes unreachable. This covers the common case of a worktree that never received a commit. A branch with commits of its own is kept and reported (`kept branch worktree-NAME: 3 commit(s) not in any other branch`). If the worktree was switched to another branch, the original `worktree-NAME` branch is judged the same way.
4. `--force` in the hook command skips the checks of step 1 and passes `--force` to `git worktree remove`. It never forces a branch deletion.

Limits: a squash- or rebase-merged branch has no commit in common with its target and is kept; delete it by hand. Branches other than `worktree-*` are never deleted. A refused removal leaves the worktree registered; remove it later with:

```bash
git worktree list
git worktree remove --force .claude/worktrees/NAME && git branch -D worktree-NAME   # discards its work
git worktree prune
```

## Verification status

`tests/run.sh` feeds the script the documented inputs. For creation it checks path output, branch, warm caches, reuse and name validation. For removal it checks unused, unmerged and merged branches, the three refusals, `--force`, non-worktree paths and a missing path. Neither hook was exercised from inside a running Claude Code session; the description of `WorktreeRemove` in the hooks reference was read through a summarizing fetch, so its wording here is not verbatim; after installing, run `claude --worktree test-cow` once and check `python3 ~/.claude/skills/apfs-dev/scripts/apfs-inspect.py --tree .claude/worktrees/test-cow` (`private` near zero).

## Other agent-relevant facts

- Environment variables do not persist between Bash tool calls; the working directory does. Write full script paths in each command.
- A Bash call that runs `apfs-checkpoint.py restore` from inside the restored directory continues in the swapped-out tree for the rest of that call; later calls start from the path again and see the restored tree.

## Sources

- Hooks reference (WorktreeCreate, WorktreeRemove): https://code.claude.com/docs/en/hooks.md
- Worktrees guide (`.worktreeinclude`, default location, `worktree.baseRef`, cleanup): https://code.claude.com/docs/en/worktrees.md
- Bash tool persistence: https://code.claude.com/docs/en/tools-reference.md
