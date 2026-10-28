"""Host alert delivery of a Tracking Agent: fan-out, stable IDs, admission and zero-send cases.

Every transport is a local stand-in. No provider is reached and nothing here costs money.
"""
from retail_agent_inputs import RetailInputProvider
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from messaging import Messaging
from messaging_provider import SendRejected, Template
from phone_chat import PhoneChat
from phone_delivery import PhoneDelivery, alert_lane, alert_link, alert_text, delivery_id, mask
from test_messaging import incoming, webhook
from test_phone_participants import NUMBERS, phone_fixture, verify

ORIGIN = 'https://rocoto.example'
SUMMARY = 'Six people are waiting at checkout two.'
TEXT = 'Queue watch: ' + SUMMARY
TEMPLATE = Template('rocoto_monitoring_alert_v1', 'en_US', 'UTILITY', 'APPROVED', 'Alert from {{1}}: {{2}} Open {{3}}')


def link(alert='event-1'):
    return f'{ORIGIN}/?chat=tracker-chat&alert={alert}'


def delivery_fixture(tmp_path, *, verified=('alice',), template=True, origin=ORIGIN, kind='tracker'):
    f = phone_fixture(tmp_path, kind=kind)
    if template:
        f.provider.config = replace(f.provider.config, utility_templates=(TEMPLATE,))
    f.spend = {'company_daily_messages': 50, 'agent_daily_messages': 20, 'stopped': False}
    f.store.spend = lambda: dict(f.spend)
    f.chat, f.busy = [], [False]
    def post(chat_id, entry):
        entry = json.loads(json.dumps(entry))
        for position, (chat, item) in enumerate(f.chat):
            if chat == chat_id and item['alert'] == entry['alert']:
                f.chat[position] = (chat_id, entry)
                return True
        if f.busy[0]:
            return False  # an alert joins the conversation once its active turn has drained
        f.chat.append((chat_id, entry))
        return True
    f.delivery = PhoneDelivery(f.store, f.phone, origin=origin, post=post)
    for member in verified:
        asyncio.run(verify(f.store, f.sent, member))
    if kind == 'tracker':
        f.phone.promote(f.agent)
    f.wire = lambda: [item for item in f.sent if item.get('type') == 'text' or item['template']['name'] == TEMPLATE.name]
    return f


def writes(f, member):
    """The member wrote on WhatsApp, which opens the customer-service window for free text."""
    raw, signature = webhook(f.provider, [incoming(f.now, id='wamid.in-' + member + str(f.now[0]), sender=NUMBERS[member][1:])])
    assert f.store.handle_webhook(raw, signature)['accepted'] == 1


def work(f, ticks=1):
    async def run():
        for _ in range(ticks):
            await f.delivery.tick()
            await f.delivery.drain()
    asyncio.run(run())


def alert(f, key='event-1', **options):
    return f.delivery.fan_out(f.key, alert_id=key, summary=SUMMARY, **options)


def states(f, key=None):
    return {row['member_id']: (row['status'], row['held_reason']) for row in f.delivery.deliveries(f.key, alert_id=key)}


def used(f):
    with sqlite3.connect(f.store.path) as db:
        return dict(db.execute('SELECT scope,units FROM phone_spend'))


def test_the_delivery_id_is_the_contract_digest():
    compact = json.dumps(['company', 'agent-1', 'alice', 'event-1', 1], separators=(',', ':'))
    assert compact == '["company","agent-1","alice","event-1",1]'
    expected = 'dlv-' + hashlib.sha256(compact.encode()).hexdigest()[:32]
    assert delivery_id('company', 'agent-1', 'alice', 'event-1', 1) == expected and len(expected) == 36
    others = {delivery_id('company', 'agent-1', member, alert, revision)
        for member, alert, revision in (('bob', 'event-1', 1), ('alice', 'event-2', 1), ('alice', 'event-1', 2))}
    assert len(others | {expected}) == 4


def test_alert_text_link_and_masking():
    assert alert_text('Queue watch', SUMMARY) == TEXT
    assert alert_text('Queue watch', 'Line one.\nLine\ttwo.') == 'Queue watch: Line one. Line two.'
    long = alert_text('Queue watch', 'x' * 2000)
    assert len(long) == 600 and long.endswith('...')
    assert alert_text('Queue watch', 'Four people are waiting.', 2) == 'Update to an earlier alert from Queue watch: Four people are waiting.'
    assert alert_link(ORIGIN, 'chat-91ab', 'event 1/2') == 'https://rocoto.example/?chat=chat-91ab&alert=event%201%2F2'
    assert alert_link('', 'chat', 'event') == '' and alert_link('http://rocoto.example', 'chat', 'event') == ''
    assert mask('+359888123456') == '+359•••••••56' and mask('viber-user-7f3a91c2') == '••••91c2' and mask(None) is None


def test_one_alert_fans_out_to_each_enabled_participant_under_one_stable_id(tmp_path):
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    for member in ('bob', 'carol'):
        f.phone.add_participant(f.key, member, by='alice')
    f.roles['dave'] = 'operator'
    f.phone.add_participant(f.key, 'dave', by='alice')
    f.phone.update_binding(f.key, 'dave', by='dave', enabled=False)
    sent = len(f.sent)
    written = alert(f)
    assert [row['member_id'] for row in written] == ['alice', 'bob', 'carol']  # dave switched his alerts off
    state = f.phone.state(f.key)
    for row in written:
        binding = next(item['binding'] for item in state['participants'] if item['member_id'] == row['member_id'])
        assert row == {'id': delivery_id('company', f.key, row['member_id'], 'event-1', 1), 'agent_id': f.key,
            'chat_id': 'tracker-chat', 'member_id': row['member_id'], 'alert_id': 'event-1', 'alert_revision': 1,
            'channel': binding['channel'], 'account': None, 'text': TEXT, 'link': link(), 'status': 'queued',
            'held_reason': None, 'binding_generation': binding['generation'],
            'address_generation': binding['address_generation'], 'policy_generation': state['policy']['generation'],
            'provider_message_id': None, 'units': 0, 'attempts': 0, 'created_at': f.now[0], 'updated_at': f.now[0],
            'expires_at': f.now[0] + 900}
    # Fan-out is host bookkeeping: zero provider calls until the worker claims.
    assert len(f.sent) == sent and len(written[0]['text']) <= 600
    # A retry, a replayed event and a restart read the same alert revision: nothing is added.
    assert alert(f) == []
    again = PhoneDelivery(f.store, PhoneChat(f.store, agents=f.agents, member=f.phone.member), origin=ORIGIN)
    assert again.fan_out(f.key, alert_id='event-1', summary=SUMMARY) == []
    assert [row['id'] for row in f.delivery.deliveries(f.key)] == [row['id'] for row in written]


def test_the_worker_sends_text_and_one_link_and_never_an_image(tmp_path):
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    f.phone.add_participant(f.key, 'bob', by='alice')
    f.phone.add_participant(f.key, 'carol', by='alice')
    writes(f, 'alice')
    alert(f)
    sent = len(f.sent)
    work(f)
    wire = {item['to']: item for item in f.sent[sent:]}  # the two sends leave side by side
    # Inside the customer-service window: free text with the link, link preview off.
    assert wire['34611111111'] == {'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': '34611111111',
        'type': 'text', 'text': {'preview_url': False, 'body': TEXT + ' ' + link()}}
    # Outside it: the approved utility template with the alert's three factual details.
    assert wire['34622222222']['type'] == 'template'
    assert wire['34622222222']['template'] == {'name': 'rocoto_monitoring_alert_v1', 'language': {'code': 'en_US'},
        'components': [{'type': 'body', 'parameters': [{'type': 'text', 'text': 'Queue watch'},
            {'type': 'text', 'text': SUMMARY}, {'type': 'text', 'text': link()}]}]}
    assert len(f.sent) == sent + 2 and not any(key in json.dumps(wire) for key in ('image', 'video', 'document', 'thumbnail', 'header'))
    rows = {row['member_id']: row for row in f.delivery.deliveries(f.key)}
    for member in ('alice', 'bob'):
        assert (rows[member]['status'], rows[member]['channel'], rows[member]['account']) == ('sent', 'whatsapp', '12345:67890')
        assert rows[member]['provider_message_id'].startswith('wamid.out-') and rows[member]['units'] == 1
        assert rows[member]['attempts'] == 1 and rows[member]['id'] == delivery_id('company', f.key, member, 'event-1', 1)
    # A participant with no verified number keeps the alert in Rocoto.
    assert (rows['carol']['status'], rows['carol']['held_reason'], rows['carol']['channel']) == ('held', 'number_required', None)
    # Each attempt is one outbox row under the delivery's own request identity.
    with sqlite3.connect(f.store.path) as db:
        assert sorted(db.execute("SELECT member,kind,request_id,chat FROM outbox WHERE kind!='otp'").fetchall()) == [
            ('alice', 'text', rows['alice']['id'] + ':1', 'tracker-chat'), ('bob', 'utility', rows['bob']['id'] + ':1', 'tracker-chat')]
    # An alert suspends no turn, so the member's messaging profile shows nothing waiting for a reply.
    from messaging import MessagingActor
    assert f.store.describe(MessagingActor('company', 'alice', 'shop', 'setup-alice', 'alice'))['waiting'] is False
    # Further passes and a replayed event send nothing more.
    alert(f)
    work(f, ticks=3)
    assert len(f.sent) == sent + 2 and used(f) == {'company': 2, 'agent:' + f.key: 2}


def test_the_alert_joins_the_chat_once_and_carries_its_delivery_states(tmp_path):
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    f.phone.add_participant(f.key, 'bob', by='alice')
    f.busy[0] = True
    first = alert(f)
    second = alert(f, 'event-2')
    work(f)
    # The send left while the conversation's turn was active; the entry waits for the turn to drain.
    assert f.chat == [] and states(f, 'event-1') == {'alice': ('sent', None), 'bob': ('sent', None)}
    f.busy[0] = False
    work(f)
    assert [(chat, entry['alert']) for chat, entry in f.chat] == [
        ('tracker-chat', {'id': 'event-1', 'revision': 1}), ('tracker-chat', {'id': 'event-2', 'revision': 1})]
    entry = f.chat[0][1]
    assert set(entry) == {'role', 'content', 'alert', 'channel_deliveries', 'created_at'}
    assert (entry['role'], entry['content'], entry['created_at']) == ('assistant', TEXT, f.now[0])
    assert entry['channel_deliveries'] == [{'id': row['id'], 'member_id': row['member_id'], 'channel': 'whatsapp',
        'status': 'sent', 'held_reason': None, 'updated_at': f.now[0]} for row in first]
    assert '+34' not in json.dumps(f.chat) and [row['id'] for row in second] == [item['id'] for item in f.chat[1][1]['channel_deliveries']]
    # A provider receipt moves the same entry forward; it stays one entry per alert revision.
    sent = {row['member_id']: row for row in f.delivery.deliveries(f.key, alert_id='event-1')}
    f.now[0] += 5
    raw, signature = webhook(f.provider, statuses=[{'id': sent['alice']['provider_message_id'], 'status': 'delivered',
        'timestamp': str(int(f.now[0])), 'recipient_id': '34611111111'}])
    assert f.store.handle_webhook(raw, signature)['delivery_updates'] == 1
    work(f)
    assert len(f.chat) == 2 and [(item['member_id'], item['status']) for item in f.chat[0][1]['channel_deliveries']] == [
        ('alice', 'delivered'), ('bob', 'sent')]
    assert f.delivery.deliveries(f.key, alert_id='event-1')[0]['updated_at'] == f.now[0]


def test_a_plain_reply_to_a_host_alert_continues_its_tracking_chat_with_that_alert_as_context(tmp_path):
    """A reply typed with no provider reply context answers the most recent delivery to that number inside 24 hours."""
    f = delivery_fixture(tmp_path)
    written = alert(f)
    work(f)
    assert states(f) == {'alice': ('sent', None)}
    def reply(identifier):
        raw, signature = webhook(f.provider, [incoming(f.now, id=identifier, sender='34611111111', text='Open a second till.')])
        assert f.store.handle_webhook(raw, signature)['accepted'] == 1
        return next(row for row in f.store.pending_replies() if row['provider_event_id'] == identifier)
    f.now[0] += 30
    plain = reply('wamid.plain-1')
    assert (plain['chat_id'], plain['member_id'], plain['channel'], plain['reply_context']) == ('tracker-chat', 'alice', 'whatsapp', None)
    # The turn it starts receives the alert it answers as host-supplied context.
    assert f.delivery.answered(plain) == {'alert_id': 'event-1', 'alert_revision': 1, 'text': written[0]['text'], 'link': link()}
    # Past 24 hours a plain reply continues the member's setup chat, with no alert as context.
    f.now[0] += 86400
    late = reply('wamid.plain-2')
    assert late['chat_id'] == 'setup-alice' and f.delivery.answered(late) is None


def test_a_busy_conversation_holds_only_its_own_alert_entries(tmp_path):
    f = delivery_fixture(tmp_path)
    other = f.agents.create({'title': 'Door watch', 'instruction': 'Report door events.', 'dataset': 'shop',
        'agent_kind': 'tracker', 'input_kind': 'tracker_events'}, ['shop'], owner_principal_id='alice',
        conversation_chat_id='door-chat')
    f.phone.promote(other)
    posted = []
    def post(chat_id, entry):
        if chat_id == 'tracker-chat':
            return False  # its turn is still active
        posted.append((chat_id, entry['alert']['id']))
        return True
    f.delivery.post = post
    for key in ('event-1', 'event-2', 'event-3'):
        alert(f, key)
    f.delivery.fan_out(other['id'], alert_id='event-9', summary=SUMMARY)
    assert f.delivery.sync_entries(limit=2) == 1 and posted == [('door-chat', 'event-9')]
    assert f.delivery.sync_entries(limit=2) == 0 and len(posted) == 1  # an entry is written once per state


def test_a_correction_is_a_new_revision_of_the_same_alert(tmp_path):
    f = delivery_fixture(tmp_path)
    original = alert(f)
    work(f)
    updated = f.delivery.fan_out(f.key, alert_id='event-1', alert_revision=2, summary='Two people are waiting now.')
    assert [(row['alert_id'], row['alert_revision']) for row in updated] == [('event-1', 2)]
    assert updated[0]['id'] == delivery_id('company', f.key, 'alice', 'event-1', 2) != original[0]['id']
    assert updated[0]['text'] == 'Update to an earlier alert from Queue watch: Two people are waiting now.'
    assert updated[0]['link'] == original[0]['link'] == link()
    work(f)
    assert [value['text'] for value in f.wire()[-1]['template']['components'][0]['parameters']] == [
        'Update to an earlier alert from Queue watch', 'Two people are waiting now.', link()]
    assert [entry['alert'] for _, entry in f.chat] == [{'id': 'event-1', 'revision': 1}, {'id': 'event-1', 'revision': 2}]
    assert len(f.wire()) == 2 and f.delivery.fan_out(f.key, alert_id='event-1', alert_revision=2, summary='Two people are waiting now.') == []


def test_a_paused_policy_writes_zero_deliveries_and_resuming_covers_later_alerts_only(tmp_path):
    f = delivery_fixture(tmp_path)
    f.phone.set_delivery(f.key, 'paused', by='alice')
    assert alert(f) == []
    work(f)
    assert f.wire() == [] and f.chat == [] and f.delivery.deliveries(f.key) == []
    f.phone.set_delivery(f.key, 'on', by='alice')
    # Switching back on applies to alerts committed afterwards: the paused one stays in Rocoto.
    assert alert(f) == []
    work(f)
    assert f.wire() == []
    assert [row['alert_id'] for row in alert(f, 'event-2')] == ['event-2']
    work(f)
    assert len(f.wire()) == 1 and states(f) == {'alice': ('sent', None)}


def test_stop_cancels_queued_deliveries_and_resuming_sends_nothing_for_that_period(tmp_path):
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    f.phone.add_participant(f.key, 'bob', by='alice')
    alert(f)
    # The creator's Stop for this agent reaches every queued delivery in the same change.
    f.phone.set_delivery(f.key, 'paused', by='alice')
    assert states(f) == {'alice': ('cancelled', None), 'bob': ('cancelled', None)}
    f.phone.set_delivery(f.key, 'on', by='alice')
    work(f, ticks=2)
    assert f.wire() == [] and states(f) == {'alice': ('cancelled', None), 'bob': ('cancelled', None)}
    # A participant's own switch and a removal stop their own queued deliveries.
    alert(f, 'event-2')
    f.phone.update_binding(f.key, 'alice', by='alice', enabled=False)
    f.phone.remove_participant(f.key, 'bob', by='alice')
    assert states(f, 'event-2') == {'alice': ('cancelled', None), 'bob': ('cancelled', None)}
    work(f, ticks=2)
    assert f.wire() == [] and used(f) == {}


def test_an_edit_only_change_writes_zero_deliveries_and_makes_zero_provider_calls(tmp_path):
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    alert(f)
    work(f)
    assert len(f.wire()) == 1
    before = (len(f.sent), f.delivery.deliveries(f.key))
    # Participant, switch, channel, policy and Tracker edits: configuration and history only.
    f.phone.add_participant(f.key, 'bob', by='alice')
    f.phone.update_binding(f.key, 'bob', by='bob', channel='whatsapp')
    f.phone.update_binding(f.key, 'alice', by='alice', enabled=False)
    f.phone.update_binding(f.key, 'alice', by='alice', enabled=True)
    f.phone.set_delivery(f.key, 'paused', by='alice')
    f.phone.set_delivery(f.key, 'on', by='alice')
    f.agents.update(f.key, {'title': 'Queue watch', 'instruction': 'Report queue events at the tills.', 'dataset': 'shop'}, ['shop'])
    work(f, ticks=2)
    # The alert recorded before the edits is read again: the participant added afterwards gets nothing for it.
    assert alert(f) == []
    work(f, ticks=2)
    assert (len(f.sent), f.delivery.deliveries(f.key)) == before and len(f.chat) == 1


def test_an_alert_from_before_the_agent_listened_or_a_participant_joined_reaches_zero_phones(tmp_path):
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    # Committed before this agent's phone records existed: its source being read again adds nothing.
    assert alert(f, 'event-0', committed_at=f.now[0] - 1) == []
    assert alert(f, committed_at=f.now[0] - 60, since=f.now[0] - 30) == []
    # Older than a delivery's 900 seconds when the host first reads it.
    assert alert(f, 'event-2', committed_at=f.now[0] - 900) == []
    f.now[0] += 10
    f.phone.add_participant(f.key, 'bob', by='alice')
    assert [row['member_id'] for row in alert(f, 'event-3', committed_at=f.now[0] - 5, since=f.now[0] - 500)] == ['alice']
    assert [row['member_id'] for row in alert(f, 'event-4', committed_at=f.now[0], since=f.now[0] - 500)] == ['alice', 'bob']
    work(f)
    assert len(f.wire()) == 3 and f.delivery.deliveries(f.key, alert_id='event-1') == [] and f.delivery.deliveries(f.key, alert_id='event-0') == []
    # A Tracking Agent from before this rule gets its default records with its first alert, and that alert.
    earlier = f.agents.create({'title': 'Door watch', 'instruction': 'Report door events.', 'dataset': 'shop',
        'agent_kind': 'tracker', 'input_kind': 'tracker_events'}, ['shop'], owner_principal_id='alice',
        conversation_chat_id='door-chat')
    first = f.delivery.fan_out(earlier['id'], alert_id='event-5', summary=SUMMARY, committed_at=f.now[0] - 1, since=f.now[0] - 500)
    assert [(row['member_id'], row['chat_id'], row['status']) for row in first] == [('alice', 'door-chat', 'queued')]
    assert f.phone.state(earlier['id'])['policy']['participant_phone_delivery'] == 'on'


def test_the_replay_lane_a_core_module_and_an_action_agent_write_zero_deliveries(tmp_path):
    f = delivery_fixture(tmp_path / 'tracker')
    for lane in ('replay', 'unknown'):
        assert alert(f, lane=lane) == []
    work(f)
    assert f.wire() == [] and f.chat == [] and f.delivery.deliveries(f.key) == []
    with sqlite3.connect(f.store.path) as db:
        assert db.execute('SELECT count(*) FROM phone_alerts').fetchone()[0] == 0
    # The same event arriving in the live lane is an alert.
    assert len(alert(f, lane='live')) == 1
    # Phone chat belongs to Tracking Agents: an Action Agent and an unknown agent hold no delivery.
    action = delivery_fixture(tmp_path / 'action', kind='action')
    assert alert(action) == [] and action.delivery.fan_out('module-queue', alert_id='event-1', summary=SUMMARY) == []
    work(action)
    assert action.wire() == [] and action.delivery.deliveries(action.key) == []
    with pytest.raises(ValueError):
        f.delivery.fan_out(f.key, alert_id='', summary=SUMMARY)
    with pytest.raises(ValueError):
        f.delivery.fan_out(f.key, alert_id='event-9', alert_revision='2', summary=SUMMARY)


def test_retained_delivery_history_is_bounded_and_a_full_store_sends_nothing(tmp_path, monkeypatch):
    import phone_delivery
    f = delivery_fixture(tmp_path)
    monkeypatch.setattr(phone_delivery, 'MAX_DELIVERIES', 1)
    assert len(alert(f)) == 1
    with pytest.raises(ValueError, match='Review retained delivery history'):
        alert(f, 'event-2')
    work(f)
    assert len(f.wire()) == 1 and [row['alert_id'] for row in f.delivery.deliveries(f.key)] == ['event-1']


def test_alert_lane_reads_the_stream_window_the_event_arrived_in(tmp_path):
    from monitoring import MonitoringStore
    from stream_ingestion import StreamIngestion
    source = {'id': 'clip', 'video_id': 'recording', 'dataset': 'shop', 'start': 0, 'end': 60}
    monitoring = MonitoringStore(tmp_path, source_lookup=lambda owner, key: source if key == 'clip' else None)
    profile = monitoring.save_profile('alice', {'name': 'Queue', 'dataset': 'shop',
        'vocabulary': [{'type': 'queue_forms', 'description': 'A queue forms at a till.'}]}, datasets=['shop'])
    ingestion = StreamIngestion(tmp_path, monitoring)
    seen = []
    monitoring.event_sink = lambda event: seen.append((event['event_key'], alert_lane(ingestion, event)))
    def observation(key, start):
        return {'profile_id': profile['id'], 'profile_revision': 1, 'event_key': key, 'type': 'queue_forms',
            'description': SUMMARY, 'source': {'clip_id': 'clip', 'start_s': start, 'end_s': start + 1}}
    def window(stream, kind, key):
        return {'stream_id': stream, 'source_id': 'clip', 'profile_id': profile['id'], 'profile_revision': 1,
            'start_s': 0, 'end_s': 30, 'input_kind': kind, 'observations': [observation(key, 2)],
            'model': 'fixture', 'sampling': 'fixture', 'processing_seconds': 0}
    ingestion.accept('alice', window('replay-stream', 'dataset_replay', 'replayed'))
    ingestion.accept('alice', window('live-stream', 'live', 'observed'))
    monitoring.commit_event('alice', observation('typed', 40))  # a person or a model turn, outside any window
    assert seen == [('replayed', 'replay'), ('observed', 'live'), ('typed', 'live')]
    # A replayed window read again stays in its lane; an unreadable lane is unknown, and only live alerts reach phones.
    ingestion.accept('alice', window('replay-stream', 'dataset_replay', 'replayed'))
    assert seen[-1] == ('replayed', 'replay')
    assert alert_lane(SimpleNamespace(connect=lambda: sqlite3.connect(tmp_path / 'absent' / 'store.sqlite')), {'id': 'e'}) == 'unknown'


def test_admission_checks_repeat_at_claim(tmp_path):
    f = delivery_fixture(tmp_path, verified=('alice', 'bob', 'carol', 'olga'))
    for member in ('bob', 'carol', 'olga'):
        f.phone.add_participant(f.key, member, by='alice')
    alert(f)
    # Between the alert and its claim: alice leaves the company, bob stops the channel, carol's window is shut.
    del f.roles['alice']
    f.store.authorized = lambda actor: actor.member_id in f.roles
    with sqlite3.connect(f.store.path) as db:
        db.execute("UPDATE contacts SET opted_out=? WHERE member='bob'", (f.now[0],))
    f.provider.config = replace(f.provider.config, utility_templates=())
    writes(f, 'olga')
    work(f)
    assert states(f) == {'alice': ('held', 'access_required'), 'bob': ('held', 'opted_out'),
        'carol': ('held', 'window_closed'), 'olga': ('sent', None)}
    assert [item['to'] for item in f.wire()] == ['34644444444']
    # A held delivery stays unsent for good: completing the missing step is an edit, and it sends nothing.
    f.roles['alice'] = 'operator'
    f.provider.config = replace(f.provider.config, utility_templates=(TEMPLATE,))
    work(f, ticks=2)
    assert len(f.wire()) == 1 and states(f)['carol'] == ('held', 'window_closed')
    # The channel's sender stops being ready: the alert stays in Rocoto.
    alert(f, 'event-2')
    f.provider.config = replace(f.provider.config, commissioned=False)
    work(f)
    assert states(f, 'event-2')['olga'] == ('held', 'channel_unready') and len(f.wire()) == 1
    # 900 seconds with the message still unsent.
    f.provider.config = replace(f.provider.config, commissioned=True)
    alert(f, 'event-3')
    f.now[0] += 900
    work(f)
    assert states(f, 'event-3')['olga'] == ('expired', None) and len(f.wire()) == 1


def test_an_alert_with_no_rocoto_origin_stays_in_rocoto(tmp_path):
    f = delivery_fixture(tmp_path, origin='')
    assert alert(f)[0]['link'] == ''
    work(f)
    assert states(f) == {'alice': ('held', 'channel_unready')} and f.wire() == []


def test_the_fence_repeats_the_checks_immediately_before_the_provider_call(tmp_path):
    f = delivery_fixture(tmp_path)
    alert(f)
    claim = f.delivery._claim
    def claim_then_pause(identifier):
        claimed = claim(identifier)
        f.phone.set_delivery(f.key, 'paused', by='alice')  # Stop lands after the claim, ahead of the send
        return claimed
    f.delivery._claim = claim_then_pause
    work(f)
    f.delivery._claim = claim
    # The request stayed inside Rocoto: its units are back and Stop closed it at the fence.
    assert f.wire() == [] and used(f) == {'company': 0, 'agent:' + f.key: 0}
    assert states(f) == {'alice': ('cancelled', None)} and f.delivery.deliveries(f.key)[0]['attempts'] == 1
    work(f)
    assert states(f) == {'alice': ('cancelled', None)} and f.wire() == []


@pytest.mark.parametrize('stop', ['agent_paused', 'participant_off', 'participant_removed'])
def test_a_delivery_stopped_at_the_fence_stays_unsent_after_resume(tmp_path, stop):
    """CHANNELS.md "Stop": Stop admits zero new claims and resuming sends nothing for the period it covered."""
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    f.phone.add_participant(f.key, 'bob', by='alice')
    target = next(row for row in alert(f) if row['member_id'] == 'bob')
    wire = len(f.wire())
    async def claimed_then_stopped():
        actor, outbox, prepared = f.delivery._claim(target['id'])  # dispatching: units reserved, outbox written
        if stop == 'agent_paused':
            f.phone.set_delivery(f.key, 'paused', by='alice')
        elif stop == 'participant_off':
            f.phone.update_binding(f.key, 'bob', by='bob', enabled=False)
        else:
            f.phone.remove_participant(f.key, 'bob', by='alice')
        await f.store.send_prepared(actor, outbox, prepared, still=lambda: f.delivery._still(target['id'], outbox['id']))
    asyncio.run(claimed_then_stopped())
    assert states(f)['bob'] == ('cancelled', None) and len(f.wire()) == wire
    assert used(f).get('agent:' + f.key, 0) == 0  # the units came back
    # Resume ahead of the worker's next pass: the alert from before Stop stays in Rocoto.
    if stop == 'agent_paused':
        f.phone.set_delivery(f.key, 'on', by='alice')
    elif stop == 'participant_off':
        f.phone.update_binding(f.key, 'bob', by='bob', enabled=True)
    else:
        f.phone.add_participant(f.key, 'bob', by='alice')
    work(f, ticks=3)
    assert states(f)['bob'] == ('cancelled', None)
    assert '34622222222' not in [item['to'] for item in f.wire()[wire:]]
    # A later alert reaches him again.
    alert(f, 'event-2')
    work(f)
    assert states(f, 'event-2')['bob'] == ('sent', None)


def test_an_uncertain_send_keeps_its_id_and_is_reconciled_before_any_retry(tmp_path):
    f = delivery_fixture(tmp_path)
    lost = [True]
    def handler(request):
        if lost[0] and json.loads(request.content).get('type') == 'template' and b'rocoto_monitoring' in request.content:
            raise httpx.ReadTimeout('the response was lost')
        return httpx.Response(200, json={'messaging_product': 'whatsapp', 'messages': [{'id': f'wamid.out-{len(f.sent)}'}]})
    f.provider.transport = httpx.MockTransport(lambda request: (f.sent.append(json.loads(request.content)), handler(request))[1])
    written = alert(f)
    work(f)
    assert states(f) == {'alice': ('uncertain', None)} and len(f.wire()) == 1
    assert used(f) == {'company': 1, 'agent:' + f.key: 1}  # an uncertain send keeps its units
    # Later passes, a replayed event and a restart leave it where it is.
    alert(f)
    work(f, ticks=3)
    f.store.recover()
    work(f)
    assert len(f.wire()) == 1 and states(f) == {'alice': ('uncertain', None)}
    # Other alert revisions keep their own deliveries.
    lost[0] = False
    alert(f, 'event-2')
    work(f)
    assert states(f, 'event-2') == {'alice': ('sent', None)} and len(f.wire()) == 2
    with pytest.raises(ValueError):
        f.delivery.reconcile(written[0]['id'], 'maybe')
    # Reconciled as unsent inside its 900 seconds it returns to queued under the same ID.
    assert f.delivery.reconcile(written[0]['id'], 'unsent')['status'] == 'queued'
    work(f)
    row = f.delivery.deliveries(f.key, alert_id='event-1')[0]
    assert (row['id'], row['status'], row['attempts']) == (written[0]['id'], 'sent', 2) and len(f.wire()) == 3
    with sqlite3.connect(f.store.path) as db:  # the review is on record with the operator who looked
        assert db.execute('SELECT delivery,outcome,reviewed_by,reviewed FROM phone_reviews').fetchall() == [
            (written[0]['id'], 'unsent', 'host', f.now[0])]
    with sqlite3.connect(f.store.path) as db:
        assert db.execute('SELECT request_id,status FROM outbox WHERE request_id LIKE ? ORDER BY request_id',
            (row['id'] + ':%',)).fetchall() == [(row['id'] + ':1', 'cancelled'), (row['id'] + ':2', 'sent')]
    with pytest.raises(ValueError):
        f.delivery.reconcile(row['id'], 'unsent')  # only a delivery that is confirming its send


def test_a_restart_moves_a_dispatching_delivery_to_uncertain(tmp_path):
    f = delivery_fixture(tmp_path)
    written = alert(f)
    assert f.delivery._claim(written[0]['id']) is not None  # the process stops between the claim and the provider's answer
    assert states(f) == {'alice': ('dispatching', None)}
    restarted = Messaging(f.store.state, company_id='company', provider=f.provider, authorized=lambda actor: True,
        clock=lambda: f.now[0], spend=f.store.spend)
    restarted.recover()
    f.delivery = PhoneDelivery(restarted, PhoneChat(restarted, agents=f.agents, member=f.phone.member), origin=ORIGIN)
    work(f, ticks=2)
    assert states(f) == {'alice': ('uncertain', None)} and f.wire() == []
    # Reconciled with the provider's own status it keeps that status.
    assert f.delivery.reconcile(written[0]['id'], 'delivered')['status'] == 'delivered'
    work(f)
    assert f.wire() == []


def test_a_send_that_stayed_inside_rocoto_is_retried_three_times_then_held(tmp_path):
    f = delivery_fixture(tmp_path)
    alert(f)
    still = f.delivery._still
    f.delivery._still = lambda identifier, outbox: False  # the fence closes each time
    work(f, ticks=2)
    assert states(f) == {'alice': ('queued', None)} and f.delivery.deliveries(f.key)[0]['attempts'] == 2
    work(f, ticks=3)
    assert states(f) == {'alice': ('held', 'channel_unready')} and f.delivery.deliveries(f.key)[0]['attempts'] == 3
    assert f.wire() == [] and used(f) == {'company': 0, 'agent:' + f.key: 0}
    f.delivery._still = still
    work(f, ticks=2)
    assert f.wire() == []


def multi_fixture(tmp_path):
    """Viber, SMS and WhatsApp behind local stand-ins, with bob verified on SMS and WhatsApp."""
    from test_phone_channels import channels_fixture, local_code, member, verify as verify_channel, whatsapp_code
    from agent_test_support import retail_agent_store as AgentStore
    store, whatsapp, sms, bot, sent, now = channels_fixture(tmp_path / 'messaging')
    whatsapp.config = replace(whatsapp.config, utility_templates=(TEMPLATE,))
    agents = AgentStore(tmp_path / 'agents', input_provider=RetailInputProvider())
    saved = agents.create({'title': 'Queue watch', 'instruction': 'Report queue events.', 'dataset': 'shop',
        'agent_kind': 'tracker', 'input_kind': 'tracker_events'}, ['shop'], owner_principal_id='bob',
        conversation_chat_id='tracker-chat')
    phone = PhoneChat(store, agents=agents, member=lambda principal: {'id': principal, 'display_name': principal.title(), 'role': 'operator'})
    spend = {'company_daily_messages': 50, 'agent_daily_messages': 20, 'stopped': False}
    store.spend = lambda: dict(spend)
    async def enroll():
        assert (await verify_channel(store, member('bob'), '+34622222222', 'sms', local_code(sms)))['verified']
        now[0] += 61
        assert (await verify_channel(store, member('bob'), '+34622222222', 'whatsapp', whatsapp_code(sent)))['verified']
    asyncio.run(enroll())
    phone.promote(saved)
    codes = len(sms.sent)  # the security code this channel carried
    return SimpleNamespace(store=store, whatsapp=whatsapp, sms=sms, viber=bot, sent=sent, now=now, agents=agents, key=saved['id'],
        phone=phone, spend=spend, delivery=PhoneDelivery(store, phone, origin=ORIGIN), texts=lambda: sms.sent[codes:])


def test_channel_order_then_the_next_eligible_channel_after_a_definitive_rejection(tmp_path):
    f = multi_fixture(tmp_path)
    written = f.delivery.fan_out(f.key, alert_id='event-1', summary=SUMMARY)
    assert written[0]['channel'] == 'sms'  # Viber, then SMS, then WhatsApp: the first where a verified address is held
    work(f)
    assert [item['body'] for item in f.texts()] == [TEXT + ' ' + link()]
    row = f.delivery.deliveries(f.key)[0]
    assert (row['status'], row['channel'], row['account'], row['units'], row['provider_message_id']) == ('sent', 'sms', 'sms-account-1', 1, 'sms.out-2')
    # A definitive rejection returns the same delivery ID to queued for the next eligible channel.
    f.sms.failure = SendRejected('The provider refused this message.')
    second = f.delivery.fan_out(f.key, alert_id='event-2', summary=SUMMARY)
    work(f)
    row = f.delivery.deliveries(f.key, alert_id='event-2')[0]
    assert (row['id'], row['status'], row['channel'], row['attempts']) == (second[0]['id'], 'queued', 'sms', 1)
    work(f)
    row = f.delivery.deliveries(f.key, alert_id='event-2')[0]
    assert (row['id'], row['status'], row['channel'], row['attempts'], row['units']) == (second[0]['id'], 'sent', 'whatsapp', 2, 2)
    assert f.sent[-1]['to'] == '34622222222' and f.sent[-1]['template']['name'] == TEMPLATE.name and len(f.texts()) == 1
    # A picked channel stays where it is: the rejection is its final answer.
    f.phone.update_binding(f.key, 'bob', by='bob', channel='sms')
    count = len(f.sent)
    f.delivery.fan_out(f.key, alert_id='event-3', summary=SUMMARY)
    work(f, ticks=3)
    row = f.delivery.deliveries(f.key, alert_id='event-3')[0]
    assert (row['status'], row['channel'], row['attempts']) == ('failed', 'sms', 1) and len(f.sent) == count
    # Every eligible channel rejected under an automatic choice: the delivery failed.
    f.phone.update_binding(f.key, 'bob', by='bob', channel='automatic')
    f.whatsapp.transport = httpx.MockTransport(lambda request: httpx.Response(400, json={'error': {'code': 131026}}))
    f.delivery.fan_out(f.key, alert_id='event-4', summary=SUMMARY)
    work(f, ticks=4)
    row = f.delivery.deliveries(f.key, alert_id='event-4')[0]
    assert (row['status'], row['channel'], row['attempts']) == ('failed', 'whatsapp', 2)


def test_an_sms_alert_is_bounded_to_three_segments_with_its_link(tmp_path):
    f = multi_fixture(tmp_path)
    written = f.delivery.fan_out(f.key, alert_id='event-1', summary='Queue status. ' * 60)
    assert len(written[0]['text']) == 600
    work(f)
    body = f.texts()[-1]['body']
    assert len(body) <= 480 and body.endswith('... ' + link()) and body.startswith('Queue watch: Queue status.')
    row = f.delivery.deliveries(f.key)[0]
    assert (row['status'], row['units']) == ('sent', 3) and len(row['text']) == 600  # the alert in Rocoto keeps its text


def test_delivery_leaves_the_agents_run_as_it_is(tmp_path):
    """Fan-out starts no run, ends no run and leaves an active turn untouched (CHANNELS.md)."""
    import time
    from background_agents import AgentScheduler
    from agent_test_support import retail_agent_store as AgentStore
    from test_messaging import fixture
    from test_monitoring import SOURCES, event, profile, store as monitoring_store

    class Index:
        clips = list(SOURCES.values())
        def status(self): return {'ready': False, 'datasets': ['shop']}
        def get_clip(self, key): return SOURCES.get(key)

    class Harness:
        def __init__(self):
            self.children, self.child_tasks, self.active, self.calls, self.release = {}, {}, {}, [], asyncio.Event()
        def _save_child(self, child): self.children[child['id']] = child
        async def turn(self, message, chat_id, dataset, **kwargs):
            self.calls.append(message)
            await self.release.wait()
            self.children[chat_id]['messages'].append({'role': 'assistant', 'content': 'One queue event reviewed.'})
            yield {'type': 'done', 'status': 'completed'}

    async def scenario():
        monitoring = monitoring_store(tmp_path / 'monitoring')
        watched = profile(monitoring)
        agents, harness = AgentStore(tmp_path / 'agents', input_provider=RetailInputProvider(monitoring=monitoring,index=Index())), Harness()
        scheduler = AgentScheduler(agents, harness, asyncio.Semaphore(1))
        saved = agents.create({'title': 'Pickup watch', 'instruction': 'Summarize pickups.', 'dataset': 'shop',
            'agent_kind': 'tracker', 'input_kind': 'tracker_events', 'mode': 'ongoing', 'max_runs': 3,
            'profile_ids': [watched['id']]}, ['shop'], owner_principal_id='alice')
        store, _, provider, sent, now = fixture(tmp_path / 'messaging')
        provider.config = replace(provider.config, utility_templates=(TEMPLATE,))
        store.spend = lambda: {'company_daily_messages': 50, 'agent_daily_messages': 20, 'stopped': False}
        phone = PhoneChat(store, agents=agents, member=lambda principal: {'id': principal, 'display_name': 'Alice', 'role': 'operator'})
        await verify(store, sent, 'alice')
        def post(chat_id, entry):
            record = harness.children[chat_id]
            if record.get('running') or record.get('active_run_id'):
                return False
            record['messages'].append(entry)
            return True
        delivery = PhoneDelivery(store, phone, origin=ORIGIN, post=post)
        harness.phone_delivery = delivery
        scheduler.ensure_conversation(saved)
        phone.promote(saved)
        observed = monitoring.commit_event('alice', event(watched))
        agents.activate(saved['id'], time.time() + 1000)
        assert agents.active_since(saved['id']) <= time.time()
        await scheduler.tick()
        while not harness.calls:
            await asyncio.sleep(0)
        chat = harness.children[saved['chat_id']]
        assert chat['running'] and agents.get(saved['id'])['last_run']['status'] == 'running'
        # The alert reaches the creator's phone while the agent's own turn keeps running.
        written = delivery.fan_out(saved['id'], alert_id=observed['id'], summary=observed['description'])
        await delivery.tick()
        await delivery.drain()
        assert [row['status'] for row in delivery.deliveries(saved['id'])] == ['sent'] and len(written) == 1
        assert sent[-1]['to'] == '34611111111' and sent[-1]['template']['components'][0]['parameters'][1]['text'] == 'Picks up a blue packet.'
        record = agents.get(saved['id'])
        assert (record['last_run']['status'], len(record['runs']), record['status']) == ('running', 1, 'active')
        assert chat['running'] and not harness.release.is_set() and [m for m in chat['messages'] if m.get('alert')] == []
        assert agents.wakes.pending(saved['id'], owner='alice') is None  # fan-out queued no wake of its own
        harness.release.set()
        await asyncio.gather(*scheduler.tasks.values())
        record = agents.get(saved['id'])
        assert (record['last_run']['status'], len(record['runs']), record['runs_started']) == ('completed', 1, 1)
        # Run completion drains the turn: the alert entry joins the conversation after the run's own report.
        assert [m.get('alert', {}).get('id') for m in chat['messages']][-2:] == [None, observed['id']]
        report = agents.run_report(saved['id'], record['last_run']['id'])['report_messages']
        assert all('alert' not in message for message in report) and report[-1]['content'] == 'One queue event reviewed.'
        await scheduler.close()
    asyncio.run(scenario())


def lost_response(f):
    """The provider accepts the alert and its response is lost on the way back."""
    def handler(request):
        if b'rocoto_monitoring' in request.content:
            raise httpx.ReadTimeout('the response was lost')
        return httpx.Response(200, json={'messaging_product': 'whatsapp', 'messages': [{'id': f'wamid.out-{len(f.sent)}'}]})
    f.provider.transport = httpx.MockTransport(lambda request: (f.sent.append(json.loads(request.content)), handler(request))[1])


def test_an_uncertain_send_stays_reviewable_for_a_day_then_closes_with_its_units(tmp_path):
    """CHANNELS.md "Uncertain sends": an uncertain delivery has a stated end. It is never sent again."""
    from phone_delivery import RECONCILE_SECONDS
    f = delivery_fixture(tmp_path)
    lost_response(f)
    written = alert(f)
    work(f)
    assert states(f) == {'alice': ('uncertain', None)} and len(f.wire()) == 1
    # A signed receipt carries a provider message ID. The lost response gave this send none, so nothing matches it.
    receipt = {'id': 'wamid.accepted-by-provider', 'status': 'delivered', 'timestamp': str(int(f.now[0])), 'recipient_id': '34611111111'}
    assert f.store.handle_webhook(*webhook(f.provider, statuses=[receipt]))['delivery_updates'] == 0
    f.now[0] += RECONCILE_SECONDS - 1
    work(f, ticks=2)
    assert states(f) == {'alice': ('uncertain', None)}
    f.now[0] += 1
    work(f, ticks=2)
    row = f.delivery.deliveries(f.key)[0]
    assert (row['id'], row['status'], row['held_reason'], row['units'], row['attempts']) == (written[0]['id'], 'expired', None, 1, 1)
    assert len(f.wire()) == 1 and f.chat[0][1]['channel_deliveries'][0]['status'] == 'expired'
    with pytest.raises(ValueError, match='Choose a delivery that is confirming its send'):
        f.delivery.reconcile(written[0]['id'], 'unsent')


def test_the_host_command_records_the_operators_review_of_an_uncertain_send(tmp_path, capsys):
    """The entry point of operator review: `phone_delivery.py reconcile <state> <delivery_id> <outcome> --by <name>`."""
    import phone_delivery
    f = delivery_fixture(tmp_path)
    lost_response(f)
    written = alert(f)
    work(f)
    assert states(f) == {'alice': ('uncertain', None)}
    monkey = pytest.MonkeyPatch()
    monkey.setattr('workspace_identity.workspace_scope', lambda state: 'company')
    try:
        state = str(f.store.state)
        for arguments in (['reconcile', state, written[0]['id'], 'delivered'],                 # the operator is named
                ['reconcile', state, written[0]['id'], 'maybe', '--by', 'peter'],                # a reviewed outcome
                ['reconcile', state, 'dlv-unknown', 'delivered', '--by', 'peter'],               # a delivery confirming its send
                ['reconcile', str(tmp_path / 'elsewhere'), written[0]['id'], 'delivered', '--by', 'peter']):
            with pytest.raises(SystemExit) as stopped:
                phone_delivery.main(arguments)
            assert stopped.value.code == 2
        assert states(f) == {'alice': ('uncertain', None)} and not (tmp_path / 'elsewhere').exists()
        capsys.readouterr()
        assert phone_delivery.main(['reconcile', state, written[0]['id'], 'delivered', '--by', 'peter']) == 0
    finally:
        monkey.undo()
    printed = json.loads(capsys.readouterr().out)
    assert (printed['id'], printed['status'], printed['units']) == (written[0]['id'], 'delivered', 1)
    assert states(f) == {'alice': ('delivered', None)} and len(f.wire()) == 1
    with sqlite3.connect(f.store.path) as db:
        assert db.execute('SELECT delivery,outcome,reviewed_by FROM phone_reviews').fetchall() == [(written[0]['id'], 'delivered', 'peter')]
    work(f, ticks=2)
    assert f.chat[0][1]['channel_deliveries'][0]['status'] == 'delivered' and len(f.wire()) == 1


def test_retention_releases_old_history_and_alerts_keep_flowing(tmp_path, monkeypatch):
    """CHANNELS.md "Records": a delivery that reached a provider is kept 366 days, an unsent one 24 hours."""
    import phone_delivery
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    f.phone.add_participant(f.key, 'bob', by='alice')
    with sqlite3.connect(f.store.path) as db:
        db.execute("UPDATE contacts SET opted_out=? WHERE member='bob'", (f.now[0],))
    alert(f)
    work(f)
    assert states(f) == {'alice': ('sent', None), 'bob': ('held', 'opted_out')}
    def rows():
        with sqlite3.connect(f.store.path) as db:
            return (db.execute('SELECT count(*) FROM phone_deliveries').fetchone()[0],
                db.execute('SELECT count(*) FROM phone_alerts').fetchone()[0],
                db.execute("SELECT count(*) FROM outbox WHERE request_id LIKE 'dlv-%'").fetchone()[0])
    assert rows() == (2, 1, 1)
    # Inside both periods a pass removes nothing, and passes are a minute apart.
    f.now[0] += 86400
    assert f.delivery.prune(force=True) == 0 and rows() == (2, 1, 1)
    f.now[0] += 1
    assert f.delivery.prune(force=True) == 1 and f.delivery.prune() == 0
    assert states(f) == {'alice': ('sent', None)} and rows() == (1, 1, 1)  # the unsent one left after 24 hours
    f.now[0] += 364 * 86400
    work(f)
    assert rows() == (1, 1, 1)  # inside the 366 days the activity statistics read
    f.now[0] += 86400
    work(f)
    assert rows() == (0, 0, 0)  # the worker's own pass: 366 days after the provider's answer
    # A full store prunes ahead of refusing, so a year of held history leaves the next alert its room.
    monkeypatch.setattr(phone_delivery, 'MAX_DELIVERIES', 5)
    for key in ('old-1', 'old-2', 'old-3'):
        alert(f, key)
    with sqlite3.connect(f.store.path) as db:
        db.execute("UPDATE phone_deliveries SET status='held',held_reason='spend_unreadable'")
        assert db.execute('SELECT count(*) FROM phone_deliveries').fetchone()[0] == 6
    assert f.delivery.history_full() is True
    f.now[0] += 400 * 86400
    assert [row['member_id'] for row in alert(f, 'event-new')] == ['alice', 'bob'] and f.delivery.history_full() is False
    # Recent history that fills the store is the one case that still refuses, and the block says so.
    monkeypatch.setattr(phone_delivery, 'MAX_DELIVERIES', 2)
    with pytest.raises(ValueError, match='Review retained delivery history'):
        alert(f, 'event-later')
    assert f.delivery.block(f.key, 'alice', members=[{'id': 'alice', 'display_name': 'Alice'}])['history'] == 'full'


def test_an_observations_lane_is_decided_once_and_kept(tmp_path):
    f = delivery_fixture(tmp_path)
    asked = []
    def decide(answer):
        def read():
            asked.append(answer)
            if answer == 'broken':
                raise OSError('the stream store is busy')
            return answer
        return read
    # An answer that could not be read is asked again; only a live alert reaches a phone.
    assert f.delivery.lane('event-a', decide('unknown')) == 'unknown' and f.delivery.lane('event-a', decide('broken')) == 'unknown'
    assert f.delivery.lane('event-a', decide('windowless')) == 'unknown'
    assert f.delivery.lane('event-a', decide('replay')) == 'replay'
    # Once kept, a later read gets the kept lane, whatever the moment would answer now.
    assert f.delivery.lane('event-a', decide('live')) == 'replay' and f.delivery.lane('event-b', decide('live')) == 'live'
    assert f.delivery.lane('event-b', decide('replay')) == 'live'
    assert asked == ['unknown', 'broken', 'windowless', 'replay', 'live']
    # The catch-up pass reads again what holds no receipt and no kept replay lane.
    alert(f, 'event-b')
    assert f.delivery.unread(f.key, ['event-a', 'event-b', 'event-c']) == {'event-c'}
    assert f.delivery.unread('agent-other', ['event-a', 'event-b']) == {'event-b'}


def test_window_lane_tells_a_windowless_observation_from_a_live_window(tmp_path):
    from phone_delivery import window_lane
    from stream_ingestion import StreamIngestion
    from test_monitoring import SOURCES, profile, store as monitoring_store
    monitoring = monitoring_store(tmp_path / 'monitoring')
    watched = profile(monitoring)
    ingestion = StreamIngestion(tmp_path / 'ingestion', monitoring)
    def window(stream, kind, key):
        return {'stream_id': stream, 'source_id': 'a', 'profile_id': watched['id'], 'profile_revision': 1, 'start_s': 0, 'end_s': 5,
            'input_kind': kind, 'model': 'fixture', 'sampling': 'fixture', 'processing_seconds': 0,
            'observations': [{'profile_id': watched['id'], 'profile_revision': 1, 'event_key': key, 'type': 'pick_up',
                'description': 'Picks up a blue packet.', 'source': {'clip_id': 'a', 'start_s': 2, 'end_s': 3}}]}
    replayed = ingestion.accept('alice', window('replay-1', 'dataset_replay', 'replayed'))
    live = ingestion.accept('alice', window('live-1', 'live', 'observed-live'))
    typed = monitoring.commit_event('alice', {'profile_id': watched['id'], 'profile_revision': 1, 'event_key': 'typed',
        'type': 'pick_up', 'description': 'Picks up a red packet.', 'source': {'clip_id': 'a', 'start_s': 2, 'end_s': 3}})
    lanes = [window_lane(ingestion, monitoring.get_event('alice', key)) for key in (replayed['event_ids'][0], live['event_ids'][0], typed['id'])]
    assert lanes == ['replay', 'live', 'windowless']
    assert [alert_lane(ingestion, monitoring.get_event('alice', key)) for key in (replayed['event_ids'][0], live['event_ids'][0], typed['id'])] == [
        'replay', 'live', 'live']
    assert window_lane(object(), monitoring.get_event('alice', typed['id'])) == 'unknown'
