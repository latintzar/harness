"""Every signal uses the same ledger, authority and bounded step receipts."""
import asyncio
import time

import pytest

from background_agents import AgentStore,AgentScheduler


def setup(tmp_path,chain=None):
    allowed={'alice'}
    store=AgentStore(tmp_path,authority=lambda record,actor:actor in allowed)
    definition={'title':'Updates','instruction':'Summarize updates.','dataset':'company',
        'mode':'scheduled','interval_seconds':60,'max_runs':20}
    if chain:definition['signal_chain']=chain
    saved=store.create(definition,['company'],owner_principal_id='alice')
    store.activate(saved['id'],standing=True,actor_principal_id='alice')
    return store,saved,allowed


def event(store,saved,identity='one',**more):
    return store.wakes.enqueue(saved['id'],owner='alice',expected_revision=1,provider='hook',
        connection_id='deliveries',event_id=identity,text='Delivery ready',payload={'quantity':3},**more)


def test_all_ingress_kinds_share_rules_and_durable_receipts(tmp_path):
    chain=[{'kind':'rules','all':[{'field':'quantity','op':'gte','value':4}]},{'kind':'deliver'}]
    store,saved,allowed=setup(tmp_path,chain)
    for kind in ('event','message','schedule','data','script','timer','hook'):
        wake=event(store,saved,kind,kind=kind)
        assert asyncio.run(store.signals.process(wake)) is None
        assert store.wakes.get(wake['id'],owner='alice')['status']=='screened'
        assert store.signals.receipts(wake['id'],owner='alice')[0]['status']=='filtered'
    assert store.signals.usage()['turn']['used']==0
    with store.connect() as db:assert db.execute('SELECT COUNT(*) FROM agent_wakes').fetchone()[0]==7


def test_uncertain_external_step_is_never_replayed_after_restart(tmp_path):
    chain=[{'kind':'effect','handler':'notify'},{'kind':'deliver'}]
    store,saved,allowed=setup(tmp_path,chain);calls=[]
    async def ambiguous(payload,config,receipt):
        calls.append(receipt['idempotency_key'])
        raise TimeoutError('Provider may have accepted the request')
    store.signals.register('notify',ambiguous,kind='effect')
    wake=event(store,saved)
    assert asyncio.run(store.signals.process(wake)) is None
    restarted=AgentStore(tmp_path,authority=lambda record,actor:True)
    restarted.recover()
    restarted.signals.register('notify',ambiguous,kind='effect')
    assert asyncio.run(restarted.signals.process(wake)) is None
    assert len(calls)==1
    assert restarted.signals.usage()['send']['used']==1
    assert restarted.signals.receipts(wake['id'],owner='alice')[0]['status']=='uncertain'
    assert restarted.claim(saved['id'],{},wake_id=wake['id']) is None


def test_step_output_cannot_replace_destination_and_live_revocation_stops_delivery(tmp_path):
    chain=[{'kind':'script','handler':'transform'},{'kind':'deliver'}]
    store,saved,allowed=setup(tmp_path,chain)
    async def transform(payload,config,receipt):
        allowed.clear()
        assert not receipt['current']()
        return {'payload':{'quantity':5},'text':'Processed'}
    store.signals.register('transform',transform,kind='script')
    wake=event(store,saved)
    assert asyncio.run(store.signals.process(wake)) is None
    assert store.claim(saved['id'],{},wake_id=wake['id']) is None


def test_external_steps_claim_before_await_and_screen_daily_cap(tmp_path):
    async def run():
        store,saved,_=setup(tmp_path,[{'kind':'screen','handler':'screen'},{'kind':'deliver'}])
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def screen(payload,config,receipt):
            calls.append(receipt);entered.set();await release.wait()
            return {'payload':{'quantity':5}}
        store.signals.register('screen',screen,kind='screen')
        wake=event(store,saved)
        task=asyncio.create_task(store.signals.process(wake));await entered.wait()
        assert await store.signals.process(wake) is None
        release.set();assert (await task)['chain_status']=='ready'
        assert len(calls)==1
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            assert store.signals.reserve(db,'screen','fill',amount=499)
            assert not store.signals.reserve(db,'screen','extra')
        assert store.signals.usage()['screen']=={'used':500,'limit':500}
    asyncio.run(run())


def test_scheduler_schedule_actually_passes_the_reviewed_chain(tmp_path):
    async def run():
        store,saved,_=setup(tmp_path,[{'kind':'rules','all':[{'field':'text','op':'contains','value':'NEVER'}]},{'kind':'deliver'}])
        class Agent:
            children={};active={};child_tasks={}
            def _save_child(self,child):self.children[child['id']]=child
            async def turn(self,*args,**kw):
                raise AssertionError('Filtered schedule reached the agent')
                yield
        scheduler=AgentScheduler(store,Agent())
        await scheduler.tick()
        assert scheduler.tasks=={}
        with store.connect() as db:
            rows=db.execute('SELECT body FROM agent_wakes').fetchall()
            assert len(rows)==1 and 'screened' in rows[0]['body']
        assert store.get(saved['id'])['next_run_at']>time.time()
    asyncio.run(run())


def test_chat_signals_use_same_queue_limits_and_actor_fence(tmp_path):
    store=AgentStore(tmp_path);allowed={'bob'}
    store.signal_authority=lambda record:record['actor_principal_id'] in allowed and record['chat_id']=='org-chat'
    options=dict(chat_id='org-chat',collection='company',owner='organization',actor='bob',provider='whatsapp',
        connection_id='phone',event_id='reply-1',text='Delivery arrived',sender={'principal_id':'bob','channel':'whatsapp'})
    signal=store.wakes.enqueue_chat(**options)
    assert store.wakes.enqueue_chat(**options)['id']==signal['id']
    with pytest.raises(ValueError):store.wakes.enqueue_chat(**{**options,'text':'Changed event'})
    assert asyncio.run(store.signals.process(signal))['id']==signal['id']
    claim=store.wakes.claim_chat(signal['id'],owner='organization')
    second=store.wakes.enqueue_chat(**{**options,'event_id':'reply-2'})
    assert store.wakes.claim_chat(second['id'],owner='organization') is None
    store.wakes.finish_chat(signal['id'],claim['run_id'],owner='organization',status='completed')
    allowed.clear()
    assert store.wakes.claim_chat(second['id'],owner='organization') is None
    assert store.signals.usage()['turn']['used']==1


def test_admitted_outbound_effects_share_two_hundred_daily_cap(tmp_path):
    store=AgentStore(tmp_path)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        assert store.signals.reserve(db,'send','already-admitted',amount=200)
        assert not store.signals.reserve(db,'send','next')
        assert store.signals.reserve(db,'send','already-admitted',amount=200)
    assert store.signals.usage()['send']=={'used':200,'limit':200}


def test_existing_wake_identity_accepts_the_same_payload_after_schema_upgrade(tmp_path):
    import hashlib
    import json
    store,saved,_=setup(tmp_path)
    wake=store.wakes.enqueue(saved['id'],owner='alice',expected_revision=1,provider='event',
        connection_id='feed',event_id='old',text='Saved input')
    with store.connect() as db:
        stored=store.wakes._get(db,wake['id'])
        old={key:stored[key] for key in ('agent_id','owner_principal_id','revision','provider','connection_id',
            'event_id','kind','sender_id','channel_id','text')}
        for key in ('chain','payload','chain_status'):stored.pop(key,None)
        stored['payload_sha256']=hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest()
        store.wakes._write(db,stored)
    again=store.wakes.enqueue(saved['id'],owner='alice',expected_revision=1,provider='event',
        connection_id='feed',event_id='old',text='Saved input')
    assert again['id']==wake['id']
