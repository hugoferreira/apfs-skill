# Snapshots and checkpoints

## APFS snapshots: what an agent can and cannot do

| Operation | Command | Works as a normal user? |
|---|---|---|
| List snapshots of a volume | `tmutil listlocalsnapshots /`, `diskutil apfs listSnapshots /` | yes |
| Create a Time Machine local snapshot | `tmutil localsnapshot` | creates `com.apple.TimeMachine.<date>.local` snapshots, auto-deleted after ~24 h or under space pressure; not exercised for this skill (it pins space on a nearly full disk) |
| Create a snapshot of an arbitrary volume | `fs_snapshot_create(2)`, `apfs_systemsnapshot -s` | **no**: `Operation not permitted`, also on a user-attached disk image. Needs root + `com.apple.developer.vfs.snapshot` entitlement (granted only to backup vendors) |
| Mount a snapshot read-only | `sudo mount_apfs -o rdonly,nobrowse -s SNAPNAME /System/Volumes/Data /path/mnt` | **no** without sudo |
| Revert a volume to a snapshot | `fs_snapshot_revert(2)` | effectively no: private entitlement; no supported CLI |
| Delete / thin snapshots | `tmutil deletelocalsnapshots DATE`, `tmutil thinlocalsnapshots / BYTES 4`, `diskutil apfs deleteSnapshot` | may need sudo; destructive, ask first |
| Compare a snapshot with the live tree | `tmutil compare SNAPSHOT_PATH` | requires a mounted snapshot |

Snapshots cover whole volumes; nothing can be excluded from them. Time Machine exclusions do not exclude data from local snapshots.

Consequence for disk space: deleting files after a local snapshot was taken does not free their blocks until the snapshot is removed. "Deleted 50 GB, free space unchanged" usually means snapshots (`tmutil listlocalsnapshots /`) or clones.

## Clone checkpoints: per-directory undo without privileges

`apfs-checkpoint.py` emulates snapshots for one directory using `clonefile(2)` on the whole tree.

```bash
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py create DIR [ID] -m "before dependency upgrade"   # 0.26 s for 12,000 files + .git
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py list DIR
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py diff DIR [ID]          # M/A/D/T path; 0.4 s for 12,000 files
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py restore DIR [ID]       # atomic swap; old state kept as pre-restore-*
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py drop DIR ID | --pre-restore | --all
```

- Store: `DIR/../.DIRNAME.apfs-checkpoints/<id>/tree` (same volume, outside DIR so DIR's tools, watchers and globbing do not see it). Override with `--store`; a store inside DIR or on another volume is refused. The store is excluded from Time Machine backups (xattr `com.apple.metadata:com_apple_backup_excludeItem`), which would otherwise back up every checkpoint in full.
- Captures tracked, untracked and ignored files (including `node_modules`, build outputs, local databases). Hard links inside DIR become independent files in the checkpoint. One unreadable item fails the whole `create` (EACCES).
- IDs: generated from the timestamp, or given (`create DIR before-upgrade`); letters, digits, `.`, `_`, `-`.
- Default ID for `diff` and `restore`: the newest checkpoint made with `create`. `pre-restore-*` checkpoints are used only when named. The loop create, attempt, restore, attempt, restore therefore returns to the same baseline each time.
- `diff` reports `M` (content, symlink target or executable bit), `A` (added since the checkpoint), `D` (deleted since), `T` (type changed); added or deleted directories are one line with a trailing `/`; entries named `.git` are skipped. Files whose APFS clone id equals that of the checkpoint copy share one data stream and are reported identical without being read; the rest are compared byte by byte. `--json` gives `{id, changes: [{status, path}]}`.
- `restore` clones the checkpoint to a staging dir and swaps it with DIR via `renamex_np(RENAME_SWAP)` (atomic, verified to work on directories). The swapped-out tree becomes checkpoint `pre-restore-<time>`, so a restore is itself undoable: `restore DIR pre-restore-<time>`. SIGINT/SIGTERM are held from the swap until the old tree is registered.
- `restore` keeps DIR's current `.git` (commits, refs, index made after the checkpoint survive; `git status` then shows the restored files as changes). The live `.git` is cloned into the staged tree, so DIR has a `.git` at every instant and each `pre-restore-*` tree is a complete repository. `--with-git` restores the checkpoint's `.git` instead and is refused when the checkpoint holds none.
- In a linked worktree `.git` is a file; HEAD and the index live in the main repository's `.git/worktrees/NAME` and are neither captured nor restored.
- Processes with their cwd or open files inside DIR keep referring to the swapped-out tree, including the shell that ran `restore` from inside DIR. `cd` to DIR's path again; restart dev servers, file watchers and shells after a restore.
- Creation is not atomic with respect to concurrent writers in DIR; pause writers (dev servers, builds) first. Live SQLite databases need their `-wal`/`-shm` to be consistent.
- Cost: metadata only at creation. Files changed or deleted in DIR afterwards keep their old blocks alive in the checkpoint, and every restore adds a `pre-restore-*` checkpoint. `drop DIR --pre-restore` or `drop DIR --all` reclaims the space; `apfs-inspect.py --tree STORE` shows what a store pins (`private`).
- `drop` removes read-only directories and `uchg` files, and with `--all` also slots left incomplete by an interrupted run. Other content of a user-supplied `--store` directory is left alone.

Checkpoint vs git stash/commit: a checkpoint also covers ignored and untracked files and has no merge semantics; use git for history, checkpoints for "undo everything the agent just did to this tree".

Agent loop:

```bash
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py create /abs/project -m baseline
# attempt; on failure:
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py diff /abs/project            # what the attempt changed
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py restore /abs/project && cd /abs/project
# on success:
python3 ~/.claude/skills/apfs-dev/scripts/apfs-checkpoint.py drop /abs/project --all
```

## Disk-image level snapshots

For full reset semantics on a disposable environment, use a disk image with a shadow file (see volumes-images.md): attach with `hdiutil attach -shadow FILE`, discard FILE to reset.

## Sources

- fs_snapshot_create(2): https://keith.github.io/xcode-man-pages/fs_snapshot_create.2.html
- Entitlement discussion: https://developer.apple.com/forums/thread/89635 , https://developer.apple.com/forums/thread/768708
- Eclectic Light, snapshots: https://eclecticlight.co/2024/04/08/apfs-snapshots/ , https://eclecticlight.co/2026/05/10/last-week-on-my-mac-snapshots-the-elephant-in-apfs/
- Working with snapshots: https://eshop.macsales.com/blog/56681-working-with-macos-snapshots/
