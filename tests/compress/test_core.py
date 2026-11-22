import argparse
import os
import queue
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import apfs_compress_plus as app


class FakeBar:
    def __init__(self):
        self.updates = 0

    def update(self, count=1):
        self.updates += count


class FakeCache:
    def __init__(self):
        self.marks = []
        self.methods = []

    def seen(self, path, stat_result, policy):
        return False

    def mark(self, path, stat_result, policy, outcome, method=None):
        self.marks.append((path, stat_result, policy, outcome))
        self.methods.append(method)

    def begin_recompression(self, path, old_algorithm, new_algorithm):
        pass

    def finish_recompression(self, path):
        pass


class FixedSampler(app.Sampler):
    def __init__(self, value):
        self.value = value

    def ratio(self, path):
        return self.value


class ProbeSampler(app.Sampler):
    def __init__(self, ratios):
        self.ratios = ratios
        self.requested = []

    def probe(self, path, algorithms):
        self.requested = algorithms
        return {
            algorithm: app.ProbeResult(
                algorithm, self.ratios[algorithm], 0.01, 1024, 0.5
            )
            for algorithm in algorithms
        }


class CoreTests(unittest.TestCase):
    def setUp(self):
        # Make sure that unit tests do not list the real open files of the
        # machine.
        patcher = mock.patch.object(app.OPEN_FILES, "busy", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_allocated_size_uses_512_byte_stat_blocks(self):
        st = SimpleNamespace(st_blocks=7, st_size=99_999)
        self.assertEqual(app.allocated_size(st), 3584)

    def test_estimated_size_is_block_rounded_and_never_inflates(self):
        self.assertEqual(app.estimated_allocated_size(10_000, 12_288, 0.5), 8192)
        self.assertEqual(app.estimated_allocated_size(10_000, 4096, 0.9), 4096)

    def test_compression_state_comes_from_the_file_flag(self):
        path = mock.Mock()
        path.stat.return_value = SimpleNamespace(
            st_flags=getattr(app.stat, "UF_COMPRESSED", 0x20)
        )
        self.assertTrue(app.already_compressed(path))
        path.stat.return_value = SimpleNamespace(st_flags=0)
        self.assertFalse(app.already_compressed(path))

    def test_files_with_stale_metadata_never_reach_the_compressor(self):
        header = struct.pack("<IIQ", 0x636d7066, 8, 4096).hex()
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory, "stale.txt")
            forked = Path(directory, "forked.txt")
            clean = Path(directory, "clean.txt")
            for path in (stale, forked, clean):
                path.write_bytes(b"text" * 1024)
            app.subprocess.run(["xattr", "-wx", "com.apple.decmpfs", header,
                                str(stale)], check=True)
            app.subprocess.run(["xattr", "-w", "com.apple.ResourceFork", "x",
                                str(forked)], check=True)
            blockers = [app.compression_blockers(path)
                        for path in (stale, forked, clean)]
            args = argparse.Namespace(
                cutoff_days=0, min_size=1, exclude_ext="", exclude_dir="",
                include_hardlinks=False, include_sparse=False,
                internal_paths=set(),
            )
            work = queue.Queue()
            stats = app.Stats()
            app.scanner(args, [Path(directory)], work, stats,
                        app.threading.Lock())
            queued = work.get_nowait()[2].path
            with mock.patch.object(app, "TOOL", "afsctool"), \
                 mock.patch.object(app, "BASE", ["afsctool", "-c", "-v"]), \
                 mock.patch.object(app.subprocess, "run") as run, \
                 mock.patch.object(app, "already_compressed",
                                   return_value=True):
                with self.assertRaises(app.CompressorRefusedError) as raised:
                    app.run_compressor([stale, forked, clean], "lzvn")
            content = stale.read_bytes()

        self.assertEqual(blockers, [["stale compression metadata"],
                                    ["a resource fork"], []])
        self.assertEqual(queued, clean)
        self.assertEqual(stats.skipped_blocked, 2)
        command = run.call_args.args[0]
        self.assertIn(str(clean), command)
        self.assertNotIn(str(stale), command)
        self.assertNotIn(str(forked), command)
        self.assertIn("stale compression metadata",
                      raised.exception.failures[str(stale)])
        self.assertEqual(content, b"text" * 1024)

    def test_afsctool_uses_supported_codec_flag_and_one_inner_worker(self):
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "BASE", ["afsctool", "-c", "-v"]), \
             mock.patch.object(app.subprocess, "run") as run, \
             mock.patch.object(app, "already_compressed", return_value=True):
            app.run_compressor(Path("example.txt"), "lzfse")

        command = run.call_args.args[0]
        self.assertEqual(
            command,
            ["afsctool", "-c", "-v", "-T", "LZFSE", "-j1", "example.txt"],
        )

    def test_afsctool_error_on_a_file_uses_atomic_fallback(self):
        path = Path("refused.txt")
        output = SimpleNamespace(
            stdout="", stderr="refused.txt: Operation not permitted\n"
        )
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "BASE", ["afsctool", "-c", "-v"]), \
             mock.patch.object(app.subprocess, "run", return_value=output), \
             mock.patch.object(app, "already_compressed", return_value=False), \
             mock.patch.object(app, "atomic_compress_file") as fallback:
            fallback_paths = app.run_compressor(path, "lzvn")

        fallback.assert_called_once_with(path, "lzvn")
        self.assertEqual(fallback_paths, {path})

    def test_silent_decline_is_not_retried_on_a_copy(self):
        dense = Path("dense.bin")
        output = SimpleNamespace(
            stdout="dense.bin:\nUnable to compress file.\n",
            stderr="Worker thread #0 processed 1 files (0.00%)\n",
        )
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "BASE", ["afsctool", "-c", "-v"]), \
             mock.patch.object(app.subprocess, "run",
                               return_value=output) as run, \
             mock.patch.object(app, "already_compressed", return_value=False), \
             mock.patch.object(app, "atomic_compress_file") as fallback:
            with self.assertRaises(app.CompressorRefusedError) as raised:
                app.run_compressor(dense, "lzvn")

        fallback.assert_not_called()
        run.assert_called_once()
        self.assertIsInstance(raised.exception.error_for_path(dense),
                              app.NoSavingsError)

    def test_error_on_the_copy_is_a_refusal_not_a_lack_of_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "denied.txt")
            path.write_bytes(b"text" * 1024)

            def execute(command, **kwargs):
                if command[0] == "ditto":
                    Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
                    return SimpleNamespace(stdout="", stderr="")
                return SimpleNamespace(
                    stdout="", stderr=f"{command[-1]}: Permission denied\n"
                )

            with mock.patch.object(app, "BASE", ["afsctool"]), \
                 mock.patch.object(app.subprocess, "run", side_effect=execute), \
                 mock.patch.object(app, "already_compressed",
                                   return_value=False):
                with self.assertRaises(RuntimeError) as raised:
                    app.atomic_compress_file(path, "lzvn")

        self.assertNotIsInstance(raised.exception, app.NoSavingsError)
        self.assertEqual(str(raised.exception), "Permission denied")

    def test_copy_that_differs_from_the_source_is_never_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "source.txt")
            path.write_bytes(b"real content" * 1024)

            def execute(command, **kwargs):
                if command[0] == "ditto":
                    Path(command[-1]).write_bytes(b"")
                return SimpleNamespace(stdout="", stderr="")

            with mock.patch.object(app, "BASE", ["afsctool"]), \
                 mock.patch.object(app.subprocess, "run", side_effect=execute), \
                 mock.patch.object(app, "already_compressed", return_value=True), \
                 mock.patch.object(app, "open_file_users", return_value=[]), \
                 mock.patch.object(app, "allocated_size",
                                   side_effect=[0, 16_384]):
                with self.assertRaisesRegex(RuntimeError, "copy differs"):
                    app.atomic_compress_file(path, "lzvn")

            self.assertEqual(path.read_bytes(), b"real content" * 1024)

    def test_atomic_fallback_replaces_only_unchanged_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "candidate.txt")
            path.write_bytes(b"compressible" * 1024)

            def execute(command, **kwargs):
                if command[0] == "ditto":
                    Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
                return SimpleNamespace(stdout="", stderr="")

            with mock.patch.object(app, "BASE", ["afsctool"]), \
                 mock.patch.object(app.subprocess, "run", side_effect=execute), \
                 mock.patch.object(app, "already_compressed", return_value=True), \
                 mock.patch.object(app, "open_file_users", return_value=[]), \
                 mock.patch.object(app, "allocated_size",
                                   side_effect=[1024, 16_384]):
                app.atomic_compress_file(path, "lzfse")

            self.assertEqual(path.read_bytes(), b"compressible" * 1024)
            self.assertFalse(any(path.parent.glob(
                f"{app.ATOMIC_TEMP_PREFIX}*"
            )))

    def test_executable_images_are_replaced_never_rewritten_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory, "program")
            image.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 4096)
            data = Path(directory, "data.txt")
            data.write_bytes(b"text" * 1024)
            with mock.patch.object(app, "TOOL", "afsctool"), \
                 mock.patch.object(app, "BASE", ["afsctool", "-c", "-v"]), \
                 mock.patch.object(app.subprocess, "run") as run, \
                 mock.patch.object(app, "already_compressed",
                                   return_value=True), \
                 mock.patch.object(app, "atomic_compress_file") as replace, \
                 mock.patch.object(app, "atomic_decompress_file") as restore:
                replaced = app.run_compressor([image, data], "lzvn")
                app.decompress_file(image)
                app.recompress_file(image, "zlib", "lzvn")

        in_place = [call.args[0] for call in run.call_args_list]
        self.assertEqual(replaced, {image})
        self.assertTrue(all(str(image) not in command for command in in_place))
        self.assertTrue(any(str(data) in command for command in in_place))
        self.assertEqual(
            replace.call_args_list,
            [mock.call(image, "lzvn"), mock.call(image, "zlib")],
        )
        restore.assert_called_once_with(image)

    def test_files_open_for_writing_are_left_untouched(self):
        busy = Path("live.db")
        idle = Path("idle.txt")
        users = [{"pid": 7, "command": "Editor", "access": "u"}]
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "BASE", ["afsctool", "-c", "-v"]), \
             mock.patch.object(app.OPEN_FILES, "busy",
                               return_value={busy: users}), \
             mock.patch.object(app.subprocess, "run") as run, \
             mock.patch.object(app, "already_compressed", return_value=True):
            with self.assertRaises(app.CompressorRefusedError) as raised:
                app.run_compressor([busy, idle], "lzvn")
            with self.assertRaises(app.FileInUseError):
                app.decompress_file(busy)

        command = run.call_args.args[0]
        self.assertIn("idle.txt", command)
        self.assertNotIn("live.db", command)
        cause = raised.exception.error_for_path(busy)
        self.assertIsInstance(cause, app.FileInUseError)
        self.assertEqual(cause.users, users)
        self.assertNotIn(str(idle), raised.exception.failures)

    def test_open_file_snapshot_keeps_writers_and_mappings_only(self):
        use = app.OpenUse
        fake = SimpleNamespace(open_file_users=lambda mappings=True: {
            (1, 10): [use(41, "u"), use(42, "w")],
            (1, 11): [use(41, "r"), use(43, "-")],
            (1, 12): [use(44, "r"), use(44, "mapped")],
        })
        with mock.patch.object(app, "native", return_value=fake):
            users = app.snapshot_open_files()

        self.assertEqual(sorted(users), [(1, 10), (1, 12)])
        self.assertEqual([user.pid for user in users[(1, 10)]], [41, 42])
        self.assertEqual(users[(1, 12)], [use(44, "mapped")])

    def test_busy_files_are_matched_by_inode_not_by_spelling(self):
        # A path typed in another letter case names the same file on a
        # case-insensitive volume; a comparison of names would miss it.
        mock.patch.stopall()
        self.addCleanup(mock.patch.stopall)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory, "CaseDir")
            folder.mkdir()
            held = folder / "held.txt"
            held.write_text("x" * 4096)
            idle = folder / "idle.txt"
            idle.write_text("y" * 4096)
            other_spelling = Path(directory, "casedir", "held.txt")
            if not other_spelling.exists():
                self.skipTest("volume is case-sensitive")
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys, time; h = open(sys.argv[1], 'r+'); "
                 "print('ready', flush=True); time.sleep(60)", str(held)],
                stdout=subprocess.PIPE, text=True)
            try:
                holder.stdout.readline()
                busy = app.OpenFiles().busy([other_spelling, idle])
                users = app.open_file_users(other_spelling)
            finally:
                holder.kill()
                holder.wait()
                holder.stdout.close()

        self.assertEqual(list(busy), [other_spelling])
        self.assertEqual(busy[other_spelling][0]["pid"], holder.pid)
        self.assertEqual(busy[other_spelling][0]["access"], "u")
        self.assertTrue(busy[other_spelling][0]["command"])
        self.assertIn(holder.pid, [user["pid"] for user in users])

    def test_unlistable_attributes_block_compression(self):
        def refuse(path, options=0):
            raise PermissionError(13, "Permission denied", str(path))

        fake = SimpleNamespace(xattr_names=refuse)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(app, "native", return_value=fake):
            guarded = Path(directory, "guarded.bin")
            guarded.write_bytes(b"data")
            blockers = app.compression_blockers(guarded)
            problems = list(app.find_file_problems([Path(directory)]))

        self.assertEqual(blockers,
                         ["unreadable extended attributes (Permission denied)"])
        self.assertEqual(problems, [])

    def test_codec_is_read_from_the_compression_metadata(self):
        afsctool = app.shutil.which("afsctool")
        if not afsctool:
            self.skipTest("afsctool is not installed")
        with tempfile.TemporaryDirectory() as directory:
            for codec in ("zlib", "lzvn", "lzfse"):
                for size in (2000, 400_000):   # payload in the xattr, in the fork
                    path = Path(directory, f"{codec}-{size}.txt")
                    path.write_bytes(b"alpha,beta,gamma,delta\n" * (size // 23))
                    subprocess.run([afsctool, "-c", "-T", codec.upper(),
                                    str(path)], capture_output=True, check=True)
                    if not app.already_compressed(path):
                        continue   # afsctool found no gain for this size
                    with mock.patch.object(
                            app.shutil, "which",
                            side_effect=AssertionError("afsctool consulted")):
                        self.assertEqual(app.compression_algorithm(path), codec)
            plain = Path(directory, "plain.txt")
            plain.write_text("not compressed")
            with mock.patch.object(app.shutil, "which", return_value=None):
                self.assertIsNone(app.compression_algorithm(plain))

    def test_atomic_fallback_preserves_hard_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "linked.txt")
            path.write_bytes(b"linked" * 1024)
            os.link(path, Path(directory, "linked-again.txt"))
            with mock.patch.object(app.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "multiply-linked"):
                    app.atomic_compress_file(path, "lzvn")

            run.assert_not_called()
            self.assertEqual(path.stat().st_nlink, 2)

    def test_ditto_fallback_does_not_replace_a_changed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "changing.txt")
            path.write_bytes(b"original" * 1024)

            def execute(command, **kwargs):
                Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
                path.write_bytes(b"written after the copy" * 1024)
                return SimpleNamespace(stdout="", stderr="")

            with mock.patch.object(app, "TOOL", "ditto"), \
                 mock.patch.object(app, "BASE", ["ditto", "--hfsCompression"]), \
                 mock.patch.object(app.subprocess, "run", side_effect=execute), \
                 mock.patch.object(app, "already_compressed", return_value=True), \
                 mock.patch.object(app, "open_file_users", return_value=[]), \
                 mock.patch.object(app, "allocated_size",
                                   side_effect=[1024, 16_384]):
                with self.assertRaises(app.CompressorRefusedError) as raised:
                    app.run_compressor(path, "lzvn")

            self.assertIn("source changed", str(raised.exception))
            self.assertEqual(path.read_bytes(),
                             b"written after the copy" * 1024)
            self.assertFalse(any(path.parent.glob(
                f"{app.ATOMIC_TEMP_PREFIX}*"
            )))

    def test_open_file_users_returns_process_and_access_metadata(self):
        use = app.OpenUse
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "document.txt")
            path.write_text("text")
            st = path.stat()
            fake = SimpleNamespace(
                open_file_users=lambda mappings=True: {
                    (st.st_dev, st.st_ino): [use(123, "u"), use(456, "r"),
                                             use(456, "r")],
                    (st.st_dev, st.st_ino + 1): [use(789, "w")],
                },
                process_name={123: "TextEdit", 456: "QuickLook"}.get,
            )
            with mock.patch.object(app, "native", return_value=fake):
                users = app.open_file_users(path)

        self.assertEqual(users, [
            {"pid": 123, "command": "TextEdit", "access": "u"},
            {"pid": 456, "command": "QuickLook", "access": "r"},
        ])

    def test_atomic_fallback_does_not_replace_an_open_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "active.txt")
            path.write_bytes(b"active" * 1024)
            original_inode = path.stat().st_ino

            def execute(command, **kwargs):
                if command[0] == "ditto":
                    Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
                return SimpleNamespace(stdout="", stderr="")

            users = [{"pid": 123, "command": "Editor", "access": "u"}]
            with mock.patch.object(app, "BASE", ["afsctool"]), \
                 mock.patch.object(app.subprocess, "run", side_effect=execute), \
                 mock.patch.object(app, "already_compressed", return_value=True), \
                 mock.patch.object(app, "open_file_users",
                                   return_value=users), \
                 mock.patch.object(app, "allocated_size",
                                   side_effect=[1024, 8192]):
                with self.assertRaises(app.FileInUseError):
                    app.atomic_compress_file(path, "lzvn")

            self.assertEqual(path.stat().st_ino, original_inode)
            self.assertFalse(any(path.parent.glob(
                f"{app.ATOMIC_TEMP_PREFIX}*"
            )))

    def test_afsctool_is_preferred_for_recovery_support(self):
        with mock.patch.object(app.shutil, "which") as which:
            which.side_effect = lambda name: f"/bin/{name}"
            tool, command = app.find_compressor()

        self.assertEqual(tool, "afsctool")
        self.assertEqual(command[0], "afsctool")

    def test_ditto_backend_disables_cloning_where_supported(self):
        for usage, expected in (
                ("    --noclone    do not attempt to clone files\n",
                 ["ditto", "--hfsCompression", "--noclone"]),
                ("    --hfsCompression    compress files\n",
                 ["ditto", "--hfsCompression"])):
            output = SimpleNamespace(stdout="", stderr=usage)
            with mock.patch.object(app.shutil, "which", return_value=None), \
                 mock.patch.object(app.subprocess, "run", return_value=output):
                tool, command = app.find_compressor("ditto")

            self.assertEqual(tool, "ditto")
            self.assertEqual(command, expected)

    def test_afsctool_is_the_default_backend_even_when_absent(self):
        with mock.patch.object(app.shutil, "which", return_value=None):
            tool, _ = app.find_compressor()
            with self.assertRaises(app.AppError) as raised:
                app.select_backend(app.DEFAULT_BACKEND, selectable=True)
            with self.assertRaises(app.AppError) as fixed:
                app.select_backend(app.DEFAULT_BACKEND)

        self.assertEqual(fixed.exception.suggestion, "install afsctool")
        self.assertEqual(tool, "afsctool")
        self.assertEqual(raised.exception.error, "unsupported_backend")
        self.assertEqual(raised.exception.exit_code, app.EXIT_UNSUPPORTED)
        self.assertIn("--backend ditto", raised.exception.suggestion)

    def test_explicit_backend_replaces_the_default(self):
        output = SimpleNamespace(stdout="", stderr="--noclone\n")
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "BASE", ["afsctool", "-c", "-v"]), \
             mock.patch.object(app, "DITTO_COPY", ["ditto"]), \
             mock.patch.object(app.shutil, "which",
                               return_value="/usr/bin/ditto"), \
             mock.patch.object(app.subprocess, "run", return_value=output):
            app.select_backend("ditto")
            selected = (app.TOOL, app.BASE)

        self.assertEqual(
            selected, ("ditto", ["ditto", "--hfsCompression", "--noclone"])
        )

    def test_selected_backend_copies_without_clones_or_compression(self):
        output = SimpleNamespace(stdout="", stderr="--noclone\n")
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "BASE", ["afsctool", "-c", "-v"]), \
             mock.patch.object(app, "DITTO_COPY", ["ditto"]), \
             mock.patch.object(app.shutil, "which",
                               return_value="/bin/afsctool"), \
             mock.patch.object(app.subprocess, "run", return_value=output):
            app.select_backend("afsctool")
            copy = app.DITTO_COPY

        self.assertEqual(
            copy, ["ditto", "--nopreserveHFSCompression", "--noclone"]
        )

    def test_afsctool_decompression_uses_explicit_path(self):
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "BASE", ["afsctool"]), \
             mock.patch.object(app.subprocess, "run") as run:
            app.decompress_file(Path("example.txt"))

        run.assert_called_once_with(
            ["afsctool", "-d", "example.txt"], check=True,
            capture_output=True, text=True,
        )

    def test_decompression_is_idempotent_for_uncompressed_files(self):
        record = {
            "path": "/tmp/file",
            "compressed": False,
            "algorithm": None,
            "logical_bytes": 10,
            "allocated_bytes": 4096,
        }
        with mock.patch.object(app, "inspect_file", return_value=record), \
             mock.patch.object(app, "decompress_file") as decompress:
            result = app.decompress_files([Path("/tmp/file")], False, True)

        decompress.assert_not_called()
        self.assertEqual(result["files_changed"], 0)
        self.assertEqual(result["files_unchanged"], 1)

    def test_decompression_verifies_the_result(self):
        before = {
            "path": "/tmp/file",
            "compressed": True,
            "algorithm": "lzvn",
            "logical_bytes": 10,
            "allocated_bytes": 4096,
        }
        after = {**before, "compressed": False, "algorithm": None}
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "inspect_file", side_effect=[before, after]), \
             mock.patch.object(app, "decompress_file") as decompress:
            result = app.decompress_files([Path("/tmp/file")], False, True)

        decompress.assert_called_once_with(Path("/tmp/file"))
        self.assertEqual(result["files_changed"], 1)
        self.assertEqual(result["errors"], 0)

    def test_dry_run_never_marks_state_cache(self):
        args = argparse.Namespace(
            dry_run=True, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False,
        )
        cache = FakeCache()
        stats = app.Stats()
        bar = FakeBar()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "compressible.txt")
            path.write_bytes(b"a" * 4096)
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat())))
            work.put((float("inf"), 1, None))
            with mock.patch.object(app, "already_compressed", return_value=False):
                app.worker(
                    args,
                    work,
                    FixedSampler(0.25),
                    cache,
                    bar,
                    stats,
                    app.threading.Lock(),
                    app.BatchingCompressor(2, app.VolumeLimiter(1)),
                    "test-policy",
                    app.WorkBudget(0, 0),
                    app.RateLimiter(0),
                    app.Reporter("json"),
                )

        self.assertEqual(cache.marks, [])
        self.assertEqual(stats.compressed, 1)
        self.assertEqual(bar.updates, 1)

    def test_scan_audits_suggested_files_that_are_already_compressed(self):
        args = argparse.Namespace(
            dry_run=True, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False, audit_compressed=True,
        )
        stats = app.Stats(already_compressed=1)
        reporter = app.Reporter("json", retain=True)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "compressed.txt")
            path.write_bytes(b"a" * 4096)
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat(), True)))
            work.put((float("inf"), 1, None))
            with mock.patch.object(app, "already_compressed", return_value=True):
                app.worker(
                    args, work, FixedSampler(0.25), FakeCache(), FakeBar(),
                    stats, app.threading.Lock(),
                    app.BatchingCompressor(2, app.VolumeLimiter(1)),
                    "test-policy", app.WorkBudget(0, 0), app.RateLimiter(0),
                    reporter,
                )

        self.assertEqual(stats.suggested, 1)
        self.assertEqual(stats.suggested_already_compressed, 1)
        self.assertEqual(stats.suggested_not_compressed, 0)
        self.assertEqual(stats.compressed, 0)
        self.assertEqual(
            reporter.records[0]["status"], "suggested_already_compressed"
        )

    def test_ratio_rejected_files_are_not_reported_as_candidates(self):
        args = argparse.Namespace(
            dry_run=True, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False,
        )
        stats = app.Stats()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "incompressible.bin")
            path.write_bytes(os.urandom(64 << 10))
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat())))
            work.put((float("inf"), 1, None))
            with mock.patch.object(app, "already_compressed", return_value=False):
                app.worker(
                    args, work, FixedSampler(0.9), FakeCache(), FakeBar(),
                    stats, app.threading.Lock(),
                    app.BatchingCompressor(2, app.VolumeLimiter(1)),
                    "test-policy", app.WorkBudget(0, 0), app.RateLimiter(0),
                    app.Reporter("json"),
                )

        self.assertEqual(stats.skipped_ratio, 1)
        self.assertEqual(stats.estimated_bytes_saved, 0)
        self.assertEqual(stats.ranked_candidates(), [])

    def test_progress_line_reports_counts_savings_and_completion(self):
        stats = app.Stats(scanned=1500, queued=40, compressed=7, errors=1,
                          bytes_before=5 << 20, bytes_after=1 << 20)
        progress = app.Progress(stats, dry_run=False, enabled=False)
        progress.update(10)
        scanning = progress.line()
        progress.scan_finished()
        finished = progress.line()

        self.assertIn("scanning, 1,500 found | 10/40 processed", scanning)
        self.assertIn("10/40 processed (25%)", finished)
        self.assertIn("7 compressed | 4.0 MiB saved | 1 errors", finished)
        self.assertIn("7 planned | 4.0 MiB estimated",
                      app.Progress(stats, True, False).line())

    def test_top_candidates_are_bounded_and_ranked(self):
        stats = app.Stats()
        for sequence, score in enumerate([3.0, 9.0, 1.0, 9.0, 5.0]):
            stats.offer_candidate(
                2, sequence, {"path": str(sequence), "value_score": score}
            )
        stats.offer_candidate(0, 9, {"path": "ignored", "value_score": 99.0})

        self.assertEqual(len(stats.top_candidates), 2)
        self.assertEqual(
            [record["value_score"] for record in stats.ranked_candidates()],
            [9.0, 9.0],
        )

    def test_worker_exits_an_idle_queue_once_stopped(self):
        args = argparse.Namespace(dry_run=True)
        stop = app.threading.Event()
        stop.set()
        bar = FakeBar()

        app.worker(
            args, queue.Queue(), FixedSampler(0.25), FakeCache(), bar,
            app.Stats(), app.threading.Lock(),
            app.BatchingCompressor(2, app.VolumeLimiter(1)), "test-policy",
            app.WorkBudget(0, 0), app.RateLimiter(0), app.Reporter("json"),
            stop,
        )

        self.assertEqual(bar.updates, 0)

    def test_failed_compression_remains_retryable(self):
        args = argparse.Namespace(
            dry_run=False, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False,
        )
        cache = FakeCache()
        stats = app.Stats()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "retry.txt")
            path.write_bytes(b"a" * 4096)
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat())))
            work.put((float("inf"), 1, None))
            error = app.subprocess.CalledProcessError(2, ["afsctool"])
            with mock.patch.object(app, "already_compressed", return_value=False), \
                 mock.patch.object(app, "run_compressor", side_effect=error):
                app.worker(
                    args,
                    work,
                    FixedSampler(0.25),
                    cache,
                    FakeBar(),
                    stats,
                    app.threading.Lock(),
                    app.BatchingCompressor(2, app.VolumeLimiter(1), 0),
                    "test-policy",
                    app.WorkBudget(0, 0),
                    app.RateLimiter(0),
                    app.Reporter("json"),
                )

        self.assertEqual(cache.marks, [])
        self.assertEqual(stats.errors, 1)

    def test_semantic_compressor_refusal_is_an_actionable_failure(self):
        args = argparse.Namespace(
            dry_run=False, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False,
        )
        cache = FakeCache()
        stats = app.Stats()
        reporter = app.Reporter("json", retain=True)
        batcher = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "refused.txt")
            path.write_bytes(b"a" * 4096)
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat())))
            work.put((float("inf"), 1, None))
            batcher.submit.side_effect = app.CompressorRefusedError(
                {path: "temporary copy was not compressed"}
            )
            with mock.patch.object(app, "already_compressed",
                                   return_value=False):
                app.worker(
                    args, work, FixedSampler(0.25), cache, FakeBar(), stats,
                    app.threading.Lock(), batcher, "test-policy",
                    app.WorkBudget(0, 0), app.RateLimiter(0), reporter,
                )

        self.assertEqual(cache.marks, [])
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.error_messages, {"compressor_refused": 1})
        self.assertEqual(reporter.failures[0]["error"], "compressor_refused")
        self.assertFalse(reporter.failures[0]["retryable"])

    def test_compressor_finding_no_gain_is_a_cached_skip_not_an_error(self):
        args = argparse.Namespace(
            dry_run=False, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False,
        )
        cache = FakeCache()
        stats = app.Stats()
        reporter = app.Reporter("json", retain=True)
        batcher = mock.Mock()
        batcher.submit.side_effect = app.NoSavingsError("no gain")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "dense.txt")
            path.write_bytes(b"a" * 4096)
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat())))
            work.put((float("inf"), 1, None))
            with mock.patch.object(app, "already_compressed",
                                   return_value=False):
                app.worker(
                    args, work, FixedSampler(0.25), cache, FakeBar(), stats,
                    app.threading.Lock(), batcher, "test-policy",
                    app.WorkBudget(0, 0), app.RateLimiter(0), reporter,
                )

        self.assertEqual(stats.errors, 0)
        self.assertEqual(stats.processed_no_savings, 1)
        self.assertEqual(reporter.failures, [])
        self.assertEqual(reporter.records[0]["status"], "no_savings")
        self.assertEqual(cache.marks[-1][3], "no_savings")

    def test_declined_copy_is_reported_as_no_savings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "dense.bin")
            path.write_bytes(os.urandom(4096))

            def execute(command, **kwargs):
                if command[0] == "ditto":
                    Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
                return SimpleNamespace(stdout="Unable to compress file.",
                                       stderr="")

            with mock.patch.object(app, "BASE", ["afsctool"]), \
                 mock.patch.object(app.subprocess, "run", side_effect=execute), \
                 mock.patch.object(app, "already_compressed",
                                   return_value=False):
                with self.assertRaises(app.NoSavingsError):
                    app.atomic_compress_file(path, "lzvn")
                refusal = app.CompressorRefusedError(
                    {path: app.NoSavingsError("no gain")}
                )

        self.assertIsInstance(refusal.error_for_path(path), app.NoSavingsError)

    def test_budget_names_the_limit_that_stopped_work(self):
        self.assertEqual(app.WorkBudget(0, 100).limit(), "space_target")
        expired = app.WorkBudget(5, 0)
        expired.deadline = app.time.monotonic() - 1
        self.assertEqual(expired.limit(), "time_budget")

    def test_estimates_are_attributed_to_sample_coverage(self):
        stats = app.Stats()
        stats.add_coverage(1.0, 1000, 600)
        stats.add_coverage(0.5, 4000, 1000)
        stats.add_coverage(0.05, 9000, 5000)
        stats.add_coverage(0.0001, 80_000, 70_000)
        report = {bucket["coverage"]: bucket for bucket in
                  stats.coverage_report()}

        self.assertEqual(list(report), ["full", "10-100%", "1-10%", "under 1%"])
        self.assertEqual(report["full"]["estimated_saved_bytes"], 600)
        self.assertEqual(report["1-10%"]["files"], 1)
        self.assertEqual(report["under 1%"]["allocated_bytes"], 80_000)

    def test_open_file_is_a_retryable_partial_failure(self):
        args = argparse.Namespace(
            dry_run=False, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False,
        )
        stats = app.Stats()
        reporter = app.Reporter("json", retain=True)
        batcher = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "open.txt")
            path.write_bytes(b"a" * 4096)
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat())))
            work.put((float("inf"), 1, None))
            users = [{"pid": 123, "command": "Editor", "access": "u"}]
            batcher.submit.side_effect = app.FileInUseError(path, users)
            with mock.patch.object(app, "already_compressed",
                                   return_value=False):
                app.worker(
                    args, work, FixedSampler(0.25), FakeCache(), FakeBar(),
                    stats, app.threading.Lock(), batcher, "test-policy",
                    app.WorkBudget(0, 0), app.RateLimiter(0), reporter,
                )

        self.assertEqual(stats.skipped_in_use, 1)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(reporter.failures[0]["error"], "file_in_use")
        self.assertTrue(reporter.failures[0]["retryable"])
        self.assertEqual(reporter.failures[0]["open_processes"], users)

    def test_zero_savings_afsctool_result_is_decompressed(self):
        args = argparse.Namespace(
            dry_run=False, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False,
        )
        cache = FakeCache()
        stats = app.Stats()
        batcher = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "no-savings.txt")
            path.write_bytes(b"a" * 4096)
            device = path.stat().st_dev
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat())))
            work.put((float("inf"), 1, None))
            with mock.patch.object(app, "TOOL", "afsctool"), \
                 mock.patch.object(
                     app, "already_compressed", side_effect=[False, True]
                 ):
                app.worker(
                    args, work, FixedSampler(0.25), cache, FakeBar(), stats,
                    app.threading.Lock(), batcher, "test-policy",
                    app.WorkBudget(0, 0), app.RateLimiter(0),
                    app.Reporter("json"),
                )

        batcher.decompress.assert_called_once_with(path, device)
        self.assertEqual(stats.reverted_no_savings, 1)
        self.assertEqual(cache.marks[-1][-1], "no_savings_reverted")

    def test_space_budget_reserves_work_across_workers(self):
        budget = app.WorkBudget(0, 100)
        self.assertTrue(budget.claim(80))
        self.assertTrue(budget.claim(20))
        self.assertFalse(budget.claim(1))
        budget.complete(80, 70)
        self.assertFalse(budget.claim(40))
        budget.complete(20, 20)
        self.assertEqual(budget.saved, 90)

    def test_rate_limiter_reserves_transfer_time_before_io(self):
        limiter = app.RateLimiter(1)
        limiter.next_at = 10.0
        with mock.patch.object(app.time, "monotonic", return_value=10.0), \
             mock.patch.object(app.time, "sleep") as sleep:
            limiter.throttle(512 * 1024)
            limiter.throttle(512 * 1024)

        self.assertEqual(sleep.call_args_list, [mock.call(0.5), mock.call(1.0)])

    def test_rate_limiter_wait_ends_when_the_run_stops(self):
        stop = app.threading.Event()
        limiter = app.RateLimiter(1, stop)
        app.threading.Timer(0.05, stop.set).start()
        started = app.time.monotonic()
        with self.assertRaises(app.RunStopped):
            limiter.throttle(60 << 20)
        with self.assertRaises(app.RunStopped):
            app.RateLimiter(0, stop).throttle(1)

        self.assertLess(app.time.monotonic() - started, 5)

    def test_rate_limiter_refuses_a_wait_longer_than_the_timeout(self):
        limiter = app.RateLimiter(1)
        with mock.patch.object(app.time, "sleep") as sleep:
            refused = limiter.throttle(10 << 20, timeout=2.0)
            accepted = limiter.throttle(1 << 20, timeout=2.0)

        self.assertFalse(refused)
        self.assertTrue(accepted)
        sleep.assert_called_once()

    def test_write_that_would_outlast_the_time_budget_is_not_started(self):
        args = argparse.Namespace(
            dry_run=False, ratio=0.85, algo="lzvn", profile="fast",
            cold_days=365, archive_days=730, min_codec_gain=0.05,
            recompress=False, ignore_cache=False,
        )
        cache = FakeCache()
        stats = app.Stats()
        batcher = mock.Mock()
        reporter = app.Reporter("json", retain=True)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "slow.txt")
            path.write_bytes(b"a" * (1 << 20))
            work = queue.Queue()
            work.put((0, 0, app.Candidate(path, path.stat())))
            work.put((float("inf"), 1, None))
            with mock.patch.object(app, "already_compressed",
                                   return_value=False):
                app.worker(
                    args, work, FixedSampler(0.5), cache, FakeBar(), stats,
                    app.threading.Lock(), batcher, "test-policy",
                    app.WorkBudget(1, 0), app.RateLimiter(0.001), reporter,
                )

        batcher.submit.assert_not_called()
        self.assertEqual(stats.skipped_budget, 1)
        self.assertEqual(reporter.records[0]["status"], "budget_exhausted")
        self.assertEqual(cache.marks, [])

    def test_batcher_coalesces_compatible_concurrent_files(self):
        batcher = app.BatchingCompressor(2, app.VolumeLimiter(1), 0.2)
        paths = [Path("one"), Path("two")]
        with mock.patch.object(app, "run_compressor") as run:
            with app.cf.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(batcher.submit, path, "lzvn", 1)
                    for path in paths
                ]
                for future in futures:
                    future.result()

        run.assert_called_once_with(paths, "lzvn")
        self.assertEqual(batcher.processes, 1)
        self.assertEqual(batcher.files, 2)

    def test_batcher_reports_which_files_were_replaced(self):
        batcher = app.BatchingCompressor(2, app.VolumeLimiter(1), 0.2)
        paths = [Path("image"), Path("data")]
        with mock.patch.object(app, "run_compressor",
                               return_value={paths[0]}):
            with app.cf.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(batcher.submit, path, "lzvn", 1)
                    for path in paths
                ]
                replaced = [future.result() for future in futures]

        self.assertEqual(replaced, [True, False])
        self.assertEqual(batcher.atomic_fallback_files, 1)

    def test_batcher_bisects_compressor_failures_to_save_good_files(self):
        batcher = app.BatchingCompressor(3, app.VolumeLimiter(1), 0.2)
        paths = [Path("good-one"), Path("bad"), Path("good-two")]

        def compress(batch, algorithm):
            if Path("bad") in batch:
                raise app.subprocess.CalledProcessError(1, ["afsctool"])

        with mock.patch.object(app, "run_compressor", side_effect=compress):
            with app.cf.ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    pool.submit(batcher.submit, path, "lzvn", 1)
                    for path in paths
                ]
                results = []
                for future in futures:
                    try:
                        future.result()
                        results.append("ok")
                    except app.subprocess.CalledProcessError:
                        results.append("failed")

        self.assertEqual(results.count("ok"), 2)
        self.assertEqual(results.count("failed"), 1)
        self.assertGreater(batcher.processes, 1)

    def test_batcher_attributes_semantic_refusal_to_failed_path(self):
        batcher = app.BatchingCompressor(2, app.VolumeLimiter(1), 0.2)
        paths = [Path("good"), Path("refused")]

        def compress(batch, algorithm):
            raise app.CompressorRefusedError({paths[1]: "refused"})

        with mock.patch.object(app, "run_compressor", side_effect=compress):
            with app.cf.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(batcher.submit, path, "lzvn", 1)
                    for path in paths
                ]
                results = []
                for future in futures:
                    try:
                        future.result()
                        results.append("ok")
                    except app.CompressorRefusedError:
                        results.append("refused")

        self.assertEqual(results, ["ok", "refused"])

    def test_existing_codec_is_parsed_from_afsctool(self):
        output = SimpleNamespace(
            stdout="Compression type: LZFSE in decmpfs xattr (11)\n"
        )
        with mock.patch.object(app.shutil, "which", return_value="afsctool"), \
             mock.patch.object(app.subprocess, "run", return_value=output):
            self.assertEqual(app.compression_algorithm(Path("file")), "lzfse")

    def test_recompression_restores_old_codec_after_failure(self):
        with mock.patch.object(app, "TOOL", "afsctool"), \
             mock.patch.object(app, "BASE", ["afsctool"]), \
             mock.patch.object(app.subprocess, "run"), \
             mock.patch.object(
                 app, "run_compressor", side_effect=[RuntimeError("failed"), None]
             ) as compress:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                app.recompress_file(Path("file"), "zlib", "lzvn")

        self.assertEqual(
            compress.call_args_list,
            [mock.call(Path("file"), "zlib"), mock.call(Path("file"), "lzvn")],
        )

    def test_benchmark_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "benchmark.json")
            report = app.run_benchmark(destination)
            persisted = app.json.loads(destination.read_text())

        self.assertEqual(set(report["codecs"]), {"lzvn", "lzfse", "zlib"})
        self.assertEqual(persisted, report)

    def test_valid_benchmark_is_loaded_for_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "benchmark.json")
            report = app.run_benchmark(destination)

            self.assertEqual(app.load_benchmark(destination), report)

    def test_invalid_benchmark_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "benchmark.json")
            destination.write_text("not json")

            self.assertIsNone(app.load_benchmark(destination))

    def test_filesystem_validation_uses_the_volume_of_each_root(self):
        def fake(filesystem):
            return SimpleNamespace(mount=lambda path: SimpleNamespace(
                point="/Volumes/Archive", device="/dev/disk2s1",
                fstype=filesystem))

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(app.os, "statvfs",
                               return_value=SimpleNamespace(f_flag=0)):
            with mock.patch.object(app, "native", return_value=fake("apfs")):
                volumes = app.validate_target_filesystems([Path(directory)])
            with mock.patch.object(app, "native", return_value=fake("exfat")):
                with self.assertRaises(app.AppError) as raised:
                    app.validate_target_filesystems([Path(directory)])

        self.assertEqual(volumes[0]["mountpoint"], "/Volumes/Archive")
        self.assertEqual(raised.exception.error, "unsupported_filesystem")
        self.assertEqual(raised.exception.exit_code, app.EXIT_UNSUPPORTED)

    def test_filesystem_validation_inspects_each_volume_once(self):
        calls = []

        def mount(path):
            calls.append(path)
            return SimpleNamespace(point="/", device="/dev/disk1",
                                   fstype="apfs")

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(app, "native",
                               return_value=SimpleNamespace(mount=mount)), \
             mock.patch.object(app.os, "statvfs",
                               return_value=SimpleNamespace(f_flag=0)):
            roots = []
            for name in ("one", "two", "three"):
                root = Path(directory, name)
                root.write_text(name)
                roots.append(root)
            app.validate_target_filesystems(roots)

        self.assertEqual(len(calls), 1)

    def test_volume_inspection_reports_the_real_mount(self):
        info = app.inspect_volume(Path.home())
        self.assertEqual(info["filesystem"], "apfs")
        self.assertTrue(info["compression_supported"])
        self.assertTrue(Path(info["mountpoint"]).is_dir())


class InputTests(unittest.TestCase):
    def test_stdin_path_list_is_refused_on_a_terminal(self):
        with mock.patch.object(app.sys, "stdin") as stdin:
            stdin.isatty.return_value = True
            with self.assertRaises(app.AppError) as raised:
                app.read_path_lines("-")

        self.assertEqual(raised.exception.exit_code, app.EXIT_USAGE)
        stdin.readlines.assert_not_called()

    def test_path_lines_ignore_blanks_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "file.txt"
            target.write_text("content")
            listing = root / "paths.txt"
            listing.write_text(f"\n  {target}  \n\n{target}\n")
            args = argparse.Namespace(
                paths=[], files_from=str(listing),
                default_current_directory=False,
            )
            roots = app.resolve_input_paths(args)

        self.assertEqual(roots, [target.resolve()])


def lose_compressed_flag(path, content):
    """Make path a compressed file that lost its flag. The data fork is empty
    and the compression metadata holds the content, compressed with zlib."""
    path.write_bytes(b"")
    header = struct.pack("<IIQ", 0x636d7066, 3, len(content))
    app.subprocess.run(
        ["xattr", "-wx", "com.apple.decmpfs",
         (header + app.zlib.compress(content)).hex(), str(path)], check=True)


class RepairTests(unittest.TestCase):
    def test_problems_on_disk_are_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lost = root / "lost.o"
            lose_compressed_flag(lost, b"object code" * 100)
            stale = root / "stale.txt"
            stale.write_bytes(b"own content" * 100)
            app.subprocess.run(
                ["xattr", "-wx", "com.apple.decmpfs",
                 struct.pack("<IIQ", 0x636d7066, 8, 1100).hex(), str(stale)],
                check=True)
            (root / "healthy.txt").write_bytes(b"fine" * 100)
            leftover = root / f"{app.ATOMIC_TEMP_PREFIX}orphan"
            leftover.mkdir()
            (leftover / "copy.o").write_bytes(b"copy")
            found = sorted(app.find_file_problems([root]))
            single = list(app.find_file_problems([lost]))

        self.assertEqual(found, [("leftover_copy", leftover),
                                 ("lost_flag", lost),
                                 ("stale_metadata", stale)])
        self.assertEqual(single, [("lost_flag", lost)])

    def test_restoring_the_flag_recovers_content_or_changes_nothing(self):
        content = b"object code" * 100
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lost = root / "lost.o"
            lose_compressed_flag(lost, content)
            hollow = root / "hollow.o"
            hollow.write_bytes(b"")
            # Metadata that points to a resource fork that does not exist.
            app.subprocess.run(
                ["xattr", "-wx", "com.apple.decmpfs",
                 struct.pack("<IIQ", 0x636d7066, 4, 5000).hex(), str(hollow)],
                check=True)
            self.assertEqual(lost.stat().st_size, 0)
            cache = app.StateCache(str(root / "state.sqlite"), readonly=True)
            preview = app.repair_files(cache, [root], True, ["lost_flag"])
            size_after_preview = lost.stat().st_size
            result = app.repair_files(cache, [root], False,
                                      app.REPAIR_KINDS)
            again = app.repair_files(cache, [root], False, ["lost_flag"])
            recovered = lost.read_bytes()
            hollow_stat = hollow.stat()

        self.assertEqual(preview["files_repaired"], 2)
        self.assertEqual(size_after_preview, 0)
        self.assertEqual(recovered, content)
        self.assertEqual(result["flags_restored"], 1)
        self.assertEqual(result["errors"], 1)
        failure = result["failures"][0]
        self.assertEqual(failure["path"], str(hollow))
        self.assertEqual(failure["error"], "repair_failed")
        self.assertNotIn("Errno", failure["message"])
        self.assertEqual(hollow_stat.st_size, 0)
        self.assertFalse(hollow_stat.st_flags & app.stat.UF_COMPRESSED)
        self.assertEqual(again["flags_restored"], 0)


class StateCacheTests(unittest.TestCase):
    def test_prune_removes_only_missing_file_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "state.sqlite")
            present = Path(directory, "present")
            missing = Path(directory, "missing")
            present.write_text("present")
            missing.write_text("missing")
            cache = app.StateCache(str(db))
            cache.mark(present, present.stat(), "policy", "compressed")
            cache.mark(missing, missing.stat(), "policy", "compressed")
            missing.unlink()

            preview = cache.prune_missing(dry_run=True)
            applied = cache.prune_missing()
            cache.close()

            self.assertEqual(preview["stale"], 1)
            self.assertEqual(preview["removed"], 0)
            self.assertEqual(applied["removed"], 1)
            self.assertEqual(app.inspect_state(db)["entries"], 1)

    def test_recompression_journal_is_durable_until_finished(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "state.sqlite")
            path = Path(directory, "file")
            path.write_text("content")
            cache = app.StateCache(str(db))
            cache.begin_recompression(path, "lzvn", "lzfse")
            cache.close()

            reopened = app.StateCache(str(db))
            pending = reopened.pending_recompressions()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["old_algorithm"], "lzvn")
            self.assertEqual(app.inspect_state(db)["pending_recompressions"], 1)
            reopened.finish_recompression(path)
            self.assertEqual(reopened.pending_recompressions(), [])
            reopened.close()

    def test_recovery_restores_an_interrupted_decompressed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "state.sqlite")
            path = Path(directory, "file")
            path.write_text("content")
            cache = app.StateCache(str(db))
            cache.begin_recompression(path, "lzvn", "lzfse")
            with mock.patch.object(
                    app, "compression_algorithm", side_effect=[None, "lzvn"]), \
                 mock.patch.object(app, "run_compressor") as compress:
                result = app.recover_recompressions(cache)

            compress.assert_called_once_with(path, "lzvn")
            self.assertEqual(result["recovered"], 1)
            self.assertEqual(cache.pending_recompressions(), [])
            cache.close()

    def test_older_databases_gain_method_and_time_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "state.sqlite")
            old = Path(directory, "old")
            new = Path(directory, "new")
            sibling = Path(directory + "-sibling")
            for path in (old, new):
                path.write_text("content")
            connection = app.sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE file_state (path TEXT PRIMARY KEY, "
                "device INTEGER, inode INTEGER, size INTEGER, "
                "mtime_ns INTEGER, policy TEXT, outcome TEXT)")
            for path in (old, sibling):
                connection.execute(
                    "INSERT INTO file_state VALUES (?, 1, 2, 3, 4, 'p', "
                    "'compressed')", (str(path),))
            connection.commit()
            connection.close()

            readonly = app.StateCache(str(db), readonly=True)
            before = readonly.entries(under=Path(directory))
            readonly.close()
            cache = app.StateCache(str(db))
            cache.mark(new, new.stat(), "p", "compressed", "replace")
            after = {Path(entry["path"]).name: entry
                     for entry in cache.entries("compressed", Path(directory))}
            rejected = cache.entries("ratio_rejected")
            cache.close()

        self.assertEqual([entry["path"] for entry in before], [str(old)])
        self.assertIsNone(before[0]["method"])
        self.assertIsNone(before[0]["recorded_at"])
        self.assertEqual(sorted(after), ["new", "old"])
        self.assertEqual(after["new"]["method"], "replace")
        self.assertIsInstance(after["new"]["recorded_at"], int)
        self.assertEqual(rejected, [])

    def test_reseat_selects_only_images_that_may_be_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = app.StateCache(str(Path(directory, "state.sqlite")))
            names = ("unknown", "since-boot", "before-boot", "replaced")
            for name in names:
                path = Path(directory, name)
                path.write_text(name)
                cache.mark(path, path.stat(), "p", "compressed",
                           "replace" if name == "replaced" else "in_place")
            cache.entries()
            with cache.conn:
                cache.conn.execute(
                    "UPDATE file_state SET recorded_at = NULL, method = NULL "
                    "WHERE path LIKE '%/unknown'")
                cache.conn.execute(
                    "UPDATE file_state SET recorded_at = 100 "
                    "WHERE path LIKE '%/before-boot'")
                cache.conn.execute(
                    "UPDATE file_state SET recorded_at = 900 "
                    "WHERE path LIKE '%/since-boot' OR path LIKE '%/replaced'")
            with mock.patch.object(app, "boot_time", return_value=500), \
                 mock.patch.object(app, "is_mach_o", return_value=True), \
                 mock.patch.object(app, "already_compressed",
                                   return_value=True), \
                 mock.patch.object(app, "reseat_file") as reseat:
                preview = app.reseat_images(cache, [Path(directory)], True,
                                            False)
                everything = app.reseat_images(cache, [Path(directory)], True,
                                               True)
                reseat.assert_not_called()
                applied = app.reseat_images(cache, [Path(directory)], False,
                                            False)
                again = app.reseat_images(cache, [Path(directory)], False,
                                          False)
            cache.close()

        def names_of(result):
            return sorted(Path(item["path"]).name for item in result["files"])

        self.assertEqual(names_of(preview), ["since-boot", "unknown"])
        self.assertEqual(names_of(everything), sorted(names))
        self.assertEqual(applied["files_reseated"], 2)
        self.assertEqual(reseat.call_count, 2)
        self.assertEqual(again["files_reseated"], 0)

    def test_recovery_drops_records_for_deleted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = app.StateCache(str(Path(directory, "state.sqlite")))
            cache.begin_recompression(Path(directory, "deleted"), "lzvn", "zlib")
            with mock.patch.object(app, "run_compressor") as compress:
                preview = app.recover_recompressions(cache, dry_run=True)
                pending_after_preview = len(cache.pending_recompressions())
                result = app.recover_recompressions(cache)
            remaining = cache.pending_recompressions()
            cache.close()

        compress.assert_not_called()
        self.assertEqual(preview["missing"], 1)
        self.assertEqual(pending_after_preview, 1)
        self.assertEqual((result["missing"], result["errors"]), (1, 0))
        self.assertEqual(result["operations"][0]["status"], "missing")
        self.assertEqual(remaining, [])

    def test_recovery_keeps_records_whose_directory_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = app.StateCache(str(Path(directory, "state.sqlite")))
            unmounted = Path(directory, "unmounted-volume", "file")
            cache.begin_recompression(unmounted, "lzvn", "zlib")
            kept = app.recover_recompressions(cache)
            pending_after_kept = len(cache.pending_recompressions())
            discarded = app.recover_recompressions(cache, discard_missing=True)
            remaining = cache.pending_recompressions()
            cache.close()

        self.assertEqual((kept["missing"], kept["errors"]), (0, 1))
        self.assertIn("--discard-missing", kept["failures"][0]["message"])
        self.assertEqual(pending_after_kept, 1)
        self.assertEqual((discarded["missing"], discarded["errors"]), (1, 0))
        self.assertEqual(remaining, [])

    def test_state_lock_refuses_a_second_holder(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory, "state.sqlite"))
            with app.StateLock(db):
                with self.assertRaises(app.AppError) as raised:
                    with app.StateLock(db):
                        pass
            with app.StateLock(db):
                pass

        self.assertEqual(raised.exception.error, "state_locked")
        self.assertEqual(raised.exception.exit_code, app.EXIT_CONFLICT)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.details["holder_pid"], os.getpid())

    def test_readonly_cache_reads_a_journal_left_in_the_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "state.sqlite")
            path = Path(directory, "file")
            path.write_text("content")
            # A writer that is not closed simulates a killed process. The
            # journal record is committed to the WAL, but there is no
            # checkpoint.
            interrupted = app.StateCache(str(db))
            interrupted.begin_recompression(path, "lzvn", "lzfse")

            readonly = app.StateCache(str(db), readonly=True)
            pending = readonly.pending_recompressions()
            readonly.close()
            interrupted.close()

        self.assertEqual([record["path"] for record in pending], [str(path)])

    def test_readonly_access_leaves_no_sidecars_and_quotes_the_path(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory, "odd#name?x")
            folder.mkdir()
            db = folder / "state.sqlite"
            path = Path(directory, "file")
            path.write_text("content")
            st = path.stat()
            writable = app.StateCache(str(db))
            writable.mark(path, st, "policy", "compressed")
            writable.close()

            readonly = app.StateCache(str(db), readonly=True)
            seen = readonly.seen(path, st, "policy")
            readonly.close()
            entries = app.inspect_state(db)["entries"]
            remaining = sorted(item.name for item in folder.iterdir())

        self.assertTrue(seen)
        self.assertEqual(entries, 1)
        self.assertEqual(remaining, ["state.sqlite"])

    def test_readonly_cache_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "state.sqlite")
            cache = app.StateCache(str(db), readonly=True)
            cache.close()

            self.assertFalse(db.exists())

    def test_readonly_cache_can_use_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "state.sqlite")
            path = Path(directory, "file")
            path.write_text("content")
            st = path.stat()
            writable = app.StateCache(str(db))
            writable.mark(path, st, "policy", "compressed")
            writable.close()

            readonly = app.StateCache(str(db), readonly=True)
            self.assertTrue(readonly.seen(path, st, "policy"))
            readonly.mark(path, st, "policy", "ignored")
            readonly.close()

    def test_cache_matches_path_and_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = app.StateCache(str(Path(directory, "state.sqlite")))
            path = Path(directory, "file")
            path.write_text("first")
            first = path.stat()
            cache.mark(path, first, "policy-a", "ratio_rejected")
            self.assertTrue(cache.seen(path, first, "policy-a"))
            self.assertFalse(cache.seen(path, first, "policy-b"))
            path.write_text("different size")
            self.assertFalse(cache.seen(path, path.stat(), "policy-a"))
            cache.close()

    def test_cache_flushes_buffered_rows_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory, "state.sqlite"))
            path = Path(directory, "file")
            path.write_text("content")
            st = path.stat()
            cache = app.StateCache(db)
            cache.mark(path, st, "policy", "compressed")
            cache.close()

            reopened = app.StateCache(db)
            self.assertTrue(reopened.seen(path, st, "policy"))
            reopened.close()


class ScannerTests(unittest.TestCase):
    def test_scanner_partitions_eligible_files_by_compression_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compressed = root / "compressed.txt"
            compressed.write_bytes(b"a" * 4096)
            uncompressed = root / "uncompressed.txt"
            uncompressed.write_bytes(b"b" * 4096)
            args = argparse.Namespace(
                cutoff_days=0, min_size=1, exclude_ext="", exclude_dir="",
                include_hardlinks=False, include_sparse=False,
                internal_paths=set(),
            )
            work = queue.Queue()
            stats = app.Stats()
            with mock.patch.object(
                    app, "already_compressed",
                    side_effect=lambda path: path == compressed):
                app.scanner(args, [root], work, stats, app.threading.Lock())

        self.assertEqual(stats.queued, 2)
        self.assertEqual(stats.already_compressed, 1)
        self.assertEqual(stats.not_compressed, 1)

    def test_scanner_excludes_internal_state_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_bytes(b"candidate" * 1024)
            state = root / "state.sqlite"
            wal = root / "state.sqlite-wal"
            state.write_bytes(b"state" * 1024)
            wal.write_bytes(b"wal" * 1024)
            fallback_directory = root / f"{app.ATOMIC_TEMP_PREFIX}orphan"
            fallback_directory.mkdir()
            (fallback_directory / "temporary.txt").write_bytes(
                b"temporary" * 1024
            )
            args = argparse.Namespace(
                cutoff_days=0,
                min_size=1,
                exclude_ext="",
                exclude_dir="",
                include_hardlinks=False,
                include_sparse=False,
                internal_paths={state, wal},
            )
            work = queue.Queue()
            stats = app.Stats()
            app.scanner(args, [root], work, stats, app.threading.Lock())
            paths = []
            while True:
                _, _, item = work.get()
                if item is None:
                    break
                paths.append(item.path)

        self.assertEqual(paths, [candidate])

    def test_scanner_reports_unreadable_roots_as_partial_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                cutoff_days=0,
                min_size=1,
                exclude_ext="",
                exclude_dir="",
                include_hardlinks=False,
                include_sparse=False,
                internal_paths=set(),
            )
            work = queue.Queue()
            stats = app.Stats()
            reporter = app.Reporter("json")
            denied = PermissionError("permission denied")
            with mock.patch.object(app.os, "scandir", side_effect=denied):
                app.scanner(
                    args, [root], work, stats, app.threading.Lock(), reporter
                )

        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.error_messages, {"permission_denied": 1})
        self.assertEqual(reporter.failures[0]["path"], str(root))
        self.assertFalse(reporter.failures[0]["retryable"])

    def test_scanner_reports_unexpected_io_errors_and_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                cutoff_days=0, min_size=1, exclude_ext="", exclude_dir="",
                include_hardlinks=False, include_sparse=False,
                internal_paths=set(),
            )
            work = queue.Queue()
            stats = app.Stats()
            reporter = app.Reporter("json")
            failure = OSError(5, "Input/output error")
            with mock.patch.object(app.os, "scandir", side_effect=failure):
                app.scanner(
                    args, [root], work, stats, app.threading.Lock(), reporter
                )

        self.assertEqual(stats.error_messages, {"scan_failed": 1})
        self.assertIsNone(work.get_nowait()[2])

    def test_scanner_abandons_a_full_queue_once_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.txt", "two.txt"):
                (root / name).write_bytes(b"content" * 1024)
            args = argparse.Namespace(
                cutoff_days=0, min_size=1, exclude_ext="", exclude_dir="",
                include_hardlinks=False, include_sparse=False,
                internal_paths=set(),
            )
            work = queue.Queue(maxsize=1)
            work.put("occupied")
            stop = app.threading.Event()
            stop.set()
            app.scanner(args, [root], work, app.Stats(),
                        app.threading.Lock(), None, stop)

        self.assertEqual(work.get_nowait(), "occupied")
        self.assertTrue(work.empty())

    def test_scanner_normalizes_excluded_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "photo.JPG").write_bytes(b"image" * 1024)
            (root / "notes.txt").write_bytes(b"notes" * 1024)
            args = argparse.Namespace(
                cutoff_days=0, min_size=1, exclude_ext="jpg, .PNG",
                exclude_dir="", include_hardlinks=False, include_sparse=False,
                internal_paths=set(),
            )
            stats = app.Stats()
            app.scanner(args, [root], queue.Queue(), stats,
                        app.threading.Lock())

        self.assertEqual(stats.skipped_excluded_ext, 1)
        self.assertEqual(stats.queued, 1)

    def test_exclusion_patterns_match_names_paths_and_subtrees(self):
        patterns = app.compile_exclusions(
            ["*.tmp", "/data/cache/uv", "build/*.json", "~/scratch/"]
        )
        home = os.path.expanduser("~")

        def excluded(path):
            return app.is_excluded(path, os.path.basename(path), patterns)

        self.assertTrue(excluded("/data/a/file.tmp"))
        self.assertTrue(excluded("/data/cache/uv"))
        self.assertTrue(excluded("/data/cache/uv/wheels/x.whl"))
        self.assertFalse(excluded("/data/cache/uv-other/x.whl"))
        self.assertTrue(excluded("/data/project/build/netlist.json"))
        self.assertFalse(excluded("/data/project/src/netlist.json"))
        self.assertTrue(excluded(f"{home}/scratch/notes.txt"))
        self.assertFalse(excluded("/data/a/file.txt"))

    def test_scanner_prunes_excluded_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "cache" / "uv").mkdir(parents=True)
            (root / "cache" / "uv" / "wheel.txt").write_bytes(b"w" * 4096)
            (root / "keep.txt").write_bytes(b"k" * 4096)
            (root / "drop.tmp").write_bytes(b"d" * 4096)
            args = argparse.Namespace(
                cutoff_days=0, min_size=1, exclude_ext="", exclude_dir="",
                include_hardlinks=False, include_sparse=False,
                internal_paths=set(),
                exclusions=app.compile_exclusions(
                    ["*.tmp", str(root / "cache" / "uv")]),
            )
            work = queue.Queue()
            stats = app.Stats()
            stats.track_roots([root])
            app.scanner(args, [root], work, stats, app.threading.Lock())
            queued = work.get_nowait()[2]

        self.assertEqual(queued.path, root / "keep.txt")
        self.assertEqual(queued.root, 0)
        self.assertEqual(stats.skipped_excluded, 2)
        self.assertEqual(stats.roots[0]["files_queued"], 1)

    def test_files_owned_by_others_fail_once_per_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.txt", "two.txt"):
                (root / name).write_bytes(b"content" * 1024)
            someone_else = os.geteuid() + 1

            def scan(dry_run):
                args = argparse.Namespace(
                    cutoff_days=0, min_size=1, exclude_ext="", exclude_dir="",
                    include_hardlinks=False, include_sparse=False,
                    internal_paths=set(), dry_run=dry_run,
                )
                stats = app.Stats()
                stats.track_roots([root])
                reporter = app.Reporter("json")
                with mock.patch.object(app.os, "geteuid",
                                       return_value=someone_else):
                    app.scanner(args, [root], queue.Queue(), stats,
                                app.threading.Lock(), reporter)
                return stats, reporter

            scanned, scan_reporter = scan(dry_run=True)
            applied, apply_reporter = scan(dry_run=False)

        self.assertEqual(scanned.skipped_not_owned, 2)
        self.assertEqual(scanned.queued, 0)
        self.assertEqual(scan_reporter.failures, [])
        self.assertEqual(applied.errors, 1)
        failure = apply_reporter.failures[0]
        self.assertEqual(failure["error"], "not_owner")
        self.assertEqual(failure["files"], 2)
        self.assertEqual(failure["path"], str(root))
        self.assertIn("sudo", failure["suggestion"])

    def test_scanner_filters_hardlinks_sparse_files_and_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "regular.txt"
            regular.write_bytes(b"regular" * 1024)
            linked = root / "linked.txt"
            linked.write_bytes(b"linked" * 1024)
            os.link(linked, root / "linked-again.txt")
            sparse = root / "sparse.dat"
            with sparse.open("wb") as stream:
                stream.truncate(1024 * 1024)
            (root / "image.jpg").write_bytes(b"image" * 1024)

            args = argparse.Namespace(
                cutoff_days=0,
                min_size=1,
                exclude_ext=".jpg",
                exclude_dir=".git",
                include_hardlinks=False,
                include_sparse=False,
                internal_paths=set(),
            )
            work = queue.Queue()
            stats = app.Stats()
            app.scanner(args, [root], work, stats, app.threading.Lock())

            candidates = []
            while True:
                _, _, item = work.get()
                if item is None:
                    break
                candidates.append(item.path)

        self.assertEqual(candidates, [regular])
        self.assertEqual(stats.skipped_hardlink, 2)
        self.assertEqual(stats.skipped_sparse, 1)
        self.assertEqual(stats.skipped_excluded_ext, 1)

    def test_candidate_priority_favors_large_cold_files(self):
        now = 4 * 365 * 86400
        hot = SimpleNamespace(st_blocks=200, st_size=100_000, st_mtime=now)
        cold = SimpleNamespace(st_blocks=200, st_size=100_000, st_mtime=0)
        large = SimpleNamespace(st_blocks=400, st_size=200_000, st_mtime=now)
        self.assertGreater(app.candidate_priority(cold, now),
                           app.candidate_priority(hot, now))
        self.assertGreater(app.candidate_priority(large, now),
                           app.candidate_priority(hot, now))


class SamplingTests(unittest.TestCase):
    def test_stratified_sampler_reads_beginning_middle_and_end(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "regions.bin")
            path.write_bytes(b"A" * 100 + b"B" * 100 + b"C" * 100)
            segments = list(app.StratifiedSampler(90)._segments(path))

        self.assertEqual([len(segment) for segment in segments], [30, 30, 30])
        self.assertEqual([segment[:1] for segment in segments], [b"A", b"B", b"C"])

    def test_sampler_reserves_read_bandwidth_before_each_segment(self):
        limiter = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "regions.bin")
            path.write_bytes(b"A" * 300)
            list(app.StratifiedSampler(90, limiter)._segments(path))

        self.assertEqual(
            limiter.throttle.call_args_list,
            [mock.call(30), mock.call(30), mock.call(30)],
        )

    def test_large_files_are_sampled_in_many_regions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "disk.img")
            # A zero-filled start, then incompressible data, as in a disk
            # image.
            # With three regions, two samples are in the zeros.
            path.write_bytes(b"\0" * (1 << 20) + os.urandom(7 << 20))
            sampler = app.StratifiedSampler(1 << 20)
            segments = list(sampler._segments(path))
            result = sampler.probe(path, ["zlib"])["zlib"]

        self.assertEqual(len(segments), app.MAX_SAMPLE_REGIONS)
        self.assertEqual(sum(map(len, segments)), 1 << 20)
        self.assertGreater(result.ratio, 0.8)

    def test_full_file_sampling_reads_bounded_chunks(self):
        limiter = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(app, "FULL_READ_CHUNK_BYTES", 100):
            path = Path(directory, "whole.bin")
            path.write_bytes(b"A" * 250)
            sampler = app.StratifiedSampler(0, limiter)
            segments = list(sampler._segments(path))
            result = sampler.probe(path, ["zlib"])["zlib"]

        self.assertEqual([len(segment) for segment in segments], [100, 100, 50])
        self.assertEqual(
            limiter.throttle.call_args_list[:3],
            [mock.call(100), mock.call(100), mock.call(50)],
        )
        self.assertEqual(result.sampled_bytes, 250)
        self.assertEqual(result.confidence, 1.0)

    def test_max_profile_chooses_materially_better_archive_codec(self):
        args = argparse.Namespace(
            algo="auto", profile="max", cold_days=30, archive_days=60,
            min_codec_gain=0.05,
        )
        old = SimpleNamespace(st_mtime=0, st_size=4096)
        sampler = ProbeSampler({"lzvn": 0.80, "lzfse": 0.70, "zlib": 0.60})
        result = app.choose_algorithm(args, Path("archive"), old, sampler)

        self.assertEqual(sampler.requested, ["lzvn", "lzfse", "zlib"])
        self.assertEqual(result.algorithm, "zlib")

    def test_fast_profile_only_probes_lzvn(self):
        args = argparse.Namespace(
            algo="auto", profile="fast", cold_days=0, archive_days=0,
            min_codec_gain=0.0,
        )
        sampler = ProbeSampler({"lzvn": 0.50})
        result = app.choose_algorithm(
            args, Path("hot"), SimpleNamespace(st_mtime=0, st_size=4096), sampler
        )

        self.assertEqual(sampler.requested, ["lzvn"])
        self.assertEqual(result.algorithm, "lzvn")

    def test_benchmark_rejects_slow_codec_with_low_incremental_value(self):
        args = argparse.Namespace(
            algo="auto", profile="balanced", cold_days=0, archive_days=0,
            min_codec_gain=0.05, min_savings_per_second=2 << 20,
            benchmark_data={
                "codecs": {
                    "lzvn": {"throughput_mib_s": 200.0},
                    "lzfse": {"throughput_mib_s": 10.0},
                }
            },
        )
        size = 100 << 20
        old = SimpleNamespace(st_mtime=0, st_size=size)
        sampler = ProbeSampler({"lzvn": 0.80, "lzfse": 0.70})

        result = app.choose_algorithm(args, Path("archive"), old, sampler)

        self.assertEqual(result.algorithm, "lzvn")

    def test_benchmark_allows_slow_codec_when_incremental_value_is_high(self):
        args = argparse.Namespace(
            algo="auto", profile="balanced", cold_days=0, archive_days=0,
            min_codec_gain=0.05, min_savings_per_second=1 << 20,
            benchmark_data={
                "codecs": {
                    "lzvn": {"throughput_mib_s": 200.0},
                    "lzfse": {"throughput_mib_s": 10.0},
                }
            },
        )
        size = 100 << 20
        old = SimpleNamespace(st_mtime=0, st_size=size)
        sampler = ProbeSampler({"lzvn": 0.80, "lzfse": 0.70})

        result = app.choose_algorithm(args, Path("archive"), old, sampler)

        self.assertEqual(result.algorithm, "lzfse")


if __name__ == "__main__":
    unittest.main()
