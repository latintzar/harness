"""Signed ingress and finite self-wakes feed the same ordinary-message dispatcher."""
import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from background_agents import AgentStore
from signal_sources import SignalSources
from signal_dispatcher import SignalDispatcher


def services(tmp_path):
    store=AgentStore(tmp_path)
    allowed={'alice'}
    store.signal_authority=lambda row:row['actor_principal_id'] in allowed and row['chat_id']=='chat'
    sources=SignalSources(store,public_origin='https://harness.example.test')
    identity={'owner':'alice','actor':'alice','chat_id':'chat','collection':'company','label':'Delivery'}
    return store,sources,identity,allowed


def sign(secret,body):return 'sha256='+hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()


def test_hook_secret_once_signature_before_parse_and_immutable_event(tmp_path):
    store,sources,identity,allowed=services(tmp_path)
    hook=sources.create_hook(**identity)
    assert 'secret' not in hook and hook['secret_available'] is True
    secret=sources.reveal(hook['id'],actor='alice',chat_id='chat')
    with pytest.raises(ValueError):sources.reveal(hook['id'],actor='alice',chat_id='chat')
    with pytest.raises(PermissionError):sources.receive(hook['id'],b'invalid json','wrong')
    raw=b'{"id":"one","text":"Delivery arrived"}'
    result=sources.receive(hook['id'],raw,sign(secret,raw))
    assert result['status']=='accepted'
    assert sources.receive(hook['id'],raw,sign(secret,raw))['status']=='duplicate'
    changed=b'{"id":"one","text":"Changed"}'
    with pytest.raises(ValueError):sources.receive(hook['id'],changed,sign(secret,changed))
    wake=store.wakes.get(result['id'],owner='alice')
    assert wake['target_kind']=='chat' and wake['sender']['principal_id']=='alice'
    assert wake['sender']['channel']=='webhook'
    assert 'secret' not in json.dumps(wake)
    allowed.clear()
    with pytest.raises(ValueError):sources.receive(hook['id'],raw,sign(secret,raw))


def test_timer_restart_catches_up_once_without_extending_original_grant(tmp_path):
    store,sources,identity,allowed=services(tmp_path);now=time.time()
    timer=sources.set_timer(**identity,note='Review this delivery.',delay_seconds=60,every_seconds=300,fires=3,now=now)
    sources=SignalSources(store)
    sources.tick(now=now+2000)
    sources.tick(now=now+2000)
    current=sources.get(timer['id'])
    assert current['fired']==1 and current['next_at']==now+2300
    assert current['authority_until']==timer['authority_until']
    with store.connect() as db:assert db.execute('SELECT COUNT(*) FROM agent_wakes').fetchone()[0]==1
    with pytest.raises(ValueError):sources.set_timer(**identity,note='After grant',delay_seconds=600,authority_until=now+100,now=now)
    sources.cancel(timer['id'],actor='alice',chat_id='chat')
    with store.connect() as db:assert db.execute('SELECT status FROM agent_wakes').fetchone()[0]=='cancelled'


def test_real_dispatcher_delivers_timer_as_ordinary_message_with_actor(tmp_path):
    async def proof():
        store,sources,identity,allowed=services(tmp_path);now=time.time()
        timer=sources.set_timer(**identity,note='Review delivery.',now=now)
        sources.tick(now=now+61)
        class Agent:
            active={};child_tasks={}
            children={'chat':{'id':'chat','dataset':'company','owner_principal_id':'alice','messages':[]}}
            def _save_child(self,child):pass
            async def turn(self,text,chat_id,collection,*,sender,model_context):
                assert store.wakes.signal_current(self.children[chat_id]['active_signal_id'])
                assert sender['principal_id']=='alice' and sender['channel']=='timer'
                self.children[chat_id]['messages'].extend([{'role':'user','content':text,'channel':sender},
                    {'role':'assistant','content':'Reviewed'}])
                yield {'type':'done','status':'completed'}
        class Scheduler:
            async def tick(self):pass
        slots=asyncio.Semaphore(2)
        agent=Agent();dispatcher=SignalDispatcher(store,agent,Scheduler(),slots,sources)
        await dispatcher.tick();await dispatcher.task;await asyncio.sleep(0)
        assert len(agent.children['chat']['messages'])==2
        assert store.signals.usage()['turn']['used']==1
        assert slots._value==2
        with store.connect() as db:assert db.execute('SELECT status FROM agent_wakes').fetchone()[0]=='completed'
        await dispatcher.close()
    asyncio.run(proof())


def test_wake_tools_inherit_original_deadline_and_hide_secret(tmp_path):
    import wake_tools
    store,sources,identity,allowed=services(tmp_path)
    source=sources.create_hook(**identity)
    now=time.time()
    source_record=sources.get(source['id'])
    source_record['authority_until']=now+90
    with store.connect() as db:sources._write(db,source_record)
    agent=SimpleNamespace(signal_sources=sources,_turn_senders={'chat':{'channel':'webhook',
        'wake_source_id':source['id'],'authority_until':now+90}})
    context=SimpleNamespace(agent=agent,chat_id='chat',actor='alice',data_owner='alice',collection='company',
        require_current=lambda:None,require_parent=lambda:None,record={})
    timer=wake_tools.call(context,'set_wake',{'label':'Follow-up','note':'Follow up','delay_seconds':60})
    assert timer['authority_until']==now+90
    with pytest.raises(ValueError):wake_tools.call(context,'create_webhook',{'label':'Extend authority'})
    assert all('secret' not in item for item in wake_tools.read(context,'list_wakes',{})['sources'])


def test_signed_http_ingress_keeps_provider_work_lazy_and_reveals_secret_once(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_server_access import fixture_app
    app=fixture_app(tmp_path,monkeypatch)
    selected=app.state.workspaces['demo'];agent=selected['agent']
    headers={'X-Video-Demo':'1'}
    with TestClient(app) as client:
        code=app.state.access.issue('demo',owner=True,principal_id='alice',display_name='Alice')
        assert client.post('/api/session',json={'code':code},headers=headers).status_code==200
        chat={'id':'hook-chat','dataset':'cafe','owner_principal_id':'alice','messages':[]}
        agent.children[chat['id']]=chat
        directory=selected['state']/'chats';directory.mkdir(exist_ok=True,mode=0o700)
        path=directory/'hook-chat.json';path.write_text(json.dumps(chat));path.chmod(0o600)
        source=selected['signal_sources'].create_hook(owner='alice',actor='alice',chat_id='hook-chat',collection='cafe',label='Deliveries')
        route='/api/wakes/hooks/'+source['id']+'/secret'
        response=client.post(route,headers=headers)
        assert response.status_code==200,response.text
        secret=response.json()['secret']
        assert response.headers['cache-control']=='no-store'
        assert client.post(route,headers=headers).status_code==409
        client.cookies.clear()
        incoming='/api/hooks/demo/'+source['id']
        assert client.post(incoming,content=b'bad json',headers={'X-Hub-Signature-256':'wrong'}).status_code==403
        raw=b'{"id":"receipt","text":"The delivery arrived"}'
        response=client.post(incoming,content=raw,headers={'X-Hub-Signature-256':sign(secret,raw)})
        assert response.status_code==202,response.text
        signal=selected['agents'].wakes.get(response.json()['id'],owner='alice')
        assert signal['text']=='The delivery arrived' and signal['sender']['principal_id']=='alice'
        assert client.post(incoming,content=b'x'*65537).status_code==413


@pytest.mark.parametrize('withdrawal', ['revoked', 'expired'])
def test_admitted_signal_recovery_withdraws_provider_before_releasing_slot(tmp_path, withdrawal):
    async def proof():
        store,sources,identity,allowed=services(tmp_path)
        options=dict(chat_id='chat',collection='company',owner='alice',actor='alice',provider='timer',
            connection_id='timer',event_id='one',text='Check delivery',sender={'principal_id':'alice','channel':'timer'})
        first=store.wakes.enqueue_chat(**options);claim=store.wakes.claim_chat(first['id'],owner='alice')
        second=store.wakes.enqueue_chat(**{**options,'event_id':'two'})
        with store.connect() as db:
            record=store.wakes._get(db,first['id']);record['message_start']=0
            if withdrawal=='expired':record['expires_at']=time.time()-1
            store.wakes._write(db,record)
        if withdrawal=='revoked':allowed.clear()
        class Agent:
            active={};child_tasks={}
            children={'chat':{'id':'chat','owner_principal_id':'alice','dataset':'company',
                'messages':[{'role':'user','managed_request_id':'original'}]}}
            def _save_child(self,child):pass
        calls=[];outcomes=iter(['uncertain','cancelled'])
        class Runtime:
            async def recover_request(self,agent,chat_id,logical_id):
                calls.append((chat_id,logical_id))
                assert not store.wakes.signal_current(first['id'])
                assert agent.children[chat_id]['active_signal_id']==first['id']
                return {'status':next(outcomes),'turn_id':'original-provider-turn'}
        agent=Agent();agent.managed_runtime=Runtime()
        dispatcher=SignalDispatcher(store,agent,None,asyncio.Semaphore(2),sources)
        await dispatcher.recover_pending()
        assert calls==[('chat','original')]
        assert store.wakes.get(first['id'],owner='alice')['status']=='uncertain'
        assert agent.children['chat']['active_signal_id']==first['id']
        allowed.add('alice')
        assert store.wakes.claim_chat(second['id'],owner='alice') is None
        from test_standing_agents import saved,activate
        store.authority=lambda record,actor:True
        background=saved(store);activate(store,background,time.time())
        assert store.claim(background['id'],{}) is None
        if withdrawal=='revoked':allowed.clear()
        await dispatcher.recover_pending()
        assert calls==[('chat','original')]*2
        assert store.wakes.get(first['id'],owner='alice')['status']=='cancelled'
        assert 'active_signal_id' not in agent.children['chat']
        assert store.signals.usage()['turn']['used']==1
        allowed.add('alice')
        assert store.wakes.claim_chat(second['id'],owner='alice') is not None
    asyncio.run(proof())


def test_admitted_signal_without_request_identity_keeps_workspace_reserved(tmp_path):
    async def proof():
        store,sources,identity,allowed=services(tmp_path)
        options=dict(chat_id='chat',collection='company',owner='alice',actor='alice',provider='timer',
            connection_id='timer',event_id='one',text='Check delivery',sender={'principal_id':'alice','channel':'timer'})
        first=store.wakes.enqueue_chat(**options);store.wakes.claim_chat(first['id'],owner='alice')
        second=store.wakes.enqueue_chat(**{**options,'event_id':'two'})
        with store.connect() as db:
            record=store.wakes._get(db,first['id']);record['message_start']=0;store.wakes._write(db,record)
        class Runtime:
            async def recover_request(self,*args):raise AssertionError('Recovery requires the saved request identity')
        agent=SimpleNamespace(children={'chat':{'id':'chat','messages':[{'role':'user','content':'Unknown request'}]}},
            active={},child_tasks={},managed_runtime=Runtime(),_save_child=lambda child:None)
        dispatcher=SignalDispatcher(store,agent,None,asyncio.Semaphore(2),sources)
        await dispatcher.recover_pending()
        assert store.wakes.get(first['id'],owner='alice')['status']=='uncertain'
        assert agent.children['chat']['active_signal_id']==first['id']
        assert store.wakes.claim_chat(second['id'],owner='alice') is None
    asyncio.run(proof())


def test_lost_admitted_signal_keeps_fence_for_later_generic_recovery(tmp_path):
    async def proof():
        store,sources,identity,allowed=services(tmp_path)
        signal=store.wakes.enqueue_chat(chat_id='chat',collection='company',owner='alice',actor='alice',
            provider='timer',connection_id='timer',event_id='one',text='Check delivery',
            sender={'principal_id':'alice','channel':'timer','admission_key':'timer:one'})
        claim=store.wakes.claim_chat(signal['id'],owner='alice')
        class Agent:
            active={};child_tasks={}
            children={'chat':{'id':'chat','owner_principal_id':'alice','dataset':'company','messages':[]}}
            def _save_child(self,child):pass
            async def turn(self,text,chat_id,collection,**options):
                self.children[chat_id]['messages'].append({'role':'user','managed_request_id':'original',
                    'channel':options['sender']})
                raise TimeoutError('Provider cleanup remains uncertain')
                yield
        agent=Agent();dispatcher=SignalDispatcher(store,agent,None,asyncio.Semaphore(2),sources)
        await dispatcher._execute(claim)
        assert store.wakes.get(signal['id'],owner='alice')['status']=='uncertain'
        assert agent.children['chat']['active_signal_id']==signal['id']
        assert not store.wakes.signal_current(signal['id'])
    asyncio.run(proof())


def test_wake_tools_select_only_declared_chain_steps_and_keep_default_transparent(tmp_path):
    import wake_tools
    from tool_families import ToolRegistry
    store,sources,identity,allowed=services(tmp_path)
    schema={'type':'object','properties':{'threshold':{'type':'integer','minimum':1,'maximum':10}},
        'required':['threshold'],'additionalProperties':False}
    async def classify(payload,config,context):return {'deliver':payload.get('quantity',0)>=config['threshold']}
    store.signals.register('reviewed_screen',classify,kind='screen',config_schema=schema,description='Review quantity.')
    store.signals.register('private_script',classify,kind='script')
    agent=SimpleNamespace(signal_sources=sources,_turn_senders={})
    context=SimpleNamespace(agent=agent,chat_id='chat',actor='alice',data_owner='alice',collection='company',
        require_current=lambda:None,require_parent=lambda:None,record={})
    registry=ToolRegistry((wake_tools,))
    chain=[{'kind':'rules','all':[{'field':'quantity','op':'gte','value':1}]},
        {'kind':'screen','handler':'reviewed_screen','config':{'threshold':4}},{'kind':'deliver'}]
    args={'label':'Reviewed deliveries','chain':chain}
    registry.declared('create_webhook',args)
    hook=wake_tools.call(context,'create_webhook',args)
    assert hook['chain']==chain
    timer=wake_tools.call(context,'set_wake',{'label':'Reviewed reminder','note':'Reminder','chain':chain})
    assert timer['chain']==chain
    default=wake_tools.call(context,'create_webhook',{'label':'All deliveries'})
    assert default['chain']==[{'kind':'deliver'}]
    listed=wake_tools.read(context,'list_wakes',{})
    assert listed['steps']==[{'handler':'reviewed_screen','kind':'screen','description':'Review quantity.','config_schema':schema}]
    for bad in ([{'kind':'script','handler':'private_script'},{'kind':'deliver'}],
            [{'kind':'screen','handler':'unknown'},{'kind':'deliver'}],
            [{'kind':'screen','handler':'reviewed_screen','config':{'threshold':99}},{'kind':'deliver'}]):
        with pytest.raises(ValueError):wake_tools.call(context,'create_webhook',{'label':'Invalid','chain':bad})
    assert len(sources.list(actor='alice'))==3
