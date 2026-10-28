"""Messaging authority and actual Meta wire format using local transports only."""
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from messaging import Messaging, MessagingActor, MessagingError
from messaging_provider import MetaWhatsApp, MetaConfiguration, MetaCredentials, Template, ProviderError


def fixture(tmp_path, *, clock=None, handler=None, authorized=lambda actor: True, otp=True):
    now = clock or [1_800_000_000.0]
    sent = []
    def transport(request):
        body = json.loads(request.content)
        sent.append(body)
        if handler:
            return handler(request)
        return httpx.Response(200, json={'messaging_product':'whatsapp',
            'messages':[{'id':f'wamid.out-{len(sent)}'}]})
    credentials = MetaCredentials('synthetic-access-token', 'synthetic-app-secret', 'synthetic-verify-token')
    config = MetaConfiguration(company_id='company', waba_id='12345', phone_number_id='67890',
        sender_phone='+34600000000', api_version='v25.0', commissioned=True, webhook_ready=True,
        otp_template=Template('security_code', 'en_US', 'AUTHENTICATION', 'APPROVED') if otp else None,
        hello_template=Template('setup_hello', 'en_US', 'UTILITY', 'APPROVED', 'Hello from Rocoto. Reply here.'))
    provider = MetaWhatsApp(config, lambda: credentials, transport=httpx.MockTransport(transport))
    store = Messaging(tmp_path, company_id='company', provider=provider, authorized=authorized, clock=lambda: now[0])
    actor = MessagingActor('company', 'alice', 'shop', 'setup-alice', 'alice')
    return store, actor, provider, sent, now


async def enroll(store, actor, sent):
    challenge = await store.request_code(actor, '+34611111111', request_id='otp-1')
    code = sent[-1]['template']['components'][0]['parameters'][0]['text']
    result = store.verify_code(actor, challenge['id'], code)
    assert result['verified']
    return challenge, code


def webhook(provider, messages=None, statuses=None, *, account='12345', phone='67890'):
    body = json.dumps({'object':'whatsapp_business_account','entry':[{'id':account,'changes':[
        {'field':'messages','value':{'messaging_product':'whatsapp','metadata':{'phone_number_id':phone},
            'messages':messages or [], 'statuses':statuses or []}}]}]}).encode()
    signature = 'sha256=' + hmac.new(b'synthetic-app-secret', body, hashlib.sha256).hexdigest()
    return body, signature


def incoming(now, *, id='wamid.in-1', context=None, text='Hello back', sender='34611111111'):
    value = {'id':id,'from':sender,'timestamp':str(int(now[0])),'type':'text','text':{'body':text}}
    if context:
        value['context'] = {'id':context}
    return value


def test_default_readiness_is_honest_and_browsing_creates_no_store(tmp_path):
    store = Messaging(tmp_path, company_id='company', authorized=lambda actor: True)
    actor = MessagingActor('company', 'alice', 'shop', 'setup-alice', 'alice')
    assert store.describe(actor)['readiness']['reason'] == 'provider_setup_required'
    assert not list(tmp_path.iterdir())
    with pytest.raises(MessagingError):
        asyncio.run(store.request_code(actor, '+34611111111'))
    assert not list(tmp_path.iterdir())


def test_authentication_template_is_separate_from_ordinary_approved_templates(tmp_path):
    store, actor, _, sent, _ = fixture(tmp_path, otp=False)
    assert store.describe(actor)['readiness']['reason'] == 'auth_template_required'
    with pytest.raises(MessagingError):
        asyncio.run(store.request_code(actor, '+34611111111'))
    assert sent == []


def test_real_template_wire_private_code_verify_hello_and_restart(tmp_path):
    async def scenario():
        store, actor, provider, sent, now = fixture(tmp_path)
        challenge, code = await enroll(store, actor, sent)
        assert sent[0]['type'] == 'template'
        assert sent[0]['template']['components'][1]['sub_type'] == 'url'
        assert code not in json.dumps(challenge)
        with sqlite3.connect(store.path) as db:
            assert code not in '\n'.join(db.iterdump())
        hello = await store.send_hello(actor, request_id='hello-1')
        assert hello['status'] == 'sent' and hello['waiting_for_reply'] and hello['finish_turn']
        assert sent[1]['type'] == 'template'  # Receiving an OTP did not open the 24h reply window.
        again = await store.send_hello(actor, request_id='hello-1')
        assert again['id'] == hello['id'] and len(sent) == 2
        restored = Messaging(tmp_path, company_id='company', provider=provider, authorized=lambda actor: True, clock=lambda: now[0])
        assert restored.describe(actor)['contact']['phone'] == '+34611111111'
        assert restored.describe(actor)['waiting']
    asyncio.run(scenario())


@pytest.mark.parametrize('phone', ['34611111111', '+034611111111', '+34 611111111', '+1', '+1234567890123456', '+34611111111\n'])
def test_strict_e164_before_network(tmp_path, phone):
    store, actor, _, sent, _ = fixture(tmp_path)
    with pytest.raises(MessagingError):
        asyncio.run(store.request_code(actor, phone))
    assert not sent


def test_expiry_attempt_caps_resend_and_cross_actor_scope(tmp_path):
    async def scenario():
        store, actor, _, sent, now = fixture(tmp_path)
        challenge = await store.request_code(actor, '+34611111111')
        code = sent[0]['template']['components'][0]['parameters'][0]['text']
        admin = MessagingActor('company','alice','shop','setup-alice','admin')
        with pytest.raises(MessagingError):store.verify_code(admin, challenge['id'], code)
        foreign = MessagingActor('other','alice','shop','setup-alice','alice')
        with pytest.raises(MessagingError):store.verify_code(foreign, challenge['id'], code)
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(5):
            with pytest.raises(MessagingError):store.verify_code(actor, challenge['id'], wrong)
        with pytest.raises(MessagingError):store.verify_code(actor, challenge['id'], code)
        with pytest.raises(MessagingError):await store.request_code(actor, '+34611111111')
        now[0] += 61
        replacement = await store.request_code(actor, '+34611111111')
        correct = sent[-1]['template']['components'][0]['parameters'][0]['text']
        now[0] += 301
        with pytest.raises(MessagingError):store.verify_code(actor, replacement['id'], correct)
        assert len(sent) == 2
    asyncio.run(scenario())


def test_signed_reply_context_dedup_setup_fallback_and_admission(tmp_path):
    async def scenario():
        store, actor, provider, sent, now = fixture(tmp_path)
        await enroll(store, actor, sent)
        await store.send_hello(actor, request_id='hello')
        body, signature = webhook(provider, [incoming(now)])
        assert store.handle_webhook(body, signature)['accepted'] == 1
        assert store.handle_webhook(body, signature)['duplicates'] == 1
        reply = store.pending_replies()[0]
        assert reply['chat_id'] == 'setup-alice' and reply['member_id'] == 'alice'
        claim = store.claim_reply(reply['id'])
        assert claim['admission_key'] == reply['admission_key']
        assert store.claim_reply(reply['id']) is None
        store.finish_reply(reply['id'], claim['claim_id'], admitted=True)
        assert store.pending_replies() == []
        agent = MessagingActor('company','alice','shop','agent-chat','alice')
        store.bind_conversation(agent)
        outbound = await store.send_message(agent, 'What changed today?', request_id='agent-1')
        assert outbound['finish_turn'] and sent[-1]['type'] == 'text'
        body, signature = webhook(provider, [incoming(now, id='wamid.in-2', context=outbound['provider_message_id'])])
        store.handle_webhook(body, signature)
        assert store.pending_replies()[0]['chat_id'] == 'agent-chat'
        body, signature = webhook(provider, [incoming(now, id='wamid.in-3', context='unknown-reply')])
        store.handle_webhook(body, signature)
        assert next(x for x in store.pending_replies() if x['provider_event_id']=='wamid.in-3')['chat_id'] == 'setup-alice'
    asyncio.run(scenario())


def test_webhook_signature_account_timestamp_and_member_revocation(tmp_path):
    async def scenario():
        current = [True]
        store, actor, provider, sent, now = fixture(tmp_path, authorized=lambda _: current[0])
        await enroll(store, actor, sent)
        body, signature = webhook(provider, [incoming(now)])
        with pytest.raises(MessagingError):store.handle_webhook(body+b' ', signature)
        other, signed = webhook(provider, [incoming(now)], account='99999')
        with pytest.raises(MessagingError):store.handle_webhook(other, signed)
        current[0] = False
        assert store.handle_webhook(body, signature)['blocked'] == 1
        assert store.pending_replies() == []
        with pytest.raises(MessagingError):await store.send_hello(actor, request_id='revoked')
    asyncio.run(scenario())


def test_lost_send_fences_replays_and_changed_argument_retry(tmp_path):
    async def scenario():
        store, actor, provider, sent, now = fixture(tmp_path)
        await enroll(store, actor, sent)
        async def lost(request):
            raise httpx.ReadTimeout('synthetic-access-token MUST NEVER LEAK')
        provider.transport = httpx.MockTransport(lost)
        result = await store.send_hello(actor, request_id='lost')
        assert result['status'] == 'uncertain'
        assert 'synthetic-access-token' not in json.dumps(result)
        assert (await store.send_hello(actor, request_id='lost'))['status'] == 'uncertain'
        with pytest.raises(MessagingError):await store.send_hello(actor, request_id='changed-id')
        assert store.describe(actor)['outbox'][0]['status'] == 'uncertain'
    asyncio.run(scenario())


def test_cancellation_after_dispatch_drains_and_retains_real_receipt(tmp_path):
    async def scenario():
        store, actor, provider, sent, _ = fixture(tmp_path)
        await enroll(store, actor, sent)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(request):
            entered.set();await release.wait()
            return httpx.Response(200, json={'messages':[{'id':'wamid.slow'}]})
        provider.transport = httpx.MockTransport(slow)
        task = asyncio.create_task(store.send_hello(actor, request_id='slow'))
        await entered.wait();task.cancel();await asyncio.sleep(0)
        task.cancel();assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert store.describe(actor)['outbox'][0]['provider_message_id'] == 'wamid.slow'
        assert store.describe(actor)['outbox'][0]['status'] == 'sent'
    asyncio.run(scenario())


def test_concurrent_verification_requests_share_one_dispatch(tmp_path):
    async def scenario():
        store, actor, _, sent, _ = fixture(tmp_path)
        results = await asyncio.gather(*(store.request_code(actor, '+34611111111', request_id='same') for _ in range(8)))
        assert len({item['id'] for item in results}) == 1 and len(sent) == 1
        with pytest.raises(MessagingError):
            await store.request_code(actor, '+34622222222', request_id='same')
    asyncio.run(scenario())


def test_duplicate_phone_cannot_be_claimed_by_another_member(tmp_path):
    async def scenario():
        store, actor, _, sent, now = fixture(tmp_path)
        await enroll(store, actor, sent)
        now[0] += 61
        bob = MessagingActor('company', 'bob', 'shop', 'setup-bob', 'bob')
        challenge = await store.request_code(bob, '+34611111111')
        code = sent[-1]['template']['components'][0]['parameters'][0]['text']
        with pytest.raises(MessagingError):store.verify_code(bob, challenge['id'], code)
        assert store.describe(bob)['contact'] is None
        assert store.describe(actor)['contact']['verified']
    asyncio.run(scenario())


def test_admin_may_request_but_only_target_member_verifies(tmp_path):
    async def scenario():
        store, actor, _, sent, _ = fixture(tmp_path)
        admin = MessagingActor('company', 'alice', 'shop', 'setup-alice', 'admin')
        challenge = await store.request_code(admin, '+34611111111')
        code = sent[-1]['template']['components'][0]['parameters'][0]['text']
        with pytest.raises(MessagingError):store.verify_code(admin, challenge['id'], code)
        assert store.verify_code(actor, challenge['id'], code)['verified']
    asyncio.run(scenario())


def test_company_database_binding_refuses_substitution(tmp_path):
    async def scenario():
        store, actor, _, sent, _ = fixture(tmp_path)
        await enroll(store, actor, sent)
        other = Messaging(tmp_path, company_id='other', authorized=lambda actor: True)
        with pytest.raises(MessagingError):
            other.describe(MessagingActor('other','alice','shop','setup-alice','alice'))
    asyncio.run(scenario())


def test_service_window_requires_real_inbound_and_expires(tmp_path):
    async def scenario():
        store, actor, provider, sent, now = fixture(tmp_path)
        await enroll(store, actor, sent)
        with pytest.raises(MessagingError):await store.send_message(actor, 'Hello', request_id='one')
        body, signature = webhook(provider, [incoming(now)])
        store.handle_webhook(body, signature)
        assert (await store.send_message(actor, 'Hello', request_id='one'))['status']=='sent'
        now[0] += 86400
        with pytest.raises(MessagingError):await store.send_message(actor, 'Again', request_id='two')
        assert len(sent)==2
    asyncio.run(scenario())


def test_revoke_and_reverify_changes_generation_and_closes_old_replies(tmp_path):
    async def scenario():
        store, actor, provider, sent, now = fixture(tmp_path)
        await enroll(store, actor, sent)
        store.handle_webhook(*webhook(provider,[incoming(now)]))
        old = store.pending_replies()[0]
        initial = store.describe(actor)['contact']['generation']
        store.revoke(actor)
        assert store.pending_replies()==[] and store.describe(actor)['contact'] is None
        with pytest.raises(MessagingError):await store.send_hello(actor,request_id='revoked')
        now[0]+=61
        challenge=await store.request_code(actor,'+34611111111',request_id='new-enrollment')
        store.verify_code(actor,challenge['id'],sent[-1]['template']['components'][0]['parameters'][0]['text'])
        assert store.describe(actor)['contact']['generation']>initial
        assert store.claim_reply(old['id']) is None
    asyncio.run(scenario())


@pytest.mark.parametrize('status', ['delivered','read','failed'])
def test_delivery_events_are_separate_from_replies_and_preserve_real_state(tmp_path,status):
    async def scenario():
        store,actor,provider,sent,now=fixture(tmp_path)
        await enroll(store,actor,sent)
        outgoing=await store.send_hello(actor,request_id='hello')
        event={'id':outgoing['provider_message_id'],'status':status,'timestamp':str(int(now[0])),
               'recipient_id':'34611111111'}
        assert store.handle_webhook(*webhook(provider,statuses=[event]))['delivery_updates']==1
        assert store.handle_webhook(*webhook(provider,statuses=[event]))['delivery_updates']==0
        assert store.describe(actor)['outbox'][0]['status']==status
        assert store.pending_replies()==[]
    asyncio.run(scenario())


def test_delivery_before_http_receipt_is_retained_and_applied(tmp_path):
    async def scenario():
        store,actor,provider,sent,now=fixture(tmp_path)
        await enroll(store,actor,sent)
        async def reply(request):
            event={'id':'wamid.racing','status':'delivered','timestamp':str(int(now[0])),
                   'recipient_id':'34611111111'}
            store.handle_webhook(*webhook(provider,statuses=[event]))
            return httpx.Response(200,json={'messages':[{'id':'wamid.racing'}]})
        provider.transport=httpx.MockTransport(reply)
        assert (await store.send_hello(actor,request_id='hello'))['status']=='delivered'
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['stale','future','nontext','wrong_sender','echo'])
def test_unverified_stale_nontext_and_echo_events_never_resume_chat(tmp_path,change):
    async def scenario():
        store,actor,provider,sent,now=fixture(tmp_path)
        await enroll(store,actor,sent)
        message=incoming(now)
        if change=='stale':message['timestamp']=str(int(now[0]-8*86400))
        elif change=='future':message['timestamp']=str(int(now[0]+301))
        elif change=='nontext':message['type']='image'
        elif change=='wrong_sender':message['from']='34699999999'
        else:message['from']='34600000000'
        store.handle_webhook(*webhook(provider,[message]))
        assert store.pending_replies()==[]
    asyncio.run(scenario())


def test_reply_is_rechecked_at_claim_and_admission_uncertainty_is_not_replayed(tmp_path):
    async def scenario():
        current=[True]
        store,actor,provider,sent,now=fixture(tmp_path,authorized=lambda actor:current[0])
        await enroll(store,actor,sent)
        store.handle_webhook(*webhook(provider,[incoming(now)]))
        reply=store.pending_replies()[0]
        current[0]=False
        with pytest.raises(MessagingError):store.claim_reply(reply['id'])
        current[0]=True
        claim=store.claim_reply(reply['id'])
        store.finish_reply(reply['id'],claim['claim_id'],admitted=False)
        assert store.pending_replies()==[] and store.claim_reply(reply['id']) is None
        store.finish_reply(reply['id'],claim['claim_id'],admitted=True) # Host reconciled exact admission key.
        with pytest.raises(MessagingError):store.finish_reply(reply['id'],'wrong',admitted=True)
    asyncio.run(scenario())


def test_crash_recovery_keeps_send_and_chat_admission_uncertain(tmp_path):
    async def scenario():
        store,actor,provider,sent,now=fixture(tmp_path)
        await enroll(store,actor,sent)
        await store.send_hello(actor,request_id='hello')
        store.handle_webhook(*webhook(provider,[incoming(now)]))
        reply=store.pending_replies()[0];store.claim_reply(reply['id'])
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET status='dispatching' WHERE kind='hello'")
        restarted=Messaging(tmp_path,company_id='company',provider=provider,authorized=lambda actor:True,clock=lambda:now[0])
        assert restarted.recover()=={'uncertain_sends':1,'uncertain_replies':1}
        assert restarted.pending_replies()==[]
        with pytest.raises(MessagingError):await restarted.send_hello(actor,request_id='new')
        assert len(sent)==2
    asyncio.run(scenario())


@pytest.mark.parametrize('response',[302,401,429,'invalid_json','oversize'])
def test_provider_one_attempt_bounded_safe_errors(tmp_path,response):
    calls=[]
    async def scenario():
        store,actor,provider,sent,_=fixture(tmp_path)
        await enroll(store,actor,sent)
        def bad(request):
            calls.append(request)
            if response=='invalid_json':return httpx.Response(200,text='synthetic-access-token')
            if response=='oversize':return httpx.Response(200,content=b'a'*65537)
            return httpx.Response(response,headers={'location':'https://evil.example/token'},text='synthetic-access-token')
        provider.transport=httpx.MockTransport(bad)
        receipt=await store.send_hello(actor,request_id='failure')
        assert receipt['status']==('failed' if response==401 else 'uncertain')
        assert 'synthetic-access-token' not in json.dumps(receipt)
        assert len(calls)==1 and str(calls[0].url)=='https://graph.facebook.com/v25.0/67890/messages'
        assert (await store.send_hello(actor,request_id='failure'))['id']==receipt['id']
        assert len(calls)==1
    asyncio.run(scenario())


def test_hello_template_unavailable_and_provider_secret_reprs(tmp_path):
    from dataclasses import replace
    store,actor,provider,sent,_=fixture(tmp_path)
    provider.config=replace(provider.config,hello_template=None)
    async def scenario():
        await enroll(store,actor,sent)
        assert store.describe(actor)['readiness']['hello_reason']=='hello_template_required'
        with pytest.raises(MessagingError):await store.send_hello(actor,request_id='hello')
        assert len(sent)==1
    asyncio.run(scenario())
    assert 'synthetic' not in repr(provider.credentials())
    assert provider.verify_subscription('subscribe','synthetic-verify-token','challenge')=='challenge'
    with pytest.raises(ProviderError):provider.verify_subscription('subscribe','wrong','challenge')


def test_stop_before_send_and_shutdown_admission_are_closed(tmp_path):
    async def scenario():
        store,actor,_,sent,_=fixture(tmp_path)
        stop=asyncio.Event();stop.set()
        with pytest.raises(asyncio.CancelledError):await store.request_code(actor,'+34611111111',cancelled=stop)
        assert sent==[]
        await store.close()
        with pytest.raises(MessagingError):await store.request_code(actor,'+34611111111')
        assert sent==[]
    asyncio.run(scenario())


def test_verification_commit_rechecks_revoked_member_after_code_hash(tmp_path):
    async def scenario():
        current=[True]
        store,actor,provider,sent,_=fixture(tmp_path,authorized=lambda _:current[0])
        challenge=await store.request_code(actor,'+34611111111')
        code=sent[-1]['template']['components'][0]['parameters'][0]['text']
        original=provider.otp_hash
        def revoking_hash(*args):
            value=original(*args);current[0]=False;return value
        provider.otp_hash=revoking_hash
        with pytest.raises(MessagingError):store.verify_code(actor,challenge['id'],code)
        current[0]=True
        assert store.describe(actor)['contact'] is None
    asyncio.run(scenario())


def test_receipt_readback_waits_for_short_concurrent_database_commit(tmp_path):
    """A parallel send must not convert a durable receipt into a transient 500."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    store, actor, _, sent, _ = fixture(tmp_path)
    asyncio.run(enroll(store,actor,sent))
    writer = sqlite3.connect(store.path)
    writer.execute('BEGIN EXCLUSIVE')
    entered = threading.Event()
    def read():
        entered.set()
        return store.describe(actor)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read)
        assert entered.wait(2)
        assert not future.done()
        writer.commit(); writer.close()
        assert future.result(timeout=2)['contact']['verified']


def test_stop_remains_responsive_while_dispatch_authority_read_waits(tmp_path, monkeypatch):
    import threading
    async def scenario():
        store, actor, _, sent, _ = fixture(tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = store._current
        def current(*args):
            entered.set()
            assert release.wait(3)
            return original(*args)
        monkeypatch.setattr(store,'_current',current)
        stop = asyncio.Event()
        request = asyncio.create_task(store.request_code(actor,'+34611111111',request_id='slow-check',cancelled=stop))
        assert await asyncio.to_thread(entered.wait,2)
        # A live host Stop can execute while the DB probe is waiting in a thread.
        stop.set(); await asyncio.sleep(0); release.set()
        with pytest.raises(asyncio.CancelledError): await request
        assert store.describe(actor)['challenge']['status'] == 'cancelled' and sent == []
    asyncio.run(scenario())


def test_pending_replies_scan_past_revoked_members_without_retiring_transient_authority(tmp_path, monkeypatch):
    import messaging
    async def scenario():
        allowed = {'alice', 'bob'}
        checked = []
        def authorized(actor):
            checked.append(actor.member_id)
            return actor.member_id in allowed
        store, alice, provider, sent, now = fixture(tmp_path, authorized=authorized)
        await enroll(store, alice, sent)
        bob = MessagingActor('company', 'bob', 'shop', 'setup-bob', 'bob')
        challenge = await store.request_code(bob, '+34622222222')
        code = sent[-1]['template']['components'][0]['parameters'][0]['text']
        store.verify_code(bob, challenge['id'], code)
        for index in range(9):
            store.handle_webhook(*webhook(provider, [incoming(now, id=f'wamid.alice-{index}')]))
            now[0] += 1
        store.handle_webhook(*webhook(provider, [incoming(now, id='wamid.bob', sender='34622222222')]))
        allowed.remove('alice')
        # Each tick has bounded authority work, and its keyset progresses even
        # when more stale rows exist than one tick's entire scan allowance.
        monkeypatch.setattr(messaging, 'MAX_PENDING_SCAN', 4, raising=False)
        replies = []
        for _ in range(3):
            checked.clear()
            replies = store.pending_replies(limit=8)
            assert len(checked) <= 4
            if replies:
                break
        assert [reply['member_id'] for reply in replies] == ['bob']
        claim = store.claim_reply(replies[0]['id'])
        store.finish_reply(claim['id'], claim['claim_id'], admitted=True)
        # A temporarily busy membership store must not permanently discard a
        # valid signed reply. Once authority recovers, the scan wraps to it.
        allowed.add('alice')
        restored = store.pending_replies(limit=8)
        assert restored and all(reply['member_id'] == 'alice' for reply in restored)
        assert restored[0]['provider_event_id'] == 'wamid.alice-0'
        assert len(sent) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('malformed', ['\u00e9', '\ud800', 'x' * 8193])
def test_provider_untrusted_verification_strings_are_private_protocol_errors(tmp_path, malformed):
    _, _, provider, sent, _ = fixture(tmp_path)
    with pytest.raises(ProviderError, match='Verify this messaging subscription'):
        provider.verify_subscription('subscribe', malformed, 'challenge')
    raw, _ = webhook(provider)
    with pytest.raises(ProviderError, match='Verify this messaging event'):
        provider.webhook(raw, malformed)
    assert sent == []


def test_approved_utility_outside_window_is_exact_and_idempotent(tmp_path):
    async def exercise():
        from dataclasses import replace
        store,actor,provider,sent,now=fixture(tmp_path)
        provider.config=replace(provider.config, utility_templates=(Template(
            'rocoto_urgent_alert_v1','en_US','UTILITY','APPROVED',
            'Urgent alert for {{1}}: {{2}}. Recorded {{3}}. Reply with an update.'),))
        await enroll(store,actor,sent)
        store.bind_conversation(actor)
        alert={'purpose':'urgent','language':'en_US','parameters':['Kitchen','Blocked exit','09:00 EEST']}
        result=await store.send_alert(actor,alert,request_id='alert-1')
        assert result['status']=='sent'
        assert sent[-1]['type']=='template'
        assert sent[-1]['template']['name']=='rocoto_urgent_alert_v1'
        assert sent[-1]['template']['components'][0]['parameters'][1]=={'type':'text','text':'Blocked exit'}
        count=len(sent)
        assert (await store.send_alert(actor,alert,request_id='alert-1'))['id']==result['id']
        assert len(sent)==count
        with pytest.raises(ValueError):
            await store.send_message(actor,'Free text',request_id='free-outside-window')
    asyncio.run(exercise())


def test_pending_utility_and_missing_language_cannot_send(tmp_path):
    async def exercise():
        from dataclasses import replace
        store,actor,provider,sent,now=fixture(tmp_path)
        provider.config=replace(provider.config, utility_templates=(Template(
            'rocoto_task_reminder_v1','en_US','UTILITY','PENDING','Task {{1}} due {{2}}: {{3}}.'),))
        for language in ['en_US','es']:
            with pytest.raises(ProviderError):
                await store.send_alert(actor,{'purpose':'reminder','language':language,
                    'parameters':['Opening','09:00','Check room']},request_id='pending-'+language)
        assert sent==[]
    asyncio.run(exercise())