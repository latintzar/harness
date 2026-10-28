"""Exact inspection JPEGs retain current company, owner, chat and source scope."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from inspection_previews import InspectionPreviews, PreviewError
from test_server_access import FixtureAgent, fixture_app

HEADERS = {'X-Video-Demo': '1'}
JPEG = b'\xff\xd8\xffsynthetic exact sampled JPEG\xff\xd9'


def login(client, app, principal='alice', *, company='demo', owner=False):
    result = client.post('/api/session', json={'code': app.state.access.issue(
        company, principal_id=principal, display_name=principal, owner=owner)}, headers=HEADERS)
    assert result.status_code == 200


def saved_preview(tmp_path, *, company='demo', owner='alice', chat_dataset='cafe'):
    state = tmp_path / 'state'
    if company != 'demo':
        state = state / 'environments' / company
    chat = company + '-chat'
    FixtureAgent.instances[company].children[chat].update(owner_principal_id=owner, dataset=chat_dataset)
    frames = InspectionPreviews(state).save(owner_principal_id=owner, chat_id=chat,
        dataset='cafe', clip_id=company + '-clip', frames=[
            {'time': .25, 'image_bytes': JPEG, 'basis': 'source_sample', 'width': 640, 'height': 360}])
    return frames[0]['url']


def assert_private(response):
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'


@pytest.mark.parametrize('chat_dataset', ['cafe', None])
def test_owned_preview_returns_exact_bytes_without_decoder(tmp_path, monkeypatch, chat_dataset):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app)
        url = saved_preview(tmp_path, chat_dataset=chat_dataset)

        def forbidden(*args, **kwargs):
            pytest.fail('Serving a saved preview invoked a media decoder')

        monkeypatch.setattr('PIL.Image.open', forbidden)
        monkeypatch.setattr('cv2.VideoCapture', forbidden)
        monkeypatch.setattr('subprocess.run', forbidden)
        monkeypatch.setattr('asyncio.create_subprocess_exec', forbidden)
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == JPEG
        assert response.headers['content-type'] == 'image/jpeg'
        assert_private(response)


def test_anonymous_and_browse_never_read_preview_store(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        def forbidden(*args, **kwargs):
            pytest.fail('Unprivileged request reached the private preview store')
        monkeypatch.setattr(InspectionPreviews, 'get', forbidden)
        url = '/api/inspection-previews/' + 'a' * 32 + '/0.jpg'
        assert client.get(url).status_code == 401
        assert client.post('/api/session/demo', headers=HEADERS).status_code == 200
        assert client.get(url).status_code == 403


def test_foreign_owner_in_same_company_cannot_read_preview(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app)
        url = saved_preview(tmp_path)
        login(client, app, 'bob')
        response = client.get(url)
        assert response.status_code == 404 and JPEG not in response.content
        assert_private(response)


def test_same_principal_in_another_company_cannot_read_preview(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    app.state.access.create_environment('company-a', 'Company A')
    app.state.access.create_environment('company-b', 'Company B')
    with TestClient(app) as client:
        login(client, app, company='company-a')
        client.get('/api/status')
        url = saved_preview(tmp_path, company='company-a')
        assert client.get(url).status_code == 200
        login(client, app, company='company-b')
        response = client.get(url)
        assert response.status_code == 404 and JPEG not in response.content
        assert not (tmp_path / 'state/environments/company-b/inspection-previews.sqlite').exists()


def test_owner_observation_uses_exact_observed_principal(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app)
        url = saved_preview(tmp_path)
        login(client, app, 'bob')
        login(client, app, 'peter', owner=True)
        assert client.get(url).status_code == 404
        result = client.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS)
        assert result.status_code == 200 and result.json()['can_query'] is False
        assert client.get(url).content == JPEG
        assert client.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'bob'}, headers=HEADERS).status_code == 200
        assert client.get(url).status_code == 404
        assert client.delete('/api/observer', headers=HEADERS).status_code == 200
        assert client.get(url).status_code == 404


@pytest.mark.parametrize('change', ['deleted_chat', 'chat_owner', 'chat_dataset', 'withdrawn_source', 'source_dataset'])
def test_preview_rechecks_chat_and_source_on_every_get(tmp_path, monkeypatch, change):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app)
        url = saved_preview(tmp_path)
        assert client.get(url).status_code == 200
        agent = FixtureAgent.instances['demo']
        if change == 'deleted_chat':
            del agent.children['demo-chat']
        elif change == 'chat_owner':
            agent.children['demo-chat']['owner_principal_id'] = 'bob'
        elif change == 'chat_dataset':
            agent.children['demo-chat']['dataset'] = 'warehouse'
        elif change == 'withdrawn_source':
            app.state.index._raw.clips.clear()
        else:
            app.state.index._raw.clips[0]['dataset'] = 'warehouse'
        response = client.get(url)
        assert response.status_code == 404 and JPEG not in response.content
        assert_private(response)


def test_chat_removed_during_store_read_is_denied(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app)
        url = saved_preview(tmp_path)
        original_get = InspectionPreviews.get

        def read_then_delete(store, **kwargs):
            row = original_get(store, **kwargs)
            del FixtureAgent.instances['demo'].children['demo-chat']
            return row

        monkeypatch.setattr(InspectionPreviews, 'get', read_then_delete)
        assert client.get(url).status_code == 404


@pytest.mark.parametrize('suffix', ['not-a-receipt/0.jpg', 'a' * 32 + '/4.jpg',
    'a' * 32 + '/-1.jpg', 'a' * 32 + '/x.jpg', 'a' * 32 + '/00.jpg', 'a' * 32 + '/0.jpg'])
def test_bad_or_missing_previews_return_neutral_404_without_creating_store(tmp_path, monkeypatch, suffix):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app)
        response = client.get('/api/inspection-previews/' + suffix)
        assert response.status_code == 404
        assert_private(response)
        assert not (tmp_path / 'state/inspection-previews.sqlite').exists()


def test_store_failure_has_neutral_private_response(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app)
        url = saved_preview(tmp_path)
        def unavailable(*args, **kwargs):
            raise PreviewError('/private/untrusted-error-path')
        monkeypatch.setattr(InspectionPreviews, 'get', unavailable)
        response = client.get(url)
        assert response.status_code == 404
        assert '/private/' not in response.text
        assert_private(response)
