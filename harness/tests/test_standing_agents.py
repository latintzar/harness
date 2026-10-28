"""Standing authority survives process restarts, never revoked actors or daily limits."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from background_agents import AgentStore, AgentScheduler


def store_at(path, allowed=None):
    allowed = allowed if allowed is not None else {'alice'}
    return AgentStore(path, authority=lambda record, actor: actor in allowed)


def saved(store, **fields):
    return store.create({'title':'Daily workspace report','instruction':'Summarize this workspace.',
        'dataset':'company','mode':'scheduled','interval_seconds':60,'max_runs':2,**fields},
        ['company'],owner_principal_id='alice')


def activate(store, record, now):
    return store.activate(record['id'], now+60, now=now, standing=True, actor_principal_id='alice')


def test_standing_requires_live_actor_and_legacy_grants_keep_expiry(tmp_path):
    now=time.time()
    unsafe=AgentStore(tmp_path)
    record=saved(unsafe)
    with pytest.raises(ValueError): activate(unsafe,record,now)
    store=store_at(tmp_path)
    with pytest.raises(ValueError): store.activate(record['id'],now+60,now=now,standing=True,actor_principal_id='bob')
    legacy=store.activate(record['id'],now+60,now=now)
    assert legacy['expires_at']==now+60
    store.recover()
    assert store.get(record['id'])['status']=='paused'


def test_standing_restart_preserves_pending_wake_and_daily_limit_resets(tmp_path):
    now=int(time.time()/86400)*86400+100
    store=store_at(tmp_path)
    record=saved(store)
    active=activate(store,record,now)
    assert active['expires_at'] is None
    wake=store.wakes.enqueue(record['id'],owner='alice',expected_revision=1,provider='timer',
        connection_id='hourly',event_id='one',now=now)
    restarted=store_at(tmp_path)
    restarted.recover(now=now+100)
    assert restarted.get(record['id'])['status']=='active'
    assert restarted.wakes.pending(record['id'],owner='alice',now=now+100)['id']==wake['id']
    for offset in (100,200):
        run=restarted.claim(record['id'],{},now=now+offset)
        assert run['actor_principal_id']=='alice'
        assert run['expires_at']==now+offset+1800
        restarted.finish(run['id'],'completed',now=now+offset+1)
    assert restarted.get(record['id'])['status']=='active'
    assert restarted.claim(record['id'],{},now=now+300) is None
    assert restarted.claim(record['id'],{},now=now+86400) is not None


def test_revocation_at_claim_and_dispatch_preserves_real_actor(tmp_path):
    now=time.time(); allowed={'alice'}; store=store_at(tmp_path,allowed)
    record=saved(store);activate(store,record,now)
    run=store.claim(record['id'],{},now=now)
    allowed.clear()
    class Agent:
        children={};active={};child_tasks={}
        def _save_child(self,child): pass
        async def turn(self,*args,**kwargs):
            raise AssertionError('A revoked actor reached model execution')
            yield
    scheduler=object.__new__(AgentScheduler)
    scheduler.store=store;scheduler.agent=Agent()
    asyncio.run(scheduler._execute(run))
    assert store.run_report(record['id'],run['id'])['status']=='interrupted'
    assert store.get(record['id'])['status']=='needs_attention'


def test_restart_holds_started_work_and_releases_only_unstarted_claim(tmp_path):
    now=time.time();store=store_at(tmp_path);record=saved(store);activate(store,record,now)
    run=store.claim(record['id'],{},now=now)
    store.recover(now=now+1)
    assert store.run_report(record['id'],run['id'])['status']=='deferred'
    again=store.claim(record['id'],{},now=now+2)
    store.mark_conversation_run(again['id'],0)
    store.recover(now=now+3)
    assert store.run_report(record['id'],again['id'])['status']=='recovering'
    assert store.get(record['id'])['status']=='active'
    assert store.claim(record['id'],{},now=now+100) is None


def test_workspace_wake_daily_limit_is_atomic_across_agents(tmp_path):
    now=int(time.time()/86400)*86400+100;store=store_at(tmp_path)
    records=[saved(store,max_runs=20) for _ in range(3)]
    for record in records:activate(store,record,now)
    runs=[]
    for index in range(49):
        record=records[index//20]
        run=store.claim(record['id'],{},now=now+index*61)
        if index==48:assert run is None
        else:
            assert run is not None
            store.finish(run['id'],'completed',now=now+index*61+1)
            runs.append(run)
    assert len(runs)==48


def test_exact_managed_request_recovery_finalizes_original_run(tmp_path):
    async def proof():
        store=store_at(tmp_path);record=saved(store);activate(store,record,time.time())
        run=store.claim(record['id'],{})
        store.mark_conversation_run(run['id'],0)
        class Agent:
            active={};child_tasks={}
            def __init__(self):
                self.children={record['chat_id']:{'id':record['chat_id'],'owner_principal_id':'alice',
                    'dataset':'company','messages':[{'role':'user','content':'original','managed_request_id':'request-original'},
                        {'role':'assistant','content':'Completed original request'}], 'active_run_id':run['id']}}
            def _save_child(self,child):pass
            async def turn(self,*args,**kw):
                raise AssertionError('Recovery must preserve the original provider request')
                yield
        calls=[]
        class Runtime:
            async def recover_request(self,agent,chat_id,logical_id):
                calls.append((chat_id,logical_id))
                assert store.execution_current(run['id'])
                return {'status':'completed','turn_id':'original-provider-turn'}
        agent=Agent();agent.managed_runtime=Runtime()
        scheduler=AgentScheduler(store,agent)
        await scheduler.recover_pending()
        assert calls==[(record['chat_id'],'request-original')]
        result=store.run_report(record['id'],run['id'])
        assert result['status']=='completed'
        assert store.get(record['id'])['runs_started']==1
        assert store.signals.usage()['turn']['used']==1
        assert result['report_messages'][0]['managed_request_id']=='request-original'
    asyncio.run(proof())


def test_revoked_recovering_job_waits_for_provider_cancellation_proof(tmp_path):
    async def proof():
        store=store_at(tmp_path);record=saved(store);activate(store,record,time.time())
        run=store.claim(record['id'],{});store.mark_conversation_run(run['id'],0);store.hold(run['id']);store.pause(record['id'])
        class Agent:
            active={};child_tasks={}
            children={record['chat_id']:{'id':record['chat_id'],'owner_principal_id':'alice','dataset':'company',
                'messages':[{'role':'user','managed_request_id':'original'}]}}
            def _save_child(self,record):pass
        outcomes=iter(['uncertain','cancelled'])
        class Runtime:
            async def recover_request(self,agent,chat_id,logical_id):
                assert not store.execution_current(run['id'])
                return {'status':next(outcomes),'turn_id':'provider-turn'}
        agent=Agent();agent.managed_runtime=Runtime()
        scheduler=object.__new__(AgentScheduler);scheduler.store=store;scheduler.agent=agent
        await scheduler.recover_pending()
        assert store.run_report(record['id'],run['id'])['status']=='recovering'
        await scheduler.recover_pending()
        assert store.run_report(record['id'],run['id'])['status']=='interrupted'
        assert store.get(record['id'])['status']=='paused'
    asyncio.run(proof())


@pytest.mark.parametrize('withdrawal', ['paused', 'revoked', 'expired'])
def test_restart_withdrawn_admitted_job_keeps_slot_until_provider_cleanup(tmp_path, withdrawal):
    async def proof():
        store=store_at(tmp_path);record=saved(store);now=time.time()
        if withdrawal=='expired':store.activate(record['id'],now+60)
        else:activate(store,record,now)
        run=store.claim(record['id'],{});store.mark_conversation_run(run['id'],0)
        if withdrawal=='paused':store.pause(record['id'])
        else:
            with store.connect() as db:
                definition=store._read(db,'agents',record['id'])
                if withdrawal=='expired':definition['expires_at']=now-1
                else:definition['grant']['revoked_at']=now
                store._save(db,'agents',definition)
        restarted=store_at(tmp_path)
        class Agent:
            active={};child_tasks={}
            children={record['chat_id']:{'id':record['chat_id'],'owner_principal_id':'alice',
                'dataset':'company','messages':[{'role':'user','managed_request_id':'original'}]}}
            def _save_child(self,child):self.children[child['id']]=child
        calls=[];outcomes=iter(['uncertain','cancelled'])
        class Runtime:
            async def recover_request(self,agent,chat_id,logical_id):
                calls.append((chat_id,logical_id))
                assert not restarted.execution_current(run['id'])
                assert agent.children[chat_id]['active_run_id']==run['id']
                return {'status':next(outcomes),'turn_id':'original-provider-turn'}
        agent=Agent();agent.managed_runtime=Runtime()
        scheduler=AgentScheduler(restarted,agent)
        assert restarted.run_report(record['id'],run['id'])['status']=='recovering'
        other=saved(restarted);activate(restarted,other,time.time())
        assert restarted.claim(other['id'],{}) is None
        await scheduler.recover_pending()
        assert restarted.run_report(record['id'],run['id'])['status']=='recovering'
        assert restarted.claim(other['id'],{}) is None
        await scheduler.recover_pending()
        assert calls==[(record['chat_id'],'original')]*2
        assert restarted.run_report(record['id'],run['id'])['status']=='interrupted'
        assert restarted.get(record['id'])['status']=='paused'
        assert restarted.signals.usage()['turn']['used']==1
        assert restarted.claim(other['id'],{}) is not None
    asyncio.run(proof())


def test_restart_admitted_job_with_missing_request_identity_stays_held(tmp_path):
    async def proof():
        store=store_at(tmp_path);record=saved(store);activate(store,record,time.time())
        run=store.claim(record['id'],{});store.mark_conversation_run(run['id'],0)
        class Agent:
            active={};child_tasks={}
            children={record['chat_id']:{'id':record['chat_id'],'owner_principal_id':'alice',
                'dataset':'company','messages':[{'role':'user','content':'Saved without request identity'}]}}
            def _save_child(self,child):self.children[child['id']]=child
        class Runtime:
            async def recover_request(self,*args):raise AssertionError('Recovery requires the saved request identity')
        agent=Agent();agent.managed_runtime=Runtime();scheduler=AgentScheduler(store,agent)
        await scheduler.recover_pending()
        assert store.run_report(record['id'],run['id'])['status']=='recovering'
        assert agent.children[record['chat_id']]['active_run_id']==run['id']
        other=saved(store);activate(store,other,time.time())
        assert store.claim(other['id'],{}) is None
    asyncio.run(proof())
