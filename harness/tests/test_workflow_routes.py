"""Host-owned camera review and editing-package routes retain principal scope."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import server
from agent_bridge import CodexVideoAgent, tool_specs
from execution_policy import admit_fresh_inspection
from test_server_access import fixture_app, FixtureAgent
from test_media_exports import Index, document

HEADERS = {'X-Video-Demo': '1'}


def login(client, app, principal, **options):
    return client.post('/api/session', json={'code': app.state.access.issue(
        'demo', principal_id=principal, **options)}, headers=HEADERS)


def test_camera_review_is_scoped_idempotent_and_source_frame_only(tmp_path, monkeypatch):
    calls = []
    async def turn(self, prompt, chat_id, dataset):
        calls.append((prompt, chat_id, dataset))
        with pytest.raises(ValueError, match='source frames'):
            admit_fresh_inspection('2.50')
        yield {'type': 'delta', 'text': 'Camera review'}
    monkeypatch.setattr(FixtureAgent, 'turn', turn, raising=False)
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        login(alice, app, 'alice'); login(bob, app, 'bob')
        camera = alice.get('/api/cameras?collection=cafe').json()['cameras'][0]
        body = {'collection': 'cafe', 'camera_ids': [camera['id']]}
        review = alice.post('/api/cameras/review', json=body, headers=HEADERS)
        assert review.status_code == 200
        result = review.json()
        assert result['kind'] == 'camera_review' and result['parent_id'] is None
        assert alice.post('/api/cameras/review', json=body, headers=HEADERS).json()['id'] == result['id']
        assert len(calls) == 1
        assert 'reference frames' in calls[0][0]
        assert bob.get(result['url'].replace('/?chat=', '/api/chats/')).status_code == 404
        assert bob.post('/api/cameras/review', json=body, headers=HEADERS).json()['id'] != result['id']
        assert alice.post('/api/cameras/review', json={**body, 'collection': 'warehouse'}, headers=HEADERS).status_code == 404
        alice.post('/api/session/demo', headers=HEADERS)
        assert alice.post('/api/cameras/review', json=body, headers=HEADERS).status_code == 403


def test_session_lists_only_granted_demo_collections(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/session').json()['environments'] == []
        data = login(client, app, 'alice').json()
        assert data['environments'][0]['category'] == 'demo'
        assert data['environments'][0]['collections'] == [{'id': 'cafe', 'name': 'Café'}]
        app.state.access.create_environment('other-company', 'Other')
        client.post('/api/session', json={'code': app.state.access.issue('other-company')}, headers=HEADERS)
        assert [x['id'] for x in client.get('/api/session').json()['environments']] == ['other-company']


def test_real_export_routes_are_private_and_observer_can_only_read(tmp_path, monkeypatch):
    # Scope this tiny actual-media fixture to its own package budget.
    monkeypatch.setattr('media_exports.MAX_OUTPUT_BYTES',8*1024**2)
    state = tmp_path / 'state'; source = state / 'media' / 'source.mp4'
    source.parent.mkdir(parents=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=96x64:rate=10:duration=3', '-c:v', 'libx264', '-threads', '1', str(source)], check=True)
    index = Index(source); index.company = 'demo'; index.status = lambda: {'ready': True, 'datasets': ['cafe']}
    monkeypatch.setattr(server, 'HarnessAgent', FixtureAgent)
    monkeypatch.setattr(server.shutil, 'which', lambda name: None)
    app = server.create_app(state, index)
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as owner:
        login(alice, app, 'alice'); login(bob, app, 'bob'); login(owner, app, 'owner', owner=True)
        result = alice.post('/api/exports', json={'dataset': 'cafe', 'document': document()}, headers=HEADERS)
        assert result.status_code == 200, result.text
        result = result.json(); assert result['status'] == 'ready'
        path = result['source_url']; download = result['download_url']
        assert alice.get(download).content[:2] == b'PK'
        assert bob.get(path).status_code == 404 and bob.get(download).status_code == 404
        assert bob.get('/api/exports').json()['exports'] == []
        owner.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS)
        assert owner.get(download).content[:2] == b'PK'
        assert owner.post('/api/exports', json={'dataset': 'cafe', 'document': document()}, headers=HEADERS).status_code == 403
        assert str(state) not in json.dumps(alice.get(path).json())
        bob.post('/api/session/demo', headers=HEADERS)
        assert bob.get(download).status_code == 403


def test_export_tools_require_parent_and_host_bound_owner(tmp_path):
    class EmptyIndex:
        clips = []
    agent = CodexVideoAgent(tmp_path, EmptyIndex(), lambda: [])
    class Exports:
        record = {'id': 'export-' + 'a' * 32, 'title': 'Editing handoff', 'dataset': 'cafe',
                  'created_at': '2026-10-04T00:00:00Z', 'status': 'ready',
                  'download_url': '/api/exports/' + 'export-' + 'a' * 32 + '/download'}
        async def prepare(self, document, **scope):
            assert scope == {'dataset': 'cafe', 'owner_principal_id': 'alice'}
            return self.record
        def get(self, identity, **scope):
            assert identity == self.record['id'] and scope['owner_principal_id'] == 'alice'
            assert scope['dataset'] in (None, 'cafe')
            return self.record
    agent.export_store = Exports()
    agent.children['parent'] = {'id': 'parent', 'owner_principal_id': 'alice'}
    agent.children['child'] = {'id': 'child', 'owner_principal_id': 'alice', 'parent_id': 'parent'}
    async def run():
        for chat in ['child', 'unowned']:
            assert not (await agent._tool('prepare_export', {'document': document()}, 'cafe', chat))[2]
        result = await agent._tool('prepare_export', {'document': document()}, 'cafe', 'parent')
        assert result[2] and json.loads(result[0][0]['text'])['status'] == 'ready'
    asyncio.run(run())
    assert 'prepare_export' in {t['name'] for t in tool_specs()}
    assert 'prepare_export' not in {t['name'] for t in tool_specs(children=False)}
