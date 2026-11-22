#!/usr/bin/env python3
"""Create a git worktree whose files are APFS clones of an existing checkout.

  apfs-worktree.py SOURCE DEST [--ref REF | -b BRANCH [--ref REF] | --checkout BRANCH]
                   [--warm] [--wip] [--fast-index] [--include NAME]... [--exclude NAME]...
                   [--require-clone] [--dry-run] [--json]

Default (clean): DEST is a registered `git worktree` at REF (default HEAD), detached
unless -b (new branch at REF) or --checkout (existing branch) is given. Tracked files
whose index entry in SOURCE equals REF are cloned from SOURCE's working tree with
clonefile(2); git then verifies them against REF and writes the files that differ or
are missing, so DEST is exactly REF.

--warm   also clone ignored paths (node_modules, target, dist, .next, .env, ...), so
         the new worktree starts with warm caches. Names that embed absolute paths
         (.venv, CMake build trees, Xcode user data) are excluded by default; recreate
         them (uv sync, cmake -B) instead. Other worktrees of the repository, DEST
         itself and nested linked checkouts (directories holding a `.git` file) are
         never cloned.
--wip    reproduce SOURCE's uncommitted state: modified tracked files, staged changes
         and untracked non-ignored files. SOURCE's index is copied, so `git status` in
         DEST equals `git status` in SOURCE. Requires REF to be SOURCE's HEAD.
--fast-index
         reuse SOURCE's index instead of rehashing every cloned file (requires REF to
         be SOURCE's HEAD). Sets extensions.worktreeConfig=true in the shared repo
         config and core.checkStat=minimal + core.trustctime=false for DEST only,
         because a clone has a new inode and ctime but the original mtime and size.
         With minimal stat checking git detects a change only through mtime (whole
         seconds), size or mode.
--exclude NAME
         with --warm/--wip: never clone ignored or untracked entries with this
         basename. Tracked files are not affected.
--require-clone
         fail before creating anything if DEST is on another volume than SOURCE
         (there every file would be copied in full; reported as `copied_files`).

Clones share blocks with SOURCE until either side writes; a write allocates new
blocks only for the touched extents. On failure the worktree, its directory and a
branch created with -b are removed again.
Remove a worktree with:  git -C SOURCE worktree remove --force DEST

Exit status: 0 success, 1 error. Errors and warnings go to stderr.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apfs_common import (CLONE_NOFOLLOW, CLONE_NOOWNERCOPY, COPYFILE_ALL, COPYFILE_NOFOLLOW,
                         Native, existing_ancestor, force_rmtree, unlock)

DEFAULT_EXCLUDE = {'.venv', 'venv', '.tox', '.nox', 'CMakeFiles', 'CMakeCache.txt',
                   'xcuserdata', '.DS_Store', '__pycache__'}
CLONE_FLAGS = CLONE_NOFOLLOW | CLONE_NOOWNERCOPY
native = Native()


class Fail(Exception):
    pass


def git(repo, *args, check=True):
    r = subprocess.run(['git', '-C', str(repo), *args], check=False,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and r.returncode != 0:
        raise Fail(f'git {" ".join(args)}: {r.stderr.decode(errors="replace").strip()}')
    return r


def gitout(repo, *args):
    return os.fsdecode(git(repo, *args).stdout).strip()


def zlist(repo, *args):
    return [os.fsdecode(p) for p in git(repo, *args).stdout.split(b'\0') if p]


def is_linked_checkout(d):
    """True for a linked worktree or submodule checkout: its `.git` is a file."""
    try:
        return stat.S_ISREG(os.lstat(os.path.join(d, '.git')).st_mode)
    except OSError:
        return False


class Cloner:
    def __init__(self, exclude, avoid):
        self.exclude, self.avoid = exclude, avoid
        self.cloned = self.copied = self.bytes = 0
        self.skipped = []
        self.made = set()

    def file(self, src, dst):
        parent = os.path.dirname(dst)
        if parent not in self.made:
            os.makedirs(parent, exist_ok=True)
            self.made.add(parent)
        for attempt in (0, 1):
            try:
                native.clone(src, dst, CLONE_FLAGS)
                self.cloned += 1
                return
            except OSError as e:
                error = e
                if e.errno != errno.EEXIST or attempt:
                    break
                os.unlink(dst)  # duplicate path (unmerged entry, or case-only collision)
        if error.errno not in (errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise error
        # copyfile(3) keeps sparse regions and decmpfs compression where a clone is
        # impossible (other volume); shutil.copy2 would not.
        native.copyfile(src, dst, COPYFILE_ALL | COPYFILE_NOFOLLOW)
        self.bytes += os.lstat(src).st_size
        self.copied += 1

    def tree(self, src, dst):
        """Clone an ignored or untracked path of any type."""
        src, dst = str(src), str(dst)
        try:
            mode = os.lstat(src).st_mode
        except OSError:
            return
        if not stat.S_ISDIR(mode):
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                self.file(src, dst)
            else:
                self.skipped.append(dict(path=src, reason='special file'))
            return
        if src in self.avoid:
            return  # another worktree of this repository, or DEST itself
        if is_linked_checkout(src):
            self.skipped.append(dict(path=src, reason='linked checkout'))
            return
        holds_worktree = any(p.startswith(src + os.sep) for p in self.avoid)
        if not holds_worktree and not os.path.lexists(dst):
            # One clonefile(2) call clones a whole directory hierarchy, ~10x faster
            # than per-file cloning. Excluded names are then removed from the clone.
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                native.clone(src, dst, CLONE_FLAGS)
            except OSError:
                pass  # unreadable item or other volume: clone entry by entry below
            else:
                self.prune(src, dst)
                return
        try:
            names = sorted(os.listdir(src))
        except OSError as e:
            self.skipped.append(dict(path=src, reason=e.strerror))
            return
        os.makedirs(dst, exist_ok=True)
        for name in names:
            if name not in self.exclude:
                self.tree(os.path.join(src, name), os.path.join(dst, name))
        try:
            shutil.copystat(src, dst, follow_symlinks=False)
        except OSError:
            pass

    def prune(self, src, dst):
        for root, dirs, files in os.walk(dst):
            if root != dst and '.git' in files:
                self.skipped.append(dict(path=src + root[len(dst):], reason='linked checkout'))
                force_rmtree(root)
                dirs[:] = []
                continue
            for name in [d for d in dirs if d in self.exclude]:
                force_rmtree(os.path.join(root, name))
                dirs.remove(name)
            for name in files:
                if name in self.exclude:
                    unlock(root)
                    os.unlink(os.path.join(root, name))
                else:
                    self.cloned += 1


def index_entries(src):
    """[(mode, oid, stage, path)] of SOURCE's index."""
    out = []
    for rec in git(src, 'ls-files', '-s', '-z').stdout.split(b'\0'):
        if rec:
            meta, path = rec.split(b'\t', 1)
            mode, oid, stage = meta.split()
            out.append((mode, oid, stage, os.fsdecode(path)))
    return out


def tree_entries(src, commit):
    """{path: (mode, oid)} of the commit's tree."""
    out = {}
    for rec in git(src, 'ls-tree', '-r', '-z', commit).stdout.split(b'\0'):
        if rec:
            meta, path = rec.split(b'\t', 1)
            mode, _, oid = meta.split()
            out[os.fsdecode(path)] = (mode, oid)
    return out


def other_worktrees(src):
    out = set()
    for line in git(src, 'worktree', 'list', '--porcelain').stdout.split(b'\n'):
        if line.startswith(b'worktree '):
            out.add(os.path.realpath(os.fsdecode(line[9:])))
    out.discard(os.path.realpath(src))
    return out


def copy_index(src, dest):
    """Copy SOURCE's index (and split-index base) to DEST, keeping its mtime.

    Git treats an entry as possibly modified ("racy") when its mtime is not older
    than the index file's; a fresh index mtime would hide same-second edits.
    """
    def git_path(repo, name):
        return gitout(repo, 'rev-parse', '--path-format=absolute', '--git-path', name)
    src_index, dst_index = git_path(src, 'index'), git_path(dest, 'index')
    if not os.path.exists(src_index):
        raise Fail(f'{src} has no index file')
    shared = gitout(src, 'rev-parse', '--path-format=absolute', '--shared-index-path')
    if shared:
        shutil.copyfile(shared, os.path.join(os.path.dirname(dst_index), os.path.basename(shared)))
    st = os.stat(src_index)
    shutil.copyfile(src_index, dst_index)
    os.utime(dst_index, ns=(st.st_atime_ns, st.st_mtime_ns))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('source')
    ap.add_argument('dest')
    ap.add_argument('--ref', help='commit to check out (default: HEAD of SOURCE)')
    ap.add_argument('-b', '--branch', help='create this branch at REF (default: detached)')
    ap.add_argument('--checkout', metavar='BRANCH', help='check out this existing branch')
    ap.add_argument('--warm', action='store_true', help='also clone ignored build/dependency paths')
    ap.add_argument('--wip', action='store_true', help="reproduce SOURCE's uncommitted state")
    ap.add_argument('--include', action='append', default=[], metavar='NAME',
                    help='with --warm: clone only ignored paths with this basename or top-level name')
    ap.add_argument('--exclude', action='append', default=[], metavar='NAME',
                    help='never clone ignored/untracked entries with this basename (repeatable)')
    ap.add_argument('--fast-index', action='store_true', help="reuse SOURCE's index (REF must be HEAD)")
    ap.add_argument('--require-clone', action='store_true', help='fail if DEST is on another volume')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    if a.checkout and (a.branch or a.ref):
        ap.error('--checkout cannot be combined with -b or --ref')

    src = gitout(a.source, 'rev-parse', '--show-toplevel')
    if not src:
        raise Fail(f'{a.source} has no working tree (bare repository)')
    src = os.path.realpath(src)
    dest_arg = os.path.abspath(os.path.expanduser(a.dest))
    dest = os.path.join(os.path.realpath(os.path.dirname(dest_arg)), os.path.basename(dest_arg))
    if os.path.lexists(dest):
        raise Fail(f'{dest} exists')
    if a.checkout and git(src, 'show-ref', '--verify', '--quiet', 'refs/heads/' + a.checkout,
                          check=False).returncode != 0:
        raise Fail(f'no local branch {a.checkout!r}')
    ref = 'refs/heads/' + a.checkout if a.checkout else a.ref or 'HEAD'
    commit = gitout(src, 'rev-parse', '--verify', ref + '^{commit}')
    head = git(src, 'rev-parse', '--verify', '-q', 'HEAD^{commit}', check=False).stdout.decode().strip()
    for flag, on in (('--fast-index', a.fast_index), ('--wip', a.wip)):
        if on and commit != head:
            raise Fail(f'{flag} requires REF to be the commit checked out in SOURCE')
    if a.fast_index and git(src, 'config', '--local', '--get', 'core.worktree', check=False).returncode == 0:
        raise Fail('--fast-index: core.worktree is set in the repository config; '
                   'extensions.worktreeConfig would require moving it (see git-worktree(1))')
    same_volume = native.mount(existing_ancestor(dest)) == native.mount(src)
    if not same_volume:
        if a.require_clone:
            raise Fail(f'{dest} is on another volume than {src}; clones are impossible there')
        print(f'warning: {dest} is on another volume than {src}; every file will be copied in full',
              file=sys.stderr)

    exclude = DEFAULT_EXCLUDE | set(a.exclude)
    avoid = other_worktrees(src) | {dest}

    def wanted(rel):
        """Filter for ignored/untracked paths."""
        full = os.path.join(src, rel)
        return (not set(Path(rel).parts) & exclude
                and not any(full == p or full.startswith(p + os.sep) for p in avoid))

    index = index_entries(src)
    if a.wip:
        tracked = list(dict.fromkeys(path for mode, _, _, path in index if mode != b'160000'))
    else:
        at_ref = tree_entries(src, commit)
        tracked = [path for mode, oid, stage, path in index
                   if stage == b'0' and mode != b'160000' and at_ref.get(path) == (mode, oid)]
    untracked, ignored = [], []
    if a.wip:
        # A trailing slash marks a nested repository; it is never cloned.
        untracked = [p for p in zlist(src, 'ls-files', '-z', '--others', '--exclude-standard')
                     if not p.endswith('/') and wanted(p)]
    if a.warm:
        ignored = [p.rstrip('/') for p in zlist(src, 'ls-files', '-z', '--others', '--ignored',
                                                '--exclude-standard', '--directory')]
        if a.include:
            ignored = [p for p in ignored if Path(p).name in a.include or Path(p).parts[0] in a.include]
        # git can list a directory and one of its subdirectories; keep the outermost.
        listed = set(ignored)
        ignored = [p for p in ignored
                   if wanted(p) and not any(str(q) in listed for q in Path(p).parents)]
    plan = dict(source=src, dest=dest, commit=commit, branch=a.branch or a.checkout,
                same_volume=same_volume, tracked_files=len(tracked),
                untracked_files=len(untracked), ignored_paths=ignored)
    if a.dry_run:
        print(json.dumps(plan, indent=2) if a.json else '\n'.join(f'{k}: {v}' for k, v in plan.items()))
        return

    t0 = time.monotonic()
    add = ['worktree', 'add', '-q', '--no-checkout']
    if a.checkout:
        add += [dest, a.checkout]
    else:
        add += (['-b', a.branch] if a.branch else ['--detach']) + [dest, commit]
    git(src, *add)
    try:
        c = Cloner(exclude, avoid)
        for p in tracked:
            full = os.path.join(src, p)
            try:
                mode = os.lstat(full).st_mode
            except OSError:
                continue  # deleted in SOURCE's working tree
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                c.file(full, os.path.join(dest, p))
        for p in untracked + ignored:
            c.tree(os.path.join(src, p), os.path.join(dest, p))
        if a.fast_index or a.wip:
            # Record current stat data in SOURCE's index so DEST inherits it.
            git(src, 'update-index', '-q', '--refresh', check=False)
            copy_index(src, dest)
        if a.fast_index:
            git(src, 'config', 'extensions.worktreeConfig', 'true')
            git(dest, 'config', '--worktree', 'core.checkStat', 'minimal')
            git(dest, 'config', '--worktree', 'core.trustctime', 'false')
        elif a.wip:
            git(dest, 'update-index', '-q', '--refresh', check=False)
        else:
            git(dest, 'read-tree', commit)
            # Refresh hashes each clone once and records its stat data; status 1 only
            # means some files differ from REF, which the reset below repairs.
            git(dest, 'update-index', '-q', '--refresh', check=False)
        if not a.wip:
            git(dest, 'reset', '-q', '--hard', commit)
        dirty = len(zlist(dest, 'status', '--porcelain', '-z', '--untracked-files=no'))
    except BaseException:
        git(src, 'worktree', 'remove', '--force', dest, check=False)
        try:
            force_rmtree(dest)
        except OSError:
            pass
        git(src, 'worktree', 'prune', check=False)
        if a.branch:
            git(src, 'branch', '-D', a.branch, check=False)
        raise
    out = dict(plan, cloned_files=c.cloned, copied_files=c.copied, copied_bytes=c.bytes,
               skipped=c.skipped, dirty_tracked_files=dirty, seconds=round(time.monotonic() - t0, 3))
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print(f'{dest}: {c.cloned} cloned, {c.copied} copied, {len(c.skipped)} skipped, '
              f'{dirty} dirty tracked, {out["seconds"]}s')
        for s in c.skipped:
            print(f'skipped {s["path"]}: {s["reason"]}', file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except (Fail, OSError) as e:
        print(f'error: {e}', file=sys.stderr)
        sys.exit(1)
