"""Host-selected steps share receipts, preserve ingress and can finish without a turn."""
import asyncio
import time
import pytest
from background_agents import AgentStore


def job(store, **extra):
    return store.wakes.enqueue_job(producer='fixture',producer_id='display-1',owner='alice',actor='alice',
        collection='company',event_id='revision-1',payload={'text':'Original'},source_scope={'incarnation':'one'},
        chain=[{'kind':'script','handler':'calculate'},{'kind':'deliver'}],**extra)


def test_pure_calculation_has_no_agent_or_turn_and_keeps_original_ingress(tmp_path):
    async def proof():
        store=AgentStore(tmp_path);store.signals.register_producer('fixture',lambda record:True)
        async def calculate(payload,config,context):return {'payload':{'total':9},'text':'Calculated'}
        store.signals.register('calculate',calculate,kind='script')
        signal=job(store)
        result=await store.signals.process(signal)
        assert result['status']=='completed' and result['target_kind']=='job'
        assert result['text']=='Original' and result['processed_text']=='Calculated'
        assert store.list()==[] and store.signals.usage()['turn']['used']==0
        assert len(store.signals.receipts(signal['id'],owner='alice'))==2
    asyncio.run(proof())


def test_transformed_verified_chat_keeps_authority_bound_to_original_text(tmp_path):
    async def proof():
        store=AgentStore(tmp_path);store.signal_authority=lambda row:row['text']=='Original'
        async def calculate(payload,config,context):return {'text':'Calculated'}
        store.signals.register('calculate',calculate,kind='script')
        signal=store.wakes.enqueue_chat(chat_id='chat',collection='company',owner='alice',actor='alice',
            provider='timer',connection_id='timer',event_id='one',text='Original',sender={'principal_id':'alice'},
            chain=[{'kind':'script','handler':'calculate'},{'kind':'deliver'}])
        result=await store.signals.process(signal)
        assert result['text']=='Original' and result['processed_text']=='Calculated'
        assert store.wakes.claim_chat(signal['id'],owner='alice') is not None
    asyncio.run(proof())


def test_external_identity_is_journaled_and_reconciliation_never_replays(tmp_path):
    async def proof():
        store=AgentStore(tmp_path);store.signals.register_producer('fixture',lambda record:True);calls=[]
        async def calculate(payload,config,context):
            context['external']({'job_id':'original-job'});calls.append('run')
            raise TimeoutError('Original result lost')
        async def reconcile(context):
            assert context['external_identity']=={'job_id':'original-job'}
            return {'status':'succeeded','output':{'payload':{'total':9}},'evidence':{'job_closed':True}}
        store.signals.register('calculate',calculate,kind='script',reconcile=reconcile)
        signal=job(store);assert await store.signals.process(signal) is None
        assert await store.signals.process(signal) is None
        await store.signals.reconcile(signal['id'],owner='alice')
        result=await store.signals.process(signal)
        assert result['status']=='completed' and result['processed_payload']=={'total':9}
        assert calls==['run']
    asyncio.run(proof())


def test_deadline_waits_for_cleanup_and_holds_unknown_result(tmp_path):
    async def proof():
        store=AgentStore(tmp_path);store.signals.register_producer('fixture',lambda record:True)
        cleaning=asyncio.Event();release=asyncio.Event()
        async def calculate(payload,config,context):
            context['external']({'job_id':'original-job'})
            try:await asyncio.Event().wait()
            finally:cleaning.set();await release.wait()
        store.signals.register('calculate',calculate,kind='script',timeout_seconds=.01,cleanup_seconds=1)
        signal=job(store);task=asyncio.create_task(store.signals.process(signal));await cleaning.wait()
        assert not task.done();release.set();assert await task is None
        receipt=store.signals.receipts(signal['id'],owner='alice')[0]
        assert receipt['status']=='uncertain' and receipt['cleanup']['task_stopped'] is True
        assert receipt['cleanup']['quiescent'] is False
        assert receipt['external_identity']=={'job_id':'original-job'}
    asyncio.run(proof())


def test_step_waiting_for_commissioning_records_unstarted_hold(tmp_path):
    from signal_chain import StepHeld
    async def proof():
        store=AgentStore(tmp_path);store.signals.register_producer('fixture',lambda record:True)
        async def calculate(payload,config,context):raise StepHeld('Commission the calculation provider.')
        store.signals.register('calculate',calculate,kind='script')
        signal=job(store);assert await store.signals.process(signal) is None
        receipt=store.signals.receipts(signal['id'],owner='alice')[0]
        assert receipt['status']=='held' and 'external_identity' not in receipt
    asyncio.run(proof())


def test_no_agent_producer_rechecks_incarnation_after_await(tmp_path):
    async def proof():
        store=AgentStore(tmp_path);current=['one']
        store.signals.register_producer('fixture',lambda record:record['source_scope']['incarnation']==current[0])
        async def calculate(payload,config,context):current[0]='two';return {'text':'Stale'}
        store.signals.register('calculate',calculate,kind='script')
        signal=job(store);assert await store.signals.process(signal) is None
        assert store.wakes.get(signal['id'],owner='alice')['chain_status']=='uncertain'
    asyncio.run(proof())


def test_current_revocation_cancels_running_step_and_waits_cleanup(tmp_path):
    async def proof():
        store=AgentStore(tmp_path);allowed=[True];entered=asyncio.Event();closed=asyncio.Event()
        store.signals.register_producer('fixture',lambda record:allowed[0])
        async def calculate(payload,config,context):
            context['external']({'job_id':'job'});entered.set()
            try:await asyncio.Event().wait()
            finally:closed.set()
        async def cleanup(context):
            assert context['external_identity']=={'job_id':'job'}
            await closed.wait();return {'quiescent':True,'job_id':'job'}
        store.signals.register('calculate',calculate,kind='script',cancel=cleanup)
        signal=job(store);pending=asyncio.create_task(store.signals.process(signal));await entered.wait()
        allowed[0]=False;assert await asyncio.wait_for(pending,1) is None
        receipt=store.signals.receipts(signal['id'],owner='alice')[0]
        assert receipt['cleanup']['quiescent'] is True and receipt['status']=='uncertain'
    asyncio.run(proof())


def test_job_promotes_same_signal_and_preserves_reviewed_event_kind(tmp_path):
    from test_signal_chain import setup
    async def proof():
        store,saved,_=setup(tmp_path)
        store.signals.register_producer('fixture',lambda record:record['source_scope']=={'incarnation':'one'})
        store.wake_authorizer=lambda definition,wake,admit=False:wake['kind']=='event'
        async def calculate(payload,config,context):return {'text':'Calculation ready'}
        store.signals.register('calculate',calculate,kind='script')
        signal=job(store,destination={'agent_id':saved['id'],'revision':1},kind='event')
        assert store.wakes.pending(saved['id'],owner='alice') is None
        result=await store.signals.process(signal)
        assert result['id']==signal['id'] and result['target_kind']=='agent' and result['kind']=='event'
        assert result['text']=='Original' and result['processed_text']=='Calculation ready'
        assert store.wakes.pending(saved['id'],owner='alice')['id']==signal['id']
        run=store.claim(saved['id'],{},wake_id=signal['id'])
        assert run is not None
        with store.connect() as db:assert db.execute('SELECT COUNT(*) FROM agent_wakes').fetchone()[0]==1
        assert store.signals.usage()['turn']['used']==1
    asyncio.run(proof())


def test_paused_destination_still_calculates_but_cannot_deliver(tmp_path):
    from test_signal_chain import setup
    async def proof():
        store,saved,_=setup(tmp_path);calls=[]
        store.signals.register_producer('fixture',lambda record:True)
        async def calculate(payload,config,context):calls.append('published');return {'text':'Calculated'}
        store.signals.register('calculate',calculate,kind='script')
        signal=job(store,destination={'agent_id':saved['id'],'revision':1})
        store.pause(saved['id'])
        assert store.wakes.get(signal['id'],owner='alice')['status']=='pending'
        result=await store.signals.process(signal)
        assert calls==['published'] and result['status']=='cancelled'
        assert result['reason']=='destination_authority_ended' and store.signals.usage()['turn']['used']==0
    asyncio.run(proof())


def test_promoted_producer_revocation_blocks_direct_claim_and_predispatch(tmp_path):
    from test_signal_chain import setup
    async def proof():
        store,saved,_=setup(tmp_path);current=[True]
        store.signals.register_producer('fixture',lambda record:current[0])
        store.wake_authorizer=lambda definition,wake,admit=False:True
        async def calculate(payload,config,context):return {'text':'Calculated'}
        store.signals.register('calculate',calculate,kind='script')
        signal=job(store,destination={'agent_id':saved['id'],'revision':1})
        promoted=await store.signals.process(signal)
        current[0]=False
        assert store.claim(saved['id'],{},wake_id=signal['id']) is None
        assert store.input_optional_wake(store.get(saved['id']),promoted,admit=True) is False
    asyncio.run(proof())


def test_failed_reconciliation_retires_original_job(tmp_path):
    async def proof():
        store=AgentStore(tmp_path);store.signals.register_producer('fixture',lambda record:True)
        async def calculate(payload,config,context):context['external']({'job_id':'original'});raise TimeoutError()
        async def reconcile(context):return {'status':'failed','evidence':{'quiescent':True}}
        store.signals.register('calculate',calculate,kind='script',reconcile=reconcile)
        event=job(store);await store.signals.process(event)
        await store.signals.reconcile(event['id'],owner='alice')
        result=store.wakes.get(event['id'],owner='alice')
        assert result['status']=='failed' and result['completed_at']>0
        assert await store.signals.process(event) is None
    asyncio.run(proof())
