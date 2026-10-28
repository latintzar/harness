import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json,sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_skills import NativePluginSkills,NativeSkillError,native_skill_inputs
from native_packages import NativePackageStore,MARKETPLACE
from native_executor import NativeScope
from deploy.migration.app_server import AppServerConnection
from test_native_packages import package,snapshot,Fake
from agent_test_support import root_agent

@pytest.fixture
def anyio_backend():return 'asyncio'

async def fixture(tmp_path,package,snapshot):
    snapshot['plugins'][0]['skills']=[{'name':'notes','description':'Original notes workflow.','path':'plugins/notion/skills/notes/SKILL.md','metadata_status':'available','source_url':'https://github.com/openai/plugins/blob/'+'a'*40+'/plugins/notion/skills/notes/SKILL.md'}]
    store=NativePackageStore(tmp_path/'state',catalogue_snapshot=snapshot,materializer=Fake(package))
    await store.install('notion',principal_id='alice');skills=NativePluginSkills(store)
    selected=store.snapshot(principal_id='alice');public=skills.list('alice')
    path='/codex-state/plugins/cache/'+MARKETPLACE+'/notion/0.1.7/skills/notes/SKILL.md'
    class Native:
        def __init__(self):self.calls=[];self.gate=None;self.entered=asyncio.Event();self.row={'name':'notion:notes','path':path,'pluginId':'notion@'+MARKETPLACE,'enabled':True}
        async def rpc(self,method,params):
            assert method=='skills/list' and params=={'cwds':['/workspace'],'forceReload':True}
            self.calls.append(method);self.entered.set()
            if self.gate:await self.gate.wait()
            return {'data':[{'cwd':'/workspace','skills':[self.row],'errors':[]}]}
    return store,skills,selected,public,Native()

@pytest.mark.anyio
async def test_original_descriptor_is_private_path_free_namespaced_and_owner_bound(tmp_path,package,snapshot):
    store,skills,selection,rows,native=await fixture(tmp_path,package,snapshot)
    assert len(rows)==1 and rows[0]['command']=='notion:notes' and rows[0]['description']=='Notes for a connected account.'
    assert rows[0]['native_plugin'] and str(tmp_path) not in json.dumps(rows) and '/codex-state' not in json.dumps(rows)
    assert skills.list('bob')==[]
    with pytest.raises(NativeSkillError):skills.resolve([rows[0]['id']],'bob')
    result=await skills.inputs([rows[0]['id']],principal_id='alice',selection=selection,connection=native,cancelled=asyncio.Event())
    assert result==[{'type':'skill','name':'notion:notes','path':native.row['path']}]
    assert native.calls==['skills/list']

@pytest.mark.anyio
async def test_remove_and_reinstall_invalidates_previous_skill_generation(tmp_path,package,snapshot):
    store,skills,selection,rows,native=await fixture(tmp_path,package,snapshot)
    await store.uninstall('notion',principal_id='alice')
    assert skills.list('alice')==[]
    await store.install('notion',principal_id='alice')
    assert skills.list('alice')[0]['id']!=rows[0]['id']
    with pytest.raises(NativeSkillError):skills.resolve([rows[0]['id']],'alice')
    with pytest.raises(NativeSkillError):await skills.inputs([rows[0]['id']],principal_id='alice',selection=selection,connection=native,cancelled=asyncio.Event())
    assert native.calls==[]

@pytest.mark.anyio
@pytest.mark.parametrize('change',[{'path':'/etc/passwd'},{'path':'/codex-state/secrets.json'},{'pluginId':'other@rocoto-pinned-api'},{'enabled':False},{'name':'notes'}])
async def test_actual_native_inventory_must_match_selected_file_owner_name_and_enabled(tmp_path,package,snapshot,change):
    _,skills,selection,rows,native=await fixture(tmp_path,package,snapshot);native.row.update(change)
    with pytest.raises(NativeSkillError):await skills.inputs([rows[0]['id']],principal_id='alice',selection=selection,connection=native,cancelled=asyncio.Event())

@pytest.mark.anyio
async def test_catalogue_refresh_keeps_original_installed_skill(tmp_path,package,snapshot):
    store,skills,_,rows,_=await fixture(tmp_path,package,snapshot)
    store.snapshot_catalogue['revision']='b'*40
    row=store.entries['notion']['skills'][0]
    for path in ['/etc/passwd','plugins/notion/../../secret/SKILL.md','plugins/other/skills/notes/SKILL.md']:
        row['path']=path
        assert skills.list('alice')==rows
    store.entries['notion']['skills'].append(dict(row))
    assert skills.list('alice')==rows


@pytest.mark.anyio
@pytest.mark.parametrize('change',['stop','remove'])
async def test_stop_or_remove_while_inventory_waits_prevents_skill_delivery(tmp_path,package,snapshot,change):
    store,skills,selection,rows,native=await fixture(tmp_path,package,snapshot);native.gate=asyncio.Event();cancelled=asyncio.Event()
    task=asyncio.create_task(skills.inputs([rows[0]['id']],principal_id='alice',selection=selection,connection=native,cancelled=cancelled))
    await native.entered.wait()
    if change=='stop':cancelled.set()
    else:await store.uninstall('notion',principal_id='alice')
    native.gate.set()
    with pytest.raises(NativeSkillError):await task

@pytest.mark.anyio
async def test_mixed_custom_and_original_skill_resolution_preserves_existing_custom_instructions(tmp_path,package,snapshot):
    store,skills,selection,rows,native=await fixture(tmp_path,package,snapshot)
    agent=root_agent(store.state);agent.isolated_launcher=SimpleNamespace(enabled=True,package_store=store,company_id=store.company_scope)
    agent._connections=lambda:SimpleNamespace(capabilities=lambda principal:[])
    custom=agent._skills().create({'name':'Review source','description':'Review supplied evidence.','instructions':'Review supplied source evidence carefully.','required_tools':[],'required_connections':[]})
    agent._skills().install(custom['id'],expected_revision=custom['revision'])
    resolved=agent.resolve_skills([custom['id'],rows[0]['id']],'chat')
    assert resolved['skills'][0]['id']==custom['id'] and resolved['skills'][0]['instructions']
    assert resolved['native_skills']==rows
    assert '/codex-state' not in json.dumps(resolved)
    with pytest.raises(ValueError):agent.resolve_skills([rows[0]['id']],'chat',owner_principal_id='bob')

@pytest.mark.anyio
async def test_uninstall_while_native_write_is_queued_fences_actual_skill_dispatch(tmp_path,package,snapshot):
    store,skills,selection,rows,native=await fixture(tmp_path,package,snapshot)
    agent=root_agent(store.state);agent.isolated_launcher=SimpleNamespace(enabled=True,package_store=store,company_id=store.company_scope)
    session=SimpleNamespace(scope=NativeScope(store.company_scope,'alice','chat'),runtime_packages=selection,_closing=False,stopped=False)
    run={'isolated_session':session,'connection':native,'cancel':asyncio.Event()};agent.active['chat']=run
    inputs=await native_skill_inputs(agent,{'native_skills':rows},run)
    class Writer:
        def __init__(self):self.values=[]
        def write(self,value):self.values.append(value)
        async def drain(self):pass
    writer=Writer();connection=AppServerConnection(SimpleNamespace(stdin=writer,stdout=asyncio.StreamReader()))
    await connection.write_lock.acquire()
    task=asyncio.create_task(connection.rpc('turn/start',{'threadId':'native-root','input':inputs},guard=run['native_skill_guard']))
    await asyncio.sleep(0);await store.uninstall('notion',principal_id='alice');connection.write_lock.release()
    with pytest.raises(RuntimeError,match='ended'):await task
    assert writer.values==[] and not connection.pending
    connection.reader.cancel();await asyncio.gather(connection.reader,return_exceptions=True)


def test_http_original_skills_resolve_current_owner_observer_and_company(tmp_path,monkeypatch,package,snapshot):
    from fastapi.testclient import TestClient
    from test_server_access import fixture_app,FixtureAgent
    from test_workflow_routes import login,HEADERS
    import server
    from agent_bridge import CodexVideoAgent
    stores={}
    original=FixtureAgent.__init__
    def initialize(self,state,index=None,corpus=None):
        original(self,state,index)
        self.state=Path(state)
        store=NativePackageStore(state,catalogue_snapshot=snapshot,materializer=Fake(package))
        self.isolated_launcher=SimpleNamespace(enabled=True,company_id=store.company_scope,package_store=store)
        stores[self.company]=self.isolated_launcher.package_store
    monkeypatch.setattr(FixtureAgent,'__init__',initialize)
    monkeypatch.setattr(FixtureAgent,'_native_skills',CodexVideoAgent._native_skills,raising=False)
    snapshot['plugins'][0]['skills']=[{'name':'notes','description':'Original notes workflow.','path':'plugins/notion/skills/notes/SKILL.md','metadata_status':'available'}]
    app=fixture_app(tmp_path,monkeypatch);app.state.access.create_environment('other','Other')
    with TestClient(app) as alice,TestClient(app) as bob,TestClient(app) as owner,TestClient(app) as other:
        login(alice,app,'alice');login(bob,app,'bob');login(owner,app,'owner',owner=True)
        asyncio.run(stores['demo'].install('notion',principal_id='alice'))
        own=alice.get('/api/skills');assert own.status_code==200,own.text
        originals=[s for s in own.json()['skills'] if s.get('native_plugin')]
        assert len(originals)==1 and originals[0]['command']=='notion:notes'
        assert bob.get('/api/skills').json()['skills']==[]
        from skills import BUILTINS
        assert len(alice.get('/api/skills/catalogue').json()['skills'])==len(BUILTINS)+1
        other.post('/api/session',json={'code':app.state.access.issue('other',principal_id='alice')},headers=HEADERS)
        assert other.get('/api/skills').json()['skills']==[]
        owner.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS)
        assert owner.get('/api/skills').json()['skills']==originals
        assert owner.post('/api/skills/install',json={'skill_id':originals[0]['id']},headers=HEADERS).status_code==403
        alice.post('/api/session/demo',headers=HEADERS)
        assert alice.get('/api/skills').json()['skills']==[]
        assert all(not s.get('native_plugin') for s in alice.get('/api/skills/catalogue').json()['skills'])
        asyncio.run(stores['demo'].uninstall('notion',principal_id='alice'))
        assert owner.get('/api/skills').json()['skills']==[]

@pytest.mark.anyio
async def test_same_named_originals_are_distinct_by_package(tmp_path,package,snapshot):
    import copy
    other=copy.deepcopy(snapshot['plugins'][0]);other.update(id='other',name='Other',source_url='https://github.com/openai/plugins/tree/'+'a'*40+'/plugins/other')
    other['catalogue_entry']={'name':'other','source':{'source':'local','path':'./plugins/other'}}
    snapshot['plugins'].append(other)
    for item in snapshot['plugins']:
        if item['id'] in {'notion','other'}:item['skills']=[{'name':'notes','description':'Original notes workflow.','path':'plugins/'+item['id']+'/skills/notes/SKILL.md','metadata_status':'available'}]
    other_package=tmp_path/'other-original';import shutil;shutil.copytree(package,other_package)
    manifest=other_package/'.codex-plugin/plugin.json';metadata=json.loads(manifest.read_text());metadata['name']='other';manifest.write_text(json.dumps(metadata))
    class Both:
        @asynccontextmanager
        async def acquire(self,entry,snapshot):yield (package if entry['id']=='notion' else other_package),{'version':'0.1.7'}
    store=NativePackageStore(tmp_path/'state',catalogue_snapshot=snapshot,materializer=Both())
    for plugin in ['notion','other']:await store.install(plugin,principal_id='alice')
    rows=NativePluginSkills(store).list('alice')
    assert {r['command'] for r in rows}=={'notion:notes','other:notes'}
    assert len({r['id'] for r in rows})==2


@pytest.mark.anyio
@pytest.mark.parametrize('steer',[False,True])
@pytest.mark.parametrize('writes',[False,True])
async def test_complete_isolated_turn_delivers_original_native_input_after_inventory(tmp_path,package,snapshot,steer,writes):
    from test_isolated_turn import NativeProtocol
    from deploy.migration.legacy.native_policy import isolated_thread_params
    # Commissioning is attached to the actual package server inventory.
    snapshot['plugins'][0]['mcp_servers']=[{'name':'notion','url':'https://mcp.notion.com/mcp','transport':'http'}]
    store,skills,selection,rows,native=await fixture(tmp_path,package,snapshot)
    agent=root_agent(store.state)
    from mcp_connections import MCPConnections
    agent.connection_store=MCPConnections(store.state,public_origin='https://example.invalid',
        write_providers={'notion'} if writes else frozenset())
    class Protocol(NativeProtocol):
        def write(self,data):
            item=json.loads(data)
            if item.get('method')=='skills/list':
                self.calls.append(('skills/list',item['params']));self.feed({'id':item['id'],'result':{'data':[{'cwd':'/workspace','skills':[native.row],'errors':[]}]}})
            elif item.get('method')=='turn/steer':
                self.calls.append(('turn/steer',item['params']));self.feed({'id':item['id'],'result':{'turnId':'turn1'}});self.finish()
            else:super().write(data)
    protocol=Protocol(question_first=steer)
    class Launcher:
        enabled=True;company_id=store.company_scope;package_store=store
        @asynccontextmanager
        async def lease(self,scope,run_id):
            class Session:
                process=protocol;runtime_packages=selection;stopped=False;_closing=False
                def thread_params(self,instructions,tools):return isolated_thread_params(instructions,tools)
                async def bind_mcp(self,mcp):
                    assert mcp.provider_relay is not None
                    assert ('approval_router' in agent.active['chat']) is writes
                    if writes:
                        router=agent.active['chat']['approval_router']
                        assert router.scope.owner=='alice' and router.scope.root_chat_id=='chat'
                        assert router.scope.root_thread_id=='isolated-root'
                        assert router.store is agent.native_approvals
                    return {'keyPath':'mcp_servers','value':{'rocoto':{'private':'scoped-token'}},'mergeStrategy':'replace'}
                async def stop(self):self._closing=True;self.stopped=True
            session=Session();session.scope=scope
            try:yield session
            finally:await session.stop()
    agent.isolated_launcher=Launcher()
    resolved=agent.resolve_skills([rows[0]['id']],'chat')
    output=[]
    async for event in agent._turn('Prepare report','chat','cafe',resolved_skills=None if steer else resolved):
        output.append(event)
        if event['type']=='user_input_request':
            reply=await agent.control('chat','steer','Use this original workflow.',skill_ids=[rows[0]['id']])
            assert reply['ok']
    assert not any(e['type']=='error' for e in output),output
    assert output[-1]['status']=='completed'
    selected_method='turn/steer' if steer else 'turn/start'
    start=next(params for method,params in protocol.calls if method==selected_method)
    assert start['input'][-1]=={'type':'skill','name':'notion:notes','path':native.row['path']}
    names=[method for method,_ in protocol.calls]
    assert names.index('skills/list')<names.index(selected_method)
    initialization=next(params for method,params in protocol.calls if method=='initialize')
    assert initialization['capabilities'].get('mcpServerOpenaiFormElicitation',False) is writes
    thread_params=next(params for method,params in protocol.calls if method=='thread/start')
    if writes:
        assert thread_params['approvalPolicy']['granular']['mcp_elicitations'] is True
        assert thread_params['approvalPolicy']['granular']['sandbox_approval'] is False
    else:assert thread_params['approvalPolicy']=='never'
    assert '/codex-state' not in json.dumps(output)

@pytest.mark.anyio
async def test_stop_while_native_skill_write_waits_sends_no_turn(tmp_path,package,snapshot):
    store,skills,selection,rows,native=await fixture(tmp_path,package,snapshot)
    agent=root_agent(store.state);agent.isolated_launcher=SimpleNamespace(enabled=True,package_store=store,company_id=store.company_scope)
    session=SimpleNamespace(scope=NativeScope(store.company_scope,'alice','chat'),runtime_packages=selection,_closing=False,stopped=False)
    run={'isolated_session':session,'connection':native,'cancel':asyncio.Event()};agent.active['chat']=run
    inputs=await native_skill_inputs(agent,{'native_skills':rows},run)
    class Writer:
        def __init__(self):self.values=[]
        def write(self,value):self.values.append(value)
        async def drain(self):pass
    writer=Writer();connection=AppServerConnection(SimpleNamespace(stdin=writer,stdout=asyncio.StreamReader()))
    await connection.write_lock.acquire()
    task=asyncio.create_task(connection.rpc('turn/start',{'threadId':'native-root','input':inputs},guard=run['native_skill_guard']))
    await asyncio.sleep(0);run['cancel'].set();connection.write_lock.release()
    with pytest.raises(RuntimeError):await task
    assert writer.values==[] and not connection.pending
    connection.reader.cancel();await asyncio.gather(connection.reader,return_exceptions=True)
