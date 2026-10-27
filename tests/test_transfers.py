import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from recognition_helper.config import Settings
from recognition_helper import transfers as t


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith('TRANSFER_'):
            monkeypatch.delenv(key)
    source = tmp_path / 'released'
    source.mkdir()
    targets = {c: tmp_path / ('library-' + c) for c in t.CATEGORIES}
    for path in targets.values():
        path.mkdir()
    settings = t.TransferSettings(True, False, 1, source, tmp_path / 'state' / 'transfers',
                                  targets, {c: 'synthetic/' + c for c in targets})
    records = []
    worker = t.TransferWorker(settings, lambda: copy.deepcopy(records), lambda p, d: ('synthetic', str(p), d))
    return worker, records, targets


def released(worker, records, relative='movies/Example (2020)/Film.mkv', **fields):
    path = worker.s.source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'synthetic published media\0' * 100)
    record = dict(cycle_id='synthetic-cycle', job_guid='synthetic-guid', source_id=len(records),
                  state='released', path=str(path), size_bytes=path.stat().st_size, released_at=123.5)
    record.update(fields)
    records.append(record)
    return path


def recent(worker):
    return worker.status()['recent'][0]


@pytest.mark.parametrize('category', t.CATEGORIES)
def test_category_nested_unicode_and_source_preserved(setup, category):
    worker, records, targets = setup
    path = released(worker, records, category + '/Bücher/Season 01/Épisode.mkv')
    before = copy.deepcopy(records)
    worker.run_once()
    target = targets[category] / 'Bücher/Season 01/Épisode.mkv'
    assert path.read_bytes() == target.read_bytes()
    assert recent(worker)['status'] == 'success'
    assert recent(worker)['checksum'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert recent(worker)['request']['destination'] == str(target)
    assert not list(target.parent.glob('*.partial'))
    assert records == before


def test_partial_cycles_detained_unknown_and_unreleased_files_excluded(setup):
    w, records, targets = setup
    released(w, records)
    released(w, records, 'movies/Unfinished.mkv', state='complete')
    released(w, records, 'extras/Bonus.mkv')
    outside = w.s.source.parent / 'detention' / 'DETAINED.mkv'
    outside.parent.mkdir(); outside.write_bytes(b'never copy')
    records.append(dict(records[0], path=str(outside), source_id=3))
    w.run_once()
    assert w.status()['counts'] == {'skipped': 2, 'success': 1}
    assert not (targets['movies'] / 'Unfinished.mkv').exists()
    assert outside.read_bytes() == b'never copy'


def test_collision_even_if_contents_identical_then_retry(setup):
    w, records, targets = setup
    path = released(w, records, 'movies/Film.mkv')
    target = targets['movies'] / path.name
    target.write_bytes(path.read_bytes())
    before = target.stat().st_ino
    w.run_once()
    assert recent(w)['status'] == 'conflict'
    assert target.stat().st_ino == before
    target.unlink()
    w.run_once()
    assert recent(w)['status'] == 'success' and recent(w)['attempts'] == 2


def test_duplicate_records_and_restart_are_idempotent(setup):
    w, records, targets = setup
    released(w, records)
    records.append(copy.deepcopy(records[0]))
    w.run_once()
    assert w.status()['counts'] == {'success': 1}
    restarted = t.TransferWorker(w.s, lambda: records, w.mount_check)
    restarted.run_once()
    assert recent(restarted)['attempts'] == 1
    records[0]['size_bytes'] += 1
    restarted.run_once()
    assert recent(restarted)['status'] == 'conflict'


def test_dry_run_writes_only_ledger_and_later_copies(setup):
    w, records, targets = setup
    released(w, records)
    w.s.dry_run = True
    w.run_once()
    assert recent(w)['status'] == 'dry_run' and recent(w)['checksum'] is None
    assert not list(targets['movies'].iterdir())
    w.s.dry_run = False
    w.run_once()
    assert recent(w)['status'] == 'success'


@pytest.mark.parametrize('kind', ['traversal', 'source_link', 'source_parent', 'destination_parent', 'destination_root', 'final_link'])
def test_path_and_symlink_protection(setup, kind, tmp_path):
    w, records, targets = setup
    path = released(w, records, 'movies/nested/Film.mkv')
    outside = tmp_path / 'outside'; outside.mkdir()
    foreign = outside / 'Film.mkv'; foreign.write_bytes(path.read_bytes())
    original = foreign.read_bytes()
    if kind == 'traversal':
        records[0]['path'] = str(w.s.source) + '/movies/../../outside/Film.mkv'
    elif kind == 'source_link':
        path.unlink(); path.symlink_to(foreign)
    elif kind == 'source_parent':
        path.unlink(); path.parent.rmdir(); path.parent.symlink_to(outside)
    elif kind == 'destination_parent':
        (targets['movies'] / 'nested').symlink_to(outside)
    elif kind == 'destination_root':
        targets['movies'].rmdir(); targets['movies'].symlink_to(outside)
    else:
        (targets['movies'] / 'nested').mkdir()
        (targets['movies'] / 'nested' / 'Film.mkv').symlink_to(foreign)
    w.run_once()
    assert recent(w)['status'] in ('failed', 'skipped', 'conflict')
    assert foreign.read_bytes() == original
    assert not list(outside.glob('*.partial'))


@pytest.mark.parametrize('error', [PermissionError(errno.EACCES, 'synthetic denied'), OSError(errno.ENOSPC, 'synthetic full')])
def test_write_failures_never_publish_delete_or_change_modes(setup, monkeypatch, error):
    w, records, targets = setup
    path = released(w, records, 'movies/Film.mkv')
    before = path.stat().st_mode
    with monkeypatch.context() as m:
        m.setattr(t.os, 'write', lambda *a: (_ for _ in ()).throw(error))
        w.run_once()
    assert recent(w)['status'] == 'failed'
    assert type(error).__name__ in recent(w)['error']
    assert path.exists() and path.stat().st_mode == before
    assert not list(targets['movies'].iterdir())
    w.run_once()
    assert recent(w)['status'] == 'success'


def test_checksum_failure_then_retry(setup, monkeypatch):
    w, records, targets = setup
    path = released(w, records, 'music/Artist/Album/01.flac')
    with monkeypatch.context() as m:
        m.setattr(t, 'digest', lambda *a: 'wrong checksum')
        w.run_once()
    assert recent(w)['status'] == 'failed' and 'SHA-256' in recent(w)['error']
    assert path.exists() and not list(targets['music'].rglob('*.flac'))
    w.run_once()
    assert recent(w)['status'] == 'success'


def test_missing_mount_and_mount_change_are_fail_closed(setup):
    w, records, targets = setup
    released(w, records, 'tv/Show/S01E01.mkv')
    w.mount_check = lambda *a: (_ for _ in ()).throw(ValueError('Dataset not mounted'))
    w.run_once()
    assert recent(w)['status'] == 'failed' and not list(targets['tv'].iterdir())
    calls = iter([('mount', 1), ('mount', 2)])
    w.mount_check = lambda *a: next(calls)
    w.run_once()
    assert recent(w)['status'] == 'failed' and not list(targets['tv'].rglob('*.mkv'))


def test_native_mount_guard_rejects_plain_directory(tmp_path):
    with pytest.raises(ValueError, match='not mounted'):
        t.dataset_mount(tmp_path, 'synthetic/dataset')


def test_interrupted_copy_retries_after_restart(setup, monkeypatch):
    w, records, targets = setup
    path = released(w, records, 'movies/Film.mkv')
    with monkeypatch.context() as m:
        m.setattr(t.os, 'write', lambda *a: (_ for _ in ()).throw(InterruptedError('synthetic interruption')))
        w.run_once()
    restarted = t.TransferWorker(w.s, lambda: records, w.mount_check)
    restarted.run_once()
    assert recent(restarted)['status'] == 'success' and path.exists()


def test_restart_reconciles_publish_before_ledger_commit(setup, monkeypatch):
    w, records, targets = setup
    path = released(w, records, 'movies/Film.mkv')
    update = w.update
    def fail_success(db, key, status, error=None, **kw):
        if status == 'success':
            raise sqlite3.OperationalError('synthetic commit interruption')
        return update(db, key, status, error, **kw)
    with monkeypatch.context() as m:
        m.setattr(w, 'update', fail_success)
        w.run_once()
    target = targets['movies'] / 'Film.mkv'
    assert target.read_bytes() == path.read_bytes() and recent(w)['status'] == 'failed'
    ino = target.stat().st_ino
    restarted = t.TransferWorker(w.s, lambda: records, w.mount_check)
    restarted.run_once()
    assert recent(restarted)['status'] == 'success' and target.stat().st_ino == ino
    assert not list(targets['movies'].glob('*.partial'))


def test_changed_source_and_reused_job_id_do_not_alias(setup):
    w, records, targets = setup
    path = released(w, records, 'movies/Film.mkv')
    path.write_bytes(b'changed size')
    w.run_once()
    assert 'size differs' in recent(w)['error']
    records[0]['size_bytes'] = path.stat().st_size
    records[0]['cycle_id'] = 'different-cycle'
    records[0]['job_guid'] = 'different-guid'
    w.run_once()
    assert w.status()['counts'] == {'failed': 1, 'success': 1}


def test_process_lock_prevents_duplicate_workers(setup):
    import fcntl
    w, records, targets = setup
    released(w, records)
    w.s.state.mkdir(parents=True)
    with (w.s.state / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        w.run_once()
        assert not list(targets['movies'].iterdir())
    w.run_once()
    assert recent(w)['status'] == 'success'


def test_existing_environment_configuration_and_disabled_defaults(tmp_path, monkeypatch):
    s = Settings(state=tmp_path/'state', media=tmp_path/'media', released=tmp_path/'released')
    assert not s.transfer.enabled and s.transfer.dry_run
    monkeypatch.setenv('TRANSFER_ENABLED', 'true')
    monkeypatch.setenv('TRANSFER_MUSIC_DIR', str(tmp_path/'music-archive'))
    monkeypatch.setenv('TRANSFER_MUSIC_DATASET', 'synthetic/music-archive')
    s = Settings(state=tmp_path/'state', media=tmp_path/'media', released=tmp_path/'released')
    assert s.transfer.destinations['music'] == tmp_path/'music-archive'
    assert s.transfer.datasets['music'] == 'synthetic/music-archive'
    monkeypatch.setenv('TRANSFER_DELETE_SOURCE', 'true')
    with pytest.raises(ValueError, match='not supported'):
        Settings()


@pytest.mark.parametrize('key,value', [('TRANSFER_ENABLED','yes'), ('TRANSFER_VERIFY','size'),
    ('TRANSFER_INTERVAL_SECONDS','0'), ('TRANSFER_SOURCE_ROOT','/media/detention'),
    ('TRANSFER_STATE_DIR','/other-ledger'), ('TRANSFER_TV_DIR','/media/released/other')])
def test_configuration_fail_closed(monkeypatch, key, value):
    monkeypatch.setenv(key,value)
    with pytest.raises(ValueError):
        Settings()


def test_racing_destination_does_not_replace_existing_file(setup, monkeypatch):
    w, records, targets = setup
    path = released(w, records, 'movies/Film.mkv')
    link = t.os.link
    def race(src, dst, **kw):
        (targets['movies'] / 'Film.mkv').write_bytes(b'other publisher won')
        return link(src, dst, **kw)
    monkeypatch.setattr(t.os, 'link', race)
    w.run_once()
    assert recent(w)['status'] == 'conflict'
    assert (targets['movies'] / path.name).read_bytes() == b'other publisher won'
    assert path.exists() and not list(targets['movies'].glob('*.partial'))


def test_source_changes_during_verification_are_rejected(setup, monkeypatch):
    w, records, targets = setup
    path = released(w, records, 'movies/Film.mkv')
    digest = t.digest
    def changed(fd, *args):
        path.write_bytes(b'x' * path.stat().st_size)
        return digest(fd, *args)
    monkeypatch.setattr(t, 'digest', changed)
    w.run_once()
    assert recent(w)['status'] == 'failed'
    assert path.exists() and not list(targets['movies'].iterdir())


def test_mount_guard_matches_dataset_and_device_not_parent(tmp_path, monkeypatch):
    dev = tmp_path.stat().st_dev
    number = f'{os.major(dev)}:{os.minor(dev)}'
    content = f'88 77 {number} /library {tmp_path} rw - zfs synthetic/media rw\n'
    original = Path.read_text
    def mountinfo(path, *args, **kw):
        return content if str(path) == '/proc/self/mountinfo' else original(path, *args, **kw)
    monkeypatch.setattr(Path, 'read_text', mountinfo)
    assert t.dataset_mount(tmp_path, 'synthetic/media')[0] == '88'
    with pytest.raises(ValueError, match='not the expected'):
        t.dataset_mount(tmp_path, 'synthetic/different-child')
    content = f'88 77 {number} /library {tmp_path.parent} rw - zfs synthetic/media rw\n'
    with pytest.raises(ValueError, match='not mounted'):
        t.dataset_mount(tmp_path, 'synthetic/media')


def test_existing_compose_template_forwards_runtime_env_and_safe_mounts():
    import yaml
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root/'compose.yaml').read_text())['services']['helper']
    keys = {line.split('=', 1)[0] for line in (root/'.env.example').read_text().splitlines()
            if line.startswith('TRANSFER_')}
    compose_only = {'TRANSFER_GID', 'TRANSFER_DESTINATIONS_READ_ONLY'}
    assert keys - compose_only <= set(compose['environment'])
    assert 'env_file' not in compose  # existing .env interpolation, explicit runtime forwarding
    mounts = [m for m in compose['volumes'] if isinstance(m, dict)]
    assert len(mounts) == 4
    assert all(m['bind']['create_host_path'] is False for m in mounts)
    assert all('/dev/null' in m['source'] for m in mounts)
    assert all(m['read_only'] == '${TRANSFER_DESTINATIONS_READ_ONLY:-true}' for m in mounts)


def test_engine_projection_is_release_only_without_record_mutation(tmp_path):
    from recognition_helper.engine import Engine
    e = Engine(Settings(state=tmp_path/'state', media=tmp_path/'media', released=tmp_path/'released'))
    try:
        c = e.store.create(state='partially_released')
        c['backend_done'] = True
        c['job_guid'] = 'synthetic-guid'
        c['sources'] = [dict(source_id=0, state='released', path=str(e.s.released/'movies/Film.mkv'), released_at=2),
                        dict(source_id=1, state='complete', path=str(e.s.detention/'DETAINED.mkv'))]
        before = copy.deepcopy(c)
        projection = e.transfer_records()
        assert len(projection) == 1 and projection[0]['state'] == 'released'
        assert projection[0]['cycle_id'] == c['id'] and projection[0]['job_guid'] == 'synthetic-guid'
        assert c == before and not e.transfers.status()['enabled']
    finally:
        e.pool.shutdown(wait=True)


def test_music_dataset_reconfiguration_has_distinct_identity(setup, tmp_path):
    w, records, targets = setup
    path = released(w, records, 'music/Artist/Album/01.flac')
    w.run_once()
    targets['music'].rename(tmp_path/'previous-music-library')
    targets['music'].mkdir()
    w.s.datasets['music'] = 'synthetic/music-archive'
    w.run_once()
    assert w.status()['counts'] == {'success': 2}
    assert (targets['music']/'Artist/Album/01.flac').read_bytes() == path.read_bytes()


def test_lifecycle_disabled_no_thread_and_enabled_worker_stops(setup):
    import threading
    w, records, targets = setup
    w.s.enabled = False
    w.start()
    assert w.thread is None
    scanned = threading.Event()
    def records():
        scanned.set()
        return []
    w.records = records
    w.s.enabled = True
    w.start()
    try:
        assert scanned.wait(2)
    finally:
        w.close()
    assert not w.thread.is_alive()


def test_destination_parent_moved_during_copy_is_not_published(setup, tmp_path, monkeypatch):
    w, records, targets = setup
    released(w, records, 'movies/nested/Film.mkv')
    original = t.digest
    moved = tmp_path/'moved-directory'
    def digest(fd, *args):
        if not moved.exists():
            (targets['movies']/'nested').rename(moved)
        return original(fd, *args)
    monkeypatch.setattr(t, 'digest', digest)
    w.run_once()
    assert recent(w)['status'] == 'failed'
    assert not (moved/'Film.mkv').exists()
