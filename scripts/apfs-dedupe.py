#!/usr/bin/env python3
"""Deduplicate equal file contents using independent APFS copy-on-write clones.

macOS + Python 3.9+. No dependencies or network access. See --help.
A file that is open elsewhere, locked, changing or gone is skipped, not replaced.
"""
import argparse
from collections import Counter, defaultdict
import fcntl
import gzip
import heapq
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid
import zlib
import sqlite3
import tempfile
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent))
from apfs_common import (EF_SHARES_ALL_BLOCKS, PROVENANCE_XATTR, SF_DATALESS, STAGE_PAYLOAD,
                         STAGING_PREFIXES, UF_COMPRESSED, UNSAFE_FLAGS,
                         XATTR_NOFOLLOW, Conflict, Native, allocated_bytes, file_checkpoint,
                         free_bytes, hash_fd, human_bytes, lstat_or_none, make_private_stage,
                         matches_checkpoint, metadata_digest, on_device, private_stage_directory,
                         remove_orphan_stage, remove_private_stage, safe_path, signature,
                         sync_dir, sync_file, tracked_staging_path, unchanged)
from apfs_job import Job, Progress, control, default_state

VERSION = '1.7.0'
JOURNAL_INDEX_VERSION = 2
PREFIX = '.apfs-dedupe-stage-'
COMPRESSED = UF_COMPRESSED
# Staged clones are exchanged in batches; one open-file snapshot covers a batch.
BATCH_FILES, BATCH_SECONDS = 64, 1.0


class ProvenanceMismatch(Conflict):
    pass


class Skip(Conflict):
    """One file is in use, changing, unreadable or gone. It is left as found; the run continues."""
    def __init__(self, reason, path):
        super().__init__(f'{reason}: {path}')
        self.reason = reason


def eligible(s):
    # Hashing a dataless (iCloud, File Provider) file would download it.
    return (stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_size > 0
            and s.st_uid == os.geteuid() and not s.st_flags & (UNSAFE_FLAGS | SF_DATALESS)
            and not s.st_mode & 0o6000)


def reclaimable_bytes(native, path):
    """Estimate storage freed by replacing path with a clone; not a guarantee."""
    s = path.lstat()
    shared = native.sharing(path)
    private, flags = shared.private, shared.ext_flags
    if flags is not None and flags & EF_SHARES_ALL_BLOCKS:
        return 0
    # APFS reports zero private bytes for compressed files that share nothing.
    if private is not None and not s.st_flags & COMPRESSED:
        return min(private, allocated_bytes(s))
    return allocated_bytes(s)


def order_keeper(native, files, completed, origins, root, digest, skip):
    """Move the copy with the smallest allocation first; it becomes the clone source.

    Equal allocations prefer compressed storage, then the largest APFS clone
    family in the group, then a file not replaced by a previous run. Remaining
    ties and all destinations keep scan order. Unreadable files are passed to
    skip and dropped.

    Track the canonical path separately from the source of the smallest
    stream. Use that path as the source once it shares the chosen stream.
    """
    known = []
    for path, sig in files:
        try:
            known.append((path, sig, native.sharing(path).clone_id, allocated_bytes(path.lstat())))
        except OSError as e:
            skip(path, e)
    families = Counter(clone_id for _, _, clone_id, _ in known if clone_id)
    def rank(index):
        path, sig, clone_id, allocated = known[index]
        return (allocated, not bool(sig[8] & COMPRESSED), -families.get(clone_id, 1),
                completed.get(str(path.relative_to(root))) == sig)
    if not known:
        return [], None
    keeper = min(range(len(known)), key=rank)
    source, _, clone_id, _ = known[keeper]
    canonical = str(source.relative_to(root))
    origin = origins.get(canonical)
    if origin and origin[0] == digest and origin[1] != canonical:
        for index, (path, _, candidate_id, _) in enumerate(known):
            if str(path.relative_to(root)) == origin[1]:
                canonical = origin[1]
                if clone_id and candidate_id == clone_id:
                    keeper = index
                break
    known.insert(0, known.pop(keeper))
    return [item[:3] for item in known], canonical


def measured_hash(fd, on_bytes=None, metrics=None):
    def advance(n):
        if metrics:
            metrics.snapshot['io_bytes_hashed'] += n
        if on_bytes:
            on_bytes(n)
    digest = hash_fd(fd, advance)
    if metrics:
        metrics.snapshot['hash_reads_completed'] += 1
    return digest


def hash_file(path, on_bytes=None, cache=None, metrics=None):
    """Return (sha256, signature), or raise Skip if the file changes while read.

    No lock is taken: holding one would make other programs' lock attempts fail.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not eligible(before):
            raise Skip('File became ineligible', path)
        sig = signature(before)
        digest = cache.get('sha256', path, sig) if cache else None
        cached = digest is not None
        metrics = metrics or cache
        if digest is None:
            digest = measured_hash(fd, on_bytes, metrics)
        else:
            if metrics:
                metrics.snapshot['cached_hashes_reused'] += 1
            if on_bytes:
                on_bytes(before.st_size)
        if signature(os.fstat(fd)) != sig or signature(path.lstat()) != sig:
            raise Skip('File changed while hashing', path)
        if cache and not cached:
            cache.put('sha256', path, sig, digest)
        return digest, sig
    finally:
        os.close(fd)


def validate_journal_file(fd):
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.geteuid():
        raise Conflict('Journal must be an owned regular file with one link')
    return s


def root_identity(root):
    """{'uuid', 'root_inode'}: the volume and inode of the root directory, or None."""
    try:
        return Native().volume_identity(root)
    except (OSError, ValueError):
        return None


def validate_header(records, root):
    """Require the journal of this root.

    A header with 'volume' is bound to the root directory by volume UUID and
    inode, so a renamed or remounted root keeps its journal. Older headers
    are bound to the root path.
    """
    if not records:
        return
    header = records[0]
    expected = {'event': 'header', 'version': 1, 'root': str(root)}
    bound = header.get('volume') if isinstance(header, dict) else None
    if bound:
        valid = ({**header, 'root': str(root)} == {**expected, 'volume': bound}
                 and bound == root_identity(root))
    else:
        valid = header == expected
    if not valid:
        raise Conflict('Journal belongs to another root or version. '
                       'If this tree is a copy of a deduplicated tree, delete the copied journal')


def journal_records(fd, compressed, on_record=None):
    """Return complete records and the safe append offset; reject corruption.

    A compressed append is one complete member. Only records from members with
    validated deflate data and CRC/trailer are accepted. A partial final member
    never authorized a filesystem mutation, so it may be dropped on recovery.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    records, end = [], 0
    with os.fdopen(os.dup(fd), 'rb') as raw:
        if not compressed:
            for line in raw:
                if not line.endswith(b'\n'):
                    break
                record = json.loads(line)
                on_record(record) if on_record else records.append(record)
                end += len(line)
            return records, end
        decoder = zlib.decompressobj(31)
        parts, offset = [], 0
        while True:
            chunk = raw.read(8192)
            if not chunk:
                break
            while chunk:
                try:
                    parts.append(decoder.decompress(chunk))
                except zlib.error as e:
                    raise Conflict('Corrupt gzip journal; file left intact: ' + str(e)) from e
                if not decoder.eof:
                    offset += len(chunk)
                    break
                offset += len(chunk) - len(decoder.unused_data)
                payload = b''.join(parts)
                if payload and not payload.endswith(b'\n'):
                    raise Conflict('Complete gzip member contains incomplete JSONL')
                for line in payload.splitlines():
                    record = json.loads(line)
                    on_record(record) if on_record else records.append(record)
                end = offset
                chunk = decoder.unused_data
                decoder, parts = zlib.decompressobj(31), []
        # Bulk members are only produced by atomic migration. Their truncation
        # indicates damage, not an interrupted single-record append.
        if b''.join(parts).count(b'\n') > 1:
            raise Conflict('Truncated bulk gzip member; journal left intact')
    return records, end


def legacy_journal(path):
    """Name of the uncompressed journal older versions wrote: PATH without .gz."""
    return Path(str(path)[:-3])


def migrate_journal(path, root):
    """Publish and verify gzip before removing an owned, locked legacy journal."""
    legacy = legacy_journal(path)
    if not legacy.exists() and not legacy.is_symlink():
        return
    oldfd = os.open(legacy, os.O_RDONLY | os.O_NOFOLLOW)
    tmp = path.parent / (PREFIX + uuid.uuid4().hex)
    try:
        original = validate_journal_file(oldfd)
        fcntl.flock(oldfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        records, _ = journal_records(oldfd, False)
        validate_header(records, root)
        if path.exists() or path.is_symlink():
            # Resume an interruption between publishing gzip and unlinking plain.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                validate_journal_file(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                existing, _ = journal_records(fd, True)
                validate_header(existing, root)
                if existing[:len(records)] != records:
                    raise Conflict('Plain and gzip journals differ; both left intact')
            finally:
                os.close(fd)
        else:
            fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(os.dup(fd), 'wb') as raw:
                    with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as out:
                        for record in records:
                            out.write((json.dumps(record, sort_keys=True) + '\n').encode())
                    raw.flush()
                    os.fsync(raw.fileno())
                verified, end = journal_records(fd, True)
                if verified != records or end != os.fstat(fd).st_size:
                    raise Conflict('Compressed journal migration failed verification')
            finally:
                os.close(fd)
            if signature(legacy.lstat()) != signature(original):
                raise Conflict('Legacy journal changed during migration; original retained')
            os.replace(tmp, path)
            sync_dir(path.parent)
        if signature(legacy.lstat()) != signature(original):
            raise Conflict('Legacy journal changed during migration; both left intact')
        legacy.unlink()
        sync_dir(path.parent)
    finally:
        os.close(oldfd)
        if tmp.exists():
            tmp.unlink()


class SignatureMap:
    def __init__(self, records, kind):
        self.records, self.kind = records, kind

    def get(self, key, default=None):
        row = self.records.job.db.execute('SELECT value FROM dedupe_history WHERE kind=? AND key=?',
                                          (self.kind, key)).fetchone()
        return json.loads(row[0]) if row else default


class DiskRecords:
    """Compact journal state on disk; do not retain millions of event dictionaries."""
    def __init__(self, job):
        self.job = job
        job.db.execute('CREATE TABLE IF NOT EXISTS dedupe_history (kind TEXT, key TEXT, value TEXT, PRIMARY KEY(kind,key))')

    def __bool__(self):
        return self[0] is not None

    def __getitem__(self, key):
        if key != 0:
            raise IndexError(key)
        return SignatureMap(self, 'header').get('header')

    def append(self, record):
        event = record['event']
        db = self.job.db
        if event == 'header':
            kind, key, value = 'header', 'header', record
        elif event == 'stage':
            kind, key, value = 'pending', record['id'], record
        elif event == 'prepared':
            kind, key = 'pending', record['id']
            value = SignatureMap(self, kind).get(key)
            if value is None:
                raise Conflict('Invalid journal: prepared without stage')
            value['prepared'] = record['signature']
            if 'generation' in record:
                value['replacement_generation'] = record['generation']
        elif event == 'conflict':
            kind, key = 'pending', record['id']
            value = SignatureMap(self, kind).get(key)
            if value is None:
                raise Conflict('Invalid journal: conflict without stage')
            value['conflict'] = True
        elif event == 'done':
            stage = SignatureMap(self, 'pending').get(record['id'])
            db.execute("DELETE FROM dedupe_history WHERE kind='pending' AND key=?", (record['id'],))
            kind, key, value = 'done', record['destination'], record['signature']
            if stage and stage.get('source') and stage.get('sha256'):
                source, digest = stage['source'], stage['sha256']
                canonical = stage.get('canonical_keeper')
                if canonical is None:
                    # Legacy records only name the physical clone source.
                    origin = SignatureMap(self, 'origin').get(source)
                    canonical = origin[1] if origin and origin[0] == digest else source
                db.execute('INSERT OR REPLACE INTO dedupe_history VALUES (?,?,?)',
                           ('origin', key, json.dumps([digest, canonical])))
        elif event == 'excluded':
            kind, key, value = 'excluded', record['destination'], record['signature']
        elif event == 'aborted':
            db.execute("DELETE FROM dedupe_history WHERE kind='pending' AND key=?", (record['id'],))
            return
        else:
            raise Conflict('Unknown journal event: ' + event)
        db.execute('INSERT OR REPLACE INTO dedupe_history VALUES (?,?,?)', (kind, key, json.dumps(value)))

    def pending(self):
        return {key: json.loads(value) for key, value in self.job.db.execute("SELECT key,value FROM dedupe_history WHERE kind='pending'")}


def load_disk_records(fd, compressed, job, path, root):
    records = DiskRecords(job)
    cached = job.get('journal-index', str(path), signature(os.fstat(fd)))
    if cached is not None and cached.get('version') == JOURNAL_INDEX_VERSION:
        validate_header(records, root)
        return records, cached['end']
    job.db.execute("DELETE FROM cache WHERE kind='journal-index'")
    job.db.execute('DELETE FROM dedupe_history')
    count = 0
    def consume(record):
        nonlocal count
        # Validate the first header before indexing any later event.
        if count == 0:
            validate_header([record], root)
        records.append(record)
        count += 1
        if count % 256 == 0:
            job.flush()
            job.checkpoint()
            job.publish(phase='Recovering journal', files_done=0, files_total=None, records_done=count,
                        bytes_done=os.lseek(fd, 0, os.SEEK_CUR), bytes_total=os.fstat(fd).st_size)
    _, end = journal_records(fd, compressed, on_record=consume)
    validate_header(records, root)
    job.put('journal-index', str(path), signature(os.fstat(fd)), {'end': end, 'version': JOURNAL_INDEX_VERSION})
    job.flush()
    return records, end


class Journal:
    def __init__(self, path, root, job=None):
        self.job = job
        self.path, self.root = path, root
        self.compressed = str(path).endswith('.gz')
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            validate_journal_file(self.fd)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if job:
                self.records, end = load_disk_records(self.fd, self.compressed, job, path, root)
            else:
                self.records, end = journal_records(self.fd, self.compressed)
            validate_header(self.records, root)
            if end != os.fstat(self.fd).st_size:
                os.ftruncate(self.fd, end)
                os.fsync(self.fd)
            os.lseek(self.fd, 0, os.SEEK_END)
            if not self.records:
                header = dict(event='header', version=1, root=str(root))
                identity = root_identity(root)
                if identity:
                    header['volume'] = identity
                self.add(**header)
                sync_dir(path.parent)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def add(self, **record):
        data = (json.dumps(record, sort_keys=True) + '\n').encode()
        if self.compressed:
            data = gzip.compress(data, compresslevel=6, mtime=0)
        try:
            while data:
                data = data[os.write(self.fd, data):]
            os.fsync(self.fd)
        except OSError as e:
            raise Conflict('Journal write failed: ' + str(e)) from e
        self.records.append(record)
        if self.job:
            stat = os.fstat(self.fd)
            self.job.put('journal-index', str(self.path), signature(stat),
                         {'end': stat.st_size, 'version': JOURNAL_INDEX_VERSION})

    def recover(self, open_files):
        """Settle interrupted replacements; open_files is Native.open_files."""
        if isinstance(self.records, DiskRecords):
            pending, completed = self.records.pending(), SignatureMap(self.records, 'done')
        else:
            pending, completed = {}, {}
        for r in ([] if isinstance(self.records, DiskRecords) else self.records):
            if r['event'] == 'stage':
                pending[r['id']] = r
            elif r['event'] == 'prepared':
                if r['id'] not in pending:
                    raise Conflict('Invalid journal: prepared without stage')
                pending[r['id']]['prepared'] = r['signature']
                if 'generation' in r:
                    pending[r['id']]['replacement_generation'] = r['generation']
            elif r['event'] == 'conflict':
                if r['id'] not in pending:
                    raise Conflict('Invalid journal: conflict without stage')
                pending[r['id']]['conflict'] = True
            elif r['event'] == 'done':
                completed[r['destination']] = r['signature']
                pending.pop(r['id'], None)
            elif r['event'] == 'aborted':
                pending.pop(r['id'], None)
        native = Native() if pending else None
        # Device numbers change with attach order. The job identity or the
        # journal header has matched the volume UUID, so recorded identities
        # are read with the root's current device.
        header = self.records[0] if self.records else None
        same_volume = self.job is not None or bool(header and header.get('volume'))
        device = self.root.stat().st_dev if pending else None
        for r in pending.values():
            recorded = r.get('staging_directory')
            if same_volume and recorded and recorded[0] != device:
                r['staging_directory'] = on_device(recorded, device)
                for key in ('original', 'prepared'):
                    r[key] = on_device(r.get(key), device)
        for ident, r in list(pending.items()):
            dst = safe_path(self.root, r['destination'])
            name, tmp = tracked_staging_path(native, self.root, dst, r, PREFIX)
            directory = r.get('staging_directory')
            staged, original = r.get('prepared'), r.get('original')
            fingerprint = r.get('metadata_digest')
            replacement = dict(signature=staged, metadata=fingerprint,
                               generation=r.get('replacement_generation'))
            s = lstat_or_none(tmp) if tmp is not None else None
            if s is not None:
                if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or s.st_nlink != 1:
                    raise Conflict('Unsafe staging file: ' + str(tmp))
                if staged and original and signature(s)[:2] == original[:2] != staged[:2]:
                    # Interrupted after the exchange: the staging name holds the original.
                    if r.get('conflict'):
                        raise Conflict(conflict_message(name, dst))
                    busy = open_files()
                    try:
                        original_checkpoint = None
                        if fingerprint is not None:
                            original_checkpoint = dict(signature=original, metadata=fingerprint,
                                                       generation=r.get('original_generation'))
                        else:
                            # A legacy journal has no metadata fingerprint. A rollback is
                            # safe only when the replacement still has the recorded bytes
                            # and both files have equivalent user-visible metadata.
                            try:
                                if hash_file(dst)[0] != r['sha256']:
                                    raise Conflict(conflict_message(name, dst))
                                current = file_checkpoint(native, dst)
                                if current['metadata'] != metadata_digest(native.metadata(tmp, nofollow=True)):
                                    raise Conflict(conflict_message(name, dst))
                                replacement.update(metadata=current['metadata'],
                                                   generation=current['generation'])
                            except (Skip, OSError) as error:
                                raise Conflict(conflict_message(name, dst)) from error
                        settle_swapped(native, tmp, dst, replacement, tuple(staged[:2]) in busy,
                                       original_checkpoint, display=name)
                    except Conflict:
                        self.add(event='conflict', id=ident)
                        raise
                else:
                    if staged and (signature(s) != staged or fingerprint is not None):
                        # The ctime may have changed during an interrupted rollback.
                        if (fingerprint is None or tuple(staged[:2]) in open_files()
                                or not matches_checkpoint(native, tmp, replacement)):
                            raise Conflict('Staging file changed or is open: ' + str(name))
                    tmp.unlink()
                    sync_dir(tmp.parent)
            if directory is not None:
                remove_private_stage(native, name, directory)
            if s is None and staged and lstat_or_none(dst) is not None:
                try:
                    digest, sig = hash_file(dst)
                except (Skip, OSError):
                    digest, sig = None, None
                # The replacement completed if dst is still the verified clone.
                # One changed or replaced since then is rescanned like any file.
                if sig and digest == r['sha256'] and unchanged(sig, staged):
                    self.add(event='done', id=ident, destination=r['destination'], signature=sig)
                    if isinstance(completed, dict):
                        completed[r['destination']] = sig
                    continue
            self.add(event='aborted', id=ident)
        return completed


def conflict_message(tmp, dst):
    return (f'Conflicting versions at {dst} and {tmp}. For recovery, both are kept. '
            f'Keep the wanted version at {dst}, remove {tmp}, then rerun')


def settle_swapped(native, tmp, dst, replacement, clone_open, original_checkpoint, display=None):
    """Reverse an exchange that was not confirmed. tmp holds the original, dst held the clone.

    The original returns to dst while the clone there is unwritten and unopened.
    Otherwise the path carries newer data: the clone stands, and the original
    is removed if it is known to be unmodified and unused. If neither side can
    be discarded without losing data, both are kept and Conflict is raised.
    The rollback also exchanges names, so a change to the clone during the
    rollback remains recoverable under the stage name.
    """
    if not clone_open and matches_checkpoint(native, dst, replacement):
        native.swap(tmp, dst)
        sync_dir(dst.parent)
        if tmp.parent != dst.parent:
            sync_dir(tmp.parent)
        if (tuple(replacement['signature'][:2]) in native.open_files(mappings=False)
                or not matches_checkpoint(native, tmp, replacement)):
            raise Conflict(conflict_message(display or tmp, dst))
        tmp.unlink()
    elif (original_checkpoint is not None
          and tuple(original_checkpoint['signature'][:2]) not in native.open_files(mappings=False)
          and matches_checkpoint(native, tmp, original_checkpoint)):
        tmp.unlink()
    else:
        raise Conflict(conflict_message(display or tmp, dst))
    sync_dir(tmp.parent)
    if tmp.parent != dst.parent:
        sync_dir(dst.parent)


def discard_stage(native, journal, ident, tmp, directory):
    """Remove an unpublished clone and close its journal entry."""
    anchor = private_stage_directory(native, tmp.parent, directory, missing_ok=True)
    if anchor is not None:
        (anchor / STAGE_PAYLOAD).unlink(missing_ok=True)
        sync_dir(anchor)
        remove_private_stage(native, tmp, directory)
    journal.add(event='aborted', id=ident)


def prepare(native, root, src, dst, digest, expected_src, expected_dst, journal,
            cache=None, on_bytes=None, busy=(), canonical_keeper=None):
    """Stage a verified clone of src beside dst for publish(); dst is not touched.

    Raises Skip when dst is open or mapped elsewhere (busy is a recent
    Native.open_files() result) or when either file changed since the scan.
    """
    src, dst = safe_path(root, str(src.relative_to(root))), safe_path(root, str(dst.relative_to(root)))
    ident = uuid.uuid4().hex
    tmp = dst.parent / (PREFIX + ident) / STAGE_PAYLOAD
    relative = str(dst.relative_to(root))
    journaled = False
    source_fd = fd = None
    try:
        source_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        source_stat = os.fstat(source_fd)
        if signature(source_stat) != expected_src or signature(src.lstat()) != expected_src:
            raise Skip('Kept copy changed since scan', src)
        if source_stat.st_flags & COMPRESSED:
            native.require_verified_compression_xattrs(src)
        fd = os.open(dst, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if signature(os.fstat(fd)) != expected_dst or signature(dst.lstat()) != expected_dst:
            raise Skip('Changed since scan', dst)
        if tuple(expected_dst[:2]) in busy:
            raise Skip('Open in another process', dst)
        # Read before hashing; publish() requires the same value after the exchange.
        generation = native.generation(dst)
        if generation == 0:
            raise Skip('Mapped for writing by another process', dst)
        source_digest = cache.get('sha256', src, expected_src) if cache else None
        if source_digest is None:
            source_digest = measured_hash(source_fd, on_bytes, cache)
        elif cache:
            cache.snapshot['cached_hashes_reused'] += 1
        if source_digest != digest:
            raise Skip('Kept copy changed since scan', src)
        if measured_hash(fd, on_bytes, cache) != digest:
            raise Skip('Contents changed since scan', dst)
        ds = os.fstat(fd)
        metadata = native.metadata(dst)
        fingerprint = metadata_digest(metadata)
        directory = make_private_stage(native, tmp.parent)
        work = private_stage_directory(native, tmp.parent, directory) / STAGE_PAYLOAD
        journal.add(event='stage', id=ident, destination=relative, source=str(src.relative_to(root)),
                    temporary=str(tmp.relative_to(root)), staging_directory=directory,
                    sha256=digest, original=expected_dst, metadata_digest=fingerprint,
                    original_generation=generation, canonical_keeper=canonical_keeper)
        journaled = True
        native.clone(src, work)
        native.restore(work, metadata, ds.st_atime_ns)
        # Hashing the clone, not src, covers a src written while it was cloned.
        actual, _ = hash_file(work, on_bytes=on_bytes, metrics=cache)
        if actual != digest:
            raise Skip('Kept copy changed while cloning', src)
        copied_metadata = native.metadata(work)
        if copied_metadata != metadata:
            labels = ['mode', 'uid', 'gid', 'flags', 'mtime', 'birthtime', 'xattrs', 'ACL']
            differences = [name for name, a, b in zip(labels, metadata, copied_metadata) if a != b]
            if 'xattrs' in differences:
                a, b = metadata[6], copied_metadata[6]
                differences += [os.fsdecode(k) for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)]
            if differences == ['xattrs', os.fsdecode(PROVENANCE_XATTR)]:
                # Some provenance is regenerated by clonefile itself. Preserve
                # the original and journal the exclusion for subsequent runs.
                discard_stage(native, journal, ident, tmp, directory)
                journaled = False
                journal.add(event='excluded', destination=relative,
                            signature=expected_dst, reason='provenance_not_preserved')
                raise ProvenanceMismatch('macOS did not preserve com.apple.provenance: ' + str(dst))
            raise Skip('Clone metadata failed verification (' + ', '.join(differences) + ')', dst)
        os.utime(work, ns=(ds.st_atime_ns, ds.st_mtime_ns))
        sync_file(work)
        sync_dir(work.parent)
        staged = signature(work.lstat())
        replacement = dict(signature=staged, metadata=fingerprint,
                           generation=native.generation(work))
        journal.add(event='prepared', id=ident, signature=staged,
                    generation=replacement['generation'])
        return SimpleNamespace(ident=ident, dst=dst, tmp=tmp, relative=relative, fd=fd, digest=digest,
                               expected=expected_dst, metadata=metadata, generation=generation,
                               staged=staged, replacement=replacement, directory=directory, work=work)
    except BaseException as e:
        if fd is not None:
            os.close(fd)
        if isinstance(e, (Skip, OSError)):
            # dst is untouched; tmp, if present, is the unpublished clone.
            if journaled:
                discard_stage(native, journal, ident, tmp, directory)
            if not isinstance(e, Skip):
                raise Skip(e.strerror or str(e), e.filename or dst) from e
        # Journal recovery handles the staged file after other failures or interruption.
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)


def publish(native, journal, batch, cache=None):
    """Exchange staged clones with their destinations, then remove the originals.

    Returns ([(item, final signature or Skip)], open-file snapshot). Each
    destination is rechecked, including against fresh snapshots of open
    files, and exchanged with its clone atomically (RENAME_SWAP), so the
    original survives under the staging name. A snapshot taken after the
    exchanges then decides each original: if a process holds it open, or its
    data or metadata changed, the exchange is reversed and that process keeps
    the file at the destination path. Otherwise the original is removed.

    Snapshots next to the exchange list descriptors only, which keeps the
    interval in which a process can slip between check and exchange near
    10 ms. A writable mapping without a descriptor shows as generation 0. A
    read-only mapping made in the 0.1 s before the exchange is not seen.

    Not detected: a descriptor open in a process this user may not inspect and
    not yet written to. A destination written through its path while a process
    that slipped past the first check still uses the original cannot be
    merged: both files are kept, the journal records the conflict, and
    Conflict is raised after the rest of the batch is settled.
    """
    outcomes, exchanged, reverting, conflict = {}, [], [], None
    try:
        snapshot = native.open_files()
        busy = snapshot | native.open_files(mappings=False)
        for item in batch:
            try:
                if tuple(item.expected[:2]) in busy:
                    raise Skip('Open in another process', item.dst)
                # Held until the exchange is confirmed or marked for reversal;
                # a cooperating flock holder wins.
                try:
                    fcntl.flock(item.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Skip('Locked by another process', item.dst) from None
                if signature(os.fstat(item.fd)) != item.expected or signature(item.dst.lstat()) != item.expected:
                    raise Skip('Changed during verification', item.dst)
                if native.metadata(item.dst) != item.metadata or native.generation(item.dst) != item.generation:
                    raise Skip('Changed during verification', item.dst)
                private_stage_directory(native, item.tmp.parent, item.directory)
                if signature(item.work.lstat()) != item.staged:
                    raise Skip('Staged clone changed', item.tmp)
                native.swap(item.work, item.dst)
                exchanged.append(item)
            except (Skip, OSError) as e:
                discard_stage(native, journal, item.ident, item.tmp, item.directory)
                outcomes[item.ident] = e if isinstance(e, Skip) else Skip(e.strerror or str(e), item.dst)
        # item.dst now names the clone and item.tmp the original, still open as item.fd.
        busy = native.open_files(mappings=False)
        for item in exchanged:
            try:
                if tuple(item.expected[:2]) in busy:
                    reverting.append((item, 'Opened by another process during replacement'))
                elif (not unchanged(signature(os.fstat(item.fd)), item.expected)
                      or native.generation(item.work) != item.generation
                      or native.metadata(item.work) != item.metadata):
                    reverting.append((item, 'Changed during replacement'))
            except OSError as e:
                reverting.append((item, 'Replacement could not be confirmed (' + (e.strerror or str(e)) + ')'))
        for item in batch:
            try:
                fcntl.flock(item.fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if reverting:
            busy = native.open_files(mappings=False)
        for item, reason in reverting:
            try:
                clone_open = tuple(item.staged[:2]) in busy or native.generation(item.dst) == 0
                settle_swapped(native, item.work, item.dst, item.replacement,
                               clone_open, None, display=item.tmp)
                remove_private_stage(native, item.tmp, item.directory)
            except Conflict as e:
                journal.add(event='conflict', id=item.ident)
                outcomes[item.ident] = Skip('Conflict; both versions kept', item.dst)
                conflict = e
                continue
            journal.add(event='aborted', id=item.ident)
            outcomes[item.ident] = Skip(reason, item.dst)
        for item in exchanged:
            if item.ident in outcomes:
                continue
            sync_dir(item.dst.parent)
            sync_dir(item.work.parent)
            item.work.unlink()
            sync_dir(item.work.parent)
            remove_private_stage(native, item.tmp, item.directory)
            current = lstat_or_none(item.dst)
            final = signature(current) if current else None
            if final is None or not unchanged(final, item.staged):
                # Another process wrote, replaced or removed dst after the exchange; its version stands.
                journal.add(event='aborted', id=item.ident)
                outcomes[item.ident] = Skip('Changed after replacement', item.dst)
                continue
            journal.add(event='done', id=item.ident, destination=item.relative, signature=final)
            if cache:
                cache.put('sha256', item.dst, final, item.digest)
                provenance = item.metadata[6].get(PROVENANCE_XATTR)
                cache.put('provenance', item.dst, final, {'hex': provenance.hex() if provenance is not None else None})
            outcomes[item.ident] = final
        if conflict:
            raise conflict
        return [(item, outcomes[item.ident]) for item in batch], snapshot
    finally:
        for item in batch:
            os.close(item.fd)
        # Journal recovery settles anything left exchanged by a failure here.


def clone_one(native, root, src, dst, digest, expected_src, expected_dst, journal, cache=None, on_bytes=None):
    """Replace dst with a verified clone of src; raise Skip, leaving dst as found, if it is in use."""
    item = prepare(native, root, src, dst, digest, expected_src, expected_dst, journal, cache, on_bytes,
                   native.open_files())
    (_, outcome), = publish(native, journal, [item], cache)[0]
    if isinstance(outcome, Skip):
        raise outcome
    return outcome


def read_completed(path, root, job=None):
    """Inspect an existing journal without recovery or mutation."""
    if not path.exists() and not path.is_symlink():
        return {}, {}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        validate_journal_file(fd)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if job:
            lines, _ = load_disk_records(fd, str(path).endswith('.gz'), job, path, root)
            return SignatureMap(lines, 'done'), read_exclusions(lines)
        lines, _ = journal_records(fd, str(path).endswith('.gz'))
        validate_header(lines, root)
        return ({r['destination']: r['signature'] for r in lines if r['event'] == 'done'},
                read_exclusions(lines))
    finally:
        os.close(fd)


def read_exclusions(records):
    if isinstance(records, DiskRecords):
        return SignatureMap(records, 'excluded')
    return {r['destination']: r['signature'] for r in records if r['event'] == 'excluded'}


def directory_groups(root, groups, exclusions, journal_path):
    """Find directories whose complete contents equal another directory's.

    Equal means the same names and, recursively, the same subdirectories, the
    same symlink targets, empty files, and non-empty files in the same
    provenance-compatible content group. Any other entry, including unhashed,
    excluded, staging or foreign-device entries, disqualifies the directory.
    Only the outermost equal directories are returned.
    """
    device = root.stat().st_dev
    member = {path: index for index, (_, paths) in enumerate(groups) for path, _ in paths}
    blocked = {journal_path, legacy_journal(journal_path)}
    interned, info, memo = {}, [], {}

    def signature(directory):
        if directory in memo:
            return memo[directory]
        memo[directory] = None
        entries, files, size, subdirs = [], 0, 0, []
        try:
            with os.scandir(directory) as it:
                items = sorted(it, key=lambda e: e.name)
            for entry in items:
                path = Path(entry.path)
                if entry.name in exclusions or entry.name.startswith(STAGING_PREFIXES) or path in blocked:
                    return None
                s = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(s.st_mode):
                    entries.append(('l', entry.name, os.readlink(path)))
                elif stat.S_ISDIR(s.st_mode):
                    if s.st_dev != device:
                        return None
                    subdirs.append(path)
                elif stat.S_ISREG(s.st_mode) and s.st_size == 0:
                    entries.append(('e', entry.name))
                elif path in member:
                    entries.append(('f', entry.name, member[path]))
                    files += 1
                    size += s.st_size
                else:
                    return None
        except OSError:
            # Entries removed or replaced since the scan disqualify the directory.
            return None
        # Subdirectories last: a disqualifying file avoids descending.
        for path in subdirs:
            ident = signature(path)
            if ident is None:
                return None
            entries.append(('d', path.name, ident))
            files += info[ident][0]
            size += info[ident][1]
        ident = interned.setdefault(tuple(entries), len(interned))
        if ident == len(info):
            info.append((files, size))
        memo[directory] = ident
        return ident

    climbed = set()
    for directory in {path.parent for _, paths in groups for path, _ in paths}:
        while directory not in climbed:
            climbed.add(directory)
            if signature(directory) is None or directory == root:
                break
            directory = directory.parent
    by_ident = defaultdict(list)
    for directory, ident in memo.items():
        if ident is not None and directory != root and info[ident][0]:
            by_ident[ident].append(directory)
    result = []
    for ident, directories in by_ident.items():
        # Redundant when distinct, equal parents already form the larger match.
        parents = {d.parent for d in directories}
        idents = {memo.get(d) for d in parents}
        if len(directories) < 2 or (len(parents) == len(directories) and len(idents) == 1 and None not in idents):
            continue
        result.append({'directories': sorted(str(d.relative_to(root)) for d in directories),
                       'files_per_directory': info[ident][0], 'bytes_per_directory': info[ident][1]})
    return result


def select_units(planned, dir_groups, limit):
    """Choose up to limit directory or file-group units by remaining reclaimable bytes.

    Changes inside an identical directory belong to directory units; file-group
    units hold only the remaining changes. Each change counts toward one unit.
    Overlapping directory units are chosen greedily and re-valued without
    changes already taken.
    """
    units, owner = [], {}
    for group in dir_groups:
        for directory in group['directories']:
            owner[directory] = len(units)
        units.append(dict(group, kind='directory', changes=[], order=(0, group['directories'][0])))
    by_content = {}
    for i, change in enumerate(planned):
        parts = change['destination'].split('/')
        owned = False
        for depth in range(1, len(parts)):
            unit = owner.get('/'.join(parts[:depth]))
            if unit is not None:
                units[unit]['changes'].append(i)
                owned = True
        if owned:
            continue
        key = change['size_bytes'], change['sha256'], change['source']
        if key not in by_content:
            by_content[key] = len(units)
            units.append({'kind': 'file', 'sha256': change['sha256'], 'size_bytes': change['size_bytes'],
                          'source': change['source'], 'changes': [], 'order': (1, change['sha256'])})
        units[by_content[key]]['changes'].append(i)
    taken = set()
    def value(unit):
        return sum(planned[i]['size_bytes'] for i in unit['changes'] if i not in taken)
    heap = [(-value(u), u['order'], n) for n, u in enumerate(units)]
    heapq.heapify(heap)
    chosen = []
    while heap and len(chosen) < limit:
        negative, order, n = heapq.heappop(heap)
        current = value(units[n])
        if current == 0:
            continue
        if current != -negative:
            heapq.heappush(heap, (-current, order, n))
            continue
        unit = units[n]
        changes = [planned[i] for i in unit['changes'] if i not in taken]
        taken.update(unit['changes'])
        record = {'rank': len(chosen) + 1, 'kind': unit['kind']}
        if unit['kind'] == 'directory':
            def role(directory):
                prefix = directory + '/'
                if any(c['destination'].startswith(prefix) for c in changes):
                    return 'clone'
                return 'keep' if any(c['source'].startswith(prefix) for c in changes) else 'unchanged'
            record.update(directories=[{'path': d, 'role': role(d)} for d in unit['directories']],
                          files_per_directory=unit['files_per_directory'],
                          bytes_per_directory=unit['bytes_per_directory'])
        else:
            record.update(sha256=unit['sha256'], size_bytes=unit['size_bytes'],
                          source=unit['source'], canonical_keeper=changes[0]['canonical_keeper'])
        record.update(destinations=[c['destination'] for c in changes], planned_files=len(changes),
                      logical_duplicate_bytes=sum(c['size_bytes'] for c in changes),
                      estimated_physical_bytes=sum(c['estimated_physical_bytes'] for c in changes),
                      cloned_files=0)
        chosen.append(record)
    return chosen


def path_tree(entries):
    """Render (relative path, label, is_directory) as a tree; unlabelled single-subdirectory chains collapse."""
    root = {}
    for path, label, is_directory in entries:
        node = root
        *dirs, name = path.split('/')
        for part in dirs:
            node = node.setdefault(part + '/', [None, {}])[1]
        if is_directory:
            node.setdefault(name + '/', [None, {}])[0] = label
        else:
            node[name] = [label, {}]
    rows = []
    def walk(node, depth):
        for name in sorted(node):
            label, children = node[name]
            # A directory holding exactly one subdirectory prints as one segment.
            while label is None and len(children) == 1 and next(iter(children)).endswith('/'):
                (sub, (label, children)), = children.items()
                name += sub
            rows.append((depth, name, label))
            walk(children, depth + 1)
    walk(root, 0)
    width = min(72, max((2 * depth + len(name) for depth, name, label in rows if label), default=0))
    return [('  ' * depth + name).ljust(width) + '  ' + label if label else '  ' * depth + name
            for depth, name, label in rows]


def print_top(units, applied, shown=3):
    if not units:
        print('No duplicate groups to rank.')
        return
    print(f'Top {len(units):,} duplicate groups by reclaimable size:')
    entries = []
    for u in units:
        status = f'; cloned {u["cloned_files"]:,}/{u["planned_files"]:,}' if applied else ''
        head = (f'{u["rank"]:>4}. {human_bytes(u["logical_duplicate_bytes"])} reclaimable '
                f'(~{human_bytes(u["estimated_physical_bytes"])} physical): ')
        if u['kind'] == 'directory':
            order = {'keep': 0, 'clone': 1, 'unchanged': 2}
            members = sorted(u['directories'], key=lambda d: order[d['role']])
            visible = members[:shown + 1]
            hidden = len(members) - len(visible)
            more = f'; {hidden:,} more directories in --json' if hidden > 0 else ''
            print(f'{head}{len(members):,} identical directories, {u["files_per_directory"]:,} files '
                  f'({human_bytes(u["bytes_per_directory"])}) each; {u["planned_files"]:,} files to clone{status}{more}')
            entries += [(d['path'], f'#{u["rank"]} {d["role"]}', True) for d in visible]
        else:
            hidden = len(u['destinations']) - shown
            more = f'; {hidden:,} more clones in --json' if hidden > 0 else ''
            print(f'{head}{u["planned_files"]:,} x {human_bytes(u["size_bytes"])}{status}  '
                  f'sha256 {u["sha256"][:12]}{more}')
            using_other_source = u['canonical_keeper'] != u['source']
            if using_other_source:
                print(f'      Canonical keeper {u["canonical_keeper"]}')
            source_role = 'donor' if using_other_source else 'keep'
            entries.append((u['source'], f'#{u["rank"]} {source_role}', False))
            entries += [(d, f'#{u["rank"]} clone', False) for d in u['destinations'][:shown]]
    print()
    for line in path_tree(entries):
        print('   ' + line)


def print_summary(result, top):
    """Human-readable outcome of a run; result is the --json object."""
    applied = not result['dry_run']
    planned, skipped = result['planned_files'], result['skipped_files']
    count = result['cloned_files'] if applied else planned
    size = result['cloned_logical_bytes'] if applied else result['logical_duplicate_bytes']
    print(f'{"Cloned and verified" if applied else "Would clone"} {count:,} files ({size:,} logical bytes).')
    if not applied and planned:
        print(f'Estimated physical savings: ~{human_bytes(result["estimated_physical_bytes"])}.')
    if result['already_completed_files']:
        print(f'Already completed: {result["already_completed_files"]:,} files.')
    if result['already_shared_files']:
        print(f'Already sharing the keeper’s blocks: {result["already_shared_files"]:,} files.')
    if result['provenance_unmatched_files']:
        print(f'Preserved {len(result["provenance_unmatched_files"]):,} files with incompatible provenance.')
    if skipped:
        print(f'Skipped {len(skipped):,} files in use, changed or unreadable; rerun to retry them:')
        for item in skipped[:10]:
            print(f'  {item["path"]}: {item["reason"]}')
        if len(skipped) > 10:
            print(f'  {len(skipped) - 10:,} more in --json')
    if result.get('deferred_files'):
        print(f'Outside --top {top:,}: {result["deferred_files"]:,} files in {result["deferred_groups"]:,} groups '
              f'({result["deferred_logical_bytes"]:,} logical bytes) left unchanged.')
    if applied:
        print('Journal: ' + result['journal'])
    else:
        print('Dry run; use --apply to replace files. Physical savings may be lower.')


def scan(root, minimum, exclusions, journal_path, progress, native, skip, stages=None):
    """Group eligible files by content and provenance.

    The tree may change during the scan: entries that disappear are ignored,
    and a file that changes or cannot be read is passed to skip and left out.
    """
    sizes = defaultdict(list)
    scanned = skipped = directories = seen = 0
    device = root.stat().st_dev
    progress.emit('Scanning: 0 files seen; total not yet known', force=True)
    def walk_error(error):
        # A directory that vanished is ignored; one that cannot be listed holds
        # nothing this run could replace, so it is reported and left out.
        if not isinstance(error, FileNotFoundError):
            skip(Path(error.filename or root), error)
    def descend(path):
        s = lstat_or_none(path)
        return s is not None and stat.S_ISDIR(s.st_mode) and s.st_dev == device
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        directories += 1
        if stages is not None:
            stages.extend(Path(directory) / n for n in dirs if n.startswith(PREFIX))
        dirs[:] = sorted(n for n in dirs if n not in exclusions and not n.startswith(STAGING_PREFIXES)
                         and descend(Path(directory) / n))
        for name in sorted(files):
            progress.checkpoint()
            seen += 1
            if progress.job:
                progress.job.publish(phase='Scanning', files_done=seen, files_total=None,
                                     bytes_done=0, bytes_total=None, current_path=str(Path(directory) / name))
            path = Path(directory) / name
            if name in exclusions or name.startswith(PREFIX) or path in (journal_path, legacy_journal(journal_path)):
                continue
            s = lstat_or_none(path)
            if s is None:
                continue
            if not eligible(s) or s.st_size < minimum or s.st_dev != device:
                skipped += 1
                continue
            if s.st_flags & COMPRESSED:
                try:
                    native.require_verified_compression_xattrs(path)
                except OSError as error:
                    skip(path, error)
                    continue
            sizes[s.st_size].append(path)
            scanned += 1
        progress.emit(f'Scanning: {seen:,} files seen; {scanned:,} eligible; {directories:,} directories')
    candidates = [(size, paths) for size, paths in sorted(sizes.items(), reverse=True) if len(paths) > 1]
    progress.start('Hashing', sum(len(paths) for size, paths in candidates),
                   sum(size * len(paths) for size, paths in candidates))
    groups = defaultdict(list)
    for size, paths in candidates:
        for path in paths:
            progress.current_path = str(path)
            try:
                digest, sig = hash_file(path, on_bytes=progress.advance_bytes, cache=progress.job)
                groups[size, digest].append((path, sig))
            except (Skip, OSError) as e:
                skip(path, e)
            progress.count += 1
            progress.report()
    progress.report(force=True)
    compatible, unmatched = [], []
    # macOS can silently ignore writes to com.apple.provenance. Never replace a
    # destination using data cloned from a file with different provenance.
    metadata_candidates = [(key, paths) for key, paths in groups.items() if len(paths) > 1]
    progress.start('Checking provenance', sum(len(paths) for key, paths in metadata_candidates), 0)
    for key, paths in metadata_candidates:
        provenance_groups = defaultdict(list)
        for path, sig in paths:
            progress.current_path = str(path)
            progress.checkpoint()
            progress.count += 1
            cached = progress.job.get('provenance', path, sig) if progress.job else None
            try:
                if cached is not None:
                    provenance = bytes.fromhex(cached['hex']) if cached['hex'] is not None else None
                else:
                    provenance = native.xattr(path, PROVENANCE_XATTR, XATTR_NOFOLLOW)
                if signature(path.lstat()) != sig:
                    raise Skip('File changed after hashing', path)
            except (Skip, OSError) as e:
                skip(path, e)
                continue
            if progress.job and cached is None:
                progress.job.put('provenance', path, sig, {'hex': provenance.hex() if provenance is not None else None})
            provenance_groups[provenance].append((path, sig))
            progress.report()
        for subgroup in provenance_groups.values():
            if len(subgroup) > 1:
                compatible.append((key, subgroup))
            elif len(provenance_groups) > 1:
                unmatched.append(str(subgroup[0][0].relative_to(root)))
    progress.report(force=True)
    return compatible, scanned, skipped, unmatched


SHORT_HELP = '''Share identical file data in a tree using APFS clones. Dry run by default.

usage: apfs-dedupe.py ROOT [--top N] [--apply] [--json] [options]

Examples:
  apfs-dedupe.py ~/project                    preview all duplicates
  apfs-dedupe.py ~/project --top 10           preview the 10 largest sets
  apfs-dedupe.py ~/project --top 10 --apply   clone only those
  apfs-dedupe.py ~/project --apply            clone every duplicate
  apfs-dedupe.py ~/project --control status   inspect a running or stopped job

Run 'apfs-dedupe.py --help' for all options.
'''

EPILOG = '''examples:
  apfs-dedupe.py ~/project                    preview all duplicates
  apfs-dedupe.py ~/project --top 10           preview the 10 largest sets
  apfs-dedupe.py ~/project --top 10 --apply   clone only those
  apfs-dedupe.py ~/project --apply --json     clone everything; one JSON result
  apfs-dedupe.py ~/project --control stop     stop safely; rerun to resume

what is deduplicated:
  Regular files on ROOT's volume with identical contents and identical
  com.apple.provenance. Compressed and sparse files are included; in each
  group the copy using the least space is kept. Files sharing that copy's
  APFS data stream are skipped. When a journaled clone has the smallest
  stream, its original keeper retains the canonical path if it is still
  in the same duplicate group. Skipped: symlinks, hard
  links, empty files, files owned by others, special files, protected or
  setuid/setgid files, .git and --exclude names.

safety:
  Each file is replaced atomically by a clone after its data and metadata
  (mode, owner, mtime, birthtime, xattrs, ACLs) are verified; there is no
  tree-wide transaction. Inodes, ctime and parent directory times change.

  ROOT may be in use. A file that another process holds open or mapped,
  has locked with flock, or changes during the run is skipped and listed
  (skipped_files); rerun to retry it. A file opened or written at the
  moment of replacement is detected afterwards and the original is put
  back. If the replacement was also written by then, both versions are
  kept and the run stops, naming them. Scanning takes no locks.
  Not detected: a file held open, but not yet written, by a process of
  another user (visible only to root).

--top N:
  Ranks duplicate sets by reclaimable bytes and limits the plan, and
  --apply, to the largest N. A set is either a group of identical files or
  directories whose complete contents are identical (shown as one entry).
  Rerunning picks the next N. Paths print as one tree tagged #RANK keep/clone.

recovery:
  --apply records progress in a journal (ROOT/.apfs-dedupe.jsonl.gz) and a
  state directory. Ctrl-C, SIGTERM or --control stop finish the current file
  and exit 130; rerun the same command to resume. Keep the journal: deleting
  it causes recloning. Deleting the state only costs rereading files.

results:
  A summary on stdout, progress on stderr. --json prints one object
  (schema_version 1; ignore unknown fields); errors have ok=false, error and
  message. Logical bytes are an upper bound; estimated_physical_bytes is an
  estimate; volume_free_change_bytes is measured.

exit status:
  0    success, including nothing to do and skipped files
  1    failure or partial completion
  2    usage error
  130  stopped or interrupted

Details: references/apfs-tools.md in the apfs-dev skill.
Requires macOS and Python 3.9+.
'''


def parser():
    p = argparse.ArgumentParser(
        description='Share identical file data in a tree using APFS clones.\n'
                    'Dry run by default; --apply replaces files.',
        formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False, epilog=EPILOG)
    p.add_argument('root', type=Path, metavar='ROOT', help='directory to deduplicate, recursively')
    plan = p.add_argument_group('what to do')
    mode = plan.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='replace duplicates with verified clones')
    mode.add_argument('-n', '--dry-run', action='store_true', help='preview only (default)')
    plan.add_argument('--top', type=int, metavar='N', help='only the N largest duplicate sets; see --top below')
    plan.add_argument('--min-size', type=int, default=1, metavar='BYTES', help='ignore smaller files (default: 1)')
    plan.add_argument('--exclude', action='append', default=[], metavar='NAME',
                      help='skip files and directories with this name; repeatable')
    out = p.add_argument_group('output')
    out.add_argument('--json', action='store_true', help='print one JSON object on stdout')
    out.add_argument('-q', '--quiet', action='store_true', help='no progress or summary (--top list still prints)')
    state = p.add_argument_group('state and control')
    state.add_argument('--journal', type=Path, metavar='FILE',
                       help='journal path (default: ROOT/.apfs-dedupe.jsonl.gz); .gz is appended if missing')
    state.add_argument('--state-dir', type=Path, metavar='DIR',
                       help='status and cache directory, outside ROOT '
                            '(default with --apply: ~/Library/Application Support/apfs-tools/dedupe-HASH)')
    state.add_argument('--rehash', action='store_true', help='ignore cached hashes and reread every candidate')
    state.add_argument('--control', choices=['status', 'pause', 'resume', 'stop'], metavar='ACTION',
                       help='status, pause, resume or stop the job for ROOT; does not scan')
    p.add_argument('--version', action='version', version=VERSION)
    return p


def main(argv=None):
    p = parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print(SHORT_HELP, end='')
        return 0
    args = p.parse_args(argv)
    if args.min_size < 1:
        p.error('--min-size must be positive')
    if any(not x or '/' in x or x in ('.', '..') for x in args.exclude):
        p.error('--exclude requires a basename')
    if args.top is not None and args.top < 1:
        p.error('--top must be positive')
    if args.top is not None and args.control:
        p.error('--top cannot be combined with --control')
    start = time.monotonic()
    journal, rootfd, job, temporary_state = None, None, None, None
    changed = changed_bytes = 0
    cloned = set()
    progress = Progress(args.quiet)
    try:
        if sys.platform != 'darwin':
            raise OSError('APFS cloning requires macOS')
        root = args.root.expanduser().resolve(strict=not bool(args.control))
        if not args.control and not root.is_dir():
            raise OSError('Root must be a directory: ' + str(root))
        state_dir = args.state_dir.expanduser().absolute() if args.state_dir else default_state('dedupe', root)
        if state_dir.resolve() == root or root in state_dir.resolve().parents:
            raise ValueError('State directory must be outside the scanned tree')
        if args.control:
            print(json.dumps(control(state_dir, args.control), indent=None if args.json else 2))
            return 0
        jp = (args.journal.expanduser().absolute() if args.journal else root / '.apfs-dedupe.jsonl.gz')
        if not str(jp).endswith('.gz'):
            jp = Path(str(jp) + '.gz')
        legacy_jp = legacy_journal(jp)
        native = Native()
        skipped = []
        def skip(path, error):
            reason = error.reason if isinstance(error, Skip) else error.strerror or str(error)
            skipped.append({'path': str(path.relative_to(root)), 'reason': reason})
            progress.emit(f'Skipped ({reason}): {path}')
        if args.apply or args.state_dir:
            job = Job(state_dir, {'tool': 'dedupe', 'root': str(root), 'volume': native.volume_identity(root)})
            progress.job = job
            if args.rehash:
                job.db.execute("DELETE FROM cache WHERE kind IN ('sha256','provenance')")
                job.flush()
        free_before = free_bytes(root)
        completed, previous_exclusions = {}, {}
        if args.apply:
            rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            fcntl.flock(rootfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            migrate_journal(jp, root)
            journal = Journal(jp, root, job=job)
            completed = journal.recover(native.open_files)
            previous_exclusions = read_exclusions(journal.records)
        else:
            read_path = jp if jp.exists() or jp.is_symlink() else legacy_jp
            if job is None and read_path.exists():
                temporary_state = tempfile.TemporaryDirectory(prefix='apfs-dedupe-preview-')
                job = Job(Path(temporary_state.name), {'tool': 'dedupe-preview', 'root': str(root)})
                progress.job = job
            completed, previous_exclusions = read_completed(read_path, root, job=job)
        origins = {}
        if job and (args.apply or read_path.exists()):
            origins = SignatureMap(DiskRecords(job), 'origin')
        stages = [] if args.apply else None
        groups, scanned, excluded, unmatched = scan(root, args.min_size, {'.git', *args.exclude}, jp, progress,
                                                    native, skip, stages)
        # Recovery has settled every journaled stage. An empty directory left
        # is from a run that died between mkdir and its journal record.
        for stage in stages or ():
            remove_orphan_stage(stage)
        planned, tasks = [], []
        progress.start('Planning replacements', sum(len(paths) - 1 for _, paths in groups),
                       sum(key[0] * (len(paths) - 1) for key, paths in groups))
        already = already_shared = 0
        for (size, digest), files in groups:
            files, canonical = order_keeper(native, files, completed, origins, root, digest, skip)
            for dst, ds, dst_clone_id in files[1:]:
                src, ss, src_clone_id = files[0]
                progress.current_path = str(dst)
                progress.count += 1
                progress.bytes += size
                progress.report()
                rel = str(dst.relative_to(root))
                if previous_exclusions.get(rel) == ds:
                    unmatched.append(rel)
                    continue
                journaled = completed.get(rel) == ds
                if src_clone_id and dst_clone_id:
                    if src_clone_id == dst_clone_id:
                        if journaled:
                            already += 1
                        else:
                            already_shared += 1
                        continue
                elif journaled:
                    already += 1
                    continue
                try:
                    estimate = reclaimable_bytes(native, dst)
                except OSError as e:
                    skip(dst, e)
                    continue
                planned.append({'source': str(src.relative_to(root)), 'destination': rel,
                                'canonical_keeper': canonical, 'size_bytes': size, 'sha256': digest,
                                'estimated_physical_bytes': estimate})
                tasks.append((src, dst, digest, ss, ds, size, canonical))
        progress.report(force=True)
        deferred = []
        if args.top is not None:
            # --top scopes the plan, so a dry run shows exactly what --apply replaces.
            progress.emit('Comparing directories', force=True)
            same_dirs = directory_groups(root, groups, {'.git', *args.exclude}, jp)
            top = select_units(planned, same_dirs, args.top)
            selected = {d for u in top for d in u['destinations']}
            deferred = [x for x in planned if x['destination'] not in selected]
            planned = [x for x in planned if x['destination'] in selected]
            tasks = [t for t in tasks if str(t[1].relative_to(root)) in selected]
        if args.apply:
            if already:
                progress.emit(f'Resuming: {already:,} unchanged completed files will be skipped.', force=True)
            progress.start('Processing clone candidates', len(tasks), sum(t[5] for t in tasks))
            batch, sizes = [], {}
            busy, inspected = native.open_files() if tasks else set(), time.monotonic()
            def publish_batch():
                nonlocal changed, changed_bytes, busy, inspected
                outcomes, busy = publish(native, journal, batch, cache=job)
                inspected = time.monotonic()
                del batch[:]
                for item, outcome in outcomes:
                    if isinstance(outcome, Skip):
                        skip(item.dst, outcome)
                    else:
                        changed += 1
                        changed_bytes += sizes[item.ident]
                        cloned.add(item.relative)
            try:
                for src, dst, digest, ss, ds, size, canonical in tasks:
                    progress.current_path = str(dst)
                    progress.checkpoint()
                    try:
                        if job:
                            job.snapshot.update(activity='Verifying clone candidate', current_file_bytes=0)
                        def clone_bytes(n):
                            if job:
                                job.snapshot['current_file_bytes'] += n
                            progress.report()
                        item = prepare(native, root, src, dst, digest, ss, ds, journal, cache=job,
                                       on_bytes=clone_bytes, busy=busy, canonical_keeper=canonical)
                        sizes[item.ident] = size
                        batch.append(item)
                    except ProvenanceMismatch as e:
                        unmatched.append(str(dst.relative_to(root)))
                        progress.emit('Preserved original: ' + str(e), force=True)
                    except Skip as e:
                        skip(dst, e)
                    # Also bounds the age of the snapshot that prepare() consults.
                    if len(batch) >= BATCH_FILES or time.monotonic() - inspected >= BATCH_SECONDS:
                        publish_batch()
                    progress.count += 1
                    progress.bytes += size
                    progress.report()
            except KeyboardInterrupt:
                # A stop request: staged clones are verified, so finish them first.
                publish_batch()
                raise
            if batch:
                publish_batch()
            progress.report(force=True)
        result = {'schema_version': 1, 'ok': True, 'root': str(root), 'dry_run': not args.apply,
                  'files_scanned': scanned, 'files_excluded': excluded,
                  'duplicate_groups': len(groups), 'planned_files': len(planned),
                  'provenance_unmatched_files': unmatched, 'skipped_files': skipped,
                  'logical_duplicate_bytes': sum(x['size_bytes'] for x in planned),
                  'estimated_physical_bytes': sum(x['estimated_physical_bytes'] for x in planned),
                  'estimated_applied_physical_bytes': sum(x['estimated_physical_bytes'] for x in planned
                                                          if x['destination'] in cloned),
                  'cloned_files': changed, 'cloned_logical_bytes': changed_bytes,
                  'already_completed_files': already, 'already_shared_files': already_shared,
                  'changes': planned,
                  'journal': str(jp),
                  'volume_free_change_bytes': free_bytes(root) - free_before,
                  'elapsed_seconds': round(time.monotonic() - start, 3)}
        if args.top is not None:
            for unit in top:
                unit['cloned_files'] = sum(d in cloned for d in unit['destinations'])
            result['top_groups'] = top
            result['deferred_files'] = len(deferred)
            result['deferred_groups'] = len({(x['size_bytes'], x['sha256']) for x in deferred})
            result['deferred_logical_bytes'] = sum(x['size_bytes'] for x in deferred)
        if job:
            if temporary_state is None:
                result['state_dir'] = str(state_dir)
            result['cache_hits'] = job.snapshot['cache_hits']
            job.finish('complete', result={k: v for k, v in result.items() if k not in ('changes', 'top_groups')})
        if args.json:
            print(json.dumps(result))
        elif not args.quiet:
            print_summary(result, args.top)
        if args.top is not None and not args.json:
            if not args.quiet:
                print()
            print_top(result['top_groups'], args.apply)
        return 0
    except (OSError, Conflict, ValueError, sqlite3.Error, KeyboardInterrupt) as e:
        interrupted = isinstance(e, KeyboardInterrupt)
        message = 'Stopped; rerun with the same journal and state directory to recover.' if interrupted else str(e)
        if job:
            job.finish('stopped' if interrupted else 'failed', error=str(e), cloned_files=changed, cloned_logical_bytes=changed_bytes)
        if args.json:
            print(json.dumps({'schema_version': 1, 'ok': False,
                              'error': 'interrupted' if interrupted else 'operation_failed',
                              'message': message, 'cloned_files': changed,
                              'cloned_logical_bytes': changed_bytes}))
        else:
            print('Error: ' + message, file=sys.stderr)
            if changed:
                print(f'{changed} files completed; rerun with the same journal.', file=sys.stderr)
        return 130 if interrupted else 1
    finally:
        if job is not None:
            job.close()
        if journal is not None:
            journal.close()
        if rootfd is not None:
            os.close(rootfd)
        if temporary_state is not None:
            temporary_state.cleanup()


if __name__ == '__main__':
    sys.exit(main())
