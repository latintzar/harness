"""One bounded classification settles real usage and never replays paid work."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx
import pytest

from background_agents import AgentStore
from company_usage import CompanyUsage, OWNER
from signal_screening import SignalScreening, MODEL, PRICING, MAX_OUTPUT


def fixture(path, *, funded=True, response=None, callback=None, key='fixture-key'):
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    store=AgentStore(path);allowed=[True];store.signal_authority=lambda row:allowed[0]
    pool=CompanyUsage.openai_pool(path)
    if funded:
        proof=pool.state/'funding.json'
        proof.write_text(json.dumps({'schema_version':1,'record_id':'fixture-screening',
            'company_scope':pool.company_scope,'parent_company_scope':pool.summary()['parent_company_scope'],
            'provider':'openai','kind':'prepaid_customer_funds','amount_usd':'1',
            'settled':True,'user_verified':True,'verified_by':OWNER,'receipt_reference':'fixture'}))
        proof.chmod(0o600);pool.import_funding_record(proof);pool.set_monthly_limit(0,'1')
    calls=[]
    async def request(req):
        calls.append(req)
        if callback:return await callback(req)
        return httpx.Response(200,json=response or terminal())
    screen=SignalScreening(store,pool=pool,api_key=lambda:key,transport=httpx.MockTransport(request))
    return store,pool,screen,calls,allowed


def terminal(decision='wake', **overrides):
    return {'id':'resp_screening1','model':MODEL,'status':'completed','service_tier':'default',
        'usage':{'input_tokens':100,'input_tokens_details':{'cached_tokens':20},'output_tokens':4,'total_tokens':104},
        'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':decision}]}],**overrides}


def signal(store, identity='one', **more):
    return store.wakes.enqueue_chat(chat_id='chat',collection='company',owner='alice',actor='alice',provider='hook',
        connection_id='hook',event_id=identity,text='Delivery arrived',sender={'principal_id':'alice'},
        payload={'quantity':4},chain=[{'kind':'screen','handler':'classify','config':{'wake_when':'A delivery arrived'}},
            {'kind':'deliver'}],**more)


@pytest.mark.parametrize('decision,expected',[('wake','pending'),('keep','screened')])
def test_classification_bound_and_actual_usage_settle_once(tmp_path,decision,expected):
    async def proof():
        store,pool,screen,calls,_=fixture(tmp_path,response=terminal(decision))
        event=signal(store);await store.signals.process(event);await store.signals.process(event)
        assert store.wakes.get(event['id'],owner='alice')['status']==expected and len(calls)==1
        body=json.loads(calls[0].content)
        assert body['model']==MODEL and body['tools']==[] and body['store'] is False
        assert body['max_output_tokens']==MAX_OUTPUT==64 and body['reasoning']=={'effort':'minimal'}
        assert set(json.loads(body['input']))=={'wake_when','event'}
        assert pool.summary()['provider_actual_cost_usd']=='0.000006'
        assert pool.summary()['reserved_usd']=='0.000000'
        assert store.signals.usage()['screen']['used']==1 and store.signals.usage()['turn']['used']==0
        assert 'fixture-key' not in json.dumps(store.signals.receipts(event['id'],owner='alice'))
    asyncio.run(proof())


@pytest.mark.parametrize('missing',['funding','key','pricing'])
def test_missing_commissioning_starts_zero_requests(tmp_path,missing):
    async def proof():
        store,pool,screen,calls,_=fixture(tmp_path,funded=missing!='funding',key='' if missing=='key' else 'fixture-key')
        if missing=='pricing':screen.clock=lambda:datetime(2027,1,1,tzinfo=timezone.utc).timestamp()
        event=signal(store);assert await store.signals.process(event) is None
        assert calls==[] and store.signals.receipts(event['id'],owner='alice')[0]['status']=='held'
        assert pool.summary()['reserved_usd']=='0.000000'
    asyncio.run(proof())


def test_screen_500th_is_atomic_and_501st_starts_zero_requests(tmp_path):
    async def proof():
        store,pool,screen,calls,_=fixture(tmp_path)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE');assert store.signals.reserve(db,'screen','earlier',amount=499)
        events=[signal(store,str(n)) for n in range(2)]
        await asyncio.gather(*(store.signals.process(event) for event in events))
        assert len(calls)==1 and store.signals.usage()['screen']['used']==500
    asyncio.run(proof())


def test_lost_response_keeps_money_and_durable_identity_across_restart(tmp_path):
    async def proof():
        async def lost(req):raise httpx.ReadTimeout('fixture')
        store,pool,screen,calls,_=fixture(tmp_path,callback=lost)
        event=signal(store);assert await store.signals.process(event) is None
        receipt=store.signals.receipts(event['id'],owner='alice')[0]
        assert receipt['status']=='uncertain' and receipt['external_identity']['request_id'].startswith('screen-')
        assert pool.summary()['reserved_usd']!='0.000000'
        restarted=AgentStore(tmp_path);restarted.signal_authority=lambda row:True;restarted.recover()
        SignalScreening(restarted,pool=pool,api_key=lambda:'fixture-key',transport=screen.transport)
        await restarted.signals.reconcile(event['id'],owner='alice')
        assert await restarted.signals.process(event) is None and len(calls)==1
    asyncio.run(proof())


def test_current_revocation_withholds_decision_and_preserves_actual_charge(tmp_path):
    async def proof():
        store,pool,screen,calls,allowed=fixture(tmp_path)
        async def revoke(req):allowed[0]=False;return httpx.Response(200,json=terminal())
        screen.transport=httpx.MockTransport(revoke)
        event=signal(store);assert await store.signals.process(event) is None
        assert pool.summary()['provider_actual_cost_usd']=='0.000006'
        assert store.wakes.get(event['id'],owner='alice')['chain_status']=='uncertain'
        assert store.wakes.claim_chat(event['id'],owner='alice') is None
    asyncio.run(proof())


def test_saved_terminal_receipt_recovers_without_provider_request(tmp_path,monkeypatch):
    async def proof():
        store,pool,screen,calls,_=fixture(tmp_path)
        settle=pool.settle_openai
        monkeypatch.setattr(pool,'settle_openai',lambda *args:(_ for _ in ()).throw(OSError('interrupted settlement')))
        event=signal(store);assert await store.signals.process(event) is None
        monkeypatch.setattr(pool,'settle_openai',settle)
        await store.signals.reconcile(event['id'],owner='alice')
        ready=await store.signals.process(event)
        assert ready['chain_status']=='ready' and len(calls)==1
        assert pool.summary()['provider_actual_cost_usd']=='0.000006'
    asyncio.run(proof())


def test_unknown_provider_usage_holds_money_and_future_screening(tmp_path):
    async def proof():
        store,pool,screen,calls,_=fixture(tmp_path,response=terminal(usage={'input_tokens':100}))
        event=signal(store);assert await store.signals.process(event) is None
        assert await store.signals.process(signal(store,'two')) is None
        assert len(calls)==1 and pool.summary()['reserved_usd']!='0.000000'
    asyncio.run(proof())


def test_invalid_decision_settles_usage_then_retires_on_reconciliation(tmp_path):
    async def proof():
        store,pool,screen,calls,_=fixture(tmp_path,response=terminal('Maybe wake'))
        event=signal(store);assert await store.signals.process(event) is None
        assert pool.summary()['provider_actual_cost_usd']=='0.000006'
        result=await store.signals.reconcile(event['id'],owner='alice')
        assert result['status']=='failed' and len(calls)==1
    asyncio.run(proof())


def test_revocation_during_provider_wait_keeps_original_paid_request(tmp_path):
    async def proof():
        entered=asyncio.Event();cancelled=asyncio.Event()
        async def wait(req):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        store,pool,screen,calls,allowed=fixture(tmp_path,callback=wait)
        event=signal(store);running=asyncio.create_task(store.signals.process(event));await entered.wait()
        allowed[0]=False;assert await asyncio.wait_for(running,1) is None
        assert cancelled.is_set() and len(calls)==1
        assert pool.summary()['reserved_usd']!='0.000000'
        assert store.signals.receipts(event['id'],owner='alice')[0]['status']=='uncertain'
    asyncio.run(proof())


def test_screen_pool_cannot_use_other_workspace_or_native_chat_allocation(tmp_path):
    store,pool,screen,calls,_=fixture(tmp_path/'one')
    for other in (CompanyUsage.openai_pool(tmp_path/'other'),CompanyUsage.native_agent_pool(store.state)):
        with pytest.raises(ValueError):SignalScreening(store,pool=other,api_key=lambda:'fixture-key')
    assert calls==[]
