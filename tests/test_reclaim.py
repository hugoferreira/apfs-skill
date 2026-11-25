"""End-to-end checks on disposable APFS trees."""
import fcntl
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from apfs_common import Native


class ReclaimTests(unittest.TestCase):
    def run_reclaim(self, *args, code=0):
        run = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "apfs-reclaim.py"), "--json", *map(str, args)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(run.returncode, code, run.stderr + run.stdout)
        return json.loads(run.stdout)

    def test_preview_apply_and_retry(self):
        native = Native()
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            root = scratch / "tree"
            root.mkdir()
            state = scratch / "state"
            cold, recent, damaged = (root / name for name in ("cold", "recent", "damaged"))
            content = b"preview and apply should preserve file content\n" * 40_000
            cold.write_bytes(content)
            recent.write_bytes(content)
            past = time.time() - 365 * 86400
            os.utime(cold, (past, past))
            native.set_xattr(recent, "org.example.apfs", b"value")
            damaged.touch()
            recoverable = b"old compressed file"
            image = struct.pack("<IIQ", 0x636d7066, 3, len(recoverable)) + zlib.compress(recoverable)
            native.set_xattr(damaged, "com.apple.decmpfs", image)
            metadata = native.metadata(recent)
            inode = cold.stat().st_ino

            preview = self.run_reclaim(root)
            self.assertEqual(preview["before"]["missing_compression_flag_files"], 1)
            self.assertEqual(preview["compression"]["files_suggested"], 1)
            self.assertEqual(preview["dedupe"]["planned_files"], 1)
            self.assertEqual(cold.stat().st_ino, inode)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["cold", "damaged", "recent"])

            applied = self.run_reclaim(root, "--apply", "--state-dir", state)
            self.assertEqual(applied["compression"]["files_compressed"], 1)
            self.assertEqual(applied["dedupe"]["cloned_files"], 1)
            self.assertEqual(applied["after"]["missing_compression_flag_files"], 1)
            self.assertLess(applied["after"]["estimated_physical"], applied["before"]["estimated_physical"])
            self.assertEqual(native.metadata(recent), metadata)
            self.assertEqual(cold.read_bytes(), content)
            self.assertEqual(recent.read_bytes(), content)
            self.assertEqual(native.codec(recent), "lzvn")
            self.assertEqual(native.sharing(cold).clone_id, native.sharing(recent).clone_id)
            self.assertTrue((Path(applied["reports"]) / "dedupe.json").is_file())

            retried = self.run_reclaim(root, "--apply", "--state-dir", state)
            self.assertEqual(retried["compression"]["files_compressed"], 0)
            self.assertEqual(retried["dedupe"]["cloned_files"], 0)
            self.assertEqual(retried["dedupe"]["already_completed_files"], 1)

    def test_reject_state_inside_tree_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            result = self.run_reclaim(root, "--apply", "--state-dir", state, code=2)
            self.assertEqual(result["error"], "invalid_arguments")
            self.assertFalse(state.exists())

    def test_conflicting_root_lock_prevents_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            root = scratch / "tree"
            root.mkdir()
            handle = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
                result = self.run_reclaim(root, "--apply", "--state-dir", scratch / "state", code=1)
            finally:
                os.close(handle)
            self.assertEqual(result["error"], "reclaim_failed")
            self.assertFalse((root / ".apfs-dedupe.jsonl.gz").exists())


if __name__ == "__main__":
    unittest.main()
