"""Participants, recipient bindings and contact cards of a Tracking Agent: local stores only."""
from retail_agent_inputs import RetailInputProvider
import asyncio
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import MethodType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_test_support import retail_agent_store as AgentStore
from messaging import MessagingActor
from phone_chat import PhoneChat, PhoneChatError
from test_messaging import fixture, incoming, webhook

ROLES = {'alice': 'operator', 'bob': 'operator', 'carol': 'operator', 'olga': 'owner'}
# The host's spend record for participant phone chat. With none, a Tracking Agent's phone messages stay unsent.
SPEND = {'company_daily_messages': 100, 'agent_daily_messages': 100, 'stopped': False}
NUMBERS = {'alice': '+34611111111', 'bob': '+34622222222', 'carol': '+34633333333', 'olga': '+34644444444'}


def setup_actor(member):
    return MessagingActor('company', member, 'shop', 'setup-' + member, member)


async def verify(store, sent, member):
    actor = setup_actor(member)
    challenge = await store.request_code(actor, NUMBERS[member], request_id='otp-' + member)
    code = sent[-1]['template']['components'][0]['parameters'][0]['text']
    assert store.verify_code(actor, challenge['id'], code)['verified']


def phone_fixture(tmp_path, *, kind='tracker', roles=None):
    store, _, provider, sent, now = fixture(tmp_path / 'messaging')
    store.spend = lambda: dict(SPEND)
    roles = dict(ROLES if roles is None else roles)
    agents = AgentStore(tmp_path / 'agents', input_provider=RetailInputProvider())
    saved = agents.create({'title': 'Queue watch', 'instruction': 'Report queue events.', 'dataset': 'shop',
        'agent_kind': kind, 'input_kind': 'tracker_events'}, ['shop'], owner_principal_id='alice',
        conversation_chat_id='tracker-chat')
    entries = []
    def post(chat_id, entry):
        entries.append((chat_id, json.loads(json.dumps(entry))))
        return True
    def member(principal):
        return {'id': principal, 'display_name': principal.title(), 'role': roles[principal]} if principal in roles else None
    phone = PhoneChat(store, agents=agents, member=member, post=post)
    return SimpleNamespace(phone=phone, store=store, provider=provider, sent=sent, now=now, agents=agents,
        agent=saved, key=saved['id'], entries=entries, roles=roles)


def participant(state, member):
    return next(row for row in state['participants'] if row['member_id'] == member)


def refusal(status, sentence, call, *args, **kwargs):
    with pytest.raises(PhoneChatError) as caught:
        call(*args, **kwargs)
    assert (caught.value.status, str(caught.value)) == (status, sentence)


def test_promotion_gives_a_verified_creator_an_enabled_binding(tmp_path):
    f = phone_fixture(tmp_path)
    asyncio.run(verify(f.store, f.sent, 'alice'))
    state = f.phone.promote(f.agent)
    assert state['agent_id'] == f.key and state['chat_id'] == 'tracker-chat' and state['collection'] == 'shop'
    assert state['policy'] == {'agent_id': f.key, 'participant_phone_delivery': 'on', 'generation': 1,
        'updated_by': 'alice', 'updated_at': f.now[0]}
    assert [(row['member_id'], row['role'], row['added_by'], row['removed_at']) for row in state['participants']] == [
        ('alice', 'creator', 'alice', None)]
    binding = participant(state, 'alice')['binding']
    assert binding == {'agent_id': f.key, 'chat_id': 'tracker-chat', 'member_id': 'alice', 'channel': 'whatsapp',
        'channel_choice': 'automatic', 'purpose': 'tracking_alerts', 'enabled': True, 'generation': 1,
        'address_generation': 1, 'created_at': f.now[0], 'updated_at': f.now[0], 'revoked_at': None}
    assert participant(state, 'alice')['contact_request'] is None and f.entries == []
    # Durable: a new process reads the same records, and a repeat adds nothing.
    again = PhoneChat(f.store, agents=f.agents, member=f.phone.member).promote(f.agent)
    assert again['policy']['generation'] == 1 and again['participants'] == state['participants']


def test_promotion_raises_one_contact_card_in_the_chat_for_an_unverified_creator(tmp_path):
    f = phone_fixture(tmp_path)
    state = f.phone.promote(f.agent)
    binding = participant(state, 'alice')['binding']
    assert (binding['channel'], binding['address_generation'], binding['enabled']) == (None, None, True)
    request = participant(state, 'alice')['contact_request']
    assert re.fullmatch(r'contact-[0-9a-f]{32}', request['id'])
    assert request == {**request, 'agent_id': f.key, 'chat_id': 'tracker-chat', 'member_id': 'alice',
        'requested_by': 'host', 'purpose': 'tracking_alerts', 'status': 'open', 'channel': None,
        'created_at': f.now[0], 'expires_at': f.now[0] + 604800, 'completed_at': None}
    assert [chat for chat, _ in f.entries] == ['tracker-chat']
    card = f.entries[0][1]
    assert card['role'] == 'assistant' and card['contact_request']['id'] == request['id']
    assert card['content'] == 'Alerts from Queue watch: add a verified phone number for Alice.'
    assert card['contact_request']['member_id'] == 'alice' and card['contact_request']['status'] == 'open'
    assert '+34' not in json.dumps(card)
    # An open request is reused: same card, same chat entry.
    assert participant(f.phone.promote(f.agent), 'alice')['contact_request']['id'] == request['id']
    assert len(f.entries) == 1
    # Completing the card resolves the waiting binding in the verification's own commit.
    asyncio.run(verify(f.store, f.sent, 'alice'))
    with sqlite3.connect(f.store.path) as db:
        assert db.execute("SELECT status,channel FROM contact_requests").fetchall() == [('completed', 'whatsapp')]
        assert db.execute('SELECT channel,generation,address_generation FROM recipient_bindings').fetchall() == [('whatsapp', 2, 1)]
    state = f.phone.state(f.key)
    assert participant(state, 'alice')['contact_request'] is None and state['policy']['generation'] == 2
    assert f.entries[-1][1]['contact_request'] == {**card['contact_request'], 'status': 'completed', 'channel': 'whatsapp',
        'completed_at': f.entries[-1][1]['contact_request']['completed_at']}
    # The entry says what the card's state is: the request is done and names the channel.
    assert f.entries[-1][1]['content'] == 'Alice gets alerts from Queue watch on WhatsApp.'


def test_an_open_contact_card_expires_after_seven_days_and_can_be_raised_again(tmp_path):
    f = phone_fixture(tmp_path)
    first = participant(f.phone.promote(f.agent), 'alice')['contact_request']
    f.now[0] += 604800
    assert participant(f.phone.state(f.key), 'alice')['contact_request'] is None
    ended = f.entries[-1][1]
    assert (ended['contact_request']['id'], ended['contact_request']['status']) == (first['id'], 'expired')
    assert ended['content'] == 'The phone number request for Alice ended after seven days. Ask again from “Alerts on your phone”.'
    raised = f.phone.request_contact(f.key, 'alice', by='alice')
    assert raised['status'] == 'open' and raised['id'] != first['id'] and raised['requested_by'] == 'alice'
    assert f.entries[-1][1]['content'] == 'Alerts from Queue watch: add a verified phone number for Alice.'
    assert f.phone.request_contact(f.key, 'alice', by='alice')['id'] == raised['id']
    asyncio.run(verify(f.store, f.sent, 'alice'))
    refusal(409, 'This participant’s number is verified.', f.phone.request_contact, f.key, 'alice', by='alice')


def test_a_contact_cards_chat_entry_says_what_its_state_is(tmp_path):
    from phone_chat import card_text
    f = phone_fixture(tmp_path)
    asyncio.run(verify(f.store, f.sent, 'alice'))
    f.phone.promote(f.agent)
    f.phone.add_participant(f.key, 'bob', by='alice')
    opened = f.entries[-1][1]
    assert (opened['contact_request']['member_id'], opened['contact_request']['status']) == ('bob', 'open')
    assert opened['content'] == 'Alerts from Queue watch: add a verified phone number for Bob.'
    # The creator removes him while his card is open: the same entry says the request ended.
    f.phone.remove_participant(f.key, 'bob', by='alice')
    ended = f.entries[-1][1]
    assert (ended['contact_request']['id'], ended['contact_request']['status']) == (opened['contact_request']['id'], 'cancelled')
    assert ended['content'] == 'The phone number request for Bob ended.'
    assert [card_text('completed', 'Queue watch', 'Bob', channel) for channel in ('viber', 'sms', 'whatsapp', None)] == [
        'Bob gets alerts from Queue watch on Viber.', 'Bob gets alerts from Queue watch by SMS.',
        'Bob gets alerts from Queue watch on WhatsApp.', 'Bob gets alerts from Queue watch on their phone.']
    assert '+34' not in json.dumps(f.entries)


def test_an_action_agent_holds_none_of_the_phone_records(tmp_path):
    f = phone_fixture(tmp_path, kind='action')
    refusal(404, 'Choose an agent with participant messaging from your current workspace.', f.phone.promote, f.agent)
    refusal(404, 'Choose an agent with participant messaging from your current workspace.', f.phone.state, 'agent-unknown')
    assert not f.store.path.exists()


def test_promotion_succeeds_whatever_the_phone_state_is(tmp_path):
    from monitoring_routes import promotion
    f = phone_fixture(tmp_path)
    class Broken:
        def promote(self, record):
            raise sqlite3.OperationalError('disk I/O error')
    harness = SimpleNamespace(children={'chat': {'id': 'chat', 'dataset': 'shop', 'owner_principal_id': 'alice', 'messages': []}},
        active={}, phone_chat=Broken(), _save_child=lambda child: None)
    definition = {'title': 'Shelf watch', 'instruction': 'Report shelf events.', 'dataset': 'shop', 'input_kind': 'tracker_events'}
    selected = {'agent': harness, 'agents': f.agents, 'index': SimpleNamespace(status=lambda: {'datasets': ['shop']}),
        'monitoring': SimpleNamespace(registered_datasets=lambda owner: [])}
    record, child = promotion(selected, 'chat', 'tracker', definition)
    assert record['agent_kind'] == 'tracker' and child['agent_id'] == record['id']
    # The same host operation creates the default records when the store is healthy.
    harness.phone_chat = f.phone
    harness.children['other'] = {'id': 'other', 'dataset': 'shop', 'owner_principal_id': 'alice', 'messages': []}
    record, child = promotion(selected, 'other', 'tracker', {**definition, 'title': 'Door watch'})
    with sqlite3.connect(f.store.path) as db:
        assert db.execute('SELECT agent,chat,delivery,generation FROM phone_policies').fetchall() == [(record['id'], 'other', 'on', 1)]
        assert db.execute('SELECT member,role FROM phone_participants').fetchall() == [('alice', 'creator')]
        assert db.execute("SELECT member,status FROM contact_requests").fetchall() == [('alice', 'open')]
    # An Action promotion writes none of them.
    harness.children['third'] = {'id': 'third', 'dataset': 'shop', 'owner_principal_id': 'alice', 'messages': []}
    promotion(selected, 'third', 'action', {**definition, 'title': 'Notify supplier'})
    with sqlite3.connect(f.store.path) as db:
        assert db.execute('SELECT count(*) FROM phone_policies').fetchone()[0] == 1


def test_the_creator_or_an_owner_adds_and_removes_company_members(tmp_path):
    f = phone_fixture(tmp_path)
    asyncio.run(verify(f.store, f.sent, 'alice'))
    asyncio.run(verify(f.store, f.sent, 'carol'))
    f.phone.promote(f.agent)
    refusal(403, 'This agent’s creator manages its participants.', f.phone.add_participant, f.key, 'carol', by='bob')
    refusal(400, 'Choose a member of this company.', f.phone.add_participant, f.key, 'zed', by='alice')
    refusal(409, 'Refresh the phone alerts and repeat your change.', f.phone.add_participant, f.key, 'bob', by='alice', expected_generation=7)
    state = f.phone.add_participant(f.key, 'bob', by='alice', expected_generation=1)
    assert state['policy']['generation'] == 2 and state['policy']['updated_by'] == 'alice'
    bob = participant(state, 'bob')
    assert (bob['role'], bob['added_by'], bob['binding']['enabled'], bob['binding']['channel']) == ('added', 'alice', True, None)
    assert bob['contact_request']['requested_by'] == 'alice' and f.entries[-1][1]['contact_request']['member_id'] == 'bob'
    # Adding a current participant answers with the records as they are.
    assert f.phone.add_participant(f.key, 'bob', by='alice', expected_generation=99)['policy']['generation'] == 2
    # A company owner manages participants too; a verified member needs no card.
    state = f.phone.add_participant(f.key, 'carol', by='olga', company_owner=True)
    assert participant(state, 'carol')['binding']['channel'] == 'whatsapp' and participant(state, 'carol')['contact_request'] is None
    assert [row['member_id'] for row in state['participants']] == ['alice', 'bob', 'carol']
    refusal(409, 'The creator stays a participant of this agent.', f.phone.remove_participant, f.key, 'alice', by='alice')
    refusal(403, 'This agent’s creator manages its participants.', f.phone.remove_participant, f.key, 'carol', by='bob')
    refusal(400, 'Add this member as a participant first.', f.phone.remove_participant, f.key, 'olga', by='alice')
    refusal(409, 'Refresh the phone alerts and repeat your change.', f.phone.remove_participant, f.key, 'bob', by='alice', expected_generation=1)
    # An added participant removes themself; removal revokes the binding and cancels the open card.
    state = f.phone.remove_participant(f.key, 'bob', by='bob')
    assert [row['member_id'] for row in state['participants']] == ['alice', 'carol']
    with sqlite3.connect(f.store.path) as db:
        assert db.execute("SELECT removed IS NOT NULL FROM phone_participants WHERE member='bob'").fetchone()[0] == 1
        assert db.execute("SELECT revoked IS NOT NULL FROM recipient_bindings WHERE member='bob'").fetchone()[0] == 1
        assert db.execute("SELECT status FROM contact_requests WHERE member='bob'").fetchall() == [('cancelled',)]
    assert f.entries[-1][1]['contact_request']['status'] == 'cancelled'
    # Adding the member again restores one row per agent and member.
    state = f.phone.add_participant(f.key, 'bob', by='alice')
    assert participant(state, 'bob')['binding']['generation'] == 2 and participant(state, 'bob')['binding']['revoked_at'] is None
    with sqlite3.connect(f.store.path) as db:
        assert db.execute("SELECT count(*) FROM phone_participants WHERE member='bob'").fetchone()[0] == 1


def test_an_agent_holds_at_most_twenty_participants(tmp_path):
    roles = {**ROLES, **{f'member-{index}': 'operator' for index in range(20)}}
    f = phone_fixture(tmp_path, roles=roles)
    f.phone.promote(f.agent)
    for index in range(19):
        f.phone.add_participant(f.key, f'member-{index}', by='alice')
    refusal(409, 'This agent holds its full 20 participants.', f.phone.add_participant, f.key, 'member-19', by='alice')
    assert len(f.phone.state(f.key)['participants']) == 20


def test_each_participant_owns_their_switch_and_channel(tmp_path):
    f = phone_fixture(tmp_path)
    for member in ('alice', 'bob'):
        asyncio.run(verify(f.store, f.sent, member))
    f.phone.promote(f.agent)
    generation = f.phone.add_participant(f.key, 'bob', by='alice')['policy']['generation']
    refusal(400, 'Send the phone alert fields.', f.phone.update_binding, f.key, 'bob', by='bob')
    refusal(400, 'Send the phone alert fields.', f.phone.update_binding, f.key, 'bob', by='bob', channel='fax')
    refusal(400, 'Send the phone alert fields.', f.phone.update_binding, f.key, 'bob', by='bob', enabled='yes')
    refusal(400, 'Add this member as a participant first.', f.phone.update_binding, f.key, 'carol', by='carol', enabled=False)
    refusal(403, 'This agent’s creator manages its participants.', f.phone.update_binding, f.key, 'alice', by='bob', enabled=False)
    refusal(409, 'Refresh the phone alerts and repeat your change.', f.phone.update_binding, f.key, 'bob', by='bob', enabled=False, expected_generation=generation + 5)
    # The creator switches another participant's alerts off; switching on and the channel are the participant's own.
    state = f.phone.update_binding(f.key, 'bob', by='alice', enabled=False, expected_generation=generation)
    assert (participant(state, 'bob')['binding']['enabled'], participant(state, 'bob')['binding']['generation']) == (False, 2)
    assert state['policy']['generation'] == generation + 1
    for change in ({'enabled': True}, {'channel': 'whatsapp'}, {'channel': 'automatic'}):
        refusal(403, 'Each participant chooses their own channel and switches their own alerts on.',
            f.phone.update_binding, f.key, 'bob', by='alice', **change)
    state = f.phone.update_binding(f.key, 'bob', by='bob', enabled=True, channel='whatsapp')
    binding = participant(state, 'bob')['binding']
    assert (binding['enabled'], binding['channel'], binding['channel_choice'], binding['generation']) == (True, 'whatsapp', 'picked', 3)
    refusal(409, 'This channel is being prepared.', f.phone.update_binding, f.key, 'bob', by='bob', channel='sms')
    state = f.phone.update_binding(f.key, 'bob', by='bob', channel='automatic')
    assert participant(state, 'bob')['binding']['channel_choice'] == 'automatic'
    # An unchanged request leaves both generations as they are.
    assert f.phone.update_binding(f.key, 'bob', by='bob', channel='automatic')['policy'] == state['policy']
    f.phone.add_participant(f.key, 'carol', by='alice')
    refusal(409, 'Verify a number on this channel to choose it.', f.phone.update_binding, f.key, 'carol', by='carol', channel='whatsapp')


def test_the_creator_pauses_and_resumes_participant_phone_delivery(tmp_path):
    f = phone_fixture(tmp_path)
    f.phone.promote(f.agent)
    refusal(400, 'Send the phone alert fields.', f.phone.set_delivery, f.key, 'off', by='alice')
    refusal(403, 'This agent’s creator manages its participants.', f.phone.set_delivery, f.key, 'paused', by='bob')
    state = f.phone.set_delivery(f.key, 'paused', by='alice', expected_generation=1)
    assert state['policy']['participant_phone_delivery'] == 'paused' and state['policy']['generation'] == 2
    refusal(409, 'Refresh the phone alerts and repeat your change.', f.phone.set_delivery, f.key, 'on', by='alice', expected_generation=1)
    assert f.phone.set_delivery(f.key, 'on', by='olga', company_owner=True)['policy']['participant_phone_delivery'] == 'on'


def test_the_gate_recipient_is_a_current_participant_with_a_verified_number(tmp_path):
    f = phone_fixture(tmp_path)
    for member in ('alice', 'bob'):
        asyncio.run(verify(f.store, f.sent, member))
    f.phone.promote(f.agent)
    f.phone.add_participant(f.key, 'bob', by='alice')
    outside = 'This agent’s messages reach its own participants.'
    recipient = f.phone.recipient(f.key, 'tracker-chat', 'bob')
    assert recipient == {**recipient, 'agent_id': f.key, 'chat_id': 'tracker-chat', 'member_id': 'bob', 'channel': 'whatsapp'}
    assert f.phone.recipient(f.key, 'tracker-chat', 'bob', channel='whatsapp')['channel'] == 'whatsapp'
    refusal(403, outside, f.phone.recipient, f.key, 'tracker-chat', 'carol')
    refusal(403, outside, f.phone.recipient, f.key, 'another-chat', 'bob')
    refusal(409, 'Verify a number on this channel to choose it.', f.phone.recipient, f.key, 'tracker-chat', 'bob', channel='sms')
    # A turn with no human sender uses the creator's enabled binding under the agent's policy.
    assert f.phone.recipient(f.key, 'tracker-chat', 'alice', host=True)['member_id'] == 'alice'
    refusal(403, outside, f.phone.recipient, f.key, 'tracker-chat', 'bob', host=True)
    f.phone.update_binding(f.key, 'alice', by='alice', enabled=False)
    refusal(409, 'This participant’s phone alerts are switched off.', f.phone.recipient, f.key, 'tracker-chat', 'alice', host=True)
    assert f.phone.recipient(f.key, 'tracker-chat', 'alice')['member_id'] == 'alice'  # answering the person who wrote
    f.phone.update_binding(f.key, 'alice', by='alice', enabled=True)
    f.phone.set_delivery(f.key, 'paused', by='alice')
    refusal(409, 'This agent’s phone alerts are paused.', f.phone.recipient, f.key, 'tracker-chat', 'alice', host=True)
    # Removal and leaving the company both end the recipient.
    f.phone.remove_participant(f.key, 'bob', by='alice')
    refusal(403, outside, f.phone.recipient, f.key, 'tracker-chat', 'bob')
    del f.roles['alice']
    refusal(403, outside, f.phone.recipient, f.key, 'tracker-chat', 'alice')


def test_an_unavailable_membership_read_refuses_sends_and_keeps_the_participants(tmp_path):
    """The host's member lookup answers None for a busy store too, so a missing answer removes nobody."""
    f = phone_fixture(tmp_path)
    asyncio.run(verify(f.store, f.sent, 'bob'))
    f.phone.promote(f.agent)
    before = f.phone.add_participant(f.key, 'bob', by='alice')
    roles = dict(f.roles)
    f.roles.clear()
    with pytest.raises(PhoneChatError):
        f.phone.recipient(f.key, 'tracker-chat', 'bob')
    assert not f.phone.participant_chat('tracker-chat', 'bob')
    during = f.phone.state(f.key)  # a read of the phone block while the lookup is unavailable
    assert [(row['member_id'], row['member_current']) for row in during['participants']] == [('alice', False), ('bob', False)]
    f.roles.update(roles)
    after = f.phone.state(f.key)
    assert [row['member_id'] for row in after['participants']] == ['alice', 'bob']
    assert participant(after, 'bob')['binding'] == participant(before, 'bob')['binding'] and after['policy'] == before['policy']
    assert f.phone.recipient(f.key, 'tracker-chat', 'bob')['member_id'] == 'bob'


def test_a_participant_reply_continues_the_tracking_chat_and_any_other_reply_goes_to_setup(tmp_path):
    from dataclasses import replace
    from messaging_provider import Template
    async def scenario():
        f = phone_fixture(tmp_path)
        f.provider.config = replace(f.provider.config, utility_templates=(Template(
            'rocoto_monitoring_alert_v1', 'en_US', 'UTILITY', 'APPROVED', 'Alert for {{1}}: {{2}}. Recorded {{3}}.'),))
        for member in ('alice', 'bob'):
            await verify(f.store, f.sent, member)
        f.phone.promote(f.agent)
        f.phone.add_participant(f.key, 'bob', by='alice')
        actor = MessagingActor('company', 'bob', 'shop', 'tracker-chat', 'bob')
        f.store.bind_conversation(actor)
        alert = {'purpose': 'monitoring', 'language': 'en_US', 'parameters': ['Checkout', 'Six people waiting', '09:00']}
        outgoing = await f.store.send_alert(actor, alert, request_id='alert-1')
        assert outgoing['status'] == 'sent' and outgoing['chat_id'] == 'tracker-chat'
        reply = incoming(f.now, id='wamid.bob-1', context=outgoing['provider_message_id'], sender='34622222222')
        assert f.store.handle_webhook(*webhook(f.provider, [reply]))['accepted'] == 1
        pending = f.store.pending_replies()
        assert [(row['member_id'], row['chat_id']) for row in pending] == [('bob', 'tracker-chat')]
        assert f.phone.participant_chat('tracker-chat', 'bob') and not f.phone.participant_chat('tracker-chat', 'carol')
        # A removed participant's reply to that same alert continues their own setup chat.
        f.phone.remove_participant(f.key, 'bob', by='alice')
        assert [(row['member_id'], row['chat_id']) for row in f.store.pending_replies()] == [('bob', 'setup-bob')]
        later = incoming(f.now, id='wamid.bob-2', context=outgoing['provider_message_id'], sender='34622222222')
        assert f.store.handle_webhook(*webhook(f.provider, [later]))['accepted'] == 1
        assert sorted((row['provider_event_id'], row['chat_id']) for row in f.store.pending_replies()) == [
            ('wamid.bob-1', 'setup-bob'), ('wamid.bob-2', 'setup-bob')]
        assert not f.phone.participant_chat('tracker-chat', 'bob')
    asyncio.run(scenario())


def test_ordinary_turn_admission_takes_a_participant_and_refuses_everyone_else(tmp_path):
    from agent_test_support import root_agent
    f = phone_fixture(tmp_path / 'phone')
    f.phone.promote(f.agent)
    f.phone.add_participant(f.key, 'bob', by='alice')
    agent = root_agent(tmp_path / 'native')
    agent.phone_chat = f.phone
    agent.children['tracker-chat'] = {'id': 'tracker-chat', 'kind': 'agent_conversation', 'agent_id': f.key,
        'agent_kind': 'tracker', 'owner_principal_id': 'alice', 'dataset': 'shop', 'title': 'Queue watch', 'messages': []}
    observed = []
    async def native(self, message, chat_id, *args, **kwargs):
        observed.append(json.loads((self.chat_dir / (chat_id + '.json')).read_text())['messages'][-2])
        yield {'type': 'done', 'status': 'completed'}
    agent._turn = MethodType(native, agent)
    async def run():
        reply = {'principal_id': 'bob', 'display_name': 'Bob', 'channel': 'whatsapp', 'admission_key': 'whatsapp:one', 'provider_event_id': 'wamid.one'}
        first = [e async for e in agent.turn('Is the queue shorter now?', 'tracker-chat', 'shop', sender=reply)]
        again = [e async for e in agent.turn('Is the queue shorter now?', 'tracker-chat', 'shop', sender=reply)]
        outsider = [e async for e in agent.turn('Let me in', 'tracker-chat', 'shop', sender={'principal_id': 'carol', 'channel': 'whatsapp', 'admission_key': 'whatsapp:two'})]
        assert first[-1] == {'type': 'done', 'status': 'completed'} and again == [{'type': 'done', 'status': 'already_admitted'}]
        assert outsider[0]['type'] == 'error'
        f.phone.remove_participant(f.key, 'bob', by='alice')
        removed = [e async for e in agent.turn('Still here', 'tracker-chat', 'shop', sender={**reply, 'admission_key': 'whatsapp:three'})]
        assert removed[0]['type'] == 'error'
        # Participation covers this one conversation: the creator's other chats stay the creator's.
        f.phone.add_participant(f.key, 'bob', by='alice')
        private = [e async for e in agent.turn('Hello', 'chat', 'cafe', sender={**reply, 'admission_key': 'whatsapp:four'})]
        assert private[0]['type'] == 'error'
        # The app passes a participant as the sender of a typed message: the same admission on the web channel.
        typed = [e async for e in agent.turn('Count till two as well.', 'tracker-chat', 'shop',
            sender={'principal_id': 'bob', 'display_name': 'Bob', 'channel': 'web'})]
        assert typed[-1] == {'type': 'done', 'status': 'completed'}
        stranger = [e async for e in agent.turn('Let me in', 'tracker-chat', 'shop',
            sender={'principal_id': 'carol', 'display_name': 'Carol', 'channel': 'web'})]
        assert stranger[0]['type'] == 'error'
    asyncio.run(run())
    assert len(observed) == 2 and observed[0]['sender_principal_id'] == 'bob'
    assert observed[0]['channel']['channel'] == 'whatsapp' and observed[0]['channel']['admission_key'] == 'whatsapp:one'
    assert (observed[1]['content'], observed[1]['sender_principal_id'], observed[1]['channel']) == (
        'Count till two as well.', 'bob', {'principal_id': 'bob', 'display_name': 'Bob', 'channel': 'web'})


def test_the_real_gate_sends_a_tracker_message_to_its_own_participant_and_the_reply_returns(tmp_path):
    """The dispatch gate, the participant store and the messaging store together, on a local transport."""
    from dataclasses import replace
    from agent_bridge import CodexVideoAgent
    from messaging_provider import Template
    class Index:
        def status(self): return {'ready': True, 'datasets': ['shop']}
    async def scenario():
        f = phone_fixture(tmp_path / 'phone')
        f.provider.config = replace(f.provider.config, utility_templates=(Template(
            'rocoto_monitoring_alert_v1', 'en_US', 'UTILITY', 'APPROVED', 'Alert for {{1}}: {{2}}. Recorded {{3}}.'),))
        for person in ('alice', 'bob', 'carol'):
            await verify(f.store, f.sent, person)
        f.phone.promote(f.agent)
        f.phone.add_participant(f.key, 'bob', by='alice')
        agent = CodexVideoAgent(tmp_path / 'native', Index(), lambda: {})
        agent.messaging_store, agent.phone_chat, agent.automation_store = f.store, f.phone, f.agents
        agent.messaging_member = f.phone.member
        lane = ['live']
        agent.phone_run_lane = lambda chat_id: lane[0]  # the host names the lane of the run active in a conversation
        from retail_host import RetailWorkspace
        agent.vertical=RetailWorkspace(None,{'agent':agent})
        agent.children['tracker-chat'] = {'id': 'tracker-chat', 'kind': 'agent_conversation', 'agent_id': f.key,
            'agent_kind': 'tracker', 'owner_principal_id': 'alice', 'dataset': 'shop', 'messages': []}
        alert = {'purpose': 'monitoring', 'language': 'en_US', 'parameters': ['Checkout', 'Six people waiting', '09:00']}
        async def turn(generation, sender, **record):
            agent.children['tracker-chat'].pop('active_run_id', None)
            agent.children['tracker-chat'].update(record)
            agent.active['tracker-chat'] = {'cancel': asyncio.Event(), 'generation': generation}
            agent._turn_senders = {'tracker-chat': sender}
            count = len(f.sent)
            content, _, success = await agent._scoped_tool('send_alert', alert, 'shop', 'tracker-chat')
            return success, json.loads(content[0]['text']) if success else content[0]['text'], f.sent[count:]
        # A participant wrote from WhatsApp; the agent's answer reaches that same participant there.
        success, receipt, wire = await turn('turn-1', {'principal_id': 'bob', 'channel': 'whatsapp'})
        assert success and receipt['status'] == 'sent' and receipt['channel'] == 'whatsapp' and receipt['chat_id'] == 'tracker-chat'
        assert [item['to'] for item in wire] == ['34622222222']
        with sqlite3.connect(f.store.path) as db:
            assert db.execute("SELECT member,chat,channel,address FROM outbox WHERE kind='utility'").fetchall() == [
                ('bob', 'tracker-chat', 'whatsapp', '+34622222222')]
        reply = incoming(f.now, id='wamid.bob-reply', context=receipt['provider_message_id'], sender='34622222222')
        assert f.store.handle_webhook(*webhook(f.provider, [reply]))['accepted'] == 1
        assert [(row['member_id'], row['chat_id'], row['channel']) for row in f.store.pending_replies()] == [('bob', 'tracker-chat', 'whatsapp')]
        # A company member outside the participants is refused ahead of any binding or provider call.
        success, refusal_text, wire = await turn('turn-2', {'principal_id': 'carol', 'channel': 'whatsapp'})
        assert not success and wire == [] and 'its own participants' in refusal_text
        with sqlite3.connect(f.store.path) as db:
            assert db.execute("SELECT count(*) FROM bindings WHERE member='carol' AND chat='tracker-chat'").fetchone()[0] == 0
        # A scheduled or event turn has no human sender: it reaches the creator on the creator's binding.
        success, receipt, wire = await turn('turn-3', {'principal_id': 'alice', 'channel': 'web'}, active_run_id='run-1')
        assert success and [item['to'] for item in wire] == ['34611111111']
        # A run reading replayed observations reaches zero phones.
        lane[0] = 'replay'
        success, refusal_text, wire = await turn('turn-3b', {'principal_id': 'alice', 'channel': 'web'}, active_run_id='run-1b')
        assert not success and wire == [] and refusal_text == 'This run reads replayed observations. Phone alerts follow live observations.'
        lane[0] = 'live'
        f.phone.set_delivery(f.key, 'paused', by='alice')
        success, refusal_text, wire = await turn('turn-4', {'principal_id': 'alice', 'channel': 'web'}, active_run_id='run-2')
        assert not success and wire == [] and refusal_text == 'This agent’s phone alerts are paused.'
        # Removal ends the reach at once.
        f.phone.remove_participant(f.key, 'bob', by='alice')
        success, refusal_text, wire = await turn('turn-5', {'principal_id': 'bob', 'channel': 'whatsapp'})
        assert not success and wire == [] and 'its own participants' in refusal_text
    asyncio.run(scenario())


# The application host: real routes, authority callbacks and the reply tick ----------

HEADERS = {'X-Video-Demo': '1'}
TRACKER = {'agent_kind': 'tracker', 'definition': {'title': 'Queue watch', 'instruction': 'Report queue events.',
    'dataset': 'cafe', 'input_kind': 'tracker_events', 'mode': 'scheduled'}}


def app_fixture(tmp_path, monkeypatch):
    from test_organization_chats import commissioned
    from test_server_access import fixture_app
    app = fixture_app(tmp_path, monkeypatch)
    return app, lambda: commissioned(app, tmp_path)


def sign_in(client, app, person, owner=False):
    code = app.state.access.issue('demo', owner=owner, principal_id=person, display_name=person.title())
    assert client.post('/api/session', json={'code': code}, headers=HEADERS).status_code == 200


def verify_over_http(client, sent, member, phone):
    challenge = client.post('/api/messaging/verification', json={'phone': phone, 'member_id': member, 'collection': 'cafe'}, headers=HEADERS)
    assert challenge.status_code == 200, challenge.text
    code = sent[-1]['template']['components'][0]['parameters'][0]['text']
    confirmed = client.post('/api/messaging/verification/confirm', json={'challenge_id': challenge.json()['id'], 'code': code,
        'member_id': member, 'collection': 'cafe'}, headers=HEADERS)
    assert confirmed.status_code == 200 and confirmed.json()['verified'] is True, confirmed.text


def promote_chat(client, selected, owner, chat_id):
    selected['agent'].children[chat_id] = {'id': chat_id, 'dataset': 'cafe', 'owner_principal_id': owner,
        'messages': [{'role': 'user', 'content': 'Watch the queue.'}], 'running': False}
    promoted = client.post(f'/api/chats/{chat_id}/promote-agent', json=TRACKER, headers=HEADERS)
    assert promoted.status_code == 200, promoted.text
    return promoted.json()['agent']['id']


def test_http_promotion_binds_a_verified_creator_and_cards_an_unverified_one(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    app, commission = app_fixture(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        sign_in(alice, app, 'alice'); sign_in(bob, app, 'bob')
        selected, service, provider, sent, now = commission()
        verify_over_http(alice, sent, 'alice', '+34611111111')
        key = promote_chat(alice, selected, 'alice', 'alice-track')
        state = selected['phone'].state(key)
        assert state['policy']['participant_phone_delivery'] == 'on' and state['creator_id'] == 'alice'
        assert participant(state, 'alice')['binding']['channel'] == 'whatsapp'
        assert participant(state, 'alice')['contact_request'] is None
        assert [m for m in selected['agent'].children['alice-track']['messages'] if m.get('contact_request')] == []
        # An unverified creator gets the contact card in that same chat; promotion still succeeds.
        other = promote_chat(bob, selected, 'bob', 'bob-track')
        cards = [m for m in selected['agent'].children['bob-track']['messages'] if m.get('contact_request')]
        assert len(cards) == 1 and cards[0]['contact_request']['status'] == 'open' and cards[0]['contact_request']['member_id'] == 'bob'
        assert selected['agent'].children['bob-track']['messages'][0] == {'role': 'user', 'content': 'Watch the queue.'}
        assert participant(selected['phone'].state(other), 'bob')['binding']['channel'] is None
        # Completing the card through the built verification routes resolves the binding and the chat entry.
        verify_over_http(bob, sent, 'bob', '+34622222222')
        cards = [m for m in selected['agent'].children['bob-track']['messages'] if m.get('contact_request')]
        assert len(cards) == 1 and cards[0]['contact_request']['status'] == 'completed'
        assert cards[0]['contact_request']['channel'] == 'whatsapp' and '+34622222222' not in json.dumps(cards)
        state = selected['phone'].state(other)
        assert participant(state, 'bob')['binding']['channel'] == 'whatsapp' and participant(state, 'bob')['contact_request'] is None


def test_signed_reply_from_an_added_participant_resumes_the_tracking_chat_once(tmp_path, monkeypatch):
    import time
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from messaging_provider import Template
    app, commission = app_fixture(tmp_path, monkeypatch)
    observed = []
    with TestClient(app) as alice, TestClient(app) as bob:
        sign_in(alice, app, 'alice'); sign_in(bob, app, 'bob')
        selected, service, provider, sent, now = commission()
        service.spend = lambda: dict(SPEND)
        provider.config = replace(provider.config, utility_templates=(Template(
            'rocoto_monitoring_alert_v1', 'en_US', 'UTILITY', 'APPROVED', 'Alert for {{1}}: {{2}}. Recorded {{3}}.'),))
        verify_over_http(bob, sent, 'bob', '+34622222222')
        setup_chat = bob.get('/api/messaging?collection=cafe').json()['setup_chat_id']
        key = promote_chat(alice, selected, 'alice', 'track')
        agent = selected['agent']
        tracker = MessagingActor(service.company_id, 'bob', 'cafe', 'track', 'bob')
        # Before the creator adds him, the host authority keeps the tracking chat closed to this member.
        with pytest.raises(ValueError):
            service.bind_conversation(tracker)
        selected['phone'].add_participant(key, 'bob', by='alice')
        service.bind_conversation(tracker)
        alert = {'purpose': 'monitoring', 'language': 'en_US', 'parameters': ['Checkout', 'Six people waiting', '09:00']}
        outgoing = asyncio.run(service.send_alert(tracker, alert, request_id='alert-1'))
        assert outgoing['status'] == 'sent'
        async def turn(self, message, identity, collection, *, sender, model_context=""):
            assert "Host-authenticated signal origin" in model_context
            observed.append((identity, collection, sender))
            record = self.children[identity]
            record['messages'].append({'role': 'user', 'content': message, 'sender_principal_id': sender['principal_id'], 'channel': sender})
            self._save_child(record)
            yield {'type': 'done', 'status': 'completed'}
        agent.turn = MethodType(turn, agent)
        agent._input = lambda *args: []
        def post(identifier):
            raw, signature = webhook(provider, [incoming(now, id=identifier, context=outgoing['provider_message_id'], sender='34622222222')])
            return bob.post('/api/messaging/webhook/demo', content=raw, headers={'x-hub-signature-256': signature}).json()
        def wait(count):
            deadline = time.monotonic() + 4
            while len(observed) < count and time.monotonic() < deadline:
                time.sleep(.02)
        assert post('wamid.bob-1')['accepted'] == 1
        wait(1)
        assert len(observed) == 1 and observed[0][0:2] == ('track', 'cafe')
        assert observed[0][2]['principal_id'] == 'bob' and observed[0][2]['channel'] == 'whatsapp'
        assert observed[0][2]['display_name'] == 'Bob' and observed[0][2]['provider_event_id'] == 'wamid.bob-1'
        assert post('wamid.bob-1')['duplicates'] == 1
        # Participation covers this conversation while the participant's own location
        # assignments cover its scope: the host reads that authority for the participant.
        import scope_host
        original, asked, covered = scope_host.history_allowed, [], [False]
        def assignments(selected_workspace, record, principal):
            asked.append((record['id'], principal))
            return covered[0] and original(selected_workspace, record, principal)
        monkeypatch.setattr(scope_host, 'history_allowed', assignments)
        blocked = post('wamid.bob-scope')
        assert (blocked['accepted'], blocked['blocked']) == (0, 1)
        with pytest.raises(ValueError):
            service.bind_conversation(tracker)
        time.sleep(1.1)
        assert ('track', 'bob') in asked and len(observed) == 1
        covered[0] = True
        # Once the creator removes him, his reply to that same alert continues his own setup chat.
        selected['phone'].remove_participant(key, 'bob', by='alice')
        assert post('wamid.bob-2')['accepted'] == 1
        wait(2)
        assert len(observed) == 2 and observed[1][0] == setup_chat
        time.sleep(1.1)
        assert [item[0] for item in observed] == ['track', setup_chat]


def test_participant_policy_uses_custom_declared_authority(tmp_path):
    from background_agents import AgentStore as CommonStore
    f=phone_fixture(tmp_path)
    agents=CommonStore(tmp_path/'custom-agents',agent_classes=[
        {'kind':'order_observer','authority':'participant_reach'},
        {'kind':'order_fulfillment','authority':'granted_effects'}])
    def create(kind):
        return agents.create({'title':'Order updates','instruction':'Summarize changes.','dataset':'shop','agent_kind':kind},
            ['shop'],owner_principal_id='alice')
    observer=create('order_observer');actor=create('order_fulfillment')
    phone=PhoneChat(f.store,agents=agents,member=f.phone.member)
    policy=phone.promote(observer)
    assert policy['agent_id']==observer['id'] and participant(policy,'alice')['role']=='creator'
    with pytest.raises(PhoneChatError):phone.promote(actor)
    del agents.agent_classes['order_observer']
    with pytest.raises(PhoneChatError):phone.promote(observer)
