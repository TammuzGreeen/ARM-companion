"""Experimental, opt-in local copy of immutable authoritative releases.

No recognition records are edited. Directory descriptors and O_NOFOLLOW keep path
resolution beneath pinned roots. A separate SQLite ledger survives restarts; a
process lock prevents two workers sharing that ledger from publishing concurrently.
"""
import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid

log = logging.getLogger('uvicorn.error')
CATEGORIES = ('movies', 'tv', 'music', 'audiobooks')
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def absolute(value):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts or '\x00' in str(p):
        raise ValueError('Transfer paths must be absolute without traversal')
    return p


def boolean(name, default):
    value = os.getenv(name, str(default)).lower()
    if value not in ('true', 'false'):
        raise ValueError(name + ' must be true or false')
    return value == 'true'


@dataclass
class TransferSettings:
    enabled: bool
    dry_run: bool
    interval: int
    source: Path
    state: Path
    destinations: dict
    datasets: dict

    @classmethod
    def from_env(cls, state, released, detention):
        enabled = boolean('TRANSFER_ENABLED', False)
        dry = boolean('TRANSFER_DRY_RUN', True)
        if boolean('TRANSFER_DELETE_SOURCE', False):
            raise ValueError('TRANSFER_DELETE_SOURCE is not supported: release history requires source files')
        if os.getenv('TRANSFER_VERIFY', 'sha256') != 'sha256':
            raise ValueError('TRANSFER_VERIFY must be sha256')
        interval = int(os.getenv('TRANSFER_INTERVAL_SECONDS', '60'))
        if not 1 <= interval <= 86400:
            raise ValueError('TRANSFER_INTERVAL_SECONDS must be between 1 and 86400')
        source = absolute(os.getenv('TRANSFER_SOURCE_ROOT', str(released)))
        ledger = absolute(os.getenv('TRANSFER_STATE_DIR', str(state / 'transfers')))
        if source != released or source.is_relative_to(detention) or detention.is_relative_to(source):
            raise ValueError('TRANSFER_SOURCE_ROOT must equal RELEASED_PATH and exclude detention')
        if not ledger.is_relative_to(state) or ledger == state:
            raise ValueError('TRANSFER_STATE_DIR must be a child of STATE_PATH')
        destinations, datasets = {}, {}
        for category in CATEGORIES:
            prefix = 'TRANSFER_' + category.upper()
            value = os.getenv(prefix + '_DIR', '')
            dataset = os.getenv(prefix + '_DATASET', '')
            if value:
                destinations[category] = absolute(value)
                datasets[category] = dataset
                if enabled and not dataset:
                    raise ValueError(prefix + '_DATASET is required for an enabled destination')
            elif enabled and dataset:
                raise ValueError(prefix + '_DIR is required with a dataset')
        if enabled and not destinations:
            raise ValueError('At least one transfer destination is required')
        roots = [source, detention, state, *destinations.values()]
        for i, a in enumerate(roots):
            for b in roots[i + 1:]:
                if a.is_relative_to(b) or b.is_relative_to(a):
                    raise ValueError('Transfer source, detention, state and destinations must not overlap')
        return cls(enabled, dry, interval, source, ledger, destinations, datasets)


@contextlib.contextmanager
def directory(path):
    """Open every component without following symlinks, including root parents."""
    path = absolute(path)
    fd = os.open('/', DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            new = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def subdirectory(root, parts, create=False):
    fd = os.dup(root)
    try:
        for part in parts:
            if part in ('', '.', '..') or '/' in part:
                raise ValueError('Unsafe transfer path component')
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            new = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd
    finally:
        os.close(fd)


def fingerprint(s):
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def digest(fd, stop=None):
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while True:
        if stop is not None and stop.is_set():
            raise InterruptedError('Transfer shutdown requested')
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return h.hexdigest()
        h.update(chunk)


def dataset_mount(path, expected):
    """Require an exact bind mount backed by the intended ZFS dataset, not its parent."""
    def unescape(s):
        for a, b in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
            s = s.replace(a, b)
        return s
    device = os.stat(path, follow_symlinks=False).st_dev
    device = f'{os.major(device)}:{os.minor(device)}'
    for line in reversed(Path('/proc/self/mountinfo').read_text().splitlines()):
        before, after = line.split(' - ', 1)
        fields, fs = before.split(), after.split()
        if unescape(fields[4]) == str(path) and fields[2] == device:
            if fs[0] == 'zfs' and unescape(fs[1]) == expected:
                return (fields[0], fields[2], fields[3], fs[1])
            raise ValueError('Destination mount is not the expected ZFS dataset: ' + str(path))
    raise ValueError('Destination dataset is not mounted at ' + str(path))


class TransferWorker:
    def __init__(self, settings, records, mount_check=dataset_mount):
        self.s, self.records, self.mount_check = settings, records, mount_check
        self.stop = threading.Event()
        self.guard = threading.Lock()
        self.thread = None
        self.last_error = None

    def start(self):
        if self.s.enabled:
            self.thread = threading.Thread(target=self.run, daemon=True, name='media-transfer')
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def connect(self):
        self.s.state.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.s.state / 'ledger.sqlite3', timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('''CREATE TABLE IF NOT EXISTS transfers (
            id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
            error TEXT, proof TEXT, temporary TEXT, inode TEXT, checksum TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS events (
            id TEXT, at REAL, status TEXT, detail TEXT)''')
        db.commit()
        return db

    def update(self, db, key, status, error=None, **fields):
        # Column names are internal constants, never supplied through an API.
        fields.update(status=status, error=error, updated=time.time())
        db.execute('UPDATE transfers SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                   [*fields.values(), key])
        db.execute('INSERT INTO events VALUES (?,?,?,?)', (key, time.time(), status, error))
        db.commit()
        log.info('Media transfer %s: %s%s', key, status, ': ' + error if error else '')

    def status(self):
        result = {'enabled': self.s.enabled, 'dry_run': self.s.dry_run,
                  'delete_source': False, 'error': self.last_error, 'counts': {}, 'recent': []}
        path = self.s.state / 'ledger.sqlite3'
        if not path.exists():
            return result
        try:
            with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                db.row_factory = sqlite3.Row
                result['counts'] = dict(db.execute('SELECT status, count(*) FROM transfers GROUP BY status'))
                for row in db.execute('SELECT * FROM transfers ORDER BY updated DESC LIMIT 100'):
                    item = dict(row)
                    item['request'] = json.loads(item['request'])
                    result['recent'].append(item)
        except sqlite3.Error as error:
            result['error'] = str(error)
        return result

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
                self.last_error = None
            except Exception as error:
                self.last_error = str(error)
                log.exception('Media transfer scan failed; release decisions unchanged')
            self.stop.wait(self.s.interval)

    def run_once(self):
        if not self.s.enabled or not self.guard.acquire(blocking=False):
            return
        try:
            self.s.state.mkdir(parents=True, exist_ok=True)
            with (self.s.state / 'worker.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                with contextlib.closing(self.connect()) as db:
                    # Read only a small immutable projection, not captures/model history.
                    for request in self.records():
                        if self.stop.is_set():
                            break
                        self.consider(db, request)
        finally:
            self.guard.release()

    def consider(self, db, request):
        if request.get('state') != 'released':
            return
        dataset = None
        try:
            path = absolute(request['path'])
            relative = path.relative_to(self.s.source)
            if len(relative.parts) < 2:
                raise ValueError('Released file has no category-relative filename')
            category = relative.parts[0]
            destination = self.s.destinations.get(category)
            if destination is None:
                raise ValueError('Unconfigured media category: ' + category)
            target = destination.joinpath(*relative.parts[1:])
            dataset = self.s.datasets[category]
            error = None
        except (ValueError, KeyError) as issue:
            error, target = str(issue), None
        key = hashlib.sha256(json.dumps([request.get('cycle_id'), request.get('job_guid'),
                              request.get('source_id'), str(target) if target else request.get('path'), dataset],
                             ensure_ascii=False).encode()).hexdigest()
        request = dict(request, destination=str(target) if target else None, destination_dataset=dataset)
        payload = json.dumps(request, sort_keys=True, ensure_ascii=False)
        db.execute('INSERT OR IGNORE INTO transfers(id,request,status,updated) VALUES (?,?,?,?)',
                   (key, payload, 'pending', time.time()))
        db.commit()
        row = dict(db.execute('SELECT * FROM transfers WHERE id=?', (key,)).fetchone())
        if row['request'] != payload:
            self.update(db, key, 'conflict', 'Authoritative release changed for an existing transfer identity')
            return
        if row['status'] == 'success':
            return
        if error:
            if row['status'] != 'skipped' or row['error'] != error:
                self.update(db, key, 'skipped', error)
            return
        try:
            if (not request.get('cycle_id') or not request.get('job_guid')
                    or request.get('source_id') is None or request.get('released_at') is None):
                raise ValueError('Release provenance is incomplete; cannot transfer safely')
            if not self.s.dry_run:
                self.update(db, key, 'active', attempts=row['attempts'] + 1)
            self.transfer(db, key, row, path, relative, destination, category)
        except FileExistsError as issue:
            self.update(db, key, 'conflict', 'Destination collision; never overwritten: ' + str(issue))
        except Exception as issue:
            self.update(db, key, 'failed', type(issue).__name__ + ': ' + str(issue))

    def transfer(self, db, key, row, path, relative, destination, category):
        mount = self.mount_check(destination, self.s.datasets[category])
        with directory(self.s.source) as source_root, directory(destination) as target_root:
            with subdirectory(source_root, relative.parts[:-1]) as source_parent:
                fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError('Only regular released files may be transferred')
                    expected_size = json.loads(row['request']).get('size_bytes')
                    if expected_size is not None and before.st_size != expected_size:
                        raise ValueError('Released file size differs from authoritative record')
                    if self.s.dry_run:
                        # No mkdir, temporary copy or destination write in dry-run mode.
                        try:
                            with subdirectory(target_root, relative.parts[1:-1]) as parent:
                                try:
                                    os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                                except FileNotFoundError:
                                    pass
                                else:
                                    raise FileExistsError(relative.name)
                        except FileNotFoundError:
                            pass
                        self.update(db, key, 'dry_run')
                        return
                    with subdirectory(target_root, relative.parts[1:-1], create=True) as parent:
                        self.copy_publish(db, key, row, fd, before, parent, relative.name,
                                          destination, target_root, mount, path, self.s.datasets[category])
                finally:
                    os.close(fd)

    def copy_publish(self, db, key, row, source, before, parent, name, destination, root, mount, path, dataset):
        def check_mount():
            if self.mount_check(destination, dataset) != mount:
                raise ValueError('Destination mount changed during transfer')
            with directory(destination) as current:
                if fingerprint(os.fstat(current))[:2] != fingerprint(os.fstat(root))[:2]:
                    raise ValueError('Destination root changed during transfer')
            parts = Path(json.loads(row['request'])['destination']).relative_to(destination).parts[:-1]
            with subdirectory(root, parts) as current:
                if fingerprint(os.fstat(current))[:2] != fingerprint(os.fstat(parent))[:2]:
                    raise ValueError('Destination parent changed during transfer')

        def check_source():
            with directory(path.parent) as current:
                if fingerprint(os.stat(path.name, dir_fd=current, follow_symlinks=False)) != fingerprint(before):
                    raise ValueError('Released source path changed during transfer')

        def owned(temporary, inode):
            try:
                s = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                return stat.S_ISREG(s.st_mode) and [s.st_dev, s.st_ino] == inode
            except FileNotFoundError:
                return False

        # Recovery of the link-before-ledger-commit window requires our retained
        # staging inode and a durable verified checksum, not just matching contents.
        if row['temporary'] and row['inode']:
            old_inode = json.loads(row['inode'])
            temporary = row['temporary']
            if Path(temporary).name != temporary:
                raise ValueError('Unsafe staging name in transfer ledger')
            if owned(temporary, old_inode):
                if row['checksum'] and owned(name, old_inode):
                    out = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        if (digest(out, self.stop) != row['checksum'] or digest(source, self.stop) != row['checksum']
                                or json.loads(row['proof']) != fingerprint(os.fstat(source))):
                            raise ValueError('Interrupted publication verification failed')
                        check_mount()
                        check_source()
                        os.fsync(parent)
                        self.update(db, key, 'success')
                    finally:
                        os.close(out)
                    if owned(temporary, old_inode):
                        os.unlink(temporary, dir_fd=parent)
                    return
                os.unlink(temporary, dir_fd=parent)
        # Even a byte-identical unmanaged existing file is a collision.
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(name)
        temporary = '.recognition-transfer-' + uuid.uuid4().hex + '.partial'
        self.update(db, key, 'active', temporary=temporary,
                    inode=None, checksum=None, proof=json.dumps(fingerprint(before)))
        out = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o666, dir_fd=parent)
        inode = fingerprint(os.fstat(out))[:2]
        linked = False
        try:
            self.update(db, key, 'active', inode=json.dumps(inode))
            os.lseek(source, 0, os.SEEK_SET)
            h = hashlib.sha256()
            while True:
                if self.stop.is_set():
                    raise InterruptedError('Transfer shutdown requested')
                chunk = os.read(source, 1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(out, view)
                    if written <= 0:
                        raise OSError('Destination write made no progress')
                    view = view[written:]
            os.fsync(out)
            checksum = h.hexdigest()
            if (digest(out, self.stop) != checksum or digest(source, self.stop) != checksum
                    or fingerprint(os.fstat(source)) != fingerprint(before)):
                raise ValueError('SHA-256 verification failed or source changed')
            self.update(db, key, 'active', checksum=checksum)
            check_mount()
            check_source()
            if not owned(temporary, inode):
                raise ValueError('Verified staging path changed during transfer')
            # linkat follows only this kernel-controlled descriptor, not a mutable
            # staging pathname. The target inode is exactly the verified open file.
            # dst_dir_fd forces linkat semantics; the final name is never replaced.
            os.link('/proc/self/fd/' + str(out), name, dst_dir_fd=parent, follow_symlinks=True)
            linked = True
            os.fsync(parent)
            self.update(db, key, 'success')
            if owned(temporary, inode):
                os.unlink(temporary, dir_fd=parent)
        finally:
            os.close(out)
            # Retain proof after uncertain final publication so restart can reconcile.
            if not linked and owned(temporary, inode):
                os.unlink(temporary, dir_fd=parent)
