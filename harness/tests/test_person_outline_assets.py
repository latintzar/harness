"""The optional person outline loads only authenticated, pinned local assets."""
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1]))


def test_official_outline_assets_match_receipt():
    import control_room_routes as routes
    root = routes.CODE / routes.OUTLINE_VENDOR_ROOT
    receipt = json.loads((root / 'manifest.json').read_text())
    assert receipt['version'] == '0.10.22-rc.20250304'
    assert receipt['model'] == 'selfie_segmenter float16 version 1'
    for item in receipt['files']:
        data = (root / item['path']).read_bytes()
        assert len(data) == item['bytes']
        assert hashlib.sha256(data).hexdigest() == item['sha256']
        assert item['source'].startswith(('https://registry.npmjs.org/@mediapipe/',
                                          'https://storage.googleapis.com/mediapipe-models/',
                                          'https://raw.githubusercontent.com/google-ai-edge/mediapipe/'))
        assert routes.OUTLINE_VENDOR_ROOT + item['path'] in routes.OUTLINE_VENDOR_ASSETS


def test_outline_assets_authenticated_and_wasm_policy_confined(tmp_path, monkeypatch):
    import control_room_routes as routes
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app, HEADERS
    code = tmp_path / 'code'
    code.mkdir()
    for asset in ('index.html', 'person-outline-worker.js', 'person-outline.js',
                  routes.OUTLINE_VENDOR_ROOT + 'vision_bundle.mjs',
                  routes.OUTLINE_VENDOR_ROOT + 'wasm/vision_wasm_internal.wasm',
                  routes.OUTLINE_VENDOR_ROOT + 'selfie_segmenter.tflite'):
        target = code / asset
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'fixture')
    (code / 'unpublished-model.tflite').write_bytes(b'private-model')
    monkeypatch.setattr(routes, 'CODE', code)
    state = tmp_path / 'app'
    state.mkdir()
    app = make_app(state, monkeypatch)
    with TestClient(app) as client:
        model = '/control-room/' + routes.OUTLINE_VENDOR_ROOT + 'selfie_segmenter.tflite'
        assert client.get(model).status_code == 401
        client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS)
        assert client.get(model).status_code == 200
        assert client.get(model).headers['cache-control'] == 'private, max-age=31536000, immutable'
        assert client.get('/control-room/unpublished-model.tflite').status_code == 404
        wasm = client.get('/control-room/' + routes.OUTLINE_VENDOR_ROOT + 'wasm/vision_wasm_internal.wasm')
        assert wasm.status_code == 200
        assert wasm.headers['content-type'] == 'application/wasm'
        module = client.get('/control-room/' + routes.OUTLINE_VENDOR_ROOT + 'vision_bundle.mjs')
        assert module.status_code == 200 and 'javascript' in module.headers['content-type']
        worker = client.get('/control-room/person-outline-worker.js')
        policy = worker.headers['content-security-policy']
        assert "'wasm-unsafe-eval'" in policy
        assert "'unsafe-eval'" not in policy and 'blob:' not in policy and 'https:' not in policy
        assert "connect-src 'self'" in policy
        for path in ('/', '/app.js', '/control-room/', '/control-room/person-outline.js'):
            response = client.get(path)
            assert response.status_code == 200
            policy = response.headers['content-security-policy']
            assert "'wasm-unsafe-eval'" not in policy
            assert "'unsafe-eval'" not in policy
        assert "worker-src 'self'" in client.get('/control-room/').headers['content-security-policy']
