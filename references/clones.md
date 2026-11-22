# APFS clones

A clone is a new inode whose extents point at the source's blocks. Creation costs metadata only; a later write to either file allocates new blocks for the written extents (copy-on-write). Clones are independent files: different inode, permissions, xattrs, timestamps. They are not hard links.

## Creating clones

| Method | Clones? | Notes |
|---|---|---|
| `cp -c SRC DST` | yes | `-c` = `clonefile(2)`; falls back to a copy across volumes |
| `cp -ac SRC DST`, `cp -c -R` | yes | per-file clones of a tree, preserves metadata with `-a` |
| `ditto SRC DST` | yes | clones by default on the same volume; `--noclone` forces a copy |
| `clonefile(2)` on a directory | yes | whole hierarchy in one call, ~13x faster than `cp -c -R` (below) |
| Rust `std::fs::copy` | yes | |
| Bun `Bun.write(dst, Bun.file(src))` | yes | `bun install` clones by default |
| `copyfile(3)` with `COPYFILE_CLONE` | yes | falls back to a copy; Apple's recommended tree API |
| `cp` (no `-c`), `cp -R` | **no** | |
| `rsync -a` (openrsync in macOS) | **no** | |
| `tar -cf - . \| tar -xf -` | **no** | |
| Python `shutil.copy2`, `copytree` | **no** | uses `fcopyfile(COPYFILE_DATA)`; also expands sparse files and drops compression |
| Node 26 `fs.copyFileSync(..., COPYFILE_FICLONE)`, `fs.cpSync` | **no** | `COPYFILE_FICLONE_FORCE` throws ENOSYS on macOS |

Measured 2026-11-22 (macOS 27.2.1, Node 26.13.1, Python 3.15.2, Rust 1.99, Bun 1.3.11) by checking `EF_SHARES_ALL_BLOCKS` on the result. Re-check with `apfs-inspect.py DST` after upgrading a runtime.

From Python, call `clonefile` directly:

```python
import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
CLONE_NOFOLLOW, CLONE_NOOWNERCOPY = 1, 2
if libc.clonefile(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW) != 0:
    err = ctypes.get_errno()   # EXDEV / ENOTSUP: other volume or filesystem -> copy instead
```

From Node, shell out: `execFileSync('cp', ['-c', src, dst])`.

`clonefile` fails with `EEXIST` if DST exists; there is no clone-over-existing call. Clone to a temporary name and `rename(2)` (or `renamex_np(..., RENAME_SWAP)` to swap atomically, which also works on directories).

## Timing (6,000 files, 117 MiB, same volume)

| Command | Time |
|---|---|
| `clonefile(dir)` (one call) | 0.07 s |
| per-file `clonefile` loop in Python | 0.75 s |
| `cp -c -R` | 0.94 s |
| `ditto` | 1.1 s |
| `cp -R` (full copy) | 1.8 s |

Directory `clonefile` is documented as "strongly discouraged" for hierarchies: it is atomic and uninterruptible, fails entirely if one item cannot be cloned, and copies ownership unless `CLONE_NOOWNERCOPY` is passed. It is appropriate for agent-owned trees (checkpoints, caches) and for trees under one user.

Observed behavior of a directory `clonefile` (2026-11-22):

| Tree contains | Result |
|---|---|
| unreadable subdirectory (mode 000) | whole call fails with `EACCES`, nothing created |
| read-only directories, `uchg` files | cloned with mode and flags; removing the clone needs `chmod`/`chflags nouchg` first |
| hard links (two names, one inode) | two independent files in the clone (`nlink` 1 each) |
| Unix socket, FIFO | recreated as socket/FIFO nodes (per-file `clonefile` on them fails with `EINVAL`) |
| destination inside the source | `EINVAL`, nothing created |

## Divergence

- A 1-byte write in the middle of a 190 MiB clone allocated 16 KiB of private data; the remaining blocks stay shared. The written file gets a new clone id and loses `shares_all_blocks`; untouched clones keep the shared id, and their `refcnt` drops by one (a last remaining member shows `refcnt` 1 without `shares_all_blocks`). The same holds for writes through `mmap`, appends, and rewriting identical bytes in place (which unshares the whole file).
- Equal clone ids therefore prove equal content without reading the files; `apfs-checkpoint.py diff` relies on this.
- Tools that write a new file and rename it over the old one (most editors, `sed -i`, formatters, `git checkout`, package managers) unshare the whole file.
- Appending allocates only the new extents.

## Detecting clones

`stat` and `du` cannot tell. `getattrlist(2)` with `FSOPT_ATTR_CMN_EXTENDED` exposes:

- `ATTR_CMNEXT_PRIVATESIZE`: bytes freed if this file alone were deleted (0 for a pure clone; 0 also reported for compressed files, so treat as unknown there).
- `ATTR_CMNEXT_CLONEID`: equal ids mean full clones of one data stream.
- `ATTR_CMNEXT_CLONE_REFCNT`: number of full clones.
- `ATTR_CMNEXT_EXT_FLAGS`: `EF_MAY_SHARE_BLOCKS` (ever cloned), `EF_SHARES_ALL_BLOCKS` (pure clone), `EF_IS_SPARSE`, `EF_IS_PURGEABLE`.

`apfs-inspect.py` reads all of these. Partial sharing between diverged clones has no per-file API; only `PRIVATESIZE` bounds it.

## Hazards when cloning working trees

- Absolute paths survive cloning. Python venvs (`bin/*` shebangs, `pyvenv.cfg`), CMake build dirs (`CMakeCache.txt`), and some Xcode/IDE state point back at the source. A cloned `.venv` runs the source's interpreter and site-packages. Recreate them (`uv sync`, `cmake -B build`) instead; `apfs-worktree.py --warm` excludes `.venv`, `venv`, `.tox`, `.nox`, `CMakeFiles`, `CMakeCache.txt`, `xcuserdata` by default.
- A cloned `.git` directory is a second independent repository, not a worktree. Use `git worktree add` (or `apfs-worktree.py`) so refs and objects are shared.
- A linked worktree's `.git` file points at the original worktree's admin dir; cloning it creates two checkouts fighting over one index. `apfs-worktree.py --warm` skips registered worktrees and any ignored directory holding a `.git` file.
- Unix sockets and FIFOs inside a tree (dev-server sockets, `.git/fsmonitor--daemon.ipc`) are recreated by a directory clone with no listener behind them. Restart the owning process in the clone.
- Folders synced by iCloud Drive or another File Provider can hold dataless files (`dataless` flag in `apfs-inspect.py`); cloning them was not tested. Keep worktrees and checkpoints out of synced folders.
- Cloning does not copy file locks or open-file state; cloning a live SQLite database without its `-wal`/`-shm` gives an inconsistent copy. Checkpoint the database first or stop the writer.

## Sources

- clonefile(2): https://keith.github.io/xcode-man-pages/clonefile.2.html
- getattrlist(2) `ATTR_CMNEXT_*` (man page and `<sys/attr.h>`, `<sys/stat.h>` in the SDK)
- Eclectic Light, clone detection and accounting: https://eclecticlight.co/2025/05/13/how-to-tell-the-difference-between-copies-clone-files-and-hard-links/ , https://eclecticlight.co/2026/09/05/when-the-finders-size-on-disk-is-misleading/
- Apple forum on cloning directories: https://developer.apple.com/forums/thread/818372
- Existing CoW-clone tools for repos: https://github.com/chrisbanes/grove , https://docs.rs/cow-cli , https://spin.atomicobject.com/git-worktrees-untracked-files/
