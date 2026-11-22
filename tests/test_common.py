"""Direct tests of scripts/apfs_common.py on temporary APFS fixtures. Python 3.9+."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apfs_common as c


class CommonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='apfs-common-test-')
        self.addCleanup(self.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.native = c.Native()

    def cleanup(self):
        c.force_rmtree(self.root)
        self.tmp.cleanup()

    def file(self, name, data=b'x' * 70000):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_mount_and_capabilities_describe_the_volume(self):
        mount = self.native.mount(self.root)
        self.assertEqual(mount.fstype, 'apfs')
        self.assertTrue(mount.device.startswith('/dev/disk'))
        self.assertTrue(str(self.root).startswith(mount.point) or mount.point == '/System/Volumes/Data')
        fmt, interfaces = self.native.capabilities(self.root)
        self.assertTrue(interfaces['clone'] and interfaces['rename_swap'])
        self.assertIn('case_sensitive', fmt)
        self.assertEqual(self.native.volume_identity(self.root)['root_inode'], self.root.stat().st_ino)
        with self.assertRaisesRegex(ValueError, 'APFS required'):
            self.native.volume_identity('/dev')

    def test_clone_shares_one_data_stream_until_written(self):
        a = self.file('a')
        b = self.root / 'b'
        self.native.clone(a, b)
        sa, sb = self.native.sharing(a), self.native.sharing(b)
        self.assertEqual(sa.clone_id, sb.clone_id)
        self.assertEqual((sa.refcnt, sa.private), (2, 0))
        self.assertTrue(sb.ext_flags & c.EF_SHARES_ALL_BLOCKS)
        self.assertEqual(sb[1], sb.clone_id)   # positional access stays valid
        with a.open('r+b') as out:
            out.write(b'y')
        self.assertNotEqual(self.native.sharing(a).clone_id, self.native.sharing(b).clone_id)
        with self.assertRaises(FileExistsError) as raised:
            self.native.clone(a, b)
        self.assertEqual(raised.exception.filename, str(a))

    def test_swap_exchanges_names(self):
        a, b = self.file('a', b'first'), self.file('b', b'second')
        inode = a.stat().st_ino
        self.native.swap(a, b)
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b'second', b'first'))
        self.assertEqual(b.stat().st_ino, inode)

    def test_copyfile_reports_bytes_and_propagates_an_abort(self):
        src = self.file('src', os.urandom(3 << 20))
        seen = []
        flags = c.COPYFILE_ALL | c.COPYFILE_NOFOLLOW
        self.native.copyfile(src, self.root / 'copy', flags, seen.append)
        self.assertEqual(max(n for n in seen if n is not None), src.stat().st_size)
        self.assertEqual((self.root / 'copy').read_bytes(), src.read_bytes())

        def abort(copied):
            raise KeyboardInterrupt('stop')
        with self.assertRaises(KeyboardInterrupt):
            self.native.copyfile(src, self.root / 'aborted', flags, abort)
        with self.assertRaises(FileNotFoundError):
            self.native.copyfile(self.root / 'missing', self.root / 'x', flags)

    def test_extended_attributes_round_trip(self):
        path = self.file('f')
        self.assertIsNone(self.native.xattr(path, 'test.absent'))
        self.native.set_xattr(path, 'test.one', b'1')
        self.native.set_xattr(path, b'test.empty', b'')
        self.assertEqual(self.native.xattr(path, 'test.one'), b'1')
        self.assertEqual(self.native.xattr(path, 'test.empty'), b'')
        names = self.native.xattr_names(path)
        self.assertTrue({b'test.one', b'test.empty'} <= set(names))
        self.assertEqual(self.native.xattrs(path)[b'test.one'], b'1')
        self.native.remove_xattr(path, 'test.one')
        self.assertNotIn(b'test.one', self.native.xattr_names(path))
        with self.assertRaises(FileNotFoundError):
            self.native.xattr_names(self.root / 'missing')

    def test_metadata_is_restored_on_another_file(self):
        a, b = self.file('a'), self.file('b')
        a.chmod(0o640)
        os.utime(a, ns=(1_600_000_000_000_000_000, 1_600_000_001_000_000_000))
        self.native.set_xattr(a, 'test.keep', b'value')
        self.native.set_xattr(b, 'test.drop', b'value')
        wanted = self.native.metadata(a)
        self.native.restore(b, wanted, a.stat().st_atime_ns)
        self.assertEqual(self.native.metadata(b), wanted)

    def test_codec_comes_from_the_decmpfs_header(self):
        plain = self.file('plain')
        self.assertIsNone(self.native.decmpfs(plain))
        self.assertIsNone(self.native.codec(plain))
        self.assertIsNone(self.native.codec(self.root / 'missing'))
        compressed = self.file('data.txt', b'alpha,beta,gamma\n' * 20000)
        made = subprocess.run(['/usr/bin/ditto', '--hfsCompression', '--noclone', str(compressed),
                               str(self.root / 'packed.txt')], capture_output=True)
        packed = self.root / 'packed.txt'
        if made.returncode or not packed.stat().st_flags & c.UF_COMPRESSED:
            self.skipTest('ditto did not compress the fixture')
        kind, size = self.native.decmpfs(packed)
        self.assertEqual(size, compressed.stat().st_size)
        self.assertEqual(self.native.codec(packed), c.DECMPFS_CODECS[kind])

    def test_open_files_reports_access_by_inode(self):
        held, mapped = self.file('held'), self.file('mapped')
        child = subprocess.Popen(
            [sys.executable, '-c',
             'import mmap, sys, time\n'
             'w = open(sys.argv[1], "r+b")\n'
             'm = open(sys.argv[2], "rb"); view = mmap.mmap(m.fileno(), 0, prot=mmap.PROT_READ)\n'
             'print("ready", flush=True); time.sleep(60)', str(held), str(mapped)],
            stdout=subprocess.PIPE, text=True)
        try:
            child.stdout.readline()
            users = self.native.open_file_users()
            plain = self.native.open_files(mappings=False)
            name = self.native.process_name(child.pid)
        finally:
            child.kill()
            child.wait()
            child.stdout.close()
        key = lambda p: (p.stat().st_dev, p.stat().st_ino)
        self.assertIn(c.OpenUse(child.pid, 'u'), users[key(held)])
        self.assertIn(c.OpenUse(child.pid, 'mapped'), users[key(mapped)])
        self.assertIn(key(held), plain)
        self.assertTrue(name)
        self.assertIsNone(self.native.process_name(child.pid))   # gone
        self.assertNotIn(os.getpid(), [use.pid for uses in users.values() for use in uses])

    def test_exclude_from_backups_sets_the_time_machine_attribute(self):
        d = self.root / 'store'
        d.mkdir()
        self.native.exclude_from_backups(d)
        self.assertIn(b'com.apple.backupd', self.native.xattr(d, c.BACKUP_EXCLUDE_XATTR))

    def test_atomic_write_replaces_and_cleans_up(self):
        target = self.root / 'status.json'
        c.atomic_json(target, {'n': 1})
        c.atomic_write(target, '{"n": 2}\n', durable=True)
        self.assertEqual(target.read_text(), '{"n": 2}\n')
        c.atomic_write(self.root / 'raw.bin', b'\x00\x01')
        with self.assertRaises(TypeError):
            c.atomic_write(target, 12345)
        self.assertEqual(target.read_text(), '{"n": 2}\n')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['raw.bin', 'status.json'])

    def test_force_rmtree_removes_locked_trees(self):
        tree = self.root / 'tree'
        (tree / 'ro').mkdir(parents=True)
        (tree / 'ro' / 'f').write_text('x')
        (tree / 'locked').write_text('y')
        os.chmod(tree / 'ro', 0o555)
        os.chflags(tree / 'locked', stat.UF_IMMUTABLE)
        link = self.root / 'link'
        link.symlink_to(tree)
        c.force_rmtree(link)
        self.assertTrue(tree.exists())
        c.force_rmtree(tree)
        self.assertFalse(tree.exists())
        c.force_rmtree(tree)   # absent: no error

    def test_safe_path_rejects_escapes_and_symlinked_parents(self):
        (self.root / 'd').mkdir()
        self.assertEqual(c.safe_path(self.root, 'd/f'), self.root / 'd' / 'f')
        for bad in ('../x', '/etc/passwd', 'd/../../x'):
            with self.assertRaises(c.Conflict):
                c.safe_path(self.root, bad)
        (self.root / 'via').symlink_to(self.root / 'd')
        with self.assertRaises(c.Conflict):
            c.safe_path(self.root, 'via/f')

    def test_signature_and_helpers(self):
        path = self.file('f')
        first = c.signature(path.lstat())
        os.rename(path, self.root / 'g')
        second = c.signature((self.root / 'g').lstat())
        self.assertTrue(c.unchanged(second, first))
        (self.root / 'g').chmod(0o600)
        self.assertFalse(c.unchanged(c.signature((self.root / 'g').lstat()), first))
        self.assertIsNone(c.lstat_or_none(path))
        self.assertEqual(c.existing_ancestor(self.root / 'a' / 'b' / 'c'), str(self.root))
        self.assertGreater(c.free_bytes(self.root), 0)

    def test_human_bytes_formats(self):
        self.assertEqual(c.human_bytes(0), '0 B')
        self.assertEqual(c.human_bytes(1536), '1.5 KiB')
        self.assertEqual(c.human_bytes(5 << 40), '5.0 TiB')
        self.assertEqual(c.human_bytes(4096, compact=True), '4.0K')
        self.assertEqual(c.human_bytes(None), '-')


if __name__ == '__main__':
    unittest.main()
