"""Imported evidence reaches consumers as verified bytes, never host decodes."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from test_imported_media import imported, deny_decoders
from imported_media import ImportedMedia
from media import EvidenceMedia
import cosmos_inspect


class Deriver:
    enabled = True

    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    async def derive(self, record, purpose, *, owner_principal_id, frame_seconds=()):
        assert owner_principal_id == 'owner'
        assert record == self.fixture['row']
        self.calls.append((purpose, tuple(frame_seconds)))
        resolved = ImportedMedia(self.fixture['state']).resolve(record)
        if purpose == 'inspect_frames_v1':
            files = [resolved.thumbnail] * len(frame_seconds)
            metadata = [dict(source_elapsed_seconds=offset-.01, original_pts_seconds=12+offset-.01,
                source_frame_index=i, width=640, height=360) for i, offset in enumerate(frame_seconds)]
        else:
            files = [resolved.prepared]
            metadata = [dict(width=640, height=360, duration_seconds=4., average_frame_rate='4/1',
                frame_count=16, mime_type='video/mp4', video_codec='h264', audio_codecs=[],
                actual_source_first_frame_seconds=0., mapping_basis='preserved_source_frame_pts')]
        return SimpleNamespace(receipt_id='derivative-'+'a'*32, files=files,
            manifest=dict(clip_id=record['id'], asset_id=record['video_id'], collection=record['dataset'],
                request=dict(source_elapsed_start_seconds=record['start'],source_elapsed_end_seconds=record['end']),
                files=metadata))


def media_for(fixture):
    result = EvidenceMedia(fixture['state'])
    result.derivatives = Deriver(fixture)
    return result


def test_imported_cut_and_frames_use_sandbox_receipts(imported, monkeypatch):
    media = media_for(imported)
    deny_decoders(monkeypatch)
    cut = asyncio.run(media.clip(imported['row'], source_resolution=True, owner_principal_id='owner'))
    assert cut.read_bytes() == imported['prepared']
    frames = asyncio.run(media.inspect(imported['row'], owner_principal_id='owner'))
    assert len(frames['frames']) == 3
    assert [item['time'] for item in frames['frames']] == [.39, 1.99, 3.59]
    assert all(item['basis'] == 'source_frame_pts' and item['image_bytes'] == imported['thumbnail']
               for item in frames['frames'])
    assert media.derivatives.calls == [('source_cut_v1', ()), ('inspect_frames_v1', (.4,2.,3.6))]


def test_imported_cut_requires_owner_even_with_enabled_media(imported, monkeypatch):
    media = media_for(imported)
    deny_decoders(monkeypatch)
    with pytest.raises(ValueError, match='owner'):
        asyncio.run(media.clip(imported['row'], source_resolution=True))
    assert media.derivatives.calls == []


def test_cosmos_manifest_uses_private_receipt_instead_of_host_probe(imported, monkeypatch):
    deriver = Deriver(imported)
    receipt = asyncio.run(deriver.derive(imported['row'],'detail_video_v1',owner_principal_id='owner'))
    import source_derivatives
    calls = []
    def read(self, receipt_id, record=None, *, owner_principal_id, purpose):
        calls.append((receipt_id, owner_principal_id, purpose))
        return receipt
    monkeypatch.setattr(source_derivatives.SourceDerivatives, 'read_receipt', read)
    row=imported['row']
    entry=dict(id=row['id'], derivative_receipt=receipt.receipt_id, owner_principal_id='owner',
        dataset=row['dataset'], source_video_id=row['video_id'], start=row['start'],end=row['end'],camera=row['camera'],
        path='/etc/this-path-must-never-be-read')
    path=imported['state']/'manifest.json'
    document=dict(clips=[entry],requests=[dict(clip_ids=[row['id']],question='Describe the movement.')])
    path.write_text(json.dumps(document))
    deny_decoders(monkeypatch)
    clips,_=cosmos_inspect.load_manifest(path,company_state=imported['state'],quality_profile=cosmos_inspect.LIVE_SUPER_PROFILE)
    assert clips[0]['content']==imported['prepared']
    assert clips[0]['submitted_num_frames']==16
    assert clips[0]['source_timing']['clock']=='source_relative'
    assert clips[0]['source_timing']['actual_source_first_frame_seconds']==0.
    assert calls==[(receipt.receipt_id,'owner','detail_video_v1')]
    for field,value in [('dataset','other'),('id','foreign'),('source_video_id','other'),('start',1.)]:
        changed=copy.deepcopy(document);changed['clips'][0][field]=value
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError,match='receipt'):
            cosmos_inspect.load_manifest(path,company_state=imported['state'],quality_profile=cosmos_inspect.LIVE_SUPER_PROFILE)
    with pytest.raises(ValueError,match='company'):
        cosmos_inspect.load_manifest(path,quality_profile=cosmos_inspect.LIVE_SUPER_PROFILE)


def publish(fixture):
    with fixture['store']._write() as db:
        fixture['store']._queue_job(db,fixture['row']['asset_id'],'publish')
        db.execute("UPDATE jobs SET status='succeeded',result=? WHERE asset_id=? AND stage='publish'",
            (json.dumps(dict(generation_id='fixture',qdrant_collection='fixture',qdrant_fingerprint='a'*64)),
             fixture['row']['asset_id']))


def test_native_frame_tool_uses_actual_derivative_receipts(imported,monkeypatch):
    from test_source_derivatives import FakeMedia, helper
    from agent_bridge import CodexVideoAgent
    from retrieval import RetrievalIndex
    publish(imported)
    index=RetrievalIndex.__new__(RetrievalIndex)
    index.clips=[imported['row']]
    index.source_derivatives=helper(imported,FakeMedia())
    agent=CodexVideoAgent(imported['state'],index,lambda:[])
    agent.children['chat']={'owner_principal_id':'owner'}
    deny_decoders(monkeypatch)
    content,_,success=asyncio.run(agent._tool('inspect_clip',{'clip_id':imported['row']['id']},chat_id='chat'))
    assert success
    assert len([item for item in content if item['type']=='inputImage'])==3
    metadata=json.loads(content[0]['text'])
    assert metadata['sampled_frame_times']==[.4,2.,3.6]
    assert all(frame['basis']=='source_frame_pts' for frame in metadata['frame_basis'])
    assert '/source-intake/' not in str(metadata)


def test_http_download_serves_verified_source_cut_with_owner_scope(imported,monkeypatch):
    from fastapi.testclient import TestClient
    from test_source_derivatives import FakeMedia
    from test_server_access import FixtureAgent, FixtureIndex
    import server
    import source_media
    publish(imported)
    monkeypatch.setenv('ROCOTO_SOURCE_DERIVATIVES_ENABLED','1')
    monkeypatch.setattr(source_media,'MediaClient',FakeMedia)
    monkeypatch.setattr(server,'CodexVideoAgent',FixtureAgent)
    monkeypatch.setattr(server.shutil,'which',lambda _:None)
    index=FixtureIndex();index.clips=[imported['row']]
    app=server.create_app(imported['state'],index)
    deny_decoders(monkeypatch)
    with TestClient(app) as client:
        code=app.state.access.issue('demo',principal_id='owner')
        assert client.post('/api/session',json={'code':code},headers={'X-Video-Demo':'1'}).status_code==200
        clip=imported['row']['id']
        result=client.get('/downloads/'+clip+'.mp4')
        assert result.status_code==200,result.text
        assert result.content==b'opaque video content'
        assert 'attachment' in result.headers['content-disposition']
        manifest=client.get('/api/clip/'+clip+'/manifest').json()
        assert manifest['export']['kind']=='bounded source cut'
        assert manifest['download_url']=='/downloads/'+clip+'.mp4'
