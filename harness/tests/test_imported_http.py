"""Actual application routes preserve the imported-media descriptor boundary."""
import asyncio
import base64
import json

from fastapi.testclient import TestClient

from test_imported_media import imported, deny_decoders
from test_server_access import FixtureAgent, FixtureIndex
import server


def test_application_serves_imports_without_host_decode_or_path_reopen(imported, monkeypatch):
    index = FixtureIndex()
    index.clips = [imported['row']]
    monkeypatch.setattr(server, 'HarnessAgent', FixtureAgent)
    monkeypatch.setattr(server.shutil, 'which', lambda name: None)
    app = server.create_app(imported['state'], index)
    with imported['store']._write() as db:
        imported['store']._queue_job(db, imported['row']['asset_id'], 'publish')
        db.execute("UPDATE jobs SET status='succeeded',result=? WHERE asset_id=? AND stage='publish'",
            (json.dumps({'generation_id':'fixture-generation','qdrant_collection':'fixture-collection',
                'qdrant_fingerprint':'a'*64}),imported['row']['asset_id']))
    deny_decoders(monkeypatch)
    with TestClient(app) as client:
        code=app.state.access.issue('demo',principal_id=imported['actor'].principal_id)
        assert client.post('/api/session',json={'code':code},headers={'X-Video-Demo':'1'}).status_code==200
        clip = imported['row']['id']
        original = client.get('/originals/' + imported['row']['video_id'])
        assert original.status_code == 200
        assert original.content == imported['original']
        assert 'Loading%20bay.mov' in original.headers['content-disposition']
        response = client.get('/clips/' + clip + '.mp4', headers={'Range': 'bytes=2-7'})
        assert response.status_code == 206 and response.content == imported['prepared'][2:8]
        assert client.get('/thumbnails/' + clip + '.jpg').content == imported['thumbnail']
        manifest = client.get('/api/clip/' + clip + '/manifest').json()
        assert manifest['source_filename'] == 'Loading bay.mov'
        assert manifest['export']['kind'] == 'prepared preview'
        assert manifest['original_download_url'].endswith(imported['row']['video_id'])
        assert client.get('/downloads/' + clip + '.mp4').status_code == 503
        # Even a corrupted host index cannot make the HTTP route reopen an
        # unvalidated filename or bypass the authoritative preparation receipt.
        private = imported['state'] / 'private.txt'
        private.write_bytes(b'private bytes')
        index.clips[0] = {**index.clips[0], 'thumbnail_path': str(private)}
        assert client.get('/thumbnails/' + clip + '.jpg').status_code == 404


def test_native_frame_tool_consumes_verified_bytes_and_reports_frame_basis(imported, monkeypatch):
    from agent_bridge import CodexVideoAgent
    from imported_media import ImportedMedia
    from retrieval import RetrievalIndex
    index = RetrievalIndex.__new__(RetrievalIndex)
    index.clips = [imported['row']]
    index.imported_media = ImportedMedia(imported['state'])
    agent = CodexVideoAgent(imported['state'], index, lambda: [])
    deny_decoders(monkeypatch)
    content, _, success = asyncio.run(agent._tool('inspect_clip', {'clip_id': imported['row']['id']}))
    assert success
    images = [item for item in content if item['type'] == 'inputImage']
    assert len(images) == 1
    assert base64.b64decode(images[0]['imageUrl'].split(',', 1)[1]) == imported['thumbnail']
    metadata = json.loads(content[0]['text'])
    assert metadata['frame_basis'] == [{'basis': 'first_prepared_frame', 'width': 640, 'height': 360}]
