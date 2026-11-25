#!/usr/bin/env python3
"""
apfs_compress_plus.py – Apply APFS/HFS+ transparent compression to files.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import ctypes
import difflib
import fcntl
import filecmp
import fnmatch
import hashlib
import heapq
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
import zlib
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_PERMISSION = 4
EXIT_CONFLICT = 5
EXIT_UNSUPPORTED = 6
EXIT_INTERRUPTED = 130
SCHEMA_VERSION = 1
VERSION = "0.5.0"
MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:
    # Stop here, before the code that an older interpreter cannot run.
    print("Error [unsupported_python]: Python "
          f"{'.'.join(map(str, MIN_PYTHON))} or later is required; this is "
          f"{sys.version.split()[0]}", file=sys.stderr)
    print("Suggestion: rerun with a newer python3", file=sys.stderr)
    sys.exit(EXIT_UNSUPPORTED)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from apfs_common import (DECMPFS_XATTR, PROVENANCE_XATTR, STAGING_PREFIXES, UNSAFE_FLAGS, Conflict,  # noqa: E402
                         XATTR_NOFOLLOW, Native, OpenUse, atomic_write,
                         free_bytes, human_bytes, signature)
from apfs_publish import (PUBLICATION_PREFIX, PublicationConflict, publication_directory,
                          recover_publication, staged_replacement)

logger = logging.getLogger(__name__)
ATOMIC_TEMP_PREFIX = ".apfs-compress-plus-"
COMPRESSION_LZFSE = 0x801
QUEUE_POLL_SECONDS = 0.2
# How long a run waits for the state lock; outlasts the probe of 'system doctor'.
STATE_LOCK_WAIT_SECONDS = 0.5
# Access modes through which another process can change a file: a writable
# descriptor, or a mapping (whose protection the snapshot does not tell).
WRITE_ACCESS = frozenset({"w", "u", "mapped"})
CLEAR_LINE = "\r\033[K"
# The first bytes of thin and universal Mach-O images, in the two byte
# orders.
MACH_O_MAGICS = frozenset({
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
})
FULL_READ_CHUNK_BYTES = 4 << 20
SAMPLE_REGION_BYTES = 64 << 10
MAX_SAMPLE_REGIONS = 16
# Sample coverage buckets: (label, minimum sampled fraction), best first.
COVERAGE_BUCKETS = (("full", 1.0), ("10-100%", 0.1), ("1-10%", 0.01),
                    ("under 1%", 0.0))
BACKENDS = ("afsctool", "applesauce", "ditto")
DEFAULT_BACKEND = "afsctool"
REPAIR_KINDS = ("lost_flag", "stale_image", "interrupted_swap")
# Extended attributes that prevent compression of an uncompressed file.
BLOCKING_XATTRS = {
    "com.apple.ResourceFork": "a resource fork",
    DECMPFS_XATTR: "stale compression metadata",
}
# Formats that are already compressed, then files that an application
# possibly writes. A compressed file rejects writes through descriptors
# and mappings that were open before the compression.
DEFAULT_EXCLUDED_EXTENSIONS = (
    ".jpg,.jpeg,.png,.gif,.mp4,.zip,.gz,.xz,.bz2,.dmg,.pkg,.iso,"
    ".sqlite,.sqlite3,.db,.sqlite-wal,.sqlite-shm,.db-wal,.db-shm,"
    ".wal,.shm,.log,.lock"
)
OUTPUT_FORMATS = ("json", "jsonl", "text")
NOUNS = ("file", "codec", "state", "volume", "system")
DEFAULT_STATE_DB = str(Path.home() / ".apfs_compress_state.sqlite")
DEFAULT_BENCHMARK_FILE = str(Path.home() / ".apfs_compress_benchmark.json")


class FileInUseError(RuntimeError):
    def __init__(self, path: Path, users: list[dict]):
        self.path = path
        self.users = users
        names = ", ".join(
            f"{user.get('command') or 'process'} ({user['pid']})"
            for user in users
        )
        super().__init__(f"file is open by {names}")


class NoSavingsError(RuntimeError):
    """The compressor ran without an error and found no saving."""


class CompressorRefusedError(RuntimeError):
    """The compressor gave exit code 0 but did not compress a file."""
    def __init__(self, failures, fallback_paths=()):
        self.causes = {str(path): message
                       for path, message in failures.items()}
        self.failures = {str(path): str(message)
                         for path, message in failures.items()}
        self.fallback_paths = {Path(path) for path in fallback_paths}
        super().__init__("; ".join(
            f"{path}: {message}" for path, message in self.failures.items()
        ))

    def error_for_path(self, path: Path) -> Exception:
        cause = self.causes.get(str(path))
        if isinstance(cause, (FileInUseError, NoSavingsError, PublicationConflict, OSError,
                              subprocess.CalledProcessError)):
            return cause
        message = self.failures.get(str(path), "compressor refused the file")
        return CompressorRefusedError({path: message})


class RunStopped(Exception):
    """The run stops. Do not start more I/O."""


class AppError(Exception):
    def __init__(self, error: str, message: str, exit_code: int = EXIT_FAILURE,
                 suggestion: str | None = None, retryable: bool = False,
                 **details):
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code
        self.suggestion = suggestion
        self.retryable = retryable
        self.details = details

    def as_dict(self) -> dict:
        result = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": self.error,
            "message": self.message,
            "retryable": self.retryable,
            **self.details,
        }
        if self.suggestion:
            result["suggestion"] = self.suggestion
        return result


def os_error_text(exc: OSError) -> str:
    """Describe an OSError. The caller reports the path separately."""
    return exc.strerror or str(exc)


def run_command(command, *, check: bool = True, cwd=None):
    """Run a command with the standard contract: captured text output."""
    location = {"cwd": cwd} if cwd is not None else {}
    return subprocess.run(
        command, check=check, capture_output=True, text=True, **location
    )


def output_mode_from_argv(argv: list[str]) -> str:
    """Find the requested output mode when the argument parser failed."""
    flags = [value.partition("=")[0] for value in argv]
    if "--json" in flags:
        return "json"
    if "--plain" in flags or "--field" in flags:
        return "plain"
    for index, value in enumerate(argv):
        flag, _, inline = value.partition("=")
        if flag in {"--format", "--output"}:
            requested = inline or "".join(argv[index + 1:index + 2])
            return requested if requested in OUTPUT_FORMATS else "text"
    if "--quiet" in flags or "-q" in flags:
        return "quiet"
    return "text"


def counted(count: int, noun: str, plural: str | None = None) -> str:
    """Return a count with its noun in the correct singular or plural form."""
    return f"{count} {noun if count == 1 else plural or noun + 's'}"


def print_fields(fields: list[tuple[str, object]]) -> None:
    """Print indented label/value rows with aligned values."""
    width = max(len(label) for label, _ in fields) + 1
    for label, value in fields:
        print(f"  {label + ':':<{width}} {value}")


def write_file_inspection(result: dict) -> None:
    for item in result["files"]:
        state = "compressed" if item["compressed"] else "uncompressed"
        algorithm = f", {item['algorithm']}" if item["algorithm"] else ""
        print(f"{item['path']}: {state}{algorithm}, "
              f"{human_bytes(item['allocated_bytes'])} allocated")


def write_decompression(result: dict) -> None:
    action = "Would decompress" if result["dry_run"] else "Decompressed"
    print(f"{action}: {result['files_changed']}")
    print_fields([("Unchanged", result["files_unchanged"]),
                  ("Errors", result["errors"])])


def write_compression_summary(result: dict) -> None:
    dry_run = result["dry_run"]
    print("Compression scan complete" if dry_run else "Compression complete")
    skipped = (result["skipped_cached"] + result["skipped_ratio"] +
               result["skipped_already"] + result["skipped_budget"] +
               result["skipped_in_use"])
    fields = [
        ("Scanned", result["files_scanned"]),
        ("Eligible", result["files_queued"]),
        ("Already compressed", result["files_already_compressed"]),
        ("Uncompressed", result["files_not_compressed"]),
        ("Suggested",
         (f"{result['files_suggested']} "
          f"({result['files_suggested_not_compressed']} uncompressed, "
          f"{result['files_suggested_already_compressed']} already compressed)")),
        ("Planned" if dry_run else "Compressed", result["files_compressed"]),
        ("Estimated savings" if dry_run else "Space saved",
         human_bytes(result["space_saved"])),
    ]
    if dry_run:
        # An estimate is only as accurate as the sampled fraction of each file.
        coverage = {bucket["coverage"]: bucket["estimated_saved_bytes"]
                    for bucket in result["estimate_by_coverage"]}
        thin = coverage["1-10%"] + coverage["under 1%"]
        fields.append(("  fully sampled", human_bytes(coverage["full"])))
        fields.append(("  sampled under 10%", human_bytes(thin)))
    for label, key in (("No savings", "processed_no_savings"),
                       ("Safe fallback", "files_atomic_fallback"),
                       ("In use", "skipped_in_use"),
                       ("Excluded", "skipped_excluded"),
                       ("Owned by others", "skipped_not_owned"),
                       ("Unsafe metadata", "skipped_blocked")):
        if result[key]:
            fields.append((label, result[key]))
    fields += [("Elapsed", f"{result['elapsed_seconds']:.2f} s"),
               ("Skipped", skipped)]
    if result["errors"]:
        fields.append(("Errors", result["errors"]))
    if not dry_run:
        for volume in result["target_volumes"]:
            fields.append((
                f"Free on {volume['mountpoint'] or 'volume'}",
                (f"{human_bytes(volume['free_bytes_before'])} before, "
                 f"{human_bytes(volume['free_bytes_after'])} after"),
            ))
    print_fields(fields)
    if not result["complete"]:
        limit = result["stopped_by"].replace("_", " ")
        print(f"\nStopped by the {limit}: {result['files_remaining']} files "
              "remain. Rerun the same command to continue.")
    snapshots = sum(volume["local_snapshots"]
                    for volume in result["target_volumes"])
    if snapshots:
        print(f"\nNote: {snapshots} local snapshot(s) keep the previous blocks "
              "of rewritten files, so\nfree space rises only as they expire.")
    if result["skipped_not_owned"]:
        print("\nNote: files owned by another user need sudo to compress.")
    if len(result["roots"]) > 1:
        print("\nBy path:")
        for root in result["roots"]:
            print(f"  {human_bytes(root['space_saved']):>10}  "
                  f"{root['files_compressed']:>7} files  {root['path']}")
    if result["top_candidates"]:
        print("\nTop candidates:")
        for candidate in result["top_candidates"]:
            print(f"  {human_bytes(candidate['estimated_saved']):>10}  "
                  f"{candidate['path']} ({candidate['algorithm']}, "
                  f"{candidate['confidence']:.0%} sampled)")


def write_benchmark(result: dict) -> None:
    print("Codec estimator benchmark")
    print(f"  Corpus: {human_bytes(result['bytes'])}\n")
    print("  Codec      Ratio    Throughput")
    for algorithm, values in sorted(result["codecs"].items()):
        print(f"  {algorithm:<10} {values['ratio']:<8.4f} "
              f"{values['throughput_mib_s']:.1f} MiB/s")


def write_state_inspection(result: dict) -> None:
    print(f"State database: {result['state_db']}")
    print_fields([("Exists", "yes" if result["exists"] else "no"),
                  ("Entries", result["entries"]),
                  ("Size", human_bytes(result["bytes"]))])


def write_state_clear(result: dict) -> None:
    if result["dry_run"]:
        print(f"Would clear state database: {result['state_db']}")
        for path in result["would_remove"]:
            print(f"  {path}")
    elif result["changed"]:
        print(f"Cleared state database: {result['state_db']}")
    else:
        print(f"State already clear: {result['state_db']}")


def write_state_recovery(result: dict) -> None:
    action = "Would recover" if result["dry_run"] else "Recovered"
    print(f"{action}: {result['recovered']}")
    print_fields([("Missing", result["missing"]),
                  ("Errors", result["errors"])])
    for failure in result["failures"]:
        print(f"  {failure['path']}: {failure['message']}")


def write_state_prune(result: dict) -> None:
    action = "Would remove" if result["dry_run"] else "Removed"
    count = result["stale"] if result["dry_run"] else result["removed"]
    print(f"{action}: {count} stale state entries")
    print_fields([("Examined", result["examined"])])


def write_state_list(result: dict) -> None:
    for item in result["files"]:
        recorded = (time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(item["recorded_at"]))
                    if item["recorded_at"] else "-")
        print(f"{item['outcome']:<19} {item['method'] or '-':<8} "
              f"{recorded:<16} {item['path']}")
    print(f"{result['entries']} entries")


def write_repair(result: dict) -> None:
    action = "Would repair" if result["dry_run"] else "Repaired"
    print(f"{action}: {result['files_repaired']}")
    print_fields([("Flags restored", result["flags_restored"]),
                  ("Executables reseated", result["images_reseated"]),
                  ("Publications recovered", result["publications_recovered"]),
                  ("Errors", result["errors"])])
    for failure in result["failures"]:
        print(f"  {failure['path']}: {failure['message']}")


def write_doctor(result: dict) -> None:
    labels = {"ok": "ok", "warning": "warn", "problem": "FAIL"}
    for check in result["checks"]:
        print(f"{labels[check['status']]:<5} {check['check']:<26} "
              f"{check['message']}")
        for path in check.get("paths", []):
            print(f"{'':<6}{path}")
        if check.get("suggestion"):
            print(f"{'':<6}-> {check['suggestion']}")
    print(f"\n{counted(result['problems'], 'problem')}, "
          f"{counted(result['warnings'], 'warning')}")


def write_volume_inspection(result: dict) -> None:
    for volume in result["volumes"]:
        support = "supported" if volume["compression_supported"] else "unsupported"
        access = "writable" if volume["writable"] else "read-only"
        print(f"{volume['path']}: {volume['filesystem']}, {access}, {support}")


HUMAN_WRITERS = {
    "file inspect": write_file_inspection,
    "file decompress": write_decompression,
    "file scan": write_compression_summary,
    "file compress": write_compression_summary,
    "codec benchmark": write_benchmark,
    "state inspect": write_state_inspection,
    "state clear": write_state_clear,
    "state recover": write_state_recovery,
    "state prune": write_state_prune,
    "state list": write_state_list,
    "file repair": write_repair,
    "system doctor": write_doctor,
    "volume inspect": write_volume_inspection,
}


def document(command: str, ok: bool = True, **fields) -> dict:
    """Make the envelope that is common to all structured results."""
    return {"schema_version": SCHEMA_VERSION, "ok": ok, "command": command,
            **fields}


def write_result(result: dict, mode: str) -> None:
    if mode in {"json", "jsonl"}:
        payload = dict(result)
        if mode == "jsonl" and "record_type" not in payload:
            payload["record_type"] = "summary"
        print(json.dumps(payload, indent=None if mode == "jsonl" else 2,
                         sort_keys=True))
    elif mode in {"plain", "quiet"}:
        if "checks" in result:
            for check in result["checks"]:
                print(f"{check['status']}\t{check['check']}")
        elif "files" in result:
            for item in result["files"]:
                print(item["path"])
        elif "volumes" in result:
            for volume in result["volumes"]:
                print(volume["path"])
        elif "state_db" in result:
            print(result["state_db"])
        elif "codecs" in result:
            for algorithm in sorted(result["codecs"]):
                print(algorithm)
    else:
        HUMAN_WRITERS[result["command"]](result)


def write_file_collection(result: dict, mode: str) -> None:
    if mode != "jsonl":
        write_result(result, mode)
        return
    for item in result["files"]:
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "file",
            **item,
        }
        print(json.dumps(record, sort_keys=True))
    summary = {key: value for key, value in result.items() if key != "files"}
    summary["record_type"] = "summary"
    write_result(summary, mode)


def write_error(error: AppError, mode: str) -> None:
    print(f"Error [{error.error}]: {error.message}", file=sys.stderr)
    if error.suggestion:
        print(f"Suggestion: {error.suggestion}", file=sys.stderr)
    if mode in {"json", "jsonl"}:
        payload = error.as_dict()
        if mode == "jsonl":
            payload["record_type"] = "error"
        print(json.dumps(payload, sort_keys=True))


class Reporter:
    """Serialize the stream output and, as an option, keep detailed records."""
    def __init__(self, mode: str, retain: bool = False):
        self.mode = mode
        self.retain = retain
        self.records: list[dict] = []
        self.failures: list[dict] = []
        self.lock = threading.Lock()

    def file(self, path: Path, status: str, **fields) -> None:
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "file",
            "path": str(path),
            "status": status,
            **fields,
        }
        with self.lock:
            if self.retain:
                self.records.append(record)
            if status == "error":
                self.failures.append(record)
            if self.mode == "jsonl":
                print(json.dumps(record, sort_keys=True), flush=True)
            elif self.mode in {"plain", "quiet"} and status in {
                    "planned", "compressed", "recompressed"}:
                print(path, flush=True)
            elif self.mode in {"text", "quiet"} and status == "error":
                message = fields.get("message", fields.get("error", "failed"))
                clear = CLEAR_LINE if sys.stderr.isatty() else ""
                print(f"{clear}Warning: {path}: {message}", file=sys.stderr,
                      flush=True)

_NATIVE: Native | None = None


def native() -> Native:
    """Return the libSystem binding of this process; see apfs_common."""
    global _NATIVE
    if _NATIVE is None:
        _NATIVE = Native()
    return _NATIVE


def extended_attribute_names(path: Path) -> set[str]:
    """Return the extended attribute names of path, not those of its target.

    Raise OSError if the names cannot be listed.
    """
    return {name.decode(errors="replace")
            for name in native().xattr_names(path, XATTR_NOFOLLOW)}


def recorded_size(path: Path) -> int | None:
    """Return the uncompressed size from the compression metadata of path."""
    header = native().decmpfs(path)
    return header[1] if header else None


def find_file_problems(roots: list[Path]) -> Iterator[tuple[str, Path]]:
    """Yield (kind, path) for each problem found on disk at or below roots.

    lost_flag: a compressed file that lost its compressed flag. It reads
        as zero bytes, but its compressed data is still attached.
    stale_metadata: a file that has its own content and also compression
        metadata. afsctool truncates such a file.
    leftover_copy: a temporary directory that stayed after an interrupted
        replacement.
    interrupted_swap: a private directory holding a recoverable publication.
    unreadable: a directory that the tool cannot list.
    """
    def classify(path: Path, st: os.stat_result):
        if not stat.S_ISREG(st.st_mode) or st.st_flags & stat.UF_COMPRESSED:
            return
        try:
            names = extended_attribute_names(path)
        except OSError:
            return
        if DECMPFS_XATTR in names:
            yield ("lost_flag" if st.st_size == 0 else "stale_metadata", path)

    stack = list(reversed(roots))
    while stack:
        current = stack.pop()
        try:
            if current.name.startswith(PUBLICATION_PREFIX):
                yield "interrupted_swap", current
                continue
            if not current.is_dir() or current.is_symlink():
                stage = publication_directory(current)
                if stage.exists() or stage.is_symlink():
                    yield "interrupted_swap", stage
                yield from classify(current,
                                    current.stat(follow_symlinks=False))
                continue
            with os.scandir(current) as scan:
                for entry in scan:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.startswith(PUBLICATION_PREFIX):
                            yield "interrupted_swap", Path(entry.path)
                        elif entry.name.startswith(ATOMIC_TEMP_PREFIX):
                            yield "leftover_copy", Path(entry.path)
                        else:
                            stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield from classify(
                            Path(entry.path), entry.stat(follow_symlinks=False)
                        )
        except OSError:
            yield "unreadable", current


def compression_blockers(path: Path) -> list[str]:
    """Describe attributes that make compression of path unsafe or impossible.

    afsctool truncates a file to zero bytes if the file has compression
    metadata but not the compressed flag. It ignores a file with a resource
    fork and gives no message. Thus, the tool gives neither to afsctool.
    """
    try:
        names = extended_attribute_names(path)
    except FileNotFoundError:
        return []   # The compressor reports a file that is gone.
    except OSError as exc:
        # Without the names, the file cannot be shown to be safe for afsctool.
        return [f"unreadable extended attributes ({os_error_text(exc)})"]
    return [reason for name, reason in BLOCKING_XATTRS.items()
            if name in names]

# ───────────────────────────── CLI ────────────────────────────────────────────
EXIT_CODES_HELP = """\
Exit codes:
  0    success, including an idempotent no-op
  1    failure or partial failure
  2    invalid usage or missing confirmation
  3    path not found
  4    permission denied
  5    conflict (changed plan target, unrecovered recompression, or
       state database locked by another run)
  6    unsupported filesystem or missing backend
  130  interrupted"""


class AgentArgumentParser(argparse.ArgumentParser):
    """Parser that reports usage errors through the structured contract."""
    commands: dict[str, argparse.ArgumentParser]

    def __init__(self, *args, **kwargs):
        # With abbreviated flags, each new flag can break old commands.
        kwargs.setdefault("allow_abbrev", False)
        kwargs.setdefault("formatter_class",
                          argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)
        self.commands = {}

    def error(self, message):
        raise AppError("usage_error", message, EXIT_USAGE,
                       f"run {self.prog} --help")


def command_epilog(*examples: str) -> str:
    lines = ["examples:",
             *(f"  %(prog)s {example}".rstrip() for example in examples)]
    return "\n".join(lines) + "\n\n" + EXIT_CODES_HELP


def add_output_arguments(parser: argparse.ArgumentParser, plain_help: str,
                         *, paths: bool = False, details: bool = False):
    """Add output-mode flags and return their help group."""
    group = parser.add_argument_group("output")
    formats = group.add_mutually_exclusive_group()
    formats.add_argument("--format", choices=OUTPUT_FORMATS, dest="output",
                         default="text", help="Output format (default: text)")
    formats.add_argument("--json", action="store_const", const="json",
                         dest="output", help="Alias for --format json")
    formats.add_argument("--plain", action="store_const", const="plain",
                         dest="output", help=plain_help)
    if paths:
        formats.add_argument("--field", choices=("path",),
                             help="Output one stable field per record, one "
                                  "per line")
    formats.add_argument("--output", choices=OUTPUT_FORMATS, dest="output",
                         help="Deprecated alias for --format")
    formats.add_argument("--quiet", "-q", action="store_const", const="quiet",
                         dest="output", help="Deprecated alias for --plain")
    if details:
        group.add_argument("--details", action="store_true",
                           help="Include per-file decision records in JSON "
                                "output")
    parser.set_defaults(field=None, details=False)
    return group


def add_file_arguments(p: argparse.ArgumentParser,
                       default_current_directory: bool = False,
                       input_alternative: str | None = None) -> None:
    if default_current_directory:
        path_requirement = " (default: current directory)"
    elif input_alternative:
        path_requirement = f" (required unless {input_alternative})"
    else:
        path_requirement = " (required)"
    p.add_argument("paths", nargs="*", default=[], metavar="PATH",
                   help="Files or directories to process" + path_requirement)

    selection = p.add_argument_group("selection")
    selection.add_argument(
        "--files-from", metavar="FILE",
        help="Read additional paths, one per line; '-' means stdin. Blank "
             "lines and surrounding whitespace are ignored; duplicates are "
             "processed once")
    selection.add_argument("--cutoff-days", type=int, default=180,
                           metavar="DAYS",
                           help="Skip files modified within DAYS (default: 180)")
    selection.add_argument("--min-size", type=int, default=64 << 10,
                           metavar="BYTES",
                           help="Skip files smaller than BYTES (default: 65536)")
    selection.add_argument("--exclude-ext", metavar="LIST",
                           default=DEFAULT_EXCLUDED_EXTENSIONS,
                           help="Comma-separated extensions to skip "
                                "(default: common compressed formats and "
                                "database, log, and lock files)")
    selection.add_argument("--exclude-dir", metavar="LIST",
                           default=".git,.cache,__pycache__,node_modules",
                           help="Comma-separated directory names to skip "
                                "(default: %(default)s)")
    selection.add_argument(
        "--exclude", metavar="GLOB", action="append", default=[],
        help="Skip matching files and directories; repeatable. A pattern "
             "without '/' matches a name, one with '/' matches the whole "
             "path, and a plain path also excludes everything below it")
    selection.add_argument(
        "--exclude-from", metavar="FILE",
        help="Read --exclude patterns from FILE, one per line")
    selection.add_argument("--include-sparse", action="store_true",
                           help="Allow files whose allocated size is below "
                                "logical size")
    selection.add_argument("--include-hardlinks", action="store_true",
                           help="Allow multiply-linked files (afsctool only)")
    selection.add_argument("--recompress", action="store_true",
                           help="Recompress compressed files when the "
                                "predicted gain is material")

    codec = p.add_argument_group("codec policy")
    codec.add_argument("--ratio", type=float, default=0.85, metavar="RATIO",
                       help="Accept sampled compression ratio up to RATIO "
                            "(default: 0.85)")
    codec.add_argument("--probe-bytes", type=int, default=1 << 20,
                       metavar="BYTES",
                       help="Bytes to sample per file; 0 reads the whole "
                            "file (default: 1048576)")
    codec.add_argument("--algo", choices=("auto", "lzvn", "lzfse", "zlib"),
                       default="auto",
                       help="Compression algorithm (default: auto)")
    codec.add_argument("--profile", choices=("fast", "balanced", "max"),
                       default="balanced",
                       help="Automatic codec policy (default: balanced)")
    codec.add_argument("--cold-days", type=int, default=365, metavar="DAYS",
                       help="Age at which auto may select LZFSE "
                            "(default: 365)")
    codec.add_argument("--archive-days", type=int, default=730, metavar="DAYS",
                       help="Age at which max may select zlib (default: 730)")
    codec.add_argument("--min-codec-gain", type=float, default=0.05,
                       metavar="FRACTION",
                       help="Ratio improvement required for a slower codec "
                            "(default: 0.05)")
    codec.add_argument("--min-savings-per-second", type=int, default=1 << 20,
                       metavar="BYTES",
                       help="Extra BYTES a slower codec must save per extra "
                            "second (default: 1048576)")
    codec.add_argument("--benchmark-file", default=DEFAULT_BENCHMARK_FILE,
                       metavar="FILE",
                       help="Codec calibration from 'codec benchmark', read "
                            "when present (default: %(default)s)")
    codec.add_argument("--backend", choices=BACKENDS, default=DEFAULT_BACKEND,
                       help=f"Compression tool (default: {DEFAULT_BACKEND})")

    budget = p.add_argument_group("budgets")
    budget.add_argument("--workers", type=int, metavar="N",
                        default=min(4, os.cpu_count() or 4),
                        help="Parallel analysis workers (default: up to 4)")
    budget.add_argument("--volume-workers", type=int, default=1, metavar="N",
                        help="Concurrent writes per volume (default: 1)")
    budget.add_argument("--batch-files", type=int, default=32, metavar="N",
                        help="Paths per compressor process (default: 32)")
    budget.add_argument("--time-budget", type=float, default=0,
                        metavar="SECONDS",
                        help="Start no compression after SECONDS "
                             "(default: 0, unlimited)")
    budget.add_argument("--space-target", type=int, default=0, metavar="BYTES",
                        help="Stop after saving about BYTES "
                             "(default: 0, unlimited)")
    budget.add_argument("--max-read-mbps", type=float, default=0,
                        metavar="MBPS",
                        help="Limit aggregate sampling reads "
                             "(default: 0, unlimited)")
    budget.add_argument("--max-write-mbps", type=float, default=0,
                        metavar="MBPS",
                        help="Limit aggregate compressed writes "
                             "(default: 0, unlimited)")

    state = p.add_argument_group("state and safety")
    state.add_argument("-n", "--dry-run", action="store_true",
                       help="Analyse and report planned changes; modify "
                            "neither files nor state")
    state.add_argument("--state-db", default=DEFAULT_STATE_DB, metavar="FILE",
                       help="SQLite file recording processed files "
                            "(default: %(default)s)")
    state.add_argument("--ignore-cache", action="store_true",
                       help="Re-evaluate files even when state matches")
    state.add_argument("--json-log", default=None, metavar="FILE",
                       help="Write a structured debug log to FILE")
    state.add_argument("--yes", action="store_true",
                       help="Deprecated no-op; explicit paths authorize work")

    output = add_output_arguments(
        p, "Output planned or compressed paths, one per line",
        paths=True, details=True,
    )
    output.add_argument("--top", type=int, default=10, metavar="N",
                        help="Report the N highest-value candidates "
                             "(default: 10)")


def validate_file_arguments(args: argparse.Namespace,
                            parser: argparse.ArgumentParser) -> None:
    if args.workers < 1 or args.volume_workers < 1 or args.batch_files < 1:
        parser.error("--workers, --volume-workers, and --batch-files "
                     "must be at least 1")
    if args.top < 0:
        parser.error("--top cannot be negative")
    if (args.min_size < 0 or args.probe_bytes < 0 or args.cutoff_days < 0 or
            args.cold_days < 0 or args.archive_days < 0 or
            args.time_budget < 0 or args.space_target < 0 or
            args.max_read_mbps < 0 or args.max_write_mbps < 0):
        parser.error("size, probe, age, budget, and rate values cannot be "
                     "negative")
    if not 0 < args.ratio <= 1:
        parser.error("--ratio must be greater than 0 and no greater than 1")
    if not 0 <= args.min_codec_gain < 1:
        parser.error("--min-codec-gain must be between 0 and 1")
    if args.min_savings_per_second < 0:
        parser.error("--min-savings-per-second cannot be negative")
    if args.from_plan and (args.paths or args.files_from):
        parser.error("--from-plan cannot be combined with paths or "
                     "--files-from")


def build_command_parser() -> AgentArgumentParser:
    parser = AgentArgumentParser(
        description="Inspect and apply APFS/HFS+ transparent compression.\n"
                    "Text is the default output; pass --json, --format "
                    "jsonl, or --plain for automation.",
        epilog=command_epilog(
            "file scan ~/Documents",
            "file compress ~/Archive --profile max --time-budget 1800",
            "file scan ~/Archive --json",
        ) + "\n\nRun '%(prog)s RESOURCE VERB --help' for complete help.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {VERSION}")
    nouns = parser.add_subparsers(dest="noun", title="resources",
                                  metavar="RESOURCE")

    def add_noun(noun: str, summary: str):
        noun_parser = nouns.add_parser(noun, help=summary, description=summary)
        parser.commands[noun] = noun_parser
        return noun_parser.add_subparsers(dest="verb", required=True,
                                          title="verbs", metavar="VERB")

    def add_verb(verbs, noun: str, verb: str, summary: str, description: str,
                 *examples: str) -> argparse.ArgumentParser:
        command = verbs.add_parser(
            verb, help=summary, description=textwrap.fill(description, 78),
            epilog=command_epilog(*examples),
        )
        command.set_defaults(command=f"{noun} {verb}", json_log=None)
        parser.commands[f"{noun} {verb}"] = command
        return command

    file_verbs = add_noun("file", "Inspect, scan, compress, or decompress files")
    file_inspect = add_verb(
        file_verbs, "file", "inspect", "report transparent-compression state",
        "Report whether each regular file is compressed, with its codec and "
        "allocated size. Reads only.",
        "~/Archive/data.bin", "~/Archive/*.log --json",
    )
    file_inspect.add_argument("paths", nargs="+", metavar="FILE",
                              help="Regular files to inspect")
    add_output_arguments(file_inspect, "Output inspected paths, one per line",
                         paths=True)

    scan = add_verb(
        file_verbs, "file", "scan",
        "preview compression candidates without mutation",
        "Sample files and report which would be compressed and the estimated "
        "savings. Reads only; never writes files or state.",
        "~/Documents", "~/Documents --format jsonl",
        "~/Archive --plan plan.json --json",
    )
    add_file_arguments(scan, default_current_directory=True)
    scan.add_argument("--plan", metavar="FILE",
                      help="Write an identity-bound compression plan to FILE")
    scan.add_argument(
        "--no-audit-compressed", action="store_false",
        dest="audit_compressed",
        help="Skip compressibility sampling for already-compressed files",
    )
    scan.set_defaults(dry_run=True, benchmark=False,
                      default_current_directory=True, from_plan=None,
                      audit_compressed=True)

    compress = add_verb(
        file_verbs, "file", "compress",
        "idempotently apply transparent compression",
        "Compress eligible files in place. Changes files and the state "
        "database; never prompts. Repeated runs and retries are safe: "
        "processed files are skipped. Failures are reported per file and "
        "make the command exit nonzero.",
        "~/Archive", "~/Archive --dry-run --details --json",
        "--from-plan plan.json --json", "--files-from - --format jsonl",
    )
    add_file_arguments(compress, input_alternative="--from-plan")
    compress.add_argument("--from-plan", metavar="FILE",
                          help="Apply a plan written by 'file scan --plan'")
    compress.set_defaults(benchmark=False, default_current_directory=False,
                          plan=None, audit_compressed=False)

    decompress = add_verb(
        file_verbs, "file", "decompress",
        "idempotently remove transparent compression",
        "Decompress regular files with afsctool. Changes files and can grow "
        "disk usage, so it requires --yes; never prompts. Already "
        "uncompressed files are a no-op.",
        "~/Archive/data.bin --dry-run", "~/Archive/data.bin --yes --json",
    )
    decompress.add_argument("paths", nargs="+", metavar="FILE",
                            help="Regular files to decompress")
    decompress.add_argument("-n", "--dry-run", action="store_true",
                            help="Report changes without modifying files")
    decompress.add_argument("--yes", action="store_true",
                            help="Confirm decompression and possible space "
                                 "growth")
    add_output_arguments(decompress, "Output processed paths, one per line",
                         paths=True)

    repair = add_verb(
        file_verbs, "file", "repair",
        "repair compressed files left in a damaged state",
        "Repair the following conditions at or below each PATH. lost_flag: a "
        "compressed file whose compressed flag is gone reads as zero bytes "
        "although its data is still attached; the flag is restored and the "
        "content read back, and the file is left as found if that fails. "
        "stale_image: an executable compressed in place after being run is "
        "killed on launch; executables recorded in the state database are "
        "replaced by identical copies. interrupted_swap: recover a journaled "
        "compression publication and preserve both files if they conflict. "
        "'system doctor PATH' reports what would be found.",
        "~/Development --dry-run", "~/Development --json",
        "~/Development --only interrupted_swap",
    )
    repair.add_argument("paths", nargs="+", metavar="PATH",
                        help="Files or directories to repair")
    repair.add_argument("--only", choices=REPAIR_KINDS,
                        help="Repair one kind of damage (default: all)")
    repair.add_argument("--all-images", action="store_true",
                        help="Reseat every recorded compressed executable; "
                             "by default only those rewritten in place "
                             "since the last boot or at an unknown time")
    repair.add_argument("-n", "--dry-run", action="store_true",
                        help="List the files that would be repaired")
    repair.add_argument("--state-db", default=DEFAULT_STATE_DB,
                        metavar="FILE",
                        help="State database (default: %(default)s)")
    add_output_arguments(repair, "Output repaired paths, one per line",
                         paths=True)

    codec_verbs = add_noun("codec", "Measure codec performance")
    benchmark = add_verb(
        codec_verbs, "codec", "benchmark", "benchmark local codec estimators",
        "Measure estimator ratio and throughput for each codec and write the "
        "calibration file read by 'file scan' and 'file compress'.",
        "", "--json",
    )
    benchmark.add_argument("--benchmark-file", default=DEFAULT_BENCHMARK_FILE,
                           metavar="FILE",
                           help="Calibration file to write "
                                "(default: %(default)s)")
    add_output_arguments(benchmark, "Output codec names, one per line")
    benchmark.set_defaults(benchmark=True)

    state_verbs = add_noun(
        "state", "Inspect, list, recover, prune, or clear cached state"
    )
    listing = add_verb(
        state_verbs, "state", "list", "list recorded files",
        "List the files recorded in the state database with their outcome, "
        "how compressed files were written (in_place or replace), and when. "
        "Entries written before 0.4.0 have no method or time. Reads only.",
        "--outcome compressed --under ~/Development",
        "--outcome compressed --images --plain",
    )
    listing.add_argument("--state-db", default=DEFAULT_STATE_DB,
                            metavar="FILE",
                            help="State database (default: %(default)s)")
    listing.add_argument("--outcome", metavar="OUTCOME",
                            help="Only entries with this outcome, such as "
                                 "compressed or ratio_rejected")
    listing.add_argument("--under", metavar="PATH",
                            help="Only entries at or below PATH")
    listing.add_argument("--images", action="store_true",
                            help="Only executable images (programs and "
                                 "libraries) that still exist")
    add_output_arguments(listing, "Output recorded paths, one per line",
                         paths=True)
    for verb, summary, description, changes in (
            ("inspect", "summarize cached file state",
             ("Report entry counts, outcomes, and pending recompressions. "
              "Reads only."), False),
            ("recover", "recover interrupted recompressions",
             ("Restore files left decompressed by an interrupted "
              "recompression and drop records for deleted files. Changes "
              "files and state, so it requires --yes; never prompts."), True),
            ("prune", "remove entries for missing files",
             ("Remove state entries whose files no longer exist. Changes "
              "state, so it requires --yes; never prompts."), True),
            ("clear", "remove cached file state",
             ("Delete the state database and its sidecars. Requires --yes; "
              "never prompts. Clearing an absent database is a no-op."), True)):
        state_command = add_verb(
            state_verbs, "state", verb, summary, description,
            *(("--dry-run", "--yes --json") if changes else ("", "--json")),
        )
        state_command.add_argument(
            "--state-db", default=DEFAULT_STATE_DB, metavar="FILE",
            help="State database (default: %(default)s)")
        if verb == "recover":
            state_command.add_argument(
                "--discard-missing", action="store_true",
                help="Also drop records whose file and directory are both "
                     "missing, such as on an unmounted volume")
        if changes:
            state_command.add_argument(
                "-n", "--dry-run", action="store_true",
                help="Report changes without applying them")
            state_command.add_argument(
                "--yes", action="store_true", help="Confirm the change")
        add_output_arguments(state_command, "Output the state database path")

    system_verbs = add_noun("system", "Check the tool's environment")
    doctor = add_verb(
        system_verbs, "system", "doctor",
        "check tools, state, volumes, and files for problems",
        "Check that the required tools are installed, that the state "
        "database is healthy, and, for each PATH, that its volume supports "
        "compression and its files are not in a damaged state. Each check "
        "reports ok, warning, or problem, with the command that fixes it. "
        "Reads only. Exits 1 when any problem is found.",
        "", "~/Development", "~/Development --json",
    )
    doctor.add_argument("paths", nargs="*", metavar="PATH",
                        help="Files or directories whose volumes and files "
                             "to check (default: tools and state only)")
    doctor.add_argument("--state-db", default=DEFAULT_STATE_DB,
                        metavar="FILE",
                        help="State database (default: %(default)s)")
    add_output_arguments(doctor, "Output one STATUS<TAB>CHECK line per check")

    volume_verbs = add_noun("volume", "Inspect target volumes")
    inspect = add_verb(
        volume_verbs, "volume", "inspect", "report filesystem support",
        "Report the filesystem, writability, and compression support of the "
        "volume holding each path. Reads only.",
        "/Volumes/Archive", "~/Documents /Volumes/Archive --json",
    )
    inspect.add_argument("paths", nargs="+", metavar="PATH",
                         help="Paths whose volumes to inspect")
    add_output_arguments(inspect, "Output inspected paths, one per line",
                         paths=True)
    return parser


def build_legacy_parser() -> AgentArgumentParser:
    parser = AgentArgumentParser(
        description="Deprecated syntax without a resource and verb; use "
                    "'file scan' or 'file compress'."
    )
    add_file_arguments(parser)
    parser.add_argument("--benchmark", action="store_true",
                        help="Deprecated; use 'codec benchmark'")
    parser.set_defaults(plan=None, from_plan=None, audit_compressed=False)
    return parser


def reject_mistyped_resource(token: str) -> None:
    """Reject an almost-correct resource name. Do not use it as a path."""
    if token.startswith("-") or Path(token).expanduser().exists():
        return
    close = difflib.get_close_matches(token, NOUNS, n=1)
    if close:
        program = Path(sys.argv[0]).name
        raise AppError(
            "usage_error", f"unknown resource '{token}'", EXIT_USAGE,
            f"did you mean '{close[0]}'? run {program} --help",
        )


def refine_usage_error(error: AppError, parser: argparse.ArgumentParser,
                       argv: list[str]) -> None:
    """Add the most specific help and a probable fix to a usage error."""
    commands = getattr(parser, "commands", {})
    target = (commands.get(" ".join(argv[:2])) or
              commands.get(" ".join(argv[:1])) or parser)
    hints = []
    unknown = re.match(r"unrecognized arguments: (\S+)", error.message)
    # From Python 3.14, argparse adds its own "maybe you meant" before the list.
    choice = re.search(r"invalid choice: '([^']+)'.*\(choose from (.+)\)",
                       error.message)
    if unknown and unknown.group(1).startswith("-"):
        # argparse gives the registered flags only through this attribute.
        candidates = [flag for flag in target._option_string_actions
                      if flag.startswith("--")]
        word = unknown.group(1).partition("=")[0]
    elif choice:
        # From Python 3.13, argparse puts the choices in quotation marks.
        candidates = [name.strip("'") for name in choice.group(2).split(", ")]
        word = choice.group(1)
    else:
        candidates, word = [], ""
    for match in difflib.get_close_matches(word, candidates, n=1,
                                           cutoff=0.7):
        hints.append(f"did you mean '{match}'?")
    error.suggestion = " ".join([*hints, f"run {target.prog} --help"])


def deprecation_warnings(args: argparse.Namespace, argv: list[str],
                         legacy: bool) -> list[str]:
    flags = {value.partition("=")[0] for value in argv}
    warnings = []
    if legacy:
        replacement = ("codec benchmark" if args.command == "codec benchmark"
                       else f"{args.command} PATH")
        warnings.append("syntax without a resource and verb is deprecated; "
                        f"use '{replacement}'")
    if "--output" in flags:
        warnings.append("--output is deprecated; use --format")
    if args.output == "quiet":
        warnings.append("--quiet path projection is deprecated; use --plain")
    if "--yes" in flags and args.command in {"file scan", "file compress"}:
        warnings.append(f"--yes has no effect on {args.command} and is "
                        "deprecated")
    return warnings


def parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    legacy = bool(argv) and argv[0] not in {*NOUNS, "-h", "--help", "--version"}
    if legacy:
        reject_mistyped_resource(argv[0])
        parser = build_legacy_parser()
    else:
        parser = build_command_parser()
        if not argv or argv in ([noun] for noun in NOUNS):
            parser.commands.get("".join(argv), parser).print_help()
            parser.exit(EXIT_SUCCESS)
    try:
        args = parser.parse_args(argv)
        if legacy:
            args.command = "codec benchmark" if args.benchmark else (
                "file scan" if args.dry_run else "file compress"
            )
            args.default_current_directory = args.dry_run
        if args.command in {"file scan", "file compress"}:
            validate_file_arguments(args, parser)
        if args.details and args.output not in {"json", "jsonl"}:
            parser.error("--details requires --json or --format jsonl")
    except AppError as error:
        if error.error == "usage_error":
            refine_usage_error(error, parser, argv)
        raise
    args.deprecations = deprecation_warnings(args, argv, legacy)
    return args

# ──────────────────────────── Logging setup ───────────────────────────────────
def setup_logging(json_log: str | None) -> None:
    """Send debug events to --json-log only. stderr is for user diagnostics."""
    root = logging.getLogger()
    if not json_log:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.CRITICAL)
        return

    class JSONFormatter(logging.Formatter):
        def format(self, record):
            return json.dumps({
                "time": self.formatTime(record),
                "level": record.levelname,
                "msg": record.getMessage()})

    handler = logging.FileHandler(json_log)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

# ─────────────────────────── Persistent cache ────────────────────────────────
def connect_readonly(path: Path, **options) -> sqlite3.Connection:
    """Open an existing SQLite database and do not create sidecar files.

    immutable=1 ignores the WAL. Thus, use it only when there is no WAL or
    the WAL is empty. An interrupted writer leaves a WAL that contains
    records. mode=ro reads those committed records through the existing
    sidecars.
    """
    path = path.resolve()
    wal = Path(f"{path}-wal")
    query = ("mode=ro" if wal.exists() and wal.stat().st_size
             else "mode=ro&immutable=1")
    return sqlite3.connect(f"{path.as_uri()}?{query}", uri=True, **options)


class StateLock:
    """Exclusive advisory lock for a run that changes a state database.

    The system releases the lock when the process stops, for any cause. The
    lock file stays in place. If a run removes it, two runs can lock
    different inodes that have the same name.
    """
    def __init__(self, db_path: str):
        self.db_path = Path(db_path).expanduser().resolve()
        self.path = Path(f"{self.db_path}.lock")
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+")
        deadline = time.monotonic() + STATE_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
            self.handle.seek(0)
            holder = self.handle.read().strip()
            self.handle.close()
            raise AppError(
                "state_locked",
                f"another run is changing {self.db_path}"
                + (f" (pid {holder})" if holder else ""),
                EXIT_CONFLICT, "wait for that run to finish, then retry",
                retryable=True, state_db=str(self.db_path),
                holder_pid=int(holder) if holder.isdigit() else None,
            )
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.handle.close()


def state_lock(args: argparse.Namespace):
    """Lock the state database if the run changes it. A dry run has no lock."""
    return contextlib.nullcontext() if args.dry_run else StateLock(args.state_db)


class StateCache:
    """Buffered SQLite cache keyed by file identity and compression policy."""
    FLUSH_SIZE = 64

    def __init__(self, db_path: str, readonly: bool = False):
        self.readonly = readonly
        self.lock = threading.Lock()
        self.pending: dict[str, tuple] = {}
        path = Path(db_path).expanduser()
        if readonly:
            if not path.exists():
                self.conn = None
                self.has_state = False
                self.has_operations = False
                return
            self.conn = connect_readonly(path, check_same_thread=False)
            self.has_state = self.conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='file_state'"
            ).fetchone() is not None
            self.has_operations = self.conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='recompression_operations'"
            ).fetchone() is not None
            self.columns = self._columns()
            return

        self.conn = sqlite3.connect(path, check_same_thread=False)
        with self.conn:
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS file_state ("
                "path TEXT PRIMARY KEY, device INTEGER, inode INTEGER, "
                "size INTEGER, mtime_ns INTEGER, policy TEXT, outcome TEXT, "
                "recorded_at INTEGER, method TEXT)")
            # Databases older than 0.4.0 do not have the last two columns.
            for column, kind in (("recorded_at", "INTEGER"),
                                 ("method", "TEXT")):
                if column not in self._columns():
                    self.conn.execute(
                        f"ALTER TABLE file_state ADD COLUMN {column} {kind}"
                    )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS recompression_operations ("
                "path TEXT PRIMARY KEY, old_algorithm TEXT NOT NULL, "
                "new_algorithm TEXT NOT NULL, started_at INTEGER NOT NULL)"
            )
        self.has_state = True
        self.has_operations = True
        self.columns = self._columns()

    def _columns(self) -> set[str]:
        if self.conn is None:
            return set()
        return {row[1] for row in
                self.conn.execute("PRAGMA table_info(file_state)")}

    @staticmethod
    def _identity(path: Path, st: os.stat_result, policy: str):
        return (str(path), *stat_identity(st), policy)

    def seen(self, path: Path, st: os.stat_result, policy: str) -> bool:
        if not self.has_state or self.conn is None:
            return False
        identity = self._identity(path, st, policy)
        with self.lock:
            pending = self.pending.get(str(path))
            if pending and pending[:6] == identity:
                return True
            row = self.conn.execute(
                "SELECT 1 FROM file_state WHERE path=? AND device=? AND inode=? "
                "AND size=? AND mtime_ns=? AND policy=?", identity
            ).fetchone()
            return row is not None

    def mark(self, path: Path, st: os.stat_result, policy: str,
             outcome: str, method: str | None = None) -> None:
        """Record an outcome. method tells how the tool wrote the file."""
        if self.readonly:
            return
        row = self._identity(path, st, policy) + (
            outcome, int(time.time()), method,
        )
        with self.lock:
            self.pending[str(path)] = row
            if len(self.pending) >= self.FLUSH_SIZE:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if self.readonly or self.conn is None or not self.pending:
            return
        rows = list(self.pending.values())
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO file_state "
                "(path, device, inode, size, mtime_ns, policy, outcome, "
                "recorded_at, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
        self.pending.clear()

    def entries(self, outcome: str | None = None,
                under: Path | None = None) -> list[dict]:
        """List recorded files. Optional filters: outcome and parent path."""
        if not self.has_state or self.conn is None:
            return []
        optional = [name if name in self.columns else f"NULL AS {name}"
                    for name in ("recorded_at", "method")]
        query = ("SELECT path, outcome, policy, "
                 f"{', '.join(optional)} FROM file_state")
        clauses: list[str] = []
        values: list[str | int] = []
        if outcome is not None:
            clauses.append("outcome = ?")
            values.append(outcome)
        if under is not None:
            prefix = str(under).rstrip("/") + "/"
            clauses.append("(path = ? OR substr(path, 1, ?) = ?)")
            values += [str(under), len(prefix), prefix]
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.lock:
            self._flush_locked()
            rows = self.conn.execute(query + " ORDER BY path", values)
            return [
                {"path": path, "outcome": outcome, "policy": policy,
                 "recorded_at": recorded_at, "method": method}
                for path, outcome, policy, recorded_at, method in rows
            ]

    def close(self) -> None:
        with self.lock:
            self._flush_locked()
            if self.conn is not None:
                self.conn.close()

    def begin_recompression(self, path: Path, old_algorithm: str,
                            new_algorithm: str) -> None:
        if self.readonly or self.conn is None:
            return
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO recompression_operations "
                "(path, old_algorithm, new_algorithm, started_at) "
                "VALUES (?, ?, ?, ?)",
                (str(path), old_algorithm, new_algorithm, int(time.time())),
            )

    def finish_recompression(self, path: Path) -> None:
        if self.readonly or self.conn is None:
            return
        with self.lock, self.conn:
            self.conn.execute(
                "DELETE FROM recompression_operations WHERE path=?",
                (str(path),),
            )

    def pending_recompressions(self) -> list[dict]:
        if not self.has_operations or self.conn is None:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT path, old_algorithm, new_algorithm, started_at "
                "FROM recompression_operations ORDER BY started_at, path"
            ).fetchall()
        return [
            {
                "path": path,
                "old_algorithm": old_algorithm,
                "new_algorithm": new_algorithm,
                "started_at": started_at,
            }
            for path, old_algorithm, new_algorithm, started_at in rows
        ]

    def prune_missing(self, dry_run: bool = False) -> dict:
        if not self.has_state or self.conn is None:
            return {"dry_run": dry_run, "examined": 0, "stale": 0,
                    "removed": 0, "paths": []}
        with self.lock:
            self._flush_locked()
            paths = [Path(row[0]) for row in self.conn.execute(
                "SELECT path FROM file_state ORDER BY path"
            )]
        stale = []
        for path in paths:
            try:
                path.stat()
            except FileNotFoundError:
                stale.append(path)
            except OSError:
                continue
        if stale and not dry_run and not self.readonly:
            with self.lock, self.conn:
                self.conn.executemany(
                    "DELETE FROM file_state WHERE path=?",
                    [(str(path),) for path in stale],
                )
        return {
            "dry_run": dry_run,
            "examined": len(paths),
            "stale": len(stale),
            "removed": 0 if dry_run else len(stale),
            "paths": [str(path) for path in stale],
        }


def cache_policy(args, sampler: Sampler) -> str:
    policy = {
        # v5 cancels outcomes that used three sampled regions for each file.
        "version": 5,
        "tool": TOOL,
        "algorithm": args.algo,
        "ratio": args.ratio,
        "probe_bytes": args.probe_bytes,
        "sampler": type(sampler).__name__,
        "profile": args.profile,
        "cold_days": args.cold_days,
        "archive_days": args.archive_days,
        "min_codec_gain": args.min_codec_gain,
        "recompress": args.recompress,
        "benchmark": getattr(args, "benchmark_data", None),
    }
    encoded = json.dumps(policy, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]

# ─────────────────────────── Sampler strategy ────────────────────────────────
class Sampler:
    def ratio(self, path: Path) -> float:
        raise NotImplementedError


@dataclass(frozen=True)
class ProbeResult:
    algorithm: str
    ratio: float
    seconds: float
    sampled_bytes: int
    confidence: float


class CodecEstimator:
    """Estimate APFS codec ratios with public, in-process encoders."""
    def __init__(self):
        self._encode = None
        try:
            library = ctypes.CDLL("/usr/lib/libcompression.dylib")
            encode = library.compression_encode_buffer
            encode.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                               ctypes.c_void_p, ctypes.c_size_t,
                               ctypes.c_void_p, ctypes.c_uint32]
            encode.restype = ctypes.c_size_t
            self._encode = encode
        except (AttributeError, OSError):
            pass

    @staticmethod
    def _raw_zlib(data: bytes, level: int) -> int:
        encoder = zlib.compressobj(level, zlib.DEFLATED, -15)
        return len(encoder.compress(data) + encoder.flush())

    def compressed_length(self, data: bytes, algorithm: str) -> int:
        if algorithm == "zlib":
            # The documentation of the Apple Compression framework gives raw
            # DEFLATE level 5.
            return self._raw_zlib(data, 5)
        if algorithm == "lzfse" and self._encode:
            destination = ctypes.create_string_buffer(len(data) + 65_536)
            length = self._encode(destination, len(destination), data,
                                  len(data), None, COMPRESSION_LZFSE)
            return int(length) if length else len(data)
        # LZVN has no public buffer encoder. Raw DEFLATE level 1 is only a
        # fast, conservative estimate of compressibility. The tool always
        # measures the actual result from the allocated blocks after
        # compression.
        return self._raw_zlib(data, 1)


class StratifiedSampler(Sampler):
    """Probe regions at equal intervals and stay in a fixed I/O budget."""
    def __init__(self, probe_bytes: int, read_limiter=None):
        self.n = probe_bytes
        self.read_limiter = read_limiter
        self.estimator = CodecEstimator()

    def _read(self, stream, byte_count: int) -> bytes:
        if self.read_limiter is not None:
            self.read_limiter.throttle(byte_count)
        return stream.read(byte_count)

    def _segments(self, path: Path) -> Iterator[bytes]:
        """Yield the sampled regions. Only one chunk is in memory at a time."""
        size = path.stat().st_size
        budget = size if self.n == 0 else min(size, self.n)
        with path.open("rb") as stream:
            if budget == size:
                remaining = size
                while remaining > 0:
                    data = self._read(
                        stream, min(remaining, FULL_READ_CHUNK_BYTES)
                    )
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
                return
            # With many small regions, one unusual part does not control the
            # estimate for the full file. An example of such a part is the
            # zero-filled start of a disk image.
            regions = max(3, min(MAX_SAMPLE_REGIONS,
                                 budget // SAMPLE_REGION_BYTES))
            chunk = max(1, budget // regions)
            for index in range(regions):
                stream.seek((size - chunk) * index // (regions - 1))
                yield self._read(stream, chunk)

    def probe(self, path: Path, algorithms: list[str]) -> dict:
        sampled = 0
        compressed = dict.fromkeys(algorithms, 0)
        seconds = dict.fromkeys(algorithms, 0.0)
        for data in self._segments(path):
            sampled += len(data)
            for algorithm in algorithms:
                started = time.perf_counter()
                compressed[algorithm] += self.estimator.compressed_length(
                    data, algorithm
                )
                seconds[algorithm] += time.perf_counter() - started
        confidence = min(1.0, sampled / max(1, path.stat().st_size))
        return {
            algorithm: ProbeResult(
                algorithm, compressed[algorithm] / sampled if sampled else 1.0,
                seconds[algorithm], sampled, confidence,
            )
            for algorithm in algorithms
        }

    def ratio(self, path: Path) -> float:
        return self.probe(path, ["lzvn"])["lzvn"].ratio


def choose_algorithm(args, path: Path, st: os.stat_result,
                     sampler: Sampler) -> ProbeResult:
    requested = args.algo
    age_days = max(0.0, (time.time() - st.st_mtime) / 86400)
    algorithms = [requested] if requested != "auto" else ["lzvn"]
    if requested == "auto" and args.profile != "fast" and age_days >= args.cold_days:
        algorithms.append("lzfse")
    if (requested == "auto" and args.profile == "max" and
            age_days >= args.archive_days):
        algorithms.append("zlib")

    if hasattr(sampler, "probe"):
        results = sampler.probe(path, algorithms)
    else:
        algorithm = algorithms[0]
        results = {algorithm: ProbeResult(
            algorithm, sampler.ratio(path), 0.0, st.st_size, 1.0
        )}
    selected = results[algorithms[0]]
    benchmark = getattr(args, "benchmark_data", None) or {}
    throughput = benchmark.get("codecs", {})
    for algorithm in algorithms[1:]:
        candidate = results[algorithm]
        if candidate.ratio > selected.ratio - args.min_codec_gain:
            continue
        selected_speed = throughput.get(selected.algorithm, {}).get(
            "throughput_mib_s"
        )
        candidate_speed = throughput.get(candidate.algorithm, {}).get(
            "throughput_mib_s"
        )
        if selected_speed and candidate_speed:
            selected_seconds = st.st_size / (selected_speed * (1 << 20))
            candidate_seconds = st.st_size / (candidate_speed * (1 << 20))
            extra_seconds = candidate_seconds - selected_seconds
            incremental_saving = st.st_size * (
                selected.ratio - candidate.ratio
            )
            if (extra_seconds > 0 and
                    incremental_saving / extra_seconds <
                    args.min_savings_per_second):
                continue
        selected = candidate
    return selected


def compression_algorithm(path: Path) -> str | None:
    """Return the transparent-compression codec of path, or None if unknown.

    The codec comes from the compression metadata of the file. afsctool is
    asked only about a compression type that this tool does not know.
    """
    if already_compressed(path):
        codec = native().codec(path)
        if codec:
            return codec
    afsctool = shutil.which("afsctool")
    if not afsctool:
        return None
    process = run_command([afsctool, "-vv", str(path)], check=False)
    for line in process.stdout.splitlines():
        if line.startswith("Compression type:"):
            upper = line.upper()
            for algorithm in ("LZVN", "LZFSE", "ZLIB"):
                if algorithm in upper:
                    return algorithm.lower()
    return None


def recompress_file(path: Path, new_algorithm: str,
                    old_algorithm: str | None) -> bool:
    """Recompress with afsctool. On failure, restore the old codec.

    Return True if a copy replaced the file.
    """
    if TOOL != "afsctool":
        raise RuntimeError("safe recompression requires afsctool")
    if is_mach_o(path):
        try:
            atomic_compress_file(path, new_algorithm)
        except RuntimeError as exc:
            raise CompressorRefusedError({path: exc}).error_for_path(path)
        return True
    require_unused(path)
    run_command([BASE[0], "-d", str(path)])
    try:
        run_compressor(path, new_algorithm)
        if not already_compressed(path):
            raise RuntimeError(f"recompression did not compress {path}")
    except BaseException:
        if old_algorithm:
            try:
                run_compressor(path, old_algorithm)
            except BaseException:
                logger.exception("Could not restore %s using %s",
                                 path, old_algorithm)
        raise
    return False


def decompress_file(path: Path) -> None:
    """Remove transparent compression with afsctool."""
    if TOOL != "afsctool":
        raise RuntimeError("safe decompression requires afsctool")
    if is_mach_o(path):
        atomic_decompress_file(path)
    else:
        require_unused(path)
        run_command([BASE[0], "-d", str(path)])


def recover_recompressions(cache: StateCache, dry_run: bool = False,
                           discard_missing: bool = False) -> dict:
    """Resolve the recompression records that interrupted processes left.

    If the file of a record is gone but its directory exists, the file was
    deleted or moved, and this function removes the record. If the
    directory is also gone, it is possible that the volume is only
    unmounted. Then this function keeps and reports the record, if
    discard_missing is not set.
    """
    records = []
    failures = []
    for operation in cache.pending_recompressions():
        path = Path(operation["path"])
        try:
            if not path.exists():
                if not (discard_missing or path.parent.is_dir()):
                    raise FileNotFoundError(
                        "file and its directory are missing; mount the "
                        "volume or discard the record with 'state recover "
                        "--discard-missing --yes'"
                    )
                current = None
                status = "missing"
            else:
                current = compression_algorithm(path)
                if current in {operation["old_algorithm"],
                               operation["new_algorithm"]}:
                    status = "stable"
                elif dry_run:
                    status = "would_restore"
                else:
                    run_compressor(path, operation["old_algorithm"])
                    current = compression_algorithm(path)
                    if current != operation["old_algorithm"]:
                        raise RuntimeError(
                            "could not verify restoration of the previous "
                            "codec"
                        )
                    status = "restored"
            records.append({**operation, "status": status,
                            "current_algorithm": current})
            if not dry_run:
                cache.finish_recompression(path)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            failure = {
                **operation,
                "status": "error",
                "error": "recompression_recovery_failed",
                "message": str(exc),
                "retryable": True,
            }
            records.append(failure)
            failures.append(failure)
    statuses = [record["status"] for record in records]
    return {
        "dry_run": dry_run,
        "operations": records,
        "recovered": statuses.count("stable") + statuses.count("restored"),
        "missing": statuses.count("missing"),
        "errors": len(failures),
        "failures": failures,
    }


def inspect_volume(path: Path) -> dict:
    resolved = path.expanduser().resolve(strict=True)
    target = resolved if resolved.is_dir() else resolved.parent
    mount = native().mount(target)
    flags = os.statvfs(target).f_flag
    writable = not bool(flags & getattr(os, "ST_RDONLY", 1))
    return {
        "path": str(resolved),
        "device": target.stat().st_dev,
        "mountpoint": mount.point,
        "filesystem": mount.fstype,
        "writable": writable,
        "compression_supported": mount.fstype in {"apfs", "hfs"},
    }


def inspect_file(path: Path) -> dict:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise AppError(
            "not_a_file", f"not a regular file: {resolved}", EXIT_USAGE,
            "provide explicit regular-file paths", path=str(resolved),
        )
    st = resolved.stat(follow_symlinks=False)
    compressed = already_compressed(resolved)
    return {
        "path": str(resolved),
        "compressed": compressed,
        "algorithm": compression_algorithm(resolved) if compressed else None,
        "logical_bytes": st.st_size,
        "allocated_bytes": allocated_size(st),
    }


def inspect_files(paths: list[Path]) -> dict:
    return {"files": [inspect_file(path) for path in paths]}


def decompress_files(paths: list[Path], dry_run: bool,
                     confirmed: bool) -> dict:
    if not dry_run and not confirmed:
        raise AppError(
            "confirmation_required", "file decompress requires --yes",
            EXIT_USAGE, "preview with --dry-run or execute with --yes",
        )
    if not dry_run and TOOL != "afsctool":
        raise AppError(
            "unsupported_backend", "file decompression requires afsctool",
            EXIT_UNSUPPORTED, "install afsctool and retry", tool=TOOL,
        )
    records = []
    failures = []
    changed = 0
    unchanged = 0
    for path in paths:
        try:
            before = inspect_file(path)
            if not before["compressed"]:
                before.update(status="unchanged", changed=False)
                records.append(before)
                unchanged += 1
                continue
            if dry_run:
                before.update(status="planned", changed=False)
                records.append(before)
                changed += 1
                continue
            decompress_file(Path(before["path"]))
            after = inspect_file(Path(before["path"]))
            if after["compressed"]:
                raise RuntimeError("file remains compressed after decompression")
            after.update(status="decompressed", changed=True)
            records.append(after)
            changed += 1
        except (AppError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            failure = {
                "path": str(path),
                "status": "error",
                "error": (exc.error if isinstance(exc, AppError)
                          else "decompression_failed"),
                "message": str(exc),
                "retryable": not isinstance(exc, AppError),
            }
            failures.append(failure)
            records.append(failure)
    return {
        "dry_run": dry_run,
        "files": records,
        "files_changed": changed,
        "files_unchanged": unchanged,
        "errors": len(failures),
        "failures": failures,
    }


def local_snapshots(mountpoint: str) -> int:
    """Count the local Time Machine snapshots, which hold rewritten blocks."""
    tmutil = shutil.which("tmutil")
    if not tmutil or not mountpoint:
        return 0
    process = run_command([tmutil, "listlocalsnapshots", mountpoint],
                          check=False)
    return sum(line.startswith("com.apple.")
               for line in process.stdout.splitlines())


def validate_target_filesystems(roots: list[Path]) -> list[dict]:
    """Reject plans and changes that are not on writable APFS/HFS volumes.

    Return one description for each different target volume.
    """
    volumes = []
    checked = set()
    for root in roots:
        device = root.stat().st_dev
        if device in checked:
            continue
        checked.add(device)
        info = inspect_volume(root)
        if not info["compression_supported"]:
            raise AppError(
                "unsupported_filesystem",
                f"{info['path']} uses unsupported filesystem "
                f"{info['filesystem']!r}",
                EXIT_UNSUPPORTED,
                "choose a writable APFS or HFS+ target",
                path=info["path"], filesystem=info["filesystem"],
            )
        if not info["writable"]:
            raise AppError(
                "read_only_filesystem", f"{info['path']} is read-only",
                EXIT_PERMISSION, "choose a writable target",
                path=info["path"],
            )
        volumes.append(info)
    return volumes


def inspect_state(db_path: Path) -> dict:
    db_path = db_path.expanduser().resolve()
    if not db_path.exists():
        return {"state_db": str(db_path), "exists": False, "entries": 0,
                "bytes": 0, "outcomes": {}, "pending_recompressions": 0}
    connection = connect_readonly(db_path)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "file_state" not in tables:
            return {"state_db": str(db_path), "exists": True, "entries": 0,
                    "bytes": db_path.stat().st_size, "outcomes": {},
                    "pending_recompressions": 0}
        entries = connection.execute("SELECT COUNT(*) FROM file_state").fetchone()[0]
        outcomes = dict(connection.execute(
            "SELECT outcome, COUNT(*) FROM file_state GROUP BY outcome"
        ))
        pending = (connection.execute(
            "SELECT COUNT(*) FROM recompression_operations"
        ).fetchone()[0] if "recompression_operations" in tables else 0)
        return {"state_db": str(db_path), "exists": True,
                "entries": entries, "bytes": db_path.stat().st_size,
                "outcomes": outcomes, "pending_recompressions": pending}
    finally:
        connection.close()


def clear_state(db_path: Path, dry_run: bool, confirmed: bool) -> dict:
    db_path = db_path.expanduser().resolve()
    existing = [path for path in (db_path, Path(f"{db_path}-wal"),
                                  Path(f"{db_path}-shm")) if path.exists()]
    if dry_run:
        return {"state_db": str(db_path), "action": "clear",
                "dry_run": True, "would_remove": [str(path) for path in existing],
                "changed": False}
    if not confirmed:
        raise AppError(
            "confirmation_required", "state clear requires --yes",
            EXIT_USAGE, "preview with --dry-run or execute with --yes",
            state_db=str(db_path),
        )
    for path in existing:
        path.unlink()
    return {"state_db": str(db_path), "action": "clear", "dry_run": False,
            "removed": [str(path) for path in existing],
            "changed": bool(existing)}


def run_benchmark(output_path: Path) -> dict:
    estimator = CodecEstimator()
    corpus = (
        (b"alpha,beta,gamma,delta\n" * 65_536) +
        bytes(range(256)) * 4096 +
        os.urandom(1 << 20)
    )
    results = {}
    for algorithm in ("lzvn", "lzfse", "zlib"):
        started = time.perf_counter()
        compressed = estimator.compressed_length(corpus, algorithm)
        elapsed = max(time.perf_counter() - started, 1e-9)
        results[algorithm] = {
            "ratio": round(compressed / len(corpus), 4),
            "throughput_mib_s": round(len(corpus) / elapsed / (1 << 20), 1),
        }
    report = {"generated_at": int(time.time()), "bytes": len(corpus),
              "codecs": results}
    output_path = output_path.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output_path, json.dumps(report, indent=2) + "\n")
    return report


def load_benchmark(input_path: Path) -> dict | None:
    """Load a valid saved codec benchmark. The benchmark is optional."""
    path = input_path.expanduser()
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text())
        codecs = report["codecs"]
        for algorithm in ("lzvn", "lzfse", "zlib"):
            speed = codecs[algorithm]["throughput_mib_s"]
            if not isinstance(speed, (int, float)) or speed <= 0:
                return None
        return report
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None

# ─────────────────────────── Compression helpers ─────────────────────────────
def find_compressor(backend: str = DEFAULT_BACKEND) -> tuple[str, list[str]]:
    """Return (tool, base_cmd_list) for the specified backend."""
    if backend == "afsctool":
        return "afsctool", ["afsctool", "-c", "-v"]
    if backend == "applesauce":
        return "applesauce", ["applesauce", "compress"]
    # ditto must have a temporary copy.
    return "ditto", ["ditto", "--hfsCompression", *ditto_clone_flag()]


def ditto_clone_flag() -> list[str]:
    """Return the flag that makes ditto copy data, if ditto has such a flag.

    A ditto with clone support makes same-volume copies as clones by
    default. A clone always keeps the compression state of its source.
    """
    usage = run_command(["ditto"], check=False).stderr
    return ["--noclone"] if "--noclone" in usage else []


TOOL, BASE = find_compressor()
# Copy command that keeps the metadata but not the transparent compression.
DITTO_COPY = ["ditto", "--nopreserveHFSCompression"]


def select_backend(backend: str, required: bool = True,
                   selectable: bool = False) -> None:
    """Bind the module-level compressor. A required tool must be installed.

    selectable identifies the commands that accept --backend. For these
    commands, ditto is an alternative.
    """
    global TOOL, BASE, DITTO_COPY
    if required and not shutil.which(backend):
        alternative = (" or pass --backend ditto"
                       if selectable and backend != "ditto" else "")
        raise AppError(
            "unsupported_backend", f"{backend} is not installed",
            EXIT_UNSUPPORTED, f"install {backend}{alternative}", tool=backend,
        )
    TOOL, BASE = find_compressor(backend)
    DITTO_COPY = ["ditto", "--nopreserveHFSCompression", *ditto_clone_flag()]


def afsctool_compress_command(paths, algo: str) -> list[str]:
    return [BASE[0], "-c", "-v", "-T", algo.upper(), "-j1",
            *map(str, paths)]


def stat_identity(st: os.stat_result) -> tuple:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def describe_users(uses: list[OpenUse]) -> list[dict]:
    """Return one report record for each process and access mode in uses."""
    return [{"pid": use.pid, "command": native().process_name(use.pid),
             "access": use.access} for use in dict.fromkeys(uses)]


def open_file_users(path: Path) -> list[dict]:
    """Return the other processes that have path open or mapped.

    Files are matched by device and inode, not by name. Thus the spelling of
    the path does not matter, for example its letter case on a
    case-insensitive volume. Without root, only the processes of this user
    are visible.
    """
    st = path.stat(follow_symlinks=False)
    return describe_users(
        native().open_file_users().get((st.st_dev, st.st_ino), [])
    )


def snapshot_open_files() -> dict:
    """Map (device, inode) of each file that other processes can write to
    the ways in which they hold it.

    A compressed file rejects writes through descriptors that were open
    before the compression. Writes through earlier mappings cause a fault.
    Writes during the compression are lost. The snapshot does not tell if a
    mapping is writable, thus each mapped file is included. Without root,
    only the processes of this user are visible.
    """
    writers = {}
    for key, uses in native().open_file_users().items():
        uses = [use for use in uses if use.access in WRITE_ACCESS]
        if uses:
            writers[key] = uses
    return writers


class OpenFiles:
    """View of mapped files and files open for write, refreshed regularly."""
    REFRESH_SECONDS = 5.0

    def __init__(self):
        self._lock = threading.Lock()
        self._taken = None
        self._users = {}

    def busy(self, paths) -> dict:
        """Return {path: users} for paths that other processes can write."""
        with self._lock:
            now = time.monotonic()
            if (self._taken is None or
                    now - self._taken > self.REFRESH_SECONDS):
                self._users = snapshot_open_files()
                self._taken = now
            busy = {}
            for path in paths:
                try:
                    st = path.stat(follow_symlinks=False)
                except OSError:
                    continue   # The caller reports a file that is gone.
                uses = self._users.get((st.st_dev, st.st_ino))
                if uses:
                    busy[path] = describe_users(uses)
            return busy


OPEN_FILES = OpenFiles()


def require_unused(path: Path) -> None:
    users = OPEN_FILES.busy([path]).get(path)
    if users:
        raise FileInUseError(path, users)


def replace_unchanged_source(publication, original: os.stat_result) -> None:
    """Verify the copy and keep the displaced inode until publication is checked."""
    path, temporary = publication.path, publication.payload
    fs = publication.native
    expected = signature(original)
    if signature(path.stat(follow_symlinks=False)) != expected:
        raise RuntimeError("source changed before atomic replacement")
    # ditto can make an unreadable copy of a file that has stale compression
    # metadata. Thus, read the full copy before it replaces the file.
    if not filecmp.cmp(temporary, path, shallow=False):
        raise RuntimeError("copy differs from the source")
    users = open_file_users(path)
    if users:
        raise FileInUseError(path, users)
    if signature(path.stat(follow_symlinks=False)) != expected:
        raise RuntimeError("source changed before atomic replacement")
    metadata = fs.metadata(path, nofollow=True)
    copied = fs.metadata(temporary, nofollow=True)
    if copied != metadata:
        fs.restore(temporary, metadata, original.st_atime_ns)
        copied = fs.metadata(temporary, nofollow=True)
    if copied != metadata:
        if copied[6].get(PROVENANCE_XATTR) != metadata[6].get(PROVENANCE_XATTR):
            raise RuntimeError("macOS did not preserve com.apple.provenance")
        raise RuntimeError("staged compression copy does not preserve source metadata")
    publication.publish(expected)


def require_settled_publication(stage: Path) -> None:
    if os.path.lexists(stage):
        raise AppError(
            "publication_pending", f"Compression recovery stage exists: {stage}",
            EXIT_CONFLICT, "run file repair --only interrupted_swap for the affected path",
            path=str(stage),
        )


def is_mach_o(path: Path) -> bool:
    """Tell if path is an executable image (program or library).

    The kernel caches the code signature of an image that it loaded. A
    write to such a file in place makes that cache stale. Then the kernel
    kills each launch until the file gets a new inode. Thus, the tool only
    replaces images.
    """
    try:
        with path.open("rb") as stream:
            return stream.read(4) in MACH_O_MAGICS
    except OSError:
        return False


@contextlib.contextmanager
def sibling_copy(path: Path, copy_command: list[str]):
    """Yield (copy, original stat) for a sibling copy that keeps metadata."""
    original = path.stat(follow_symlinks=False)
    if original.st_nlink > 1:
        raise RuntimeError(
            "multiply-linked file cannot be replaced without breaking links"
        )
    with staged_replacement(native(), path) as publication:
        run_command([*copy_command, str(path.absolute()), publication.payload.name],
                    cwd=publication.payload.parent)
        yield publication, original


def uncompressed_sibling_copy(path: Path):
    return sibling_copy(path, DITTO_COPY)


def reseat_file(path: Path) -> None:
    """Give a compressed file a new inode and keep its compression.

    This clears the stale code-signature state of an executable image that
    was run and then written in place; see is_mach_o.
    """
    with sibling_copy(path, ["ditto"]) as (publication, original):
        if not already_compressed(publication.payload):
            raise RuntimeError("copy did not keep its compression")
        replace_unchanged_source(publication, original)


def restore_compressed_flag(path: Path) -> None:
    """Set the compressed flag again on a file that kept its compressed data.

    Then read the full file. Its size must agree with the size in its
    metadata. If not, clear the flag again and do not change the file.
    """
    original = path.stat(follow_symlinks=False)
    expected = recorded_size(path)
    if expected is None:
        raise RuntimeError("compression metadata is missing or unreadable")
    os.chflags(path, original.st_flags | stat.UF_COMPRESSED,
               follow_symlinks=False)
    try:
        length = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                length += len(chunk)
        if length != expected:
            raise RuntimeError(f"content reads as {length} bytes but "
                               f"{expected} were recorded")
    except (OSError, RuntimeError) as exc:
        os.chflags(path, original.st_flags, follow_symlinks=False)
        reason = os_error_text(exc) if isinstance(exc, OSError) else exc
        raise RuntimeError(f"content could not be recovered: {reason}")


def boot_time() -> int:
    """Return the Unix time of the last boot, or 0 if it is unknown."""
    process = run_command(["sysctl", "-n", "kern.boottime"], check=False)
    match = re.search(r"sec = (\d+)", process.stdout or "")
    return int(match.group(1)) if match else 0


def reseat_images(cache: StateCache, roots: list[Path], dry_run: bool,
                  every: bool) -> dict:
    """Reseat the compressed executable images that are recorded below roots.

    If every is not set, select only the images that can have stale
    signature state. These are images not written by replacement, with a
    record made after the last boot or at an unknown time.
    """
    booted = boot_time()
    records = []
    failures = []
    selected = {}
    for root in roots:
        for entry in cache.entries(outcome="compressed", under=root):
            selected[entry["path"]] = entry
    for entry in selected.values():
        path = Path(entry["path"])
        recorded = entry["recorded_at"]
        if not every and (entry["method"] == "replace" or
                          (recorded is not None and recorded < booted)):
            continue
        if not is_mach_o(path) or not already_compressed(path):
            continue
        if dry_run:
            records.append({"path": str(path), "status": "planned"})
            continue
        try:
            reseat_file(path)
            cache.mark(path, path.stat(follow_symlinks=False),
                       entry["policy"], "compressed", "replace")
            records.append({"path": str(path), "status": "reseated"})
        except FileInUseError as exc:
            failures.append({
                "path": str(path), "status": "error", "error": "file_in_use",
                "message": str(exc), "retryable": True,
                "open_processes": exc.users,
            })
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            failures.append({
                "path": str(path), "status": "error",
                "error": "reseat_failed", "message": str(exc),
                "retryable": True,
            })
    return {
        "dry_run": dry_run,
        "files": records + failures,
        "files_reseated": len(records),
        "errors": len(failures),
        "failures": failures,
    }


def repair_files(cache: StateCache, roots: list[Path], dry_run: bool,
                 kinds, every_image: bool = False) -> dict:
    """Repair the problems that kinds specifies, at or below roots.

    lost_flag restores the compressed flag of files found on disk.
    stale_image reseats executables recorded in the state database.
    """
    records = []
    failures = []
    restored = recovered = 0
    seen = set()
    if {"lost_flag", "interrupted_swap"}.intersection(kinds):
        for kind, path in find_file_problems(roots):
            if kind not in ("lost_flag", "interrupted_swap") or kind not in kinds:
                continue
            if path in seen:
                continue
            seen.add(path)
            action = "publication_recovered" if kind == "interrupted_swap" else "flag_restored"
            record = {"path": str(path), "repair": action}
            try:
                if kind == "interrupted_swap":
                    restored_path = recover_publication(native(), path, dry_run)
                    record.update(path=str(restored_path), stage=str(path))
                    recovered += 1
                else:
                    if not dry_run:
                        restore_compressed_flag(path)
                    restored += 1
                records.append({**record, "status": ("planned" if dry_run
                                                     else "repaired")})
            except (OSError, RuntimeError, Conflict, ValueError, TypeError, KeyError) as exc:
                failures.append({
                    **record, "status": "error", "error": "repair_failed",
                    "message": str(exc), "retryable": False,
                })
    reseated = 0
    if "stale_image" in kinds:
        images = reseat_images(cache, roots, dry_run, every_image)
        reseated = images["files_reseated"]
        for item in images["files"]:
            if item["status"] == "error":
                failures.append({**item, "repair": "reseated"})
            else:
                records.append({
                    "path": item["path"], "repair": "reseated",
                    "status": "planned" if dry_run else "repaired",
                })
    return {
        "dry_run": dry_run,
        "files": records + failures,
        "files_repaired": restored + reseated + recovered,
        "flags_restored": restored,
        "images_reseated": reseated,
        "publications_recovered": recovered,
        "errors": len(failures),
        "failures": failures,
    }


def atomic_decompress_file(path: Path) -> None:
    """Replace path with an uncompressed copy on a new inode."""
    with uncompressed_sibling_copy(path) as (publication, original):
        if already_compressed(publication.payload):
            raise RuntimeError("temporary copy remained compressed")
        replace_unchanged_source(publication, original)


def declined(path: Path, diagnostics: list[str]) -> RuntimeError:
    """Explain why afsctool gave exit code 0 but did not compress path.

    afsctool writes the file name to stderr when an operation on the file
    fails. If stderr does not contain the file name, compression does not
    make the file smaller.
    """
    prefix = f"{path}: "
    for line in diagnostics:
        if line.startswith(prefix):
            return RuntimeError(line[len(prefix):])
    return NoSavingsError("compressor found no gain")


def atomic_compress_file(path: Path, algo: str) -> None:
    """Compress a copy that keeps metadata, then atomically replace path."""
    with uncompressed_sibling_copy(path) as (publication, original):
        temporary = publication.payload
        process = run_command(afsctool_compress_command([temporary.name], algo),
                              cwd=temporary.parent)
        if not already_compressed(temporary):
            raise declined(Path(temporary.name), process.stderr.splitlines())
        if allocated_size(temporary.stat()) >= allocated_size(original):
            raise NoSavingsError("compression saved no allocated bytes")
        replace_unchanged_source(publication, original)


def ditto_compress_file(path: Path) -> bool:
    """Replace a file with a verified, smaller compressed sibling copy."""
    fs = native()
    original = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1
            or original.st_flags & UNSAFE_FLAGS or original.st_mode & 0o6000):
        raise RuntimeError("source is no longer eligible for atomic replacement")
    original_sharing = fs.sharing(path)
    if original_sharing.private == 0:
        return False
    with staged_replacement(fs, path) as publication:
        temporary = publication.payload
        command = [*BASE, str(path.absolute()), temporary.name]
        process = run_command(command, cwd=temporary.parent)
        logger.debug("Ditto stdout: %s", process.stdout)
        logger.debug("Ditto stderr: %s", process.stderr)
        if signature(path.stat(follow_symlinks=False)) != signature(original):
            raise RuntimeError("source changed during staged compression")
        if not already_compressed(temporary):
            # Some macOS builds write the decmpfs image but leave UF_COMPRESSED
            # unset. Recover only our staging file, then read it back in full.
            if not fs.decmpfs(temporary):
                return False
            restore_compressed_flag(temporary)
        private = fs.sharing(path).private
        reclaimable = allocated_size(original)
        if private is not None:
            reclaimable = min(reclaimable, private)
        if allocated_size(temporary.stat()) >= reclaimable:
            return False
        replace_unchanged_source(publication, original)
        return True


def run_compressor(paths, algo: str) -> set[Path]:
    """Compress paths. Return the paths that a copy replaced.

    Raise CompressorRefusedError with each path that stayed uncompressed.
    """
    if isinstance(paths, Path):
        paths = [paths]
    failures: dict[Path, BaseException | str] = {}
    for path in paths:
        blockers = compression_blockers(path)
        if blockers:
            failures[path] = RuntimeError(
                "file carries " + " and ".join(blockers)
            )
    paths = [path for path in paths if path not in failures]
    if TOOL == "ditto":
        replaced = set()
        for path in paths:
            try:
                if ditto_compress_file(path):
                    replaced.add(path)
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                failures[path] = exc
        if failures:
            raise CompressorRefusedError(failures, replaced)
        return replaced

    # The tool does not write executable images in place; see is_mach_o. It
    # writes other files in place if no other process can write them.
    images = [path for path in paths if is_mach_o(path)]
    busy = OPEN_FILES.busy([path for path in paths if path not in images])
    failures.update((path, FileInUseError(path, users))
                    for path, users in busy.items())
    in_place = [path for path in paths
                if path not in images and path not in busy]
    diagnostics = []
    if in_place:
        command = (afsctool_compress_command(in_place, algo)
                   if TOOL == "afsctool"
                   else [*BASE, "--algorithm", algo, *map(str, in_place)])
        logger.debug("Executing compression command: %s", command)
        process = run_command(command)
        logger.debug("Compression stdout:\n%s", process.stdout)
        logger.debug("Compression stderr:\n%s", process.stderr)
        diagnostics = process.stderr.splitlines()
    if TOOL != "afsctool":
        failures.update(dict.fromkeys(
            images, f"executable images require afsctool, not {TOOL}"
        ))
        if failures:
            raise CompressorRefusedError(failures)
        return set()

    fallback_paths = set()
    for path in [*images, *in_place]:
        if path not in images:
            if already_compressed(path):
                continue
            # Try again on a copy only if the tool reported an error.
            reason = declined(path, diagnostics)
            if isinstance(reason, NoSavingsError):
                failures[path] = reason
                continue
        try:
            atomic_compress_file(path, algo)
            fallback_paths.add(path)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            failures[path] = exc
    if failures:
        raise CompressorRefusedError(failures, fallback_paths)
    return fallback_paths

# ─────────────────────────── Utility helpers ─────────────────────────────────
def already_compressed(path: Path) -> bool:
    """Tell if the filesystem marks path as transparently compressed."""
    try:
        st = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & getattr(stat, "UF_COMPRESSED", 0))


def allocated_size(st: os.stat_result) -> int:
    """Return the physical bytes allocated to a file, if the OS gives them."""
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def estimated_allocated_size(logical_size: int, current_allocated: int,
                            ratio: float) -> int:
    """Estimate the physical size, rounded to an APFS allocation block."""
    block = 4096
    estimated = ((int(logical_size * ratio) + block - 1) // block) * block
    return min(current_allocated, estimated)

@dataclass
class Stats:
    scanned: int = 0
    queued: int = 0
    already_compressed: int = 0
    not_compressed: int = 0
    suggested: int = 0
    suggested_not_compressed: int = 0
    suggested_already_compressed: int = 0
    compressed: int = 0
    skipped_cached: int = 0
    skipped_ratio: int = 0
    skipped_already: int = 0
    skipped_shared: int = 0
    skipped_too_new: int = 0
    skipped_too_small: int = 0
    skipped_excluded_ext: int = 0
    skipped_hardlink: int = 0
    skipped_sparse: int = 0
    skipped_changed: int = 0
    skipped_in_use: int = 0
    skipped_budget: int = 0
    processed_no_savings: int = 0
    recompressed: int = 0
    skipped_recompress_gain: int = 0
    skipped_recompress_unknown: int = 0
    reverted_recompress: int = 0
    reverted_no_savings: int = 0
    errors: int = 0
    conflicts: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    sampled_bytes: int = 0
    sampling_seconds: float = 0.0
    estimated_bytes_saved: int = 0
    ratios: list[float] = field(default_factory=list)
    algorithms: dict = field(default_factory=lambda: defaultdict(int))
    top_candidates: list[tuple] = field(default_factory=list)
    error_messages: dict = field(default_factory=lambda: defaultdict(int))
    stopped_by: str | None = None
    skipped_excluded: int = 0
    skipped_blocked: int = 0
    skipped_not_owned: int = 0
    roots: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=lambda: {
        label: [0, 0, 0] for label, _ in COVERAGE_BUCKETS
    })

    def track_roots(self, roots: list[Path]) -> None:
        self.roots = [
            {"path": str(root), "files_queued": 0, "files_compressed": 0,
             "space_saved": 0, "skipped_not_owned": 0}
            for root in roots
        ]

    def count_root(self, index: int | None, key: str,
                   amount: int = 1) -> None:
        """Add to the counter of one root. Callers must hold the stats lock."""
        if index is not None and index < len(self.roots):
            self.roots[index][key] += amount

    def add_coverage(self, sampled_fraction: float, allocated: int,
                     estimated_saving: int) -> None:
        """Add the estimate of one suggested file to its coverage bucket."""
        label = next(label for label, minimum in COVERAGE_BUCKETS
                     if sampled_fraction >= minimum)
        bucket = self.coverage[label]
        bucket[0] += 1
        bucket[1] += allocated
        bucket[2] += estimated_saving

    def coverage_report(self) -> list[dict]:
        return [
            {"coverage": label, "files": files, "allocated_bytes": allocated,
             "estimated_saved_bytes": saved}
            for label, (files, allocated, saved) in self.coverage.items()
        ]

    def offer_candidate(self, limit: int, sequence: int, record: dict) -> None:
        """Keep the `limit` records that have the highest value score."""
        entry = (record["value_score"], sequence, record)
        if len(self.top_candidates) < limit:
            heapq.heappush(self.top_candidates, entry)
        elif limit:
            heapq.heappushpop(self.top_candidates, entry)

    def ranked_candidates(self) -> list[dict]:
        return [record for _, _, record in
                sorted(self.top_candidates, reverse=True)]


@dataclass(frozen=True)
class Candidate:
    path: Path
    initial_stat: os.stat_result
    initially_compressed: bool = False
    root: int | None = None


# (priority, sequence, candidate). A candidate of None is the end sentinel.
QueueItem = tuple[float, int, Candidate | None]


def candidate_priority(st: os.stat_result, now: float | None = None) -> float:
    """Give priority to large allocated files first, then to older files."""
    now = time.time() if now is None else now
    age_years = max(0.0, now - st.st_mtime) / (365 * 86400)
    coldness = 1.0 + min(age_years, 4.0)
    return allocated_size(st) * coldness


class Progress:
    """One-line run status on stderr, redrawn while stderr is a terminal."""
    INTERVAL_SECONDS = 0.5

    def __init__(self, stats: Stats, dry_run: bool, enabled: bool):
        self.stats = stats
        self.dry_run = dry_run
        self.enabled = enabled
        self.processed = 0
        self.scanning = True
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._started = time.monotonic()
        self._ticker = threading.Thread(target=self._redraw, daemon=True)

    def __enter__(self):
        if self.enabled:
            self._ticker.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.enabled:
            self._done.set()
            self._ticker.join()
            sys.stderr.write(CLEAR_LINE)
            sys.stderr.flush()

    def update(self, n: int = 1) -> None:
        with self._lock:
            self.processed += n

    def scan_finished(self) -> None:
        self.scanning = False

    def line(self) -> str:
        stats = self.stats
        elapsed = int(time.monotonic() - self._started)
        if self.scanning:
            progress = (f"scanning, {stats.scanned:,} found | "
                        f"{self.processed:,}/{stats.queued:,} processed")
        else:
            percent = 100 * self.processed // max(1, stats.queued)
            progress = (f"{self.processed:,}/{stats.queued:,} processed "
                        f"({percent}%)")
        saved = human_bytes(stats.bytes_before - stats.bytes_after)
        fields = [
            progress,
            f"{stats.compressed:,} "
            + ("planned" if self.dry_run else "compressed"),
            f"{saved} " + ("estimated" if self.dry_run else "saved"),
            f"{stats.errors:,} errors",
            f"{elapsed // 3600}:{elapsed % 3600 // 60:02}:{elapsed % 60:02}",
        ]
        return " | ".join(fields)

    def _redraw(self) -> None:
        # A timer draws the line again. Thus, the clock moves during a long
        # file.
        while not self._done.wait(self.INTERVAL_SECONDS):
            width = shutil.get_terminal_size().columns - 1
            sys.stderr.write(f"{CLEAR_LINE}{self.line()[:width]}")
            sys.stderr.flush()


class VolumeLimiter:
    """Limit concurrent compressor writes independently for each volume."""
    def __init__(self, limit: int):
        self.limit = limit
        self._lock = threading.Lock()
        self._semaphores: dict[int, threading.Semaphore] = {}

    def for_device(self, device: int) -> threading.Semaphore:
        with self._lock:
            return self._semaphores.setdefault(
                device, threading.BoundedSemaphore(self.limit)
            )


@dataclass
class BatchJob:
    path: Path
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    replaced: bool = False


class BatchingCompressor:
    """Combine concurrent compatible requests into fewer tool processes."""
    def __init__(self, batch_size: int, volume_limiter: VolumeLimiter,
                 flush_seconds: float = 0.02):
        self.batch_size = batch_size
        self.volume_limiter = volume_limiter
        self.flush_seconds = flush_seconds
        self.lock = threading.Lock()
        self.pending: defaultdict[tuple[int, str], list[BatchJob]] = (
            defaultdict(list))
        self.processes = 0
        self.files = 0
        self.atomic_fallback_files = 0

    def submit(self, path: Path, algorithm: str, device: int) -> bool:
        """Compress path. Return True if a copy replaced it."""
        key = (device, algorithm)
        job = BatchJob(path)
        batch = None
        with self.lock:
            group = self.pending[key]
            group.append(job)
            if len(group) >= self.batch_size:
                batch = group[:self.batch_size]
                del group[:self.batch_size]
        if batch:
            self._execute(key, batch)
        elif not job.done.wait(self.flush_seconds):
            with self.lock:
                group = self.pending[key]
                if job in group:
                    batch = list(group)
                    group.clear()
            if batch:
                self._execute(key, batch)
        job.done.wait()
        if job.error:
            raise job.error
        return job.replaced

    def _execute(self, key, jobs: list[BatchJob]) -> None:
        device, algorithm = key
        with self.lock:
            self.processes += 1
            self.files += len(jobs)
        try:
            with self.volume_limiter.for_device(device):
                fallback_paths = run_compressor(
                    [job.path for job in jobs], algorithm
                )
            self._record_replaced(jobs, fallback_paths)
        except subprocess.CalledProcessError as exc:
            if len(jobs) > 1:
                midpoint = len(jobs) // 2
                self._execute(key, jobs[:midpoint])
                self._execute(key, jobs[midpoint:])
                return
            jobs[0].error = exc
        except CompressorRefusedError as exc:
            self._record_replaced(jobs, exc.fallback_paths)
            for job in jobs:
                if str(job.path) in exc.failures:
                    job.error = exc.error_for_path(job.path)
        except BaseException as exc:  # noqa: BLE001 - waiters raise it again
            for job in jobs:
                job.error = exc
        for job in jobs:
            job.done.set()

    def _record_replaced(self, jobs: list[BatchJob], replaced) -> None:
        replaced = replaced or ()
        for job in jobs:
            job.replaced = job.path in replaced
        with self.lock:
            self.atomic_fallback_files += len(replaced)

    def recompress(self, path: Path, new_algorithm: str,
                   old_algorithm: str | None, device: int) -> bool:
        """Recompress path. Return True if a copy replaced it."""
        with self.volume_limiter.for_device(device):
            replaced = recompress_file(path, new_algorithm, old_algorithm)
        with self.lock:
            self.processes += 2
            self.files += 1
        return replaced

    def decompress(self, path: Path, device: int) -> None:
        with self.volume_limiter.for_device(device):
            decompress_file(path)
        with self.lock:
            self.processes += 1


class WorkBudget:
    def __init__(self, seconds: float, target_bytes: int):
        self.deadline = time.monotonic() + seconds if seconds else None
        self.target = target_bytes
        self.saved = 0
        self.reserved = 0
        self.lock = threading.Lock()

    def exhausted(self) -> bool:
        with self.lock:
            return ((self.deadline is not None and
                     time.monotonic() >= self.deadline) or
                    (self.target > 0 and self.saved >= self.target))

    def claim(self, estimated_saving: int) -> bool:
        with self.lock:
            current = self.saved + self.reserved
            if ((self.deadline is not None and time.monotonic() >= self.deadline) or
                    (self.target > 0 and current >= self.target) or
                    (self.target > 0 and current > 0 and
                     current + max(0, estimated_saving) > self.target)):
                return False
            self.reserved += max(0, estimated_saving)
            return True

    def limit(self) -> str:
        """Return the name of the limit that refuses work."""
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return "time_budget"
        return "space_target"

    def remaining(self) -> float | None:
        """Seconds until the time budget ends, or None if there is no limit."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def complete(self, estimate: int, actual: int) -> None:
        with self.lock:
            self.reserved = max(0, self.reserved - max(0, estimate))
            self.saved += max(0, actual)


class RateLimiter:
    """Thread-safe limiter of the total throughput."""
    def __init__(self, megabytes_per_second: float,
                 stop: threading.Event | None = None):
        self.rate = megabytes_per_second * 1024 * 1024
        self.next_at = time.monotonic()
        self.lock = threading.Lock()
        self.stop = stop

    def throttle(self, byte_count: int,
                 timeout: float | None = None) -> bool:
        """Reserve transfer time for byte_count and wait for it.

        Return False and reserve nothing if the wait is longer than timeout.
        Raise RunStopped if the run stops before or during the wait.
        """
        if self.stop is not None and self.stop.is_set():
            raise RunStopped
        if not self.rate or byte_count <= 0:
            return True
        with self.lock:
            now = time.monotonic()
            finish = max(now, self.next_at) + byte_count / self.rate
            delay = finish - now
            if timeout is not None and delay > timeout:
                return False
            self.next_at = finish
        if self.stop is None:
            time.sleep(delay)
        elif self.stop.wait(delay):
            raise RunStopped
        return True

# ─────────────────────────── Producer & Consumer ─────────────────────────────
def compile_exclusions(patterns: list[str]) -> list[str]:
    """Normalize --exclude patterns. Path patterns become absolute globs."""
    compiled = []
    for pattern in patterns:
        if "/" in pattern:
            pattern = os.path.expanduser(pattern).rstrip("/")
            if not os.path.isabs(pattern):
                pattern = "*/" + pattern
        compiled.append(pattern)
    return compiled


def is_excluded(path: str, name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if "/" not in pattern:
            if fnmatch.fnmatchcase(name, pattern):
                return True
        elif (path == pattern or path.startswith(pattern + "/") or
              fnmatch.fnmatchcase(path, pattern)):
            return True
    return False


def put_unless_stopped(q: queue.Queue, item, stop: threading.Event) -> bool:
    """Enqueue item. Stop the attempt when the run stops."""
    while not stop.is_set():
        try:
            q.put(item, timeout=QUEUE_POLL_SECONDS)
            return True
        except queue.Full:
            continue
    return False


def get_unless_stopped(q: queue.Queue, stop: threading.Event):
    """Dequeue the next item, or return None when the run stops."""
    while not stop.is_set():
        try:
            return q.get(timeout=QUEUE_POLL_SECONDS)
        except queue.Empty:
            continue
    return None


def scanner(args, roots: list[Path], q: queue.Queue[QueueItem], stats: Stats,
            lock: threading.Lock, reporter: Reporter | None = None,
            stop: threading.Event | None = None):
    stop = stop or threading.Event()
    cutoff_ts = time.time() - args.cutoff_days * 86400
    exts: set[str] = {
        e if e.startswith(".") else f".{e}"
        for e in (part.strip().lower() for part in args.exclude_ext.split(","))
        if e
    }
    ex_dirs: set[str] = {
        name.strip() for name in args.exclude_dir.split(",") if name.strip()
    }
    exclusions = getattr(args, "exclusions", [])
    # Only the owner of a file can set its compression flag.
    user = os.geteuid()
    count = 0
    stack = [(root, index) for index, root in reversed(list(enumerate(roots)))]

    def record_error(path: Path, exc: OSError) -> None:
        if isinstance(exc, PermissionError):
            error = "permission_denied"
            retryable = False
        elif isinstance(exc, FileNotFoundError):
            error = "not_found"
            retryable = True
        else:
            error = "scan_failed"
            retryable = False
        with lock:
            stats.errors += 1
            stats.error_messages[error] += 1
        if reporter is not None:
            reporter.file(path, "error", error=error,
                          message=os_error_text(exc), retryable=retryable)

    while stack and not stop.is_set():
        root, root_index = stack.pop()
        if root.is_file():
            if not getattr(args, "dry_run", True):
                require_settled_publication(publication_directory(root))
            entries = [root]
        else:
            try:
                with os.scandir(root) as scan:
                    entries = []
                    for entry in scan:
                        if is_excluded(entry.path, entry.name, exclusions):
                            with lock:
                                stats.skipped_excluded += 1
                        elif entry.is_dir(follow_symlinks=False):
                            if (not getattr(args, "dry_run", True)
                                    and entry.name.startswith(PUBLICATION_PREFIX)):
                                require_settled_publication(Path(entry.path))
                            if (entry.name not in ex_dirs and
                                    not entry.name.startswith(STAGING_PREFIXES)):
                                stack.append((Path(entry.path), root_index))
                        elif entry.is_file(follow_symlinks=False):
                            entries.append(Path(entry.path))
            except OSError as exc:
                logger.error("Cannot scan %s: %s", root, exc)
                record_error(root, exc)
                continue

        for fp in entries:
            with lock:
                stats.scanned += 1
            if fp in args.internal_paths:
                continue
            if fp.suffix.lower() in exts:
                with lock:
                    stats.skipped_excluded_ext += 1
                continue
            try:
                st = fp.stat(follow_symlinks=False)
            except OSError as exc:
                record_error(fp, exc)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if user != 0 and st.st_uid != user:
                with lock:
                    stats.skipped_not_owned += 1
                    stats.count_root(root_index, "skipped_not_owned")
                continue
            if (st.st_nlink > 1 and
                    (not args.include_hardlinks or TOOL == "ditto")):
                with lock:
                    stats.skipped_hardlink += 1
                continue
            is_compressed = already_compressed(fp)
            if (not args.include_sparse and not is_compressed and
                    allocated_size(st) < st.st_size):
                with lock:
                    stats.skipped_sparse += 1
                continue
            if st.st_size < args.min_size:
                with lock:
                    stats.skipped_too_small += 1
                continue
            if st.st_mtime > cutoff_ts:
                with lock:
                    stats.skipped_too_new += 1
                continue
            if not is_compressed and compression_blockers(fp):
                with lock:
                    stats.skipped_blocked += 1
                continue
            priority = candidate_priority(st)
            if not put_unless_stopped(
                    q, (-priority, count,
                        Candidate(fp, st, is_compressed, root_index)),
                    stop):
                return
            count += 1
            with lock:
                stats.queued += 1
                stats.count_root(root_index, "files_queued")
                if is_compressed:
                    stats.already_compressed += 1
                else:
                    stats.not_compressed += 1
            if count % 100 == 0:
                logger.debug("Scanner queued %d files so far", count)
    logger.info("Scanner finished, total queued: %d", count)
    if not getattr(args, "dry_run", True):
        # Report one failure for each root, not one for each file.
        for root_entry in stats.roots:
            if root_entry["skipped_not_owned"] and reporter is not None:
                with lock:
                    stats.errors += 1
                    stats.error_messages["not_owner"] += 1
                reporter.file(
                    Path(root_entry["path"]), "error", error="not_owner",
                    message=f"{root_entry['skipped_not_owned']} files belong to "
                            "another user and were not compressed",
                    retryable=False, files=root_entry["skipped_not_owned"],
                    suggestion="rerun with sudo, or exclude the path",
                )
    # Add the end sentinel.
    put_unless_stopped(q, (float("inf"), count, None), stop)


@dataclass
class WorkerContext:
    """The collaborators that all worker threads of one run share."""
    args: argparse.Namespace
    sampler: Sampler
    cache: StateCache
    bar: Progress
    stats: Stats
    lock: threading.Lock
    batcher: BatchingCompressor
    policy: str
    budget: WorkBudget
    write_limiter: RateLimiter
    reporter: Reporter

    def __post_init__(self):
        self.top_limit = getattr(self.args, "top", 10)
        self.plan_records = getattr(self.args, "plan_records", {})
        self.audit_compressed = getattr(self.args, "audit_compressed", False)


class FileTask:
    """The decision and the work for one candidate file.

    process() runs the steps in order. A step that settles the file reports
    the outcome and returns True; then no later step runs. The steps before
    reserve() only read. run() gives each failure its error record.
    """
    def __init__(self, context: WorkerContext, sequence: int,
                 candidate: Candidate):
        self.context = context
        self.sequence = sequence
        self.candidate = candidate
        self.path = candidate.path
        # The part of the space budget that this file holds until it ends.
        self.reserved_saving = 0
        # check_identity() sets these.
        self.st: os.stat_result = candidate.initial_stat
        self.identity: tuple = ()
        self.original_size = 0
        self.original_allocated = 0
        self.planned: dict | None = None
        # classify() sets these.
        self.was_compressed = False
        self.audit_existing = False
        self.old_algorithm: str | None = None
        # select() sets these.
        self.selection = ProbeResult("", 1.0, 0.0, 0, 0.0)
        self.estimated_after_size = 0
        self.estimated_saving = 0

    # ── reporting ────────────────────────────────────────────────────────────
    def finish(self, status: str, **fields) -> None:
        self.context.reporter.file(self.path, status, **fields)
        self.context.bar.update()

    def skip(self, counter: str, status: str, **fields) -> None:
        stats = self.context.stats
        with self.context.lock:
            setattr(stats, counter, getattr(stats, counter) + 1)
        self.finish(status, **fields)

    def skip_budget(self, limit: str | None = None) -> None:
        stats = self.context.stats
        with self.context.lock:
            stats.stopped_by = (stats.stopped_by or limit or
                                self.context.budget.limit())
        self.skip("skipped_budget", "budget_exhausted")

    def release_budget(self, actual_saving: int = 0) -> None:
        """Return the reservation of this file and record the real saving."""
        self.context.budget.complete(self.reserved_saving, actual_saving)
        self.reserved_saving = 0

    def report_failure(self, error: str, message: str, retryable: bool,
                       *, in_use: bool = False, **fields) -> None:
        self.release_budget()
        stats = self.context.stats
        with self.context.lock:
            stats.errors += 1
            stats.error_messages[error] += 1
            if in_use:
                stats.skipped_in_use += 1
        self.finish("error", error=error, message=message,
                    retryable=retryable, **fields)

    def plan_conflict(self, message: str, **fields) -> None:
        with self.context.lock:
            self.context.stats.conflicts += 1
        self.report_failure("plan_conflict", message, False, **fields)

    # ── steps that only read ─────────────────────────────────────────────────
    def check_identity(self) -> bool:
        """Settle a file that changed after the scan or after its plan."""
        logger.debug("Worker got file: %s", self.path)
        self.st = self.path.stat(follow_symlinks=False)
        self.original_size = self.st.st_size
        self.original_allocated = allocated_size(self.st)
        self.identity = stat_identity(self.st)
        self.planned = self.context.plan_records.get(str(self.path))
        if self.identity != stat_identity(self.candidate.initial_stat):
            if self.planned:
                self.plan_conflict("file changed while applying the plan")
            else:
                self.skip("skipped_changed", "changed")
            return True
        if self.planned:
            planned_identity = (
                self.planned["device"], self.planned["inode"],
                self.planned["logical_bytes"], self.planned["mtime_ns"],
            )
            if self.identity != planned_identity:
                self.plan_conflict(
                    "file identity changed after plan creation",
                    planned_identity=planned_identity,
                    current_identity=self.identity,
                )
                return True
        return False

    def check_cache(self) -> bool:
        """Settle a file whose recorded outcome is still valid."""
        context = self.context
        if (not context.audit_compressed and not context.args.ignore_cache
                and context.cache.seen(self.path, self.st, context.policy)):
            self.skip("skipped_cached", "cached")
            return True
        return False

    def classify(self) -> bool:
        """Find the compression state. Settle a file that needs no work."""
        context, args = self.context, self.context.args
        self.was_compressed = already_compressed(self.path)
        recompressing = args.recompress or bool(
            self.planned and self.planned.get("action") == "recompress"
        )
        self.audit_existing = (context.audit_compressed and
                               self.was_compressed and not recompressing)
        if (self.was_compressed and not recompressing and
                not self.audit_existing):
            if not args.dry_run:
                context.cache.mark(self.path, self.st, context.policy,
                                   "already_compressed")
            self.skip("skipped_already", "already_compressed")
            return True
        if self.was_compressed and not self.audit_existing:
            self.old_algorithm = compression_algorithm(self.path)
            if self.old_algorithm is None:
                self.skip("skipped_recompress_unknown",
                          "recompress_codec_unknown")
                return True
        if (TOOL == "ditto" and not self.was_compressed
                and native().sharing(self.path).private == 0):
            self.skip("skipped_shared", "all_blocks_shared")
            return True
        return False

    def check_budget(self) -> bool:
        """Settle a file that arrives after the budget ended."""
        if (self.context.budget.exhausted() and
                not self.context.audit_compressed):
            self.skip_budget()
            return True
        return False

    def select(self) -> bool:
        """Choose the codec and estimate the result. Settles nothing."""
        context, stats = self.context, self.context.stats
        if self.planned:
            self.selection = ProbeResult(
                self.planned["algorithm"], self.planned["sample_ratio"], 0.0,
                0, self.planned["sample_confidence"],
            )
        else:
            self.selection = choose_algorithm(context.args, self.path,
                                              self.st, context.sampler)
        self.estimated_after_size = estimated_allocated_size(
            self.original_size, self.original_allocated, self.selection.ratio
        )
        self.estimated_saving = max(
            0, self.original_allocated - self.estimated_after_size
        )
        with context.lock:
            stats.sampled_bytes += self.selection.sampled_bytes
            stats.sampling_seconds += self.selection.seconds
            stats.algorithms[self.selection.algorithm] += 1
        return False

    def reject(self) -> bool:
        """Settle a file whose predicted gain is too small."""
        context, args, selection = self.context, self.context.args, self.selection
        if (not self.planned and self.was_compressed and
                not self.audit_existing and
                (selection.algorithm == self.old_algorithm or
                 self.estimated_after_size >
                 self.original_allocated * (1 - args.min_codec_gain))):
            if not args.dry_run:
                context.cache.mark(self.path, self.st, context.policy,
                                   "recompress_rejected")
            self.skip(
                "skipped_recompress_gain", "recompress_rejected",
                algorithm=selection.algorithm,
                current_algorithm=self.old_algorithm,
                estimated_saved_bytes=self.estimated_saving,
            )
            return True
        if not self.planned and selection.ratio > args.ratio:
            if not args.dry_run:
                context.cache.mark(self.path, self.st, context.policy,
                                   "ratio_rejected")
            self.skip(
                "skipped_ratio", "ratio_rejected",
                algorithm=selection.algorithm,
                sample_ratio=round(selection.ratio, 6),
                threshold_ratio=args.ratio,
            )
            return True
        return False

    def suggest(self) -> bool:
        """Count the file as a suggestion. Settle an audited compressed file."""
        context, stats, selection = self.context, self.context.stats, self.selection
        with context.lock:
            stats.suggested += 1
            if self.candidate.initially_compressed:
                stats.suggested_already_compressed += 1
            else:
                stats.suggested_not_compressed += 1
            stats.estimated_bytes_saved += self.estimated_saving
            if not self.audit_existing:
                stats.add_coverage(selection.confidence,
                                   self.original_allocated,
                                   self.estimated_saving)
            if self.estimated_saving:
                stats.offer_candidate(context.top_limit, self.sequence, {
                    "path": str(self.path),
                    "algorithm": selection.algorithm,
                    "estimated_saved": self.estimated_saving,
                    "confidence": round(selection.confidence, 3),
                    "value_score": round(
                        self.estimated_saving / max(selection.seconds, 1e-6),
                        1
                    ),
                })
        if self.audit_existing:
            self.finish(
                "suggested_already_compressed",
                algorithm=selection.algorithm,
                sample_ratio=round(selection.ratio, 6),
                threshold_ratio=context.args.ratio,
            )
            return True
        return False

    # ── steps that hold budget or change the file ────────────────────────────
    def reserve(self) -> bool:
        """Reserve the estimated saving. Settle a file the budget refuses."""
        budget = self.context.budget
        if budget.exhausted() or not budget.claim(self.estimated_saving):
            self.skip_budget()
            return True
        self.reserved_saving = self.estimated_saving
        return False

    def plan(self) -> None:
        """Dry run: record what apply() would do."""
        context, stats, selection = self.context, self.context.stats, self.selection
        with context.lock:
            stats.compressed += 1
            stats.bytes_before += self.original_allocated
            stats.bytes_after += self.estimated_after_size
            stats.ratios.append(selection.ratio)
            stats.count_root(self.candidate.root, "files_compressed")
            stats.count_root(self.candidate.root, "space_saved",
                             self.estimated_saving)
        self.release_budget(self.estimated_saving)
        self.finish(
            "planned", action=("recompress" if self.was_compressed
                               else "compress"),
            method=("replace" if TOOL == "ditto" or is_mach_o(self.path)
                    else "in_place"),
            algorithm=selection.algorithm,
            current_algorithm=self.old_algorithm,
            logical_bytes=self.original_size,
            allocated_bytes_before=self.original_allocated,
            estimated_allocated_bytes_after=self.estimated_after_size,
            estimated_saved_bytes=self.estimated_saving,
            sample_ratio=round(selection.ratio, 6),
            sample_confidence=round(selection.confidence, 6),
            device=self.st.st_dev,
            inode=self.st.st_ino,
            mtime_ns=self.st.st_mtime_ns,
        )

    def journaled_recompress(self, old: str, new: str) -> bool:
        """Recompress with a record that lets a later run recover."""
        cache = self.context.cache
        cache.begin_recompression(self.path, old, new)
        try:
            replaced = self.context.batcher.recompress(
                self.path, new, old, self.st.st_dev
            )
        except BaseException:
            # Keep the journal entry if the old codec is not in use.
            if compression_algorithm(self.path) == old:
                cache.finish_recompression(self.path)
            raise
        cache.finish_recompression(self.path)
        return replaced

    def apply(self) -> None:
        """Compress or recompress the file, then record the measured result."""
        context, selection = self.context, self.selection
        current = self.path.stat(follow_symlinks=False)
        if stat_identity(current) != self.identity:
            self.release_budget()
            self.skip("skipped_changed", "changed")
            return
        estimated_write_bytes = self.estimated_after_size
        if self.was_compressed:
            estimated_write_bytes += self.original_size
        if not context.write_limiter.throttle(estimated_write_bytes,
                                              context.budget.remaining()):
            # The reserved write cannot start before the time budget ends.
            self.release_budget()
            self.skip_budget("time_budget")
            return
        if self.old_algorithm is not None:
            replaced = self.journaled_recompress(self.old_algorithm,
                                                 selection.algorithm)
        else:
            replaced = context.batcher.submit(self.path, selection.algorithm,
                                              self.st.st_dev)
        method = "replace" if replaced is True else "in_place"
        final_stat = self.path.stat(follow_symlinks=False)
        actual_new_size = allocated_size(final_stat)

        if (self.old_algorithm is not None and
                actual_new_size >= self.original_allocated):
            self.journaled_recompress(selection.algorithm, self.old_algorithm)
            final_stat = self.path.stat(follow_symlinks=False)
            actual_new_size = allocated_size(final_stat)
            with context.lock:
                context.stats.reverted_recompress += 1

        # The compression flag shows that transparent compression is on.
        # The allocated blocks give the physical saving, independent of
        # the tool.
        is_now_compressed = already_compressed(self.path)
        if is_now_compressed and actual_new_size < self.original_allocated:
            self.record_compressed(final_stat, actual_new_size, method)
        else:
            self.record_no_savings(final_stat, actual_new_size,
                                   is_now_compressed)

    def record_compressed(self, final_stat: os.stat_result,
                          actual_new_size: int, method: str) -> None:
        context, stats = self.context, self.context.stats
        actual_saving = self.original_allocated - actual_new_size
        with context.lock:
            stats.compressed += 1
            if self.was_compressed:
                stats.recompressed += 1
            stats.bytes_before += self.original_allocated
            stats.bytes_after += actual_new_size
            stats.ratios.append(actual_new_size / self.original_allocated)
            stats.count_root(self.candidate.root, "files_compressed")
            stats.count_root(self.candidate.root, "space_saved",
                             actual_saving)
        self.release_budget(actual_saving)
        context.cache.mark(self.path, final_stat, context.policy,
                           "compressed", method)
        self.finish(
            "recompressed" if self.was_compressed else "compressed",
            algorithm=self.selection.algorithm, method=method,
            allocated_bytes_before=self.original_allocated,
            allocated_bytes_after=actual_new_size,
            saved_bytes=actual_saving,
        )

    def record_no_savings(self, final_stat: os.stat_result,
                          actual_new_size: int,
                          is_now_compressed: bool) -> None:
        # A backend that cannot decompress keeps a result with no saving.
        # afsctool reverts such a result, and ditto does not apply it.
        context, stats = self.context, self.context.stats
        outcome = "no_savings"
        if (not self.was_compressed and is_now_compressed and
                TOOL == "afsctool"):
            context.batcher.decompress(self.path, self.st.st_dev)
            final_stat = self.path.stat(follow_symlinks=False)
            actual_new_size = allocated_size(final_stat)
            outcome = "no_savings_reverted"
        with context.lock:
            stats.processed_no_savings += 1
            if outcome == "no_savings_reverted":
                stats.reverted_no_savings += 1
        self.release_budget()
        context.cache.mark(self.path, final_stat, context.policy, outcome)
        self.finish(outcome, algorithm=self.selection.algorithm,
                    allocated_bytes=actual_new_size)

    # ── control ──────────────────────────────────────────────────────────────
    def process(self) -> None:
        for step in (self.check_identity, self.check_cache, self.classify,
                     self.check_budget, self.select, self.reject,
                     self.suggest, self.reserve):
            if step():
                return
        if self.context.args.dry_run:
            self.plan()
        else:
            self.apply()

    def run(self) -> bool:
        """Process the file and report each failure for it.

        Return False if the run stops and the worker must take no more files.
        """
        try:
            self.process()
        except RunStopped:
            self.release_budget()
            return False
        except NoSavingsError:
            self.release_budget()
            with self.context.lock:
                self.context.stats.processed_no_savings += 1
            # The declined in-place attempt possibly changed the file times.
            self.context.cache.mark(
                self.path, self.path.stat(follow_symlinks=False),
                self.context.policy, "no_savings",
            )
            self.finish("no_savings", algorithm=self.selection.algorithm,
                        allocated_bytes=self.original_allocated)
        except BrokenPipeError:
            # The stdout consumer is gone. Stop the run; do not report a
            # failure for each remaining file.
            raise
        except FileInUseError as e:
            self.report_failure(
                "file_in_use",
                "file is open in another process and was left untouched",
                True, in_use=True, open_processes=e.users,
                suggestion="close the listed process and retry",
            )
        except PublicationConflict as e:
            with self.context.lock:
                self.context.stats.conflicts += 1
            self.report_failure(
                "publication_conflict", str(e), False,
                suggestion="inspect the stage with file repair --only interrupted_swap --dry-run",
            )
        except CompressorRefusedError as e:
            self.report_failure(
                "compressor_refused",
                ("staged copy did not pass verification" if TOOL == "ditto"
                 else "compressor refused both in-place and atomic-copy attempts"),
                False, detail=e.failures.get(str(self.path)),
                suggestion=("check that the directory permits temporary files "
                            "and atomic replacement"),
            )
        except subprocess.CalledProcessError as e:
            logger.debug("Compressor failed for %s: exit %s, stdout %s, "
                         "stderr %s", self.path, e.returncode, e.stdout,
                         e.stderr)
            self.report_failure(
                "compressor_failed",
                f"compressor exited with status {e.returncode}", True,
                compressor_exit_code=e.returncode,
            )
        except FileNotFoundError as e:
            self.report_failure("not_found", os_error_text(e), True)
        except PermissionError as e:
            self.report_failure("permission_denied", os_error_text(e), False)
        except Exception as e:
            logger.exception("Unexpected error processing %s", self.path)
            self.report_failure(
                "internal_error", str(e), False,
                exception_type=type(e).__name__,
            )
        return True


def worker(args, q: queue.Queue[QueueItem], sampler: Sampler,
           cache: StateCache, bar, stats: Stats, lock: threading.Lock,
           batcher: BatchingCompressor, policy: str, budget: WorkBudget,
           write_limiter: RateLimiter, reporter: Reporter,
           stop: threading.Event | None = None):
    """Take candidates from q until the end sentinel or the stop event."""
    stop = stop or threading.Event()
    context = WorkerContext(args, sampler, cache, bar, stats, lock, batcher,
                            policy, budget, write_limiter, reporter)
    while True:
        item = get_unless_stopped(q, stop)
        if item is None:
            break
        _, sequence, candidate = item
        if candidate is None:
            # Put the sentinel back for the other workers.
            q.put((float("inf"), sequence, None))
            break
        if not FileTask(context, sequence, candidate).run():
            break

# ──────────────────────────── Main entry ─────────────────────────────────────
def write_plan(destination: Path, policy: str, records: list[dict]) -> dict:
    files = [record for record in records
             if record.get("status") == "planned"]
    plan = {
        "plan_version": 1,
        "created_at": int(time.time()),
        "policy": policy,
        "files": files,
    }
    path = destination.expanduser().resolve()
    atomic_write(path, json.dumps(plan, indent=2, sort_keys=True) + "\n",
                 durable=True)
    return {"path": str(path), "files": len(files),
            "policy": policy}


def load_plan(source: Path) -> dict:
    path = source.expanduser().resolve(strict=True)
    try:
        plan = json.loads(path.read_text())
        if plan.get("plan_version") != 1 or not isinstance(plan.get("files"), list):
            raise ValueError("unsupported plan format")
        required = {
            "path", "algorithm", "sample_ratio", "sample_confidence",
            "device", "inode", "logical_bytes", "mtime_ns", "action",
        }
        records = {}
        for record in plan["files"]:
            if not isinstance(record, dict) or not required <= record.keys():
                raise ValueError("plan contains an incomplete file record")
            if record["path"] in records:
                raise ValueError(f"duplicate plan path: {record['path']}")
            if not Path(record["path"]).is_absolute():
                raise ValueError("plan paths must be absolute")
            if record["algorithm"] not in {"lzvn", "lzfse", "zlib"}:
                raise ValueError("plan contains an unsupported algorithm")
            if record["action"] not in {"compress", "recompress"}:
                raise ValueError("plan contains an unsupported action")
            if not all(isinstance(record[field], int) for field in (
                    "device", "inode", "logical_bytes", "mtime_ns")):
                raise ValueError("plan identity fields must be integers")
            if not all(isinstance(record[field], (int, float)) for field in (
                    "sample_ratio", "sample_confidence")):
                raise ValueError("plan sampling fields must be numeric")
            records[record["path"]] = record
        return {**plan, "path": str(path), "records": records}
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AppError(
            "invalid_plan", f"could not load plan {path}: {exc}",
            EXIT_USAGE, "create a new plan with 'file scan --plan FILE'",
            path=str(path),
        )


def enqueue_plan(paths: list[Path], q: queue.Queue[QueueItem], stats: Stats,
                 lock: threading.Lock, reporter: Reporter,
                 stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    for sequence, path in enumerate(paths):
        try:
            st = path.stat(follow_symlinks=False)
        except (FileNotFoundError, PermissionError) as exc:
            with lock:
                stats.scanned += 1
                stats.errors += 1
                stats.conflicts += 1
                stats.error_messages["plan_conflict"] += 1
            reporter.file(
                path, "error", error="plan_conflict",
                message=os_error_text(exc), retryable=False,
            )
            continue
        is_compressed = already_compressed(path)
        with lock:
            stats.scanned += 1
            stats.queued += 1
            if is_compressed:
                stats.already_compressed += 1
            else:
                stats.not_compressed += 1
        if not put_unless_stopped(
                q, (-candidate_priority(st), sequence,
                    Candidate(path, st, is_compressed)), stop):
            return
    put_unless_stopped(q, (float("inf"), len(paths), None), stop)


def resolve_existing(value) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise AppError("not_found", f"path does not exist: {value}",
                       EXIT_NOT_FOUND, "check the input path",
                       path=str(value))


def read_path_lines(source: str) -> list[str]:
    """Read a path from each line. Ignore blank lines and outer whitespace."""
    if source == "-":
        if sys.stdin.isatty():
            raise AppError(
                "usage_error",
                "--files-from - expects piped input but stdin is a terminal",
                EXIT_USAGE, "pipe newline-delimited paths or pass PATH "
                            "arguments",
            )
        lines = sys.stdin.readlines()
    else:
        lines = resolve_existing(source).read_text().splitlines()
    return [line.strip() for line in lines if line.strip()]


def resolve_input_paths(args: argparse.Namespace) -> list[Path]:
    values = list(args.paths)
    if args.files_from:
        values.extend(read_path_lines(args.files_from))
    elif not values and args.default_current_directory:
        values = ["."]
    if not values:
        raise AppError("empty_input", "no paths were provided", EXIT_USAGE,
                       "provide paths or newline-delimited paths with --files-from -")
    return list(dict.fromkeys(resolve_existing(value) for value in values))


def require_confirmation(args: argparse.Namespace, **details) -> None:
    if not args.dry_run and not args.yes:
        raise AppError(
            "confirmation_required", f"{args.command} requires --yes",
            EXIT_USAGE, "preview with --dry-run or execute with --yes",
            **details,
        )


def codec_benchmark(args: argparse.Namespace, mode: str) -> int:
    report = run_benchmark(Path(args.benchmark_file))
    write_result(document(args.command, **report), mode)
    return EXIT_SUCCESS


def state_inspect(args: argparse.Namespace, mode: str) -> int:
    result = inspect_state(Path(args.state_db))
    write_result(document(args.command, **result), mode)
    return EXIT_SUCCESS


def state_maintain(args: argparse.Namespace, mode: str) -> int:
    """Run 'state recover' or 'state prune'."""
    require_confirmation(args)
    if args.command == "state recover":
        select_backend("afsctool", required=not args.dry_run)
    with state_lock(args):
        cache = StateCache(args.state_db, readonly=args.dry_run)
        try:
            result = (recover_recompressions(cache, args.dry_run,
                                             args.discard_missing)
                      if args.command == "state recover"
                      else cache.prune_missing(dry_run=args.dry_run))
        finally:
            cache.close()
    errors = result.get("errors", 0)
    write_result(document(
        args.command, errors == 0,
        state_db=str(Path(args.state_db).expanduser().resolve()), **result,
    ), mode)
    return EXIT_FAILURE if errors else EXIT_SUCCESS


def state_list(args: argparse.Namespace, mode: str) -> int:
    under = Path(args.under).expanduser().resolve() if args.under else None
    cache = StateCache(args.state_db, readonly=True)
    try:
        entries = cache.entries(args.outcome, under)
    finally:
        cache.close()
    if args.images:
        entries = [entry for entry in entries
                   if is_mach_o(Path(entry["path"]))]
    write_file_collection(document(
        args.command, state_db=str(Path(args.state_db).expanduser().resolve()),
        entries=len(entries), files=entries,
    ), mode)
    return EXIT_SUCCESS


def file_repair(args: argparse.Namespace, mode: str) -> int:
    roots = [resolve_existing(path) for path in args.paths]
    kinds = [args.only] if args.only else list(REPAIR_KINDS)
    # If there is no state database, there is nothing to lock or update.
    recorded = Path(args.state_db).expanduser().exists()
    with (state_lock(args) if recorded else contextlib.nullcontext()):
        cache = StateCache(args.state_db,
                           readonly=args.dry_run or not recorded)
        try:
            result = repair_files(cache, roots, args.dry_run, kinds,
                                  args.all_images)
        finally:
            cache.close()
    write_file_collection(
        document(args.command, result["errors"] == 0, **result), mode
    )
    return EXIT_FAILURE if result["errors"] else EXIT_SUCCESS


def doctor_checks(state_db: Path, roots: list[Path]) -> list[dict]:
    """Run each diagnostic and return one record for each check."""
    program = Path(sys.argv[0]).name
    checks = []

    def report(check: str, status: str, message: str, **fields) -> None:
        checks.append({"check": check, "status": status, "message": message,
                       **fields})

    afsctool = shutil.which("afsctool")
    ditto_without_clone = ditto_clone_flag()
    if afsctool:
        usage = run_command([afsctool], check=False)
        version = re.search(r"afsctool (\d[\d.]*\d)",
                            usage.stdout + usage.stderr)
        report("backend", "ok",
               f"afsctool {version.group(1) if version else ''} at {afsctool}")
    elif ditto_without_clone:
        report("backend", "ok", "ditto is available for built-in LZVN compression",
               suggestion="use --backend ditto or apfs-reclaim.py")
    else:
        report("backend", "problem", "afsctool is not installed",
               suggestion="install afsctool, or pass --backend ditto to "
                          "file compress")
    if ditto_without_clone:
        report("copy_tool", "ok", "ditto can copy without cloning")
    else:
        report("copy_tool", "warning",
               "ditto has no --noclone; copies may stay uncompressed")
    try:
        native().open_file_users(mappings=False)
        report("open_file_tool", "ok", "files in use are detected through "
                                       "libproc")
    except OSError as exc:
        report("open_file_tool", "problem",
               f"files in use cannot be detected: {os_error_text(exc)}")

    state = inspect_state(state_db)
    if not state["exists"]:
        report("state_database", "ok", f"no state database at {state_db}")
    else:
        cache = StateCache(str(state_db), readonly=True)
        current = {"recorded_at", "method"} <= cache.columns
        cache.close()
        report("state_database", "ok",
               f"{state['entries']} entries"
               + ("" if current else "; older schema, upgraded on the next "
                                     "run that writes"),
               entries=state["entries"])
        pending = state["pending_recompressions"]
        if pending:
            report("interrupted_recompressions", "problem",
                   f"{counted(pending, 'recompression')} left interrupted",
                   count=pending,
                   suggestion=f"{program} state recover --yes")
        else:
            report("interrupted_recompressions", "ok", "none pending")
    lock = Path(f"{state_db}.lock")
    holder = None
    if lock.exists():
        with lock.open() as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = handle.read().strip() or "unknown"
    if holder:
        report("state_lock", "warning",
               f"a run is changing the state database (pid {holder})")
    else:
        report("state_lock", "ok", "no run in progress")

    devices = set()
    for root in roots:
        info = inspect_volume(root)
        if info["device"] in devices:
            continue
        devices.add(info["device"])
        name = info["mountpoint"] or info["path"]
        if not info["compression_supported"]:
            report("target_volume", "problem",
                   f"{name} uses {info['filesystem']}, which has no "
                   "transparent compression", path=info["path"])
        elif not info["writable"]:
            report("target_volume", "problem", f"{name} is read-only",
                   path=info["path"])
        else:
            report("target_volume", "ok",
                   f"{name} is writable {info['filesystem']} with "
                   f"{human_bytes(free_bytes(info['path']))} free",
                   path=info["path"])
        snapshots = local_snapshots(info["mountpoint"])
        if snapshots:
            report("local_snapshots", "warning",
                   f"{counted(snapshots, 'local snapshot')} on {name}; space "
                   "freed by compression is released as they expire",
                   count=snapshots)
        else:
            report("local_snapshots", "ok", f"none on {name}")
    if not roots:
        return checks

    found = defaultdict(list)
    for kind, path in find_file_problems(roots):
        found[kind].append(str(path))
    target = " ".join(str(root) for root in roots)
    for kind, status, noun, problem, healthy, suggestion in (
            ("lost_flag", "problem", "compressed file",
             "lost the compressed flag and read as empty",
             "no compressed file has lost its flag",
             f"{program} file repair {target}"),
            ("stale_metadata", "warning", "file",
             ("with content of their own carry leftover compression "
              "metadata; the compressor would empty them, so this tool "
              "skips them"),
             "no file carries leftover compression metadata",
             ("inspect each, then remove the attribute: xattr -d "
              f"{DECMPFS_XATTR} FILE")),
            ("leftover_copy", "warning", "temporary directory",
             "left by an interrupted run",
             "no temporary directories remain",
             "inspect these legacy temporary copies"),
            ("interrupted_swap", "problem", "private staging directory",
             "has a pending compression publication",
             "no compression publication needs recovery",
             f"{program} file repair {target} --only interrupted_swap"),
            ("unreadable", "warning", "directory",
             "could not be read and were not checked",
             "every directory was readable", None)):
        paths = found[kind]
        if paths:
            plural = noun[:-1] + "ies" if noun.endswith("y") else None
            report(kind, status,
                   f"{counted(len(paths), noun, plural)} {problem}",
                   count=len(paths), paths=paths[:10], suggestion=suggestion)
        else:
            report(kind, "ok", healthy)

    cache = StateCache(str(state_db), readonly=True)
    try:
        stale = reseat_images(cache, roots, True, False)["files_reseated"]
    finally:
        cache.close()
    if stale:
        report("stale_images", "warning",
               f"{counted(stale, 'executable')} compressed in place since "
               "the last boot or at an unknown time; any that had been run "
               "may be killed on launch", count=stale,
               suggestion=f"{program} file repair {target}")
    else:
        report("stale_images", "ok",
               "no executable was compressed in place recently")
    return checks


def system_doctor(args: argparse.Namespace, mode: str) -> int:
    roots = [resolve_existing(path) for path in args.paths]
    checks = doctor_checks(Path(args.state_db).expanduser().resolve(), roots)
    problems = sum(check["status"] == "problem" for check in checks)
    warnings = sum(check["status"] == "warning" for check in checks)
    summary = document(args.command, problems == 0, problems=problems,
                       warnings=warnings)
    if mode == "jsonl":
        for check in checks:
            print(json.dumps({"schema_version": SCHEMA_VERSION,
                              "record_type": "check", **check},
                             sort_keys=True))
        write_result(summary, mode)
    else:
        write_result({**summary, "checks": checks}, mode)
    return EXIT_FAILURE if problems else EXIT_SUCCESS


def state_clear(args: argparse.Namespace, mode: str) -> int:
    require_confirmation(
        args, state_db=str(Path(args.state_db).expanduser().resolve())
    )
    with state_lock(args):
        result = clear_state(Path(args.state_db), args.dry_run, args.yes)
    write_result(document(args.command, **result), mode)
    return EXIT_SUCCESS


def file_inspect(args: argparse.Namespace, mode: str) -> int:
    result = inspect_files([resolve_existing(path) for path in args.paths])
    write_file_collection(document(args.command, **result), mode)
    return EXIT_SUCCESS


def file_decompress(args: argparse.Namespace, mode: str) -> int:
    paths = [resolve_existing(path) for path in args.paths]
    require_confirmation(args)
    if not args.dry_run:
        for path in paths:
            require_settled_publication(publication_directory(path))
    validate_target_filesystems(paths)
    select_backend("afsctool", required=not args.dry_run)
    result = decompress_files(paths, args.dry_run, args.yes)
    write_file_collection(
        document(args.command, result["errors"] == 0, **result), mode
    )
    return EXIT_FAILURE if result["errors"] else EXIT_SUCCESS


def volume_inspect(args: argparse.Namespace, mode: str) -> int:
    volumes = [inspect_volume(resolve_existing(path)) for path in args.paths]
    write_result(document(args.command, volumes=volumes), mode)
    return EXIT_SUCCESS


def internal_paths(args: argparse.Namespace) -> set[Path]:
    """Files that the tool itself reads or writes. The scan excludes them."""
    state_path = Path(args.state_db).expanduser().resolve()
    paths = {state_path, Path(f"{state_path}-wal"), Path(f"{state_path}-shm"),
             Path(f"{state_path}.lock")}
    for value in (args.benchmark_file, args.json_log, args.plan,
                  args.from_plan):
        if value:
            paths.add(Path(value).expanduser().resolve())
    return paths


def compression_summary(args: argparse.Namespace, stats: Stats,
                        reporter: Reporter, batcher: BatchingCompressor,
                        budget: WorkBudget, recovery: dict,
                        elapsed: float, volumes: list[dict]) -> dict:
    return document(
        args.command, stats.errors == 0,
        files_scanned=stats.scanned,
        files_queued=stats.queued,
        files_already_compressed=stats.already_compressed,
        files_not_compressed=stats.not_compressed,
        files_suggested=stats.suggested,
        files_suggested_not_compressed=stats.suggested_not_compressed,
        files_suggested_already_compressed=stats.suggested_already_compressed,
        suggestions_complete=bool(
            args.audit_compressed and not stats.errors and
            not stats.skipped_changed and not stats.skipped_budget
        ),
        files_compressed=stats.compressed,
        complete=stats.skipped_budget == 0,
        files_remaining=stats.skipped_budget,
        stopped_by=stats.stopped_by,
        skipped_cached=stats.skipped_cached,
        skipped_ratio=stats.skipped_ratio,
        skipped_already=stats.skipped_already,
        skipped_shared=stats.skipped_shared,
        skipped_too_new=stats.skipped_too_new,
        skipped_too_small=stats.skipped_too_small,
        skipped_excluded_ext=stats.skipped_excluded_ext,
        skipped_excluded=stats.skipped_excluded,
        skipped_not_owned=stats.skipped_not_owned,
        skipped_blocked=stats.skipped_blocked,
        roots=stats.roots,
        skipped_hardlink=stats.skipped_hardlink,
        skipped_sparse=stats.skipped_sparse,
        skipped_changed=stats.skipped_changed,
        skipped_in_use=stats.skipped_in_use,
        skipped_budget=stats.skipped_budget,
        processed_no_savings=stats.processed_no_savings,
        files_recompressed=stats.recompressed,
        skipped_recompress_gain=stats.skipped_recompress_gain,
        skipped_recompress_unknown=stats.skipped_recompress_unknown,
        recompressions_reverted=stats.reverted_recompress,
        no_savings_reverted=stats.reverted_no_savings,
        errors=stats.errors,
        conflicts=stats.conflicts,
        error_messages=dict(stats.error_messages),
        failures=reporter.failures,
        bytes_before=stats.bytes_before,
        bytes_after=stats.bytes_after,
        sampled_bytes=stats.sampled_bytes,
        sampling_seconds=round(stats.sampling_seconds, 4),
        estimated_bytes_saved=stats.estimated_bytes_saved,
        estimate_by_coverage=stats.coverage_report(),
        target_volumes=volumes,
        savings_deferred=bool(
            not args.dry_run and stats.bytes_before > stats.bytes_after and
            any(volume["local_snapshots"] for volume in volumes)
        ),
        algorithms_selected=dict(stats.algorithms),
        compressor_processes=batcher.processes,
        files_sent_to_compressor=batcher.files,
        files_atomic_fallback=batcher.atomic_fallback_files,
        top_candidates=stats.ranked_candidates(),
        median_ratio=round(median(stats.ratios) if stats.ratios else 1.0, 3),
        space_saved=stats.bytes_before - stats.bytes_after,
        budget_saved=budget.saved,
        elapsed_seconds=round(elapsed, 3),
        dry_run=args.dry_run,
        tool=TOOL,
        algo=args.algo,
        benchmark_used=args.benchmark_data is not None,
        recovery=recovery,
    )


def file_compress(args: argparse.Namespace, mode: str) -> int:
    """Run 'file scan' or 'file compress'."""
    plan = load_plan(Path(args.from_plan)) if args.from_plan else None
    args.plan_records = plan["records"] if plan else {}
    roots = ([Path(path) for path in args.plan_records]
             if plan else resolve_input_paths(args))
    if not roots:
        raise AppError(
            "empty_plan", "compression plan contains no files", EXIT_USAGE,
            "create a plan with at least one eligible file",
        )
    if plan and not args.dry_run:
        for path in roots:
            require_settled_publication(publication_directory(path))
    select_backend(args.backend, required=not args.dry_run, selectable=True)
    volumes = [
        {"mountpoint": info["mountpoint"], "device": info["device"],
         "path": info["path"], "free_bytes_before": free_bytes(info["path"])}
        for info in validate_target_filesystems(roots)
    ]
    args.benchmark_data = load_benchmark(Path(args.benchmark_file))
    args.internal_paths = internal_paths(args)
    patterns = list(args.exclude)
    if args.exclude_from:
        patterns += read_path_lines(args.exclude_from)
    args.exclusions = compile_exclusions(patterns)
    with state_lock(args):
        cache = StateCache(args.state_db, readonly=args.dry_run)
        try:
            recovery = recover_recompressions(cache, dry_run=args.dry_run)
            if recovery["errors"] and not args.dry_run:
                raise AppError(
                    "recompression_recovery_failed",
                    f"could not recover {recovery['errors']} interrupted "
                    "operation(s)",
                    EXIT_CONFLICT,
                    "run 'state recover --dry-run' to list the affected paths",
                    failures=recovery["failures"], retryable=True,
                )
            stop = threading.Event()
            sampler = StratifiedSampler(
                args.probe_bytes, RateLimiter(args.max_read_mbps, stop)
            )
            policy = cache_policy(args, sampler)
            q: queue.PriorityQueue[QueueItem] = queue.PriorityQueue(
                maxsize=1000)
            stats = Stats()
            if not plan:
                stats.track_roots(roots)
            lock = threading.Lock()
            batcher = BatchingCompressor(args.batch_files,
                                         VolumeLimiter(args.volume_workers))
            budget = WorkBudget(args.time_budget, args.space_target)
            write_limiter = RateLimiter(args.max_write_mbps, stop)
            reporter = Reporter(mode, retain=args.details or bool(args.plan))
            started = time.monotonic()
            with Progress(stats, args.dry_run, sys.stderr.isatty()) as bar, \
                 cf.ThreadPoolExecutor(max_workers=args.workers + 1) as pool:
                if plan:
                    producer = pool.submit(enqueue_plan, roots, q, stats, lock,
                                           reporter, stop)
                else:
                    producer = pool.submit(scanner, args, roots, q, stats, lock,
                                           reporter, stop)
                producer.add_done_callback(lambda _: bar.scan_finished())
                futures = [producer] + [
                    pool.submit(worker, args, q, sampler, cache, bar, stats, lock,
                                batcher, policy, budget, write_limiter, reporter,
                                stop)
                    for _ in range(args.workers)
                ]
                try:
                    for future in cf.as_completed(futures):
                        future.result()
                except BaseException:
                    # An interrupt or a failed thread stops the run. Files
                    # in progress continue to completion, and no new work
                    # starts.
                    stop.set()
                    raise
        finally:
            cache.close()

    elapsed = time.monotonic() - started
    logger.info("Finished in %.1fs", elapsed)
    for volume in volumes:
        volume["free_bytes_after"] = free_bytes(volume.pop("path"))
        volume["local_snapshots"] = local_snapshots(volume["mountpoint"])
    summary = compression_summary(args, stats, reporter, batcher, budget,
                                  recovery, elapsed, volumes)
    if args.plan:
        summary["plan"] = write_plan(
            Path(args.plan), policy, reporter.records
        )
    if plan:
        summary["applied_plan"] = {
            "path": plan["path"],
            "policy": plan.get("policy"),
            "files": len(plan["records"]),
        }
    if reporter.records:
        summary["file_records"] = reporter.records
    write_result(summary, mode)
    if stats.conflicts:
        return EXIT_CONFLICT
    return EXIT_FAILURE if stats.errors else EXIT_SUCCESS


COMMANDS = {
    "codec benchmark": codec_benchmark,
    "state inspect": state_inspect,
    "state list": state_list,
    "file repair": file_repair,
    "system doctor": system_doctor,
    "state recover": state_maintain,
    "state prune": state_maintain,
    "state clear": state_clear,
    "file inspect": file_inspect,
    "file scan": file_compress,
    "file compress": file_compress,
    "file decompress": file_decompress,
    "volume inspect": volume_inspect,
}


def main() -> int:
    args = parse_cli()
    setup_logging(args.json_log)
    for warning in args.deprecations:
        print(f"Warning: {warning}", file=sys.stderr)
    mode = "plain" if args.field else args.output
    return COMMANDS[args.command](args, mode)


def os_error(exc: OSError, error: str, exit_code: int,
             suggestion: str) -> AppError:
    """Describe an uncaught OSError and do not show its Python repr."""
    if exc.filename is None:
        return AppError(error, os_error_text(exc), exit_code, suggestion)
    return AppError(error, f"{os_error_text(exc)}: {exc.filename}", exit_code,
                    suggestion, path=str(exc.filename))


def run() -> int:
    """Run main() and convert uncaught failures to the CLI error contract."""
    try:
        return main()
    except BrokenPipeError:
        # The stdout consumer stopped (for example `| head`). Send stdout to
        # /dev/null to prevent a second failure in the final flush.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_FAILURE
    except KeyboardInterrupt:
        error = AppError("interrupted", "operation interrupted",
                         EXIT_INTERRUPTED, "retry the idempotent command")
    except AppError as exc:
        error = exc
    except FileNotFoundError as exc:
        error = os_error(exc, "not_found", EXIT_NOT_FOUND,
                         "check the path or required executable")
    except PermissionError as exc:
        error = os_error(exc, "permission_denied", EXIT_PERMISSION,
                         "check file and volume permissions")
    except Exception as exc:  # noqa: BLE001 - the CLI reports internal_error
        error = AppError("internal_error", str(exc), EXIT_FAILURE,
                         "retry with --dry-run and inspect the failing input")
    write_error(error, output_mode_from_argv(sys.argv[1:]))
    return error.exit_code


if __name__ == "__main__":
    sys.exit(run())
