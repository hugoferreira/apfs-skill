import fcntl
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "scripts"
CLI = ROOT / "apfs_compress_plus.py"
# Any regular, uncompressed file of the repository serves as a fixture.
README = ROOT.parent / "README.md"


# Only system directories. Homebrew, which has afsctool, is not included.
SYSTEM_PATH = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def run_cli(*arguments, input_text=None, env=None):
    return subprocess.run(
        [sys.executable, str(CLI), *map(str, arguments)],
        cwd=ROOT,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


class CLIContractTests(unittest.TestCase):
    def test_top_level_help_exposes_noun_verb_discovery(self):
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for noun in ("file", "codec", "state", "volume"):
            self.assertIn(noun, result.stdout)
        self.assertIn("Exit codes:", result.stdout)

        detail = run_cli("file", "compress", "--help")
        self.assertEqual(detail.returncode, 0)
        self.assertIn("--dry-run", detail.stdout)
        self.assertIn("--json", detail.stdout)
        self.assertIn("examples:", detail.stdout)
        self.assertIn("Output format (default: text)", detail.stdout)

        file_help = run_cli("file", "--help")
        for verb in ("inspect", "scan", "compress", "decompress"):
            self.assertIn(verb, file_help.stdout)

        state_help = run_cli("state", "--help")
        for verb in ("inspect", "recover", "prune", "clear"):
            self.assertIn(verb, state_help.stdout)

        version = run_cli("--version")
        self.assertEqual(version.returncode, 0)
        self.assertIn(app_version(), version.stdout)

    def test_every_command_documents_examples_and_exit_codes(self):
        commands = (
            ("file", "inspect"), ("file", "scan"), ("file", "compress"),
            ("file", "decompress"), ("file", "repair"),
            ("codec", "benchmark"), ("state", "inspect"), ("state", "list"),
            ("state", "recover"), ("state", "prune"), ("state", "clear"),
            ("volume", "inspect"), ("system", "doctor"),
        )
        for command in commands:
            with self.subTest(command=command):
                result = run_cli(*command, "--help")
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertIn("examples:", result.stdout)
                self.assertIn("Exit codes:", result.stdout)
                self.assertIn("--json", result.stdout)
                self.assertIn(" ".join(command), result.stdout)

    def test_budget_stop_is_reported_as_incomplete_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.txt", "two.txt", "three.txt"):
                (root / name).write_text("compressible\n" * 4000)
            common = ("file", "scan", root, "--cutoff-days", "0",
                      "--min-size", "1", "--ratio", "1", "--space-target", "1",
                      "--state-db", root / "state.sqlite")
            structured = run_cli(*common, "--json")
            text = run_cli(*common)
            unlimited = run_cli("file", "scan", root, "--cutoff-days", "0",
                                "--min-size", "1", "--ratio", "1",
                                "--state-db", root / "state.sqlite", "--json")

        self.assertEqual(structured.returncode, 0, structured.stderr)
        document = json.loads(structured.stdout)
        self.assertTrue(document["ok"])
        self.assertFalse(document["complete"])
        self.assertEqual(document["files_remaining"], 2)
        self.assertEqual(document["stopped_by"], "space_target")
        self.assertIn("Stopped by the space target: 2 files remain",
                      text.stdout)
        finished = json.loads(unlimited.stdout)
        self.assertTrue(finished["complete"])
        self.assertIsNone(finished["stopped_by"])

    def test_summary_reports_coverage_and_target_volumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "small.txt").write_text("compressible\n" * 4000)
            common = ("file", "scan", root, "--cutoff-days", "0",
                      "--min-size", "1", "--ratio", "1",
                      "--state-db", root / "state.sqlite")
            structured = run_cli(*common, "--json")
            text = run_cli(*common)

        document = json.loads(structured.stdout)
        coverage = {bucket["coverage"]: bucket
                    for bucket in document["estimate_by_coverage"]}
        self.assertEqual(list(coverage),
                         ["full", "10-100%", "1-10%", "under 1%"])
        self.assertEqual(coverage["full"]["files"], 1)
        self.assertEqual(coverage["full"]["estimated_saved_bytes"],
                         document["estimated_bytes_saved"])
        volume = document["target_volumes"][0]
        for key in ("free_bytes_before", "free_bytes_after",
                    "local_snapshots"):
            self.assertIsInstance(volume[key], int)
        self.assertFalse(document["savings_deferred"])
        self.assertIn("fully sampled:", text.stdout)
        self.assertIn("100% sampled)", text.stdout)

    def test_state_list_and_repair_work_without_a_database(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.sqlite")
            listed = run_cli("state", "list", "--state-db", state,
                             "--outcome", "compressed", "--json")
            plain = run_cli("state", "list", "--state-db", state, "--plain")
            reseat = run_cli("file", "repair", directory, "--state-db", state,
                             "--json")
            state_exists = state.exists()

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["entries"], 0)
        self.assertEqual(json.loads(listed.stdout)["files"], [])
        self.assertEqual(plain.stdout, "")
        self.assertEqual(reseat.returncode, 0, reseat.stderr)
        self.assertEqual(json.loads(reseat.stdout)["files_repaired"], 0)
        self.assertFalse(state_exists)

    def test_exclusions_live_files_and_per_path_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for folder in ("first", "second", "second/vendor"):
                (root / folder).mkdir()
            text = "compressible\n" * 4000
            for name in ("first/a.txt", "first/app.sqlite", "first/run.log",
                         "second/b.txt", "second/skip.tmp",
                         "second/vendor/c.txt"):
                (root / name).write_text(text)
            patterns = root / "patterns.txt"
            patterns.write_text(f"\n{root}/second/vendor\n")
            result = run_cli(
                "file", "scan", root / "first", root / "second",
                "--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                "--exclude", "*.tmp", "--exclude-from", patterns,
                "--state-db", root / "state.sqlite", "--details", "--json",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        planned = sorted(Path(record["path"]).name
                         for record in document["file_records"])
        self.assertEqual(planned, ["a.txt", "b.txt"])
        self.assertEqual(document["skipped_excluded"], 2)
        self.assertEqual(document["skipped_excluded_ext"], 2)
        self.assertEqual(
            [(Path(entry["path"]).name, entry["files_compressed"])
             for entry in document["roots"]],
            [("first", 1), ("second", 1)],
        )
        self.assertEqual(
            sum(entry["space_saved"] for entry in document["roots"]),
            document["space_saved"],
        )
        self.assertEqual({record["method"]
                          for record in document["file_records"]},
                         {"in_place"})

    def test_doctor_finds_a_lost_flag_and_repair_fixes_it(self):
        content = b"object code" * 100
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lost = root / "lost.o"
            lost.write_bytes(b"")
            header = struct.pack("<IIQ", 0x636d7066, 3, len(content))
            subprocess.run(
                ["xattr", "-wx", "com.apple.decmpfs",
                 (header + zlib.compress(content)).hex(), str(lost)],
                check=True)
            state = ("--state-db", root / "state.sqlite")
            before = run_cli("system", "doctor", root, *state, "--json")
            lines = run_cli("system", "doctor", root, *state,
                            "--format", "jsonl")
            plain = run_cli("system", "doctor", root, *state, "--plain")
            preview = run_cli("file", "repair", root, *state, "--dry-run",
                              "--plain")
            size_after_preview = lost.stat().st_size
            repaired = run_cli("file", "repair", root, *state, "--json")
            recovered = lost.read_bytes()
            after = run_cli("system", "doctor", root, *state, "--json")
            state_exists = (root / "state.sqlite").exists()

        def checks(result):
            return {check["check"]: check
                    for check in json.loads(result.stdout)["checks"]}

        self.assertEqual(before.returncode, 1)
        finding = checks(before)["lost_flag"]
        self.assertEqual(finding["status"], "problem")
        self.assertEqual(finding["paths"], [str(lost)])
        self.assertIn("file repair", finding["suggestion"])
        rows = [json.loads(line) for line in lines.stdout.splitlines()]
        self.assertEqual({row["record_type"] for row in rows[:-1]}, {"check"})
        self.assertEqual(rows[-1]["record_type"], "summary")
        self.assertFalse(rows[-1]["ok"])
        self.assertIn("problem\tlost_flag", plain.stdout.splitlines())
        self.assertEqual(preview.stdout.strip(), str(lost))
        self.assertEqual(size_after_preview, 0)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        document = json.loads(repaired.stdout)
        self.assertEqual(document["flags_restored"], 1)
        self.assertEqual(document["files"][0]["repair"], "flag_restored")
        self.assertEqual(recovered, content)
        self.assertEqual(checks(after)["lost_flag"]["status"], "ok")
        self.assertFalse(state_exists)

    def test_resource_without_a_verb_prints_its_help(self):
        for noun, verb in (("file", "decompress"), ("state", "prune"),
                           ("codec", "benchmark"), ("volume", "inspect")):
            with self.subTest(noun=noun):
                result = run_cli(noun)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertIn(verb, result.stdout)

    def test_dry_run_reports_the_invoked_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            result = run_cli(
                "file", "compress", candidate, "--dry-run", "--json",
                "--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                "--state-db", root / "state.sqlite",
            )
            leftovers = sorted(item.name for item in root.iterdir())

        document = json.loads(result.stdout)
        self.assertEqual(document["command"], "file compress")
        self.assertTrue(document["dry_run"])
        self.assertEqual(leftovers, ["candidate.txt"])

    def test_concurrent_mutation_of_one_state_database_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            state = root / "state.sqlite"
            common = (candidate, "--backend", "ditto", "--cutoff-days", "0", "--min-size", "1",
                      "--ratio", "1", "--state-db", state, "--json")
            with open(f"{state}.lock", "a+") as holder:
                fcntl.flock(holder, fcntl.LOCK_EX)
                compress = run_cli("file", "compress", *common)
                prune = run_cli("state", "prune", "--state-db", state,
                                "--yes", "--json")
                scan = run_cli("file", "scan", *common)
            state_exists = state.exists()
            released = run_cli("state", "prune", "--state-db", state,
                               "--yes", "--json")

        for blocked in (compress, prune):
            self.assertEqual(blocked.returncode, 5)
            error = json.loads(blocked.stdout)
            self.assertEqual(error["error"], "state_locked")
            self.assertTrue(error["retryable"])
        self.assertFalse(state_exists)
        self.assertEqual(scan.returncode, 0, scan.stderr)
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_flags_are_offered_only_where_they_apply(self):
        for arguments, flag in (
                (("codec", "benchmark", "--field", "path"), "--field"),
                (("state", "inspect", "--details", "--json"), "--details")):
            with self.subTest(arguments=arguments):
                result = run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn(flag, result.stderr)

    def test_abbreviated_flags_are_rejected_with_a_correction(self):
        result = run_cli("file", "scan", "--dry", "--json")

        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stdout)
        self.assertEqual(error["error"], "usage_error")
        self.assertIn("did you mean '--dry-run'?", error["suggestion"])
        self.assertIn("file scan --help", error["suggestion"])

    def test_mistyped_commands_are_not_treated_as_paths(self):
        resource = run_cli("fiel", "scan", ROOT)
        verb = run_cli("file", "scna", ROOT)

        self.assertEqual(resource.returncode, 2)
        self.assertEqual(resource.stdout, "")
        self.assertIn("unknown resource 'fiel'", resource.stderr)
        self.assertIn("did you mean 'file'?", resource.stderr)
        self.assertEqual(verb.returncode, 2)
        self.assertIn("did you mean 'scan'?", verb.stderr)
        self.assertIn("file --help", verb.stderr)

    def test_missing_paths_are_identified_without_python_error_text(self):
        for command in (("file", "inspect"), ("volume", "inspect"),
                        ("file", "decompress", "--dry-run")):
            with self.subTest(command=command):
                result = run_cli(*command, "/definitely/not/here", "--json")
                self.assertEqual(result.returncode, 3)
                error = json.loads(result.stdout)
                self.assertEqual(error["error"], "not_found")
                self.assertEqual(error["path"], "/definitely/not/here")
                self.assertNotIn("Errno", error["message"])

        listed = run_cli("file", "scan", "--files-from", "/not/here", "--json")
        self.assertEqual(listed.returncode, 3)
        self.assertEqual(json.loads(listed.stdout)["path"], "/not/here")

    def test_deprecated_forms_still_work_and_warn_on_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            common = ("--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                      "--state-db", root / "state.sqlite")
            legacy = run_cli(candidate, "--dry-run", "--json", *common)
            alias = run_cli("file", "scan", candidate, "--output", "json",
                            "--yes", *common)
            canonical = run_cli("file", "compress", candidate, "-n", "--json",
                                *common)

        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(json.loads(legacy.stdout)["command"], "file scan")
        self.assertIn("use 'file scan PATH'", legacy.stderr)
        self.assertEqual(alias.returncode, 0, alias.stderr)
        self.assertTrue(json.loads(alias.stdout)["ok"])
        self.assertIn("--output is deprecated; use --format", alias.stderr)
        self.assertIn("--yes has no effect on file scan", alias.stderr)
        self.assertEqual(canonical.returncode, 0, canonical.stderr)
        self.assertTrue(json.loads(canonical.stdout)["dry_run"])
        self.assertEqual(canonical.stderr, "")

    def test_json_stdout_is_a_single_machine_readable_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            result = run_cli(
                "file", "scan", candidate,
                "--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                "--state-db", root / "state.sqlite", "--json",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertTrue(document["ok"])
        self.assertEqual(document["command"], "file scan")
        self.assertIsInstance(document["space_saved"], int)
        self.assertIsInstance(document["files_atomic_fallback"], int)
        self.assertEqual(document["failures"], [])
        self.assertEqual(
            document["files_already_compressed"] +
            document["files_not_compressed"],
            document["files_queued"],
        )
        self.assertEqual(
            document["files_suggested_not_compressed"] +
            document["files_suggested_already_compressed"],
            document["files_suggested"],
        )
        self.assertTrue(document["suggestions_complete"])

    def test_default_output_is_human_readable_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            result = run_cli(
                "file", "scan", candidate,
                "--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                "--state-db", root / "state.sqlite",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("Compression scan complete\n"))
        self.assertIn("Already compressed:", result.stdout)
        self.assertIn("Suggested:", result.stdout)
        self.assertIn("Estimated savings:", result.stdout)
        self.assertNotIn("\r", result.stderr)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)

    def test_scan_does_not_create_state_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            state = root / "state.sqlite"
            result = run_cli(
                "file", "scan", candidate,
                "--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                "--state-db", state, "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state.exists())

    def test_default_errors_are_human_readable_on_stderr(self):
        result = run_cli("file", "scan", "--ratio", "2")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Error [usage_error]", result.stderr)

    def test_jsonl_streams_flat_file_records_then_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            result = run_cli(
                "file", "scan", candidate,
                "--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                "--state-db", root / "state.sqlite", "--output", "jsonl",
            )

        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows[0]["record_type"], "file")
        self.assertEqual(rows[0]["status"], "planned")
        self.assertEqual(rows[-1]["record_type"], "summary")

    def test_usage_and_not_found_errors_have_distinct_codes_and_json(self):
        usage = run_cli("file", "scan", "--ratio", "2", "--json")
        self.assertEqual(usage.returncode, 2)
        self.assertEqual(json.loads(usage.stdout)["error"], "usage_error")
        self.assertIn("usage_error", usage.stderr)

        missing = run_cli("file", "scan", "/definitely/not/here", "--json")
        self.assertEqual(missing.returncode, 3)
        error = json.loads(missing.stdout)
        self.assertEqual(error["error"], "not_found")
        self.assertEqual(error["path"], "/definitely/not/here")

    def test_state_clear_requires_confirmation_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.sqlite")
            state.write_bytes(b"state")
            refused = run_cli("state", "clear", "--state-db", state, "--json")
            self.assertEqual(refused.returncode, 2)
            self.assertTrue(state.exists())
            self.assertEqual(json.loads(refused.stdout)["error"],
                             "confirmation_required")

            first = run_cli("state", "clear", "--state-db", state,
                            "--yes", "--json")
            second = run_cli("state", "clear", "--state-db", state,
                             "--yes", "--json")

        self.assertEqual(first.returncode, 0)
        self.assertTrue(json.loads(first.stdout)["changed"])
        self.assertEqual(second.returncode, 0)
        self.assertFalse(json.loads(second.stdout)["changed"])

    def test_state_recover_and_prune_require_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.sqlite")
            for verb in ("recover", "prune"):
                refused = run_cli(
                    "state", verb, "--state-db", state, "--json"
                )
                preview = run_cli(
                    "state", verb, "--state-db", state, "--dry-run", "--json"
                )

                self.assertEqual(refused.returncode, 2)
                self.assertEqual(
                    json.loads(refused.stdout)["error"],
                    "confirmation_required",
                )
                self.assertEqual(preview.returncode, 0, preview.stderr)
                self.assertFalse(state.exists())

    def test_jsonl_errors_have_a_discriminator_and_retryability(self):
        result = run_cli(
            "file", "scan", "/definitely/not/here", "--format", "jsonl"
        )

        self.assertEqual(result.returncode, 3)
        error = json.loads(result.stdout)
        self.assertEqual(error["record_type"], "error")
        self.assertIsInstance(error["retryable"], bool)

    def test_compress_requires_an_explicit_target(self):
        result = run_cli("file", "compress", "--json")

        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stdout)
        self.assertEqual(error["error"], "empty_input")
        self.assertIn("provide paths", error["suggestion"])

    def test_compress_requires_the_selected_backend_but_scan_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            common = (
                candidate, "--cutoff-days", "0", "--min-size", "1",
                "--ratio", "1", "--state-db", root / "state.sqlite", "--json",
            )
            compress = run_cli("file", "compress", *common, env=SYSTEM_PATH)
            scan = run_cli("file", "scan", *common, env=SYSTEM_PATH)
            state_exists = (root / "state.sqlite").exists()

        self.assertEqual(compress.returncode, 6)
        error = json.loads(compress.stdout)
        self.assertEqual(error["error"], "unsupported_backend")
        self.assertEqual(error["tool"], "afsctool")
        self.assertIn("--backend ditto", error["suggestion"])
        self.assertFalse(state_exists)
        self.assertEqual(scan.returncode, 0, scan.stderr)
        self.assertEqual(json.loads(scan.stdout)["tool"], "afsctool")

    def test_file_inspect_reports_actual_compression_state(self):
        result = run_cli("file", "inspect", README, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        item = document["files"][0]
        self.assertEqual(item["path"], str(README.resolve()))
        self.assertIsInstance(item["compressed"], bool)
        self.assertIsInstance(item["logical_bytes"], int)
        self.assertIsInstance(item["allocated_bytes"], int)

    def test_file_decompress_requires_confirmation(self):
        result = run_cli("file", "decompress", README, "--json")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout)["error"], "confirmation_required"
        )

    def test_file_decompress_dry_run_is_jsonl_composable(self):
        result = run_cli(
            "file", "decompress", README, "--dry-run", "--format", "jsonl"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(records[0]["record_type"], "file")
        self.assertEqual(records[-1]["record_type"], "summary")

    def test_scan_and_saved_plan_skip_shared_apfs_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            clone = root / "clone.txt"
            plan = root / "plan.json"
            state = root / "state.sqlite"
            content = b"compressible text\n" * 65536
            source.write_bytes(content)
            options = ("--cutoff-days", "0", "--min-size", "1",
                       "--ratio", "1", "--state-db", state, "--json")

            before = run_cli("file", "scan", source, "--plan", plan, *options)
            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertEqual(json.loads(before.stdout)["files_suggested"], 1)
            subprocess.run(["/bin/cp", "-c", source, clone], check=True)
            def check_scan():
                scan = run_cli("file", "scan", source, clone, *options)
                self.assertEqual(scan.returncode, 0, scan.stderr)
                result = json.loads(scan.stdout)
                self.assertEqual(result["skipped_shared_blocks"], 2)
                self.assertEqual(result["estimated_bytes_saved"], 0)

            check_scan()
            applied = run_cli(
                "file", "compress", "--from-plan", plan,
                "--backend", "ditto", "--state-db", state, "--json",
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            result = json.loads(applied.stdout)
            self.assertEqual(result["skipped_shared_blocks"], 1)
            self.assertEqual(result["files_compressed"], 0)
            self.assertEqual(source.read_bytes(), content)
            self.assertEqual(clone.read_bytes(), content)
            with clone.open("r+b") as stream:
                stream.write(b"X")
            check_scan()

    def test_scan_plan_can_be_applied_without_resampling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            plan = root / "plan.json"
            state = root / "state.sqlite"
            scanned = run_cli(
                "file", "scan", candidate, "--cutoff-days", "0",
                "--min-size", "1", "--ratio", "1", "--state-db", state,
                "--plan", plan, "--json",
            )
            applied = run_cli(
                "file", "compress", "--from-plan", plan, "--dry-run",
                "--state-db", state, "--json",
            )
            persisted = json.loads(plan.read_text())
            state_exists = state.exists()

        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        self.assertEqual(persisted["plan_version"], 1)
        self.assertEqual(len(persisted["files"]), 1)
        self.assertFalse(state_exists)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        document = json.loads(applied.stdout)
        self.assertEqual(document["applied_plan"]["files"], 1)
        self.assertEqual(document["sampled_bytes"], 0)

    def test_plan_apply_detects_changed_file_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            plan = root / "plan.json"
            scanned = run_cli(
                "file", "scan", candidate, "--cutoff-days", "0",
                "--min-size", "1", "--ratio", "1", "--plan", plan,
                "--state-db", root / "state.sqlite", "--json",
            )
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            candidate.write_text("changed content\n" * 2000)
            applied = run_cli(
                "file", "compress", "--from-plan", plan, "--dry-run",
                "--state-db", root / "state.sqlite", "--json",
            )

        self.assertEqual(applied.returncode, 5, applied.stderr)
        document = json.loads(applied.stdout)
        self.assertEqual(document["conflicts"], 1)
        self.assertEqual(document["failures"][0]["error"], "plan_conflict")

    def test_stdin_batching_and_quiet_output_are_pipe_friendly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for name in ("one.txt", "two.txt"):
                path = root / name
                path.write_text(name * 1000)
                paths.append(path)
            result = run_cli(
                "file", "scan", "--files-from", "-",
                "--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                "--state-db", root / "state.sqlite", "--quiet",
                input_text="".join(f"{path}\n" for path in paths),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()),
                         {str(path.resolve()) for path in paths})
        self.assertIn("--quiet path projection is deprecated", result.stderr)

    def test_closed_stdout_ends_the_run_quietly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # There are more files than the work queue can hold. Thus, a run
            # that continues to scan after its consumer stops cannot drain
            # the queue.
            for index in range(1200):
                (root / f"{index}.txt").write_text("compressible\n" * 20)
            process = subprocess.Popen(
                [sys.executable, str(CLI), "file", "scan", str(root),
                 "--cutoff-days", "0", "--min-size", "1", "--ratio", "1",
                 "--state-db", str(root / "state.sqlite"),
                 "--format", "jsonl"],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            first = process.stdout.readline()
            process.stdout.close()
            try:
                returncode = process.wait(timeout=30)
            finally:
                process.kill()
            stderr = process.stderr.read()
            process.stderr.close()

        self.assertEqual(json.loads(first)["record_type"], "file")
        self.assertEqual(returncode, 1)
        self.assertEqual(stderr, "")

    def test_plain_and_format_are_canonical_machine_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.txt"
            candidate.write_text("compressible\n" * 1000)
            common = (
                "file", "scan", candidate, "--cutoff-days", "0",
                "--min-size", "1", "--ratio", "1",
                "--state-db", root / "state.sqlite",
            )
            plain = run_cli(*common, "--plain")
            structured = run_cli(*common, "--format", "json")

        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout.strip(), str(candidate.resolve()))
        self.assertTrue(json.loads(structured.stdout)["ok"])

    def test_conflicting_output_modes_are_rejected(self):
        result = run_cli("file", "scan", "--json", "--quiet")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "usage_error")


def app_version():
    import apfs_compress_plus as app
    return app.VERSION


if __name__ == "__main__":
    unittest.main()
