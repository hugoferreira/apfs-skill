#!/usr/bin/env python3
"""Instant copy-on-write checkpoints of a directory, without root or snapshot entitlements.

  apfs-checkpoint.py create DIR [ID] [-m NOTE]  clone DIR into its checkpoint store
  apfs-checkpoint.py list DIR                   list checkpoints, oldest first
  apfs-checkpoint.py diff DIR [ID]              paths changed in DIR since checkpoint ID
  apfs-checkpoint.py restore DIR [ID]           atomically swap DIR with a clone of ID
                                                (DIR/.git stays as it is now; --with-git
                                                restores the checkpoint's .git instead)
  apfs-checkpoint.py drop DIR ID | --pre-restore | --all
  options: --store PATH (default: DIR/../.DIRNAME.apfs-checkpoints), --json

ID defaults to the newest checkpoint made with `create`. The pre-restore-* checkpoints
that `restore` saves are selected only by explicit ID, so create / change / restore /
change / restore returns to the same baseline both times. Undo a restore with
`restore DIR pre-restore-...`.

A checkpoint is one clonefile(2) call on the whole directory: tracked, untracked and
ignored files alike, in well under a second for tens of thousands of files, sharing
all blocks with DIR until either side is written. It is not an APFS snapshot: it
captures one directory, it is writable, and creating it is not atomic with respect to
processes writing into DIR at that moment. The call fails as a whole if any item in
DIR is unreadable. Hard links inside DIR become independent files in the checkpoint.

diff output: M modified, A added since the checkpoint, D deleted since the checkpoint,
T type changed; entries named .git are ignored. Files that still share one APFS data
stream with their checkpoint counterpart are known identical without being read; all
others are compared byte by byte.

restore never discards data: the current DIR is swapped out with
renamex_np(RENAME_SWAP) and kept as a pre-restore-* checkpoint. Processes whose cwd or
open files are inside DIR keep referring to the swapped-out tree; restart dev servers,
watchers and shells after a restore.

Space: a checkpoint costs only metadata until files diverge. Deleting files from DIR
does not free their blocks while a checkpoint still references them; drop old
checkpoints to reclaim space. The store must be on DIR's APFS volume and outside DIR.
It is excluded from Time Machine backups when created.

Exit status: 0 success, 1 error. Errors go to stderr.
"""
import argparse
import datetime
import errno
import filecmp
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apfs_common import UF_COMPRESSED, Native, atomic_json, existing_ancestor, force_rmtree

META = 'checkpoint.json'
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
CLONE_HINTS = {
    errno.EXDEV: 'the checkpoint store must be on the same volume as DIR',
    errno.ENOTSUP: 'this filesystem does not support clones (APFS required)',
    errno.EACCES: 'an item in the tree is unreadable; a directory clone fails as a whole',
    errno.EINVAL: 'the destination is inside the source, or the source is not clonable',
}
native = Native()


class Fail(Exception):
    pass


def clone_tree(src, dst):
    try:
        native.clone(src, dst)
    except OSError as e:
        hint = CLONE_HINTS.get(e.errno)
        raise Fail(f'clonefile {src} -> {dst}: {e.strerror}' + (f' ({hint})' if hint else '')) from None


def resolve(d, store, need_dir):
    """Validate DIR and return (dir, store) as absolute paths."""
    d = Path(os.path.realpath(d))
    store = Path(os.path.realpath(store)) if store else d.parent / f'.{d.name}.apfs-checkpoints'
    if store == d or d in store.parents:
        raise Fail(f'store {store} is inside {d}; it must be outside the checkpointed directory')
    if not need_dir:
        return d, store
    if not d.is_dir():
        raise Fail(f'{d} is not a directory')
    if os.path.ismount(d):
        raise Fail(f'{d} is a volume root; checkpoint a directory below it')
    if native.mount(existing_ancestor(store)) != native.mount(d):
        raise Fail(f'store {store} is on another volume than {d}; clones cannot cross volumes '
                   '(pass --store PATH on the same volume)')
    return d, store


def write_meta(slot, **meta):
    atomic_json(slot / META, meta)
    return dict(meta, path=str(slot / 'tree'))


def entries(store):
    out = []
    if store.is_dir():
        for p in store.iterdir():
            try:
                meta = json.loads((p / META).read_text())
                meta['created']
            except (OSError, ValueError, KeyError, TypeError):
                continue  # incomplete slot; removed by `drop --all`
            meta['id'] = p.name
            meta.setdefault('kind', 'pre-restore' if p.name.startswith('pre-restore-') else 'manual')
            out.append(dict(meta, path=str(p / 'tree')))
    return sorted(out, key=lambda e: (e['created'], e['id']))


def pick(store, ident):
    es = entries(store)
    if ident is not None:
        for e in es:
            if e['id'] == ident:
                return e
        raise Fail(f'no checkpoint {ident!r} in {store}; see `list`')
    manual = [e for e in es if e['kind'] != 'pre-restore']
    if not manual:
        raise Fail(f'no checkpoints made with `create` in {store}' +
                   ('; give a pre-restore ID explicitly' if es else ''))
    return manual[-1]


def new_slot(store, ident):
    if not ID_RE.fullmatch(ident):
        raise Fail(f'invalid checkpoint id {ident!r}: use letters, digits, ".", "_", "-"')
    if not store.is_dir():
        store.mkdir(parents=True)
        native.exclude_from_backups(store)
    slot = store / ident
    try:
        slot.mkdir()
    except FileExistsError:
        raise Fail(f'checkpoint {ident!r} already exists') from None
    return slot


def create(d, store, note=None, ident=None):
    t = datetime.datetime.now()
    ident = ident or t.strftime('%Y%m%d-%H%M%S-%f')
    slot = new_slot(store, ident)
    try:
        clone_tree(d, slot / 'tree')
        return write_meta(slot, id=ident, kind='manual', source=str(d), note=note,
                          created=t.isoformat(timespec='microseconds'))
    except BaseException:
        force_rmtree(slot)
        raise


def same_file(a, b, sa, sb):
    if sa.st_size != sb.st_size:
        return False
    if not (sa.st_flags | sb.st_flags) & UF_COMPRESSED:
        # Equal clone ids: one shared data stream, identical without reading.
        ca = native.sharing(a).clone_id
        if ca is not None and ca == native.sharing(b).clone_id:
            return True
    return filecmp.cmp(a, b, shallow=False)


def changed(old, new):
    """[(status, relative path)] describing how tree `new` differs from tree `old`."""
    out = []

    def listing(d):
        with os.scandir(d) as it:
            return {e.name: e for e in it if e.name != '.git'}

    def visit(a, b, rel):
        ea, eb = listing(a), listing(b)
        for name in sorted(ea.keys() | eb.keys()):
            r = rel + name
            if name not in ea or name not in eb:
                e = ea.get(name) or eb[name]
                out.append(('D' if name in ea else 'A', r + ('/' if e.is_dir(follow_symlinks=False) else '')))
                continue
            sa, sb = ea[name].stat(follow_symlinks=False), eb[name].stat(follow_symlinks=False)
            if stat.S_IFMT(sa.st_mode) != stat.S_IFMT(sb.st_mode):
                out.append(('T', r))
            elif stat.S_ISDIR(sa.st_mode):
                visit(ea[name].path, eb[name].path, r + '/')
            elif stat.S_ISLNK(sa.st_mode):
                if os.readlink(ea[name].path) != os.readlink(eb[name].path):
                    out.append(('M', r))
            elif stat.S_ISREG(sa.st_mode):
                if (sa.st_mode ^ sb.st_mode) & 0o111 or not same_file(ea[name].path, eb[name].path, sa, sb):
                    out.append(('M', r))

    visit(str(old), str(new), '')
    return out


def restore(d, store, ident, with_git=False):
    e = pick(store, ident)
    tree = Path(e['path'])
    live_git, saved_git = d / '.git', tree / '.git'
    keep_git = not with_git and os.path.lexists(live_git)
    if with_git and os.path.lexists(live_git) and not os.path.lexists(saved_git):
        raise Fail(f'checkpoint {e["id"]} holds no .git; restore it without --with-git')
    if native.mount(d.parent) != native.mount(tree):
        raise Fail(f'{tree} and {d} are on different volumes')
    t = datetime.datetime.now()
    keep = f'pre-restore-{t.strftime("%Y%m%d-%H%M%S-%f")}'
    staging = d.parent / f'.{d.name}.{keep}'
    slot = new_slot(store, keep)
    try:
        clone_tree(tree, staging)
        if keep_git:
            # Keep commits, refs and the index made after the checkpoint. The live .git
            # is cloned, not moved, so DIR has a .git at every instant and the
            # swapped-out tree stays a complete repository.
            force_rmtree(staging / '.git')
            clone_tree(live_git, staging / '.git')
    except BaseException:
        force_rmtree(staging)
        force_rmtree(slot)
        raise
    # From the swap on, `staging` holds the user's previous tree: no interruption until
    # it is registered as a checkpoint.
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        try:
            native.swap(staging, d)
        except OSError as e:
            force_rmtree(staging)
            force_rmtree(slot)
            raise Fail(f'renamex_np(RENAME_SWAP) {staging} <-> {d}: {e.strerror}') from None
        os.rename(staging, slot / 'tree')
        write_meta(slot, id=keep, kind='pre-restore', source=str(d),
                   note=f'state before restoring {e["id"]}', created=t.isoformat(timespec='microseconds'))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    return dict(restored=e['id'], saved_previous_as=keep, kept_current_git=keep_git)


def is_slot(p):
    if p.is_symlink() or not p.is_dir():
        return False
    return all(c.name in ('tree', META) or c.name.startswith(META + '.') for c in p.iterdir())


def drop(store, ident, everything, pre_restore):
    if everything:
        # Complete and incomplete slots; anything else in a user-supplied --store stays.
        targets = sorted(p for p in store.iterdir() if is_slot(p)) if store.is_dir() else []
    elif pre_restore:
        targets = [Path(e['path']).parent for e in entries(store) if e['kind'] == 'pre-restore']
    elif ident:
        targets = [Path(pick(store, ident)['path']).parent]
    else:
        raise Fail('drop needs ID, --pre-restore or --all')
    for slot in targets:
        force_rmtree(slot)
    try:
        store.rmdir()  # succeeds only when empty
    except OSError:
        pass
    return [slot.name for slot in targets]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['create', 'list', 'diff', 'restore', 'drop'])
    ap.add_argument('dir')
    ap.add_argument('id', nargs='?')
    ap.add_argument('-m', '--note')
    ap.add_argument('--store')
    ap.add_argument('--all', action='store_true', help='with drop: delete every checkpoint')
    ap.add_argument('--pre-restore', action='store_true', help='with drop: delete the checkpoints saved by restore')
    ap.add_argument('--with-git', action='store_true', help="with restore: also restore the checkpoint's .git")
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    d, store = resolve(a.dir, a.store, need_dir=a.command not in ('list', 'drop'))
    if a.command == 'create':
        out = create(d, store, a.note, a.id)
        text = f'checkpoint {out["id"]} -> {out["path"]}'
    elif a.command == 'list':
        out = entries(store)
        text = '\n'.join(f'{e["id"]}  {e["created"][:19]}  {e["kind"]:11}  {e.get("note") or ""}'.rstrip()
                         for e in out) or 'no checkpoints'
    elif a.command == 'diff':
        e = pick(store, a.id)
        changes = changed(Path(e['path']), d)
        out = dict(id=e['id'], changes=[dict(status=s, path=p) for s, p in changes])
        text = '\n'.join(f'{s} {p}' for s, p in changes) or f'no differences from {e["id"]}'
    elif a.command == 'restore':
        out = restore(d, store, a.id, a.with_git)
        text = f'restored {out["restored"]}; previous state saved as {out["saved_previous_as"]}'
    else:
        out = dict(dropped=drop(store, a.id, a.all, a.pre_restore))
        text = 'dropped ' + (', '.join(out['dropped']) or 'nothing')
    print(json.dumps(out, indent=2) if a.json else text)


if __name__ == '__main__':
    try:
        main()
    except (Fail, OSError) as e:
        print(f'error: {e}', file=sys.stderr)
        sys.exit(1)
