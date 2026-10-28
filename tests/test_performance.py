"""Synthetic performance invariants; never accesses live media, ARM or models."""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from recognition_helper.config import Settings
from recognition_helper.engine import Engine
from recognition_helper.store import Store


def test_noop_save_preserves_record_bytes_revision_and_mtimes(tmp_path):
    store = Store(tmp_path)
    cycle = store.create(state='completed_detained')
    paths = [tmp_path / cycle['id'] / name for name in ('record.json', 'record.md')]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    updated = cycle['updated']
    with patch('recognition_helper.store.os.fsync') as fsync:
        for _ in range(20):
            assert store.save(cycle) is False
        fsync.assert_not_called()
    assert cycle['updated'] == updated
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    reloaded = Store(tmp_path)
    assert reloaded.save(reloaded.cycles[cycle['id']]) is False
    cycle['sources'].append({'source_id': 0, 'state': 'failed', 'error': 'new error'})
    assert store.save(cycle) is True
    assert json.loads(paths[0].read_text())['sources'][0]['error'] == 'new error'
    store.event(cycle, 'retry requested')
    assert json.loads(paths[0].read_text())['history'][-1]['action'] == 'retry requested'


def test_idle_completed_release_and_no_output_decision_are_nonwriting(tmp_path):
    engine = Engine(Settings(state=tmp_path / 'state', media=tmp_path / 'media'))
    try:
        c = engine.store.create(state='completed_detained', local_done=True)
        c.update(backend_done=True, recognition_done=True)
        c['sources'] = [{'source_id': 0, 'state': 'complete', 'path': 'DETAINED.mkv', 'reason': 'unidentified'}]
        engine.store.save(c)
        updated = c['updated']
        with patch('recognition_helper.store.os.fsync') as fsync:
            for _ in range(10):
                engine.decide(c)
                engine.release(c)
            fsync.assert_not_called()
        assert c['updated'] == updated
        c['sources'] = []
        engine.store.save(c)
        with patch('recognition_helper.engine.merge') as merge:
            engine.decide(c)
            merge.assert_not_called()
    finally:
        engine.pool.shutdown(wait=True)


def test_summary_detail_isolation_and_authenticated_compatibility(tmp_path, monkeypatch):
    from recognition_helper import main
    settings = Settings(state=tmp_path / 'state', media=tmp_path / 'media')
    engine = Engine(settings)
    try:
        c = engine.store.create(state='completed_detained')
        c.update(locals=[{'raw': 'private-evidence' * 10000}], history=[{'at': 1, 'action': 'large'}])
        c['sources'] = [{'source_id': 7, 'state': 'complete', 'path': '/media/detention/DETAINED.mkv',
                         'error': 'retained reason', 'backend_metadata': {'huge_probe': 'x' * 100000,
                         'scan_duration': 60, 'duration_validation': {'actual_seconds': 61.5}}}]
        engine.store.save(c)
        monkeypatch.setattr(main, 'engine', engine)
        monkeypatch.setattr(main, 'settings', settings)
        client = TestClient(main.app)
        for path in ('/api/status', '/api/cycles/' + c['id']):
            assert client.get(path).status_code == 401
        main.app.dependency_overrides[main.authenticate] = lambda: None
        try:
            summary = client.get('/api/status')
            assert summary.status_code == 200
            row = summary.json()['cycles'][0]
            assert 'locals' not in row and 'history' not in row
            assert 'huge_probe' not in row['sources'][0]['backend_metadata']
            assert row['sources'][0]['reason'] == 'retained reason'
            assert row['sources'][0]['backend_metadata']['duration_validation']['actual_seconds'] == 61.5
            detail = client.get('/api/cycles/' + c['id']).json()
            assert detail['locals'] == c['locals']
            assert detail['sources'][0]['backend_metadata']['huge_probe'] == 'x' * 100000
            assert client.get('/api/state').json()['cycles'][0] == detail
            assert client.get('/api/cycles/missing').status_code == 404
            assert len(summary.content) < 5000
            row['sources'][0]['backend_metadata']['duration_validation']['actual_seconds'] = 0
            assert c['sources'][0]['backend_metadata']['duration_validation']['actual_seconds'] == 61.5
        finally:
            main.app.dependency_overrides.clear()
    finally:
        engine.pool.shutdown(wait=True)
