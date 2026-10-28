from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_skill_runtime import HEADERS, make_app


def test_authenticated_search_route_returns_retirement_without_index_dispatch(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.state.index.search = lambda *args: (_ for _ in ()).throw(AssertionError("retired search dispatched"))
    with TestClient(app) as client:
        login = client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS)
        assert login.status_code == 200
        response = client.post('/api/search', json={'query': 'shopper', 'dataset': 'cafe'}, headers=HEADERS)
        assert response.status_code == 410
        assert 'list_tracker_events' in response.json()['detail']


def test_unauthenticated_search_stays_at_access_boundary(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post('/api/search', json={'query': 'shopper'}, headers=HEADERS)
        assert response.status_code == 401
