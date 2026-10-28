"""Host clarification tool exercises the real durable waiter, without model traffic."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import tool_specs
from native_questions import NativeQuestions
from test_credential_requests import agent_fixture

ARGS={'questions':[{'id':'scope','header':'Scope','question':'Which sources should I review?',
    'multiple':True,'allow_free_text':True,'options':[{'label':'Counter','description':'Ordering.'},
    {'label':'Seating','description':'Tables.'}]}]}

async def started(tmp_path):
    agent,store,saved,run=agent_fixture(tmp_path);run['generation']='run:clarification'
    task=asyncio.create_task(agent._tool('request_clarification',deepcopy(ARGS),'warehouse','chat'))
    event=await asyncio.wait_for(run['tool_events'].get(),2)
    assert event['type']=='user_input_request' and event['request']['questions'][0]['multiple']
    assert run['status']=='waiting_for_answer' and not task.done()
    return agent,store,saved,run,task,event['request']

@pytest.mark.parametrize('action',['submit','skip'])
def test_host_tool_resumes_with_exact_outcome_and_no_native_protocol_write(tmp_path,action):
    async def scenario():
        agent,_,_,run,task,record=await started(tmp_path)
        answers={'scope':['Counter','Seating']} if action=='submit' else None
        result=await agent.resolve_native_question('chat',record['id'],owner='alice',action=action,answers=answers)
        parts,_,success=await asyncio.wait_for(task,2);result=json.loads(parts[0]['text'])
        assert success and result['status']==('submitted' if action=='submit' else 'skipped')
        assert result['answers']==(answers if action=='submit' else {})
        assert run['status']=='running' and not agent.clarification_waiters
        replay=await agent.resolve_native_question('chat',record['id'],owner='alice',action=action,answers=answers)
        assert replay['status']==result['status']
        assert agent.children['chat']['messages'][-1]['native_questions'][0]['status']==result['status']
    asyncio.run(scenario())


def test_host_deadline_fires_without_browser_and_tool_returns_timed_out(tmp_path):
    async def scenario():
        agent,_,_,run,task,record=await started(tmp_path)
        with agent.native_questions._db() as db:
            db.execute('UPDATE questions SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat(),record['id']))
        parts,_,success=await asyncio.wait_for(task,2)
        assert success and json.loads(parts[0]['text'])=={'request_id':record['id'],'status':'timed_out','answers':{}}
        assert run['status']=='running'
    asyncio.run(scenario())


def test_native_timer_excludes_host_waiters_and_owner_generation_are_bound(tmp_path):
    async def scenario():
        agent,_,_,run,task,record=await started(tmp_path)
        with pytest.raises(ValueError):await agent.resolve_native_question('chat',record['id'],owner='bob',action='skip')
        sent=[]
        async def deliver(*args):sent.append(args)
        assert await agent.native_questions.expire(owner='alice',root_chat_id='chat',generation='run:clarification',deliver=deliver,cancelled=run['cancel'])==[]
        assert sent==[]
        run['cancel'].set();parts,_,success=await asyncio.wait_for(task,2)
        assert not success and json.loads(parts[0]['text'])['status']=='interrupted'
        assert not agent.clarification_waiters
    asyncio.run(scenario())


def test_cancelled_host_tool_retires_exact_request_and_blocks_late_answer(tmp_path):
    async def scenario():
        agent,_,_,run,task,record=await started(tmp_path);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        result=await agent.resolve_native_question('chat',record['id'],owner='alice',action='submit',answers={'scope':['Counter']})
        assert result['status']=='interrupted' and not agent.clarification_waiters
        assert run['status']=='running'
    asyncio.run(scenario())


def test_tool_contract_bounds_and_child_authority(tmp_path):
    async def scenario():
        specs={item['name']:item for item in tool_specs(True)}
        assert specs['request_clarification']['inputSchema']['properties']['questions']['maxItems']==3
        assert 'request_clarification' not in {item['name'] for item in tool_specs(False)}
        agent,_,_,run=agent_fixture(tmp_path);run['generation']='one'
        for args in [{'questions':[]},{**ARGS,'timeout':600}, {'questions':[{**ARGS['questions'][0],'secret':True}]}]:
            parts,_,success=await agent._tool('request_clarification',args,'warehouse','chat')
            assert not success
        agent.children['chat']['parent_id']='parent'
        _,_,success=await agent._tool('request_clarification',ARGS,'warehouse','chat');assert not success
        assert not agent.native_questions.for_chat('chat',owner='alice')
    asyncio.run(scenario())


def test_private_credential_wait_times_out_without_browser_and_preserves_secret_store(tmp_path):
    async def scenario():
        agent,store,saved,run=agent_fixture(tmp_path)
        task=asyncio.create_task(agent._tool('request_custom_connector_credentials',{'connector_id':saved['id'],'reason':'Read delivery records.'},'warehouse','chat'))
        record=(await asyncio.wait_for(run['tool_events'].get(),2))['request']
        assert 28<= (datetime.fromisoformat(record['expires_at'])-datetime.now(timezone.utc)).total_seconds()<=30
        with store.db() as db:
            record['expires_at']='2020-01-01T00:00:00+00:00';store._save_request(db,record)
        parts,_,success=await asyncio.wait_for(task,2)
        assert success and json.loads(parts[0]['text'])['status']=='timed_out'
        assert not any(field['configured'] for field in store.get(saved['id'],owner='alice')['secret_fields'])
    asyncio.run(scenario())


def test_actual_native_dynamic_tool_path_resumes_and_children_cannot_ask(tmp_path):
    from test_isolated_turn import tree_fixture, event
    async def scenario():
        agent,run,connection,tree=tree_fixture(tmp_path)
        await tree.event({'id':41,**event('item/tool/call',tool='request_clarification',arguments=deepcopy(ARGS))})
        while True:
            notice=await asyncio.wait_for(run['tool_events'].get(),2)
            if notice['type']=='user_input_request':record=notice['request'];break
        await agent.resolve_native_question('chat',record['id'],owner='alice',action='submit',answers={'scope':['Counter','Seating']})
        await asyncio.wait_for(tree.tools[41],2)
        assert len(connection.writes)==1 and connection.writes[0]['id']==41
        reply=connection.writes[0]['result'];assert reply['success']
        assert json.loads(reply['contentItems'][0]['text'])['answers']=={'scope':['Counter','Seating']}
        connection.child('child')
        await tree.event({'id':42,**event('item/tool/call','child',tool='request_clarification',arguments=deepcopy(ARGS))})
        assert connection.writes[-1]['result']['success'] is False
        assert len(agent.native_questions.for_chat('chat',owner='alice'))==1
        await tree.close()
    asyncio.run(scenario())
