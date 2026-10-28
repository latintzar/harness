"""Camera context stays draft until confirmed and is bound to source/time evidence."""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from camera_context import CameraStore
from agent_bridge import CodexVideoAgent, tool_specs
import server
from test_library import fixture_app


class Index:
    def __init__(self, root):
        self.clips=[]
        for asset in ['first','second']:
            path=root/(asset+'.mp4');path.write_bytes(asset.encode())
            for start in [0,8]:
                self.clips.append(dict(id=f'{asset}-{start}',video_id=asset,dataset='cafe',camera='Same label',
                    path=str(path),thumbnail_path=str(path),start=start,end=start+8,sample_time=start+4))
    def get_clip(self,key): return next((r for r in self.clips if r['id']==key),None)


def proposal(revision=1,clip='first-0',start=0,**fields):
    return dict(expected_revision=revision,anchor_clip_id=clip,valid_from_seconds=start,
                room='Room A',zones=[{'name':'Counter','description':'Operator-provided fixture note','bbox':[0,0,.5,.5]}],
                layout_notes='Fixture context',**fields)


def test_camera_identity_is_not_inferred_from_matching_labels_and_drafts_stay_inactive(tmp_path):
    index=Index(tmp_path);store=CameraStore(tmp_path/'state',index)
    cameras=store.list('cafe')['cameras']
    assert len(cameras)==2 and cameras[0]['id']!=cameras[1]['id']
    camera=next(c for c in cameras if c['asset_ids']==['first'])
    assert camera['confirmed_context'] is None
    draft=store.propose(camera['id'],proposal(),origin='operator_note')
    assert store.get_context_for_clip(index.get_clip('first-0')) is None
    with pytest.raises(ValueError,match='revision'):
        store.confirm(camera['id'],draft['draft']['id'],expected_revision=1,operator='Peter')
    confirmed=store.confirm(camera['id'],draft['draft']['id'],expected_revision=draft['revision'],operator='Peter')
    assert confirmed['view_revision']==1
    restored=CameraStore(tmp_path/'state',index)
    context=restored.get_context_for_clip(index.get_clip('first-0'))
    assert context['context']['room']=='Room A'
    assert context['provenance']['proposal_origin']=='operator_note'
    assert context['provenance']['source_sha256']
    assert restored.get_context_for_clip(index.get_clip('second-0')) is None


def test_moved_camera_context_applies_only_to_later_source_and_hash(tmp_path):
    index=Index(tmp_path);store=CameraStore(tmp_path/'state',index)
    camera=next(c for c in store.list('cafe')['cameras'] if c['asset_ids']==['first'])
    one=store.propose(camera['id'],proposal(),origin='operator_note')
    one=store.confirm(camera['id'],one['draft']['id'],expected_revision=one['revision'],operator='Peter')
    two=store.propose(camera['id'],proposal(one['revision'],'first-8',8,change_type='camera_moved'),origin='operator_note')
    two=store.confirm(camera['id'],two['draft']['id'],expected_revision=two['revision'],operator='Peter')
    assert store.get_context_for_clip(index.get_clip('first-0'))['view_revision']==1
    assert store.get_context_for_clip(index.get_clip('first-8'))['view_revision']==2
    assert len(two['history'])==2
    assert two['history'][0]['provenance']['valid_from_seconds']==0
    index.clips.append({**index.get_clip('first-0'),'id':'crossing','start':6,'end':10})
    assert store.get_context_for_clip(index.get_clip('crossing')) is None
    (tmp_path/'first.mp4').write_bytes(b'changed underlying recording')
    assert store.get_context_for_clip(index.get_clip('first-8')) is None


def test_context_rejects_wrong_source_invalid_geometry_and_fabricated_observation(tmp_path):
    index=Index(tmp_path);store=CameraStore(tmp_path/'state',index)
    camera=next(c for c in store.list('cafe')['cameras'] if c['asset_ids']==['first'])
    for data in [proposal(clip='second-0'), proposal(start=9),proposal(observation_ref='invented')]:
        with pytest.raises(ValueError):store.propose(camera['id'],data,origin='agent_proposal')
    data=proposal();data['zones'][0]['bbox']=[.8,0,.5,.5]
    with pytest.raises(ValueError):store.propose(camera['id'],data,origin='operator_note')
    with pytest.raises(ValueError):store.list('another-company-collection')


def test_camera_api_mutations_require_query_access_and_keep_tenant_isolation(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        client.post('/api/session/demo',headers={'X-Video-Demo':'1'})
        camera=client.get('/api/cameras?collection=cafe').json()['cameras'][0]
        body=proposal(clip=camera['reference_clip']['id'])
        assert client.post(f"/api/cameras/{camera['id']}/context",json=body,headers={'X-Video-Demo':'1'}).status_code==403
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers={'X-Video-Demo':'1'})
        draft=client.post(f"/api/cameras/{camera['id']}/context",json=body,headers={'X-Video-Demo':'1'})
        assert draft.status_code==200
        app.state.access.create_environment('company-b','Company B')
        client.post('/api/session',json={'code':app.state.access.issue('company-b')},headers={'X-Video-Demo':'1'})
        assert client.get(f"/api/cameras/{camera['id']}").status_code==404
        assert client.post(f"/api/cameras/{camera['id']}/confirm",json={'expected_revision':draft.json()['revision'],'draft_id':draft.json()['draft']['id']},headers={'X-Video-Demo':'1'}).status_code==404


def test_agent_can_only_propose_scoped_inactive_camera_context(tmp_path):
    index=Index(tmp_path);state=tmp_path/'state';state.mkdir()
    agent=CodexVideoAgent(state,index,lambda:[])

    async def run():
        content,_,ok=await agent._tool('list_camera_context',{},'cafe')
        assert ok
        camera=next(c for c in json.loads(content[0]['text'])['cameras'] if c['asset_ids']==['first'])
        body={'camera_id':camera['id'],**proposal()}
        content,_,ok=await agent._tool('propose_camera_context',body,'cafe')
        assert ok
        data=json.loads(content[0]['text'])
        assert data['draft']['provenance']['proposal_origin']=='agent_proposal'
        assert data['confirmed_context'] is None
        _,_,ok=await agent._tool('list_camera_context',{'dataset':'another-collection'},'cafe')
        assert not ok
        _,_,ok=await agent._tool('propose_camera_context',{**body,'expected_revision':data['revision']},'warehouse')
        assert not ok
        assert agent.camera_store.get_context_for_clip(index.get_clip('first-0')) is None

    asyncio.run(run())
    assert 'confirm_camera_context' not in {tool['name'] for tool in tool_specs()}
    assert 'propose_camera_context' not in {tool['name'] for tool in tool_specs(children=False)}


def test_clip_details_show_only_confirmed_source_valid_camera_context(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    headers = {'X-Video-Demo': '1'}
    with TestClient(app) as client:
        client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=headers)
        camera = client.get('/api/cameras?collection=cafe').json()['cameras'][0]
        clip_id = camera['reference_clip']['id']
        draft = client.post(f"/api/cameras/{camera['id']}/context", json=proposal(clip=clip_id), headers=headers).json()
        assert client.get(f'/api/clip/{clip_id}').json()['room']['status'] == 'unassigned'
        response = client.post(f"/api/cameras/{camera['id']}/confirm", json={
            'expected_revision': draft['revision'], 'draft_id': draft['draft']['id']}, headers=headers)
        assert response.status_code == 200
        details = client.get(f'/api/clip/{clip_id}').json()
        assert details['room']['name'] == 'Room A'
        assert details['room']['status'] == 'confirmed'
        assert details['camera_context']['provenance']['source_sha256']
        Path(app.state.index.get_clip(clip_id)['path']).write_bytes(b'replaced source')
        assert client.get(f'/api/clip/{clip_id}').json()['room']['status'] == 'unassigned'
