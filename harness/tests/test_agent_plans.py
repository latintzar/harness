"""Main-chat tool order is enforced before fresh provider work can be admitted."""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent
from test_investigation_plans import Index, document


def test_main_chat_requires_context_plan_and_retrieval_before_verification(tmp_path, monkeypatch):
    index=Index(tmp_path)
    state=tmp_path/'state';state.mkdir()
    agent=CodexVideoAgent(state,index,lambda:[])
    agent.active['chat-a']={}
    provider=AsyncMock()
    monkeypatch.setattr('cosmos_inspect.inspect_live',provider)

    async def call(name,args):
        return await agent._tool(name,args,'cafe','chat-a')

    async def scenario():
        assert (await call('search_video',{'query':'person waiting'}))[2] is False
        assert (await call('plan_investigation',document()))[2] is False
        assert index.encoded==[]
        assert (await call('get_business_context',{'dataset':'warehouse'}))[2] is False
        assert (await call('get_business_context',{}))[2] is True
        content,_,success=await call('plan_investigation',document())
        assert success
        plan=json.loads(content[0]['text'])
        assert (await call('search_video',{'query':'person waiting'}))[2] is False
        assert (await call('cosmos_inspect',{'clip_ids':['clip-0'],'question':'What is visible?',
                                          'category_ids':['service']}))[2] is False
        provider.assert_not_awaited()
        content,results,success=await call('retrieve_investigation',{'plan_id':plan['id'],'limit_per_query':2})
        assert success and len(results)==3
        assert json.loads(content[0]['text'])['coverage']['scored_window_count']==3
        assert (await call('search_video',{'query':'person waiting'}))[2] is True
        assert (await call('cosmos_inspect',{'clip_ids':['clip-0'],'question':'What is visible?',
                                          'category_ids':['unknown']}))[2] is False
        assert (await call('cosmos_inspect',{'clip_ids':['clip-0'],'question':'What is visible?',
                                          'category_ids':['service','service']}))[2] is False
        provider.assert_not_awaited()
        provider.return_value={'model':'fixture-reasoner','model_revision':'revision',
            'requests':[{'clip_ids':['clip-0'],'question':'What is visible?',
                         'final_text':'Fixture observation','parsed':True}]}
        content,_,success=await call('cosmos_inspect',{'clip_ids':['clip-0'],'question':'What is visible?',
                                                     'category_ids':['service']})
        assert success
        receipt=json.loads(content[0]['text'])['investigation']['inspection_receipt']
        recorded=agent._plans().get_inspection(receipt,'cafe','chat-a')
        assert recorded['evidence_type']=='model_observations'
        assert recorded['plan_id']==plan['id']
        provider.assert_awaited_once()
    asyncio.run(scenario())


def test_direct_plan_scope_and_cross_chat_plan_checks_precede_provider_work(tmp_path, monkeypatch):
    index=Index(tmp_path)
    state=tmp_path/'state';state.mkdir()
    agent=CodexVideoAgent(state,index,lambda:[])
    agent.active['chat-a']={}
    provider=AsyncMock()
    monkeypatch.setattr('cosmos_inspect.inspect_live',provider)

    async def scenario():
        await agent._tool('get_business_context',{},'cafe','chat-a')
        body=document();body['direct_clip_ids']=['clip-0']
        content,_,success=await agent._tool('plan_investigation',body,'cafe','chat-a')
        assert success
        plan=json.loads(content[0]['text'])
        assert (await agent._tool('cosmos_inspect',{'clip_ids':['clip-1'],'question':'What is visible?',
                        'category_ids':['service']},'cafe','chat-a'))[2] is False
        provider.assert_not_awaited()
        agent.active['chat-b']={'plan_id':plan['id'],'business_context_read':True}
        assert (await agent._tool('retrieve_investigation',{'plan_id':plan['id']},'cafe','chat-b'))[2] is False
        assert (await agent._tool('retrieve_investigation',{'plan_id':plan['id']},'warehouse','chat-a'))[2] is False
    asyncio.run(scenario())
