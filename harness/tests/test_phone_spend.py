"""The spend bound of participant phone chat: daily caps, Stop and fail-closed reads. Local stores only."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from messaging import MessagingActor, MessagingError
from phone_spend import SCHEMA, SpendHeld, day, record, release, reserve, resets_at, usage
from test_phone_delivery import alert, delivery_fixture, states, used, work, writes

RECORD = {'company_daily_messages': 50, 'agent_daily_messages': 20, 'stopped': False}
MEMBERS = [{'id': 'alice', 'display_name': 'Alice'}, {'id': 'bob', 'display_name': 'Bob'}]


def spend(f):
    return f.delivery.block(f.key, 'alice', members=MEMBERS)['spend']


def last(f, member='alice'):
    row = next(item for item in f.delivery.block(f.key, 'alice', members=MEMBERS)['participants'] if item['member_id'] == member)
    return row['last_delivery']


def held(reason, call, *args):
    with pytest.raises(SpendHeld) as caught:
        call(*args)
    assert caught.value.reason == reason
    return str(caught.value)


def test_a_spend_record_is_read_whole_or_it_is_unreadable():
    assert record(lambda: dict(RECORD)) == RECORD
    def broken():
        raise OSError('the private document left')
    for reader in (None, broken, lambda: None, lambda: 'open', lambda: {},
            lambda: {'company_daily_messages': 50, 'stopped': False},                      # an absent cap
            lambda: {**RECORD, 'agent_daily_messages': None}, lambda: {**RECORD, 'agent_daily_messages': '20'},
            lambda: {**RECORD, 'company_daily_messages': -1}, lambda: {**RECORD, 'company_daily_messages': True},
            lambda: {**RECORD, 'company_daily_messages': 1_000_001}, lambda: {**RECORD, 'stopped': 0},
            lambda: {**RECORD, 'unbounded': True}):
        held('spend_unreadable', record, reader)


def test_units_are_reserved_per_utc_day_and_returned_once():
    db = sqlite3.connect(':memory:')
    for statement in SCHEMA:
        db.execute(statement)
    noon = datetime(2026, 10, 10, 12, tzinfo=timezone.utc).timestamp()
    reader = lambda: {'company_daily_messages': 5, 'agent_daily_messages': 3, 'stopped': False}
    assert day(noon) == '2026-10-10' and resets_at(noon) == datetime(2026, 10, 11, tzinfo=timezone.utc).timestamp()
    assert reserve(db, reader, 'agent-a', 2, noon, 'msg-1') == 2
    assert held('spend_cap', reserve, db, reader, 'agent-a', 2, noon, 'msg-2') == 'Today’s phone alert limit is reached. Phone messages resume at the next UTC midnight.'
    assert reserve(db, reader, 'agent-b', 3, noon, 'msg-3') == 3
    held('spend_cap', reserve, db, reader, 'agent-b', 1, noon, 'msg-4')  # the company's cap, across its agents
    assert usage(db, reader, 'agent-a', noon) == {'state': 'cap_reached', 'agent': {'used': 2, 'cap': 3},
        'company': {'used': 5, 'cap': 5}, 'resets_at': resets_at(noon)}
    # A send that provably stayed inside Rocoto returns its units, once.
    assert (release(db, 'msg-3'), release(db, 'msg-3'), release(db, 'msg-unknown')) == (3, 0, 0)
    assert usage(db, reader, 'agent-a', noon)['state'] == 'open' and usage(db, reader, 'agent-b', noon)['agent'] == {'used': 0, 'cap': 3}
    # The next UTC day starts new counters.
    tomorrow = noon + 86400
    assert usage(db, reader, 'agent-a', tomorrow)['agent'] == {'used': 0, 'cap': 3}
    assert reserve(db, reader, 'agent-a', 3, tomorrow, 'msg-5') == 3
    # Stop, an unusable unit count and an unreadable counter admit nothing.
    assert held('stopped', reserve, db, lambda: {**reader(), 'stopped': True}, 'agent-a', 1, noon, 'msg-6') == 'Phone messages for this company are paused.'
    for units in (0, -1, 17, 1.0, True, None):
        held('spend_unreadable', reserve, db, reader, 'agent-a', units, noon, 'msg-7')
    db.execute("UPDATE phone_spend SET units=-4 WHERE scope='company' AND day=?", (day(noon),))
    assert held('spend_unreadable', reserve, db, reader, 'agent-a', 1, noon, 'msg-8') == 'Phone messages wait for this company’s daily message limit.'
    assert usage(db, reader, 'agent-a', noon) == {'state': 'unavailable', 'agent': None, 'company': None, 'resets_at': resets_at(noon)}
    assert db.execute('SELECT outbox,units,returned FROM phone_spend_holds ORDER BY outbox').fetchall() == [
        ('msg-1', 2, 0), ('msg-3', 3, 1), ('msg-5', 3, 0)]


def test_the_agents_daily_cap_stops_the_next_send_and_shows_the_reason(tmp_path):
    f = delivery_fixture(tmp_path)
    f.spend.update(agent_daily_messages=2)
    for key in ('event-1', 'event-2', 'event-3'):
        alert(f, key)
        work(f)
    assert [states(f, key)['alice'] for key in ('event-1', 'event-2', 'event-3')] == [
        ('sent', None), ('sent', None), ('held', 'spend_cap')]
    assert len(f.wire()) == 2 and used(f) == {'company': 2, 'agent:' + f.key: 2}
    assert spend(f) == {'state': 'cap_reached', 'agent': {'used': 2, 'cap': 2}, 'company': {'used': 2, 'cap': 50},
        'resets_at': resets_at(f.now[0])}
    shown = last(f)
    assert (shown['status'], shown['held_reason'], shown['channel']) == ('held', 'spend_cap', 'whatsapp')
    assert f.chat[-1][1]['channel_deliveries'][0]['held_reason'] == 'spend_cap'
    # The next UTC day starts new counters; the held alert remains readable in Rocoto and stays unsent.
    f.now[0] = resets_at(f.now[0]) + 60
    alert(f, 'event-4')
    work(f, ticks=2)
    assert states(f, 'event-4')['alice'] == ('sent', None) and states(f, 'event-3')['alice'] == ('held', 'spend_cap')
    assert len(f.wire()) == 3 and spend(f)['agent'] == {'used': 1, 'cap': 2} and spend(f)['state'] == 'open'


def test_the_companys_daily_cap_covers_every_agent_and_participant(tmp_path):
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    f.phone.add_participant(f.key, 'bob', by='alice')
    f.spend.update(company_daily_messages=1)
    alert(f)
    work(f)
    assert sorted(states(f).values()) == [('held', 'spend_cap'), ('sent', None)] and len(f.wire()) == 1
    assert spend(f) == {'state': 'cap_reached', 'agent': {'used': 1, 'cap': 20}, 'company': {'used': 1, 'cap': 1},
        'resets_at': resets_at(f.now[0])}


def test_the_hosts_stop_holds_every_phone_send_and_resuming_sends_nothing_for_that_period(tmp_path):
    f = delivery_fixture(tmp_path)
    f.spend.update(stopped=True)
    alert(f)
    work(f)
    assert states(f) == {'alice': ('held', 'stopped')} and f.wire() == [] and used(f) == {}
    assert spend(f)['state'] == 'stopped' and last(f)['held_reason'] == 'stopped'
    f.spend.update(stopped=False)
    work(f, ticks=2)
    assert f.wire() == [] and states(f) == {'alice': ('held', 'stopped')}
    alert(f, 'event-2')
    work(f)
    assert states(f, 'event-2') == {'alice': ('sent', None)} and spend(f)['state'] == 'open'


@pytest.mark.parametrize('reader', [None, lambda: (_ for _ in ()).throw(OSError('unreadable')),
    lambda: {'company_daily_messages': 50, 'stopped': False}, lambda: {**RECORD, 'agent_daily_messages': 'twenty'}])
def test_an_unreadable_spend_record_admits_zero_sends(tmp_path, reader):
    f = delivery_fixture(tmp_path)
    f.store.spend = reader
    alert(f)
    work(f, ticks=2)
    assert states(f) == {'alice': ('held', 'spend_unreadable')} and f.wire() == []
    assert spend(f) == {'state': 'unavailable', 'agent': None, 'company': None, 'resets_at': resets_at(f.now[0])}
    assert last(f)['held_reason'] == 'spend_unreadable'


def test_an_unreadable_counter_admits_zero_sends(tmp_path):
    f = delivery_fixture(tmp_path)
    alert(f)
    work(f)
    with sqlite3.connect(f.store.path) as db:
        db.execute("UPDATE phone_spend SET units='many' WHERE scope='company'")
    alert(f, 'event-2')
    work(f)
    assert states(f, 'event-2') == {'alice': ('held', 'spend_unreadable')} and len(f.wire()) == 1
    assert spend(f)['state'] == 'unavailable'


def test_an_agent_answer_in_the_tracking_chat_counts_against_the_same_bound(tmp_path):
    f = delivery_fixture(tmp_path)
    f.spend.update(agent_daily_messages=2)
    writes(f, 'alice')
    actor = MessagingActor('company', 'alice', 'shop', 'tracker-chat', 'alice')
    f.store.bind_conversation(actor)
    async def answer(request_id):
        return await f.store.send_message(actor, 'The queue is down to two people.', request_id=request_id)
    alert(f)
    work(f)
    assert asyncio.run(answer('answer-1'))['status'] == 'sent' and used(f) == {'company': 2, 'agent:' + f.key: 2}
    sent = len(f.sent)
    with pytest.raises(MessagingError, match='Today’s phone alert limit is reached'):
        asyncio.run(answer('answer-2'))
    f.spend.update(agent_daily_messages=20, stopped=True)
    with pytest.raises(MessagingError, match='Phone messages for this company are paused'):
        asyncio.run(answer('answer-3'))
    f.store.spend = None
    with pytest.raises(MessagingError, match='Phone messages wait for this company’s daily message limit'):
        asyncio.run(answer('answer-4'))
    assert len(f.sent) == sent and used(f) == {'company': 2, 'agent:' + f.key: 2}
    with sqlite3.connect(f.store.path) as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE request_id LIKE 'answer-%'").fetchone()[0] == 1
    # A replayed request identity reads its saved receipt and reserves nothing more.
    f.store.spend = lambda: dict(f.spend, stopped=False)
    assert asyncio.run(answer('answer-1'))['status'] == 'sent' and used(f) == {'company': 2, 'agent:' + f.key: 2}
    # The bound belongs to participant phone chat: a member's own setup chat keeps its built rules.
    f.store.spend = None
    setup = MessagingActor('company', 'alice', 'shop', 'setup-alice', 'alice')
    assert asyncio.run(f.store.send_message(setup, 'Reading you on WhatsApp.', request_id='setup-1'))['status'] == 'sent'
    assert used(f) == {'company': 2, 'agent:' + f.key: 2}


def test_an_uncertain_alert_leaves_the_agents_answers_open(tmp_path):
    """Host deliveries keep their own IDs and reconciliation; the answer fence covers answers."""
    import httpx, json
    f = delivery_fixture(tmp_path)
    writes(f, 'alice')
    lost = [True]
    def transport(request):
        f.sent.append(json.loads(request.content))
        if lost[0]:
            raise httpx.ReadTimeout('the response was lost')
        return httpx.Response(200, json={'messaging_product': 'whatsapp', 'messages': [{'id': f'wamid.out-{len(f.sent)}'}]})
    f.provider.transport = httpx.MockTransport(transport)
    alert(f)
    work(f)
    assert states(f) == {'alice': ('uncertain', None)}
    lost[0] = False
    actor = MessagingActor('company', 'alice', 'shop', 'tracker-chat', 'alice')
    assert asyncio.run(f.store.send_message(actor, 'Two tills are open now.', request_id='answer-1'))['status'] == 'sent'
    # An uncertain answer still fences the next answer, as built.
    lost[0] = True
    assert asyncio.run(f.store.send_message(actor, 'A third till opens.', request_id='answer-2'))['status'] == 'uncertain'
    with pytest.raises(MessagingError, match='Confirm the previous message delivery'):
        asyncio.run(f.store.send_message(actor, 'All tills are open.', request_id='answer-3'))


ANSWER = {'purpose': 'monitoring', 'language': 'en_US', 'parameters': ['Queue watch', 'Six people waiting', '09:00']}
HELD = {'stopped': 'Phone messages for this company are paused.',
    'unreadable': 'Phone messages wait for this company’s daily message limit.'}


@pytest.mark.parametrize('state', ['stopped', 'unreadable'])
def test_the_hosts_stop_and_an_unreadable_record_hold_every_phone_send_of_the_company(tmp_path, state):
    """CHANNELS.md "Spend bound": `stopped` is the host's Stop for every phone send of the company, and an
    unreadable record admits zero sends: security codes, the greeting, an answer in any chat and alerts."""
    from test_phone_participants import NUMBERS, setup_actor, verify
    f = delivery_fixture(tmp_path, verified=('alice',))
    writes(f, 'alice')
    own = MessagingActor('company', 'alice', 'shop', 'alice-own-chat', 'alice')
    tracker = MessagingActor('company', 'alice', 'shop', 'tracker-chat', 'alice')
    for actor in (own, tracker):
        f.store.bind_conversation(actor)
    def unreadable():
        raise OSError('the spend record is missing')
    f.store.spend = (lambda: {**RECORD, 'stopped': True}) if state == 'stopped' else unreadable
    count = len(f.sent)
    async def scenario():
        refused = {}
        for name, send in (
                ('security code', lambda: f.store.request_code(setup_actor('bob'), NUMBERS['bob'], request_id='otp-bob')),
                ('greeting', lambda: f.store.send_hello(setup_actor('alice'), request_id='hello-1')),
                ('answer in another chat', lambda: f.store.send_alert(own, dict(ANSWER), request_id='own-1')),
                ('text in another chat', lambda: f.store.send_message(own, 'Two tills are open.', request_id='own-2')),
                ('answer in the tracking chat', lambda: f.store.send_alert(tracker, dict(ANSWER), request_id='track-1'))):
            with pytest.raises(MessagingError) as held:
                await send()
            refused[name] = str(held.value)
        return refused
    assert asyncio.run(scenario()) == dict.fromkeys(('security code', 'greeting', 'answer in another chat',
        'text in another chat', 'answer in the tracking chat'), HELD[state])
    alert(f)
    work(f, ticks=2)
    assert states(f) == {'alice': ('held', 'stopped' if state == 'stopped' else 'spend_unreadable')}
    assert len(f.sent) == count, 'a provider request left while the host held the company’s phone sends'
    with sqlite3.connect(f.store.path) as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE request_id IN ('otp-bob','hello-1','own-1','own-2','track-1')").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM challenges WHERE member='bob'").fetchone()[0] == 0
    # A recorded authorization opens them again; codes stay outside the unit counters.
    f.store.spend = lambda: dict(RECORD)
    asyncio.run(verify(f.store, f.sent, 'bob'))
    assert len(f.sent) == count + 1 and used(f) == {}


def test_stop_reaches_a_security_code_between_its_claim_and_the_provider_call(tmp_path):
    from test_phone_participants import NUMBERS, setup_actor
    f = delivery_fixture(tmp_path, verified=())
    record, insert = dict(RECORD), f.store._insert
    f.store.spend = lambda: dict(record)
    def insert_then_stop(*args, **kwargs):
        row = insert(*args, **kwargs)
        record['stopped'] = True  # the host sets Stop after the outbox row is written
        return row
    f.store._insert = insert_then_stop
    challenge = asyncio.run(f.store.request_code(setup_actor('alice'), NUMBERS['alice'], request_id='otp-alice'))
    assert f.sent == [] and challenge['status'] == 'cancelled'
    with sqlite3.connect(f.store.path) as db:
        assert db.execute('SELECT kind,status FROM outbox').fetchall() == [('otp', 'cancelled')]


def test_the_application_host_with_no_spend_record_sends_no_security_code(tmp_path, monkeypatch):
    """The host installs its reader for every company. Until the owner's caps are recorded it sends nothing."""
    from fastapi.testclient import TestClient
    from test_phone_participants import HEADERS, app_fixture, sign_in, verify_over_http
    monkeypatch.delenv('ROCOTO_VIDEO_PHONE_SPEND_CONFIG', raising=False)
    app, commission = app_fixture(tmp_path, monkeypatch)
    with TestClient(app) as alice:
        sign_in(alice, app, 'alice')
        host_spend = app.state.workspaces['demo']['messaging'].spend
        assert callable(host_spend)
        selected, service, provider, sent, now = commission()
        service.spend = host_spend  # the host as installed today: no recorded authorization
        body = {'phone': '+34611111111', 'member_id': 'alice', 'collection': 'cafe'}
        refused = alice.post('/api/messaging/verification', json=body, headers=HEADERS)
        assert (refused.status_code, refused.json()) == (400, {'detail': HELD['unreadable']})
        assert sent == [] and alice.get('/api/messaging?member_id=alice&collection=cafe').json()['challenge'] is None
        # The owner's recorded authorization is the one thing that opens the first live send.
        service.spend = lambda: dict(RECORD)
        verify_over_http(alice, sent, 'alice', '+34611111111')
        assert len(sent) == 1
