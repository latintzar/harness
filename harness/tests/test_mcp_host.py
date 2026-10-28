"""Company/session adapters for the standard bearer-only MCP endpoint."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_server_access import fixture_app
from test_workflow_routes import login
from access import COOKIE
HEADERS={"X-Rocoto-Page":"1"}


def test_grants_require_own_workspace_and_tokens_are_returned_once(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://testserver')
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice:
        bob = TestClient(app)
        assert alice.get('/api/mcp/connections').status_code == 401
        login(alice, app, 'alice'); login(bob, app, 'bob')
        assert alice.post('/api/mcp/connections', json={'dataset': 'cafe'}).status_code == 403
        result = alice.post('/api/mcp/connections', json={'dataset': 'cafe', 'label': 'My client'}, headers=HEADERS)
        assert result.status_code == 201, result.text
        data = result.json(); token = data['token']; key = data['connection']['id']
        assert data['url'] == 'https://testserver/mcp'
        assert token.startswith('rmcp_') and result.headers['cache-control'] == 'private, no-store'
        listed = alice.get('/api/mcp/connections')
        assert listed.json()['connections'][0]['id'] == key and token not in listed.text
        assert bob.get('/api/mcp/connections').json()['connections'] == []
        assert bob.delete('/api/mcp/connections/' + key, headers=HEADERS).status_code == 404
        assert alice.post('/api/mcp/connections', json={'dataset': 'absent'}, headers=HEADERS).status_code == 400
        assert alice.post('/api/mcp/connections', json={'dataset': 'cafe', 'principal_id': 'bob'}, headers=HEADERS).status_code == 400
        assert alice.delete('/api/mcp/connections/' + key, headers=HEADERS).status_code == 200
        assert app.state.mcp_host.store.authenticate(token) is None


def test_mcp_bearer_uses_live_membership_and_exact_scope(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://testserver')
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app, 'alice')
        data = client.post('/api/mcp/connections', json={'dataset': 'cafe'}, headers=HEADERS).json()
        token = data['token']
        initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'host-proof', 'version': '1'}}}
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}
        assert client.post('/mcp', json=initialize).status_code == 401
        response = client.post('/mcp', json=initialize, headers=headers)
        assert response.status_code == 200, response.text
        session_id = response.headers['mcp-session-id']
        headers.update({'MCP-Session-Id': session_id, 'MCP-Protocol-Version': '2025-11-25'})
        assert client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers=headers).status_code == 202
        listing = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}, headers=headers)
        from public_mcp import READ_TOOLS
        assert listing.status_code == 200 and {t['name'] for t in listing.json()['result']['tools']} == READ_TOOLS
        assert client.post('/mcp', json=initialize, headers={**headers, 'Origin': 'https://elsewhere.example'}).status_code == 403
        with app.state.access.connect() as db:
            db.execute('DELETE FROM principal_environments WHERE principal_id=?', ('alice',))
        assert client.post('/mcp', json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list', 'params': {}}, headers=headers).status_code == 401


def test_deleted_person_invalidates_existing_mcp_keys(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://testserver')
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app, 'alice')
        value = client.post('/api/mcp/connections', json={'dataset': 'cafe'}, headers=HEADERS).json()
        with app.state.access.connect() as db:
            db.execute('DELETE FROM principals WHERE id=?', ('alice',))
            assert db.execute('SELECT 1 FROM principal_environments WHERE principal_id=?', ('alice',)).fetchone()
        assert client.get('/api/mcp/connections').status_code == 401
        result = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'host-proof', 'version': '1'}}},
            headers={'Authorization': 'Bearer ' + value['token'], 'Accept': 'application/json, text/event-stream'})
        assert result.status_code == 401


def test_local_unconfigured_host_stays_closed(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app, 'alice')
        assert client.get('/api/mcp/connections').json()['available'] is False
        assert client.post('/api/mcp/connections', json={'dataset': 'cafe'}, headers=HEADERS).status_code == 503


def test_observer_reads_metadata_but_cannot_issue_or_revoke(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://testserver')
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice:
        login(alice, app, 'alice')
        grant = alice.post('/api/mcp/connections', json={'dataset': 'cafe'}, headers=HEADERS).json()
        owner = TestClient(app); login(owner, app, 'peter', owner=True)
        token = owner.cookies.get(COOKIE)
        app.state.access.observe(token, 'demo', 'alice')
        assert owner.get('/api/mcp/connections').json()['connections'][0]['id'] == grant['connection']['id']
        assert owner.post('/api/mcp/connections', json={'dataset': 'cafe'}, headers=HEADERS).status_code == 403
        assert owner.delete('/api/mcp/connections/' + grant['connection']['id'], headers=HEADERS).status_code == 403


def test_cold_company_mcp_resolution_keeps_chat_runtime_unopened(tmp_path, monkeypatch):
    import asyncio
    from test_server_access import FixtureAgent
    from workspace_identity import workspace_scope
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://testserver')
    app = fixture_app(tmp_path, monkeypatch)
    root = app.state.access.create_environment('cold-company', 'Cold company')
    app.state.access.issue('cold-company', principal_id='alice')
    grant, _ = app.state.mcp_host.store.issue(company_id=workspace_scope(root), owner_principal_id='alice', dataset='cafe')
    before = dict(FixtureAgent.instances)
    services = asyncio.run(app.state.mcp_host.resolve(grant))
    assert services.state == root and FixtureAgent.instances == before
    assert not (root/'auth').exists() and not (root/'agents').exists()


def test_cancelled_host_authorization_keeps_its_thread_until_actual_completion(tmp_path, monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    import pytest
    app = fixture_app(tmp_path, monkeypatch)
    entered = threading.Event(); release = threading.Event()
    def blocked(*args):
        entered.set(); release.wait(5); return None
    monkeypatch.setattr(app.state.mcp_host, '_environment', blocked)
    async def exercise():
        task = asyncio.create_task(app.state.mcp_host.resolve(SimpleNamespace(company_id='a'*64, owner_principal_id='alice')))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel(); await asyncio.sleep(.03)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
