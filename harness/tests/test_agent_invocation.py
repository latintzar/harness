"""Foreground invocation must preserve existing saved-agent authority and limits."""
from agent_inputs import NoInputProvider
import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from agent_invocation import invoke


def fixture():
    saved={'id':'agent-a','revision':1,'owner_principal_id':'alice','dataset':'cafe','chat_id':'target','fresh_inspections_per_run':0}
    calls=[]
    def activate(key, expiry, **kwargs):
        calls.append((key,expiry,kwargs));return dict(saved,status='active')
    agent=SimpleNamespace(active={'source':{'cancel':asyncio.Event()}},
        children={'source':{'owner_principal_id':'alice','dataset':'cafe'}},
        invocation_grants={'source':{'principal':'alice','expires_at':200}},
        messaging_member=lambda principal:{'role':'operator'} if principal=='alice' else None,
        automation_store=SimpleNamespace(get=lambda key:saved,activate=activate,inputs=NoInputProvider(),
            agent_classes={'tracker':{'authority':'participant_reach'},'action':{'authority':'granted_effects'}}),
        retrieval=SimpleNamespace(status=lambda:{'ready':True}),
        workspace_capabilities=SimpleNamespace(execution=lambda definition:{'ready':True}))
    return agent,saved,calls


def test_chat_invokes_one_bounded_run_and_retry_returns_receipt():
    agent,saved,calls=fixture();args={'agent_id':'agent-a','expected_revision':1}
    result=invoke(agent,'source',args,now=100)
    assert result['status']=='active'
    assert invoke(agent,'source',args,now=100)==result
    assert len(calls)==1 and calls[0][2]['once'] is True and calls[0][1]==200


@pytest.mark.parametrize('change',['expired','other_owner','other_collection','revision','child','stopped','revoked','same_chat'])
def test_invocation_rechecks_authority_before_activation(change):
    agent,saved,calls=fixture()
    if change=='expired':agent.invocation_grants['source']['expires_at']=99
    if change=='other_owner':saved['owner_principal_id']='bob'
    if change=='other_collection':saved['dataset']='warehouse'
    if change=='revision':saved['revision']=2
    if change=='child':agent.children['source']['parent_id']='parent'
    if change=='stopped':agent.active['source']['cancel'].set()
    if change=='revoked':agent.messaging_member=lambda _:None
    if change=='same_chat':saved['chat_id']='source'
    with pytest.raises(ValueError):invoke(agent,'source',{'agent_id':'agent-a','expected_revision':1},now=100)
    assert calls==[]


def test_native_tool_starts_saved_agent_conversation_once_and_scheduler_finishes(tmp_path):
    import json
    import time
    from agent_bridge import CodexVideoAgent, tool_specs
    from background_agents import AgentStore, AgentScheduler
    class Index:
        clips = [{'id':'clip-a','video_id':'source-a','dataset':'cafe','start':0,'end':8}]
        def status(self): return {'ready':True,'datasets':['cafe']}
    async def scenario():
        index=Index();agent=CodexVideoAgent(tmp_path,index,lambda:{})
        store=AgentStore(tmp_path);agent.automation_store=store
        agent.workspace_capabilities=SimpleNamespace(execution=lambda saved:{'ready':True})
        agent.messaging_member=lambda principal:{'role':'operator'} if principal=='alice' else None
        scheduler=AgentScheduler(store,agent,asyncio.Semaphore(1))
        saved=store.create({'title':'Counter activity','instruction':'Review the saved handoff evidence.',
            'dataset':'cafe','mode':'ad_hoc'},['cafe'],owner_principal_id='alice')
        agent.children['origin']={'id':'origin','owner_principal_id':'alice','dataset':'cafe','messages':[]}
        agent.active['origin']={'cancel':asyncio.Event(),'generation':'origin-turn'}
        agent.invocation_grants={'origin':{'principal':'alice','expires_at':time.time()+120}}
        arguments={'agent_id':saved['id'],'expected_revision':saved['revision']}
        parts,_,ok=await agent._tool('invoke_agent',arguments,'cafe','origin')
        assert ok, parts
        value=json.loads(parts[0]['text']);assert value['status']=='active'
        card=agent.active['origin']['tool_events'].get_nowait()
        assert card['agent']['chat_id']==saved['chat_id']
        assert 'invoke_agent' not in {item['name'] for item in tool_specs(False)}
        assert (await agent._tool('invoke_agent',arguments,'cafe','origin'))[2]
        assert agent.children[saved['chat_id']]['agent_id']==saved['id']
        assert agent.children[saved['chat_id']]['owner_principal_id']=='alice'
        async def turn(message,chat_id,collection,**kwargs):
            assert chat_id==saved['chat_id'] and collection=='cafe'
            agent.children[chat_id]['messages'].append({'role':'assistant','content':'Review completed.'})
            yield {'type':'done','status':'completed'}
        agent.turn=turn
        await scheduler.tick()
        await asyncio.gather(*scheduler.tasks.values())
        await asyncio.sleep(0)
        result=store.get(saved['id'])
        assert len(result['runs'])==1 and result['runs'][0]['status']=='completed'
        assert result['status']=='completed'
        await scheduler.tick()
        assert len(store.get(saved['id'])['runs'])==1
    asyncio.run(scenario())


@pytest.mark.parametrize('terminal,expected', [('waiting_for_reply','completed'), ('delivery_needs_attention','interrupted')])
def test_scheduler_preserves_successful_alert_waiting_state(tmp_path, terminal, expected):
    import time
    from background_agents import AgentStore, AgentScheduler
    class Index:
        clips=[{'id':'clip-a','video_id':'source-a','dataset':'cafe','start':0,'end':8}]
    class Harness:
        def __init__(self):self.children,self.child_tasks,self.active={},{},{}
        def _save_child(self,child):pass
        async def turn(self,message,chat_id,dataset,**options):
            self.children[chat_id]['waiting_for_reply']=terminal=='waiting_for_reply'
            self.children[chat_id]['messages'].append({'role':'assistant','content':'Alert sent.','status':terminal})
            yield {'type':'done','status':terminal}
    async def scenario():
        store=AgentStore(tmp_path);agent=Harness()
        scheduler=AgentScheduler(store,agent,asyncio.Semaphore(1))
        saved=store.create({'title':'Weekly summary','instruction':'Send the configured weekly summary.',
            'dataset':'cafe','mode':'scheduled','interval_seconds':60,'max_runs':3},['cafe'],owner_principal_id='alice')
        store.activate(saved['id'],time.time()+120)
        await scheduler.tick();await asyncio.gather(*scheduler.tasks.values());await asyncio.sleep(0)
        value=store.get(saved['id'])
        assert value['runs'][0]['status']==expected
        assert value['status']==('active' if expected=='completed' else 'needs_attention')
        assert agent.children[saved['chat_id']]['waiting_for_reply']==(expected=='completed')
    asyncio.run(scenario())
