# APFS transfer and deduplication tools

Use `apfs-transfer.py` to copy a tree to another volume and `apfs-dedupe.py` for existing duplicate trees. Both need `apfs_common.py` and `apfs_job.py` beside them (all in `~/.claude/skills/apfs-dev/scripts/`). Python 3.9+ and macOS/APFS are required. There are no third-party dependencies or network calls.

## Transfer a tree

Destination is the **exact target directory** and must already exist. Preview validates the manifest and reports entry/byte totals; it does not hash all content, predict exact savings, copy data, or write persistent checkpoints.

```sh
python3 ~/.claude/skills/apfs-dev/scripts/apfs-transfer.py /Volumes/Work /Volumes/SSD --exclude-system
python3 ~/.claude/skills/apfs-dev/scripts/apfs-transfer.py /Volumes/Work /Volumes/SSD --exclude-system --apply --json
```

`/Volumes/Work` and `/Volumes/SSD` are example paths. Existing files without valid checkpoints cause a conflict. `--replace` explicitly permits atomic replacement of those files after review. Destination-only files are never deleted; source files are never written or removed.

Regular files are SHA-256 checked, copied into a sibling staging file, checked again along with macOS metadata, and published atomically. Same-volume native copying can clone source data. Across volumes, the first unique content is copied; later identical files can clone a compatible destination keeper. Duplicate copies have independent inodes and remain independently writable. Source hard links retain hard-link topology. Symlinks are copied without following their targets. Directory metadata is restored after children, including creation times. `.git` is included in transfers. `--exclude-system` omits the macOS volume bookkeeping directories; `--exclude BASENAME` is repeatable.

Integrated duplicate sharing avoids a separate full-tree dedupe pass. It does **not** preserve arbitrary partial extent sharing between files with different contents, and does not eliminate all reads: each unique cross-volume file is hashed, copied, then independently read back. Keeper hashes are reused only while their complete file signatures remain unchanged. Compression and provenance may prevent some native metadata combinations; a failed check leaves the previous destination intact and reports the path.

## Deduplicate an existing tree

The existing interface and gzip recovery journal remain supported:

```sh
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py /path/to/tree
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py /path/to/tree --apply --json
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py /path/to/tree --top 10
```

`--top N` limits the plan, and therefore `--apply`, to the N largest duplicate units by reclaimable logical bytes. A unit is either a set of identical directories (such as two toolchain versions with the same contents) or a group of identical files. A dry run with the same `--top` shows exactly what `--apply` would replace; other changes are left unchanged and reported as deferred. Rerunning `--top N --apply` takes the next largest units, because completed files drop out of the ranking.

Directories are identical only when their complete listings match recursively: names, symlink targets, empty files, and every other file in the same content and provenance group. A file that was not hashed (unique size, below `--min-size`, hard-linked, foreign-owned or otherwise ineligible), an excluded name such as `.git`, or a mount point disqualifies the directory. Only the outermost identical directories are reported, and changes inside them belong to that directory unit; file units contain only the remaining changes. Replacement is still per file, so each file keeps its own metadata.

The output lists the units, then one directory tree of their paths: identical directories are tagged `#RANK keep`, `#RANK clone` or `#RANK unchanged` (already deduplicated), and file units show the kept file and up to three replaced files. Shared directory prefixes print once, and chains of single-subdirectory directories collapse into one line. With `--json`, each `top_groups` entry has `kind` (`directory` or `file`), `destinations`, `planned_files`, `logical_duplicate_bytes`, `estimated_physical_bytes` and `cloned_files`; directory units add `directories` (`path`, `role`), `files_per_directory` and `bytes_per_directory`, and file units add `sha256`, `size_bytes` and `source`. `deferred_files`, `deferred_groups` and `deferred_logical_bytes` count what was left out. These keys are absent without `--top`. Plan totals and `changes` cover the selected units only.

Compressed (`UF_COMPRESSED`) and sparse files are eligible. In each duplicate group, the copy with the smallest allocation (`st_blocks`) is kept and cloned, so its compressed or sparse layout is shared by the replaced copies. Files replaced by an earlier run are not chosen as the kept copy, which keeps reruns stable. Equal allocations prefer the largest existing APFS clone family in the group, then path order. APFS writes zeros for files extended by `truncate` below roughly 32 MiB, so those are not sparse.

`estimated_physical_bytes` (per change, per `--top` group and in total) estimates storage freed: each replaced file's private APFS allocation, 0 when it already shares all its blocks, and its full allocation when compressed (APFS reports no private size for compressed files). It does not account for snapshots that retain replaced blocks or for partial sharing of compressed files. `volume_free_change_bytes` remains the measured value.

Default is dry run. Apply adds persistent hash/provenance checkpoints and a compact SQLite journal index outside the tree. It retains per-file SHA-256 and metadata checks before replacing anything. Unchanged keeper hashes are reused; destination and staged-clone data are still hashed for each replacement. Journal writes remain durable per atomic stage; throughput is not increased by dropping fsync safeguards.

The first run against an old journal streams and indexes it, with recovery progress, instead of materializing all historical events in RAM. Subsequent unchanged journal reads use the index. Cache validity includes inode, device, size, nanosecond mtime and ctime, mode, owner, flags and link count. Volume UUID and root inode bind the operation to the correct tree. A remount can invalidate file signatures and require rereads. `--rehash` discards cached content/provenance results, but retains the journal of completed replacements.

### Dedupe journal and replacement details

- The journal is fsync'd gzip JSON Lines. Each append is one complete gzip member; an interrupted final member is discarded on the next `--apply`. Inspect it with `gzip -dc JOURNAL`; do not recompress an active journal.
- The journal lock serializes cooperating runs that use the same journal. Do not run different journals concurrently against overlapping trees.
- The tree may be in use; see "Deduplicating a tree in use".
- Legacy `.jsonl` journals are migrated to gzip on `--apply`, after verification. Dry runs read them without migrating.
- A dry run writes no journal and does not recover interrupted staging. Reading files may update their access times.
- Replacement keeps data, mode, ownership, mtime, birthtime, visible xattrs and ACLs, and verifies them before the rename. Inodes, ctime and parent directory timestamps change, and APFS's internal compression flag may change to match the cloned storage. There is no fallback to a full copy.
- Files with different `com.apple.provenance` are never merged; they are reported as `provenance_unmatched_files` and left intact.
- The tool never prompts and makes no network access. Uninstall by removing the scripts and, optionally, the state directories.

### Deduplicating a tree in use

Other processes may read and write the tree during a dry run or `--apply`. A file that is in use is skipped and left exactly as found; the run continues, exits 0 and lists each one in `skipped_files` (`path`, `reason`). Rerun to retry them.

- Scanning takes no locks. A file that changes, disappears or cannot be read while it is hashed is skipped. A directory that cannot be listed is skipped and reported the same way (`skipped_files`, reason `Permission denied`); it no longer fails the run. A directory that changes during `--top` comparison is not reported as identical.
- Before any work on a destination, and again before it is replaced, the tool takes a snapshot of every file that other processes hold open or mapped (libproc; descriptors and file-backed memory regions). A destination in the snapshot is skipped. So is one whose signature, metadata or APFS generation count changed since it was hashed, one with a writable shared mapping (generation count 0), and one on which another process holds a `flock`. The generation count also reveals writes made through a mapping, which do not update mtime.
- Replacement is an atomic exchange (`renamex_np` with `RENAME_SWAP`) of the verified clone and the destination, so the original still exists under the staging name. A snapshot taken after the exchange then decides: if a process opened the original just before the exchange, or its data or metadata changed, the original is moved back and that process keeps writing to the file at the destination path. Otherwise the original is removed. Replacements are exchanged in batches of up to 64 files or one second of work, so one full snapshot (about 0.1 s) and the descriptor-only snapshots next to the exchange (about 10 ms each) are shared.
- The kept copy is never modified. A kept copy written while it is cloned produces a clone that fails its SHA-256 check and is discarded.
- A process that keeps a file open only for reading causes the file to be skipped too: after a replacement it would no longer see later writes made through the path.

Limits:

- Processes of other users are invisible to the snapshot unless the tool runs as root. Their completed writes are still detected through signatures and the generation count; a descriptor they hold open and have not yet written to is not. Do not deduplicate files that another user's process keeps open for writing.
- If the original must be moved back but the clone at the destination path has itself been written or opened in the meantime, the two versions cannot be merged. Both are kept, the journal records a `conflict`, and the run and later runs fail with a message naming both files until the staging file is removed. This requires one process to open the file in the tens of milliseconds before the exchange and the file to be written through its path in the tens of milliseconds after.
- A process started by `fork` during a snapshot, holding only an inherited descriptor, can be missed. So can a read-only mapping, with its descriptor already closed, made in the 0.1 s before the exchange; such a process keeps reading the original's data.
- Replaced files get new inodes. Programs that track files by inode or file reference, or that watch an open descriptor for changes, see a rename followed by a new file.

## Observe and control either tool

Apply stores state under `~/Library/Application Support/apfs-tools/TOOL-HASH/`. Set `--state-dir /absolute/path` to choose a directory outside both transfer trees or outside the dedupe tree. Reuse that directory when restarting. A dedupe dry run writes persistent state only when explicitly given `--state-dir`; otherwise any journal index is disposable temporary state.

```sh
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py /path/to/tree --control status --json
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py /path/to/tree --control pause --json
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py /path/to/tree --control resume --json
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py /path/to/tree --control stop --json
```

For transfers, use `apfs-transfer.py SOURCE DESTINATION --control ACTION`; include the same `--state-dir` when customized. Controls do not scan the tree. They work while a volume is absent, provided the same root path is supplied.

Without any state for the given roots and `--state-dir`, a control command fails with `No job state in DIR` (exit 1). A second run against a state directory in use fails with `Another run is using the state directory`. A successful control command reports **requested**, not completed. Check `--control status` until `state` is `paused` or `stopped`. `resume` unpauses a live job; after a stop, failure, terminal loss or reboot, rerun the original apply command. Stale requests cannot affect a new run. Controls use a run identifier and a private request file, never a stored PID as a signal target. SIGINT/Ctrl-C and SIGTERM also request a safe stop, and so does SIGHUP (terminal closed) unless it is ignored, as under `nohup`. Stop checks occur while hashing/copying and between files; journal, fsync and rename sections finish before stopping. An OS I/O call that is blocked cannot acknowledge immediately.

Pausing keeps the process and open locks alive. **Stop and wait for acknowledgement before changing files or updating scripts**, then rerun. Changed files are revalidated; transfers refuse to replace changed destinations without `--replace`. `--verify-all` rehashes completed transfer files and rechecks their metadata rather than trusting unchanged signatures. Deduplication still revisits the directory listing on restart, but reuses valid hashes and provenance checks.

Each job has:

- `status.json`: atomic latest status, phase, current path/activity, file and byte counts, cache hits, rates and heuristic ETA.
- `events.jsonl`: timestamped append-only progress and terminal status, normally every two seconds. At 32 MiB it is renamed to `events.jsonl.1` (replacing an older one) and started again.
- `checkpoints.sqlite3`: WAL-backed cache, transfer checkpoints and compact dedupe recovery index. Keep its SQLite sidecars if moving live state; preferably stop first.
- `identity.json` and `lock`: bind state to the selected operation and serialize cooperating runs.

`files_done`/`bytes_done` describe the current phase. `null` totals mean the scan has not established a denominator. `current_file_bytes` reports hashing/copying within a large transfer file; `io_bytes_hashed` is cumulative hashing I/O for that run. `hash_reads_completed` counts full-file hash reads (a file can be read in more than one safety check); `cached_hashes_reused` counts reused digest lookups. `phases` retains earlier phase totals, so the completed checksum count stays visible after cloning begins. Byte progress can finish well before file progress because many small files remain. ETA uses up to 30 seconds of recent file and byte rates and takes the larger estimate; it is a heuristic, not a completion promise. A new invocation resets run counters; checkpoints carry completed work forward. `--control status` also checks the lock: an ungracefully terminated job is reported as `interrupted`, not left apparently running.

Human progress goes to stderr. `--json` writes exactly one final JSON object to stdout. `--quiet` suppresses human progress; state/history are still updated. Exit codes: 0 success (including dedupe runs that skipped files in use), 1 failed/partial, 2 invalid arguments, 130 safely stopped/interrupted. JSON schemas are version 1; consumers should ignore unknown fields. `active` and `requested` are inspection/control response fields, not persisted completion guarantees.

## Detached execution

Use `nohup` to survive terminal/SSH disconnection and `caffeinate` to prevent idle sleep. Shell redirection creates the result/log files before starting. Choose a new result filename for each restarted attempt so prior outcomes remain inspectable.

```sh
mkdir -p "$HOME/volume-transfers/my-job"
nohup /usr/bin/caffeinate -i python3 "$HOME/.claude/skills/apfs-dev/scripts/apfs-transfer.py" \
  /path/to/source /path/to/destination --apply --json \
  --state-dir "$HOME/volume-transfers/my-job/state" \
  > "$HOME/volume-transfers/my-job/result-1.json" \
  2> "$HOME/volume-transfers/my-job/progress-1.log" < /dev/null &
```

## Recovery and limits

Transfer only between idle trees: advisory locks and signature checks cannot prevent unrelated applications from writing. A source file on which another process holds an exclusive `flock` stops the transfer with `Locked by another process: PATH`; rerun when it is released. Deduplication tolerates a tree in use, within the limits listed under "Deduplicating a tree in use". Do not run jobs against overlapping trees using different state directories; exact-root locks do not lock every descendant against other applications. Source protected flags, foreign ownership, setuid/setgid files, special files and nested mounts cause transfer preflight failures. Metadata verification errors retain the old destination. Existing copies made by older scripts are not automatically trusted or adopted as verified checkpoints.

Keep the deduper's `ROOT/.apfs-dedupe.jsonl.gz`; inspect it with `gzip -dc`. Completed verified clone replacements remain valid after interruption; staging recovery handles the files in flight, and moves an original back if the interruption fell between an exchange and its confirmation. Deleting the hash cache costs rereads. Deleting the dedupe journal can cause recloning. Deleting transfer state loses proof of which existing files were verified. These are per-file transactions, not a whole-tree rollback or an APFS volume-image backup. Directory attributes and filesystem accounting can lag data completion. Logical shared bytes are an upper bound on physical savings.

## Tests

```sh
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe-tests.py
python3 ~/.claude/skills/apfs-dev/scripts/apfs-tools-tests.py
python3 ~/.claude/skills/apfs-dev/tests/test_common.py
```

Tests use disposable APFS fixtures. They cover the journal/metadata safety suite, files opened, mapped, locked or written by another process before and during replacement, recovery after an interrupted exchange, unreadable directories, structured progress, cooperative controls, real stop/restart, state-lock contention, missing state, signal handler restoration, event-log rotation, stale cache invalidation despite equal size/mtime, independent clone writes, hard links, symlinks, compression, locked source files, interrupted staging and publication recovery. A forced physical-copy fixture (`clone=False`) exercises copyfile's byte callback even when fixtures share a volume. `test_common.py` tests the shared bindings directly. This is not a benchmark of a full-size transfer.
