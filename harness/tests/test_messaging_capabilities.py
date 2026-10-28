"""Email/voice cross the real outbox, signed ingress and common chat signal path."""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from capability_spend import CapabilitySpend
from messaging import Messaging, MessagingActor
from test_capability_providers import adapter


def fixture(tmp_path, kind, *, allowance=True, handler=None):
    now = [time.time()]
    calls = []
    def transport(request):
        body = json.loads(request.content) if kind == 'email' else parse_qs(request.content.decode())
        calls.append(body)
        if handler:
            return handler(request)
        return httpx.Response(200, json={'id': 'mail_' + str(len(calls))} if kind == 'email'
                              else {'sid': 'CA' + f'{len(calls):032x}'})
    provider = adapter(kind, httpx.MockTransport(transport))
    grant = {'grant_id': 'fixture-grant', 'channel': kind, 'account_id': provider.account_id,
             'expires_at': now[0] + 3600, 'stopped': False, 'unit_cost_microusd': 1000,
             'maximum_request_microusd': 10000, 'daily_microusd': 100000,
             'total_microusd': 200000}
    budget = CapabilitySpend(tmp_path, 'fixture', 'company', reader=lambda: [dict(grant)] if allowance else [])
    current = [True]
    store = Messaging(tmp_path, company_id='company', adapters=[provider], channel_names=(kind,),
                      authorized=lambda actor: current[0], clock=lambda: now[0], capability_spend=budget)
    actor = MessagingActor('company', 'alice', 'work', 'setup', 'alice')
    address = 'recipient@example.test' if kind == 'email' else '+12025550102'
    return store, actor, provider, calls, address, grant, current, now


async def enroll(store, actor, kind, address, calls):
    challenge = await store.request_code(actor, address, 'verify', channel=kind)
    text = calls[-1]['plain_text'] if kind == 'email' else calls[-1]['Twiml'][0]
    store.verify_code(actor, challenge['id'], re.search(r'code: ([0-9]{6})', text)[1])
    store.bind_conversation(actor, channel=kind)


def reply(provider, address, context, text='Reply in this chat', identifier='incoming_1'):
    if provider.channel == 'email':
        event = {'type': 'email.new', 'account_id': provider.account_id, 'payload': {
            'id': identifier, 'account_id': provider.account_id, 'from': [{'email': address}],
            'to': [{'email': provider.config.sender}], 'plain_text': text,
            'date': datetime.now(timezone.utc).isoformat(), 'in_reply_to': {'id': context} if context else None}}
        raw = json.dumps(event).encode(); stamp = str(int(time.time()))
        signature = hmac.new(b'b' * 32, stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        return raw, {'unipile-signature': f't={stamp},v0={signature}'}
    values = {'AccountSid': provider.account_id, 'CallSid': context, 'From': provider.config.sender,
              'To': address, 'Direction': 'outbound-api', 'SpeechResult': text}
    raw = urlencode(values).encode()
    material = provider.config.callback_url + ''.join(key + values[key] for key in sorted(values))
    signature = base64.b64encode(hmac.new(b'c' * 32, material.encode(), hashlib.sha1).digest()).decode()
    return raw, {'x-twilio-signature': signature}


@pytest.mark.parametrize('kind', ['email', 'voice'])
def test_channel_send_reply_is_same_ordinary_chat_signal(tmp_path, kind):
    async def run():
        from background_agents import AgentStore
        from messaging_signals import MessagingSignals
        store, actor, provider, calls, address, grant, current, now = fixture(tmp_path, kind)
        await enroll(store, actor, kind, address, calls)
        sent = await store.send_message(actor, 'Please reply', request_id='message', channel=kind)
        assert sent['status'] == 'sent' and sent['finish_turn']
        again = await store.send_message(actor, 'Please reply', request_id='message', channel=kind)
        assert again['id'] == sent['id'] and len(calls) == 2
        raw, headers = reply(provider, address, sent['provider_message_id'])
        assert store.handle_webhook(raw, headers, channel=kind)['accepted'] == 1
        assert store.handle_webhook(raw, headers, channel=kind)['duplicates'] == 1
        agents = AgentStore(tmp_path)
        agent = SimpleNamespace(children={'setup': {'id': 'setup', 'owner_principal_id': 'alice',
            'dataset': 'work', 'messages': []}})
        signals = MessagingSignals(store, agents, agent, member=lambda actor: {'display_name': 'Alice'})
        agents.signal_authority = signals.current
        await signals.tick(); await signals.tick()
        with agents.connect() as db:
            rows = db.execute('SELECT body FROM agent_wakes').fetchall()
            assert len(rows) == 1
        signal = json.loads(rows[0]['body'])
        assert signal['text'] == 'Reply in this chat' and signal['sender']['channel'] == kind
        claim = agents.wakes.claim_chat(signal['id'], owner='alice')
        agent.children['setup']['messages'].append({'role': 'user', 'content': signal['text'],
            'channel': {'admission_key': signal['sender']['admission_key']}})
        agents.wakes.finish_chat(signal['id'], claim['run_id'], owner='alice', status='completed')
        await signals.tick()
        assert not store.pending_replies()
        answered = await store.send_message(actor, 'Recorded', request_id='answer', channel=kind)
        assert answered['status'] == 'sent'
        if kind == 'email':
            assert calls[-1]['reply_to_message_id'] == 'incoming_1'
        with store._db() as db:
            assert db.execute('SELECT SUM(amount) FROM capability_spend_holds WHERE returned=0').fetchone()[0] == 3000
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['email', 'voice'])
def test_uncommissioned_spend_admits_zero_provider_requests(tmp_path, kind):
    async def run():
        store, actor, provider, calls, address, *_ = fixture(tmp_path, kind, allowance=False)
        result = await store.request_code(actor, address, 'verify', channel=kind)
        assert result['status'] == 'cancelled' and calls == []
    asyncio.run(run())


def test_email_sender_or_unknown_parent_cannot_choose_any_chat(tmp_path):
    async def run():
        store, actor, provider, calls, address, *_ = fixture(tmp_path, 'email')
        await enroll(store, actor, 'email', address, calls)
        await store.send_message(actor, 'Private conversation', request_id='message', channel='email')
        for i, context in enumerate((None, 'foreign_parent')):
            raw, headers = reply(provider, address, context, identifier=f'in_{i}')
            assert store.handle_webhook(raw, headers, channel='email')['blocked'] == 1
        assert store.pending_replies() == []
        with store._db() as db:
            assert all(row['text'] is None for row in db.execute('SELECT text FROM inbox'))
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['email', 'voice'])
@pytest.mark.parametrize('change', ['stop', 'expire', 'daily', 'total', 'account', 'authority'])
def test_last_moment_revocation_precedes_provider_request(tmp_path, kind, change):
    async def run():
        store, actor, provider, calls, address, grant, current, now = fixture(tmp_path, kind)
        await enroll(store, actor, kind, address, calls)
        def revoke(actor, row):
            if change == 'stop': grant['stopped'] = True
            elif change == 'expire': grant['expires_at'] = now[0]
            elif change == 'daily': grant['daily_microusd'] = 1000
            elif change == 'total': grant['total_microusd'] = 1000
            elif change == 'account': grant['account_id'] = 'foreign'
            else: current[0] = False
            return True
        store.send_admission = revoke
        result = await store.send_message(actor, 'Please reply', request_id='message', channel=kind)
        assert result['status'] == 'cancelled' and len(calls) == 1
        with store._db() as db:
            assert db.execute('SELECT SUM(amount) FROM capability_spend_holds WHERE returned=0').fetchone()[0] == 1000
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['email', 'voice'])
def test_uncertain_request_keeps_reservation_and_never_repeats_after_restart(tmp_path, kind):
    async def run():
        from messaging import MessagingError
        store, actor, provider, calls, address, grant, current, now = fixture(tmp_path, kind)
        await enroll(store, actor, kind, address, calls)
        provider.transport = httpx.MockTransport(lambda request: (calls.append('uncertain'), httpx.Response(503))[1])
        first = await store.send_message(actor, 'Please reply', request_id='message', channel=kind)
        assert first['status'] == 'uncertain'
        restored = Messaging(tmp_path, company_id='company', adapters=[provider], channel_names=(kind,),
            authorized=lambda actor: True, clock=lambda: now[0], capability_spend=store.capability_spend)
        restored.recover()
        assert (await restored.send_message(actor, 'Please reply', request_id='message', channel=kind))['id'] == first['id']
        with pytest.raises(MessagingError, match='previous message'):
            await restored.send_message(actor, 'Please reply', request_id='new-identity', channel=kind)
        assert len(calls) == 2
        with restored._db() as db:
            assert db.execute('SELECT SUM(amount) FROM capability_spend_holds WHERE returned=0').fetchone()[0] == 2000
    asyncio.run(run())


def test_voice_reserves_maximum_call_duration_inclusive_cost(tmp_path):
    async def run():
        from dataclasses import replace
        store, actor, provider, calls, address, grant, current, now = fixture(tmp_path, 'voice')
        provider.config = replace(provider.config, maximum_seconds=120)
        grant['maximum_request_microusd'] = 1999
        held = await store.request_code(actor, address, 'too-long', channel='voice')
        assert held['status'] == 'cancelled' and not calls
        now[0] += 61
        grant['maximum_request_microusd'] = 2000
        await store.request_code(actor, address, 'bounded', channel='voice')
        assert calls[0]['TimeLimit'] == ['120']
        with store._db() as db:
            assert db.execute('SELECT SUM(amount) FROM capability_spend_holds WHERE returned=0').fetchone()[0] == 2000
    asyncio.run(run())


def test_selected_composition_constructs_only_selected_channels(tmp_path):
    from builtin_capabilities import BuiltInCapabilities
    from messaging_composition import compose_messaging, callback_response
    calls = []
    def disabled(*args):
        calls.append(args)
        raise AssertionError('Disabled adapter constructed')
    selected = BuiltInCapabilities(tmp_path, 'bare', {}, factories={name: disabled for name in ('email', 'voice')})
    service = compose_messaging(selected, authorized=lambda actor: True)
    assert service.channel_names == () and service.channels() == [] and service.adapters == {}
    assert not calls and not (tmp_path / 'messaging.sqlite').exists()
    assert callback_response('voice') == ('<Response><Hangup/></Response>', 'application/xml')


def test_private_allowance_binding_and_file_permissions(tmp_path, monkeypatch):
    store, actor, provider, calls, address, grant, current, now = fixture(tmp_path, 'email')
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    path = private / 'allowance.json'
    value = {'schema_version': 1, 'environment_id': 'fixture', 'company_scope': 'company', 'grants': [grant]}
    def save():
        path.write_text(json.dumps(value)); path.chmod(0o600)
    save(); monkeypatch.setenv('ROCOTO_CAPABILITY_SPEND_CONFIG', str(path))
    budget = CapabilitySpend(tmp_path, 'fixture', 'company')
    assert budget.ready(provider, now[0])
    value['environment_id'] = 'other'; save()
    assert not budget.ready(provider, now[0])
    value['environment_id'] = 'fixture'; save(); path.chmod(0o644)
    assert not budget.ready(provider, now[0])
    path.chmod(0o600); value['company_scope'] = 'foreign'; save()
    assert not budget.ready(provider, now[0])


def test_quota_shares_existing_workspace_send_ledger(tmp_path):
    async def run():
        from background_agents import AgentStore
        from messaging_signals import reserve_send
        store, actor, provider, calls, address, *_ = fixture(tmp_path, 'email')
        agents = AgentStore(tmp_path)
        store.send_admission = lambda actor, row: reserve_send(agents, actor, row)
        await enroll(store, actor, 'email', address, calls)
        await store.send_message(actor, 'Please reply', request_id='message', channel='email')
        await store.send_message(actor, 'Please reply', request_id='message', channel='email')
        assert agents.signals.usage()['send']['used'] == 2
        assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize('reply_id_kind', ['provider', 'rfc822'])
def test_actual_v2_signed_email_envelope_uses_acknowledged_rfc_parent(tmp_path, reply_id_kind):
    async def run():
        store, actor, provider, calls, address, *_ = fixture(tmp_path, 'email')
        await enroll(store, actor, 'email', address, calls)
        def send(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'object': 'EmailSent', 'id': 'provider_2', 'message_id': '<outbound@example.test>'})
        provider.transport = httpx.MockTransport(send)
        await store.send_message(actor, 'Please reply', request_id='message', channel='email')
        from dataclasses import replace
        provider.config = replace(provider.config, email_reply_id=reply_id_kind)
        flat, _ = reply(provider, address, None)
        event = json.loads(flat)
        email = event['payload']; del email['plain_text']; del email['in_reply_to']
        email['message_id'] = '<inbound@example.test>'
        email['body'] = '<html><head><style>hidden</style></head><body><p>Real reply</p><script>ignored</script></body></html>'
        email['headers'] = [{'name': 'In-Reply-To', 'value': '<outbound@example.test>'}]
        event['payload'] = {'folder_id': 'INBOX', 'email': email}
        def signed():
            raw = json.dumps(event).encode(); stamp = str(int(time.time()))
            return raw, {'unipile-signature': 't=' + stamp + ',v0=' + hmac.new(b'b' * 32, stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()}
        raw, headers = signed()
        assert store.handle_webhook(raw, headers, channel='email')['accepted'] == 1
        assert store.pending_replies()[0]['text'] == 'Real reply'
        # Returning to the same conversation uses the identifier required by its commissioned mailbox.
        provider.transport = httpx.MockTransport(lambda request: (calls.append(json.loads(request.content)),
            httpx.Response(200, json={'id': 'provider_3', 'message_id': '<answer@example.test>'}))[1])
        await store.send_message(actor, 'Recorded', request_id='answer', channel='email')
        assert calls[-1]['reply_to_message_id'] == ('<inbound@example.test>' if reply_id_kind == 'rfc822' else 'incoming_1')
        email['id'] = 'forged_parent'; email['headers'][0]['value'] = '<foreign@example.test>'
        raw, headers = signed()
        assert store.handle_webhook(raw, headers, channel='email')['blocked'] == 1
        assert len(store.pending_replies()) == 1
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['email', 'voice'])
@pytest.mark.parametrize('withdraw', [True, False])
def test_credentials_changed_during_admission_keep_request_inside_host(tmp_path, kind, withdraw):
    async def run():
        store, actor, provider, calls, address, *_ = fixture(tmp_path, kind)
        await enroll(store, actor, kind, address, calls)
        def revoke(actor, row):
            provider._credentials = lambda: {} if withdraw else dict.fromkeys(provider.secret_fields, 'rotated-' + 'x' * 32)
            return True
        store.send_admission = revoke
        result = await store.send_message(actor, 'Private message', request_id='message', channel=kind)
        assert result['status'] == 'cancelled' and len(calls) == 1
        with store._db() as db:
            assert db.execute('SELECT SUM(amount) FROM capability_spend_holds WHERE returned=0').fetchone()[0] == 1000
    asyncio.run(run())


def test_midnight_admission_moves_original_hold_before_spending_today(tmp_path):
    async def run():
        store, actor, provider, calls, address, grant, current, now = fixture(tmp_path, 'email')
        await enroll(store, actor, 'email', address, calls)
        now[0] = (int(now[0]) // 86400 + 2) * 86400 - 1
        grant.update(expires_at=now[0] + 3600, daily_microusd=1000)
        original = store._capability_admission
        crossed = False
        def crossing(*args):
            nonlocal crossed
            allowed = original(*args)
            if allowed and not crossed:
                crossed = True
                now[0] += 2
            return allowed
        store._capability_admission = crossing
        first = await store.send_message(actor, 'First today', request_id='first', channel='email')
        second = await store.send_message(actor, 'Second today', request_id='second', channel='email')
        assert first['status'] == 'sent' and second['status'] == 'cancelled'
        assert len(calls) == 2  # enrollment yesterday plus exactly one send today
        today = datetime.fromtimestamp(now[0], timezone.utc).strftime('%Y-%m-%d')
        with store._db() as db:
            assert db.execute('SELECT amount,day FROM capability_spend_holds WHERE outbox=?', (first['id'],)).fetchone()['day'] == today
            assert db.execute('SELECT SUM(amount) FROM capability_spend_holds WHERE day=? AND returned=0', (today,)).fetchone()[0] == 1000
    asyncio.run(run())
