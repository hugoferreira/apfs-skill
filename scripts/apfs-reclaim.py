#!/usr/bin/env python3
"""Preview or apply transparent compression followed by APFS deduplication."""
import argparse
from contextlib import ExitStack, contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
from apfs_common import Native, atomic_json, human_bytes

SCRIPTS = Path(__file__).resolve().parent
JSON_OUTPUT = "--json" in sys.argv[1:]


class Usage(argparse.ArgumentParser):
    def error(self, message):
        if JSON_OUTPUT:
            print(json.dumps({"schema_version": 1, "ok": False,
                              "error": "invalid_arguments", "message": message}))
        super().error(message)


def progress(message):
    print(message, file=sys.stderr, flush=True)


def newer_python():
    """Use a suitable Python even if an SDK shadows it on PATH."""
    directories = [Path(directory) for directory in os.get_exec_path()]
    directories.extend((Path("/opt/homebrew/bin"), Path("/usr/local/bin")))
    candidates = dict.fromkeys(
        path for directory in directories
        for path in [directory / "python3", *directory.glob("python3.[1-9]*")]
        if path.name == "python3" or path.name.removeprefix("python3.").isdigit()
    )
    for path in dict.fromkeys([Path(sys.executable), *candidates]):
        if path.is_file() and os.access(path, os.X_OK):
            probe = subprocess.run(
                [str(path.absolute()), "-E", "-B", "-c",
                 "import sys; sys.exit(sys.version_info < (3, 12))"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                return str(path.absolute())
    return None


@contextmanager
def directory_lock(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"another APFS operation holds a lock on {path}") from error
        yield
    finally:
        os.close(fd)


def bind_state(state, identity, stack):
    state.mkdir(parents=True, exist_ok=True)
    stack.enter_context(directory_lock(state))
    descriptor = state / "reclaim-identity.json"
    if descriptor.exists():
        if json.loads(descriptor.read_text()) != identity:
            raise ValueError(f"state belongs to a different APFS tree: {state}")
    else:
        atomic_json(descriptor, identity)


def run_phase(label, args, destination, streaming=False):
    progress(label)
    with destination.open("w") as report, subprocess.Popen(
            args, stdin=subprocess.DEVNULL, text=True,
            stdout=subprocess.PIPE if streaming else report) as child:
        try:
            if streaming:
                count, updated, result = 0, time.monotonic(), {}
                for line in child.stdout:
                    report.write(line)
                    item = json.loads(line)
                    if item.get("record_type") == "file":
                        count += 1
                        if time.monotonic() - updated >= 5:
                            progress(f"  {count:,} files assessed")
                            updated = time.monotonic()
                    else:
                        result = item
            code = child.wait()
        except KeyboardInterrupt:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
            pending, _ = child.communicate()
            if pending:
                report.write(pending)
            raise
    if streaming:
        return code, result
    return code, json.loads(destination.read_text()) if destination.stat().st_size else {}


def report_result(result):
    if JSON_OUTPUT:
        print(json.dumps(result))
        return
    for stage in ("compression", "dedupe"):
        if stage in result:
            summary = result[stage]
            count = summary.get("files_compressed" if stage == "compression" else "cloned_files", 0)
            if result["dry_run"]:
                count = summary.get("files_suggested" if stage == "compression" else "planned_files", 0)
            size = summary.get("space_saved", summary.get(
                "estimated_physical_bytes" if result["dry_run"] else "estimated_applied_physical_bytes", 0))
            print(f"{stage.capitalize()}  {count:,} files, {human_bytes(size)}"
                  + (" estimated" if result["dry_run"] or stage == "dedupe" else "")
                  + " saved")
            if stage == "compression" and summary.get("errors"):
                print(f"  {summary['errors']:,} compression candidates could not be replaced")
            if stage == "dedupe":
                skipped = summary.get("files_skipped", 0) + summary.get("provenance_unmatched", 0)
                if skipped:
                    print(f"  {skipped:,} duplicates skipped")
    if "after" in result and result["after"]:
        before = result["before"]["estimated_physical"]
        after = result["after"]["estimated_physical"]
        print(f"Estimated tree allocation  {human_bytes(before)} → {human_bytes(after)}")
    missing = result.get("before", {}).get("missing_compression_flag_files", 0)
    if missing:
        print(f"Pre-existing missing compression flags  {missing:,} files")
    if result.get("reports"):
        print(f"Reports  {result['reports']}")


def main():
    parser = Usage(description=__doc__, allow_abbrev=False, epilog=(
        "Preview by default. --apply is non-interactive and writes a dedupe recovery "
        "journal in ROOT. Repeat --apply with the printed --state-dir to keep the "
        "same cache. Exit 0 for success, 1 for failure or partial execution, "
        "2 for invalid arguments, and 130 for interruption."))
    parser.add_argument("root", type=Path, metavar="ROOT")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply both operations")
    mode.add_argument("-n", "--dry-run", action="store_true", help="preview both operations")
    parser.add_argument("--state-dir", type=Path, metavar="DIR",
                        help="apply only, state and reports outside ROOT (default: new temporary directory)")
    parser.add_argument("--json", action="store_true", help="one JSON result on stdout")
    args = parser.parse_args()
    if args.state_dir and not args.apply:
        parser.error("--state-dir requires --apply")

    result = {"schema_version": 1, "ok": False, "dry_run": not args.apply}
    try:
        root = args.root.expanduser().resolve(strict=True)
        if not root.is_dir():
            parser.error("ROOT must be a directory")
        result["root"] = str(root)
        state = args.state_dir.expanduser().resolve() if args.state_dir else None
        if state is not None and (state == root or root in state.parents):
            parser.error("--state-dir must be outside ROOT")
        if state is None and Path(tempfile.gettempdir()).resolve().is_relative_to(root):
            parser.error("temporary directory must be outside ROOT (set TMPDIR or --state-dir for apply)")
        if sys.platform != "darwin":
            raise ValueError("APFS operations require macOS")
        fs = Native()
        if fs.mount(root).fstype != "apfs":
            raise ValueError("ROOT must be on an APFS volume")
        python = newer_python()
        if not python:
            raise ValueError("compression requires Python 3.12 or later on PATH or in Homebrew")

        with ExitStack() as stack:
            if args.apply:
                state = state or Path(tempfile.mkdtemp(prefix="apfs-reclaim-"))
                bind_state(state, {"root": str(root), "volume": fs.volume_identity(root)}, stack)
                reports = Path(tempfile.mkdtemp(prefix="run-", dir=state))
                result.update(state_dir=str(state), reports=str(reports))
                progress(f"State and retry path  {state}")
            else:
                state = reports = Path(stack.enter_context(tempfile.TemporaryDirectory(
                    prefix="apfs-reclaim-preview-")))

            runtime = [python, "-E", "-B"]
            inspect = [*runtime, str(SCRIPTS / "apfs-inspect.py"), "--tree", str(root), "--json"]
            code, result["before"] = run_phase("Inspecting APFS allocation", inspect, reports / "before.json")
            if code:
                raise ValueError("APFS inspection failed")
            compress = [*runtime, str(SCRIPTS / "apfs_compress_plus.py"), "file",
                        "compress" if args.apply else "scan", str(root),
                        "--backend", "ditto", "--algo", "lzvn",
                        "--state-db", str(state / "compression.sqlite3"), "--format", "jsonl"]
            if args.apply:
                compress += ["--workers", "8", "--volume-workers", "4", "--batch-files", "1"]
            with directory_lock(root) if args.apply else nullcontext():
                code, result["compression"] = run_phase(
                    "Compressing eligible files" if args.apply else "Previewing compression",
                    compress, reports / "compression.jsonl", streaming=True,
                )
            if code == 130 or code < 0:
                raise KeyboardInterrupt
            if code == 5:
                raise ValueError("Compression reported a conflict. See the compression report before retrying.")
            dedupe = [*runtime, str(SCRIPTS / "apfs-dedupe.py"), str(root), "--json",
                      "--apply" if args.apply else "--dry-run"]
            if args.apply:
                dedupe += ["--state-dir", str(state / "dedupe")]
            dedupe_code, dedupe_result = run_phase("Deduplicating files" if args.apply
                                                   else "Previewing deduplication",
                                                   dedupe, reports / "dedupe.json")
            result["dedupe"] = {key: value for key, value in dedupe_result.items()
                                if key not in ("changes", "skipped_files", "provenance_unmatched_files")}
            result["dedupe"]["files_skipped"] = len(dedupe_result.get("skipped_files", []))
            result["dedupe"]["provenance_unmatched"] = len(dedupe_result.get("provenance_unmatched_files", []))
            if dedupe_code == 130:
                raise KeyboardInterrupt
            after_code = 0
            if args.apply:
                after_code, result["after"] = run_phase("Verifying APFS allocation", inspect,
                                                        reports / "after.json")
            result["ok"] = code == dedupe_code == after_code == 0
            if not result["ok"]:
                result.update(error="incomplete", message="One or more phases reported errors")
                progress(result["message"])
    except KeyboardInterrupt:
        result.update(error="interrupted", message="Run interrupted")
        progress(result["message"])
        report_result(result)
        return 130
    except (OSError, ValueError) as error:
        result.update(error="reclaim_failed", message=str(error))
        progress(str(error))
    report_result(result)
    return 0 if result["ok"] else 1


def interrupt(_signal, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    sys.exit(main())
