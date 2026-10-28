"""Local MCP applies the same immutable owner and collection boundary as HTTP."""
import asyncio
import base64
import copy
import json
import os
from pathlib import Path
import sys

import numpy as np
import pytest
from mcp.server.fastmcp.exceptions import ToolError
BASE=Path(os.environ.get('ROCOTO_MCP_TEST_BASE',Path(__file__).parents[1]))
sys.path.insert(0,str(BASE/'tests'));sys.path.insert(0,str(BASE));sys.path.insert(0,str(Path(__file__).parents[1]))
import mcp_server
import retail_mcp_services
from test_source_derivatives import imported,prepared_import,FakeMedia,helper,anyio_backend
from test_imported_media import deny_decoders


class Index:
    def __init__(self,rows):
        self.clips=copy.deepcopy(rows);self.embeddings=np.ones((len(rows),2));self.report={'model':'synthetic'}
    def get_clip(self,key):return next((r for r in self.clips if r['id']==key),None)
    def status(self):return dict(videos=[dict(video_id=r['video_id'],dataset=r['dataset']) for r in self.clips],datasets=sorted({r['dataset'] for r in self.clips}))
    def search(self,q,limit,dataset=None):return [dict(id=r['id'],video_id=r['video_id'],dataset=r['dataset']) for r in self.clips if not dataset or r['dataset']==dataset][:limit]
    def inspect_clip(self,key):
        from imported_media import ImportedMedia
        row=self.get_clip(key);resolved=self.imported_media.resolve(row)
        return dict(clip=row,frames=[dict(time=row['start'],image_bytes=resolved.thumbnail.read_bytes(),basis='first_prepared_frame',width=row['prepared_width'],height=row['prepared_height'])])


async def call(server,name,args=None):
    value=await server.call_tool(name,args or {})
    if isinstance(value,tuple):value=value[0]
    return json.loads(value[0].text)


@pytest.mark.anyio
async def test_unbound_and_other_principals_cannot_see_ready_imports(imported,monkeypatch):
    index=Index([imported['row']]);monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    for kwargs in ({},{'principal_id':'foreign','dataset':'warehouse'}):
        server=mcp_server.make_server(imported['state'],**kwargs)
        assert (await call(server,'list_cameras'))['videos']==[]
        with pytest.raises(ToolError):await call(server,'inspect_clip',dict(clip_id=imported['row']['id']))


@pytest.mark.anyio
@pytest.mark.parametrize('collection',['warehouse','company'])
async def test_unbound_saved_profile_requires_visible_legacy_collection(imported,monkeypatch,collection):
    from retail_business import RetailBusinessStore as BusinessStore
    index=Index([imported['row'],legacy('demo','cafe')])
    business=BusinessStore(imported['state'],index)
    saved=business.update(collection,0,{'name':'Private operating context'})
    assert saved['profile']['name']=='Private operating context'
    monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    server=mcp_server.make_server(imported['state'])
    names = {tool.name for tool in await server.list_tools()}
    assert 'get_business_profile' not in names
    with pytest.raises(ToolError):
        await call(server, 'get_business_profile', dict(dataset=collection))



@pytest.mark.anyio
async def test_bound_owner_gets_byte_frames_and_fixed_dataset(imported,monkeypatch):
    index=Index([imported['row']]);monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    server=mcp_server.make_server(imported['state'],principal_id='owner',dataset='warehouse');deny_decoders(monkeypatch)
    assert (await call(server,'list_cameras'))['videos'][0]['video_id']==imported['row']['video_id']
    assert (await call(server,'list_tracker_events'))=={'events':[]}
    with pytest.raises(ToolError):await call(server,'get_business_profile',dict(dataset='other'))
    with pytest.raises(ToolError):await call(server,'inspect_clip',dict(clip_id=imported['row']['id']))


def legacy(key,dataset):
    return dict(id=key,video_id=key,dataset=dataset,camera=key,start=0.,end=4.,path='/private/demo.mp4')


@pytest.mark.anyio
async def test_all_reads_are_narrowed_and_visibility_refreshes_each_call(imported,monkeypatch):
    index=Index([imported['row'],legacy('same','warehouse'),legacy('outside','cafe')]);monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    server=mcp_server.make_server(imported['state'],principal_id='owner',dataset='warehouse')
    assert {r['video_id'] for r in (await call(server,'list_cameras'))['videos']}=={imported['row']['video_id'],'same'}
    with imported['store']._write() as db:db.execute("UPDATE assets SET status='cancelled'")
    assert [r['video_id'] for r in (await call(server,'list_cameras'))['videos']]==['same']
    with pytest.raises(ToolError):await call(server,'related_clips',{'clip_id':imported['row']['id']})


@pytest.mark.anyio
@pytest.mark.parametrize('tool,args',[('list_cameras',{})])
async def test_newly_published_generation_visible_on_next_call_and_refresh_failure_retains_snapshot(tmp_path,monkeypatch,tool,args):
    import threading
    index=Index([legacy('first','warehouse')]);pending=[];refresh_calls=[];failure=[]
    caller_thread=threading.get_ident()
    def refresh():
        refresh_calls.append(threading.get_ident())
        if failure:raise failure.pop()
        if pending:
            index.clips=copy.deepcopy(pending.pop());index.embeddings=np.ones((len(index.clips),2))
    index.refresh=refresh
    monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    server=mcp_server.make_server(tmp_path,principal_id='owner',dataset='warehouse')
    async def ids():
        result=await call(server,tool,args)
        rows=result['videos']
        return [row['video_id'] for row in rows]
    assert await ids()==['first']
    pending.append([legacy('first','warehouse'),legacy('new','warehouse'),legacy('outside','cafe')])
    assert await ids()==['first','new']
    for error in (OSError('synthetic read interruption'),ValueError('synthetic incomplete generation')):
        failure.append(error)
        assert await ids()==['first','new']
    assert len(refresh_calls)==4
    if tool=='list_intervals':assert all(thread!=caller_thread for thread in refresh_calls)


MUTATIONS=[
 ('update_business_profile',dict(dataset='warehouse',expected_revision=1,profile={})),
 ('plan_investigation',dict(dataset='warehouse',question='x',business_context='x',assumptions=[],categories=[])),
 ('retrieve_investigation',dict(dataset='warehouse',plan_id='plan-x')),
 ('propose_camera_context',dict(dataset='warehouse',camera_id='x',expected_revision=1,anchor_clip_id='x',valid_from_seconds=0.)),
 ('propose_agent',dict(dataset='warehouse',title='Test',instruction='Review')),
 ('focus_clip',dict(clip_id='x',bbox=[0.,0.,1.,1.],anchor_seconds=0.))]


@pytest.mark.anyio
@pytest.mark.parametrize('name,args',MUTATIONS)
async def test_unbound_operator_reads_keep_mutations_host_authorized(tmp_path,monkeypatch,name,args):
    index=Index([legacy('x','warehouse')]);monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    server=mcp_server.make_server(tmp_path)
    assert (await call(server,'list_cameras'))['videos'][0]['video_id']=='x'
    with pytest.raises(ToolError):await call(server,name,args)
    assert not (tmp_path/'business.sqlite').exists()


@pytest.mark.anyio
@pytest.mark.parametrize('name,args',[(n,a) for n,a in MUTATIONS if 'dataset' in a]+[
 ('get_business_context',dict(dataset='warehouse')),('get_business_profile',dict(dataset='warehouse')),
 ('list_intervals',dict(dataset='warehouse')),('list_camera_context',dict(dataset='warehouse')),
 ('discover_patterns',dict(query='x',dataset='warehouse'))])
async def test_model_dataset_cannot_override_host_scope(tmp_path,monkeypatch,name,args):
    index=Index([legacy('x','warehouse'),legacy('y','cafe')]);monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    server=mcp_server.make_server(tmp_path,principal_id='owner',dataset='cafe')
    with pytest.raises(ToolError):await call(server,name,args)


@pytest.mark.anyio
async def test_saved_plan_namespace_is_owned_across_mcp_restarts(tmp_path,monkeypatch):
    from retired_tools import RETIRED_TOOLS
    index=Index([legacy('x','cafe')]);monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    for principal in ['alice','bob','alice']:
        server=mcp_server.make_server(tmp_path,principal_id=principal,dataset='cafe')
        assert not RETIRED_TOOLS.intersection(t.name for t in await server.list_tools())
        for name in RETIRED_TOOLS:
            with pytest.raises(ToolError):await call(server,name,{})


@pytest.mark.anyio
async def test_cosmos_receives_owned_collection_snapshot(imported,monkeypatch):
    import cosmos_inspect
    index=Index([imported['row'],legacy('outside','cafe')]);monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    seen=[]
    async def inspect(*args,**kwargs):seen.append(True);raise AssertionError('Retired provider dispatched')
    monkeypatch.setattr(cosmos_inspect,'inspect_live',inspect)
    server=mcp_server.make_server(imported['state'],principal_id='owner',dataset='warehouse')
    with pytest.raises(ToolError):await call(server,'cosmos_inspect',dict(clip_ids=[imported['row']['id']],question='Check'))
    assert seen==[]


@pytest.mark.anyio
async def test_commissioned_mcp_frames_and_export_use_saved_host_owner(imported,monkeypatch):
    import source_media
    # The opaque fixture media fits a tiny export; retain the real disk guard.
    monkeypatch.setattr('media_exports.MAX_OUTPUT_BYTES',8*1024**2)
    broker=FakeMedia();monkeypatch.setattr(source_media,'MediaClient',lambda:broker)
    monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:Index([imported['row']]))
    server=mcp_server.make_server(imported['state'],principal_id='owner',dataset='warehouse',derivatives_enabled=True)
    deny_decoders(monkeypatch)
    with pytest.raises(ToolError):await server.call_tool('inspect_clip',{'clip_id':imported['row']['id']})
    result=await call(server,'prepare_export',{'document':{'title':'Delivery','items':[{'clip_id':imported['row']['id']}]}})
    assert result['status']=='ready' and broker.calls==1 and not broker.alive
    other=mcp_server.make_server(imported['state'],principal_id='foreign',dataset='warehouse')
    with pytest.raises(ToolError):await call(other,'get_export',{'payload_id':result['id']})


@pytest.mark.anyio
async def test_sdk_initialize_list_call_and_error_preserve_host_scope(imported,monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session
    monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:Index([imported['row']]))
    server=mcp_server.make_server(imported['state'],principal_id='owner',dataset='warehouse')
    async with create_connected_server_and_client_session(server) as session:
        listing=await session.list_tools()
        assert {'list_tracker_events','list_monitoring_profiles','get_tracker_event','prepare_export'}<={tool.name for tool in listing.tools}
        # The tracker reads are listed beside them; a write stays a tool of the hosted conversation.
        import monitoring_tools
        assert {'list_trackers','get_tracker','list_tracker_runs','get_harness'}<={tool.name for tool in listing.tools}
        # The harness describes itself on the local command too, one section at a time.
        described=await session.call_tool('get_harness',{'section':'conversations'})
        assert not described.isError and [chat['purpose'] for chat in json.loads(described.content[0].text)['fixed_conversations']]==['organization','cameras','messaging']
        assert (await session.call_tool('get_harness',{'section':'everything'})).isError
        assert not monitoring_tools.TRACKERS.WRITE_NAMES&{tool.name for tool in listing.tools}
        trackers=await session.call_tool('list_trackers',{})
        assert not trackers.isError and json.loads(trackers.content[0].text)=={'trackers':[]}
        runs=await session.call_tool('list_tracker_runs',{'location_id':'location-1'})
        assert not runs.isError and json.loads(runs.content[0].text)['runs']==[]
        assert (await session.call_tool('get_tracker',{'tracker_id':'tracker-of-another-owner'})).isError
        assert (await session.call_tool('list_tracker_runs',{'location_id':'location-1','limit':5000})).isError
        for tool in listing.tools:
            assert 'principal_id' not in tool.inputSchema.get('properties',{})
            assert 'derivatives_enabled' not in tool.inputSchema.get('properties',{})
        result=await session.call_tool('list_cameras',{})
        assert not result.isError and json.loads(result.content[0].text)['videos'][0]['video_id']==imported['row']['video_id']
        denied=await session.call_tool('search_video',{'query':'delivery','dataset':'other'})
        assert denied.isError


def test_tool_only_startup_preserves_active_native_and_credential_requests(tmp_path,monkeypatch):
    from native_questions import NativeQuestions
    from test_native_questions import opened
    from test_credential_requests import prepared,request
    questions=NativeQuestions(tmp_path);pending=opened(questions)
    credentials,connector=prepared(tmp_path);secret_pending=request(credentials,connector)
    index=Index([legacy('x','warehouse')]);monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:index)
    # A tool process should not even parse unrelated browser-runtime state.
    chatdir=tmp_path/'chats';chatdir.mkdir();(chatdir/'saved.json').write_text('private state left unopened')
    registry=tmp_path/'agent-chat-scopes.json';registry.write_text('private registry left unopened')
    before={path:path.read_bytes() for path in [chatdir/'saved.json',registry]}
    mcp_server.make_server(tmp_path,principal_id='alice',dataset='warehouse')
    assert questions.get(pending['id'],owner='alice',root_chat_id='root')['status']=='pending'
    assert credentials.credential_request(secret_pending['id'],owner='alice',chat_id='chat')['status']=='pending'
    assert all(path.read_bytes()==value for path,value in before.items())


@pytest.mark.parametrize('kwargs',[{'principal_id':'owner'},{'dataset':'warehouse'},
    {'principal_id':'../foreign','dataset':'warehouse'},{'derivatives_enabled':True}])
def test_invalid_host_binding_fails_before_loading_index(tmp_path,monkeypatch,kwargs):
    def forbidden(*args):raise AssertionError('index loaded before binding validation')
    monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',forbidden)
    with pytest.raises(ValueError):mcp_server.make_server(tmp_path,**kwargs)

@pytest.mark.anyio
async def test_local_observations_work_without_embedding_index_and_remain_owner_scoped(tmp_path,monkeypatch):
    from monitoring import MonitoringStore
    store=MonitoringStore(tmp_path)
    store.register_source('alice',{'id':'recording','video_id':'recording','dataset':'retail','end':10,'sha256':'a'*64,'media_url':'/api/media/recording'})
    profile=store.save_profile('alice',{'name':'Interests','dataset':'retail','vocabulary':[{'type':'touch','description':'Visible contact'}]},datasets=['retail'])
    saved=store.commit_event('alice',{'profile_id':profile['id'],'profile_revision':1,'event_key':'e1','type':'touch','description':'Touches the carton','source':{'clip_id':'recording','start_s':1,'end_s':2},'track_id':'P0'})
    monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:Index([]))
    server=mcp_server.make_server(tmp_path,principal_id='alice',dataset='retail')
    rows=(await call(server,'list_tracker_events'))['events']
    assert len(rows)==1 and rows[0]['id']==saved['id'] and rows[0]['track_id']=='P0'
    assert (await call(server,'get_tracker_event',{'event_id':saved['id']}))['source']['start_s']==1
    other=mcp_server.make_server(tmp_path,principal_id='bob',dataset='retail')
    assert (await call(other,'list_tracker_events'))['events']==[]
    with pytest.raises(ToolError):await call(other,'get_tracker_event',{'event_id':saved['id']})
