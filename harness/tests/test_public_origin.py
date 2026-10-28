"""A configured staging hostname preserves same-origin and session boundaries."""
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from server import create_app
from test_server_access import FixtureIndex, fixture_app
from access import COOKIE


def test_exact_public_origin_preserves_session_and_same_origin_write_rules(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://video-demo.soyrocoto.com')
    monkeypatch.setenv('ROCOTO_VIDEO_DEMO_BROWSING', 'true')
    app = create_app(tmp_path, FixtureIndex())
    with TestClient(app, base_url='https://video-demo.soyrocoto.com') as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/status').status_code == 401
        assert client.post('/api/session/demo', headers={
            'X-Video-Demo': '1', 'Origin': 'https://foreign.example'}).status_code == 403
        result = client.post('/api/session/demo', headers={
            'X-Video-Demo': '1', 'Origin': 'https://video-demo.soyrocoto.com'})
        assert result.status_code == 200
        assert 'Secure' in result.headers['set-cookie']
        assert client.get('/api/status').status_code == 200
        assert client.get('/', headers={'Host': 'foreign.example'}).status_code == 403


def test_public_origin_sample_browsing_requires_invitation(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://video-demo.soyrocoto.com')
    monkeypatch.delenv('ROCOTO_VIDEO_DEMO_BROWSING', raising=False)
    app = fixture_app(tmp_path, monkeypatch)
    headers = {'X-Video-Demo': '1'}
    with TestClient(app, base_url='https://video-demo.soyrocoto.com') as client:
        configuration = client.get('/api/session').json()
        assert configuration['demo_browsing'] is False
        assert configuration['authenticated'] is False
        assert client.post('/api/session/demo', headers=headers).status_code == 403
        for path in ['/api/clip/demo-clip', '/thumbnails/demo-clip.jpg',
                     '/clips/demo-clip.mp4', '/originals/demo-video']:
            assert client.get(path).status_code == 401
        code = app.state.access.issue('demo', owner=True, principal_id='fixture-owner')
        login = client.post('/api/session', json={'code': code}, headers=headers)
        assert login.status_code == 200
        assert login.json()['role'] == 'owner'
        assert 'Secure' in login.headers['set-cookie']
        assert client.get('/api/clip/demo-clip').status_code == 200
        assert client.get('/thumbnails/demo-clip.jpg').content == b'fixture-image'
        assert client.post('/api/session', json={'code': code}, headers=headers).status_code == 401


def test_invite_only_host_rejects_previously_issued_browse_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', 'https://video-demo.soyrocoto.com')
    monkeypatch.delenv('ROCOTO_VIDEO_DEMO_BROWSING', raising=False)
    app = fixture_app(tmp_path, monkeypatch)
    token, _ = app.state.access.login(app.state.access.issue('demo'), can_query=False)
    with TestClient(app, base_url='https://video-demo.soyrocoto.com') as client:
        client.cookies.set(COOKIE, token)
        assert client.get('/api/session').json()['authenticated'] is False
        assert client.get('/thumbnails/demo-clip.jpg').status_code == 401


def test_local_demo_browsing_remains_available(tmp_path, monkeypatch):
    monkeypatch.delenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', raising=False)
    monkeypatch.delenv('ROCOTO_VIDEO_DEMO_BROWSING', raising=False)
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/session').json()['demo_browsing'] is True
        assert client.post('/api/session/demo', headers={'X-Video-Demo': '1'}).status_code == 200
        assert client.get('/thumbnails/demo-clip.jpg').content == b'fixture-image'


def test_invalid_demo_browsing_flag_stops_startup(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_VIDEO_DEMO_BROWSING', 'maybe')
    with pytest.raises(ValueError, match='ROCOTO_VIDEO_DEMO_BROWSING'):
        create_app(tmp_path, FixtureIndex())


@pytest.mark.parametrize('origin', ['http://public.example', 'https://a.example/path',
                                    'https://u:p@a.example', 'https://a.example?x=1'])
def test_malformed_public_origin_stops_startup(tmp_path, monkeypatch, origin):
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN', origin)
    with pytest.raises(ValueError):
        create_app(tmp_path, FixtureIndex())
