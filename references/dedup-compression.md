# Deduplication and transparent compression

## Run both on one directory

```bash
python3 "$APFS_DEV"/scripts/apfs-reclaim.py "$PWD"          # preview
python3 "$APFS_DEV"/scripts/apfs-reclaim.py "$PWD" --apply  # run both
```

The preview assesses the files currently on disk and writes temporary reports outside the target. Its two savings figures can overlap. Apply compresses eligible cold files before cloning duplicates, so compatible copies can share the compressed blocks. The built-in `ditto` backend requires macOS and Python 3.12 or later for compression. The launcher searches PATH and standard Homebrew locations when started with Python 3.9–3.11.

Apply prints a state directory outside the target. Keep the target's `.apfs-dedupe.jsonl.gz` recovery journal. After an interruption, rerun with the same root and `--state-dir DIR` to reuse the compression records and hash cache. The state directory contains a subdirectory for each run's before/after inspection and full per-file reports. To monitor dedupe directly, use `apfs-dedupe.py ROOT --state-dir DIR/dedupe --control status --json`. Allow large trees time for hashing, metadata verification and journaled replacement. Run one mutating job at a time on overlapping trees.

`--json` emits one result with `before`, `compression`, `dedupe`, and (on apply) `after` summaries. Progress goes to stderr. Exit 0 means both phases completed, 1 means a failure or partial execution, 2 means invalid arguments, and 130 means interrupted. Dedupe can complete with skipped files and reports their counts. The underlying commands support per-phase tuning and detailed previews.

Use the before/after APFS allocation estimate for the overall reduction. Compression reports the reduction in verified per-file allocation. Dedupe separately estimates the private allocation released by completed clone replacements. A local snapshot may hold old blocks, and volume free space can change during a read-only scan. Files with a decmpfs header and a missing compression flag are reported as a pre-existing condition. For a separate repair assessment, use `apfs_compress_plus.py file repair ROOT --dry-run`.

macOS can generate a different `com.apple.provenance` value in a staged clone even when the two originals have identical values. The tool preserves the original in that case. Dedupe remembers stable refusals in its journal, so retries can focus on candidates that were open or changed during the run.

Darwin can hide an independent resource fork on a file whose compressed payload is stored inline. The verification code uses `XATTR_SHOWCOMPRESSION` to detect hidden attributes. It permits a hidden resource fork only for known fork-backed decmpfs types and declines replacements when it cannot verify the hidden metadata.

Staged compression uses a durable per-file journal and `RENAME_SWAP`. The tool rechecks the displaced original's APFS generation and metadata, including xattrs and ACLs. If the original changed or was opened during the exchange, the tool restores it when the installed copy is still unchanged. It keeps both versions when either could be lost during rollback.

A killed run can leave a private stage beside a file. `file compress` settles such a stage before it starts when no process holds it: the original returns to its path if the exchange was not confirmed, and the staged copy is removed. `SIGTERM` and `SIGHUP` stop a run like an interrupt, after the files in progress clear their stages. If compression still reports a pending publication, the stage needs review: both versions may have changed. Compare both paths the error names and retain the desired versions before clearing the stage.

```bash
python3 "$APFS_DEV"/scripts/apfs_compress_plus.py file repair ROOT --only interrupted_swap --dry-run --json
python3 "$APFS_DEV"/scripts/apfs_compress_plus.py file repair ROOT --only interrupted_swap
```

## Deduplication with clones

APFS has no online block-level dedup. Identical files can be replaced by clones of one keeper, after which they share blocks but remain independent, writable files.

```bash
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py ROOT                 # dry run: duplicate sets, reclaimable bytes
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py ROOT --top 10        # 10 largest sets (files or whole identical dirs)
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py ROOT --top 10 --apply --json
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py ROOT --control status --json
```

Safety properties (full detail in apfs-tools.md): SHA-256 verified, per-file atomic replacement with `renamex_np(RENAME_SWAP)`, metadata (mode, owner, times, xattrs, ACLs) preserved and verified, files open/mapped/locked/changing are skipped, `com.apple.provenance` mismatches never merged, resumable journal at `ROOT/.apfs-dedupe.jsonl.gz`. Files already in the keeper's clone family and `.git` are skipped. Replaced files get new inodes and ctime.

Good targets in a dev environment: multiple checkouts and their `node_modules`, toolchain/SDK versions with shared files, `~/.cache` and package caches that copied instead of cloning, Docker build contexts, datasets duplicated per experiment. Bad targets: trees whose files are rewritten constantly (savings vanish), `.git/objects` (already content-addressed and packed).

Reported savings: "logical duplicate bytes" is an upper bound; `estimated_physical_bytes` accounts for already-shared data; snapshots retain replaced blocks until they expire. Many third-party dedup apps overstate savings because they cannot see existing clones.

For copying a tree to another volume while sharing duplicates at the destination, use `apfs-transfer.py SRC DEST` (dry run by default, checkpointed, verified).

Other tools: `dedup` (ttkb-oss, clone/hardlink modes), `apfs-dedupe` (curlewlabs, uses fclones), Hyperspace (GUI).

## Transparent compression (decmpfs)

A compressed file stores its data in the resource fork / `com.apple.decmpfs` xattr and carries `UF_COMPRESSED` (`ls -lO` shows `compressed`). Reads decompress transparently. macOS itself does not compress user files; tools must do it explicitly.

Measured on a 7.1 MiB text file:

| Command | Allocated |
|---|---|
| uncompressed | 7.1 MiB |
| `afsctool -c` (zlib) | 2.2 MiB |
| `afsctool -c -T LZFSE` | 2.3 MiB |
| `afsctool -c -T LZVN` | 2.9 MiB (fastest decode) |
| `ditto --hfsCompression --noclone SRC DST` | 2.9 MiB (always LZVN) |
| `ditto --hfsCompression SRC DST` (same volume) | 7.1 MiB: **cloned, not compressed** |

What keeps or drops compression (verified):

- Keeps: `cp -c`, `ditto`, `ditto --noclone`, `touch`, `chmod`, `xattr -w`.
- Drops (file becomes uncompressed): plain `cp`, `cat a > b`, Python `shutil.copy2`, appending, in-place writes, `sed -i`, any editor save.

Use for: cold archives, toolchains and SDKs, old build caches, logs, datasets read but never written, `~/Library` caches of apps that do not rewrite them. Avoid for: working trees, files under active edit, databases, files that will be mmapped for writing. Compressed files cannot be partially written without full decompression; reads cost CPU (LZVN is cheapest).

Tools:

- `apfs_compress_plus.py` (0.5.0; **Python 3.12+**, so not `/usr/bin/python3`): samples files, predicts physical savings, chooses LZVN/LZFSE/zlib by age and content, verifies allocated blocks, keeps state for incremental runs, JSON output. Default `balanced` profile: files >= 64 KiB, >= 180 days old, predicted ratio <= 85%.
  - A file that another process has open for writing, or mapped, is left untouched and reported (`file_in_use`, with pid, command and access). Detection is by device and inode through libproc, so the spelling of the path (letter case, Unicode form) does not matter; `lsof` is not used. Without root only this user's processes are visible.
  - The codec of a compressed file is read from its decmpfs header (`file inspect` works without `afsctool`). `apfs-inspect.py FILE` shows the same as `compressed=lzvn`.
  - A file whose extended attributes cannot be listed is not handed to the compressor (`afsctool` empties a file that carries stale compression metadata).
  - Volumes are identified with `statfs(2)`: reports show the real mount point (`/System/Volumes/Data`, not `/`).

  ```bash
  python3 ~/.claude/skills/apfs-dev/scripts/apfs_compress_plus.py file scan ~/Archive
  python3 ~/.claude/skills/apfs-dev/scripts/apfs_compress_plus.py file scan ~/Archive --plan plan.json --json
  python3 ~/.claude/skills/apfs-dev/scripts/apfs_compress_plus.py file compress --from-plan plan.json --json
  python3 ~/.claude/skills/apfs-dev/scripts/apfs_compress_plus.py file inspect PATH
  python3 ~/.claude/skills/apfs-dev/scripts/apfs_compress_plus.py system doctor
  ```

  Mutating commands use `afsctool` by default. Pass `--backend ditto` for built-in LZVN compression without installing anything, or `--backend applesauce` if it is already installed. With `ditto`, the tool verifies staged copies for reduced allocation, byte-for-byte contents, and preserved metadata. On macOS builds that omit `UF_COMPRESSED` when staging, it restores the flag on the stage and verifies that the data reads back before publishing. A protected provenance attribute can prevent safe replacement. `system doctor` checks tools, state and target volumes.
- `afsctool` (`brew install afsctool`): `-c` compress, `-d` decompress, `-v` show method and ratio, `-T LZFSE|LZVN|ZLIB`, `-j N` parallel.
- `applesauce` (Rust): compress/decompress/info, refuses to grow files.

Cloning a compressed file shares the compressed blocks and keeps the compression flag. Compression scans and apply runs skip files with shared APFS blocks, because compressing one member of a clone family allocates a new compressed stream while the other members keep the old blocks. A saved plan rechecks sharing when applied. Structured output counts these files under `skipped_shared_blocks`.

To combine both techniques for exact duplicates, compress before deduplication, as `apfs-reclaim.py` does, or compress one independent copy and let dedupe choose it as the keeper if it uses less space. Clones of a compressed keeper share the compressed blocks. Old extents are freed when their last reference is gone, including references outside the scanned tree. The staged `ditto` backend only publishes a compressed copy if it uses less space than the source.

## Sources

- Eclectic Light, deduplication: https://eclecticlight.co/2021/05/29/explainer-deduplication/ , https://eclecticlight.co/2023/04/22/should-you-remove-duplicate-files/
- dedup (ttkb-oss): https://www.gigazine.net/gsc_news/en/20250302-dedup-macos-duplicate-file
- afsctool: https://github.com/RJVB/afsctool ; applesauce: https://docs.rs/applesauce
- decmpfs format: https://keramics.github.io/decmpfs.html
- Compression on a MacBook: https://xfbs.github.io/posts/enabling-apfs-compression
