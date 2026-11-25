"""Black-box regression tests; writes only temporary APFS fixtures."""
import ctypes as C
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zlib

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


t = load('apfs-transfer')
d = load('apfs-dedupe')
import apfs_common as c
from apfs_job import Job, Progress, control, inspect_state


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='apfs-tools-test-')
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'
        self.dest = self.root / 'dest'
        self.state = self.root / 'state'
        self.source.mkdir()
        self.dest.mkdir()
        self.native = c.Native()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, code=0, script='apfs-transfer.py'):
        roots = [str(self.source), str(self.dest)] if script == 'apfs-transfer.py' else [str(self.source)]
        cmd = [sys.executable, str(HERE / script), *roots, '--state-dir', str(self.state), '--json', *args]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def pair(self):
        (self.source / 'a').write_bytes(b'hello\n' * 1024)
        (self.source / 'b').write_bytes((self.source / 'a').read_bytes())

    def test_transfer_metadata_duplicates_and_repeat(self):
        self.pair()
        (self.source / 'b').chmod(0o751)
        subprocess.run(['/usr/bin/xattr', '-w', 'test.transfer', 'value', str(self.source / 'b')], check=True)
        os.utime(self.source / 'b', ns=(1_600_000_000_000_000_000, 1_600_000_001_234_567_890))
        result = self.cli('--apply')
        self.assertEqual(result['files_verified'], 2)
        self.assertEqual(result['cloned_files'], 1)
        for name in ('a', 'b'):
            self.assertEqual(t.metadata(self.native, self.source / name), t.metadata(self.native, self.dest / name))
        self.assertNotEqual((self.dest / 'a').stat().st_ino, (self.dest / 'b').stat().st_ino)
        self.assertEqual(self.cli('--apply')['resumed_files'], 2)
        (self.dest / 'b').write_bytes(b'independent')
        self.assertEqual((self.dest / 'a').read_bytes(), (self.source / 'a').read_bytes())

    def test_transfer_refuses_an_unverified_hidden_resource_fork(self):
        path = self.source / 'compressed'
        content = b'inline compression' * 200
        fork = b'independent data in the resource fork'
        path.touch()
        image = struct.pack('<IIQ', 0x636d7066, 3, len(content)) + zlib.compress(content)
        self.native.set_xattr(path, c.DECMPFS_XATTR, image)
        self.native.set_xattr(path, c.RESOURCE_FORK_XATTR, fork)
        os.chflags(path, c.UF_COMPRESSED)

        result = self.cli('--apply', code=1)
        self.assertIn('unverified hidden xattrs', result['message'])
        self.assertFalse((self.dest / path.name).exists())
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.native.xattr(path, c.RESOURCE_FORK_XATTR, c.XATTR_SHOWCOMPRESSION), fork)

    def test_transfer_nested_symlinks_and_directory_metadata(self):
        sub = self.source / 'nested'
        sub.mkdir()
        (sub / 'file').write_bytes(b'data')
        (sub / 'relative').symlink_to('file')
        (sub / 'dangling').symlink_to('/missing-apfs-test-target')
        (self.source / 'dirlink').symlink_to('nested', target_is_directory=True)
        self.cli('--apply')
        for rel in ('.', 'nested', 'nested/file', 'nested/relative', 'nested/dangling', 'dirlink'):
            self.assertEqual(t.metadata(self.native, self.source / rel), t.metadata(self.native, self.dest / rel), rel)
        self.assertEqual(os.readlink(self.dest / 'nested/dangling'), '/missing-apfs-test-target')

    def test_hardlink_topology_and_resume(self):
        self.pair()
        os.link(self.source / 'a', self.source / 'c')
        self.cli('--apply')
        self.assertEqual((self.dest / 'a').stat().st_ino, (self.dest / 'c').stat().st_ino)
        self.assertNotEqual((self.dest / 'a').stat().st_ino, (self.dest / 'b').stat().st_ino)
        self.assertEqual(self.cli('--apply')['resumed_files'], 3)

    def test_dry_run_no_destination_or_persistent_state(self):
        self.pair()
        result = self.cli()
        self.assertTrue(result['dry_run'])
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertFalse(self.state.exists())

    def test_modified_source_and_destination_require_replace(self):
        self.pair()
        self.cli('--apply')
        (self.source / 'a').write_bytes(b'HELLO\n' * 1024)
        self.assertIn('Destination exists', self.cli('--apply', code=1)['message'])
        self.cli('--apply', '--replace')
        self.assertEqual((self.dest / 'a').read_bytes(), (self.source / 'a').read_bytes())
        (self.dest / 'b').write_bytes(b'changed')
        self.cli('--apply', code=1)
        self.cli('--apply', '--replace')
        self.assertEqual((self.dest / 'b').read_bytes(), (self.source / 'b').read_bytes())

    def test_symlink_destination_parent_refused(self):
        (self.source / 'sub').mkdir()
        (self.source / 'sub/file').write_bytes(b'hello')
        outside = self.root / 'outside'
        outside.mkdir()
        (self.dest / 'sub').symlink_to(outside)
        self.cli('--apply', '--replace', code=1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_same_size_mtime_change_invalidates_dedupe_cache(self):
        self.pair()
        self.cli(script='apfs-dedupe.py')
        before = (self.source / 'b').stat()
        (self.source / 'b').write_bytes(b'HELLO\n' * 1024)
        os.utime(self.source / 'b', ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.cli(script='apfs-dedupe.py')
        self.assertEqual(result['planned_files'], 0)
        self.assertGreater(result['cache_hits'], 0)

    def test_cache_resume_avoids_content_reads(self):
        self.pair()
        self.cli(script='apfs-dedupe.py')
        original = d.hash_fd
        with mock.patch.object(d, 'hash_fd', side_effect=AssertionError('unchanged contents reread')):
            from apfs_job import Job
            job = Job(self.state, {'tool': 'dedupe', 'root': str(self.source), 'volume': self.native.volume_identity(self.source)})
            try:
                for path in self.source.iterdir():
                    digest, sig = d.hash_file(path, cache=job)
                    self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            finally:
                job.close()

    def test_status_history_and_state_lock(self):
        job = Job(self.state, {'test': True})
        try:
            job.publish(force=True, phase='Testing', files_done=3, files_total=9)
            status = inspect_state(self.state)
            self.assertTrue(status['active'])
            self.assertEqual(status['files_done'], 3)
            with self.assertRaisesRegex(ValueError, 'Another run is using the state directory'):
                Job(self.state, {'test': True})
            self.assertTrue(inspect_state(self.state)['active'])
            for line in (self.state / 'events.jsonl').read_text().splitlines():
                self.assertEqual(json.loads(line)['schema_version'], 1)
        finally:
            job.close()
        self.assertEqual(inspect_state(self.state)['state'], 'interrupted')

    def test_control_without_state_explains(self):
        for script in ('apfs-transfer.py', 'apfs-dedupe.py'):
            result = self.cli('--control', 'status', code=1, script=script)
            self.assertIn('No job state in ' + str(self.state), result['message'])

    def test_locked_source_file_is_named(self):
        self.pair()
        import fcntl
        with open(self.source / 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.cli('--apply', code=1)
        self.assertIn('Locked by another process', result['message'])
        self.assertIn(str(self.source / 'a'), result['message'])
        self.assertEqual(self.cli('--apply')['copied_files'], 2)

    def test_events_history_rotates(self):
        import apfs_job
        with mock.patch.object(apfs_job, 'EVENTS_ROTATE_BYTES', 2000):
            with Job(self.state, {'test': True}) as job:
                for n in range(12):
                    job.publish(force=True, files_done=n)
        self.assertTrue((self.state / 'events.jsonl.1').exists())
        self.assertLess((self.state / 'events.jsonl').stat().st_size, 4000)
        self.assertEqual(json.loads((self.state / 'status.json').read_text())['files_done'], 11)

    def test_hangup_handling_respects_nohup(self):
        default = signal.getsignal(signal.SIGHUP)
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            with Job(self.state, {'test': True}) as job:
                self.assertEqual(signal.getsignal(signal.SIGHUP), job._signal)
            self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)   # as set by nohup
            with Job(self.state, {'test': True}):
                self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGHUP, default)

    def test_failed_start_releases_signals_and_lock(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        with Job(self.state, {'test': True}):
            pass
        with self.assertRaisesRegex(ValueError, 'another operation'):
            Job(self.state, {'test': 'different'})
        self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)
        Job(self.state, {'test': True}).close()

    def test_cooperative_pause_resume_stop_process_boundary(self):
        worker = '''import sys,time
from pathlib import Path
from apfs_job import Job
j=Job(Path(sys.argv[1]), {'worker':True})
try:
 for n in range(10000):
  j.checkpoint();j.publish(force=True, files_done=n);time.sleep(.01)
except KeyboardInterrupt:
 j.finish('stopped')
finally:
 j.close()
'''
        proc = subprocess.Popen([sys.executable, '-c', worker, str(self.state)], cwd=HERE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def wait_for(predicate):
            end = time.monotonic() + 10
            while time.monotonic() < end:
                if (self.state / 'status.json').exists():
                    value = inspect_state(self.state)
                    if predicate(value):
                        return value
                time.sleep(.02)
            self.fail('Control acknowledgement timed out')
        try:
            wait_for(lambda x: x['files_done'] > 2)
            control(self.state, 'pause')
            paused = wait_for(lambda x: x['state'] == 'paused')
            time.sleep(.3)
            self.assertEqual(inspect_state(self.state)['files_done'], paused['files_done'])
            control(self.state, 'resume')
            wait_for(lambda x: x['files_done'] > paused['files_done'])
            control(self.state, 'stop')
            proc.communicate(timeout=10)
            self.assertEqual(inspect_state(self.state)['state'], 'stopped')
            self.assertFalse(inspect_state(self.state)['active'])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_recover_published_transfer_checkpoint(self):
        self.pair()
        self.cli('--apply')
        db = sqlite3.connect(self.state / 'checkpoints.sqlite3')
        row = db.execute("SELECT signature,value FROM cache WHERE kind='done' AND path='a'").fetchone()
        source_sig, dest_sig = json.loads(row[0])
        pending = {'temporary': PREFIX + 'already-published', 'source_signature': source_sig,
                   'prepared': dest_sig, 'result': json.loads(row[1]),
                   'metadata_digest': t.metadata_digest(t.metadata(self.native, self.dest / 'a'))}
        db.execute("DELETE FROM cache WHERE kind='done' AND path='a'")
        db.execute('INSERT INTO cache VALUES (?,?,?,?)', ('pending', 'a', '[]', json.dumps(pending)))
        db.commit()
        db.close()
        inode = (self.dest / 'a').stat().st_ino
        self.assertEqual(self.cli('--apply')['resumed_files'], 2)
        self.assertEqual((self.dest / 'a').stat().st_ino, inode)

    def test_compressed_transfer(self):
        import struct, zlib
        self.pair()
        a = self.source / 'a'
        content = a.read_bytes()
        a.write_bytes(b'')
        value = struct.pack('<IIQ', 0x636d7066, 3, len(content)) + zlib.compress(content)
        self.native.set_xattr(a,'com.apple.decmpfs',value)
        os.chflags(a, c.UF_COMPRESSED)
        self.cli('--apply')
        self.assertEqual((self.dest / 'a').read_bytes(), content)
        self.assertEqual((self.dest / 'b').read_bytes(), content)
        self.assertTrue((self.dest / 'a').stat().st_flags & c.UF_COMPRESSED)
        self.assertEqual(t.metadata(self.native, a), t.metadata(self.native, self.dest / 'a'))

    def test_transfer_keeps_compact_source_when_larger_duplicate_came_first(self):
        import struct, zlib
        self.pair()
        a = self.source / 'b'
        content = a.read_bytes()
        a.write_bytes(b'')
        value = struct.pack('<IIQ', 0x636d7066, 3, len(content)) + zlib.compress(content)
        self.native.set_xattr(a,'com.apple.decmpfs',value)
        os.chflags(a, c.UF_COMPRESSED)
        self.cli('--apply')
        self.assertEqual((self.dest / 'a').read_bytes(), content)
        self.assertEqual((self.dest / 'b').read_bytes(), content)
        self.assertTrue((self.dest / 'b').stat().st_flags & c.UF_COMPRESSED)
        self.assertEqual(t.metadata(self.native, a), t.metadata(self.native, self.dest / 'b'))

    def test_transfer_interrupted_staging_recovers(self):
        self.pair()
        args = type('Args', (), dict(apply=True, quiet=True, exclude=[], exclude_system=False, replace=False, verify_all=False))()
        original = t.copyfile
        def interrupted(*a, **kw):
            original(*a, **kw)
            raise KeyboardInterrupt('test interruption after staging')
        with mock.patch.object(t, 'copyfile', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                t.run(args, self.source, self.dest, self.state)
        self.assertTrue(list(self.dest.glob(t.PREFIX + '*')))
        self.assertEqual(inspect_state(self.state)['state'], 'stopped')
        self.cli('--apply')
        self.assertFalse(list(self.dest.glob(t.PREFIX + '*')))
        self.assertEqual((self.source / 'a').read_bytes(), (self.dest / 'a').read_bytes())

    def test_transfer_rejects_destination_created_at_publication(self):
        name = 'a'
        (self.source / name).write_bytes(b'copy')
        destination = self.dest / name
        args = type('Args', (), dict(apply=True, quiet=True, exclude=[], exclude_system=False,
                                     replace=False, verify_all=False))()
        rename = c.Native.rename_exclusive

        def publish(native, stage, path):
            path.write_bytes(b'created by another writer')
            rename(native, stage, path)

        with mock.patch.object(c.Native, 'rename_exclusive', publish), \
             self.assertRaisesRegex(c.Conflict, 'Destination appeared'):
            t.run(args, self.source, self.dest, self.state)
        self.assertEqual(destination.read_bytes(), b'created by another writer')
        self.assertIn('Destination exists', self.cli('--apply', code=1)['message'])
        self.assertFalse(list(self.dest.glob(t.PREFIX + '*')))
        self.assertEqual(destination.read_bytes(), b'created by another writer')

    def test_copy_and_clone_stage_remove_inherited_acl(self):
        self.pair()
        self.dest.chmod(0o777)
        subprocess.run(['/bin/chmod', '+a', 'everyone allow list,search,directory_inherit',
                        str(self.dest)], check=True)
        args = type('Args', (), dict(apply=True, quiet=True, exclude=[], exclude_system=False,
                                     replace=False, verify_all=False))()
        real_copy = t.copyfile
        real_clone = c.Native.clone
        observed = []

        def check_stage(native, payload):
            self.assertEqual(payload.parent.lstat().st_mode & 0o777, 0o700)
            self.assertEqual(native.acl(payload.parent), c.EMPTY_ACL)
            observed.append(payload)

        def copy(native, source, payload, **kwargs):
            if Path(payload).name == c.STAGE_PAYLOAD:
                check_stage(native, payload)
            real_copy(native, source, payload, **kwargs)

        def clone(native, source, payload):
            check_stage(native, payload)
            real_clone(native, source, payload)

        with mock.patch.object(t, 'copyfile', copy), mock.patch.object(c.Native, 'clone', clone):
            self.assertEqual(t.run(args, self.source, self.dest, self.state)['cloned_files'], 1)
        self.assertEqual(len(observed), 2)
        self.assertFalse(list(self.dest.glob(t.PREFIX + '*')))

    def test_physical_copy_callback_reports_bytes(self):
        src, dst = self.source / 'a', self.dest / 'staged'
        src.write_bytes(os.urandom(3 * 1024 * 1024))
        job = Job(self.state, {'test': 'copy-callback'})
        try:
            progress = Progress(True, job)
            # clone=False forces the byte-copy path although both fixtures share a volume.
            t.copyfile(self.native, src, dst, progress=progress, clone=False)
            self.assertEqual(job.snapshot['current_file_bytes'], src.stat().st_size)
            self.assertEqual(src.read_bytes(), dst.read_bytes())
            self.assertFalse((self.native.sharing(dst).ext_flags or 0) & c.EF_SHARES_ALL_BLOCKS)
        finally:
            job.close()

    def test_dedupe_reuses_keeper_hash(self):
        self.pair()
        (self.source / 'c').write_bytes((self.source / 'a').read_bytes())
        args = [str(self.source), '--apply', '--json', '--quiet', '--state-dir', str(self.state)]
        original = d.hash_fd
        calls = []
        def count(fd, on_bytes=None):
            calls.append(os.fstat(fd).st_size)
            return original(fd, on_bytes)
        import contextlib, io
        with mock.patch.object(d, 'hash_fd', count), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(args), 0)
        self.assertEqual(len(calls), 7)  # 3 scan + 2 destinations + 2 staged; no keeper rereads.
        calls.clear()
        with mock.patch.object(d, 'hash_fd', count), mock.patch.object(d, 'journal_records', side_effect=AssertionError('unchanged journal replayed')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(args), 0)
        self.assertEqual(calls, [])

    def test_dedupe_cli_stop_and_restart(self):
        data = b'restart-safe' * 256
        for i in range(1200):
            (self.source / f'{i:04}').write_bytes(data)
        command = [sys.executable, str(HERE / 'apfs-dedupe.py'), str(self.source),
                   '--apply', '--json', '--state-dir', str(self.state)]
        proc = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            end = time.monotonic() + 10
            while not (self.state / 'status.json').exists() and time.monotonic() < end:
                time.sleep(.01)
            self.cli('--control', 'stop', script='apfs-dedupe.py')
            out, _ = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 130, out)
            first = json.loads(out)['cloned_files']
            self.assertEqual(inspect_state(self.state)['state'], 'stopped')
            result = self.cli('--apply', script='apfs-dedupe.py')
            self.assertEqual(result['cloned_files'] + first, 1199)
            self.assertEqual(self.cli('--control', 'status', script='apfs-dedupe.py')['state'], 'complete')
            self.assertFalse(list(self.source.glob(d.PREFIX + '*')))
            self.assertEqual((self.source / '0000').read_bytes(), data)
            self.assertEqual((self.source / '1199').read_bytes(), data)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_transfer_verify_all_rereads_completed_files(self):
        self.pair()
        self.cli('--apply')
        result = self.cli('--apply', '--verify-all')
        self.assertEqual(result['resumed_files'], 2)
        self.assertGreater(inspect_state(self.state)['io_bytes_hashed'], 0)

    def test_help_and_usage_contracts(self):
        for script in ('apfs-transfer.py', 'apfs-dedupe.py'):
            for flag in ([], ['--help']):
                result = subprocess.run([sys.executable, str(HERE / script), *flag], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0)
                self.assertIn('--control', result.stdout)
                self.assertEqual(result.stderr, '')

    def test_resume_after_device_numbers_change(self):
        self.pair()
        os.link(self.source / 'a', self.source / 'linked')
        self.assertEqual(self.cli('--apply')['copied_files'], 3)
        db = sqlite3.connect(self.state / 'checkpoints.sqlite3')
        # Checkpoints as an earlier attach order would have recorded them.
        for kind, path, raw in db.execute("SELECT kind,path,signature FROM cache WHERE kind IN "
                                          "('done','sha256','destination-sha256')").fetchall():
            recorded = json.loads(raw)
            recorded = ([[x[0] + 7, *x[1:]] for x in recorded] if kind == 'done'
                        else [recorded[0] + 7, *recorded[1:]])
            db.execute('UPDATE cache SET signature=? WHERE kind=? AND path=?', (json.dumps(recorded), kind, path))
        db.execute("UPDATE cache SET value='[1, 2]' WHERE kind='devices'")
        db.commit()
        db.close()
        result = self.cli('--apply')
        self.assertEqual((result['copied_files'], result['resumed_files']), (0, 3))

    def test_dedupe_journal_is_not_transferred(self):
        self.pair()
        (self.source / '.apfs-dedupe.jsonl.gz').write_bytes(b'journal of the source root')
        self.cli('--apply')
        self.assertFalse((self.dest / '.apfs-dedupe.jsonl.gz').exists())
        self.assertTrue((self.dest / 'a').exists())

    def test_new_destination_directories_are_private_until_restored(self):
        private = self.source / 'private'
        private.mkdir()
        (private / 'f').write_bytes(b'x' * 100)
        private.chmod(0o755)
        args = type('Args', (), dict(apply=True, quiet=True, exclude=[], exclude_system=False,
                                     replace=False, verify_all=False))()
        real, seen = t.Transfer.copy_files, []

        def copy_files(transfer):
            seen.append((self.dest / 'private').stat().st_mode & 0o777)
            real(transfer)

        with mock.patch.object(t.Transfer, 'copy_files', copy_files):
            t.run(args, self.source, self.dest, self.state)
        self.assertEqual(seen, [0o700])
        self.assertEqual((self.dest / 'private').stat().st_mode & 0o777, 0o755)

    def test_switching_journals_invalidates_the_shared_index(self):
        self.pair()
        one, two = self.root / 'one.gz', self.root / 'two.gz'
        for journal, expected in ((one, (1, 0, 0)), (two, (0, 0, 1)), (one, (0, 1, 0))):
            result = self.cli('--apply', '--journal', str(journal), script='apfs-dedupe.py')
            counts = tuple(result[key] for key in ('cloned_files', 'already_completed_files',
                                                    'already_shared_files'))
            self.assertEqual(counts, expected)


PREFIX = t.PREFIX
if __name__ == '__main__':
    unittest.main(verbosity=2)
