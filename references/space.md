# Disk-space accounting and housekeeping

## Which number to trust

| Source | Counts | Misleads when |
|---|---|---|
| `ls -l`, `st_size` | logical size | sparse and compressed files |
| `du`, Finder "on disk", `st_blocks` | allocated blocks per file | clones are counted once per file (test: 572 MiB reported, 381 MiB real) |
| `df`, `statvfs`, `diskutil info /` "Container Free Space", `NSURLVolumeAvailableCapacityKey` | free blocks of the container (41.8 GB in the test; all four agree) | fluctuates by GBs as macOS purges caches and thins snapshots; excludes purgeable space; shared by all volumes in the container |
| Finder "Available", `NSURLVolumeAvailableCapacityForImportantUsageKey` | free space plus purgeable space (54.2 GB in the same test) | whether a given write can use the purgeable part was not tested; plan with the smaller figure |
| `ATTR_CMNEXT_PRIVATESIZE` (`apfs-inspect.py`) | bytes deletion of this file would free | 0 for compressed files (unknown) |

```bash
python3 ~/.claude/skills/apfs-dev/scripts/apfs-inspect.py FILE...            # logical, allocated, private, refcnt, flags
python3 ~/.claude/skills/apfs-dev/scripts/apfs-inspect.py --tree DIR --json  # totals; private = lower bound of what rm -rf DIR frees
python3 ~/.claude/skills/apfs-dev/scripts/apfs-inspect.py --families DIR     # groups of pure clones
python3 ~/.claude/skills/apfs-dev/scripts/apfs-inspect.py --volume PATH      # fstype, capabilities, free_bytes, available_incl_purgeable_bytes
python3 ~/.claude/skills/apfs-dev/scripts/apfs-inspect.py --can-clone A B    # exit 0 only if A and B are on one clone-capable volume
```

`estimated_physical` adds each clone family's shared bytes once. It is exact for pure clones and over-counts clones that diverged (they get different clone ids). Shared bytes may also be held by files outside DIR or by snapshots.

## Why space does not come back after deleting

1. Time Machine local snapshots hold the deleted blocks: `tmutil listlocalsnapshots /`. They expire after ~24 h or are thinned under pressure; `tmutil thinlocalsnapshots / <bytes> 4` forces it (ask the user).
2. Clones: deleting one member of a clone family frees only its private bytes.
3. Clone checkpoints (`apfs-checkpoint.py list DIR`; each `restore` adds a `pre-restore-*` one; `drop DIR --pre-restore` or `--all`) and other worktrees.
4. Open files: a deleted file still open by a process stays allocated (`lsof +L1`).
5. Sparse disk images (`.sparseimage`, `.sparsebundle`, Docker.raw) grow but do not shrink: `hdiutil compact IMAGE` (reclaimed 276 MB in the test after deleting a file inside).

## Sparse files

- `truncate -s 100m f`, `mkfile -n`, and seek-then-write produce sparse files (observed 0 B allocated for 16 MiB and 100 MiB `truncate`; 4 KiB for a 200 MiB file with one byte at the end).
- `cp`, `cp -c`, `ditto` keep sparseness. `shutil.copy2` allocates every zero (100 MiB for a 0 B-allocated file). Copy VM and container images with `cp -c`.
- Writing data into a hole allocates blocks; decompressing a compressed sparse file expands it to full size.

## Keep build output out of backups and Spotlight

- Time Machine: `tmutil addexclusion /abs/path` (sticky, works without sudo; took 11 s per call in the test; sets xattr `com.apple.metadata:com_apple_backup_excludeItem`, so it moves with the directory and is copied by clones). Verify with `tmutil isexcluded PATH`. `-p` (fixed-path exclusion) needs sudo. Cargo does this for `target/` automatically and writes `CACHEDIR.TAG`.
- `CACHEDIR.TAG` (bford.info/cachedir) is honored by borg, restic, GNU tar, not by Time Machine.
- Spotlight: a directory name ending in `.noindex` is not indexed. `.metadata_never_index` no longer works on recent macOS. Renaming `node_modules` breaks tooling; prefer placing caches under a `*.noindex` parent (e.g. `~/Caches.noindex/`) and pointing tools there (`CARGO_TARGET_DIR`, `npm_config_cache`, `UV_CACHE_DIR` on the same volume).
- Exclusions do not apply to APFS snapshots.

## Case sensitivity and Unicode

- Default macOS volumes are case-insensitive, case-preserving, and normalization-insensitive. `touch Foo foo` creates one file.
- Git repos with paths differing only by case, or only by Unicode normalization (NFC vs NFD), check out incorrectly. Git sets `core.ignorecase=true` and `core.precomposeunicode=true` on such volumes.
- Use a case-sensitive APFS volume for Linux-origin repos, kernel trees, and cross-platform CI reproduction (volumes-images.md). Some macOS apps misbehave on case-sensitive volumes; keep it a separate volume, not the boot volume.

## Sources

- Exclusions: https://eclecticlight.co/2026/01/06/exclude-or-include-items-in-backup-search-icloud-drive-and-quicklook-preview/ , https://eclecticlight.co/2024/07/09/excluding-folders-and-files-from-time-machine-spotlight-and-icloud-drive/
- Cargo backup exclusion: https://doc.rust-lang.org/nightly/nightly-rustc/cargo_util/paths/fn.exclude_from_backups.html
- Finder size on disk: https://eclecticlight.co/2026/09/05/when-the-finders-size-on-disk-is-misleading/
- Clone and sparse robustness: https://eclecticlight.co/2025/04/07/how-robust-are-apfs-clone-and-sparse-files/
