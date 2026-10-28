"""Native tree projection and transport races: fake Codex, zero provider calls."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import time

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent, tool_specs, SYSTEM
from deploy.migration.app_server import AppServerConnection
from deploy.migration.legacy.isolated_turn import NativeTree,isolated_instructions,portable_input,run_isolated
from deploy.migration.legacy.native_policy import isolated_thread_params
from deploy.migration.legacy.native_files import PUBLISH_FILE_TOOL, NativeFilePublisher, PublicationError
from native_executor import NativeScope, NativeLease
from test_native_output_publication import Broker

@pytest.fixture
def anyio_backend():return 'asyncio'


def root_agent(tmp_path):
    agent=CodexVideoAgent(tmp_path,SimpleNamespace(),{})
    agent.children['chat']={'id':'chat','owner_principal_id':'alice','dataset':'cafe','title':'Review service',
        'messages':[{'role':'user','content':'Review service'},{'role':'assistant','content':'','results':[],'tools':[]}]}
    agent._save_child(agent.children['chat']);agent.registry['chat']='native-root'
    agent.capability_snapshot=lambda *args:None
    agent.agent_setup_context=lambda *args:None
    return agent


class Connection:
    def __init__(self):
        self.events=asyncio.Queue();self.writes=[];self.reads=[];self.threads={
            'native-root':{'id':'native-root','parentThreadId':None,'status':{'type':'idle'},'turns':[{'id':'turn1','status':'completed'}]}}
    async def rpc(self,method,params):
        if method=='thread/read':self.reads.append(params['threadId']);return {'thread':self.threads.get(params['threadId'],{})}
        if method=='thread/turns/list':return {'data':list(reversed(self.threads.get(params['threadId'],{}).get('turns',[])))[:1]}
        raise AssertionError(method)
    async def write(self,item,**kwargs):self.writes.append(item)
    def child(self,key,parent='native-root',**extra):
        self.threads[key]={'id':key,'parentThreadId':parent,'source':{'subAgent':{'thread_spawn':{'parent_thread_id':parent}}},
            'name':'Camera evidence','status':{'type':'active'},**extra}


def tree_fixture(tmp_path):
    agent=root_agent(tmp_path);connection=Connection()
    run={'ready':asyncio.Event(),'cancel':asyncio.Event(),'child_ids':set(),'tool_events':asyncio.Queue(),
        'deadline':time.monotonic()+30,'generation':'generation','turn_id':'turn1','native_turn_active':True}
    agent.active['chat']=run
    tree=NativeTree(agent,'chat','cafe',run,connection,SimpleNamespace(),'native-root')
    tree.allowed={spec['name']:spec for spec in isolated_thread_params('',tool_specs(True))['dynamicTools']}
    return agent,run,connection,tree


def event(method,thread='native-root',**params):return {'method':method,'params':{'threadId':thread,**params}}


def question(thread='native-root',identity=0):return {'id':identity,**event('item/tool/requestUserInput',thread,
    turnId='turn1',itemId='question1',isBlocking=False,questions=[{'id':'format','header':'Format',
    'question':'Choose a format','isOther':False,'isSecret':False,'options':[{'label':'PDF','description':'Document'}]}])}


@pytest.mark.anyio
async def test_only_verified_lineage_gets_readonly_product_child(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path)
    connection.child('child');connection.child('grandchild','child')
    identity=await tree.admit('grandchild')
    child=agent.children[identity];parent=agent.children[child['parent_id']]
    assert parent['parent_id']=='chat' and child['native_root_id']=='chat'
    assert child['owner_principal_id']=='alice' and child['dataset']=='cafe'
    assert agent.active[identity]['cancel'] is run['cancel']
    connection.child('foreign','someone-else');assert await tree.admit('foreign') is None
    connection.child('mismatch',source={'subAgent':{'thread_spawn':{'parent_thread_id':'unrelated'}}})
    assert await tree.admit('mismatch') is None
    assert not any(record['id'] in {'foreign','mismatch','child','grandchild'} for record in agent.children.values())


@pytest.mark.anyio
async def test_native_child_cannot_call_parent_dynamic_mutation(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path);connection.child('child')
    request={'id':4,**event('item/tool/call','child',tool='create_file',arguments={'name':'unsafe.md','content':'x'})}
    assert await tree.event(request)==[]
    assert connection.writes[-1]['result']['success'] is False
    assert not tree.tools and not (tmp_path/'chat-files').exists()


@pytest.mark.anyio
async def test_native_parent_publication_emits_real_artifact_and_child_is_readonly(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path)
    scope=NativeScope('company-a','alice','chat')
    broker=Broker(b'original document bytes')
    tree.session=SimpleNamespace(scope=scope,lease=NativeLease(scope.key,1,'a'*48,'/private'),
        client=broker,stopped=False,_closing=False)
    tree.publisher=NativeFilePublisher(agent._files(),company_id=scope.company_id,
        owner_principal_id=scope.owner_id,root_chat_id=scope.root_chat_id,dataset='cafe')
    tree.allowed['publish_file']=PUBLISH_FILE_TOOL
    request={'id':7,**event('item/tool/call',tool='publish_file',arguments={'path':'report.docx'})}
    await tree.event(request)
    await tree.tools[7]
    reply=connection.writes[-1]['result']
    assert reply['success'] is True
    artifact=json.loads(reply['contentItems'][0]['text'])
    assert agent._files().download(artifact['id'],owner_principal_id='alice')[1]==broker.raw
    notices=[]
    while not run['tool_events'].empty():notices.append(run['tool_events'].get_nowait())
    assert [notice['artifact'] for notice in notices if notice['type']=='artifact']==[artifact]
    count=len(broker.calls)
    connection.child('child')
    await tree.event({'id':8,**event('item/tool/call','child',tool='publish_file',arguments={'path':'report.docx'})})
    assert connection.writes[-1]['result']['success'] is False and len(broker.calls)==count


@pytest.mark.anyio
async def test_publication_failure_returns_safe_tool_result(tmp_path):
    _,run,connection,tree=tree_fixture(tmp_path)
    class Rejected:
        async def publish(self,*args,**kwargs):raise PublicationError('/private/socket/secret')
    tree.publisher=Rejected();tree.allowed['publish_file']=PUBLISH_FILE_TOOL
    await tree.tool({'id':9,'params':{'tool':'publish_file','arguments':{'path':'report.txt'}}},'chat')
    assert connection.writes[-1]['result']['success'] is False
    assert 'private/socket' not in json.dumps(connection.writes)
    assert all(value.get('type')!='artifact' for value in list(run['tool_events']._queue))


@pytest.mark.anyio
async def test_child_tool_activity_persists_in_child_not_parent_answer(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path);connection.child('child')
    child=await tree.admit('child')
    await tree.event(event('item/started','child',item={'type':'commandExecution','id':'cmd','command':'pwd','cwd':'/workspace','status':'inProgress','commandActions':[]}))
    await tree.event(event('item/completed','child',item={'type':'commandExecution','id':'cmd','command':'pwd','cwd':'/workspace','status':'completed','exitCode':0,'aggregatedOutput':'/workspace','commandActions':[]}))
    assert agent.children[child]['messages'][-1]['tools'][0]['status']=='complete'
    assert not agent.children['chat']['messages'][-1]['tools']


@pytest.mark.anyio
async def test_native_parent_final_held_until_child_and_native_wakeup_finish(tmp_path,monkeypatch):
    monkeypatch.setattr('deploy.migration.legacy.isolated_turn.QUIET_SECONDS',.01)
    agent,run,connection,tree=tree_fixture(tmp_path);connection.child('child')
    await tree.event(event('item/completed',item={'type':'subAgentActivity','id':'spawn','kind':'started','agentThreadId':'child','agentPath':'/root/evidence'}))
    await tree.event(event('item/completed',item={'type':'agentMessage','id':'draft','phase':'final_answer','text':'Interim'}))
    await tree.event(event('turn/completed',turn={'id':'turn1','status':'completed'}))
    assert not tree.settling() and tree.pending_answer==['Interim']
    await tree.event(event('turn/completed','child',turn={'id':'child-turn','status':'completed'}))
    connection.events.put_nowait(event('turn/started',turn={'id':'turn2','status':'inProgress'}))
    connection.events.put_nowait(event('item/completed',item={'type':'agentMessage','id':'final','phase':'final_answer','text':'Verified result'}))
    connection.events.put_nowait(event('turn/completed',turn={'id':'turn2','status':'completed'}))
    connection.threads['native-root']['turns']=[{'id':'turn2','status':'completed'}]
    output=[value async for value in tree.stream()]
    assert [value['text'] for value in output if value['type']=='delta']==['Verified result']
    assert any(value.get('activity')=='intermediate_answer' and value['text']=='Interim' for value in output)
    assert run['turn_id']=='turn2'


@pytest.mark.anyio
async def test_normal_question_is_durable_opaque_and_child_attributed(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path);connection.child('child')
    output=await tree.event(question('child'))
    public=output[-1]['request'];identity=tree.threads['child']
    assert public['id']!='0' and public['actor_chat_id']==identity and public['root_chat_id']=='chat'
    assert not {'native_request_id','thread_id','generation','owner'}&public.keys()
    await tree.event(event('serverRequest/resolved','child',requestId=0))
    assert agent.native_questions.get(public['id'],owner='alice',root_chat_id='chat')['status']=='resolved'


@pytest.mark.anyio
async def test_secret_question_never_reaches_public_metadata(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path);request=question()
    request['params']['questions'][0].update(isSecret=True,question='secret-token-123')
    output=await tree.event(request)
    assert output[-1]['request']['status']=='unsupported'
    assert 'secret-token-123' not in json.dumps(output)
    assert connection.writes[-1]=={'id':0,'result':{'answers':{}}}


@pytest.mark.anyio
async def test_stop_fences_question_queued_behind_write_lock(tmp_path):
    agent,run,_,tree=tree_fixture(tmp_path)
    class Writer:
        def __init__(self):self.writes=[]
        def write(self,value):self.writes.append(value)
        async def drain(self):pass
    process=SimpleNamespace(stdin=Writer(),stdout=asyncio.StreamReader())
    connection=AppServerConnection(process);run['connection']=connection
    public=(await tree.event(question()))[-1]['request']
    await connection.write_lock.acquire()
    response=asyncio.create_task(agent.resolve_native_question('chat',public['id'],owner='alice',action='submit',answers={'format':['PDF']}))
    await asyncio.sleep(0)
    assert agent.native_questions.get(public['id'],owner='alice',root_chat_id='chat')['status']=='delivering'
    run['cancel'].set();agent.native_questions.retire_generation('chat',run['generation'],reason='stopped')
    connection.write_lock.release()
    result=await response
    assert result['status']=='interrupted' and process.stdin.writes==[]
    connection.reader.cancel();await asyncio.gather(connection.reader,return_exceptions=True)


@pytest.mark.anyio
async def test_parent_stop_revokes_and_stops_worker_and_pending_questions(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path)
    request=(await tree.event(question()))[-1]['request'];stopped=[]
    async def stop():
        assert run['cancel'].is_set()
        assert agent.native_questions.get(request['id'],owner='alice',root_chat_id='chat')['status']=='interrupted'
        stopped.append(True)
    run.update(isolated=True,isolated_session=SimpleNamespace(stop=stop))
    assert (await agent.control('chat','stop'))['result']['stopped'] and stopped


def test_isolated_input_embeds_authorized_image_and_preserves_upstream_prompt(tmp_path):
    (tmp_path/'attachments').mkdir();path=tmp_path/'attachments'/'a.png';path.write_bytes(b'\x89PNG\r\n\x1a\ncontent')
    result=portable_input([{'type':'localImage','path':str(path)}],tmp_path)
    assert result[0]['type']=='image' and str(tmp_path) not in json.dumps(result)
    with pytest.raises(ValueError):portable_input([{'type':'localImage','path':'/etc/passwd'}],tmp_path)
    params=isolated_thread_params(isolated_instructions(SYSTEM),tool_specs(True))
    assert 'baseInstructions' not in params and 'delegate_investigations' not in params['developerInstructions']
    assert not {'delegate_investigations','wait_investigations'}&{tool['name'] for tool in params['dynamicTools']}


@pytest.mark.anyio
async def test_no_live_run_retires_question_without_replay(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path)
    request=(await tree.event(question()))[-1]['request'];agent.active.pop('chat')
    result=await agent.resolve_native_question('chat',request['id'],owner='alice',action='submit',answers={'format':['PDF']})
    assert result['status']=='interrupted' and not connection.writes

class NativeProtocol:
    def __init__(self,*,question_first=False):
        self.stdout=asyncio.StreamReader();self.stdin=self;self.calls=[];self.writes=[]
        self.turn=0;self.status='idle';self.question_first=question_first
        self.thread={'id':'isolated-root','parentThreadId':None,'source':'appServer','status':{'type':'idle'},'turns':[]}
    def feed(self,value):self.stdout.feed_data((json.dumps(value)+'\n').encode())
    def notify(self,method,**params):self.feed({'method':method,'params':{'threadId':'isolated-root',**params}})
    def write(self,data):
        item=json.loads(data);self.writes.append(item);method=item.get('method');params=item.get('params') or {}
        if method:self.calls.append((method,params))
        if method=='initialize':result={}
        elif method=='thread/start':result={'thread':self.thread}
        elif method=='thread/resume':result={'thread':self.thread}
        elif method=='thread/read':result={'thread':{**self.thread,'status':{'type':self.status}}}
        elif method in {'config/value/write','config/mcpServer/reload'}:result={}
        elif method=='mcpServerStatus/list':result={'data':[{'name':'rocoto','tools':{'list_cameras':{}}}]}
        elif method=='turn/start':
            self.turn+=1;self.status='active';result={'turn':{'id':f'turn{self.turn}','status':'inProgress'}}
            self.feed({'id':item['id'],'result':result})
            self.notify('turn/started',turn=result['turn'])
            if self.question_first:
                self.feed(question('isolated-root',0))
            else:self.finish()
            return
        elif method=='initialized':return
        elif method is None and item.get('id')==0:
            self.finish();return
        else:raise AssertionError(method)
        self.feed({'id':item['id'],'result':result})
    def finish(self):
        self.notify('item/completed',item={'type':'agentMessage','id':f'answer{self.turn}','phase':'final_answer','text':'Completed report'})
        self.status='idle';self.thread['turns']=[{'id':f'turn{self.turn}','status':'completed'}]
        self.notify('turn/completed',turn=self.thread['turns'][-1])
    async def drain(self):pass


class Launcher:
    enabled=True;company_id='rocoto'
    def __init__(self,agent,protocol):self.agent=agent;self.protocol=protocol;self.stopped=0;self.bindings=[]
    @asynccontextmanager
    async def lease(self,scope,run_id):
        launcher=self
        class Session:
            process=launcher.protocol
            stopped=False;_closing=False
            def thread_params(self,instructions,tools):return isolated_thread_params(instructions,tools)
            async def bind_mcp(self,mcp):
                launcher.bindings.append(mcp.scope)
                assert launcher.agent.registry['chat']=='isolated-root'
                assert mcp.provider_relay is None
                return {'keyPath':'mcp_servers','value':{'rocoto':{'private':'scoped-token'}},'mergeStrategy':'replace'}
            async def stop(self):
                self._closing=True;self.stopped=True;launcher.stopped+=1
        session=Session()
        try:yield session
        finally:
            assert self.agent.active['chat']['cancel'].is_set()
            await session.stop()


@pytest.mark.anyio
async def test_full_optin_bootstrap_streams_and_resumes_scoped_thread(tmp_path):
    agent=root_agent(tmp_path);protocol=NativeProtocol();launcher=Launcher(agent,protocol);agent.isolated_launcher=launcher
    outputs=[]
    async for value in agent._turn('Report','chat','cafe'):
        outputs.append(value)
        if value['type']=='done':assert launcher.stopped==1
    assert not any(value['type']=='error' for value in outputs),outputs
    assert any(value.get('text')=='Completed report' for value in outputs)
    assert outputs[-1]['status']=='completed' and not agent.active
    names=[name for name,_ in protocol.calls]
    assert names.index('thread/read')<names.index('config/value/write')<names.index('config/mcpServer/reload')<names.index('turn/start')
    assert launcher.bindings[0].owner_principal_id=='alice' and launcher.bindings[0].dataset=='cafe'
    start=next(params for name,params in protocol.calls if name=='thread/start')
    assert 'baseInstructions' not in start
    assert 'scoped-token' not in json.dumps(outputs) and 'scoped-token' not in agent.toolsets_path.read_text()
    protocol.calls=[]
    outputs=[value async for value in agent._turn('Follow-up','chat','cafe')]
    assert not any(value['type']=='error' for value in outputs),outputs
    assert 'thread/resume' in [name for name,_ in protocol.calls]


@pytest.mark.anyio
async def test_full_question_reply_wakes_existing_native_turn_without_new_turn(tmp_path):
    agent=root_agent(tmp_path);protocol=NativeProtocol(question_first=True);agent.isolated_launcher=Launcher(agent,protocol)
    outputs=[]
    async for value in agent._turn('Report','chat','cafe'):
        outputs.append(value)
        if value['type']=='user_input_request':
            reply=await agent.resolve_native_question('chat',value['request']['id'],owner='alice',action='submit',answers={'format':['PDF']})
            assert reply['status']=='submitted'
    assert outputs[-1]['status']=='completed',outputs
    assert sum(name=='turn/start' for name,_ in protocol.calls)==1
    assert any(item=={'id':0,'result':{'answers':{'format':{'answers':['PDF']}}}} for item in protocol.writes)


@pytest.mark.anyio
async def test_default_host_route_refuses_isolated_thread_binding(tmp_path):
    agent=root_agent(tmp_path);agent.toolsets['native-root']={'runtime':'isolated'}
    output=[value async for value in agent._turn('Report','chat','cafe')]
    assert output==[{'type':'error','message':'Resume this chat through its isolated runtime.'}]

@pytest.mark.anyio
async def test_stop_during_bootstrap_emits_done_after_actual_cleanup(tmp_path):
    agent=root_agent(tmp_path);protocol=NativeProtocol()
    class StoppedBeforeLaunch(Launcher):
        @asynccontextmanager
        async def lease(self,scope,run_id):
            self.agent.active['chat']['cancel'].set()
            async with super().lease(scope,run_id) as session:yield session
    launcher=StoppedBeforeLaunch(agent,protocol);agent.isolated_launcher=launcher
    output=[]
    async for value in agent._turn('Report','chat','cafe'):
        output.append(value)
        if value['type']=='done':assert launcher.stopped==1
    assert output[-1]['status']=='interrupted' and not protocol.calls
    assert not agent.active


@pytest.mark.anyio
async def test_failed_worker_cleanup_is_never_reported_complete(tmp_path):
    agent=root_agent(tmp_path);protocol=NativeProtocol()
    class FailedStop(Launcher):
        @asynccontextmanager
        async def lease(self,scope,run_id):
            async with super().lease(scope,run_id) as session:yield session
            raise RuntimeError('private-run-token-not-for-chat')
    agent.isolated_launcher=FailedStop(agent,protocol)
    output=[value async for value in agent._turn('Report','chat','cafe')]
    assert not any(value['type']=='done' for value in output)
    assert output[-1]['status']=='failed'
    assert 'private-run-token-not-for-chat' not in json.dumps(output)


@pytest.mark.anyio
async def test_unrecognized_actor_request_gets_denied_without_question_record(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path)
    assert await tree.event(question('foreign'))==[]
    assert connection.writes[-1]['error']['code']==-32600
    assert agent.native_questions.for_chat('chat',owner='alice')==[]


@pytest.mark.anyio
async def test_deadline_cancels_waiting_question_and_records_truthfully(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path)
    question_event=(await tree.event(question()))[-1]
    agent._record_event(agent.children['chat']['messages'][-1],question_event)
    run['deadline']=time.monotonic()-1
    with pytest.raises(TimeoutError):[value async for value in tree.stream()]
    tree.retire_questions('deadline')
    assert agent.children['chat']['messages'][-1]['native_questions'][0]['status']=='interrupted'
    assert agent.children['chat']['messages'][-1]['native_questions'][0]['resolution_reason']=='deadline'


@pytest.mark.anyio
async def test_direct_child_control_and_owner_mismatch_denied(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path);connection.child('child');child=await tree.admit('child')
    with pytest.raises(ValueError,match='parent'):await agent.control(child,'stop')
    question_event=(await tree.event(question()))[-1]
    with pytest.raises(ValueError,match='owning'):await agent.resolve_native_question('chat',question_event['request']['id'],owner='mallory',action='skip')
    assert not run['cancel'].is_set()

@pytest.mark.anyio
async def test_record_edit_cannot_rebind_existing_native_workspace_dataset(tmp_path):
    agent=root_agent(tmp_path);agent.scopes['chat']='steel'
    launcher=Launcher(agent,NativeProtocol());agent.isolated_launcher=launcher
    output=[value async for value in agent._turn('Report','chat','cafe')]
    assert len(output)==1 and output[0]['type']=='error'
    assert not launcher.protocol.calls and not agent.active

@pytest.mark.anyio
async def test_referenced_completed_child_remains_complete_on_parent_resume(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path)
    connection.child('prior-child',status={'type':'idle'},turns=[{'id':'old-turn','status':'completed'}])
    output=await tree.event(event('item/completed',item={'type':'collabAgentToolCall','id':'wait',
        'tool':'wait','status':'completed','receiverThreadIds':['prior-child'],'agentsStates':{'prior-child':{'status':'completed'}}}))
    child=tree.threads['prior-child']
    assert not agent.children[child]['running'] and not agent.active[child]['native_turn_active']
    assert output[0]['status']=='completed'
    tree.root_status='completed'
    assert tree.settling() and await tree.settled()

@pytest.mark.anyio
async def test_failed_native_turn_system_error_is_terminal_not_infinite_wait(tmp_path):
    agent,run,connection,tree=tree_fixture(tmp_path)
    connection.threads['native-root'].update(status={'type':'systemError'},turns=[{'id':'turn1','status':'failed'}])
    await tree.event(event('turn/completed',turn={'id':'turn1','status':'failed'}))
    assert tree.settling() and await tree.settled()


@pytest.mark.anyio
async def test_mcp_lifespan_exception_group_projects_sanitized_failure(tmp_path):
    agent=root_agent(tmp_path);protocol=NativeProtocol()
    class FailedLifespan(Launcher):
        @asynccontextmanager
        async def lease(self,scope,run_id):
            async with super().lease(scope,run_id) as session:yield session
            raise ExceptionGroup('private-transport-value',[TimeoutError('private-timeout')])
    agent.isolated_launcher=FailedLifespan(agent,protocol)
    output=[value async for value in agent._turn('Report','chat','cafe')]
    assert output[-1]['status']=='failed' and not any(value['type']=='done' for value in output)
    assert 'private-transport-value' not in json.dumps(output)
