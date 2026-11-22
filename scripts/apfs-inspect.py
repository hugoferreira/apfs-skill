#!/usr/bin/env python3
"""Report APFS clone, compression and sparseness state for files, trees and volumes.

Read-only. macOS + Python 3.9+, no dependencies.

  apfs-inspect.py FILE...             per-file sharing/compression report
  apfs-inspect.py --tree DIR          tree totals: logical, allocated, private, estimated physical
  apfs-inspect.py --families DIR      list clone families (files sharing one clone id)
  apfs-inspect.py --volume PATH       filesystem type, capabilities, free space
  apfs-inspect.py --can-clone A B     can files be cloned between paths A and B?
                                      (exit 0 only if yes; paths need not exist yet)
  add --json for one JSON object on stdout

Definitions (getattrlist(2)):
  allocated  st_blocks * 512; what du(1) and Finder "on disk" add up, so clones are
             counted once per file.
  private    ATTR_CMNEXT_PRIVATESIZE: bytes freed immediately if this file alone were
             deleted (not shared with a clone or a snapshot). APFS reports 0 for
             UF_COMPRESSED files, so private is shown as None for them.
  clone_id   ATTR_CMNEXT_CLONEID: equal ids mean the files are one shared data stream.
             A write gives the written file a new id.
  refcnt     ATTR_CMNEXT_CLONE_REFCNT: number of files sharing that data stream.
  codec      zlib, lzvn or lzfse for a transparently compressed file (decmpfs header).

--volume fields:
  free_bytes                     statvfs free space; equals df and diskutil
                                 "Container Free Space". Excludes purgeable space.
  available_incl_purgeable_bytes NSURLVolumeAvailableCapacityForImportantUsage: free
                                 space plus what macOS can purge on demand (Finder's
                                 "Available"). None if osascript is unavailable.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apfs_common import (EF_IS_PURGEABLE, EF_IS_SPARSE, EF_MAY_SHARE_BLOCKS, EF_SHARES_ALL_BLOCKS,
                         SF_DATALESS, UF_COMPRESSED, Native, Sharing, allocated_bytes,
                         existing_ancestor, free_bytes, human_bytes)

EXT = {EF_MAY_SHARE_BLOCKS: 'may_share_blocks', EF_IS_PURGEABLE: 'purgeable',
       EF_IS_SPARSE: 'sparse', EF_SHARES_ALL_BLOCKS: 'shares_all_blocks'}
COUNTS = ('root', 'files', 'compressed_files', 'clone_shared_files', 'hardlinked_extra', 'unreadable')
native = Native()


def file_report(path):
    st = os.lstat(path)
    s = native.sharing(path) if stat.S_ISREG(st.st_mode) else Sharing(None, None, None, None)
    compressed = bool(st.st_flags & UF_COMPRESSED)
    flags = [name for bit, name in EXT.items() if (s.ext_flags or 0) & bit]
    codec = native.codec(path) if compressed else None
    if compressed:
        flags.append('compressed' + (f'={codec}' if codec else ''))
    if st.st_flags & SF_DATALESS:
        flags.append('dataless')
    if st.st_nlink > 1 and stat.S_ISREG(st.st_mode):
        flags.append(f'hardlinks={st.st_nlink}')
    return dict(path=str(path), logical=st.st_size, allocated=allocated_bytes(st),
                private=None if compressed else s.private, clone_id=s.clone_id,
                refcnt=s.refcnt, compressed=compressed, codec=codec, flags=flags,
                inode=st.st_ino, dev=st.st_dev)


def walk(root, errors):
    """Yield regular files below root, staying on its volume."""
    dev = os.lstat(root).st_dev
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError as e:
            print(f'skip {d}: {e.strerror}', file=sys.stderr)
            errors.append(d)
            continue
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    errors.append(entry.path)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if st.st_dev == dev:
                        stack.append(entry.path)
                elif stat.S_ISREG(st.st_mode):
                    yield entry.path


def require_dir(root):
    if not os.path.isdir(root):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), root)


def tree_report(root):
    require_dir(root)
    t = dict(root=str(root), files=0, logical=0, allocated=0, private=0,
             compressed_files=0, compressed_allocated=0, clone_shared_files=0,
             hardlinked_extra=0, unreadable=0, estimated_physical=0)
    family_shared = {}
    seen_inodes = set()
    errors = []
    for p in walk(root, errors):
        try:
            r = file_report(p)
        except OSError:
            errors.append(p)
            continue
        key = (r['dev'], r['inode'])
        if key in seen_inodes:
            t['hardlinked_extra'] += 1
            continue
        seen_inodes.add(key)
        t['files'] += 1
        t['logical'] += r['logical']
        t['allocated'] += r['allocated']
        if r['compressed']:
            t['compressed_files'] += 1
            t['compressed_allocated'] += r['allocated']
            # No private size for compressed files: count full allocation once per clone id.
            cid = r['clone_id'] or ('inode', key)
            family_shared[cid] = max(family_shared.get(cid, 0), r['allocated'])
            continue
        priv = r['private'] if r['private'] is not None else r['allocated']
        t['private'] += priv
        shared = max(r['allocated'] - priv, 0)
        if shared:
            t['clone_shared_files'] += 1
            cid = r['clone_id'] or ('inode', key)
            family_shared[cid] = max(family_shared.get(cid, 0), shared)
    # Shared bytes may also be held only by a snapshot or by a file outside the tree;
    # counting each clone family's shared bytes once is therefore an upper bound.
    t['estimated_physical'] = t['private'] + sum(family_shared.values())
    t['unreadable'] = len(errors)
    return t


def families(root):
    require_dir(root)
    groups = {}
    for p in walk(root, []):
        try:
            r = file_report(p)
        except OSError:
            continue
        if r['clone_id'] and (r['refcnt'] or 0) > 1:
            groups.setdefault(r['clone_id'], []).append(r)
    return [dict(clone_id=cid, refcnt=rs[0]['refcnt'], logical=rs[0]['logical'],
                 paths_in_tree=[r['path'] for r in rs])
            for cid, rs in sorted(groups.items(), key=lambda kv: -kv[1][0]['logical'])]


def important_capacity(path):
    """Free space including purgeable, via Foundation (JXA). None when unavailable."""
    script = ('ObjC.import("Foundation"); function run(argv) { '
              'var k = "NSURLVolumeAvailableCapacityForImportantUsageKey"; '
              'var d = $.NSURL.fileURLWithPath(argv[0]).resourceValuesForKeysError([k], null); '
              'return String(ObjC.unwrap(d.objectForKey(k))); }')
    try:
        r = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script, str(path)],
                           capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def volume_report(path, purgeable=True):
    mount = native.mount(path)
    fmt, interfaces = native.capabilities(path)
    sv = os.statvfs(path)
    out = dict(path=str(path), mount_point=mount.point, device=mount.device, fstype=mount.fstype,
               format=fmt, interfaces=interfaces,
               free_bytes=free_bytes(path), total_bytes=sv.f_blocks * sv.f_frsize)
    if purgeable:
        out['available_incl_purgeable_bytes'] = important_capacity(path)
    return out


def can_clone(a, b):
    """Clones need one mounted volume whose filesystem supports clonefile(2)."""
    ea, eb = existing_ancestor(a), existing_ancestor(b)
    ma, mb = native.mount(ea), native.mount(eb)
    supported = bool(native.capabilities(ea)[1].get('clone'))
    if ma != mb:
        reason = f'different volumes: {ma.point} and {mb.point}; a copy would be written in full'
    elif not supported:
        reason = f'{ma.fstype} at {ma.point} does not support clones'
    else:
        reason = 'same volume with clone support'
    return dict(can_clone=ma == mb and supported, reason=reason, a=str(a), b=str(b))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--tree', metavar='DIR')
    g.add_argument('--families', metavar='DIR')
    g.add_argument('--volume', metavar='PATH')
    g.add_argument('--can-clone', nargs=2, metavar=('A', 'B'))
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    status = 0

    def short(n):
        return human_bytes(n, compact=True)

    if a.tree:
        out = tree_report(a.tree)
        text = '\n'.join(f'{k:22} {v if k in COUNTS else short(v)}' for k, v in out.items())
    elif a.families:
        out = families(a.families)
        text = '\n'.join(f'{f["clone_id"]:#x} refcnt={f["refcnt"]} {short(f["logical"])}\n  ' + '\n  '.join(f['paths_in_tree'])
                         for f in out) or 'no clone families with refcnt > 1'
    elif a.volume:
        out = volume_report(a.volume)
        text = json.dumps(out, indent=2)
    elif a.can_clone:
        out = can_clone(*a.can_clone)
        text = ('yes: ' if out['can_clone'] else 'no: ') + out['reason']
        status = 0 if out['can_clone'] else 1
    elif a.paths:
        out = [file_report(p) for p in a.paths]
        rows = [f'{"logical":>9} {"alloc":>9} {"private":>9} {"refcnt":>6}  flags / path']
        for r in out:
            rows.append(f'{short(r["logical"]):>9} {short(r["allocated"]):>9} {short(r["private"]):>9} '
                        f'{r["refcnt"] if r["refcnt"] is not None else "-":>6}  {",".join(r["flags"]) or "-"}  {r["path"]}')
        text = '\n'.join(rows)
    else:
        ap.error('give FILE..., --tree, --families, --volume or --can-clone')
    print(json.dumps(out, indent=2) if a.json else text)
    sys.exit(status)


if __name__ == '__main__':
    try:
        main()
    except OSError as e:
        print(f'error: {e}', file=sys.stderr)
        sys.exit(1)
