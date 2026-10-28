"""Vertical URLs name their owner; retained addresses share exact endpoints."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_canonical_and_saved_alias_share_endpoint_and_methods():
    from retail_urls import retail_router
    app = FastAPI()
    router = retail_router(app)
    @router.get('/api/retail/cameras')
    def cameras():
        return {'scope': 'same'}
    old = next(route for route in app.routes if route.path == '/api/cameras')
    new = next(route for route in app.routes if route.path == '/api/retail/cameras')
    assert old.endpoint is new.endpoint and old.methods == new.methods
    with TestClient(app) as client:
        assert client.get('/api/cameras').json() == client.get('/api/retail/cameras').json()
        assert client.post('/api/retail/cameras').status_code == 405


def test_new_top_level_route_is_rejected():
    from retail_urls import retail_router
    app = FastAPI()
    with pytest.raises(ValueError, match='namespace'):
        retail_router(app).get('/api/new-retail-feature')(lambda: {})


def test_canonical_scope_classification_maps_exact_templates():
    from retail_urls import legacy_path
    assert legacy_path('/api/retail/storage/conn-123/run', 'POST') == '/api/connections/custom/conn-123/storage/run'
    assert legacy_path('/api/retail/cameras/camera-1/context', 'POST') == '/api/cameras/camera-1/context'
    assert legacy_path('/pages/retail/catalog-upload/demo', 'POST') == '/api/catalog/mobile-upload/demo'
    assert legacy_path('/pages/retail/catalog-upload/demo', 'GET') == '/catalog-upload/demo'
    assert legacy_path('/api/retail/unregistered', 'GET') == '/api/retail/unregistered'


def test_real_host_has_one_endpoint_per_alias_and_canonical_method(tmp_path, monkeypatch):
    from retail_urls import LEGACY, METHODS
    from test_server_access import fixture_app
    app = fixture_app(tmp_path, monkeypatch)
    for old, canonical in LEGACY.items():
        if canonical.startswith('/api/retail/realtime/'):
            continue  # Registered only when maintained read authority is commissioned.
        for method in METHODS[old]:
            before = [route for route in app.routes if getattr(route, 'path', None) == old and method in getattr(route, 'methods', ())]
            after = [route for route in app.routes if getattr(route, 'path', None) == canonical and method in getattr(route, 'methods', ())]
            assert len(before) == len(after) == 1, (old, canonical, method, len(before), len(after))
            assert before[0].endpoint is after[0].endpoint
    with TestClient(app) as client:
        for path in ('/api/cameras', '/api/retail/cameras', '/api/retail/location',
                     '/api/business/scope?collection=company', '/api/retail/scope?collection=company'):
            assert client.get(path).status_code == 401
        for path in ('/static/retail/retail-ui.js', '/static/retail/camera-streams.js', '/static/retail/tracker-canvas.css'):
            response = client.get(path)
            assert response.status_code == 200 and len(response.content) > 100
        for extension in ('js', 'css'):
            canonical = client.get('/static/retail/retail-scope.' + extension)
            assert canonical.status_code == 200
            for prefix in ('/', '/static/harness/'):
                alias = client.get(prefix + 'business-scope.' + extension)
                assert alias.status_code == 200 and alias.content == canonical.content
        assert client.get('/static/retail/retail_host.py').status_code in {401, 404}
        assert client.get('/pages/retail/catalog-upload/demo').status_code == 200
        assert client.post('/api/session/demo', headers={'X-Rocoto-Page': '1'}).status_code == 200
        for path in ('/api/search', '/api/retail/search', '/api/cameras/review', '/api/retail/cameras/review',
                     '/api/connections/custom/fixture/storage/run', '/api/retail/storage/fixture/run'):
            assert client.post(path, json={}, headers={'X-Rocoto-Page': '1'}).status_code == 403, path
        for path in ('/api/exports', '/api/retail/exports', '/api/retail/inspection-previews/' + 'a' * 32 + '/0.jpg'):
            assert client.get(path).status_code == 403, path


def test_canonical_media_reads_keep_current_company_boundary(tmp_path, monkeypatch):
    from test_server_access import fixture_app
    app = fixture_app(tmp_path, monkeypatch)
    for company in ('company-a', 'company-b'):
        app.state.access.create_environment(company, company)
    with TestClient(app) as first, TestClient(app) as second:
        for client, company in ((first, 'company-a'), (second, 'company-b')):
            assert client.post('/api/session', json={'code': app.state.access.issue(company)},
                               headers={'X-Rocoto-Page': '1'}).status_code == 200
            old = client.get('/api/clip/' + company + '-clip')
            new = client.get('/api/retail/clip/' + company + '-clip')
            assert old.status_code == new.status_code == 200 and old.json() == new.json()
        assert second.get('/api/retail/clip/company-a-clip').status_code == 404
        assert second.get('/clips/company-a-clip.mp4').status_code == 404
