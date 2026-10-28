"""Trackers reach their own participants by phone; Action permissions stay with Action agents."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from mcp import types
sys.path.insert(0,str(Path(__file__).parents[1]))
from agent_kind_policy import has_participant_reach_context
from background_agents import AgentStore as Store

def AgentStore(state):
    return Store(state,agent_classes=[{'kind':'action','authority':'granted_effects'},
        {'kind':'tracker','authority':'participant_reach'}])
from agent_invocation import invoke
from test_agent_invocation import fixture as invocation_fixture
from test_native_mcp import fixture as native_fixture
from native_mcp import NativeMCP


def test_tracker_authority_follows_both_lineages_and_persisted_role(tmp_path):
    store=AgentStore(tmp_path)
    record=store.create({'title':'Observe','instruction':'Record observed events.','dataset':'cafe','agent_kind':'tracker'},['cafe'])
    agent=SimpleNamespace(automation_store=store,children={
        'root':{'agent_id':record['id'],'agent_kind':'action'},
        'child':{'parent_id':'root','agent_kind':'action'},
        'native':{'native_root_id':'child'},'ordinary':{}})
    assert has_participant_reach_context(agent,'root') and has_participant_reach_context(agent,'child')
    assert has_participant_reach_context(agent,'native') and not has_participant_reach_context(agent,'ordinary')


@pytest.mark.parametrize('kind,other',[('tracker','action'),('action','tracker')])
def test_saved_role_is_immutable_and_legacy_edits_preserve_it(tmp_path,kind,other):
    store=AgentStore(tmp_path);body={'title':'Observe','instruction':'Record events.','dataset':'cafe'}
    saved=store.create({**body,'agent_kind':kind},['cafe'])
    with pytest.raises(ValueError,match='original agent class'):
        store.update(saved['id'],{**body,'agent_kind':other},['cafe'],expected_revision=1)
    assert store.get(saved['id'])['revision']==1
    revised=store.update(saved['id'],{**body,'instruction':'Record product events.'},['cafe'],expected_revision=1)
    assert revised['agent_kind']==kind and revised['revision']==2


def test_tracker_cannot_start_an_action_even_with_valid_foreground_grant():
    agent,saved,calls=invocation_fixture()
    agent.children['source']['agent_kind']='tracker';saved['agent_kind']='action'
    with pytest.raises(ValueError,match='granted external actions'):
        invoke(agent,'source',{'agent_id':'agent-a','expected_revision':1},now=100)
    assert calls==[]
    saved['agent_kind']='tracker'
    assert invoke(agent,'source',{'agent_id':'agent-a','expected_revision':1},now=100)['status']=='active'
    assert len(calls)==1


class Index:
    def status(self):return {'ready':True,'datasets':['cafe']}


class Participants:
    """The gate's seam to the participant store: the host resolves the recipient."""
    def __init__(self,members):self.members=set(members);self.calls=[];self.generation=1
    def recipient(self,agent_id,chat_id,member_id,*,channel=None,host=False):
        self.calls.append((agent_id,chat_id,member_id,channel,host))
        if member_id not in self.members:raise ValueError('A Tracking Agent’s phone messages reach its own participants.')
        return {'agent_id':agent_id,'chat_id':chat_id,'member_id':member_id,'channel':channel or 'whatsapp','generation':self.generation}


def phone_fixture(tmp_path,*,kind='tracker',members=('alice',),sender='alice',channel='web'):
    from agent_bridge import CodexVideoAgent
    agent=CodexVideoAgent(tmp_path,Index(),lambda:{});calls=[]
    def bind(actor,**options):calls.append(('bind',actor.member_id,actor.actor_id,actor.chat_id,options.get('channel')))
    async def send(actor,*a,**k):
        calls.append(('send',actor.member_id,actor.actor_id,actor.chat_id,k.get('channel')))
        return {'waiting_for_reply':True,'status':'sent'}
    agent.messaging_store=SimpleNamespace(company_id='test',bind_conversation=bind,send_message=send,send_alert=send)
    agent.messaging_member=lambda principal:{'id':principal,'role':'operator'} if principal in {'alice','bob','mallory'} else None
    agent.phone_chat=Participants(members)
    agent.automation_store=SimpleNamespace(agent_classes={
        'tracker':{'authority':'participant_reach'},'action':{'authority':'granted_effects'}},
        get=lambda key:{'agent_kind':agent.children['root']['agent_kind']})
    from retail_host import RetailWorkspace
    agent.vertical=RetailWorkspace(None,{'agent':agent})
    agent.children['root']={'id':'root','owner_principal_id':'alice','dataset':'cafe','agent_kind':kind,'agent_id':'agent-1','messages':[]}
    agent.active['root']={'cancel':asyncio.Event(),'generation':'turn-one'}
    agent._turn_senders={'root':{'principal_id':sender,'channel':channel}}
    return agent,calls


MESSAGE_TOOLS=[('send_message',{'text':'Summary'}),('send_alert',{'purpose':'monitoring','language':'en_US','parameters':['Queue','Six people','09:00']})]


@pytest.mark.parametrize('name,arguments',MESSAGE_TOOLS)
def test_tracker_chat_binds_and_dispatches_to_its_own_verified_participant(tmp_path,name,arguments):
    async def scenario():
        agent,calls=phone_fixture(tmp_path)
        result=await agent._scoped_tool(name,arguments,'cafe','root')
        assert result[2],result
        # The recipient is the admitted turn sender, resolved by the host.
        assert calls==[('bind','alice','alice','root','whatsapp'),('send','alice','alice','root','whatsapp')]
        assert set(agent.phone_chat.calls)=={('agent-1','root','alice',None,False)}
    asyncio.run(scenario())


@pytest.mark.parametrize('name,arguments',MESSAGE_TOOLS)
def test_tracker_answers_a_phone_reply_on_the_channel_it_arrived_on(tmp_path,name,arguments):
    async def scenario():
        agent,calls=phone_fixture(tmp_path,members=('alice','bob'),sender='bob',channel='sms')
        result=await agent._scoped_tool(name,arguments,'cafe','root')
        assert result[2],result
        assert calls==[('bind','bob','bob','root','sms'),('send','bob','bob','root','sms')]
        assert set(agent.phone_chat.calls)=={('agent-1','root','bob','sms',False)}
    asyncio.run(scenario())


def test_scheduled_tracker_turn_uses_the_creators_binding(tmp_path):
    async def scenario():
        agent,calls=phone_fixture(tmp_path)
        agent.children['root']['active_run_id']='run-1'
        agent.phone_run_lane=lambda chat_id:'live'
        result=await agent._scoped_tool('send_message',{'text':'Summary'},'cafe','root')
        assert result[2],result
        assert set(agent.phone_chat.calls)=={('agent-1','root','alice',None,True)}
    asyncio.run(scenario())


@pytest.mark.parametrize('lane',['replay','unknown','unnamed','unreadable'])
def test_a_host_run_reading_replayed_observations_sends_no_phone_message(tmp_path,lane):
    """The compiled-data replay lane reaches zero phones, also through the turn it wakes (CHANNELS.md, "Zero-send cases")."""
    async def scenario():
        agent,calls=phone_fixture(tmp_path)
        agent.children['root']['active_run_id']='run-1'
        def unreadable(chat_id):raise OSError('the run record left')
        if lane=='unreadable':agent.phone_run_lane=unreadable
        elif lane!='unnamed':agent.phone_run_lane=lambda chat_id:lane  # 'unnamed': the host named no lane at all
        for name,arguments in (('send_message',{'text':'Summary'}),('send_alert',{'purpose':'monitoring','language':'en_US','parameters':['a','b','c']})):
            refused=await agent._scoped_tool(name,arguments,'cafe','root')
            assert (refused[2],refused[0][0]['text'])==(False,'This run reads replayed observations. Phone alerts follow live observations.')
        assert calls==[] and agent.phone_chat.calls==[]
        # The same conversation's own turn, started by its participant, is live: a person is writing.
        del agent.children['root']['active_run_id']
        result=await agent._scoped_tool('send_message',{'text':'Summary'},'cafe','root')
        assert result[2] and [call[0] for call in calls]==['bind','send']
    asyncio.run(scenario())


def test_a_refused_recipient_leaves_the_turns_one_send_unused(tmp_path):
    async def scenario():
        agent,calls=phone_fixture(tmp_path,members=())
        refused=await agent._scoped_tool('send_message',{'text':'Summary'},'cafe','root')
        assert not refused[2] and calls==[] and 'its own participants' in refused[0][0]['text']
        agent.phone_chat.members.add('alice')
        result=await agent._scoped_tool('send_message',{'text':'Summary'},'cafe','root')
        assert result[2],result
        assert [call[0] for call in calls]==['bind','send']
        # One send per turn: the saved receipt answers any further attempt.
        again=await agent._scoped_tool('send_message',{'text':'Again'},'cafe','root')
        assert not again[2] and [call[0] for call in calls]==['bind','send']
    asyncio.run(scenario())


def test_action_chat_keeps_its_own_member_messaging(tmp_path):
    async def scenario():
        agent,calls=phone_fixture(tmp_path,kind='action')
        result=await agent._scoped_tool('send_message',{'text':'Summary'},'cafe','root')
        assert result[2],result
        assert [call[0] for call in calls]==['bind','send'] and agent.phone_chat.calls==[]
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['outside_member','recipient_argument','member_argument','descendant','store_missing','unsaved_tracker','former_member'])
def test_tracker_context_cannot_address_anyone_outside_its_participants(tmp_path,case):
    async def scenario():
        agent,calls=phone_fixture(tmp_path,sender='mallory' if case=='outside_member' else 'alice')
        arguments={'text':'Summary'};chat='root'
        if case=='recipient_argument':arguments['to']='+359888000000'
        elif case=='member_argument':arguments['member_id']='bob'
        elif case=='descendant':
            agent.children['child']={'id':'child','parent_id':'root','native_root_id':'root','owner_principal_id':'alice','dataset':'cafe','messages':[]}
            agent.active['child']=agent.active['root'];agent._turn_senders['child']={'principal_id':'alice','channel':'web'};chat='child'
        elif case=='store_missing':del agent.phone_chat
        elif case=='unsaved_tracker':del agent.children['root']['agent_id']
        elif case=='former_member':agent.messaging_member=lambda principal:None
        result=await agent._tool('send_message',arguments,'cafe',chat)
        assert not result[2] and calls==[]
        if case in {'outside_member','store_missing'}:
            assert 'its own participants' in result[0][0]['text']
    asyncio.run(scenario())


class Relay:
    def __init__(self,agent,race=False):self.calls=[];self.agent=agent;self.race=race
    async def valid(self,binding):return True
    async def operation_kind(self,binding,name):return 'write' if name=='change' else 'read'
    async def call(self,binding,name,arguments,**kwargs):
        if self.race:self.agent.children['root']['agent_kind']='tracker'
        if kwargs.get('alive') and not kwargs['alive']():return types.CallToolResult(isError=True,content=[])
        self.calls.append(name)
        return types.CallToolResult(isError=False,content=[types.TextContent(type='text',text='done')])


@pytest.mark.parametrize('actor',['root','child'])
def test_native_provider_write_denied_for_tracker_descendants_reads_preserved(actor):
    async def scenario():
        agent,scope,_=native_fixture();agent.children['root']['agent_kind']='tracker'
        app=NativeMCP(agent,scope,company_id='rocoto');relay=Relay(agent);app.provider_relay=relay
        app.issue(actor_chat_id=actor,actor_thread_id=agent.registry[actor])
        binding=next(iter(app._bindings.values()));binding.provider=object()
        result=await app._call(binding,'change',{})
        assert result.isError and relay.calls==[]
        result=await app._call(binding,'read',{})
        assert not result.isError and relay.calls==['read']
        agent.children['root']['agent_kind']='action'
        result=await app._call(binding,'change',{})
        assert not result.isError and relay.calls==['read','change']
    asyncio.run(scenario())


def test_native_provider_alive_guard_rechecks_tracker_authority_at_dispatch():
    async def scenario():
        agent,scope,_=native_fixture();app=NativeMCP(agent,scope,company_id='rocoto')
        relay=Relay(agent,race=True);app.provider_relay=relay
        app.issue();binding=next(iter(app._bindings.values()));binding.provider=object()
        result=await app._call(binding,'change',{})
        assert result.isError and relay.calls==[]
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['participant_removed','binding_changed','membership_ended','becomes_tracker'])
def test_messaging_rechecks_participant_binding_and_member_authority_between_bind_and_send(tmp_path,change):
    async def scenario():
        agent,calls=phone_fixture(tmp_path,kind='action' if change=='becomes_tracker' else 'tracker',
            members=() if change=='becomes_tracker' else ('alice',))
        sent=[]
        def bind(actor,**options):
            if change=='participant_removed':agent.phone_chat.members.discard('alice')
            elif change=='binding_changed':agent.phone_chat.generation+=1
            elif change=='membership_ended':agent.messaging_member=lambda principal:None
            else:agent.children['root']['agent_kind']='tracker'
        async def send(*a,**k):sent.append('send');return {'waiting_for_reply':True,'status':'sent'}
        agent.messaging_store=SimpleNamespace(company_id='test',bind_conversation=bind,send_message=send)
        result=await agent._scoped_tool('send_message',{'text':'Summary'},'cafe','root')
        assert not result[2] and sent==[]
    asyncio.run(scenario())


def test_custom_declared_authority_follows_saved_class_and_both_ancestries(tmp_path):
    from agent_kind_policy import has_participant_reach_context
    store=Store(tmp_path,agent_classes=[{'kind':'observer','authority':'participant_reach'},
        {'kind':'fulfillment','authority':'granted_effects'}])
    saved=store.create({'title':'Observe jobs','instruction':'Report changes.','dataset':'company','agent_kind':'observer'},
        ['company'],owner_principal_id='alice')
    agent=SimpleNamespace(automation_store=store,children={
        'root':{'agent_id':saved['id'],'agent_kind':'fulfillment'},'child':{'parent_id':'root'},
        'native':{'native_root_id':'child'},'ordinary':{},'granted':{'agent_kind':'fulfillment'}})
    assert all(has_participant_reach_context(agent,key) for key in ('root','child','native'))
    assert not has_participant_reach_context(agent,'ordinary')
    assert not has_participant_reach_context(agent,'granted')
    del store.agent_classes['fulfillment']
    assert has_participant_reach_context(agent,'granted')


@pytest.mark.parametrize('records',[
    {'root':{'agent_kind':'unknown'}}, {'root':{'agent_id':'missing'}},
    {'root':{'parent_id':'missing'}}, {'root':{'parent_id':'root'}},
    {'root':{'parent_id':'child'},'child':{'native_root_id':'root'}}])
def test_unknown_class_or_incomplete_ancestry_keeps_effects_restricted(records):
    from agent_kind_policy import has_participant_reach_context
    agent=SimpleNamespace(children=records)
    assert has_participant_reach_context(agent,'root')


def test_custom_participant_class_can_invoke_only_same_declared_authority():
    agent,saved,calls=invocation_fixture()
    agent.automation_store.agent_classes={'observer':{'authority':'participant_reach'},
        'fulfillment':{'authority':'granted_effects'}}
    agent.children['source']['agent_kind']='observer';saved['agent_kind']='fulfillment'
    with pytest.raises(ValueError):invoke(agent,'source',{'agent_id':'agent-a','expected_revision':1},now=100)
    assert calls==[]
    saved['agent_kind']='observer'
    assert invoke(agent,'source',{'agent_id':'agent-a','expected_revision':1},now=100)['status']=='active'


def test_participant_replies_use_the_workspace_declared_channels(tmp_path):
    from agent_kind_policy import participant_recipient, messaging_channels
    for channel in ('email','voice'):
        agent,calls=phone_fixture(tmp_path/channel,channel=channel)
        agent.messaging_store.channel_names=(channel,)
        assert messaging_channels(agent)==(channel,)
        recipient=participant_recipient(agent,'root',agent._turn_senders['root'])
        assert recipient['channel']==channel and agent.phone_chat.calls[-1][3]==channel


def test_generic_participant_host_run_uses_shared_delivery_authority(tmp_path):
    from agent_kind_policy import participant_recipient
    from host import BareWorkspace
    agent,calls=phone_fixture(tmp_path,kind='order_observer')
    agent.automation_store.agent_classes['order_observer']={'authority':'participant_reach'}
    agent.vertical=BareWorkspace({});agent.children['root']['active_run_id']='run-1'
    assert has_participant_reach_context(agent,'root')
    recipient=participant_recipient(agent,'root',agent._turn_senders['root'])
    assert recipient['member_id']=='alice' and agent.phone_chat.calls[-1][-1] is True
    agent.vertical=SimpleNamespace()
    with pytest.raises(ValueError):participant_recipient(agent,'root',agent._turn_senders['root'])


def test_custom_participant_class_blocks_native_connected_account_write():
    async def scenario():
        agent,scope,_=native_fixture()
        agent.automation_store.agent_classes['order_observer']={'authority':'participant_reach'}
        agent.children['root']['agent_kind']='order_observer'
        app=NativeMCP(agent,scope,company_id='rocoto');relay=Relay(agent);app.provider_relay=relay
        app.issue(actor_chat_id='child',actor_thread_id=agent.registry['child'])
        binding=next(iter(app._bindings.values()));binding.provider=object()
        assert (await app._call(binding,'change',{})).isError and relay.calls==[]
        assert not (await app._call(binding,'read',{})).isError and relay.calls==['read']
    asyncio.run(scenario())
