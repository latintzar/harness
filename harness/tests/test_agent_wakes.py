"""Authenticated host events retain scope, idempotency, grants, and pending work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
from background_agents import AgentStore, AgentScheduler
from test_background_agents import definition
from agent_inputs import NoInputProvider
from retail_agent_inputs import RetailInputProvider


def setup(tmp_path):
    store=AgentStore(tmp_path)
    record=store.create(definition(), ['cafe'], owner_principal_id='person-a')
    store.activate(record['id'],time.time()+1000)
    return store,record


def event(store,record,**changes):
    return store.wakes.enqueue(record['id'], **{'owner':'person-a','expected_revision':1,
        'provider':'fixture','connection_id':'fixture-account-1','event_id':'event-1',**changes})


def test_duplicate_concurrent_events_queue_and_claim_once(tmp_path):
    store,record=setup(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        events=list(pool.map(lambda _:event(store,record),range(4)))
    assert len({item['id'] for item in events})==1
    run=store.claim(record['id'], {}, wake_id=events[0]['id'])
    assert run['wake']['id']==events[0]['id'] and run['trigger']=='event'
    assert store.wakes.get(events[0]['id'],owner='person-a')['status']=='claimed'
    store.finish(run['id'],'completed',now=time.time()-100)
    assert store.claim(record['id'],{},wake_id=events[0]['id']) is None
    assert event(store,record)['status']=='completed'
    assert store.get(record['id'])['runs_started']==1
    with pytest.raises(ValueError):event(store,record,text='Changed replay')


def test_wakes_require_current_owner_revision_and_verified_reply_metadata(tmp_path):
    store,record=setup(tmp_path)
    for changes in ({'owner':'person-b'}, {'expected_revision':2}, {'kind':'channel_reply'}, {'provider':'bad/provider'}):
        with pytest.raises(ValueError):event(store,record,**changes)
    wake=event(store,record,kind='channel_reply',sender_id='sender-1',channel_id='thread-2',text='Check the loading area.')
    assert wake['sender_id']=='sender-1' and wake['channel_id']=='thread-2'
    with pytest.raises(ValueError):store.wakes.get(wake['id'],owner='person-b')
    assert AgentStore(tmp_path).wakes.get(wake['id'],owner='person-a')['status']=='pending'


@pytest.mark.parametrize('action',['pause','revise','recover','expire'])
def test_revocation_retires_pending_wakes_without_retrying(tmp_path,action):
    store,record=setup(tmp_path);wake=event(store,record)
    if action=='recover':store.recover()
    elif action=='expire':assert store.wakes.pending(record['id'],owner='person-a',now=time.time()+1100) is None
    else:
        store.pause(record['id'])
        if action=='revise':store.update(record['id'],definition(title='Changed'),['cafe'])
    assert store.wakes.get(wake['id'],owner='person-a')['status']=='cancelled'
    assert store.claim(record['id'],{},wake_id=wake['id']) is None
    assert store.get(record['id'])['runs_started']==0


def test_busy_or_unready_scheduler_leaves_event_pending(tmp_path):
    class Harness:
        def __init__(self):self.children,self.child_tasks,self.active={},{},{}
        def _save_child(self,child):pass
        async def turn(self,*args,**kwargs):yield {'type':'done','status':'completed'}
    async def prove():
        ready=[False]
        class ReadyInput(NoInputProvider):
            def ready(self,*args):return ready[0]
        store=AgentStore(tmp_path,input_provider=ReadyInput());harness=Harness()
        scheduler=AgentScheduler(store,harness,asyncio.Semaphore(2))
        record=store.create(definition(),['cafe'],owner_principal_id='person-a')
        store.activate(record['id'],time.time()+1000);wake=event(store,record)
        await scheduler.tick()
        assert store.wakes.get(wake['id'],owner='person-a')['status']=='pending'
        ready[0]=True;harness.active[record['chat_id']]={}
        await scheduler.tick()
        assert store.wakes.get(wake['id'],owner='person-a')['status']=='pending'
        harness.active.clear()
        await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
        assert store.wakes.get(wake['id'],owner='person-a')['status']=='completed'
        assert store.get(record['id'])['last_run']['wake']['id']==wake['id']
    asyncio.run(prove())


def test_claimed_wake_interrupted_on_restart_and_allowance_blocks_other_events(tmp_path):
    store,record=setup(tmp_path)
    wake=event(store,record)
    run=store.claim(record['id'],{},wake_id=wake['id'])
    store.recover()
    assert store.wakes.get(wake['id'],owner='person-a')['status']=='interrupted'
    store.activate(record['id'],time.time()+100)
    assert store.claim(record['id'],{},wake_id=wake['id']) is None
    # A new event still consumes the same definition's remaining run allowance.
    for n in range(2):
        next_event=event(store,record,event_id=f'event-{n+2}')
        next_run=store.claim(record['id'],{},wake_id=next_event['id'])
        store.finish(next_run['id'],'completed')
    with pytest.raises(ValueError):event(store,record,event_id='beyond-allowance')
    assert store.get(record['id'])['runs_started']==3


def test_channel_reply_keeps_exact_text_and_verified_origin_in_same_conversation(tmp_path):
    class Harness:
        def __init__(self):self.children,self.child_tasks,self.active={},{},{}
        def _save_child(self,child):pass
        async def turn(self,message,chat_id,dataset,**options):
            assert message=='Please check the second counter.'
            assert definition()['instruction'] in options['model_context']
            assert 'Authenticated host event origin' in options['model_context']
            self.children[chat_id]['messages'].extend([{'role':'user','content':message},{'role':'assistant','content':'Reviewed.'}])
            yield {'type':'done','status':'completed'}
    async def prove():
        store=AgentStore(tmp_path);harness=Harness()
        scheduler=AgentScheduler(store,harness,asyncio.Semaphore(2))
        record=store.create(definition(),['cafe'],owner_principal_id='person-a');store.activate(record['id'],time.time()+100)
        wake=event(store,record,kind='channel_reply',sender_id='verified-sender',channel_id='verified-channel',
            text='Please check the second counter.')
        await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
        chat=harness.children[record['chat_id']]
        assert chat['messages'][0]['origin']['sender_id']=='verified-sender'
        assert chat['messages'][0]['origin']['event_id']=='event-1'
        assert chat['messages'][0]['content']=='Please check the second counter.'
        assert store.wakes.get(wake['id'],owner='person-a')['status']=='completed'
    asyncio.run(prove())


def test_event_wake_bypasses_timer_delay_but_not_run_admission(tmp_path):
    store,record=setup(tmp_path)
    first=store.claim(record['id'],{})
    store.finish(first['id'],'completed',now=time.time()+120)
    assert store.claim(record['id'],{}) is None
    wake=event(store,record)
    assert store.claim(record['id'],{},wake_id=wake['id'])['chat_id']==record['chat_id']


def test_retired_archive_agent_cannot_start_or_accept_event_wakes(tmp_path):
    store=AgentStore(tmp_path,input_provider=RetailInputProvider())
    record=store.create(definition(input_kind='camera_footage',mode='ongoing'),
        ['cafe'],owner_principal_id='person-a')
    assert record['status']=='retired'
    with pytest.raises(ValueError,match='list_tracker_events'):
        store.activate(record['id'],time.time()+100)
    with pytest.raises(ValueError):event(store,record)
    assert store.claim(record['id'],{}) is None
    assert store.get(record['id'])['runs_started']==0


def test_manual_turn_winning_dispatch_race_preserves_pending_wake_and_allowance(tmp_path):
    class Harness:
        def __init__(self):self.children,self.child_tasks,self.active={},{},{}
        def _save_child(self,child):pass
        async def turn(self,*args,**kwargs):
            raise AssertionError('The already active manual turn wins')
            yield
    async def prove():
        store=AgentStore(tmp_path);harness=Harness()
        scheduler=AgentScheduler(store,harness,asyncio.Semaphore(2))
        record=store.create(definition(),['cafe'],owner_principal_id='person-a');store.activate(record['id'],time.time()+100)
        wake=event(store,record)
        run=store.claim(record['id'],{},wake_id=wake['id'])
        harness.active[record['chat_id']]={}
        await scheduler._execute(run)
        assert store.get(record['id'])['runs_started']==0
        assert store.get(record['id'])['last_run']['status']=='deferred'
        assert store.wakes.get(wake['id'],owner='person-a')['status']=='pending'
        harness.active.clear()
        retried=store.claim(record['id'],{},wake_id=wake['id'])
        assert retried['id']!=run['id'] and store.get(record['id'])['runs_started']==1
    asyncio.run(prove())


def test_identical_provider_event_ids_in_different_connections_are_distinct(tmp_path):
    store,record=setup(tmp_path)
    first=event(store,record)
    second=event(store,record,connection_id='fixture-account-2')
    assert first['id'] != second['id']
    assert event(store,record)['id']==first['id']
    assert event(store,record,connection_id='fixture-account-2')['id']==second['id']
    with pytest.raises(ValueError):event(store,record,connection_id='../outside')


def test_saving_new_revision_cancels_pending_wakes_from_previous_revision(tmp_path):
    store,record=setup(tmp_path);wake=event(store,record)
    with pytest.raises(ValueError):store.update(record['id'],definition(title='New objective'),['cafe'])
    assert store.wakes.get(wake['id'],owner='person-a')['status']=='pending'
    # Simulate a paused legacy definition whose pending event survived an older host.
    with store.connect() as db:
        saved=store._read(db,'agents',record['id']);saved.update(status='paused',next_run_at=None)
        store._save(db,'agents',saved)
    updated=store.update(record['id'],definition(title='New objective'),['cafe'],expected_revision=1)
    assert updated['revision']==2
    retired=store.wakes.get(wake['id'],owner='person-a')
    assert retired['status']=='cancelled' and retired['reason']=='definition_revised'
    store.activate(record['id'],time.time()+100)
    assert store.claim(record['id'],{},wake_id=wake['id']) is None
