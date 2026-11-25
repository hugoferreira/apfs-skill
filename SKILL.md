---
name: apfs-dev
description: APFS techniques for agentic development on macOS - copy-on-write clones (cp -c, clonefile), instant CoW git worktrees with warm node_modules/target caches, automatic copy-on-write worktree guidance for Codex and a Claude Code WorktreeCreate hook, instant directory checkpoints and restore, deduplication of identical files into clones, transparent decmpfs compression, Time Machine local snapshots, case-sensitive or quota-limited volumes, sparse disk images, shadow overlays, RAM disks, and correct disk-space accounting (private size vs du vs df vs purgeable). Use when creating parallel worktrees or sandboxes for agents, copying large trees or dependency dirs, checkpointing before risky changes and undoing them, reclaiming disk space, diagnosing "disk full" or space that does not come back after deleting, choosing copy commands in scripts, or working with build caches on a Mac.
---

# APFS for agentic development

The scripts live in `scripts/` beside this `SKILL.md`. Resolve their paths from the skill directory your agent discovered. The commands below use `APFS_DEV` for that directory. Set it to the absolute path in the shell call that runs the example, such as `APFS_DEV=~/.agents/skills/apfs-dev` for Codex or `APFS_DEV=~/.claude/skills/apfs-dev` for Claude Code.

Every script prints its manual with `--help`. Use Python 3.9 or later for the standard-library scripts, and Python 3.12 or later for `apfs_compress_plus.py`. Its available backends are described in [dedup-compression.md](references/dedup-compression.md). Measurements were taken on macOS 27.2.1 with Apple silicon and an internal SSD on 2026-11-22.

## Deduplicate and compress a directory

```bash
python3 "$APFS_DEV"/scripts/apfs-reclaim.py "$PWD" --apply
```

Use `--apply` when the user asks to run both operations. For an estimate, omit `--apply`. The command finds Python 3.12+ when an older SDK Python is first on PATH, uses macOS `ditto`, compresses eligible cold files, then deduplicates by cloning the smallest compatible copy. It reports estimated APFS allocation before and after, identifies any pre-existing missing compression flags, and prints a state path to reuse if interrupted. Use [dedup-compression.md](references/dedup-compression.md) for tuning, separate operations, and interpreting skips.

## First checks

```bash
python3 "$APFS_DEV"/scripts/apfs-inspect.py --can-clone SRC DEST   # exit 0 only if SRC and DEST can share blocks
python3 "$APFS_DEV"/scripts/apfs-inspect.py --volume .             # fstype, clone/snapshot/case-sensitivity support, free space
```

- Clones only work within one APFS volume. Across volumes (another volume in the same container, an external disk, a disk image, a RAM disk) every "clone" silently becomes a full copy. Keep sources, worktrees, caches and checkpoints on one volume. On a standard Mac `~`, `/private/tmp` and `$TMPDIR` are all on the Data volume.
- `df`, `statvfs` and `diskutil info` ("Container Free Space") report the same figure and exclude purgeable space; Finder's "Available" includes it (41.8 GB vs 54.2 GB in the test). `--volume` reports both (`free_bytes`, `available_incl_purgeable_bytes`).
- Free space on a nearly full Mac fluctuates by gigabytes as macOS purges caches and thins Time Machine snapshots. Do not measure savings with `df` deltas; use `apfs-inspect.py --tree` (private bytes).

## Decision table

| Goal | Use | Reference |
|---|---|---|
| Copy a file or tree on the same volume | `cp -c` / `cp -ac` / `ditto` (clones); never `cp` without `-c`, `rsync`, `tar`, `shutil`, Node `fs.cp*` | [clones.md](references/clones.md) |
| New worktree for a parallel agent, zero extra space | `apfs-worktree.py SRC DEST [-b NEW_BRANCH \| --checkout BRANCH]` | [worktrees.md](references/worktrees.md) |
| Same, with node_modules/target/dist/.env already populated | `apfs-worktree.py SRC DEST --warm --fast-index` | [worktrees.md](references/worktrees.md) |
| Fork the current uncommitted state (same `git status`) | `apfs-worktree.py SRC DEST --wip` | [worktrees.md](references/worktrees.md) |
| Automatically use CoW for worktrees created by Codex, including under `/private/tmp` | Add the worktree policy to an active `AGENTS.md` | [codex-agents.md](references/codex-agents.md) |
| Make Claude Code's own worktrees (`--worktree`, subagent isolation) CoW and warm; remove them and their merged branches | `WorktreeCreate` and `WorktreeRemove` hooks running `claude-worktree-hook.py` | [claude-code.md](references/claude-code.md) |
| Checkpoint a directory before a risky step, restore later | `apfs-checkpoint.py create/diff/restore/drop DIR` | [snapshots.md](references/snapshots.md) |
| Whole-volume point-in-time | `tmutil localsnapshot` (no restore API without entitlements) | [snapshots.md](references/snapshots.md) |
| Find out what a file or tree really occupies | `apfs-inspect.py FILE...` / `--tree DIR` / `--families DIR` | [space.md](references/space.md) |
| Reclaim space from duplicate files | `apfs-dedupe.py ROOT` previews, `--apply` scans and replaces | [dedup-compression.md](references/dedup-compression.md), [apfs-tools.md](references/apfs-tools.md) |
| Copy a tree to another volume, sharing duplicates at the destination | `apfs-transfer.py SRC DEST` | [apfs-tools.md](references/apfs-tools.md) |
| Shrink cold, compressible files transparently | `apfs_compress_plus.py file scan ROOT --backend ditto` previews, `file compress` applies | [dedup-compression.md](references/dedup-compression.md) |
| Case-sensitive or size-capped scratch area | `diskutil apfs addVolume` on a sparse image, `-quota` | [volumes-images.md](references/volumes-images.md) |
| Disposable environment that resets | sparse image + `hdiutil attach -shadow` | [volumes-images.md](references/volumes-images.md) |
| Keep build output out of Time Machine / Spotlight | `tmutil addexclusion DIR`, `.noindex` suffix | [space.md](references/space.md) |
| Compress and deduplicate a tree in one run | `apfs-reclaim.py ROOT --apply` | [dedup-compression.md](references/dedup-compression.md) |

## Rules

1. **Copy with clones.** In scripts and commands, use `cp -c` (file), `cp -ac` / `cp -c -R` (tree), `ditto` (clones by default), or `clonefile(2)`. Verified to produce full copies instead: plain `cp`, `rsync`/openrsync, `tar | tar`, Python `shutil.copy*`/`copytree`, Node 26 `fs.copyFile*`/`fs.cp*` (even with `COPYFILE_FICLONE`; `COPYFILE_FICLONE_FORCE` fails with ENOSYS). Rust `std::fs::copy` and Bun `Bun.write(dst, Bun.file(src))` clone.
2. **Clone directories in one call when speed matters.** `clonefile(src_dir, dst, 0)` cloned 6,000 files in 0.07 s; `cp -c -R` took 0.94 s and `ditto` 1.1 s. The man page discourages it for hierarchies: it is one uninterruptible call that fails as a whole (one unreadable item gives EACCES for the entire tree) and copies ownership. Hard links inside the tree become independent files; sockets and FIFOs are recreated. Use it for trees the agent owns (checkpoints, caches); use `cp -ac` for arbitrary user trees.
3. **Writes break sharing only where they land.** A 1-byte write to a 190 MiB clone allocated 16 KiB of private data; the rest stays shared. Tools that rewrite whole files (most editors, `sed -i`, formatters, `git checkout`) unshare the entire file.
4. **Compression is lost on write.** Appending, in-place writes and `sed -i` decompress a decmpfs file; `touch`, `chmod` and `xattr -w` keep it. Plain `cp`, `cat >`, and `shutil` decompress; `cp -c`, `ditto` and `ditto --noclone` keep it. `ditto --hfsCompression` on the same volume clones instead of compressing; add `--noclone`. Compress only cold files (archives, toolchains, old build caches, logs), never files under active edit. The compression script skips files with shared APFS extents, so compress before deduplicating, as `apfs-reclaim.py` does. After a dedupe-first run, compress one independent copy and let dedupe choose it as the smaller keeper.
5. **Sparse files explode under naive copies.** `truncate -s`/`mkfile -n`/seek-and-write produce sparse files (0 allocated). `cp` and `cp -c` keep them sparse; `shutil.copy2` writes every zero (100 MiB allocated for a 0-byte-allocated file). Docker/VM disk images are the usual victims.
6. **Deleting does not always free space.** Blocks shared with a clone, a checkpoint, or a Time Machine local snapshot stay allocated. Check `private` with `apfs-inspect.py`; list checkpoints with `apfs-checkpoint.py list DIR`; list snapshots with `tmutil listlocalsnapshots /`; thin them with `tmutil thinlocalsnapshots / <bytes> 4`, which deletes snapshots without prompting, so confirm with the user first.
7. **Do not trust `du` or Finder for clones.** `du` counts every clone in full (572 MiB reported for 381 MiB on disk in the test). Use `apfs-inspect.py --tree` (`private` is a lower bound of what deletion would free; `estimated_physical` is exact for pure clones and over-counts partially diverged ones).
8. **Default volumes are case-insensitive and normalization-insensitive.** `Foo` and `foo` are one file; git repos with case-only renames or names differing only in Unicode normalization break. Use a case-sensitive volume for such repos (rule-of-thumb test: `touch Foo foo; ls`).
9. **Snapshots need privileges.** Creating, mounting, or reverting APFS snapshots requires root and, for arbitrary volumes or revert, Apple-granted entitlements (`fs_snapshot_create: Operation not permitted` as a user, even on a user-attached disk image). Use clone checkpoints for per-directory undo.
10. **A restore replaces the directory inode.** After `apfs-checkpoint.py restore DIR`, any process whose cwd or open files are inside DIR (including the shell that ran the restore from inside DIR) still refers to the swapped-out tree. Run restore from outside DIR or `cd` to DIR's absolute path again; restart dev servers and watchers.
11. **Never clone `.git` or a linked worktree by hand.** A cloned `.git` directory is a second repository; a cloned linked worktree shares one index with its original. Use `apfs-worktree.py`, which registers a real worktree and skips other worktrees and nested linked checkouts.
12. **Check the authorized scope.** A request to run deduplication or compression on a named tree authorizes applying it there. A request to inspect or estimate calls for a preview. Confirm before deleting snapshots, changing volumes on the internal disk, or changing the user's Git config or hook settings unless the user already authorized that change.

## Common commands

```bash
# clone a file / tree (same volume)
cp -c big.bin big-copy.bin
cp -ac node_modules ../other-checkout/node_modules
```

```bash
# CoW worktree for an agent (tracked files cloned, verified against REF)
python3 "$APFS_DEV"/scripts/apfs-worktree.py . ../wt-agent1 -b agent1
python3 "$APFS_DEV"/scripts/apfs-worktree.py . ../wt-agent2 -b agent2 --warm --fast-index   # with ignored caches
python3 "$APFS_DEV"/scripts/apfs-worktree.py . ../wt-fix --checkout existing-branch --require-clone
git worktree remove --force ../wt-agent1 && git branch -d agent1                                          # cleanup
```

```bash
# checkpoint / restore a directory (keeps the current .git unless --with-git)
python3 "$APFS_DEV"/scripts/apfs-checkpoint.py create "$PWD" -m "before migration"
python3 "$APFS_DEV"/scripts/apfs-checkpoint.py diff "$PWD"       # M/A/D/T path, relative to the checkpoint
python3 "$APFS_DEV"/scripts/apfs-checkpoint.py restore "$PWD" && cd "$PWD"
python3 "$APFS_DEV"/scripts/apfs-checkpoint.py drop "$PWD" --all
```

`restore` without an ID always returns to the newest checkpoint made with `create`, however many restores came before; each restore saves the replaced state as `pre-restore-*` (undo with `restore DIR pre-restore-...`, reclaim with `drop DIR --pre-restore`).

```bash
# what does it really cost?
python3 "$APFS_DEV"/scripts/apfs-inspect.py file1 file2
python3 "$APFS_DEV"/scripts/apfs-inspect.py --tree ~/Development --json
python3 "$APFS_DEV"/scripts/apfs-inspect.py --families some/dir
```

```bash
# preview dedupe and compression
python3 "$APFS_DEV"/scripts/apfs-reclaim.py ~/Development --json
```

## Symptoms

| Symptom | Cause | Action |
|---|---|---|
| "Clone" took as long as a copy; `copied_files` > 0; `warning: ... another volume` | SRC and DEST on different volumes | `apfs-inspect.py --can-clone SRC DEST`; move DEST; `--require-clone` to fail early |
| Deleted a tree, free space unchanged | clones, checkpoints or local snapshots hold the blocks | rule 6 |
| `du` total exceeds the volume's used space | clones counted once per file | `apfs-inspect.py --tree` |
| `clonefile ...: Permission denied` on a directory | one unreadable item fails the whole call | fix permissions, or `cp -ac` and accept per-file errors |
| `clonefile ...: Invalid argument` | destination inside source, or a socket/FIFO cloned as a single file | choose a destination outside the tree |
| New worktree: tools run the old checkout's interpreter | a cloned `.venv`/CMake dir embeds absolute paths | recreate it (`uv sync`, `cmake -B`); `--warm` excludes these names |
| After restore, commands see stale files | cwd inode is the swapped-out tree | rule 10 |
| `git status` slow once after cloning | git rehashes clones (new inode/ctime) | expected once; `--fast-index` avoids it |

## Package managers that already clone

- **pnpm**: `package-import-method=auto` tries clone first on macOS (then hardlink, copy). Set `clone` explicitly if packages in `node_modules` get patched; hardlinks share edits with the store.
- **Bun**: `bun install --backend clonefile` is the default on macOS.
- **uv**: `--link-mode clone` (`UV_LINK_MODE`); clone is the macOS default per uv's source. Cache and project must be on the same volume, otherwise uv warns and copies.
- **npm/yarn classic**: copy. Clone a populated `node_modules` with `cp -ac` or `apfs-worktree.py --warm` instead of reinstalling.
- **Cargo**: `std::fs::copy` clones; `target/` is automatically excluded from Time Machine and tagged with `CACHEDIR.TAG`.

## Scripts, provenance, tests

| File | Role |
|---|---|
| `apfs_common.py` | Shared module: the only libSystem bindings (clonefile, copyfile, renamex_np, getattrlist, statfs, xattr, ACL, libproc), signatures, hashing, atomic writes, tree removal, byte formatting. Every script imports it. |
| `apfs_publish.py` | Journaled compression publication and recovery. |
| `apfs_job.py` | Job state for long runs: lock, status, pause/resume/stop, checkpoints, progress. Used by dedupe and transfer. |
| `apfs-inspect.py`, `apfs-worktree.py`, `apfs-checkpoint.py`, `claude-worktree-hook.py` | Written for this skill. |
| `apfs-dedupe.py` (1.7.0), `apfs-transfer.py` | Started as standalone tools; refactored onto `apfs_common.py` and hardened in this skill. Manual: [apfs-tools.md](references/apfs-tools.md). |
| `apfs_compress_plus.py` (0.5.0) | Started as a standalone tool (0.4.0); same treatment. Tests: `tests/compress/`. |
| `apfs-reclaim.py` | Runs inspection, staged compression, and deduplication with one invocation. |

Keep the scripts in one directory; they import each other by path.

Tests, all on throwaway fixtures under `$TMPDIR`:

```bash
sh "$APFS_DEV"/tests/run.sh          # inspect, worktree, checkpoint, hook, apfs_common (about 5 s)
```

```bash
sh "$APFS_DEV"/tests/run.sh --all    # plus cross-volume cases and the dedupe, transfer and compression suites (about 1 min)
```

`PYTHON=/usr/bin/python3 sh .../run.sh` selects the interpreter (the compression suite is skipped below 3.12). Run the tests after editing a script or upgrading macOS.
