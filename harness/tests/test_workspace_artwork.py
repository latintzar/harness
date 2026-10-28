"""The real HTTP application serves the exact decorative assets used by CSS."""
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from test_server_access import fixture_app


@pytest.mark.parametrize('name', ['chat-dreamscape.png', 'search-dreamscape.png'])
def test_workspace_artwork_matches_recorded_original_bytes(tmp_path, monkeypatch, name):
    root = Path(__file__).parents[1] / 'web' / 'assets'
    manifest = json.loads((root / 'workspace-backgrounds.json').read_text())
    expected = next(row for row in manifest['assets'] if row['name'] == name)
    with TestClient(fixture_app(tmp_path, monkeypatch)) as client:
        response = client.get('/assets/' + name)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/png'
    assert len(response.content) == expected['size_bytes']
    assert hashlib.sha256(response.content).hexdigest() == expected['sha256']


def test_artwork_routes_keep_repository_provenance_private(tmp_path, monkeypatch):
    with TestClient(fixture_app(tmp_path, monkeypatch)) as client:
        response = client.get('/assets/workspace-backgrounds.json')
    assert response.status_code in {401, 404}
    assert b'original' not in response.content
