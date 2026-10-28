"""Verified replies use the shared signal queue and ordinary chat receipts."""
import asyncio
from types import SimpleNamespace

from background_agents import AgentStore
from messaging_signals import MessagingSignals
from test_messaging import fixture, enroll, webhook, incoming


def test_verified_reply_queues_once_and_reconciles_saved_message(tmp_path):
    async def run():
        messaging, actor, provider, sent, now = fixture(tmp_path)
        await enroll(messaging, actor, sent)
        body, signature = webhook(provider, [incoming(now)])
        messaging.handle_webhook(body, signature)
        store = AgentStore(tmp_path)
        agent = SimpleNamespace(children={'setup-alice': {'id':'setup-alice', 'owner_principal_id':'alice',
            'dataset':'shop', 'messages':[]}})
        bridge = MessagingSignals(messaging, store, agent, member=lambda actor:{'display_name':'Alice'})
        store.signal_authority = bridge.current
        await bridge.tick(); await bridge.tick()
        with store.connect() as db:
            signals = db.execute('SELECT body FROM agent_wakes').fetchall()
            assert len(signals) == 1
        import json
        signal = json.loads(signals[0]['body'])
        assert signal['actor_principal_id'] == 'alice' and signal['text'] == 'Hello back'
        assert signal['sender']['channel'] == 'whatsapp'
        assert messaging.pending_replies() and bridge.current(signal)
        claimed = store.wakes.claim_chat(signal['id'], owner='alice')
        agent.children['setup-alice']['messages'].append({'role':'user', 'content':'Hello back',
            'channel':{'admission_key':signal['sender']['admission_key']}})
        store.wakes.finish_chat(signal['id'], claimed['run_id'], owner='alice', status='completed')
        await bridge.tick()
        assert messaging.pending_replies() == []
        assert not bridge.current(signal)
        assert store.signals.usage()['turn']['used'] == 1
    asyncio.run(run())


def test_contact_revocation_stops_already_queued_reply(tmp_path):
    async def run():
        current = [True]
        messaging, actor, provider, sent, now = fixture(tmp_path, authorized=lambda _:current[0])
        await enroll(messaging, actor, sent)
        body, signature = webhook(provider, [incoming(now)])
        messaging.handle_webhook(body, signature)
        store = AgentStore(tmp_path)
        agent = SimpleNamespace(children={'setup-alice': {'id':'setup-alice', 'owner_principal_id':'alice',
            'dataset':'shop', 'messages':[]}})
        bridge = MessagingSignals(messaging, store, agent, member=lambda actor:{'display_name':'Alice'})
        store.signal_authority = bridge.current
        await bridge.tick()
        import json
        with store.connect() as db: signal = json.loads(db.execute('SELECT body FROM agent_wakes').fetchone()['body'])
        forged = {**signal, 'text':'Changed'}
        assert not bridge.current(forged)
        current[0] = False
        assert not bridge.current(signal)
        assert store.wakes.claim_chat(signal['id'], owner='alice') is None
        assert store.signals.usage()['turn']['used'] == 0
    asyncio.run(run())


def test_queue_failure_leaves_provider_reply_retryable(tmp_path):
    async def run():
        messaging, actor, provider, sent, now = fixture(tmp_path)
        await enroll(messaging, actor, sent)
        body, signature = webhook(provider, [incoming(now)])
        messaging.handle_webhook(body, signature)
        store = AgentStore(tmp_path)
        agent = SimpleNamespace(children={'setup-alice': {'id':'setup-alice', 'owner_principal_id':'alice',
            'dataset':'shop', 'messages':[]}})
        bridge = MessagingSignals(messaging, store, agent, member=lambda actor:{'display_name':'Alice'})
        store.signal_authority = lambda signal: False
        await bridge.tick()
        assert len(messaging.pending_replies()) == 1
        store.signal_authority = bridge.current
        await bridge.tick()
        with store.connect() as db: assert db.execute('SELECT COUNT(*) FROM agent_wakes').fetchone()[0] == 1
    asyncio.run(run())


def test_admission_receipt_survives_restart_and_does_not_end_live_authority(tmp_path):
    async def run():
        messaging, actor, provider, sent, now = fixture(tmp_path)
        await enroll(messaging, actor, sent)
        body, signature = webhook(provider, [incoming(now)])
        messaging.handle_webhook(body, signature)
        store = AgentStore(tmp_path)
        agent = SimpleNamespace(children={'setup-alice': {'id':'setup-alice', 'owner_principal_id':'alice',
            'dataset':'shop', 'messages':[]}})
        bridge = MessagingSignals(messaging, store, agent, member=lambda actor:{'display_name':'Alice'})
        store.signal_authority = bridge.current
        await bridge.tick()
        import json
        with store.connect() as db: signal = json.loads(db.execute('SELECT body FROM agent_wakes').fetchone()['body'])
        claimed = store.wakes.claim_chat(signal['id'], owner='alice')
        reply = messaging.pending_replies()[0]
        inbox_claim = messaging.claim_reply(reply['id'])
        agent.children['setup-alice']['messages'].append({'role':'user', 'content':'Hello back',
            'channel':{'admission_key':reply['admission_key']}})
        messaging.recover()
        assert messaging.unsettled_replies() and bridge.current(claimed)
        await bridge.tick()
        assert messaging.unsettled_replies() == [] and bridge.current(claimed)
        assert not bridge.current(signal)
        assert store.signals.usage()['turn']['used'] == 1
    asyncio.run(run())


def test_terminal_signal_is_held_after_display_name_changes(tmp_path):
    async def run():
        messaging, actor, provider, sent, now = fixture(tmp_path)
        await enroll(messaging, actor, sent)
        body, signature = webhook(provider, [incoming(now)])
        messaging.handle_webhook(body, signature)
        store = AgentStore(tmp_path)
        agent = SimpleNamespace(children={'setup-alice': {'id':'setup-alice', 'owner_principal_id':'alice',
            'dataset':'shop', 'messages':[]}})
        member = {'display_name':'Alice'}
        bridge = MessagingSignals(messaging, store, agent, member=lambda actor:member)
        store.signal_authority = bridge.current
        await bridge.tick()
        import json
        with store.connect() as db: signal = json.loads(db.execute('SELECT body FROM agent_wakes').fetchone()['body'])
        claimed = store.wakes.claim_chat(signal['id'], owner='alice')
        store.wakes.finish_chat(signal['id'], claimed['run_id'], owner='alice', status='uncertain', admitted=False)
        member['display_name'] = 'Alice Updated'
        await bridge.tick(); await bridge.tick()
        assert messaging.pending_replies() == []
        assert len(messaging.unsettled_replies()) == 1
        assert store.signals.usage()['turn']['used'] == 1
    asyncio.run(run())


def test_verified_reply_runs_through_common_dispatcher_and_shared_limits(tmp_path):
    async def run():
        from signal_dispatcher import SignalDispatcher
        messaging, actor, provider, sent, now = fixture(tmp_path)
        await enroll(messaging, actor, sent)
        body, signature = webhook(provider, [incoming(now)])
        messaging.handle_webhook(body, signature)
        store = AgentStore(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        class Agent:
            def __init__(self):
                self.children={'setup-alice': {'id':'setup-alice', 'owner_principal_id':'alice',
                    'dataset':'shop', 'messages':[]}}
                self.active={};self.child_tasks={};self.calls=[]
            def _save_child(self, child):pass
            async def turn(self, text, chat_id, collection, **options):
                self.calls.append((text,chat_id,collection,options))
                self.children[chat_id]['messages'].append({'role':'user','content':text,'channel':options['sender']})
                entered.set();await release.wait()
                assert store.wakes.signal_current(self.children[chat_id]['active_signal_id'])
                yield {'type':'done','status':'completed'}
        class Scheduler:
            async def tick(self):pass
        agent=Agent();slots=asyncio.Semaphore(2)
        bridge=MessagingSignals(messaging,store,agent,member=lambda actor:{'display_name':'Alice'},
            answered=lambda reply:{'title':'Stock changed'})
        store.signal_authority=bridge.current
        dispatcher=SignalDispatcher(store,agent,Scheduler(),slots,SimpleNamespace(tick=lambda:None))
        await bridge.tick();await dispatcher.tick();await entered.wait()
        await bridge.tick()  # admission receipt is reconciled while the same turn remains live
        assert messaging.pending_replies()==[] and len(agent.calls)==1
        assert 'Stock changed' in agent.calls[0][3]['model_context']
        release.set();await dispatcher.task;await asyncio.sleep(0)
        await bridge.tick();await dispatcher.tick()
        assert len(agent.calls)==1 and store.signals.usage()['turn']['used']==1
        assert slots._value==2
        import json
        with store.connect() as db: signal=json.loads(db.execute('SELECT body FROM agent_wakes').fetchone()['body'])
        assert signal['status']=='completed'
        await dispatcher.close()
    asyncio.run(run())


def test_direct_phone_sends_share_daily_effect_cap_and_keep_uncertain_units(tmp_path):
    async def run():
        import httpx
        from messaging_signals import reserve_send
        messaging, actor, provider, sent, now = fixture(tmp_path)
        store=AgentStore(tmp_path)
        messaging.send_admission=lambda actor, row:reserve_send(store, actor, row)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            assert store.signals.reserve(db,'send','prior-effects',amount=198)
        await enroll(messaging,actor,sent)
        assert store.signals.usage()['send']['used']==199
        async def lost(request):raise httpx.ReadTimeout('synthetic lost reply')
        provider.transport=httpx.MockTransport(lost)
        result=await messaging.send_hello(actor,request_id='hello')
        assert result['status']=='uncertain'
        assert store.signals.usage()['send']['used']==200
        assert (await messaging.send_hello(actor,request_id='hello'))['status']=='uncertain'
        assert store.signals.usage()['send']['used']==200
        assert not reserve_send(store,actor,{'id':'next','channel':'whatsapp'})
    asyncio.run(run())


def test_contact_revocation_during_send_quota_admission_stops_provider_request(tmp_path):
    async def run():
        messaging, actor, provider, sent, now = fixture(tmp_path)
        await enroll(messaging, actor, sent)
        def admission(actor, row):
            messaging.revoke(actor)
            return True
        messaging.send_admission = admission
        result = await messaging.send_hello(actor, request_id='revoked-during-quota')
        assert result['status'] == 'cancelled'
        assert len(sent) == 1  # Only the enrollment OTP reached the provider.
    asyncio.run(run())


def test_unresolved_receipts_do_not_starve_later_saved_message(tmp_path):
    async def run():
        messaging, actor, provider, sent, now = fixture(tmp_path)
        await enroll(messaging, actor, sent)
        for i in range(9):
            now[0] += 1
            body, signature = webhook(provider, [incoming(now, id=f'wamid.receipt-{i}')])
            messaging.handle_webhook(body, signature)
            reply = messaging.pending_replies()[0]
            claim = messaging.claim_reply(reply['id'])
            messaging.finish_reply(reply['id'], claim['claim_id'], admitted=False)
        store = AgentStore(tmp_path)
        agent = SimpleNamespace(children={'setup-alice': {'id':'setup-alice', 'owner_principal_id':'alice',
            'dataset':'shop', 'messages':[{'role':'user', 'content':reply['text'],
                'channel':{'admission_key':reply['admission_key']}}]}})
        bridge = MessagingSignals(messaging, store, agent, member=lambda actor:{'display_name':'Alice'})
        await bridge.tick(); await bridge.tick()
        with messaging._db() as db:
            assert db.execute('SELECT status FROM inbox WHERE id=?', (reply['id'],)).fetchone()[0] == 'admitted'
            assert db.execute("SELECT count(*) FROM inbox WHERE status='admission_uncertain'").fetchone()[0] == 8
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM agent_wakes').fetchone()[0] == 0
    asyncio.run(run())
