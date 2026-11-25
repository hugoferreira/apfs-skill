# apfs-dev

A skill for [Claude Code](https://claude.com/claude-code) and [Codex](https://chatgpt.com/codex) that teaches an agent to use APFS on macOS: copy-on-write clones, instant git worktrees and directory checkpoints, correct disk-space accounting, deduplication, and transparent compression. It ships the scripts that do the work, the reference notes behind them, and their tests.

The scripts are standalone command-line tools.

## What it provides

| Task | Tool | Measured |
|---|---|---|
| Worktree for a parallel agent that occupies no extra space, optionally with `node_modules`, `target`, `.env` already in place | `apfs-worktree.py` | 6,000 tracked + 6,000 ignored files: 1.2 s, 4 KiB of new data (`git worktree add`, tracked files only: 1.1 s, 141 MiB) |
| Undo everything done to a directory, including ignored and untracked files | `apfs-checkpoint.py` | create 0.26 s, diff 0.4 s for 12,000 files |
| What a file or tree really occupies when clones are involved | `apfs-inspect.py` | `du` reported 572 MiB for 381 MiB on disk |
| Replace duplicate files with clones, safely, in a tree that is in use | `apfs-dedupe.py` | |
| Preview or run transparent compression and deduplication together | `apfs-reclaim.py` | |
| Copy a tree to another volume, verified and resumable, sharing duplicates at the destination | `apfs-transfer.py` | |
| Compress cold files transparently (LZVN/LZFSE/zlib) with a plan, a budget and a state database | `apfs_compress_plus.py` | |
| Make Claude Code's own worktrees (`--worktree`, subagent isolation) copy-on-write, and delete their branches once merged | `claude-worktree-hook.py` | |

`SKILL.md` adds the rules an agent needs to apply these correctly: which copy commands clone and which silently write full copies, why deleting does not always free space, what breaks when a `.venv` or `.git` is cloned, and when snapshots, case-sensitive volumes, sparse images or RAM disks are the right tool.

Measurements were taken on macOS 27.2.1 (Apple silicon, internal SSD) on 2026-11-22. Each reference file states how its numbers were obtained.

## Requirements

- macOS on an APFS volume. Clones need source and destination on the same volume.
- Python 3.9 or later for everything except `apfs_compress_plus.py`, which needs Python 3.12 or later. The system `/usr/bin/python3` (3.9) runs the rest.
- `git` for `apfs-worktree.py`.
- Optional: [`afsctool`](https://github.com/RJVB/afsctool) (`brew install afsctool`) for compression. Without it, `apfs_compress_plus.py` can still scan and inspect, and can compress with `--backend ditto`.

No Python packages are required. Nothing accesses the network.

## Install

### Claude Code

```bash
git clone https://github.com/hugoferreira/apfs-skill.git ~/.claude/skills/apfs-dev
sh ~/.claude/skills/apfs-dev/tests/run.sh
```

Claude Code discovers the skill on its next start. Use `/apfs-dev` to invoke it explicitly.

### Codex

For a standalone Codex install, clone into the personal skills directory.

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/hugoferreira/apfs-skill.git ~/.agents/skills/apfs-dev
sh ~/.agents/skills/apfs-dev/tests/run.sh
```

If you already installed it for Claude Code, share that checkout with Codex. Run this only when `~/.agents/skills/apfs-dev` is free.

```bash
mkdir -p ~/.agents/skills
ln -s ~/.claude/skills/apfs-dev ~/.agents/skills/apfs-dev
```

Use `$apfs-dev` to invoke the skill explicitly. For automatic use on agent-created worktrees, add the [Codex worktree instructions](references/codex-agents.md) to `~/.codex/AGENTS.md`, or to the active `AGENTS.override.md` if you use one. For project-only use, add them to the project's `AGENTS.md`. The instructions cover temporary worktrees under `/private/tmp`, branch and ref selection, and APFS clone verification. Open a fresh session to load the instructions. [Codex instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md)

The desktop app's **Worktree** control checks out the repository before setup scripts run. To use APFS cloning for the checkout itself, have Codex create the worktree through this skill and open that checkout as a local project. [Codex worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees) · [Local environment setup](https://learn.chatgpt.com/docs/environments/local-environment#setup-scripts)

## Using the scripts directly

```bash
APFS_DEV=~/.agents/skills/apfs-dev # or ~/.claude/skills/apfs-dev

# Can two paths share blocks? What does this volume support?
python3 "$APFS_DEV"/scripts/apfs-inspect.py --can-clone . ../wt-1
python3 "$APFS_DEV"/scripts/apfs-inspect.py --volume .

# A worktree made of clones, on a new branch, with ignored caches
python3 "$APFS_DEV"/scripts/apfs-worktree.py . ../wt-1 -b agent/1 --warm

# Checkpoint, inspect changes, restore
python3 "$APFS_DEV"/scripts/apfs-checkpoint.py create "$PWD" -m baseline
python3 "$APFS_DEV"/scripts/apfs-checkpoint.py diff "$PWD"
python3 "$APFS_DEV"/scripts/apfs-checkpoint.py restore "$PWD" && cd "$PWD"

# Preview or run both operations using built-in macOS compression
python3 "$APFS_DEV"/scripts/apfs-reclaim.py ~/code
python3 "$APFS_DEV"/scripts/apfs-reclaim.py ~/code --apply
```

Every script prints its manual with `--help`.

## Copy-on-write worktrees for Claude Code sessions

Claude Code creates worktrees with `git worktree add`, which writes every tracked file and starts without dependencies or build output. A `WorktreeCreate` hook replaces that step, and a `WorktreeRemove` hook cleans up afterwards. Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "WorktreeCreate": [
      { "hooks": [ { "type": "command",
          "command": "python3 ~/.claude/skills/apfs-dev/scripts/claude-worktree-hook.py" } ] }
    ],
    "WorktreeRemove": [
      { "hooks": [ { "type": "command",
          "command": "python3 ~/.claude/skills/apfs-dev/scripts/claude-worktree-hook.py" } ] }
    ]
  }
}
```

On creation the hook bases the worktree on the session checkout's `HEAD` and clones ignored paths instead of reading `.worktreeinclude`. On removal it refuses to delete a worktree with uncommitted work, removes clean ones, and deletes the `worktree-*` branch only when its commits are contained in another branch. Details and the differences from the built-in behavior are in [references/claude-code.md](references/claude-code.md).

## Safety

- `apfs-inspect.py` only reads.
- `apfs-dedupe.py`, `apfs-transfer.py` and `apfs_compress_plus.py file scan` are dry runs unless told otherwise (`--apply`, `file compress`). `file decompress` and the state-changing `state` commands require `--yes`.
- `apfs-worktree.py` creates a new directory and a worktree registration. It writes to the source repository's config only with `--fast-index`. A failed run removes what it created.
- `apfs-checkpoint.py restore` swaps the directory atomically and keeps the replaced tree as a `pre-restore-*` checkpoint. Nothing is discarded until `drop`.
- Dedupe replaces a file only after hashing both copies and verifying data and metadata of the clone. It skips files that are open, mapped, locked or changing, and journals every step for recovery.
- Transfer never writes to or deletes from the source and never deletes at the destination.
- Compression skips files with shared APFS blocks or files that another process has open for writing or mapped. It replaces executables by copy instead of writing them in place.

Limits that apply throughout: open-file detection sees only the current user's processes unless run as root; clone checkpoints and worktree clones are not atomic with respect to concurrent writers; APFS snapshots cannot be created or reverted without privileges, which is why the skill uses clone checkpoints.

## Repository layout

| Path | Content |
|---|---|
| `SKILL.md` | Checks, decision table, rules, symptoms, and common commands for agents |
| `references/` | One file per topic: clones, worktrees, snapshots and checkpoints, space accounting, volumes and disk images, dedupe and compression, the dedupe/transfer manual, and Claude Code and Codex integration |
| `scripts/apfs_common.py` | Shared module: all libSystem bindings (clonefile, copyfile, renamex_np, getattrlist, statfs, xattr, ACL, libproc) and common helpers |
| `scripts/apfs_job.py` | Job state for long runs: lock, status, pause/resume/stop, checkpoints |
| `scripts/*.py` | The tools listed above; keep them in one directory |
| `tests/` | `run.sh` (shell regression tests), `test_common.py`, `compress/` |
| `scripts/apfs-dedupe-tests.py`, `scripts/apfs-tools-tests.py` | Suites for dedupe, transfer and job control |

## Tests

```bash
sh tests/run.sh          # inspect, worktree, checkpoint, hook, shared module
sh tests/run.sh --all    # plus cross-volume cases and the dedupe, transfer and compression suites (about 1 minute)
```

`PYTHON=/usr/bin/python3 sh tests/run.sh --all` runs under another interpreter; the compression suite is skipped below Python 3.12. `--volumes` and `--all` attach a temporary 100 MB disk image with `hdiutil` and detach it afterwards. All fixtures live under `$TMPDIR` and are removed on exit. The suites were run with `afsctool` installed.

## Status

- Developed and measured on one machine and one macOS version. Behavior of copy tools (which commands clone) changes between OS and runtime versions; `SKILL.md` says how to re-check.
- `claude-worktree-hook.py` is tested against the documented hook inputs and outputs for both events. It has not been exercised from inside a live Claude Code session.
- Versions: `apfs-dedupe.py` 1.7.0, `apfs_compress_plus.py` 0.5.0. The other scripts are unversioned.

## License

MIT. See [LICENSE](LICENSE).
