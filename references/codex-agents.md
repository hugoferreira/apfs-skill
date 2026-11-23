# APFS worktrees

On macOS, automatically load and apply `$apfs-dev` before creating a Git worktree, including isolated worktrees for agents, experiments, benchmarks, and temporary work under `/private/tmp`. Resolve the skill directory from Codex's skill discovery, typically `~/.agents/skills/apfs-dev`.

- Use the skill's `scripts/apfs-worktree.py SOURCE DEST` with the requested ref and branch options. Use Git normally to inspect and remove registered worktrees.
- For a temporary worktree without a specified path, create a unique parent with `mktemp -d /private/tmp/codex-worktree.XXXXXX` and use a child of that directory as `DEST`. Check the source and destination with `scripts/apfs-inspect.py --can-clone SOURCE DEST`. Use `--require-clone` when they support APFS cloning. Honor an explicitly requested destination or isolation method.
- Use a clean checkout by default. Use `--wip` when the task calls for carrying current uncommitted changes. When warming caches, limit `--warm` to relevant paths through `--include`. Use `--fast-index` only if the user requests it because it changes shared Git configuration.
- Verify the new checkout's commit and Git status. When CoW matters, check the helper's clone and copy counts with `--json`. If the requested destination cannot share blocks, report that before relying on CoW.
