from types import SimpleNamespace

from fastapi.testclient import TestClient

from recognition_helper.transfers import TransferSettings, TransferWorker


def test_transfer_status_uses_existing_authentication(tmp_path, monkeypatch):
    # No lifespan context: no ARM/camera/model workers are started by this test.
    from recognition_helper import main
    password = tmp_path / 'synthetic-password'
    password.write_text('synthetic-transfer-test-only')
    worker = TransferWorker(TransferSettings(False, True, 60, tmp_path/'released',
                            tmp_path/'state'/'transfers', {}, {}), lambda: [])
    monkeypatch.setattr(main, 'settings', SimpleNamespace(user='operator', password_file=str(password)))
    monkeypatch.setattr(main, 'engine', SimpleNamespace(transfers=worker))
    client = TestClient(main.app, raise_server_exceptions=True)
    try:
        assert client.get('/api/transfers').status_code == 401
        response = client.get('/api/transfers', auth=('operator', 'synthetic-transfer-test-only'))
        assert response.status_code == 200
        assert response.json() == {'enabled': False, 'dry_run': True, 'delete_source': False,
                                   'error': None, 'counts': {}, 'recent': []}
        assert not worker.s.state.exists()
    finally:
        client.close()
