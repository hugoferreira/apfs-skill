# Copy-on-write worktrees for parallel agents

`git worktree add` writes every tracked file. On APFS the checkout can instead be cloned from an existing checkout: no data is written, and the new worktree occupies only metadata until files change.

## apfs-worktree.py

```bash
python3 ~/.claude/skills/apfs-dev/scripts/apfs-worktree.py SOURCE DEST \
        [--ref REF | -b BRANCH [--ref REF] | --checkout BRANCH] [--warm] [--wip] [--fast-index] \
        [--include NAME]... [--exclude NAME]... [--require-clone] [--dry-run] [--json]
```

Procedure:

1. `git worktree add --no-checkout [--detach | -b BRANCH | BRANCH] DEST REF` registers DEST, so objects and refs are shared with SOURCE.
2. Tracked files whose index entry in SOURCE has the same mode and blob as in REF are cloned from SOURCE's working tree with `clonefile(2)` (per file). Files that REF does not track, or tracks with other content, are not cloned.
3. Default: `git read-tree REF`, `git update-index --refresh` (rehashes each clone once), `git reset --hard REF` (writes only files that differ from REF or are missing, e.g. dirty files in SOURCE). DEST is exactly REF.
4. Across volumes every file is copied with `copyfile(3)` (keeps sparseness and compression), reported as `copied_files`, after a warning on stderr. `--require-clone` fails instead, before anything is created.
5. On any failure the worktree registration, DEST and a branch created with `-b` are removed.

Branch selection: detached at REF by default; `-b BRANCH` creates a branch at REF; `--checkout BRANCH` checks out an existing local branch (git refuses a branch that is checked out in another worktree).

Modes:

- `--warm`: also clone ignored paths (`git ls-files --others --ignored --exclude-standard --directory`): `node_modules`, `target`, `dist`, `.next`, build caches, `.env` files. Each ignored directory is cloned with one `clonefile` call; excluded names are then removed from the clone. Excluded by default because they embed absolute paths or are disposable: `.venv venv .tox .nox CMakeFiles CMakeCache.txt xcuserdata __pycache__ .DS_Store`. Restrict with `--include node_modules --include target`. Never cloned: other worktrees of the repository (e.g. an ignored `.claude/worktrees/`), DEST itself when it lies inside an ignored directory, nested linked checkouts (directories holding a `.git` file), sockets, FIFOs and devices reached through the per-file path. Skipped items are listed in `skipped` (JSON) or on stderr.
- `--wip`: reproduce SOURCE's uncommitted state. Modified tracked files and untracked, non-ignored files are cloned as they are, and SOURCE's index is copied, so staged, unstaged and untracked changes show identically in `git status` of both. Requires REF to be SOURCE's HEAD. Untracked nested repositories are not cloned.
- `--fast-index`: reuse SOURCE's index instead of rehashing. Requires REF == SOURCE's HEAD. Sets `extensions.worktreeConfig=true` in the shared repo config and, for DEST only, `core.checkStat=minimal` and `core.trustctime=false`, because a clone has a new inode and ctime but the source's mtime and size. Git then detects changes in DEST only through whole-second mtime, size, and mode. Ask before using it on a user's repository (it changes the repo config; tools embedding another git implementation may not honor per-worktree config). Refused when `core.worktree` is set in the repository config. To return DEST to full stat checking later: `git -C DEST config --worktree --unset core.checkStat && git -C DEST update-index --refresh` (rehashes once).
- `--exclude NAME` applies to ignored and untracked entries only. Tracked files are always present in DEST.

Not handled:

- Submodules: gitlinks are skipped, as with `git worktree add`; run `git submodule update --init` in DEST.
- Hooks: `post-checkout` does not run (`git worktree add --no-checkout` skips it). Run setup hooks manually if the repository relies on them.
- Sparse checkout settings of SOURCE are not carried over; DEST is a full checkout.
- SOURCE is read while it may be changing. In default mode git verifies every clone against REF, so concurrent edits in SOURCE cannot leak into DEST. With `--wip` or `--warm` the copied state is whatever was on disk at clone time; pause builds and installers in SOURCE for a consistent copy.

Cleanup: `git -C SOURCE worktree remove --force DEST`, then `git branch -d BRANCH` if the branch is merged or unused, and `git worktree prune`. Deleting a worktree frees only its private (diverged) blocks.

## Measurements

Synthetic repo: 6,000 tracked text files (121 MiB) + ignored `node_modules` (6,000 files, 117 MiB). Private bytes from `apfs-inspect.py --tree`.

| Method | Time | New private data | `git status` |
|---|---|---|---|
| `git worktree add` | 1.1 s | 140.6 MiB | clean |
| `git worktree add --no-checkout` + `cp -ac` in batches + `git reset` | 2.5 s | 0 | clean |
| `apfs-worktree.py` | 2.2 s | 0 | clean |
| `apfs-worktree.py --fast-index` | 1.1 s | 0 | clean |
| `apfs-worktree.py --warm --fast-index` (+ node_modules) | 1.2 s | 0 | clean |
| `apfs-worktree.py --wip` | 2.2 s | 0 | same as SOURCE |
| `apfs-worktree.py --wip --warm --fast-index` | 1.3 s | 0 | same as SOURCE |
| `apfs-worktree.py` to a RAM disk (copy fallback, `--warm`) | 6.2 s | full | clean |

The default mode is slower than `git worktree add` for small text repos because `update-index --refresh` reads every file once (1.2 s of the 2.2 s). The clone approach wins on space always, on time with `--fast-index`, with large binary assets (no writes), and with `--warm` whenever it replaces a dependency install or cold build.

## Agent workflow

```bash
# one worktree per agent, each on its own branch, sharing warm caches
for n in 1 2 3; do
  python3 ~/.claude/skills/apfs-dev/scripts/apfs-worktree.py . ../wt-$n -b agent/$n \
      --warm --include node_modules --include target --require-clone
done
# ... agents work, commit on agent/N ...
git worktree remove --force ../wt-2 && git branch -D agent/2     # discard an attempt
```

- Put worktrees on the same volume as the source (a sibling directory, or inside the repo under an ignored directory such as `.claude/worktrees/`). On a standard Mac `/private/tmp`, `$TMPDIR` and `~` are all on the Data volume; external disks, disk images and RAM disks are not.
- After `--warm`, a cloned `node_modules` needs no reinstall. Re-run the package manager only if lockfiles differ between SOURCE and REF.
- Cargo: workspace crates may rebuild in the new path because fingerprints include paths; registry dependencies in `target/` are typically reused. Not measured here.
- Never clone `.git` itself to make a worktree (that creates an independent repository with its own refs).
- Editors and formatters that rewrite files unshare those files only; the rest stays shared.
- To have Claude Code create its own session and subagent worktrees this way, see [claude-code.md](claude-code.md).

## Alternatives

- `git worktree add` + `cp -ac SOURCE/node_modules DEST/` for a quick manual version.
- `git clone --local` (hardlinks objects, separate refs) for a fully independent repo.
- Existing tools using the same idea: grove (cp -c -R backend), cow-cli, git-workon.
