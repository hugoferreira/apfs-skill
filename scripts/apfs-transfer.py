#!/usr/bin/env python3
"""Checkpointed macOS tree transfer with destination APFS duplicate sharing."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import sqlite3
import tempfile
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent))
from apfs_common import (COPYFILE_ALL, COPYFILE_CLONE, COPYFILE_EXCL, COPYFILE_METADATA,
                         COPYFILE_NOFOLLOW, PROVENANCE_XATTR, UNSAFE_FLAGS, Conflict, Native,
                         hash_fd, safe_path, signature, sync_dir, sync_file)
from apfs_job import Job, Progress, control, default_state

PREFIX = '.apfs-transfer-stage-'
SYSTEM = {'.fseventsd', '.Spotlight-V100', '.Trashes', '.TemporaryItems', '.DocumentRevisions-V100'}
# COPYFILE_CLONE clones where possible and copies across volumes.
DATA_FLAGS = COPYFILE_ALL | COPYFILE_NOFOLLOW | COPYFILE_CLONE | COPYFILE_EXCL
METADATA_FLAGS = COPYFILE_METADATA | COPYFILE_NOFOLLOW


def sig(path):
    return signature(os.lstat(path))


def exists(path):
    return os.path.lexists(path)


def metadata(native, path):
    return native.metadata(path, nofollow=True)


def same_file_state(current, recorded):
    """Signatures equal apart from ctime and link count, which link()/unlink() of a
    sibling hard link change."""
    return current[:4] == recorded[:4] and current[5:9] == recorded[5:9]


def copyfile(native, source, destination, data=True, progress=None, clone=True):
    """Copy data and metadata (data=True) or metadata only, reporting bytes to progress."""
    flags = DATA_FLAGS if data else METADATA_FLAGS
    if not clone:
        flags &= ~COPYFILE_CLONE
    if progress is None:
        native.copyfile(source, destination, flags)
        return

    def on_progress(copied):
        progress.checkpoint()
        if copied is not None and progress.job:
            progress.job.publish(activity='Copying data', current_file_bytes=copied)
            progress.report()
    native.copyfile(source, destination, flags, on_progress)


def hash_regular(path, progress):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Conflict('Locked by another process; transfer idle trees: ' + str(path)) from None
        before = sig(path)
        if not stat.S_ISREG(os.fstat(fd).st_mode) or signature(os.fstat(fd)) != before:
            raise Conflict('Source changed: ' + str(path))
        done = 0

        def advance(n):
            nonlocal done
            done += n
            progress.checkpoint()
            if progress.job:
                progress.job.publish(current_file_bytes=done, io_bytes_hashed=progress.job.snapshot.get('io_bytes_hashed', 0) + n)
            progress.report()
        value = hash_fd(fd, advance)
        if sig(path) != before or signature(os.fstat(fd)) != before:
            raise Conflict('Changed while hashing: ' + str(path))
        if progress.job:
            progress.job.snapshot['hash_reads_completed'] += 1
        return value
    finally:
        os.close(fd)


def keeper_digest(job, path, relative, expected, progress):
    value = job.get('destination-sha256', relative, expected)
    if value is None:
        value = hash_regular(path, progress)
        ensure_unchanged(path, expected)
        job.put('destination-sha256', relative, expected, value)
    return value


def ensure_unchanged(path, expected):
    if (sig(path) if exists(path) else None) != expected:
        raise Conflict('Changed since checkpoint: ' + str(path))


def metadata_digest(value):
    mode, uid, gid, flags, mtime, birthtime, attrs, acl = value
    payload = [mode, uid, gid, flags, mtime, birthtime.hex(), sorted((k.hex(), v.hex()) for k, v in attrs.items()), acl.hex()]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def recover(job, destination, native, progress):
    """Settle files that an interrupted run left staged or just published."""
    for rel, raw in job.db.execute("SELECT path,value FROM cache WHERE kind='pending'").fetchall():
        value = json.loads(raw)
        dst, tmp = safe_path(destination, rel), safe_path(destination, value['temporary'])
        if tmp.parent != dst.parent or not tmp.name.startswith(PREFIX):
            raise Conflict('Invalid transfer staging record')
        if exists(tmp):
            s = tmp.lstat()
            if s.st_uid != os.geteuid() or not (stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode)):
                raise Conflict('Unsafe staging file: ' + str(tmp))
            if s.st_nlink != 1:
                keeper = safe_path(destination, value.get('hardlink_keeper', ''))
                if not value.get('hardlink_keeper') or not exists(keeper) or sig(keeper)[:2] != sig(tmp)[:2]:
                    raise Conflict('Unexpected staging hardlink: ' + str(tmp))
            if value.get('prepared'):
                ensure_unchanged(tmp, value['prepared'])
            tmp.unlink()
            sync_dir(tmp.parent)
        elif value.get('prepared') and exists(dst):
            actual = sig(dst)
            expected = value['prepared']
            if actual[:4] == expected[:4] and actual[5:] == expected[5:]:
                if metadata_digest(metadata(native, dst)) != value['metadata_digest']:
                    raise Conflict('Published metadata changed before recovery: ' + str(dst))
                if stat.S_ISREG(actual[5]):
                    if hash_regular(dst, progress) != value['result']['sha256']:
                        raise Conflict('Published contents changed before recovery: ' + str(dst))
                elif os.readlink(dst) != value['result']['symlink_target']:
                    raise Conflict('Published symlink changed before recovery: ' + str(dst))
                job.put('done', rel, [value['source_signature'], actual], value['result'])
            elif actual[:2] == expected[:2]:
                raise Conflict('Published file changed before recovery: ' + str(dst))
        job.db.execute("DELETE FROM cache WHERE kind='pending' AND path=?", (rel,))
        job.flush()


def manifest(job, source, exclusions, progress):
    """Record every entry of the source tree; refuse what a transfer cannot reproduce."""
    job.db.execute('CREATE TABLE IF NOT EXISTS manifest (path TEXT PRIMARY KEY, depth INTEGER, type TEXT, size INTEGER, signature TEXT)')
    job.db.execute('DELETE FROM manifest')
    files = size = 0
    device = source.stat().st_dev

    def error(exc):
        raise exc
    for directory, dirs, names in os.walk(source, followlinks=False, onerror=error):
        # Directory rows are restored in reverse depth order after all children.
        for name in ['.'] + sorted(dirs + names):
            path = Path(directory) if name == '.' else Path(directory) / name
            if name in exclusions:
                if name in dirs:
                    dirs.remove(name)
                continue
            if name != '.' and path.is_dir() and not path.is_symlink():
                continue
            progress.checkpoint()
            s = path.lstat()
            if s.st_dev != device:
                raise Conflict('Nested mount encountered: ' + str(path))
            if s.st_flags & UNSAFE_FLAGS or s.st_uid != os.geteuid() or s.st_mode & 0o6000:
                raise Conflict('Unsupported ownership or protected flags: ' + str(path))
            kind = 'directory' if stat.S_ISDIR(s.st_mode) else 'file' if stat.S_ISREG(s.st_mode) else 'symlink' if stat.S_ISLNK(s.st_mode) else None
            if kind is None:
                raise Conflict('Unsupported special file: ' + str(path))
            rel = str(path.relative_to(source))
            n = s.st_size if kind == 'file' else 0
            job.db.execute('INSERT OR REPLACE INTO manifest VALUES (?,?,?,?,?)', (rel, len(Path(rel).parts), kind, n, json.dumps(signature(s))))
            if kind != 'directory':
                files += 1
                size += n
            job.publish(phase='Scanning', files_done=files, files_total=None, bytes_done=size,
                        bytes_total=None, current_path=str(path))
    job.flush()
    return files, size


class Transfer:
    """One --apply run: the per-file steps and their counters."""

    def __init__(self, args, job, native, source, destination, progress):
        self.args, self.job, self.native, self.progress = args, job, native, progress
        self.source, self.destination = source, destination
        self.copied = self.resumed = self.cloned = self.hardlinked = 0
        self.roots = [(root, root.stat().st_dev, root.stat().st_ino) for root in (source, destination)]

    def require_roots(self):
        for root, device, inode in self.roots:
            current = root.stat()
            if (current.st_dev, current.st_ino) != (device, inode):
                raise Conflict('Volume disconnected or replaced: ' + str(root))

    def make_directories(self):
        # All directories exist before any leaf is published. Existing symlink parents fail.
        for rel, in self.job.db.execute("SELECT path FROM manifest WHERE type='directory' ORDER BY depth,path"):
            self.progress.checkpoint()
            path = safe_path(self.destination, rel)
            if exists(path) and (path.is_symlink() or not path.is_dir()):
                raise Conflict('Destination directory conflict: ' + str(path))
            path.mkdir(exist_ok=True)

    def completed(self, rel, kind, src, dst, source_sig, dest_sig):
        """Result of an earlier run that still describes src and dst, else None."""
        job, native, progress = self.job, self.native, self.progress
        previous = job.get('done', rel, [source_sig, dest_sig])
        if previous is None and kind == 'file' and source_sig[-1] > 1 and dest_sig:
            row = job.db.execute("SELECT signature,value FROM cache WHERE kind='done' AND path=?", (rel,)).fetchone()
            old = json.loads(row[0]) if row else None
            if old and old[0] == source_sig and same_file_state(dest_sig, old[1]):
                # link()/unlink() alter ctime and link count of all siblings.
                # Revalidate data and complete metadata; never blindly relax signatures.
                if hash_regular(src, progress) == hash_regular(dst, progress) and metadata(native, src) == metadata(native, dst):
                    ensure_unchanged(src, source_sig)
                    ensure_unchanged(dst, dest_sig)
                    previous = json.loads(row[1])
                    job.put('done', rel, [source_sig, dest_sig], previous)
        if previous is not None and self.args.verify_all:
            job.publish(activity='Rechecking completed file', current_file_bytes=0)
            matches = metadata(native, src) == metadata(native, dst)
            matches = matches and (hash_regular(src, progress) == hash_regular(dst, progress) if kind == 'file' else os.readlink(src) == os.readlink(dst))
            ensure_unchanged(src, source_sig)
            ensure_unchanged(dst, dest_sig)
            if not matches:
                previous = None
        return previous

    def find_keeper(self, key, digest, hardlink, provenance):
        """Destination file already holding this content (or this source inode), or None."""
        row = self.job.db.execute('SELECT path,signature FROM keepers WHERE key=?', (key,)).fetchone()
        if not row:
            return None
        candidate = safe_path(self.destination, row[0])
        if not exists(candidate):
            return None
        current = sig(candidate)
        # link() changes keeper ctime/nlink; validate other fields and its hash.
        if not same_file_state(current, json.loads(row[1])):
            return None
        if keeper_digest(self.job, candidate, row[0], current, self.progress) != digest:
            return None
        # A clone carries the keeper's provenance; macOS can refuse to rewrite it.
        if hardlink or self.native.xattrs(candidate).get(PROVENANCE_XATTR) == provenance:
            return candidate
        return None

    def publish(self, rel, kind, src, dst, source_sig, dest_sig):
        """Stage a verified copy of src beside dst, then replace dst atomically."""
        job, native, progress = self.job, self.native, self.progress
        if dest_sig is not None and not self.args.replace:
            raise Conflict('Destination exists without a valid checkpoint; use --replace after reviewing: ' + str(dst))
        if dest_sig is not None and not (stat.S_ISREG(dest_sig[5]) or stat.S_ISLNK(dest_sig[5])):
            raise Conflict('Refusing to replace non-file: ' + str(dst))
        expected_metadata = metadata(native, src)
        hardlink = kind == 'file' and source_sig[-1] > 1
        digest = key = keeper = None
        if kind == 'file':
            digest = job.get('sha256', rel, source_sig)
            if digest is None:
                job.publish(activity='Hashing source', current_file_bytes=0)
                digest = hash_regular(src, progress)
                job.put('sha256', rel, source_sig, digest)
            # Preserve hardlink topology separately from CoW duplicate sharing.
            key = 'inode:' + str(source_sig[:2]) if hardlink else 'sha256:' + digest
            keeper = self.find_keeper(key, digest, hardlink, expected_metadata[6].get(PROVENANCE_XATTR))
        tmp = dst.parent / (PREFIX + uuid.uuid4().hex)
        record = {'temporary': str(tmp.relative_to(self.destination)), 'source_signature': source_sig}
        if keeper is not None and hardlink:
            record['hardlink_keeper'] = str(keeper.relative_to(self.destination))
        job.put('pending', rel, [], record)
        job.flush()  # Must be durable before staging; never batch this commit.
        if keeper is not None and hardlink:
            os.link(keeper, tmp, follow_symlinks=False)
            self.hardlinked += 1
        elif keeper is not None:
            native.clone(keeper, tmp)
            native.restore(tmp, expected_metadata, src.stat().st_atime_ns)
            self.cloned += 1
        else:
            job.publish(activity='Copying data', current_file_bytes=0)
            copyfile(native, src, tmp, progress=progress)
        native.set_birth(tmp, expected_metadata[5], nofollow=True)
        if kind == 'file':
            job.publish(activity='Verifying staged file', current_file_bytes=0)
            if hash_regular(tmp, progress) != digest:
                raise Conflict('Copied checksum differs; original destination retained: ' + rel)
        elif os.readlink(src) != os.readlink(tmp):
            raise Conflict('Copied symlink differs: ' + rel)
        if metadata(native, tmp) != expected_metadata:
            raise Conflict('Copied metadata differs; original destination retained: ' + rel)
        ensure_unchanged(src, source_sig)
        ensure_unchanged(dst, dest_sig)
        if kind == 'file':
            sync_file(tmp)
        record.update(prepared=sig(tmp), metadata_digest=metadata_digest(expected_metadata),
                      result={'sha256': digest, 'symlink_target': os.readlink(tmp) if kind == 'symlink' else None})
        job.put('pending', rel, [], record)
        job.flush()
        os.replace(tmp, dst)
        sync_dir(dst.parent)
        final_sig = sig(dst)
        job.put('done', rel, [source_sig, final_sig], record['result'])
        if digest is not None:
            job.put('destination-sha256', rel, final_sig, digest)
        if key:
            job.db.execute('INSERT OR REPLACE INTO keepers VALUES (?,?,?)', (key, rel, json.dumps(final_sig)))
        job.db.execute("DELETE FROM cache WHERE kind='pending' AND path=?", (rel,))
        job.flush()
        self.copied += 1

    def copy_files(self):
        job, progress = self.job, self.progress
        job.db.execute('CREATE TABLE IF NOT EXISTS keepers (key TEXT PRIMARY KEY, path TEXT, signature TEXT)')
        for rel, kind, n, raw_sig in job.db.execute("SELECT path,type,size,signature FROM manifest WHERE type!='directory' ORDER BY path"):
            progress.current_path = rel
            job.publish(current_path=rel, activity='Inspecting file', current_file_bytes=0)
            progress.checkpoint()
            self.require_roots()
            src, dst = self.source / rel, safe_path(self.destination, rel)
            source_sig = json.loads(raw_sig)
            ensure_unchanged(src, source_sig)
            dest_sig = sig(dst) if exists(dst) else None
            if self.completed(rel, kind, src, dst, source_sig, dest_sig) is not None:
                self.resumed += 1
            else:
                self.publish(rel, kind, src, dst, source_sig, dest_sig)
            progress.count += 1
            progress.bytes += n
            job.snapshot.update(copied_files=self.copied, resumed_files=self.resumed,
                                cloned_files=self.cloned, hardlinked_files=self.hardlinked)
            progress.report()
        progress.report(force=True)

    def restore_directories(self):
        job, native, progress = self.job, self.native, self.progress
        job.publish(force=True, phase='Restoring directory metadata')
        for rel, raw_sig in job.db.execute("SELECT path,signature FROM manifest WHERE type='directory' ORDER BY depth DESC,path DESC"):
            progress.checkpoint()
            src, dst = self.source / rel, safe_path(self.destination, rel)
            ensure_unchanged(src, json.loads(raw_sig))
            expected = metadata(native, src)
            copyfile(native, src, dst, data=False, progress=progress)
            native.set_birth(dst, expected[5], nofollow=True)
            if metadata(native, dst) != expected:
                raise Conflict('Directory metadata differs: ' + rel)
            job.publish(current_path=rel)


def run(args, source, destination, state):
    native = Native()
    identity = {'tool': 'transfer', 'source': str(source), 'destination': str(destination),
                'source_volume': native.volume_identity(source),
                'destination_volume': native.volume_identity(destination),
                'exclusions': sorted(set(args.exclude) | (SYSTEM if args.exclude_system else set()))}
    exclusions = set(identity['exclusions'])
    if not args.apply:
        # Preview uses the same manifest validation, but only disposable state.
        with tempfile.TemporaryDirectory(prefix='apfs-transfer-preview-') as tmp:
            with Job(Path(tmp), identity) as job:
                files, size = manifest(job, source, exclusions, Progress(args.quiet, job))
                return {'schema_version': 1, 'ok': True, 'dry_run': True, 'files': files, 'logical_bytes': size,
                        'source': str(source), 'destination': str(destination)}
    job = Job(state, identity)
    rootfd = None
    try:
        rootfd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(rootfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Conflict('Destination is locked by another process: ' + str(destination)) from None
        progress = Progress(args.quiet, job)
        recover(job, destination, native, progress)
        files, size = manifest(job, source, exclusions, progress)
        progress.start('Copying and verifying', files, size)
        transfer = Transfer(args, job, native, source, destination, progress)
        transfer.make_directories()
        transfer.copy_files()
        transfer.restore_directories()
        result = {'schema_version': 1, 'ok': True, 'dry_run': False, 'files_verified': files,
                  'logical_bytes': size, 'copied_files': transfer.copied, 'resumed_files': transfer.resumed,
                  'cloned_files': transfer.cloned, 'hardlinked_files': transfer.hardlinked,
                  'state_dir': str(state), 'source_untouched': True}
        job.finish('complete', result=result)
        return result
    except BaseException as exc:
        job.finish('stopped' if isinstance(exc, KeyboardInterrupt) else 'failed', error=str(exc))
        raise
    finally:
        job.close()
        if rootfd is not None:
            os.close(rootfd)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
        epilog='Dry run by default. Destination directory must exist. No deletions or source writes. '
        'Use idle trees. Per-file atomic publication, SHA-256 and macOS metadata checks. '
        'Rerun with the same arguments/state to resume; changed checkpoints are rejected unless --replace. '
        'Pause/stop are cooperative requests; status.json acknowledges them. Ctrl-C/SIGTERM stop safely. '
        'Exit: 0 success, 1 failure/partial completion, 2 usage, 130 stopped. '
        'See apfs-tools.md in the skill references for limitations and detached execution.')
    p.add_argument('source', type=Path)
    p.add_argument('destination', type=Path, help='exact target directory, already existing')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('-n', '--dry-run', action='store_true')
    p.add_argument('--verify-all', action='store_true', help='reread data and metadata of completed files instead of relying on unchanged signatures')
    p.add_argument('--replace', action='store_true', help='allow atomic replacement of existing destination files; no deletions')
    p.add_argument('--state-dir', type=Path, help='checkpoint/status directory outside both trees; default ~/Library/Application Support/apfs-tools/transfer-HASH')
    p.add_argument('--control', choices=['status', 'pause', 'resume', 'stop'])
    p.add_argument('--exclude', action='append', default=[], metavar='BASENAME')
    p.add_argument('--exclude-system', action='store_true', help='exclude macOS volume bookkeeping directories')
    p.add_argument('--json', action='store_true')
    p.add_argument('-q', '--quiet', action='store_true')
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        p.print_help()
        return 0
    args = p.parse_args(argv)
    if any(not x or '/' in x or x in ('.', '..') for x in args.exclude):
        p.error('--exclude requires a basename')
    try:
        if sys.platform != 'darwin':
            raise ValueError('macOS required')
        source, destination = args.source.expanduser().resolve(strict=not bool(args.control)), args.destination.expanduser().resolve(strict=not bool(args.control))
        if not args.control and (not source.is_dir() or not destination.is_dir()):
            raise ValueError('Both roots must be existing directories')
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError('Source and destination must not overlap')
        state = args.state_dir.expanduser().absolute() if args.state_dir else default_state('transfer', source, destination)
        if any(state == root or root in state.resolve().parents for root in (source, destination)):
            raise ValueError('State directory must be outside both trees')
        result = control(state, args.control) if args.control else run(args, source, destination, state)
        if args.json:
            print(json.dumps(result))
        elif not args.quiet:
            print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, Conflict, sqlite3.Error, KeyboardInterrupt) as exc:
        stopped = isinstance(exc, KeyboardInterrupt)
        result = {'schema_version': 1, 'ok': False, 'error': 'interrupted' if stopped else 'operation_failed',
                  'message': str(exc) or 'Stopped; rerun to resume'}
        if args.json:
            print(json.dumps(result))
        else:
            print(result['message'], file=sys.stderr)
        return 130 if stopped else 1


if __name__ == '__main__':
    sys.exit(main())
