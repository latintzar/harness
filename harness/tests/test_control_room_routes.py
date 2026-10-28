import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from control_room_routes import owner_directory, safe_file


def test_safe_assets_reject_traversal_and_symlinks(tmp_path):
    root = tmp_path/'bundle'; root.mkdir()
    (root/'ok.json').write_text('{}')
    outside = tmp_path/'private.json'; outside.write_text('{}')
    (root/'link.json').symlink_to(outside)
    for value in ['../private.json', str(outside), 'link.json', 'missing.json']:
        with pytest.raises(ValueError): safe_file(root, value)
    assert safe_file(root, 'ok.json') == root/'ok.json'


def test_authenticated_bundle_range_and_source_revocation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app, HEADERS
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login = client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS).json()
        owner = login['principal']['id']; ws = app.state.workspaces['demo']
        root = owner_directory(ws['state'], owner); root.mkdir(parents=True)
        (root/'data.json').write_text('{"clips":[]}')
        (root/'camera.mp4').write_bytes(b'a'*1000)
        ws['monitoring'].register_source(owner, {'id':'shop-recording','video_id':'shop-recording',
            'dataset':'shop', 'start':0, 'end':20, 'sha256':'a'*64,
            'media_url':'/control-room/camera.mp4'})
        manifest = {'schema_version':1,'owner_principal_id':owner,
            'privacy':{'face_redaction':'source_anonymized'},
            'source_ids':['shop-recording'],'files':['data.json','camera.mp4']}
        (root/'manifest.json').write_text(json.dumps(manifest))
        assert client.get('/api/control-room').json()['ready'] is True
        r = client.get('/control-room/camera.mp4',headers={'Range':'bytes=10-19'})
        assert r.status_code == 206 and len(r.content) == 10
        assert 'no-store' in r.headers['cache-control']
        assert client.get('/control-room/manifest.json').status_code == 404
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='other')},headers=HEADERS)
        assert client.get('/api/control-room').json()['ready'] is False
        assert client.get('/control-room/data.json').status_code == 404
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id=owner)},headers=HEADERS)
        ws['monitoring'].source_lookup = lambda *args: None
        assert client.get('/api/control-room').json()['ready'] is False
        assert client.get('/control-room/camera.mp4').status_code == 404


def test_bundle_requires_recorded_privacy_state(tmp_path):
    from control_room_routes import read_bundle
    class Monitoring:
        source_lookup = staticmethod(lambda *args: {'id':'source'})
    owner='peter'; root=owner_directory(tmp_path,owner);root.mkdir(parents=True)
    manifest={'schema_version':1,'owner_principal_id':owner,'source_ids':['source'],
        'files':['data.json'],'privacy':{'face_redaction':'pending'}}
    (root/'manifest.json').write_text(json.dumps(manifest))
    assert read_bundle({'state':tmp_path,'monitoring':Monitoring()},owner) is None


def test_generated_illustration_allowlist_keeps_camera_images_private(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import control_room_routes
    from test_skill_runtime import make_app, HEADERS
    code=tmp_path/'code';images=code/'location-images';images.mkdir(parents=True)
    (images/'store-exterior.png').write_bytes(b'generic-generated-illustration')
    (images/'store-exterior.provenance.json').write_text('{"kind":"generated_demo"}')
    (images/'camera-frame.png').write_bytes(b'camera-private')
    (images/'store-exterior-secret.json').write_text('{"private":true}')
    monkeypatch.setattr(control_room_routes,'CODE',code)
    (tmp_path/'app').mkdir()
    app=make_app(tmp_path/'app',monkeypatch)
    with TestClient(app) as client:
        assert client.get('/control-room/location-images/store-exterior.png').status_code==401
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        response=client.get('/control-room/location-images/store-exterior.png')
        assert response.status_code==200 and response.content==b'generic-generated-illustration'
        assert 'private' in response.headers['cache-control']
        assert client.get('/control-room/location-images/store-exterior.provenance.json').json()['kind']=='generated_demo'
        assert client.get('/control-room/location-images/camera-frame.png').status_code==404
        assert client.get('/control-room/location-images/store-exterior-secret.json').status_code==404


def test_requests_inside_one_interval_share_a_bundle_read(monkeypatch, tmp_path):
    import control_room_routes
    from types import SimpleNamespace
    reads = []
    answers = iter([('root', {'files': ['data.json']}), ('root', {'files': ['data.json']}), None])
    monkeypatch.setattr(control_room_routes, 'read_bundle', lambda selected, owner: (reads.append(owner), next(answers))[1])
    clock = {'now': 50.0}
    monkeypatch.setattr(control_room_routes.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(control_room_routes, '_RECENT_BUNDLES', {})
    store = tmp_path / 'monitoring.sqlite'; store.write_bytes(b'one')
    selected = {'state': tmp_path, 'monitoring': SimpleNamespace(source_lookup=lambda *a: {'id': 'source'})}
    assert control_room_routes.recent_bundle(selected, 'peter') and control_room_routes.recent_bundle(selected, 'peter')
    assert reads == ['peter']  # a burst of files: one read
    store.write_bytes(b'a write to the company store')
    assert control_room_routes.recent_bundle(selected, 'peter') and reads == ['peter'] * 2  # any write: a fresh read at once
    clock['now'] += 0.25
    assert control_room_routes.recent_bundle(selected, 'peter') is None and reads == ['peter'] * 3  # and at least every interval


def test_revision_follows_the_published_page_for_its_owner(tmp_path, monkeypatch):
    """What an open page polls: the revision moves when the page data is replaced, an unchanged page
    answers 304, the route needs the session and each owner reads their own."""
    import hashlib
    import os
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app, HEADERS
    import tracker_runner
    app = make_app(tmp_path, monkeypatch)
    def publish(root, value):
        (root / 'data.json.tmp').write_text(json.dumps(value))
        os.replace(root / 'data.json.tmp', root / 'data.json')
        return hashlib.sha256((root / 'data.json').read_bytes()).hexdigest()
    def bundle(ws, owner, source):
        root = owner_directory(ws['state'], owner); root.mkdir(parents=True)
        ws['monitoring'].register_source(owner, {'id': source, 'video_id': source, 'dataset': 'shop', 'start': 0, 'end': 20,
            'sha256': 'a' * 64, 'media_url': '/control-room/camera.mp4'})
        (root / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'owner_principal_id': owner,
            'privacy': {'face_redaction': 'source_anonymized'}, 'location': {'id': 'location-1', 'name': 'Shop'},
            'source_ids': [source], 'files': ['data.json']}))
        return root
    with TestClient(app) as client:
        assert client.get('/api/control-room/revision').status_code == 401, 'the route needs the session'
        login = client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS).json()
        owner = login['principal']['id']; ws = app.state.workspaces['demo']
        assert client.get('/api/control-room/revision').status_code == 404, 'a page exists once the host has published it'
        root = bundle(ws, owner, 'shop-recording')
        digest = publish(root, {'clips': [], 'core_trackers': []})
        first = client.get('/api/control-room/revision')
        assert first.status_code == 200 and first.headers['etag'] == f'"{digest}"' and 'no-cache' in first.headers['cache-control']
        assert first.json() == {'schema': 'rocoto.home-revision.v1', 'read_authority': 'legacy', 'revision': digest, 'day': None, 'outputs': [],
                                'run': None, 'schedule': None}
        same = client.get('/api/control-room/revision', headers={'If-None-Match': first.headers['etag']})
        assert same.status_code == 304 and same.content == b'' and same.headers['etag'] == first.headers['etag']
        weak = client.get('/api/control-room/revision', headers={'If-None-Match': 'W/' + first.headers['etag'] + ', "another"'})
        assert weak.status_code == 304, 'a compressing proxy returns the tag marked weak'
        assert client.get('/api/control-room/revision', headers={'If-None-Match': '"another"'}).status_code == 200
        # A pass stores its outputs first and replaces the page data last: the revision moves with the page.
        ws['location_intelligence']._put(owner, 'tracker_output', {'id': 'output-1', 'collection': 'shop', 'location_id': 'location-1',
            'tracker_id': 'tracker-1', 'tracker_key': 'visitors', 'revision': 3, 'tile': {}, 'canvas': {}})
        runs = tracker_runner.TrackerRuns(ws['state'])
        claimed = runs.claim(owner, 'shop', 'location-1', trigger='events', stamp='s1', day='2026-10-11')
        between = client.get('/api/control-room/revision', headers={'If-None-Match': first.headers['etag']})
        assert between.status_code == 200 and between.json()['revision'] == digest, 'outputs ahead of the page leave the page as it is'
        assert between.json()['run']['status'] == 'running' and between.json()['schedule']['running'] is True, 'the page reads the pass in hand'
        assert client.get('/api/control-room/revision', headers={'If-None-Match': between.headers['etag']}).status_code == 304
        changed = publish(root, {'clips': [], 'core_trackers': [{'key': 'visitors', 'value': 4}]})
        runs.finish(claimed['id'], claimed['lease_token'], status='completed', stamp='s1', day='2026-10-11', published=True)
        after = client.get('/api/control-room/revision', headers={'If-None-Match': first.headers['etag']})
        value = after.json()
        assert after.status_code == 200 and value['revision'] == changed != digest and after.headers['etag'].startswith(f'"{changed}.')
        assert value['schedule'] == {'automatic': True, 'needs_attention': False, 'running': False, 'requested': False, 'service': 'waiting'}
        assert value['outputs'] == [{'key': 'visitors', 'location_id': 'location-1', 'revision': 3}] and value['day'] == '2026-10-11'
        assert value['run']['status'] == 'completed' and value['run']['trigger'] == 'events' and value['run']['finished_at'].endswith('Z')
        assert owner not in after.text and str(tmp_path) not in after.text
        # The same bytes published again keep the revision.
        assert publish(root, {'clips': [], 'core_trackers': [{'key': 'visitors', 'value': 4}]}) == changed
        assert client.get('/api/control-room/revision', headers={'If-None-Match': after.headers['etag']}).status_code == 304
        # What the tools write reaches the page by the same poll: the schedule and the newest run, with the page data as it was.
        runs.set_policy(owner, 'shop', 'location-1', {'automatic': False}, expected_revision=1)
        paused = client.get('/api/control-room/revision', headers={'If-None-Match': after.headers['etag']})
        assert paused.status_code == 200 and paused.json()['revision'] == changed and paused.json()['schedule']['automatic'] is False
        assert paused.headers['etag'] != after.headers['etag']
        failing = runs.claim(owner, 'shop', 'location-1', trigger='manual', stamp='s2', day='2026-10-11')
        runs.finish(failing['id'], failing['lease_token'], status='failed', stamp='s2', error='A Core Tracker stopped. Its last output stays in place.')
        failed = client.get('/api/control-room/revision', headers={'If-None-Match': paused.headers['etag']}).json()
        assert failed['revision'] == changed and failed['run']['status'] == 'failed' and failed['run']['error'].startswith('A Core Tracker stopped')
        # Another owner reads their own page.
        client.post('/api/session', json={'code': app.state.access.issue('demo', principal_id='other')}, headers=HEADERS)
        assert client.get('/api/control-room/revision').status_code == 404
        theirs = publish(bundle(ws, 'other', 'other-recording'), {'clips': [], 'core_trackers': [], 'theirs': True})
        other = client.get('/api/control-room/revision').json()
        assert other['revision'] == theirs != changed and other['outputs'] == [] and other['run'] is None
        # Access to the page's sources ends: so does the revision.
        client.post('/api/session', json={'code': app.state.access.issue('demo', principal_id=owner)}, headers=HEADERS)
        assert client.get('/api/control-room/revision').status_code == 200
        ws['monitoring'].source_lookup = lambda *args: None
        assert client.get('/api/control-room/revision').status_code == 404
