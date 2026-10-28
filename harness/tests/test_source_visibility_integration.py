"""Real HTTP/agent/scheduler boundaries use a coherent principal-owned source view."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

HERE=Path(__file__).parents[1]
BASE=Path(os.environ.get('ROCOTO_SOURCE_TEST_BASE',HERE))
WORKER=Path(os.environ.get('ROCOTO_WORKER_TEST_BASE',BASE))
sys.path.insert(0,str(BASE));sys.path.insert(0,str(WORKER));sys.path.insert(0,str(HERE))
import server
from agent_bridge import CodexVideoAgent
from background_agents import AgentScheduler,AgentStore
from execution_policy import RETRIEVAL_VIEW
from qdrant_retrieval import QdrantRetrievalIndex
from source_intake import IntakeActor
from source_visibility import ReadyIndex,chat_source_view

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module
fixture=load('visibility_worker_fixture',WORKER/'tests/test_source_worker.py')
web=load('visibility_web_fixture',BASE/'tests/test_server_access.py')

@pytest.fixture
def anyio_backend():return 'asyncio'


async def prepared(tmp_path):
    instance,store,media,embedding,api=fixture.setup(tmp_path)
    submit=media.submit
    async def enriched(*args,**kwargs):
        handle=await submit(*args,**kwargs);result,files=media.results[handle]
        result['manifest']['source'].update(coded_width=640,coded_height=360,rotation_degrees=0,
            variable_frame_intervals=False,sample_aspect_ratio='1:1')
        data=json.dumps(result['manifest']).encode()
        entry=next(item for item in result['files'] if item['path']=='manifest.json')
        result['total_bytes']+=len(data)-entry['size_bytes']
        entry.update(size_bytes=len(data),sha256=fixture.sha(data));files[entry['index']]=data
        return handle
    media.submit=enriched
    uploads={}
    for owner in ('alice','bob'):
        uploads[owner]=fixture.new_asset(store,actor=IntakeActor(owner),key=owner)
        await fixture.through_embed(instance);assert (await instance.run_once())['status']=='succeeded'
    raw=QdrantRetrievalIndex(instance.publisher.state,client=api)
    raw._encode_text=lambda text:np.eye(1,256,dtype=np.float32)[0]
    raw._encoder_verified=lambda:True
    raw.company='demo'
    return instance,store,raw,uploads


def test_http_owner_observer_browse_and_direct_media_use_same_ready_view(tmp_path,monkeypatch):
    instance,store,raw,uploads=asyncio.run(prepared(tmp_path))
    monkeypatch.setattr(server,'CodexVideoAgent',web.FixtureAgent)
    async def connection_status(self):return {'managed_authenticated':False,'route':'fixture'}
    monkeypatch.setattr(server.ManagedConnection,'status',connection_status)
    app=server.create_app(store.root.parent,raw);headers={'X-Video-Demo':'1'}
    with TestClient(app) as alice,TestClient(app) as bob,TestClient(app) as observer,TestClient(app) as browse:
        for client,owner in ((alice,'alice'),(bob,'bob'),(observer,'peter')):
            code=app.state.access.issue('demo',principal_id=owner,owner=owner=='peter')
            assert client.post('/api/session',json={'code':code},headers=headers).status_code==200
        for client,owner,other in ((alice,'alice','bob'),(bob,'bob','alice')):
            asset=uploads[owner]['asset_id'];foreign=uploads[other]['asset_id']
            status=client.get('/api/status');assert status.status_code==200,status.text
            body=status.json();assert {r['video_id'] for r in body['corpus']}=={asset}
            assert body['retrieval']['clip_count']==2 and foreign not in json.dumps(body)
            cameras=client.get('/api/cameras?collection=cafe');assert cameras.status_code==200,cameras.text
            assert {c['asset_ids'][0] for c in cameras.json()['cameras']}=={asset}
            gallery=client.get('/api/library?collection=cafe');assert gallery.status_code==200,gallery.text
            assert foreign not in gallery.text and asset in gallery.text
            assert client.get('/api/clip/'+asset+'-clip-000000').status_code==200
            assert client.get('/api/clip/'+foreign+'-clip-000000').status_code==404
            media=client.get('/clips/'+asset+'-clip-000000.mp4');assert media.status_code==200
            assert b'opaque video bytes' in media.content
            assert client.get('/clips/'+foreign+'-clip-000000.mp4').status_code==404
        assert observer.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=headers).status_code==200
        assert observer.get('/api/status').json()['corpus']==alice.get('/api/status').json()['corpus']
        assert browse.post('/api/session/demo',headers=headers).status_code==200
        assert browse.get('/api/status').json()['corpus']==[]
        assert browse.get('/api/clip/'+uploads['alice']['asset_id']+'-clip-000000').status_code==404


@pytest.mark.anyio
async def test_agent_turn_and_native_tool_dispatch_bind_saved_owner_and_keep_narrowing(tmp_path):
    instance,store,raw,uploads=await prepared(tmp_path)
    facade=ReadyIndex(raw,store.root.parent)
    from retail_host import RetailWorkspace, public_corpus
    from retail_scope import install_scope
    from organization_chats import OrganizationChats
    from chat_permissions import ChatPermissions
    agent=CodexVideoAgent(store.root.parent,facade,lambda:public_corpus(facade))
    member=lambda principal:{'id':principal,'role':'operator'} if principal in {'alice','bob'} else None
    selected={'state':store.root.parent,'index':facade,'agent':agent,'environment_id':'demo'}
    selected['organization_chats']=OrganizationChats(store.root.parent,environment_id='demo',
        member=member,records=lambda:agent.children)
    selected['chats']=ChatPermissions(store.root.parent,organization=selected['organization_chats'])
    install_scope(selected,lambda selected,principal:member(principal),lambda selected:[])
    agent.vertical=RetailWorkspace(None,selected)
    from monitoring import MonitoringStore
    agent.monitoring_store=MonitoringStore(agent.state)
    for owner in ('alice','bob'):
        agent.children[owner]={'id':owner,'owner_principal_id':owner,'messages':[],'dataset':'cafe'}
    observed={}
    entered={owner:asyncio.Event() for owner in ('alice','bob')}
    async def fake_turn(message,chat_id,*args,**kwargs):
        entered[chat_id].set();await entered['bob' if chat_id=='alice' else 'alice'].wait()
        observed[chat_id]=[r['video_id'] for r in agent.retrieval.clips]
        yield {'type':'done','status':'completed'}
    agent._turn=fake_turn
    async def consume(owner):return [event async for event in agent.turn('Describe footage',owner,'cafe')]
    await asyncio.gather(consume('alice'),consume('bob'))
    assert set(observed['alice'])=={uploads['alice']['asset_id']}
    assert set(observed['bob'])=={uploads['bob']['asset_id']}
    assert RETRIEVAL_VIEW.get() is None and not facade.clips
    # Native MCP dispatch restores RETRIEVAL_VIEW but runs in a separate ASGI task.
    view=facade.snapshot('alice');view.clips=view.clips[:1];view.embeddings=view.embeddings[:1]
    token=RETRIEVAL_VIEW.set(view)
    try:
        from scope_host import agent_scope
        async with agent_scope(agent,'alice','cafe') as admission:
            assert len(admission._resource.clips)==1
        items,_,ok=await agent._tool('list_cameras',{},'cafe','alice')
        assert ok,items
        assert {r['video_id'] for r in json.loads(items[0]['text'])}=={uploads['alice']['asset_id']}
    finally:RETRIEVAL_VIEW.reset(token)
    # Direct dispatch binds the saved owner too, even without an inherited turn.
    items,_,ok=await agent._tool('list_cameras',{},'cafe','bob')
    assert ok and {r['video_id'] for r in json.loads(items[0]['text'])}=={uploads['bob']['asset_id']}


@pytest.mark.anyio
async def test_scheduler_claim_and_execution_use_definition_owner(tmp_path):
    instance,store,raw,uploads=await prepared(tmp_path)
    facade=ReadyIndex(raw,store.root.parent)
    class Agent:
        children={};child_tasks={}
        def _save_child(self,child):self.children[child['id']]=child
        async def turn(self,message,chat_id,*args,**kwargs):
            view=RETRIEVAL_VIEW.get()
            assert view is not None
            self.seen={row['video_id'] for row in view.clips}
            yield {'type':'done','status':'completed'}
    from retail_agent_inputs import RetailInputProvider
    from monitoring import MonitoringStore
    monitoring=MonitoringStore(store.root.parent,source_lookup=lambda owner,key:facade.snapshot(owner).get_clip(key))
    for owner in ('alice','bob'):
        profile=monitoring.save_profile(owner,{'name':'Activity','dataset':'cafe',
            'vocabulary':[{'type':'activity','description':'Visible activity.'}]},datasets=['cafe'])
        for clip in facade.snapshot(owner).clips:
            monitoring.commit_event(owner,{'profile_id':profile['id'],'profile_revision':profile['revision'],
                'event_key':clip['id'],'type':'activity','description':'Source-owned activity.',
                'source':{'clip_id':clip['id'],'start_s':clip['start'],'end_s':clip['end']},'track_id':clip['id']})
    agent=Agent();definitions=AgentStore(store.root.parent,
        input_provider=RetailInputProvider(monitoring=monitoring,index=facade))
    definition=definitions.create(web.agent_definition(input_kind='tracker_events'),['cafe'],owner_principal_id='alice')
    definitions.activate(definition['id'],time.time()+100)
    scheduler=AgentScheduler(definitions,agent,slots=asyncio.Semaphore(2))
    # Constructor recovery pauses inherited running work, then explicit activation is authoritative.
    definitions.activate(definition['id'],time.time()+100)
    await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
    assert agent.seen=={uploads['alice']['asset_id']}
    run=definitions.get(definition['id'])['runs'][0]
    assert run['source_count']==2 and all(key.startswith(uploads['alice']['asset_id']) for key in run['source_ids'])
    await scheduler.close()


def test_browse_facade_creates_no_intake_database_and_restores_context(tmp_path):
    raw=web.FixtureIndex('demo',tmp_path/'sample.jpg')
    facade=ReadyIndex(raw,tmp_path/'company')
    assert facade.status()['datasets']==['cafe'] and len(facade.clips)==1
    view=facade.snapshot(None)
    with facade.bind(view):assert facade.current() is view
    assert facade.current() is not view and not (tmp_path/'company/source-intake').exists()


def test_new_company_shares_only_host_encoder_hook_before_facade_binding(tmp_path,monkeypatch):
    raw=web.FixtureIndex('demo')
    received=[]
    def factory(path):
        index=web.FixtureIndex(Path(path).parent.name)
        index.use_text_encoder=lambda source:received.append((index,source))
        return index

    monkeypatch.setattr(server,'CodexVideoAgent',web.FixtureAgent)
    async def status(self):return {'managed_authenticated':False,'route':'fixture'}
    monkeypatch.setattr(server.ManagedConnection,'status',status)
    app=server.create_app(tmp_path/'state',raw,index_factory=factory)
    app.state.access.create_environment('company-new','New company')
    code=app.state.access.issue('company-new',principal_id='alice')
    with TestClient(app) as client:
        assert client.post('/api/session',json={'code':code},headers={'X-Video-Demo':'1'}).status_code==200
        response=client.get('/api/status');assert response.status_code==200,response.text
    assert len(received)==1 and received[0][1] is raw
    assert received[0][0] is not raw
    assert {row['video_id'] for row in response.json()['corpus']}=={'company-new-video'}
