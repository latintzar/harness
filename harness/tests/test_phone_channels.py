"""The channel dimension: one adapter interface, WhatsApp first, local stand-ins for the rest.

LocalChannel stands in for a Viber or SMS adapter so the store's channel rules
run on more than one channel. It reaches no provider and no network.
"""
from retail_agent_inputs import RetailInputProvider
import asyncio
import hashlib
import hmac
import inspect
import json
from pathlib import Path
import sqlite3
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from messaging import Messaging, MessagingActor, MessagingError
from messaging_provider import (CHANNELS, ChannelAdapter, MetaWhatsApp, PreparedMessage, ProviderError,
    SendNotStarted, SendRejected, SendUncertain, e164)
from test_messaging import fixture, incoming, webhook


class LocalChannel(ChannelAdapter):
    def __init__(self, channel, *, reply_rule, sender, company_id='company', account=None, ready=True):
        self.channel, self.reply_rule, self.sender, self.company_id = channel, reply_rule, sender, company_id
        self.account, self.ready, self.sent, self.failure = account or channel + '-account-1', ready, [], None
        self.secret = ('local-secret-' + channel).encode()

    @property
    def account_id(self):
        return self.account

    @property
    def commissioned(self):
        return True

    def readiness(self):
        reason = None if self.ready else 'provider_setup_required'
        return {'provider': self.channel, 'configured': True, 'ready': self.ready, 'reason': reason,
            'otp_ready': self.ready, 'hello_ready': self.ready, 'hello_reason': reason, 'webhook_ready': True,
            'sender': self.sender}

    def otp_hash(self, challenge_id, code):
        return hmac.new(self.secret, (challenge_id + '\0' + code).encode(), hashlib.sha256).hexdigest()

    def address(self, value):
        if self.channel == 'sms':
            return e164(value)
        if not isinstance(value, str) or not 8 <= len(value) <= 64 or not value.isascii():
            raise ProviderError('Open the channel from the contact card.')
        return value

    def prepare(self, kind, address, *, text=None, code=None, alert=None, reference=None):
        if not self.ready:
            raise SendNotStarted('provider_setup_required')
        body = {'otp': code, 'hello': 'Hello from Rocoto.', 'text': text,
            'utility': ' · '.join(alert['parameters']) if alert else None}[kind]
        return PreparedMessage('local://' + self.channel, {'to': address, 'kind': kind, 'body': body, 'reference': reference}, 'local-token')

    def preview(self, kind, *, text=None, alert=None):
        return {'hello': 'Hello from Rocoto.', 'text': text, 'utility': ' · '.join(alert['parameters']) if alert else None}.get(kind)

    def units(self, prepared):
        return -(-len(prepared.payload['body']) // 160) if self.channel == 'sms' else 1

    async def send(self, prepared, current):
        admitted = current()
        if inspect.isawaitable(admitted):
            admitted = await admitted
        if admitted is not True:
            raise SendNotStarted('The messaging action has finished.')
        if self.failure:
            raise self.failure
        self.sent.append(prepared.payload)
        return f'{self.channel}.out-{len(self.sent)}'

    def sign(self, value):
        raw = json.dumps(value).encode()
        return raw, {'x-local-signature': hmac.new(self.secret, raw, hashlib.sha256).hexdigest()}

    def verify(self, raw_body, headers):
        expected = hmac.new(self.secret, raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(headers.get('x-local-signature', ''))):
            raise ProviderError('Verify this messaging event.')

    def events(self, raw_body):
        value = json.loads(raw_body)
        return {'messages': value.get('messages', []), 'receipts': value.get('receipts', []),
            'opt_outs': value.get('opt_outs', []), 'subscriptions': value.get('subscriptions', []), 'ignored': 0}


def channels_fixture(tmp_path, *, viber=True):
    store, actor, whatsapp, sent, now = fixture(tmp_path)
    sms = LocalChannel('sms', reply_rule='recent', sender='+35929990000')
    bot = LocalChannel('viber', reply_rule='tracking', sender='Rocoto')
    store = Messaging(tmp_path, company_id='company', adapters=[whatsapp, sms] + ([bot] if viber else []),
        authorized=lambda actor: True, clock=lambda: now[0])
    return store, whatsapp, sms, bot, sent, now


def member(name, chat=None):
    return MessagingActor('company', name, 'shop', chat or 'setup-' + name, name)


async def verify(store, actor, address, channel, read, *, request_id=None):
    challenge = await store.request_code(actor, address, request_id=request_id or f'otp-{actor.member_id}-{channel}', channel=channel)
    return store.verify_code(actor, challenge['id'], read())


def whatsapp_code(sent):
    return lambda: sent[-1]['template']['components'][0]['parameters'][0]['text']


def local_code(adapter):
    return lambda: adapter.sent[-1]['body']


def message(adapter, now, *, id, address, text='On my way', reply_to=None):
    return adapter.sign({'messages': [{'id': id, 'address': address, 'text': text, 'sent_at': now[0], 'reply_to': reply_to,
        'payload_sha256': hashlib.sha256((id + text).encode()).hexdigest()}]})


def rows(store, query, values=()):
    with sqlite3.connect(store.path) as db:
        return db.execute(query, values).fetchall()


def test_whatsapp_is_the_first_adapter_behind_the_interface(tmp_path):
    store, actor, provider, sent, now = fixture(tmp_path)
    assert isinstance(provider, ChannelAdapter) and isinstance(provider, MetaWhatsApp)
    assert (provider.channel, provider.account_id, provider.company_id) == ('whatsapp', '12345:67890', 'company')
    readiness = provider.readiness()
    assert readiness['provider'] == 'whatsapp' and readiness['ready'] is True and readiness['sender'] == '+34600000000'
    prepared = provider.prepare('text', '+34611111111', text='Queue is six people.')
    assert isinstance(prepared, PreparedMessage) and provider.units(prepared) == 1
    assert prepared.payload['to'] == '34611111111' and prepared.payload['text']['body'] == 'Queue is six people.'
    assert provider.preview('text', text='Queue is six people.') == 'Queue is six people.'
    # The signature is checked on the raw body ahead of any parsing.
    with pytest.raises(ProviderError, match='Verify this messaging event'):
        provider.verify(b'not json', {'x-hub-signature-256': 'sha256=' + '0' * 64})
    raw, signature = webhook(provider, [incoming(now, context='wamid.out-9')],
        [{'id': 'wamid.out-9', 'status': 'delivered', 'timestamp': str(int(now[0])), 'recipient_id': '34611111111'}])
    provider.verify(raw, {'X-Hub-Signature-256': signature})
    events = provider.events(raw)
    assert set(events) == {'messages', 'receipts', 'opt_outs', 'subscriptions', 'ignored'}
    assert events['messages'] == [{'id': 'wamid.in-1', 'address': '+34611111111', 'text': 'Hello back', 'sent_at': now[0],
        'reply_to': 'wamid.out-9', 'payload_sha256': events['messages'][0]['payload_sha256']}]
    assert events['receipts'] == [{'provider_message_id': 'wamid.out-9', 'status': 'delivered', 'at': now[0], 'address': '+34611111111'}]
    assert events['opt_outs'] == [] and events['subscriptions'] == [] and events['ignored'] == 0
    with pytest.raises(ProviderError, match='commissioned messaging account'):
        provider.events(b'not json')
    assert CHANNELS == ('viber', 'sms', 'whatsapp')


def test_the_gate_policy_names_the_same_channels():
    from types import SimpleNamespace
    from agent_kind_policy import messaging_channels
    assert messaging_channels(SimpleNamespace(messaging_store=SimpleNamespace(channel_names=CHANNELS))) == CHANNELS


def test_the_store_takes_one_adapter_per_channel_for_its_own_company(tmp_path):
    store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
    assert store.provider is whatsapp and store.adapter('sms') is sms and store.adapter('viber') is bot
    assert store.channels() == [
        {'channel': 'viber', 'label': 'Viber', 'ready': True, 'reason': None, 'sender': 'Rocoto'},
        {'channel': 'sms', 'label': 'SMS', 'ready': True, 'reason': None, 'sender': '+35929990000'},
        {'channel': 'whatsapp', 'label': 'WhatsApp', 'ready': True, 'reason': None, 'sender': '+34600000000'}]
    only = Messaging(tmp_path / 'other', company_id='company', provider=whatsapp, authorized=lambda actor: True)
    assert [(row['channel'], row['ready'], row['reason'], row['sender']) for row in only.channels()] == [
        ('viber', False, 'provider_setup_required', None), ('sms', False, 'provider_setup_required', None),
        ('whatsapp', True, None, '+34600000000')]
    with pytest.raises(MessagingError):
        Messaging(tmp_path, company_id='company', adapters=[whatsapp, LocalChannel('sms', reply_rule='recent', sender='+1', company_id='another')])
    with pytest.raises(MessagingError):
        Messaging(tmp_path, company_id='company', adapters=[sms, LocalChannel('viber', reply_rule='tracking', sender='Rocoto', account=sms.account_id)])
    with pytest.raises(MessagingError):
        Messaging(tmp_path, company_id='company', adapters=[sms, LocalChannel('sms', reply_rule='recent', sender='+2', account='second')])
    with pytest.raises(MessagingError):
        Messaging(tmp_path, company_id='company', adapters=[LocalChannel('telegram', reply_rule='recent', sender='+3')])
    with pytest.raises(MessagingError, match='provider_setup_required'):
        only.adapter('sms')


def test_each_address_is_verified_on_its_own_channel(tmp_path):
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
        alice, bob = member('alice'), member('bob')
        assert (await verify(store, alice, '+34611111111', 'sms', local_code(sms)))['channel'] == 'sms'
        # The security code travelled over the channel it proves.
        assert [item['kind'] for item in sms.sent] == ['otp'] and sent == [] and bot.sent == []
        now[0] += 61
        assert (await verify(store, alice, '+34611111111', 'whatsapp', whatsapp_code(sent)))['channel'] == 'whatsapp'
        now[0] += 61
        await verify(store, alice, 'viber-user-alice-0001', 'viber', local_code(bot))
        assert rows(store, 'SELECT channel,address,account,generation FROM contacts ORDER BY channel') == [
            ('sms', '+34611111111', 'sms-account-1', 1), ('viber', 'viber-user-alice-0001', 'viber-account-1', 1),
            ('whatsapp', '+34611111111', '12345:67890', 1)]
        described = store.describe(alice)
        assert [item['channel'] for item in described['addresses']] == ['viber', 'sms', 'whatsapp']
        assert described['contact']['channel'] == 'viber' and described['contact']['phone'] == 'viber-user-alice-0001'
        assert described['channels'] == store.channels()
        # A number stays with the member who verified it, on every channel.
        now[0] += 61
        challenge = await store.request_code(bob, '+34611111111', channel='whatsapp')
        with pytest.raises(MessagingError, match='Use a number available for this member'):
            store.verify_code(bob, challenge['id'], whatsapp_code(sent)())
        with pytest.raises(MessagingError):
            await store.request_code(bob, 'not-a-number', channel='sms')
        with pytest.raises(MessagingError, match='provider_setup_required'):
            await Messaging(tmp_path / 'other', company_id='company', provider=whatsapp, authorized=lambda actor: True).request_code(
                bob, '+34622222222', channel='sms')
        # Verifying another address on one channel raises that channel's generation alone.
        # Security codes keep their built limit of three per member and hour across channels.
        with pytest.raises(MessagingError, match='Wait before requesting another security code'):
            await store.request_code(alice, '+34655555555', request_id='otp-fourth', channel='sms')
        now[0] += 3600
        assert (await verify(store, alice, '+34655555555', 'sms', local_code(sms), request_id='otp-second'))['generation'] == 2
        assert rows(store, 'SELECT channel,generation FROM contact_generations ORDER BY channel') == [('sms', 2), ('viber', 1), ('whatsapp', 1)]
        assert rows(store, "SELECT channel,chat FROM bindings ORDER BY channel") == [('sms', 'setup-alice'), ('viber', 'setup-alice'), ('whatsapp', 'setup-alice')]
    asyncio.run(scenario())


def test_channel_order_is_viber_then_sms_then_whatsapp_among_commissioned_channels(tmp_path):
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
        alice = member('alice')
        await verify(store, alice, '+34611111111', 'whatsapp', whatsapp_code(sent))
        with store._db() as db:
            assert store.eligible(db, 'alice') == [('whatsapp', 1)]
        now[0] += 61
        await verify(store, alice, '+34611111111', 'sms', local_code(sms))
        now[0] += 61
        await verify(store, alice, 'viber-user-alice-0001', 'viber', local_code(bot))
        with store._db() as db:
            assert [channel for channel, _ in store.eligible(db, 'alice')] == ['viber', 'sms', 'whatsapp']
            bot.ready = False
            assert [channel for channel, _ in store.eligible(db, 'alice')] == ['sms', 'whatsapp']
            assert store.eligible(db, 'bob') == []
    asyncio.run(scenario())


def test_whatsapp_only_rules_stay_with_whatsapp(tmp_path):
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
        alice = member('alice', 'agent-chat')
        await verify(store, member('alice'), '+34611111111', 'whatsapp', whatsapp_code(sent))
        now[0] += 61
        await verify(store, member('alice'), '+34611111111', 'sms', local_code(sms))
        for channel in ('whatsapp', 'sms'):
            store.bind_conversation(alice, channel=channel)
        # WhatsApp free text needs the customer-service window; SMS carries the same text now.
        with pytest.raises(MessagingError, match='approved template'):
            await store.send_message(alice, 'Queue is six people.', request_id='text-whatsapp', channel='whatsapp')
        receipt = await store.send_message(alice, 'Queue is six people.', request_id='text-sms', channel='sms')
        assert receipt['status'] == 'sent' and receipt['channel'] == 'sms' and receipt['provider_message_id'] == 'sms.out-2'
        assert sms.sent[-1] == {'to': '+34611111111', 'kind': 'text', 'body': 'Queue is six people.', 'reference': receipt['id']}
        assert rows(store, "SELECT channel,account,address FROM outbox WHERE request_id='text-sms'") == [('sms', 'sms-account-1', '+34611111111')]
        assert [item['channel'] for item in store.describe(alice)['outbox'][:1]] == ['sms']
        # An alert on a channel with its own wording needs no WhatsApp template.
        now[0] += 5
        alert = {'purpose': 'monitoring', 'language': 'en_US', 'parameters': ['Checkout', 'Six people waiting', '09:00']}
        with pytest.raises(ValueError):
            await store.send_alert(alice, alert, request_id='alert-whatsapp', channel='whatsapp')
        sent_alert = await store.send_alert(alice, alert, request_id='alert-sms', channel='sms')
        assert sent_alert['status'] == 'sent' and sent_alert['text'] == 'Checkout · Six people waiting · 09:00'
    asyncio.run(scenario())


def test_an_sms_reply_continues_the_most_recent_delivery_inside_24_hours(tmp_path):
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
        await verify(store, member('alice'), '+34611111111', 'sms', local_code(sms))
        for chat, text in (('tracker-one', 'Queue is six people.'), ('tracker-two', 'Shelf three is empty.')):
            actor = member('alice', chat)
            store.bind_conversation(actor, channel='sms')
            now[0] += 10
            assert (await store.send_message(actor, text, request_id='text-' + chat, channel='sms'))['status'] == 'sent'
        raw, headers = message(sms, now, id='sms.in-1', address='+34611111111')
        assert store.handle_webhook(raw, headers, channel='sms')['accepted'] == 1
        reply = store.pending_replies()[0]
        assert (reply['chat_id'], reply['member_id'], reply['channel'], reply['provider']) == ('tracker-two', 'alice', 'sms', 'sms')
        digest = hashlib.sha256(json.dumps(['company', 'sms-account-1', 'sms.in-1'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert reply['admission_key'] == 'sms:' + digest
        assert rows(store, "SELECT channel,account,address,chat FROM inbox WHERE provider_event_id='sms.in-1'") == [
            ('sms', 'sms-account-1', '+34611111111', 'tracker-two')]
        assert store.handle_webhook(raw, headers, channel='sms')['duplicates'] == 1
        # With the last delivery older than 24 hours the reply continues the member's setup chat.
        now[0] += 86400
        raw, headers = message(sms, now, id='sms.in-2', address='+34611111111')
        store.handle_webhook(raw, headers, channel='sms')
        assert next(row for row in store.pending_replies() if row['provider_event_id'] == 'sms.in-2')['chat_id'] == 'setup-alice'
        # A signature from another channel's account proves nothing here.
        raw, headers = message(bot, now, id='sms.in-3', address='+34611111111')
        with pytest.raises(MessagingError):
            store.handle_webhook(raw, headers, channel='sms')
        assert rows(store, "SELECT count(*) FROM inbox WHERE provider_event_id='sms.in-3'") == [(0,)]
    asyncio.run(scenario())


def test_a_viber_reply_continues_the_conversation_its_tracking_value_names(tmp_path):
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
        await verify(store, member('alice'), 'viber-user-alice-0001', 'viber', local_code(bot))
        await verify(store, member('bob'), 'viber-user-bob-00002', 'viber', local_code(bot))
        outgoing = {}
        for name in ('alice', 'bob'):
            actor = member(name, 'tracker-' + name)
            store.bind_conversation(actor, channel='viber')
            outgoing[name] = await store.send_message(actor, 'Queue is six people.', request_id='text-' + name, channel='viber')
            assert bot.sent[-1]['reference'] == outgoing[name]['id']
        raw, headers = message(bot, now, id='viber.in-1', address='viber-user-alice-0001', reply_to=outgoing['alice']['id'])
        store.handle_webhook(raw, headers, channel='viber')
        # The returned value is a lookup key among that sender's own messages, never a chat identity.
        raw, headers = message(bot, now, id='viber.in-2', address='viber-user-alice-0001', reply_to=outgoing['bob']['id'])
        store.handle_webhook(raw, headers, channel='viber')
        raw, headers = message(bot, now, id='viber.in-3', address='viber-user-alice-0001', reply_to='tracker-bob')
        store.handle_webhook(raw, headers, channel='viber')
        routed = {row['provider_event_id']: (row['chat_id'], row['channel']) for row in store.pending_replies()}
        assert routed == {'viber.in-1': ('tracker-alice', 'viber'), 'viber.in-2': ('setup-alice', 'viber'), 'viber.in-3': ('setup-alice', 'viber')}
        assert all(row['admission_key'].startswith('viber:') for row in store.pending_replies())
    asyncio.run(scenario())


def test_a_reply_with_no_reply_context_continues_the_most_recent_delivery_on_every_channel(tmp_path):
    """Decision of 10 October 2026: with no provider reply context, on any channel including
    WhatsApp, a reply continues the chat of the most recent delivery to that verified address
    inside 24 hours. An exact reply context still wins, and with no such delivery the reply
    continues the member's setup chat."""
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
        await verify(store, member('alice'), '+34611111111', 'whatsapp', whatsapp_code(sent))
        now[0] += 61
        await verify(store, member('alice'), 'viber-user-alice-0001', 'viber', local_code(bot))
        def routed(identifier):
            return next(row for row in store.pending_replies() if row['provider_event_id'] == identifier)['chat_id']
        def whatsapp_reply(identifier, context=None):
            raw, signature = webhook(whatsapp, [incoming(now, id=identifier, context=context)])
            assert store.handle_webhook(raw, signature)['accepted'] == 1
            return routed(identifier)
        # Nothing was delivered to this number yet: the member's setup chat.
        assert whatsapp_reply('wamid.in-1') == 'setup-alice'
        receipts = {}
        for chat in ('tracker-one', 'tracker-two'):
            actor = member('alice', chat)
            store.bind_conversation(actor, channel='whatsapp')
            now[0] += 10
            receipts[chat] = await store.send_message(actor, 'Queue is six people.', request_id='text-' + chat, channel='whatsapp')
            assert receipts[chat]['status'] == 'sent'
        # A plain WhatsApp message carries no context: the most recent delivery to that number.
        assert whatsapp_reply('wamid.in-2') == 'tracker-two'
        # The exact reply context still wins over a more recent delivery.
        assert whatsapp_reply('wamid.in-3', receipts['tracker-one']['provider_message_id']) == 'tracker-one'
        # A context that names none of this member's messages chooses no agent conversation.
        assert whatsapp_reply('wamid.in-4', 'wamid.unknown') == 'setup-alice'
        # The same rule on Viber when the subscriber's message returns no tracking value.
        actor = member('alice', 'tracker-three')
        store.bind_conversation(actor, channel='viber')
        now[0] += 10
        assert (await store.send_message(actor, 'Shelf three is empty.', request_id='text-viber', channel='viber'))['status'] == 'sent'
        raw, headers = message(bot, now, id='viber.in-1', address='viber-user-alice-0001')
        assert store.handle_webhook(raw, headers, channel='viber')['accepted'] == 1
        assert routed('viber.in-1') == 'tracker-three'
        # Each address has its own most recent delivery: WhatsApp still continues its own chat.
        assert whatsapp_reply('wamid.in-5') == 'tracker-two'
        # Past 24 hours a reply with no context continues the member's setup chat.
        now[0] += 86400
        assert whatsapp_reply('wamid.in-6') == 'setup-alice'
        raw, headers = message(bot, now, id='viber.in-2', address='viber-user-alice-0001')
        store.handle_webhook(raw, headers, channel='viber')
        assert routed('viber.in-2') == 'setup-alice'
    asyncio.run(scenario())


def test_receipts_and_opt_outs_stay_apart_from_replies_on_every_channel(tmp_path):
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
        alice = member('alice')
        await verify(store, alice, '+34611111111', 'whatsapp', whatsapp_code(sent))
        now[0] += 61
        await verify(store, alice, '+34611111111', 'sms', local_code(sms))
        hello = await store.send_hello(alice, request_id='hello-sms', channel='sms')
        raw, headers = sms.sign({'receipts': [{'provider_message_id': hello['provider_message_id'], 'status': 'delivered', 'at': now[0], 'address': '+34611111111'}]})
        assert store.handle_webhook(raw, headers, channel='sms')['delivery_updates'] == 1
        assert store.handle_webhook(raw, headers, channel='sms')['delivery_updates'] == 0
        assert rows(store, 'SELECT channel,status,address FROM delivery_events') == [('sms', 'delivered', '+34611111111')]
        assert store.describe(alice)['outbox'][0]['status'] == 'delivered' and store.pending_replies() == []
        # A stop keyword sets the opt-out ahead of routing and never becomes a chat message.
        raw, headers = sms.sign({'opt_outs': [{'address': '+34611111111', 'at': now[0], 'opted_out': True}]})
        result = store.handle_webhook(raw, headers, channel='sms')
        assert result['opt_outs'] == 1 and result['accepted'] == 0 and store.pending_replies() == []
        assert rows(store, "SELECT opted_out FROM contacts WHERE channel='sms'") == [(now[0],)]
        with store._db() as db:
            assert store.eligible(db, 'alice') == [('whatsapp', 1)]
            assert store.member_channels(db, 'alice')['sms']['opted_out_at'] == now[0]
        with pytest.raises(MessagingError, match='stopped messages on this channel'):
            await store.send_message(alice, 'Queue is six people.', request_id='after-stop', channel='sms')
        raw, headers = sms.sign({'opt_outs': [{'address': '+34611111111', 'at': now[0] + 5, 'opted_out': False}]})
        store.handle_webhook(raw, headers, channel='sms')
        with store._db() as db:
            assert [channel for channel, _ in store.eligible(db, 'alice')] == ['sms', 'whatsapp']
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,status', [(SendUncertain('lost'), 'uncertain'), (SendRejected('refused'), 'failed'), (None, 'cancelled')])
def test_lost_rejected_and_stopped_sends_keep_their_meaning_on_another_channel(tmp_path, failure, status):
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path)
        alice = member('alice')
        await verify(store, alice, '+34611111111', 'sms', local_code(sms))
        sms.failure = failure
        if failure is None:
            stop = asyncio.Event()
            original = sms.send
            async def stopped(prepared, current):
                stop.set()
                return await original(prepared, current)
            sms.send = stopped
            with pytest.raises(asyncio.CancelledError):
                await store.send_hello(alice, request_id='hello', channel='sms', cancelled=stop)
        else:
            assert (await store.send_hello(alice, request_id='hello', channel='sms'))['status'] == status
        assert rows(store, "SELECT status,channel FROM outbox WHERE kind='hello'") == [(status, 'sms')]
        assert [item['kind'] for item in sms.sent] == ['otp']
        if status == 'uncertain':
            with pytest.raises(MessagingError, match='Confirm the previous message delivery'):
                await store.send_hello(alice, request_id='again', channel='sms')
    asyncio.run(scenario())


def test_bindings_follow_channel_order_and_the_participants_pick(tmp_path):
    from agent_test_support import retail_agent_store as AgentStore
    from phone_chat import PhoneChat, PhoneChatError
    async def scenario():
        store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path / 'messaging')
        agents = AgentStore(tmp_path / 'agents', input_provider=RetailInputProvider())
        saved = agents.create({'title': 'Queue watch', 'instruction': 'Report queue events.', 'dataset': 'shop',
            'agent_kind': 'tracker', 'input_kind': 'tracker_events'}, ['shop'], owner_principal_id='alice', conversation_chat_id='tracker-chat')
        phone = PhoneChat(store, agents=agents, member=lambda p: {'id': p, 'display_name': p.title(), 'role': 'operator'} if p == 'alice' else None)
        binding = lambda: phone.state(saved['id'])['participants'][0]['binding']
        alice = member('alice')
        await verify(store, alice, '+34611111111', 'whatsapp', whatsapp_code(sent))
        assert (binding()['channel'], binding()['generation']) == ('whatsapp', 1)
        now[0] += 61
        await verify(store, alice, '+34611111111', 'sms', local_code(sms))
        assert (binding()['channel'], binding()['channel_choice'], binding()['generation']) == ('sms', 'automatic', 2)
        now[0] += 61
        await verify(store, alice, 'viber-user-alice-0001', 'viber', local_code(bot))
        assert (binding()['channel'], binding()['generation']) == ('viber', 3)
        assert phone.state(saved['id'])['participants'][0]['verified_channels'] == ['viber', 'sms', 'whatsapp']
        # A participant who picks a channel gets that channel alone.
        state = phone.update_binding(saved['id'], 'alice', by='alice', channel='whatsapp')
        assert (state['participants'][0]['binding']['channel'], state['participants'][0]['binding']['channel_choice']) == ('whatsapp', 'picked')
        assert phone.recipient(saved['id'], 'tracker-chat', 'alice')['channel'] == 'whatsapp'
        # An answer travels on the channel the participant wrote on.
        assert phone.recipient(saved['id'], 'tracker-chat', 'alice', channel='sms')['channel'] == 'sms'
        bot.ready = False
        with pytest.raises(PhoneChatError, match='This channel is being prepared'):
            phone.update_binding(saved['id'], 'alice', by='alice', channel='viber')
        state = phone.update_binding(saved['id'], 'alice', by='alice', channel='automatic')
        assert state['participants'][0]['binding']['channel'] == 'sms'
        # An opt-out on the channel in use moves an automatic binding to the next one.
        raw, headers = sms.sign({'opt_outs': [{'address': '+34611111111', 'at': now[0], 'opted_out': True}]})
        store.handle_webhook(raw, headers, channel='sms')
        current = phone.state(saved['id'])['participants'][0]
        assert current['binding']['channel'] == 'whatsapp' and current['opted_out_channels'] == ['sms']
        with pytest.raises(PhoneChatError, match='Verify a number on this channel'):
            phone.recipient(saved['id'], 'tracker-chat', 'alice', channel='sms')
    asyncio.run(scenario())
