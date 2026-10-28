"""Generic viewer bytes retain authentication without scanning tenant footage."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from fastapi.testclient import TestClient
from test_skill_runtime import make_app,HEADERS
from control_room_routes import owner_directory


def fixture(tmp_path,monkeypatch):
    import control_room_routes
    code=tmp_path/'code';code.mkdir()
    for name in ['index.html','app.js','style.css','person-outline-worker.js']:
        (code/name).write_text('generic application asset')
    image=code/'location-images';image.mkdir()
    (image/'store-exterior.png').write_bytes(b'generic illustration')
    (image/'private-camera.png').write_bytes(b'private image')
    vendor=code/control_room_routes.OUTLINE_VENDOR_ROOT;vendor.mkdir(parents=True)
    (vendor/'selfie_segmenter.tflite').write_bytes(b'generic CPU model')
    (vendor/'unlisted.tflite').write_bytes(b'unlisted model')
    monkeypatch.setattr(control_room_routes,'CODE',code)
    state=tmp_path/'app';state.mkdir();app=make_app(state,monkeypatch)
    return app


def test_generic_asset_requests_skip_source_snapshot_but_require_current_login(tmp_path,monkeypatch):
    app=fixture(tmp_path,monkeypatch)
    with TestClient(app) as client:
        paths=['/control-room/','/control-room/app.js','/control-room/style.css',
            '/control-room/location-images/store-exterior.png',
            '/control-room/vendor/mediapipe-0.10.22-rc.20250304/selfie_segmenter.tflite']
        for path in paths:assert client.get(path).status_code==401
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        index=app.state.workspaces['demo']['index'];original=index.snapshot;calls=[]
        def snapshot(*a,**k):calls.append('snapshot');return original(*a,**k)
        monkeypatch.setattr(index,'snapshot',snapshot)
        for path in paths:
            response=client.get(path)
            assert response.status_code==200
            assert response.headers['cache-control'].startswith('private')
            assert response.headers['x-content-type-options']=='nosniff'
        assert calls==[],f'Generic application files scanned tenant sources {len(calls)} times'
        client.cookies.clear()
        assert client.get('/control-room/app.js').status_code==401


def test_media_and_bundle_metadata_keep_current_scope_and_stream_checks(tmp_path,monkeypatch):
    import scope_host
    app=fixture(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()
        owner=login['principal']['id'];ws=app.state.workspaces['demo']
        root=owner_directory(ws['state'],owner);root.mkdir(parents=True)
        (root/'data.json').write_text('{"clips":[]}');(root/'camera.mp4').write_bytes(b'x'*180000)
        ws['monitoring'].register_source(owner,{'id':'recording','video_id':'recording','dataset':'shop',
            'start':0,'end':20,'sha256':'a'*64,'media_url':'/control-room/camera.mp4'})
        (root/'manifest.json').write_text(json.dumps({'schema_version':1,'owner_principal_id':owner,
            'privacy':{'face_redaction':'source_anonymized'},'source_ids':['recording'],
            'files':['data.json','camera.mp4']}))
        calls=[];original=scope_host.scope_current
        def current(view):calls.append('check');return original(view)
        monkeypatch.setattr(scope_host,'scope_current',current)
        response=client.get('/control-room/camera.mp4')
        assert response.status_code==200 and response.content==b'x'*180000
        assert 'no-store' in response.headers['cache-control']
        assert len(calls)>=3 # admission, response, streamed body
        monkeypatch.setattr(scope_host,'scope_current',lambda view:False)
        for path in ['/control-room/data.json','/control-room/camera.mp4',
                     '/control-room/location-images/private-camera.png',
                     '/control-room/vendor/mediapipe-0.10.22-rc.20250304/unlisted.tflite']:
            assert client.get(path).status_code==403
        assert client.get('/control-room/app.js').status_code==200


def test_large_generic_model_avoids_per_chunk_source_revalidation(tmp_path,monkeypatch):
    import control_room_routes,control_room_static,scope_host
    app=fixture(tmp_path,monkeypatch)
    asset=control_room_routes.OUTLINE_VENDOR_ROOT+'selfie_segmenter.tflite'
    (control_room_routes.CODE/asset).write_bytes(b'generic-model'*100000)
    with TestClient(app) as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        count={'snapshot':0,'source_check':0};index=app.state.workspaces['demo']['index']
        snapshot=index.snapshot;current=scope_host.scope_current
        def snapshot_spy(*a,**k):count['snapshot']+=1;return snapshot(*a,**k)
        def current_spy(view):count['source_check']+=1;return current(view)
        monkeypatch.setattr(index,'snapshot',snapshot_spy);monkeypatch.setattr(scope_host,'scope_current',current_spy)
        classifier=control_room_static.is_static_viewer_request
        monkeypatch.setattr(control_room_static,'is_static_viewer_request',lambda *args:False)
        before=client.get('/control-room/'+asset);baseline=dict(count)
        assert before.status_code==200 and baseline['snapshot']==1 and baseline['source_check']>10
        count.update(snapshot=0,source_check=0)
        monkeypatch.setattr(control_room_static,'is_static_viewer_request',classifier)
        after=client.get('/control-room/'+asset)
        assert after.status_code==200 and after.content==before.content
        assert count=={'snapshot':0,'source_check':0}
        print(json.dumps({'generic_asset_bytes':len(after.content),'before':baseline,'after':count}))
