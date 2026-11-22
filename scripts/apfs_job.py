"""Job state for long-running apfs-dev tools: status, cooperative control, checkpoints.

A job owns one state directory:
  lock                flock held for the life of the run; tells live from interrupted
  identity.json       binds the directory to one operation (tool, roots, volume)
  status.json         latest snapshot, replaced atomically about every two seconds
  events.jsonl        append-only history of those snapshots (rotated at 32 MiB)
  control.json        pause/resume/stop requests addressed to one run id
  checkpoints.sqlite3 signature-keyed cache and tool-specific tables

SIGINT and SIGTERM (and SIGHUP unless it is ignored, as under nohup) request a stop
that takes effect at the next Job.checkpoint() call, never inside a critical section.
"""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apfs_common import atomic_json, human_bytes

EVENTS_ROTATE_BYTES = 32 << 20
LOCK_WAIT_SECONDS = 0.5   # outlasts the momentary probe lock of a concurrent --control status


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def default_state(kind, *roots):
    identity = json.dumps([kind, *map(str, roots)]).encode()
    return Path.home() / 'Library/Application Support/apfs-tools' / (kind + '-' + hashlib.sha256(identity).hexdigest()[:20])


def inspect_state(path):
    """Latest status of the job in path, with `active` from the lock, not from the file."""
    try:
        result = json.loads((path / 'status.json').read_text())
        fd = os.open(path / 'lock', os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ValueError(f'No job state in {path}: nothing has run with --apply for these '
                         'arguments (same roots and --state-dir)') from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            result['active'] = False
        except BlockingIOError:
            result['active'] = True
    finally:
        os.close(fd)
    if not result['active'] and result['state'] in ('running', 'paused', 'starting'):
        result['state'] = 'interrupted'
    return result


def control(path, command):
    result = inspect_state(path)
    if command != 'status':
        if not result['active']:
            raise ValueError('Job is not running; rerun the original command to resume from checkpoints')
        atomic_json(path / 'control.json', {'run_id': result['run_id'], 'command': command})
        result['requested'] = command  # A request is not an acknowledgement.
    return result


class Stopped(KeyboardInterrupt):
    pass


class Job:
    def __init__(self, path, identity):
        self.path = path
        self.db = None
        self.signals = {}
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or path.stat().st_uid != os.geteuid():
            raise ValueError('State directory must be owned and not a symlink')
        self.lock = os.open(path / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            self._acquire()
            identity_path = path / 'identity.json'
            if identity_path.exists():
                if json.loads(identity_path.read_text()) != identity:
                    raise ValueError('State directory belongs to another operation or volume')
            else:
                atomic_json(identity_path, identity)
            self.db = sqlite3.connect(path / 'checkpoints.sqlite3')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS cache (kind TEXT, path TEXT, signature TEXT, value TEXT, PRIMARY KEY(kind,path))')
            self.started = time.monotonic()
            self.phase_started = self.started
            self.last = 0
            self.pending = 0
            self.stop_requested = False
            self.next_control_check = 0
            self.snapshot = {'schema_version': 1, 'run_id': uuid.uuid4().hex, 'pid': os.getpid(),
                             'state': 'starting', 'phase': 'starting', 'identity': identity,
                             'files_done': 0, 'files_total': None, 'bytes_done': 0, 'bytes_total': None,
                             'current_path': None, 'cache_hits': 0, 'hash_reads_completed': 0,
                             'io_bytes_hashed': 0, 'cached_hashes_reused': 0, 'phases': {}, 'state_dir': str(path)}
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous = signal.getsignal(sig)
                if sig == signal.SIGHUP and previous != signal.SIG_DFL:
                    continue   # nohup: keep running when the terminal goes away
                self.signals[sig] = previous
                signal.signal(sig, self._signal)
            self.publish(force=True)
        except BaseException:
            self._release()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _acquire(self):
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError(f'Another run is using the state directory {self.path}; '
                                     'check it with --control status') from None
                time.sleep(0.05)

    def _release(self):
        for sig, handler in self.signals.items():
            signal.signal(sig, handler)
        self.signals = {}
        if self.db is not None:
            self.db.close()
            self.db = None
        os.close(self.lock)

    def _signal(self, signum, frame):
        # Finish journal / rename critical sections; stop at next checkpoint.
        self.stop_requested = True

    def get(self, kind, path, signature):
        row = self.db.execute('SELECT signature,value FROM cache WHERE kind=? AND path=?', (kind, str(path))).fetchone()
        if row and row[0] == json.dumps(signature):
            self.snapshot['cache_hits'] += 1
            return json.loads(row[1])

    def put(self, kind, path, signature, value):
        self.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?)',
                        (kind, str(path), json.dumps(signature), json.dumps(value)))
        self.pending += 1
        if self.pending >= 256:
            self.flush()

    def flush(self):
        self.db.commit()
        self.pending = 0

    def publish(self, force=False, **fields):
        now = time.monotonic()
        phase = fields.get('phase', self.snapshot['phase'])
        if phase != self.snapshot['phase']:
            self.phase_started = now
            self.snapshot.update(eta_seconds=None, files_per_second=None, bytes_per_second=None, current_file_bytes=0, activity=None)
        self.snapshot.update(fields)
        self.snapshot['phase_elapsed_seconds'] = round(now - self.phase_started, 3)
        self.snapshot['phases'][phase] = {key: self.snapshot.get(key) for key in
                                        ('files_done', 'files_total', 'bytes_done', 'bytes_total', 'phase_elapsed_seconds')}

        if not force and now - self.last < 2:
            return
        self.flush()
        self.snapshot.update(updated_at=timestamp(), elapsed_seconds=round(now - self.started, 3))
        atomic_json(self.path / 'status.json', self.snapshot)
        self._append_event()
        self.last = now

    def _append_event(self):
        events = self.path / 'events.jsonl'
        try:
            if events.stat().st_size >= EVENTS_ROTATE_BYTES:
                os.replace(events, self.path / 'events.jsonl.1')
        except FileNotFoundError:
            pass
        with events.open('a') as out:
            out.write(json.dumps(dict(self.snapshot, record_type='progress')) + '\n')

    def checkpoint(self):
        """Honor stop and pause requests. Call only where stopping is safe."""
        while True:
            if self.stop_requested:
                raise Stopped('Stop requested; rerun the same command to resume')
            now = time.monotonic()
            if now < self.next_control_check:
                return
            self.next_control_check = now + 0.2
            request = self.path / 'control.json'
            try:
                value = json.loads(request.read_text())
            except (FileNotFoundError, ValueError):   # absent, or being replaced
                value = {}
            command = value.get('command') if value.get('run_id') == self.snapshot['run_id'] else None
            if command == 'stop':
                raise Stopped('Stop requested; rerun the same command to resume')
            if command != 'pause':
                if self.snapshot['state'] != 'running':
                    self.publish(force=True, state='running')
                return
            self.publish(state='paused', eta_seconds=None)
            self.next_control_check = 0
            time.sleep(0.2)

    def finish(self, state, **fields):
        try:
            self.publish(force=True, state=state, **fields)
        except (OSError, sqlite3.Error):
            if state == 'complete':
                raise
            # A full/offline state disk must not mask the original failure.
            self.db.rollback()

    def close(self):
        if self.db is None:
            return
        try:
            try:
                self.flush()
            except sqlite3.Error:
                self.db.rollback()
        finally:
            self._release()


class Progress:
    """Bounded stderr updates, including byte progress within large files.

    With a Job attached, every report also updates the job's status and passes
    through Job.checkpoint(), so reporting points are the tool's safe stop points.
    """
    def __init__(self, quiet, job=None):
        self.job = job
        self.current_path = None
        self.samples = []
        self.quiet = quiet
        self.last = 0.0
        self.started = time.monotonic()
        self.phase = ''
        self.count = self.bytes = self.total_count = self.total_bytes = 0

    def emit(self, message, force=False):
        now = time.monotonic()
        if not self.quiet and (force or now - self.last >= 2):
            print(message, file=sys.stderr, flush=True)
            self.last = now

    def start(self, phase, count, size):
        self.phase = phase
        self.current_path = None
        self.samples = []
        self.count = self.bytes = 0
        self.total_count, self.total_bytes = count, size
        self.started = time.monotonic()
        self.report(force=True)

    def advance_bytes(self, size):
        self.bytes += size
        self.report()

    def checkpoint(self):
        if self.job:
            self.job.checkpoint()

    def report(self, force=False):
        self.checkpoint()
        elapsed = time.monotonic() - self.started
        if self.total_bytes:
            percent = 100 * self.bytes / self.total_bytes
            detail = (f'{human_bytes(self.bytes)}/{human_bytes(self.total_bytes)} '
                      f'({percent:.1f}% of bytes)')
        else:
            percent = 100 * self.count / self.total_count if self.total_count else 100.0
            detail = f'{percent:.1f}% of files'
        now = time.monotonic()
        if not self.samples or now - self.samples[-1][0] >= 1:
            self.samples.append((now, self.count, self.bytes))
            self.samples = [x for x in self.samples if now - x[0] <= 30]
        dt = now - self.samples[0][0]
        fps = (self.count - self.samples[0][1]) / dt if dt else 0
        bps = (self.bytes - self.samples[0][2]) / dt if dt else 0
        estimates = []
        if fps:
            estimates.append(max(0, self.total_count - self.count) / fps)
        if bps:
            estimates.append(max(0, self.total_bytes - self.bytes) / bps)
        eta = max(estimates) if estimates else None
        if self.job:
            self.job.publish(force=force, phase=self.phase, files_done=self.count,
                             files_total=self.total_count, bytes_done=self.bytes,
                             bytes_total=self.total_bytes, current_path=self.current_path,
                             files_per_second=round(fps, 2), bytes_per_second=round(bps, 2),
                             eta_seconds=round(eta) if eta is not None else None)
        suffix = f'; {fps:.0f} files/s; ETA ~{eta / 60:.1f} min' if eta is not None else ''
        if self.job and self.job.snapshot.get('activity'):
            suffix += '; ' + self.job.snapshot['activity']
            suffix += '; file I/O ' + human_bytes(self.job.snapshot.get('current_file_bytes', 0))
        if self.current_path is not None:
            suffix += '; path=' + json.dumps(self.current_path)
        self.emit(f'{self.phase}: {self.count:,}/{self.total_count:,} files; '
                  f'{detail}; {elapsed:.0f}s elapsed{suffix}', force)
