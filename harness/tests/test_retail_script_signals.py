"""Retail calculation, publication and optional agent delivery share one saved signal."""
import asyncio
import sqlite3

import pytest
from test_tracker_script_publish import (shop, template, go_live, manual, stored, agent_for, wakes)
from test_tracker_scripts import loop_thread


def connect(shop, store=None):
    import retail_script_signals as signals
    from agent_test_support import retail_agent_store
    store = store or retail_agent_store(shop.state)
    adapter = signals.ScriptSignals(store, lambda owner, collection: shop.company, sandbox=shop.box, clock=lambda:shop.now[0])
    shop.sweep = lambda **more: __import__('tracker_runner').tracker_pass(
        shop.root, shop.root, clock=lambda: shop.now[0], pending_since={}, custom=adapter.processor, **more)
    return adapter, store


def test_script_only_publishes_inside_one_signal_without_an_agent(shop):
    tracker = go_live(shop, wake='never')
    adapter, store = connect(shop)
    result = manual(shop)
    signal = wakes(shop)[0]
    assert result['scripts'][0]['status'] == 'succeeded'
    assert stored(shop)['tracker_id'] == tracker['id']
    assert signal['status'] == 'completed' and signal['target_kind'] == 'job'
    assert signal['destination'] is None
    assert [r['step']['kind'] for r in store.signals.receipts(signal['id'], owner='peter')] == ['script', 'deliver']
    assert store.list() == [] and store.signals.usage()['turn']['used'] == 0
    assert len(shop.box.jobs) == 2
    asyncio.run(store.signals.process(signal))
    assert len(shop.box.jobs) == 2


@pytest.mark.parametrize('policy,wanted', [('never', False), ('on_failure', False), ('on_change', True), ('every_run', True)])
def test_wake_policy_selects_delivery_on_the_original_script_signal(shop, policy, wanted):
    store, definition = agent_for(shop)
    tracker = go_live(shop, agent_id=definition['id'], wake=policy)
    from tracker_runner import script_wake_authorizer
    store.wake_authorizer = script_wake_authorizer({'state':shop.state,
        'monitoring':shop.company.monitoring, 'business_scope':shop.company.scope})
    adapter, store = connect(shop, store)
    result = manual(shop)
    signals = wakes(shop)
    assert len(signals) == 1
    signal = signals[0]
    assert signal['provider'] == 'tracker_script'
    assert signal['source_scope']['tracker_generation'] == tracker['generation_id']
    assert signal['target_kind'] == ('agent' if wanted else 'job')
    if wanted:
        assert signal['status'] == 'pending'
        assert result['scripts'][0]['wake'] == {'queued':True, 'id':signal['id']}
        claim = store.claim(definition['id'], {}, source_records=[], wake_id=signal['id'])
        assert claim is not None and claim['wake']['id'] == signal['id']
    else:
        assert signal['status'] in {'completed', 'screened'}
    assert store.signals.receipts(signal['id'], owner='peter')[0]['status'] in {'succeeded', 'filtered'}


def test_failed_calculation_and_review_output_keep_their_original_signal(shop):
    store, definition = agent_for(shop)
    tracker = go_live(shop, agent_id=definition['id'], wake='on_failure', publish='review')
    from tracker_runner import script_wake_authorizer
    store.wake_authorizer = script_wake_authorizer({'state':shop.state,
        'monitoring':shop.company.monitoring, 'business_scope':shop.company.scope})
    adapter, store = connect(shop, store)
    result = manual(shop)
    assert result['scripts'][0]['output'] == 'proposed' and stored(shop) is None
    assert shop.store.proposal(tracker) is not None
    # A different event input is needed to admit a second calculation of this revision.
    from test_tracker_runner import more_recordings
    more_recordings(shop)
    shop.box.answer = {'status':'failed', 'error':shop.scripts.fault('script_stopped'), 'started':True}
    result = manual(shop)
    signal = wakes(shop)[-1]
    assert result['scripts'][0]['status'] == 'failed'
    assert signal['target_kind'] == 'agent' and signal['status'] == 'pending'
    assert len(wakes(shop)) == 2 and len(shop.box.jobs) == 3


@pytest.mark.parametrize('change', ['policy', 'tracker', 'source', 'membership'])
def test_source_authority_changes_during_calculation_block_publication(shop, change):
    tracker = go_live(shop, wake='never')
    adapter, store = connect(shop)
    original = shop.box.run
    async def changed(**options):
        result = await original(**options)
        if change == 'policy':
            shop.store.set_policy(tracker, {'publish':'review'}, expected_revision=1)
        elif change == 'tracker':
            from core_trackers import update_tracker
            update_tracker(shop.company, shop.location, tracker['key'], {'label':'Changed tracker'})
        elif change == 'source':
            with sqlite3.connect(shop.state/'location-intelligence.sqlite') as db:
                db.execute("UPDATE records SET body=json_set(body,'$.source',?) WHERE kind='tracker_script_revision'",
                    ('def run(input): return {}',))
        else:
            with sqlite3.connect(shop.root/'access.sqlite') as db:
                db.execute("DELETE FROM principal_environments WHERE principal_id='peter'")
        return result
    shop.box.run = changed
    result = manual(shop)
    assert stored(shop) is None
    signal = wakes(shop)[0]
    assert signal['chain_status'] == 'uncertain'
    assert store.signals.receipts(signal['id'], owner='peter')[0]['status'] == 'uncertain'
    asyncio.run(store.signals.process(signal))
    assert len(shop.box.jobs) == 2


def native_callbacks(shop, *, close=True):
    """An injected transport keeps the real opened/closed receipt contract."""
    original = shop.box.run
    job = {'request_id':'original-script-job', 'scope':{'company_id':shop.company.id,
        'owner_id':'peter', 'root_chat_id':'tracker-run-original-script-job'}}
    async def run(**options):
        await asyncio.to_thread(options['opened'], job)
        result = await original(**options)
        if close:
            await asyncio.to_thread(options['closed'], job)
        return result
    shop.box.run = run
    return job


def test_unknown_native_cleanup_holds_location_until_original_job_reconciles(shop):
    tracker = go_live(shop, wake='never')
    adapter, store = connect(shop)
    job = native_callbacks(shop, close=False)
    recovered = []
    async def recover(pending, closed):
        recovered.extend(pending)
        return []
    shop.box.recover = recover
    result = manual(shop)
    signal = wakes(shop)[0]
    receipt = store.signals.receipts(signal['id'], owner='peter')[0]
    assert receipt['external_identity'] == job and receipt['cleanup']['quiescent'] is False
    assert result['status'] == 'running' and stored(shop) is None
    assert shop.runs.pending_location_jobs(*shop.key)
    shop.now[0] += 10000
    assert shop.runs.claim(*shop.key, trigger='manual', stamp='changed', day='2026-10-12') is None
    assert asyncio.run(store.signals.process(signal)) is None and len(shop.box.jobs) == 2
    async def clear(pending, closed):
        assert pending == [job]
        closed(job)
        return [job['request_id']]
    shop.box.recover = clear
    settled = asyncio.run(store.signals.reconcile(signal['id'], owner='peter'))
    assert settled['chain_status'] == 'failed'
    assert not shop.runs.pending_location_jobs(*shop.key) and len(shop.box.jobs) == 2
    assert shop.runs.claim(*shop.key, trigger='manual', stamp='changed', day='2026-10-12') is not None


def test_native_receipt_and_saved_publication_reconcile_without_reexecution(shop):
    go_live(shop, wake='never')
    adapter, store = connect(shop)
    job = native_callbacks(shop)
    manual(shop)
    signal = wakes(shop)[0]
    receipt = store.signals.receipts(signal['id'], owner='peter')[0]
    assert receipt['external_identity'] == job and stored(shop) is not None
    receipt['status'] = 'uncertain'
    store.signals._save_step(receipt)
    with store.connect() as db:
        current = store.wakes._get(db, signal['id'])
        current.update(chain_status='uncertain', status='pending')
        store.wakes._write(db, current)
    # A fresh host adapter has neither the original closure nor its live task.
    from retail_script_signals import ScriptSignals
    from agent_test_support import retail_agent_store
    store = retail_agent_store(shop.state)
    ScriptSignals(store, lambda owner, collection:shop.company, sandbox=shop.box, clock=lambda:shop.now[0])
    asyncio.run(store.signals.reconcile(signal['id'], owner='peter'))
    saved = store.signals.receipts(signal['id'], owner='peter')[0]
    assert saved['status'] == 'succeeded' and saved['evidence']['quiescent'] is True
    assert len(shop.box.jobs) == 2


def test_deadline_waits_for_original_native_cleanup_before_releasing_pass(shop):
    go_live(shop, wake='never')
    adapter, store = connect(shop)
    job = {'request_id':'cancelled-original', 'scope':{'company_id':shop.company.id,
        'owner_id':'peter', 'root_chat_id':'tracker-run-cancelled-original'}}
    stopped = []
    async def run(**options):
        shop.box.jobs.append({'cancel_test':True})
        await asyncio.to_thread(options['opened'], job)
        while not options['cancelled'].is_set():
            await asyncio.sleep(.005)
        await asyncio.sleep(.02)
        await asyncio.to_thread(options['closed'], job)
        stopped.append(True)
        return {'status':'failed', 'started':True, 'error':shop.scripts.fault('stopped')}
    shop.box.run = run
    store.signals.handlers['retail_script']['timeout'] = .05
    result = manual(shop)
    signal = wakes(shop)[0]
    receipt = store.signals.receipts(signal['id'], owner='peter')[0]
    assert stopped == [True] and receipt['cleanup']['quiescent'] is True
    assert receipt['external_identity'] == job and receipt['status'] == 'uncertain'
    assert result['status'] == 'completed' and not shop.runs.pending_location_jobs(*shop.key)
    assert not shop.runs.state(*shop.key)['running'] and stored(shop) is None


def test_current_script_revision_is_checked_again_at_direct_agent_claim(shop):
    store, definition = agent_for(shop)
    tracker = go_live(shop, agent_id=definition['id'], wake='every_run')
    from tracker_runner import script_wake_authorizer
    store.wake_authorizer = script_wake_authorizer({'state':shop.state,
        'monitoring':shop.company.monitoring, 'business_scope':shop.company.scope})
    adapter, store = connect(shop, store)
    manual(shop)
    signal = wakes(shop)[0]
    shop.store.set_policy(tracker, {'wake':'never'}, expected_revision=1)
    assert store.claim(definition['id'], {}, source_records=[], wake_id=signal['id']) is None


def test_retention_preserves_completed_publication_awaiting_reconciliation(shop):
    tracker = go_live(shop, wake='never')
    adapter, store = connect(shop)
    manual(shop)
    signal = wakes(shop)[0]
    snapshot = adapter._snapshot(signal)
    with store.connect() as db:
        signal.update(status='pending', chain_status='uncertain')
        store.wakes._write(db, signal)
    # Fifty newer completed inputs must leave this original recovery receipt intact.
    for index in range(51):
        value = {**snapshot, 'id':f"later-snapshot-{index}", 'source_scope':snapshot['source_scope']}
        adapter._save(shop.company, tracker, value)
    assert adapter._snapshot(signal)['result'] == snapshot['result']


def test_refused_signal_enqueue_removes_its_unadmitted_private_input(shop, monkeypatch):
    go_live(shop, wake='never')
    adapter, store = connect(shop)
    def refuse(**options):
        raise ValueError('Process the pending signals first.')
    monkeypatch.setattr(store.wakes, 'enqueue_job', refuse)
    manual(shop)
    with sqlite3.connect(shop.state/'location-intelligence.sqlite') as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='tracker_script_signal'").fetchone()[0] == 0
    assert len(shop.box.jobs) == 1 and stored(shop) is None


def test_uncertain_enqueue_acknowledgement_keeps_the_durable_signals_input(shop, monkeypatch):
    go_live(shop, wake='never')
    adapter, store = connect(shop)
    enqueue = store.wakes.enqueue_job
    def interrupted(**options):
        enqueue(**options)
        raise OSError('Commit acknowledgement interrupted')
    monkeypatch.setattr(store.wakes, 'enqueue_job', interrupted)
    manual(shop)
    signal = wakes(shop)[0]
    assert adapter._snapshot(signal)['input_sha256'] == signal['payload']['input_sha256']
    assert len(shop.box.jobs) == 1 and stored(shop) is None
