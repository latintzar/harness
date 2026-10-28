"""OAuth and private bearer setup stay bound to the initiating account/session."""
from contextlib import asynccontextmanager
import hashlib
import json
import pytest
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from mcp.types import ListToolsResult, Tool

from test_server_access import fixture_app
from test_workflow_routes import HEADERS, login
from test_mcp_connections import Session


PRIVATE_TOKEN = 'fixture-private-bearer_123+/='


@pytest.fixture
def bearer_app(tmp_path, monkeypatch):
    from catalogue_providers import RemoteProviderCommissioning
    from mcp_connections import MCPConnections
    from test_catalogue_providers import descriptor
    from workspace_identity import workspace_scope
    import provider_configuration
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://testserver')
    monkeypatch.setattr(provider_configuration, 'remote_providers_for', lambda state, *, public_origin:
        RemoteProviderCommissioning(workspace_scope(state), public_origin + '/api/connections/oauth/callback',
                                    (descriptor(auth='bearer'),)))
    calls = []
    class Reads(Session):
        async def list_tools(self, cursor=None):
            return ListToolsResult(tools=[Tool(name='read_original', inputSchema={'type': 'object'})])
    @asynccontextmanager
    async def connection(self, provider, principal, generation, flow=None):
        row = self._record(provider, principal)
        token = json.loads(row['tokens'])['access_token']
        calls.append({'company': self.company_scope, 'principal': principal,
                      'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
                      'access_mode': row['access_mode']})
        yield Reads()
    monkeypatch.setattr(MCPConnections, '_session', connection)
    return fixture_app(tmp_path, monkeypatch), calls


def test_connection_http_scope_callback_and_disconnect(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://testserver')
    app = fixture_app(tmp_path, monkeypatch)
    broker = app.state.agent.connection_store
    class Reads(Session):
        async def list_tools(self, cursor=None):
            return ListToolsResult(tools=[Tool(name='notion-search', description='Search', inputSchema={'type': 'object'})])
    @asynccontextmanager
    async def connection(provider, principal, generation, flow=None):
        await broker._redirect(flow, 'https://mcp.notion.com/authorize?state=state-' + flow.id + '&code_challenge=test&code_challenge_method=S256')
        code, state = await flow.callback
        assert code == 'valid-code'
        yield Reads()
    monkeypatch.setattr(broker, '_session', connection)
    # Three browser sessions share one server lifecycle. The native MCP SDK
    # session manager, like the deployed ASGI app, starts once per process.
    with TestClient(app, base_url='https://testserver') as alice:
        bob = TestClient(app, base_url='https://testserver')
        owner = TestClient(app, base_url='https://testserver')
        login(alice, app, 'alice'); login(bob, app, 'bob'); login(owner, app, 'owner', owner=True)
        flow = alice.post('/api/connections/notion/connect', headers=HEADERS).json()
        assert flow['status'] == 'awaiting_authorization'
        url = '/api/connections/flows/' + flow['flow_id']
        assert bob.get(url).status_code == 404
        state = parse_qs(urlsplit(flow['authorization_url']).query)['state'][0]
        callback = '/api/connections/oauth/callback'
        assert alice.get(callback + '?state=' + state + '&code=valid-code').status_code == 200
        data = {'state': state, 'code': 'valid-code'}
        assert bob.post(callback, json=data, headers=HEADERS).status_code == 400
        assert alice.post(callback, json=data).status_code == 403
        assert alice.post(callback, json=data, headers={**HEADERS, 'Origin': 'https://other.example'}).status_code == 403
        result = alice.post(callback, json=data, headers=HEADERS)
        assert result.status_code == 200 and result.json()['status'] == 'connected'
        assert alice.post(callback, json=data, headers=HEADERS).status_code == 400
        assert 'connected' in alice.get('/api/connections').text
        assert all(c['status'] == ('disconnected' if c['available'] else 'setup_required')
                   for c in bob.get('/api/connections').json()['connections'])
        owner.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS)
        assert 'connected' in owner.get('/api/connections').text
        assert owner.delete('/api/connections/notion', headers=HEADERS).status_code == 403
        assert alice.delete('/api/connections/notion', headers=HEADERS).status_code == 200


def test_public_oauth_landing_keeps_callback_code_out_of_content(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        result = client.get('/api/connections/oauth/callback?code=private-code&state=private-state')
        assert result.status_code == 200
        assert 'private-code' not in result.text
        assert result.headers['referrer-policy'] == 'no-referrer'
        assert result.headers['cache-control'] == 'no-store'
        assert client.get('/oauth-callback.js').status_code == 200
        assert client.post('/api/connections/oauth/callback', json={'code': 'x', 'state': 'x'}, headers=HEADERS).status_code == 401


def test_connection_access_mode_is_bounded_and_host_scoped(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    calls = []
    async def start(provider, **kwargs):
        calls.append((provider, kwargs))
        return {'status': 'connecting'}
    monkeypatch.setattr(app.state.agent.connection_store, 'start', start)
    url = '/api/connections/notion/connect'
    with TestClient(app) as alice:
        owner = TestClient(app)
        login(alice, app, 'alice'); login(owner, app, 'owner', owner=True)
        assert alice.post(url, json={'access_mode': 'read_write'}, headers=HEADERS).status_code == 200
        assert calls[-1][0] == 'notion'
        assert calls[-1][1]['principal_id'] == 'alice'
        assert calls[-1][1]['access_mode'] == 'read_write'
        assert alice.post(url, headers=HEADERS).status_code == 200
        assert calls[-1][1]['access_mode'] == 'read'
        for payload in ([], None, {'access_mode': 'admin'}, {'access_mode': False}, {'principal_id': 'bob'},
                        {'access_mode': 'read', 'oauth_scopes': ['write']}):
            assert alice.post(url, content=__import__('json').dumps(payload), headers=HEADERS).status_code == 400
        assert alice.post(url, content='{', headers=HEADERS).status_code == 400
        assert alice.post(url, content=' ' * 4097, headers=HEADERS).status_code == 413
        assert alice.post(url, json={'access_mode': 'read_write'}).status_code == 403
        owner.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS)
        assert owner.post(url, json={'access_mode': 'read_write'}, headers=HEADERS).status_code == 403
        assert len(calls) == 2


def test_provider_write_commissioning_requires_isolated_runtime(tmp_path, monkeypatch):
    (tmp_path / 'disabled').mkdir(); (tmp_path / 'enabled').mkdir()
    monkeypatch.setenv('ROCOTO_NATIVE_WRITE_PROVIDERS', 'notion,linear')
    monkeypatch.delenv('ROCOTO_NATIVE_EXECUTOR_ENABLED', raising=False)
    with pytest.raises(RuntimeError, match='isolated'):
        fixture_app(tmp_path / 'disabled', monkeypatch)
    monkeypatch.setenv('ROCOTO_NATIVE_EXECUTOR_ENABLED', '1')
    app = fixture_app(tmp_path / 'enabled', monkeypatch)
    with TestClient(app) as client:
        login(client, app, 'alice')
        rows = {row['id']: row for row in client.get('/api/connections').json()['connections']}
        assert rows['notion']['write_available'] is True
        assert rows['linear']['write_available'] is True
        assert rows['figma']['write_available'] is False
        assert rows['notion']['access_mode'] == 'read'


def test_local_agent_connection_store_keeps_host_writes_disabled(tmp_path, monkeypatch):
    from agent_test_support import root_agent
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://rocoto.example')
    monkeypatch.setenv('ROCOTO_NATIVE_WRITE_PROVIDERS', 'notion')
    agent = root_agent(tmp_path)
    broker = agent._connections()
    assert broker.public_origin == 'https://rocoto.example'
    assert broker.write_providers == frozenset()


def test_private_bearer_setup_uses_real_owner_vault_and_session_bound_flow(bearer_app, caplog):
    app, calls = bearer_app
    broker = app.state.agent.connection_store
    with TestClient(app, base_url='https://testserver') as alice:
        bob = TestClient(app, base_url='https://testserver')
        second_session = TestClient(app, base_url='https://testserver')
        login(alice, app, 'alice'); login(bob, app, 'bob'); login(second_session, app, 'alice')
        response = alice.post('/api/connections/canva/connect', headers=HEADERS,
                              json={'token': PRIVATE_TOKEN, 'access_mode': 'read_write'})
        assert response.status_code == 200, response.text
        flow = response.json()
        assert flow['provider'] == 'canva' and flow['status'] == 'connected'
        assert response.headers['cache-control'] == 'private, no-store'
        path = '/api/connections/flows/' + flow['flow_id']
        assert alice.get(path).status_code == 200
        assert bob.get(path).status_code == second_session.get(path).status_code == 404
        assert calls == [{'company': broker.company_scope, 'principal': 'alice',
                          'token_sha256': hashlib.sha256(PRIVATE_TOKEN.encode()).hexdigest(),
                          'access_mode': 'read_write'}]
        assert broker._record('canva', 'bob') is None
        responses = [response, alice.get(path), alice.get('/api/connections'), bob.get('/api/connections')]
        assert all(PRIVATE_TOKEN not in value.text for value in responses)
        assert PRIVATE_TOKEN not in caplog.text
        assert all(PRIVATE_TOKEN.encode() not in path.read_bytes()
                   for path in broker.directory.iterdir() if path.is_file())


def test_bearer_credentials_are_isolated_between_companies(bearer_app):
    from test_server_access import FixtureAgent
    app, calls = bearer_app
    app.state.access.create_environment('company-b', 'Company B')
    with TestClient(app, base_url='https://testserver') as first:
        other = TestClient(app, base_url='https://testserver')
        login(first, app, 'alice')
        other.post('/api/session', json={'code': app.state.access.issue('company-b', principal_id='alice')}, headers=HEADERS)
        response = first.post('/api/connections/canva/connect', json={'token': PRIVATE_TOKEN}, headers=HEADERS)
        assert response.status_code == 200
        assert other.get('/api/connections/flows/' + response.json()['flow_id']).status_code == 404
        company_rows = other.get('/api/connections').json()['connections']
        assert next(row for row in company_rows if row['id'] == 'canva')['status'] == 'disconnected'
        second = other.post('/api/connections/canva/connect', json={'token': 'other-private-token'}, headers=HEADERS)
        assert second.status_code == 200 and second.json()['status'] == 'connected'
        assert calls[0]['company'] != calls[1]['company']
        assert calls[0]['principal'] == calls[1]['principal'] == 'alice'
        assert FixtureAgent.instances['company-b'].connection_store._record('canva', 'alice')['generation'] == 1


@pytest.mark.parametrize('payload', [
    {}, {'token': None}, {'token': 3}, {'token': True}, {'token': []}, {'token': {}}, {'token': ''},
    {'token': PRIVATE_TOKEN + '\n'}, {'token': 'Bearer ' + PRIVATE_TOKEN},
    {'token': 'x' * 16385}, {'token': PRIVATE_TOKEN, 'principal_id': 'bob'},
    {'token': PRIVATE_TOKEN, 'endpoint': 'https://other.example/mcp'},
    {'token': PRIVATE_TOKEN, 'auth': 'anonymous'}, {'token': PRIVATE_TOKEN, 'expires_at': 9999999999},
    {'token': PRIVATE_TOKEN, 'access_mode': 'admin'},
])
def test_malformed_bearer_body_is_private_and_never_starts_connection(bearer_app, payload, caplog):
    app, calls = bearer_app
    with TestClient(app, base_url='https://testserver') as alice:
        login(alice, app, 'alice')
        response = alice.post('/api/connections/canva/connect', json=payload, headers=HEADERS)
        assert response.status_code == 400
        assert PRIVATE_TOKEN not in response.text + caplog.text
        assert calls == []
        assert app.state.agent.connection_store._record('canva', 'alice') is None


def test_bearer_body_is_bounded_and_accepts_exact_maximum_token(bearer_app):
    app, calls = bearer_app
    with TestClient(app, base_url='https://testserver') as alice:
        login(alice, app, 'alice')
        url = '/api/connections/canva/connect'
        response = alice.post(url, content=b'{"token":"' + PRIVATE_TOKEN.encode() + b'\xff"}', headers=HEADERS)
        assert response.status_code == 400 and PRIVATE_TOKEN not in response.text
        response = alice.post(url, content=PRIVATE_TOKEN + ' ' * (20 * 1024), headers=HEADERS)
        assert response.status_code == 413 and PRIVATE_TOKEN not in response.text
        response = alice.post(url, json={'token': 't' * 16384}, headers=HEADERS)
        assert response.status_code == 200 and response.json()['status'] == 'connected'
        assert len(calls) == 1


@pytest.mark.parametrize('provider', ['notion', 'openai-docs'])
def test_oauth_and_anonymous_providers_reject_private_token(bearer_app, provider):
    app, calls = bearer_app
    with TestClient(app, base_url='https://testserver') as alice:
        login(alice, app, 'alice')
        response = alice.post('/api/connections/' + provider + '/connect', json={'token': PRIVATE_TOKEN}, headers=HEADERS)
        assert response.status_code == 400 and PRIVATE_TOKEN not in response.text
        assert calls == []


def test_bearer_entry_requires_current_writable_owner_session_and_csrf(bearer_app):
    app, calls = bearer_app
    with TestClient(app, base_url='https://testserver') as alice:
        url = '/api/connections/canva/connect'
        body = {'token': PRIVATE_TOKEN}
        assert alice.post(url, json=body, headers=HEADERS).status_code == 401
        login(alice, app, 'alice')
        assert alice.post(url, json=body).status_code == 403
        assert alice.post(url, json=body, headers={**HEADERS, 'Origin': 'https://other.example'}).status_code == 403
        owner = TestClient(app, base_url='https://testserver')
        login(owner, app, 'owner', owner=True)
        owner.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS)
        assert owner.post(url, json=body, headers=HEADERS).status_code == 403
        assert calls == []


def test_bearer_connection_error_cannot_echo_secret(bearer_app, monkeypatch, caplog):
    from mcp_connections import ConnectionError
    app, calls = bearer_app
    async def failed(provider, credential, **kwargs):
        assert credential.token == PRIVATE_TOKEN and PRIVATE_TOKEN not in repr(credential)
        raise ConnectionError('Provider rejected ' + credential.token)
    monkeypatch.setattr(app.state.agent.connection_store, 'connect_bearer', failed)
    with TestClient(app, base_url='https://testserver') as alice:
        login(alice, app, 'alice')
        response = alice.post('/api/connections/canva/connect', json={'token': PRIVATE_TOKEN}, headers=HEADERS)
        assert response.status_code == 400
        assert PRIVATE_TOKEN not in response.text + caplog.text
        assert response.headers['cache-control'] == 'private, no-store'
        assert calls == []
