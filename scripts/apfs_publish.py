"""Recoverable APFS exchange of a verified staged compression copy."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from apfs_common import (STAGE_PAYLOAD, Conflict, atomic_write, file_checkpoint,
                         lstat_or_none, make_private_stage, matches_checkpoint, on_device,
                         private_stage_directory, remove_private_stage, signature, sync_dir, sync_file)

PUBLICATION_PREFIX = '.apfs-compress-plus-swap-'
JOURNAL = 'publication.json'
# atomic_write leaves this beside the journal if the process dies during a write.
JOURNAL_TEMPORARY = re.compile(re.escape(JOURNAL) + r'\.[0-9a-f]{32}\.tmp')
F_GETPATH = 50  # Darwin <sys/fcntl.h>


class PublicationConflict(RuntimeError):
    """A staged publication requires recovery or has two versions to reconcile."""


def named_publication_directory(path):
    key = hashlib.sha256(os.fsencode(path.name)).hexdigest()
    return path.parent / (PUBLICATION_PREFIX + key)


def canonical_file_name(path):
    """Get the stored spelling so a case or Unicode alias finds the same stage."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return path.name
    try:
        canonical = fcntl.fcntl(fd, F_GETPATH, bytes(1024)).split(b'\0', 1)[0]
        return Path(os.fsdecode(canonical)).name or path.name
    finally:
        os.close(fd)


def publication_directory(path):
    return named_publication_directory(path.with_name(canonical_file_name(path)))


@contextlib.contextmanager
def locked_stage(native, directory, identity):
    anchor = private_stage_directory(native, directory, identity)
    fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PublicationConflict(f'Compression stage is in use: {directory}') from error
        yield anchor
    finally:
        os.close(fd)


def volume_uuid(native, path):
    """UUID of the APFS volume holding path, or None where it cannot be read."""
    try:
        return native.volume_identity(path)['uuid']
    except (OSError, ValueError):
        return None


def journal_temporaries(anchor):
    """Interrupted journal writes in a stage; they never authorized an exchange."""
    found = []
    for entry in anchor.iterdir():
        if JOURNAL_TEMPORARY.fullmatch(entry.name):
            status = entry.lstat()
            if stat.S_ISREG(status.st_mode) and status.st_uid == os.geteuid() and status.st_nlink == 1:
                found.append(entry)
    return found


def clear_journal_temporaries(anchor):
    removed = journal_temporaries(anchor)
    for entry in removed:
        entry.unlink(missing_ok=True)
    if removed:
        sync_dir(anchor)


class Publication:
    def __init__(self, native, path, directory, identity, anchor):
        self.native = native
        self.path, self.directory, self.identity = path, directory, identity
        self.payload = anchor / STAGE_PAYLOAD
        self.journal = anchor / JOURNAL
        self.record = dict(version=1, name=path.name, directory=identity)
        self.prepared = False

    def save(self):
        atomic_write(self.journal, json.dumps(self.record) + '\n', durable=True)
        sync_dir(self.journal.parent)

    def remove(self):
        self.payload.unlink(missing_ok=True)
        sync_dir(self.payload.parent)
        self.journal.unlink(missing_ok=True)
        sync_dir(self.journal.parent)
        clear_journal_temporaries(self.journal.parent)
        remove_private_stage(self.native, self.directory / STAGE_PAYLOAD, self.identity)

    def in_use(self, expected):
        return tuple(expected['signature'][:2]) in self.native.open_file_users(mappings=False)

    def exchange(self):
        private_stage_directory(self.native, self.directory, self.identity)
        self.native.swap(self.payload, self.path)
        sync_dir(self.path.parent)
        sync_dir(self.payload.parent)

    def conflict(self):
        return PublicationConflict(
            f'Publication could not be settled. Review both {self.path} and '
            f'{self.directory / STAGE_PAYLOAD}'
        )

    def restore(self, dry_run=False):
        """Keep the user version, or retain both files when either may be newer."""
        if lstat_or_none(self.payload) is None:
            return
        original, replacement = self.record['original'], self.record['replacement']
        dst = lstat_or_none(self.path)
        if dst is not None and signature(dst)[:2] == replacement['signature'][:2]:
            candidate = self.payload.lstat()
            if (signature(candidate)[:2] != original['signature'][:2]
                    or not matches_checkpoint(self.native, self.path, replacement)
                    or self.in_use(replacement)):
                raise self.conflict()
            if dry_run:
                return
            self.exchange()
        # The stage must now hold the unmodified staged copy, which is
        # disposable whatever the path names: the original, a version saved
        # later under a new inode, or nothing. Anything else in the stage may
        # be the only copy of the original, so both are kept.
        staged = lstat_or_none(self.payload)
        if (staged is None or signature(staged)[:2] != replacement['signature'][:2]
                or not matches_checkpoint(self.native, self.payload, replacement)
                or self.in_use(replacement)):
            raise self.conflict()

    def publish(self, expected):
        try:
            original = file_checkpoint(self.native, self.path)
            replacement = file_checkpoint(self.native, self.payload)
        except Conflict as error:
            raise RuntimeError(str(error)) from error
        if original['signature'] != expected:
            raise RuntimeError('Source changed before atomic replacement')
        if original['metadata'] != replacement['metadata']:
            raise RuntimeError('Staged copy does not preserve source metadata')
        sync_file(self.payload)
        self.record.update(original=original, replacement=replacement)
        self.save()
        if (signature(self.path.lstat()) != expected or self.in_use(original)
                or signature(self.payload.lstat()) != replacement['signature']):
            raise RuntimeError('File changed or opened before atomic replacement')
        # A durable pending record is enough. With both inodes present, reruns
        # conservatively restore the original. With no payload, the exchange
        # was either confirmed and cleaned up or already reconciled.
        self.prepared = True
        try:
            self.exchange()
            if not self.in_use(original) and matches_checkpoint(self.native, self.payload, original):
                self.prepared = False
                return
            self.restore()
        except (OSError, Conflict) as error:
            raise self.conflict() from error
        self.prepared = False
        raise RuntimeError('Source changed or opened during replacement. Original restored.')


@contextlib.contextmanager
def staged_replacement(native, path):
    path = path.with_name(canonical_file_name(path))
    directory = named_publication_directory(path)
    try:
        identity = make_private_stage(native, directory)
    except FileExistsError as error:
        raise PublicationConflict(
            f'Compression stage already exists: {directory}. '
            'Use file repair --only interrupted_swap to recover an interrupted exchange.'
        ) from error
    with locked_stage(native, directory, identity) as anchor:
        publication = Publication(native, path, directory, identity, anchor)
        publication.record['volume'] = volume_uuid(native, anchor)
        try:
            publication.save()
            yield publication
        finally:
            if not publication.prepared:
                try:
                    publication.remove()
                except (OSError, Conflict) as error:
                    raise PublicationConflict(f'Could not clear compression stage: {directory}') from error


def recover_publication(native, directory, dry_run=False):
    st = directory.lstat()
    identity = [st.st_dev, st.st_ino]
    with locked_stage(native, directory, identity) as anchor:
        marker = anchor / JOURNAL
        interrupted = set(journal_temporaries(anchor))
        if not dry_run:
            clear_journal_temporaries(anchor)
        if lstat_or_none(marker) is None:
            # A crash can occur just after mkdir or during the first journal
            # write. Unknown contents require review.
            if any(entry not in interrupted for entry in anchor.iterdir()):
                raise PublicationConflict(f'Staging files have no recovery journal: {directory}')
            if not dry_run:
                remove_private_stage(native, directory / STAGE_PAYLOAD, identity)
            return directory
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
                raise PublicationConflict(f'Unsafe publication journal: {directory}')
            record = json.load(stream)
        recorded = record['directory']
        if (recorded != identity and isinstance(recorded, list) and len(recorded) == 2
                and recorded[1] == identity[1] and record.get('volume')
                and record['volume'] == volume_uuid(native, anchor)):
            # Same volume and inode: the device number changed with a remount.
            record['directory'] = identity
            for side in ('original', 'replacement'):
                if side in record:
                    record[side]['signature'] = on_device(record[side]['signature'], identity[0])
        name = record['name']
        path = directory.parent / name
        if (record['version'] != 1 or record['directory'] != identity
                or name in ('', '.', '..') or Path(name).name != name
                or named_publication_directory(path) != directory
                or ('original' in record) != ('replacement' in record)):
            raise PublicationConflict(f'Invalid publication journal: {directory}')
        publication = Publication(native, path, directory, identity, anchor)
        publication.record = record
        if 'original' in record:
            publication.restore(dry_run)
        if not dry_run:
            publication.remove()
        return path
