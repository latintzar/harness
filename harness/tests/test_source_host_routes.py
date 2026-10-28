"""Application registration remains provider-free and follows saved ownership."""
from fastapi.testclient import TestClient

from test_server_access import fixture_app
from test_workflow_routes import HEADERS, login


def commission_fixture(state):
    """Synthetic host records only: this fixture never starts a paid worker."""
    from datetime import datetime, timedelta, timezone
    import hashlib
    import json
    import time
    from source_service import SourceHost, PURPOSE, WORKSPACE, compute_guard, HEARTBEAT_TTL
    from workspace_identity import workspace_scope

    def write(path, document):
        path.write_text(json.dumps(document))
        path.chmod(0o600)

    folder = state / 'compute'
    folder.mkdir(exist_ok=True, mode=0o700)
    now = datetime.now(timezone.utc)
    approval = folder / 'synthetic-embedding.json'
    write(approval, {'schema_version': 1, 'approval_id': 'synthetic-host-fixture',
        'purpose': PURPOSE, 'provider': 'modal', 'workspace': WORKSPACE,
        'verified_by': compute_guard.OWNER, 'verification_source': 'https://modal.com/settings/usage',
        'user_verified': True, 'verified_at': now.isoformat(),
        'expires_at': (now + timedelta(hours=1)).isoformat(), 'net_spend_limit_usd': '0',
        'credit_remaining_usd': '20', 'reserved_credit_usd': '10', 'approved_run_gross_usd': '1.00'})
    write(folder / 'source-intake-service.json', {'schema_version': 1, 'enabled': True,
        'company_id': workspace_scope(state), 'principal_id': 'alice', 'collection': 'cafe',
        'approval_path': str(approval), 'approval_sha256': hashlib.sha256(approval.read_bytes()).hexdigest(),
        'receipt_sha256': 'a' * 64, 'modal_python': '/synthetic/python', 'modal_config': '/synthetic/modal.toml'})
    host = SourceHost(state)
    checked = time.time()
    write(host.root / 'heartbeat.json', {'schema_version': 1, 'company_id': host.company,
        'ready': True, 'config_sha256': host.configuration().fingerprint, 'code_sha256': host.code_sha256,
        'checked_at': checked, 'expires_at': checked + HEARTBEAT_TTL})
    return host


def test_gallery_import_routes_are_registered_but_admission_starts_closed(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/library/imports').status_code == 401
        login(client, app, 'alice')
        response = client.get('/api/library/imports?collection=cafe')
        assert response.status_code == 200
        payload = response.json()
        assert payload['imports'] == []
        assert payload['intake']['can_upload'] is False
        assert payload['intake']['can_manage_imports'] is False
        assert payload['intake']['collections'] == ['cafe']
        attempt = client.post('/api/library/imports', json={
            'collection': 'cafe', 'filename': 'recording.mp4', 'size': 8,
        }, headers={**HEADERS, 'Idempotency-Key': 'closed-upload'})
        assert attempt.status_code == 503
    assert not list((tmp_path / 'state').glob('**/embedding-*/attempt.json'))


def test_live_host_binding_survives_grant_consumption_and_worker_outage(tmp_path, monkeypatch):
    import source_service
    from source_intake import SourceStore, IntakeActor
    app = fixture_app(tmp_path, monkeypatch)
    state = tmp_path / 'state'
    assert not (state / 'source-intake').exists()
    host = commission_fixture(state)
    # HTTP lifecycle must never construct either coordinator or provider adapter.
    def forbidden(*args, **kwargs):
        raise AssertionError('The HTTP process started source execution')
    monkeypatch.setattr(source_service, 'SourceService', forbidden)
    monkeypatch.setattr(source_service, 'SourceEmbedding', forbidden)
    body = {'collection': 'cafe', 'filename': 'recording.mp4', 'size': 8}
    create_headers = {**HEADERS, 'Idempotency-Key': 'accepted-transfer'}
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as observer:
        login(alice, app, 'alice'); login(bob, app, 'bob'); login(observer, app, 'peter', owner=True)
        assert alice.get('/api/library/imports').json()['intake']['can_upload']
        assert not bob.get('/api/library/imports').json()['intake']['can_upload']
        response = alice.post('/api/library/imports', json=body, headers=create_headers)
        assert response.status_code == 201, response.text
        record = response.json()['import']; route = '/api/library/imports/' + record['id']
        assert bob.get(route).status_code == 404
        assert bob.post('/api/library/imports', json=body, headers=create_headers).status_code == 403
        # After admission closes, the exact accepted transfer remains actionable.
        source_service.compute_guard.claim_approval(host.approval(host.configuration()))
        (host.root / 'heartbeat.json').unlink()
        intake = alice.get('/api/library/imports').json()['intake']
        assert not intake['can_upload'] and intake['can_manage_imports']
        assert alice.post('/api/library/imports', json=body, headers=create_headers).json()['import'] == record
        headers = {**HEADERS, 'Content-Type': 'application/octet-stream', 'Upload-Offset': '0'}
        assert alice.patch(route + '/content', content=b'abcdefgh', headers=headers).status_code == 200
        assert bob.patch(route + '/content', content=b'abcdefgh', headers=headers).status_code == 404
        assert observer.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS).status_code == 200
        assert observer.get(route).json()['import']['committed_bytes'] == 8
        assert not observer.get('/api/library/imports').json()['intake']['can_manage_imports']
        assert observer.delete(route, headers=HEADERS).status_code == 403
        assert alice.post(route + '/complete', json={}, headers=HEADERS).json()['import']['status'] == 'received'
        assert alice.delete(route, headers=HEADERS).json()['import']['status'] == 'cancelled'
    stored = SourceStore(state).get_import(IntakeActor('alice'), record['id'])
    assert stored['asset']['status'] == 'cancelled'
    assert not list(state.glob('**/embedding-*/attempt.json'))
