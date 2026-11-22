# Deduplication and transparent compression

## Deduplication with clones

APFS has no online block-level dedup. Identical files can be replaced by clones of one keeper, after which they share blocks but remain independent, writable files.

```bash
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py ROOT                 # dry run: duplicate sets, reclaimable bytes
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py ROOT --top 10        # 10 largest sets (files or whole identical dirs)
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py ROOT --top 10 --apply --json
python3 ~/.claude/skills/apfs-dev/scripts/apfs-dedupe.py ROOT --control status --json
```

Safety properties (full detail in apfs-tools.md): SHA-256 verified, per-file atomic replacement with `renamex_np(RENAME_SWAP)`, metadata (mode, owner, times, xattrs, ACLs) preserved and verified, files open/mapped/locked/changing are skipped, `com.apple.provenance` mismatches never merged, resumable journal at `ROOT/.apfs-dedupe.jsonl.gz`. Skips `.git` by default. Inodes and ctime change, so tools tracking inodes see a new file.

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

  Mutating commands need `afsctool` (installed at /opt/homebrew/bin) unless `--backend applesauce|ditto`. `system doctor` checks tools, state and target volumes.
- `afsctool` (`brew install afsctool`): `-c` compress, `-d` decompress, `-v` show method and ratio, `-T LZFSE|LZVN|ZLIB`, `-j N` parallel.
- `applesauce` (Rust): compress/decompress/info, refuses to grow files.

Interaction with clones: cloning a compressed file shares the compressed blocks (refcnt increases, `compressed` flag kept). Compressing a file that has clones gives it new private blocks; the clones keep the old uncompressed ones, so compressing one member of a clone family can increase usage. Dedupe first, then compress the keeper, or compress before cloning.

## Sources

- Eclectic Light, deduplication: https://eclecticlight.co/2021/05/29/explainer-deduplication/ , https://eclecticlight.co/2023/04/22/should-you-remove-duplicate-files/
- dedup (ttkb-oss): https://www.gigazine.net/gsc_news/en/20250302-dedup-macos-duplicate-file
- afsctool: https://github.com/RJVB/afsctool ; applesauce: https://docs.rs/applesauce
- decmpfs format: https://keramics.github.io/decmpfs.html
- Compression on a MacBook: https://xfbs.github.io/posts/enabling-apfs-compression
