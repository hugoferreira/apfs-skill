# Claude Code integration: CoW worktrees through the WorktreeCreate hook

Claude Code creates git worktrees for `claude --worktree`, the `EnterWorktree` tool, subagents with `isolation: "worktree"`, workflow agents, background sessions and `claude remote-control` in worktree mode. By default it runs `git worktree add` under `REPO/.claude/worktrees/NAME` on branch `worktree-NAME`, which writes every tracked file and starts without `node_modules` or build output.

A `WorktreeCreate` hook replaces that behavior. `scripts/claude-worktree-hook.py` implements the hook with `apfs-worktree.py`: tracked files are clones, and with `--warm` (the default) ignored paths are clones too.

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
    ]
  }
}
```

Arguments after the script path go to `apfs-worktree.py` and replace the default `--warm`, e.g. `... claude-worktree-hook.py --warm --include node_modules --include target` or `... claude-worktree-hook.py --warm --fast-index` (changes the repository config, see worktrees.md).

## Hook contract (Claude Code hooks reference, read 2026-11-22)

- Input on stdin: JSON with the common fields (`session_id`, `cwd`, `hook_event_name`, ...) plus `name`, a slug that is user-specified or generated.
- Output: the worktree path as the last non-empty line of stdout; everything else must go to stderr. Absolute paths containing `.` or `..` and paths through a symlink below the repository root are refused.
- A non-zero exit or a missing path fails the worktree creation.
- No matcher support; the hook fires on every worktree creation. Default command-hook timeout: 600 s.
- With a `WorktreeCreate` hook, `.worktreeinclude` is not processed; the hook must provide ignored files itself (`--warm` does).
- Removal: without a `WorktreeRemove` hook, Claude Code runs `git worktree remove --force` on the returned path when the session exits. It never deletes the branch of a hook-created worktree, and its periodic cleanup sweep does not remove hook-created worktrees. A hook-created worktree of a finished subagent or workflow agent is kept, and no `WorktreeRemove` hook runs for it.

## What the hook does

1. Resolves the checkout containing `cwd` (SOURCE) and the main repository root.
2. Target: `ROOT/.claude/worktrees/NAME`, branch `worktree-NAME` (slashes in NAME become dashes in the branch name).
3. An existing worktree at the target is reused. An existing branch `worktree-NAME` is checked out (`--checkout`); otherwise the branch is created at SOURCE's HEAD.
4. Runs `apfs-worktree.py SOURCE TARGET ...` with its output on stderr and prints TARGET.

Differences from the built-in behavior:

- Base commit is HEAD of the session's checkout (equivalent to `worktree.baseRef: "head"`), not the remote default branch. Cloning needs a base whose files are already on disk; a different base would be written in full by git.
- `--warm` clones every ignored path except the default exclusions (`.venv`, CMake caches, ...), a superset of what `.worktreeinclude` selects, at no space cost.
- Add `.claude/worktrees/` to `.gitignore` (or `.git/info/exclude`) so the main checkout's `git status` stays clean. `apfs-worktree.py` never clones that directory's worktrees into new ones.

Leftover branches and worktrees after sessions:

```bash
git worktree list
git worktree remove --force .claude/worktrees/NAME && git branch -d worktree-NAME   # -d refuses unmerged branches
git worktree prune
```

## Verification status

`tests/run.sh` feeds the hook the documented input and checks path output, branch, warm caches, reuse and name validation. The hook was not exercised from inside a running Claude Code session; after installing, run `claude --worktree test-cow` once and check `python3 ~/.claude/skills/apfs-dev/scripts/apfs-inspect.py --tree .claude/worktrees/test-cow` (`private` near zero).

## Other agent-relevant facts

- Environment variables do not persist between Bash tool calls; the working directory does. Write full script paths in each command.
- A Bash call that runs `apfs-checkpoint.py restore` from inside the restored directory continues in the swapped-out tree for the rest of that call; later calls start from the path again and see the restored tree.

## Sources

- Hooks reference (WorktreeCreate, WorktreeRemove): https://code.claude.com/docs/en/hooks.md
- Worktrees guide (`.worktreeinclude`, default location, `worktree.baseRef`, cleanup): https://code.claude.com/docs/en/worktrees.md
- Bash tool persistence: https://code.claude.com/docs/en/tools-reference.md
