"""The phone block of a Tracking Agent over HTTP (CHANNELS.md, "HTTP interface"): zero remote calls."""
from dataclasses import replace
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from messaging_provider import Template
from phone_spend import resets_at
from test_phone_participants import HEADERS, app_fixture, promote_chat, sign_in, verify_over_http

NOT_FOUND = {'detail': 'Choose a Tracking Agent from your current workspace.'}
FIELDS = {'detail': 'Send the phone alert fields.'}
SPEND = {'company_daily_messages': 20, 'agent_daily_messages': 10, 'stopped': False}
CHANNELS = [{'channel': 'viber', 'label': 'Viber', 'ready': False, 'reason': 'provider_setup_required', 'sender': None},
    {'channel': 'sms', 'label': 'SMS', 'ready': False, 'reason': 'provider_setup_required', 'sender': None},
    {'channel': 'whatsapp', 'label': 'WhatsApp', 'ready': True, 'reason': None, 'sender': '+34600000000'}]


def people(app, clients):
    """alice creates the agent, bob and carol are operators, olga owns the company."""
    for client, person in zip(clients, ('alice', 'bob', 'carol', 'olga')):
        sign_in(client, app, person, owner=person == 'olga')


def post(client, key, path, body, **options):
    return client.post(f'/api/agents/{key}/phone{path}', json=body, headers=HEADERS, **options)


def block(client, key):
    response = client.get(f'/api/agents/{key}/phone')
    assert response.status_code == 200, response.text
    return response.json()


def row(value, member):
    return next(item for item in value['participants'] if item['member_id'] == member)


def test_the_phone_block_for_the_creator_a_participant_the_owner_and_everyone_else(tmp_path, monkeypatch):
    app, commission = app_fixture(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as carol, TestClient(app) as olga, TestClient(app) as anonymous:
        people(app, (alice, bob, carol, olga))
        selected, service, provider, sent, now = commission()
        service.spend = lambda: dict(SPEND)
        verify_over_http(alice, sent, 'alice', '+34611111111')
        key = promote_chat(alice, selected, 'alice', 'track')
        selected['phone'].add_participant(key, 'bob', by='alice')
        value = block(alice, key)
        assert set(value) == {'agent_id', 'chat_id', 'collection', 'generation', 'delivery', 'can_manage', 'read_only',
            'channels', 'participants', 'available_members', 'spend', 'history'}
        assert value['history'] == 'open'
        assert (value['agent_id'], value['chat_id'], value['collection'], value['delivery']) == (key, 'track', 'cafe', 'on')
        assert (value['generation'], value['can_manage'], value['read_only']) == (2, True, False)
        assert value['channels'] == CHANNELS
        assert value['participants'][0] == {'member_id': 'alice', 'name': 'Alice', 'role': 'creator', 'is_self': True,
            'channel': 'whatsapp', 'channel_choice': 'automatic', 'verified_channels': ['whatsapp'],
            'address_masked': '+346••••••11', 'verification': 'verified', 'enabled': True, 'last_delivery': None,
            'contact_request': None}
        request = value['participants'][1].pop('contact_request')
        assert value['participants'][1] == {'member_id': 'bob', 'name': 'Bob', 'role': 'added', 'is_self': False,
            'channel': None, 'channel_choice': 'automatic', 'verified_channels': [], 'address_masked': None,
            'verification': 'number_required', 'enabled': True, 'last_delivery': None}
        assert set(request) == {'id', 'status', 'created_at', 'expires_at'} and request['status'] == 'open'
        assert request['id'].startswith('contact-') and request['expires_at'] == request['created_at'] + 604800
        assert value['available_members'] == [{'member_id': 'carol', 'name': 'Carol', 'verified': False},
            {'member_id': 'olga', 'name': 'Olga', 'verified': False}]
        assert value['spend'] == {'state': 'open', 'agent': {'used': 0, 'cap': 10}, 'company': {'used': 0, 'cap': 20},
            'resets_at': resets_at(value['spend']['resets_at'] - 1)}
        assert '+34611111111' not in json.dumps(value)
        # An added participant reads the same block as themself. Every reader sees each participant's verified
        # number on the channel in use, masked (CHANNELS.md), and the list of members stays the creator's.
        mine = block(bob, key)
        assert (mine['can_manage'], mine['read_only'], mine['available_members']) == (False, False, [])
        assert (row(mine, 'bob')['is_self'], row(mine, 'alice')['is_self']) == (True, False)
        assert row(mine, 'alice')['address_masked'] == '+346••••••11' and row(mine, 'alice')['verification'] == 'verified'
        assert '+34611111111' not in json.dumps(mine)
        # A security code on its way shows as verification in progress.
        assert bob.post('/api/messaging/verification', json={'phone': '+34622222222', 'member_id': 'bob', 'collection': 'cafe'},
            headers=HEADERS).status_code == 200
        assert row(block(bob, key), 'bob')['verification'] == 'in_progress'
        verify_over_http(olga, sent, 'olga', '+34644444444')
        # The company owner reads the block and the company's masked numbers; the creator manages it.
        owned = block(olga, key)
        assert (owned['can_manage'], owned['available_members']) == (False, [])
        assert row(owned, 'alice')['address_masked'] == '+346••••••11' and not any(item['is_self'] for item in owned['participants'])
        assert next(item for item in block(alice, key)['available_members'] if item['member_id'] == 'olga')['verified'] is True
        # Everyone else, an unknown agent and an Action Agent answer the same sentence.
        assert (carol.get(f'/api/agents/{key}/phone').status_code, carol.get(f'/api/agents/{key}/phone').json()) == (404, NOT_FOUND)
        assert alice.get('/api/agents/agent-unknown/phone').json() == NOT_FOUND
        action = selected['agents'].create({'title': 'Notify supplier', 'instruction': 'Draft the order.', 'dataset': 'cafe',
            'agent_kind': 'action'}, ['cafe'], owner_principal_id='alice')
        assert (alice.get(f"/api/agents/{action['id']}/phone").status_code, alice.get(f"/api/agents/{action['id']}/phone").json()) == (404, NOT_FOUND)
        assert post(alice, action['id'], '/delivery', {'state': 'paused', 'expected_generation': 1}).json() == NOT_FOUND
        # Session, CSRF and observation checks are the host's built ones.
        missing = anonymous.get(f'/api/agents/{key}/phone')
        assert (missing.status_code, missing.text) == (401, 'Enter your company access code.')
        plain = alice.post(f'/api/agents/{key}/phone/delivery', json={'state': 'paused', 'expected_generation': 2})
        assert (plain.status_code, plain.text) == (403, 'Use the demo controls.')
        assert olga.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS).status_code == 200
        observed = block(olga, key)
        assert (observed['read_only'], observed['can_manage'], observed['available_members']) == (True, False, [])
        assert row(observed, 'alice')['is_self'] is True
        refused = post(olga, key, '/delivery', {'state': 'paused', 'expected_generation': observed['generation']})
        assert (refused.status_code, refused.text) == (403, 'Return to your workspace to make changes.')
        assert block(alice, key)['delivery'] == 'on'


def test_participants_switches_channels_delivery_and_the_contact_card_over_http(tmp_path, monkeypatch):
    app, commission = app_fixture(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as carol, TestClient(app) as olga:
        people(app, (alice, bob, carol, olga))
        selected, service, provider, sent, now = commission()
        service.spend = lambda: dict(SPEND)
        verify_over_http(alice, sent, 'alice', '+34611111111')
        verify_over_http(carol, sent, 'carol', '+34633333333')
        key = promote_chat(alice, selected, 'alice', 'track')
        def refused(client, path, body, status, sentence, **options):
            response = post(client, key, path, body, **options)
            assert (response.status_code, response.json()) == (status, {'detail': sentence}), response.text
        def done(client, path, body):
            response = post(client, key, path, body)
            assert response.status_code == 200, response.text
            assert response.json() == block(client, key)  # every POST answers with the fresh block
            return response.json()
        # Bodies outside the contract's shapes.
        for body in ({}, {'member_id': 'bob'}, {'member_id': 'bob', 'expected_generation': '1'}, {'member_id': 7, 'expected_generation': 1},
                {'member_id': 'bob', 'expected_generation': True}, {'member_id': 'bob', 'expected_generation': 1, 'role': 'creator'}):
            refused(alice, '/participants', body, 400, 'Send the phone alert fields.')
        assert post(alice, key, '/participants', None, content=b'[').json() == FIELDS
        large = alice.post(f'/api/agents/{key}/phone/participants', content=json.dumps({'member_id': 'b' * 9000, 'expected_generation': 1}),
            headers={**HEADERS, 'content-type': 'application/json'})
        assert (large.status_code, large.json()) == (413, FIELDS)
        refused(alice, '/participants/alice', {'expected_generation': 1}, 400, 'Send the phone alert fields.')
        refused(alice, '/participants/alice', {'channel': 'fax', 'expected_generation': 1}, 400, 'Send the phone alert fields.')
        refused(alice, '/delivery', {'state': 'off', 'expected_generation': 1}, 400, 'Send the phone alert fields.')
        refused(alice, '/contact-request', {}, 400, 'Send the phone alert fields.')
        # Adding: the creator, a company member, the current generation.
        refused(alice, '/participants', {'member_id': 'zed', 'expected_generation': 1}, 400, 'Choose a member of this company.')
        refused(alice, '/participants', {'member_id': 'bob', 'expected_generation': 9}, 409, 'Refresh the phone alerts and repeat your change.')
        value = done(alice, '/participants', {'member_id': 'bob', 'expected_generation': 1})
        assert value['generation'] == 2 and row(value, 'bob')['contact_request']['status'] == 'open'
        assert [m['contact_request']['member_id'] for m in selected['agent'].children['track']['messages'] if m.get('contact_request')] == ['bob']
        assert done(alice, '/participants', {'member_id': 'bob', 'expected_generation': 99})['generation'] == 2  # a current participant: as it is
        value = done(alice, '/participants', {'member_id': 'carol', 'expected_generation': 2})
        assert (row(value, 'carol')['channel'], row(value, 'carol')['contact_request'], value['generation']) == ('whatsapp', None, 3)
        refused(bob, '/participants', {'member_id': 'olga', 'expected_generation': 3}, 403, 'This agent’s creator manages its participants.')
        refused(olga, '/participants', {'member_id': 'olga', 'expected_generation': 3}, 403, 'This agent’s creator manages its participants.')
        # Each participant owns their switch and channel; the creator can switch another participant off.
        refused(alice, '/participants/zed', {'enabled': False, 'expected_generation': 3}, 400, 'Add this member as a participant first.')
        refused(bob, '/participants/carol', {'enabled': False, 'expected_generation': 3}, 403, 'This agent’s creator manages its participants.')
        for change in ({'enabled': True}, {'channel': 'whatsapp'}, {'channel': 'automatic'}):
            refused(alice, '/participants/carol', {**change, 'expected_generation': 3}, 403,
                'Each participant chooses their own channel and switches their own alerts on.')
        value = done(alice, '/participants/carol', {'enabled': False, 'expected_generation': 3})
        assert (row(value, 'carol')['enabled'], value['generation']) == (False, 4)
        refused(carol, '/participants/carol', {'enabled': True, 'expected_generation': 3}, 409, 'Refresh the phone alerts and repeat your change.')
        value = done(carol, '/participants/carol', {'enabled': True, 'channel': 'whatsapp', 'expected_generation': 4})
        assert (row(value, 'carol')['enabled'], row(value, 'carol')['channel'], row(value, 'carol')['channel_choice']) == (True, 'whatsapp', 'picked')
        refused(carol, '/participants/carol', {'channel': 'sms', 'expected_generation': 5}, 409, 'This channel is being prepared.')
        refused(bob, '/participants/bob', {'channel': 'whatsapp', 'expected_generation': 5}, 409, 'Verify a number on this channel to choose it.')
        assert row(done(carol, '/participants/carol', {'channel': 'automatic', 'expected_generation': 5}), 'carol')['channel_choice'] == 'automatic'
        # The contact card: the creator for any participant, a participant for themself.
        refused(alice, '/contact-request', {'member_id': 'olga'}, 400, 'Add this member as a participant first.')
        refused(alice, '/contact-request', {'member_id': 'carol'}, 409, 'This participant’s number is verified.')
        refused(carol, '/contact-request', {'member_id': 'bob'}, 403, 'This agent’s creator manages its participants.')
        open_request = row(block(alice, key), 'bob')['contact_request']
        assert row(done(bob, '/contact-request', {'member_id': 'bob'}), 'bob')['contact_request'] == open_request
        assert row(done(alice, '/contact-request', {'member_id': 'bob'}), 'bob')['contact_request'] == open_request
        # Completing the card over the built verification routes resolves the participant's channel.
        verify_over_http(bob, sent, 'bob', '+34622222222')
        value = block(bob, key)
        assert (row(value, 'bob')['channel'], row(value, 'bob')['verification'], row(value, 'bob')['contact_request']) == ('whatsapp', 'verified', None)
        assert row(value, 'bob')['address_masked'] == '+346••••••22'
        # The agent's delivery: paused is Stop for this agent, and the creator's alone.
        generation = value['generation']
        refused(bob, '/delivery', {'state': 'paused', 'expected_generation': generation}, 403, 'This agent’s creator manages its participants.')
        refused(alice, '/delivery', {'state': 'paused', 'expected_generation': generation - 1}, 409, 'Refresh the phone alerts and repeat your change.')
        value = done(alice, '/delivery', {'state': 'paused', 'expected_generation': generation})
        assert (value['delivery'], value['generation']) == ('paused', generation + 1)
        assert done(alice, '/delivery', {'state': 'on', 'expected_generation': generation + 1})['delivery'] == 'on'
        # Removing: the creator stays; the creator removes others; an added participant removes themself.
        generation += 2
        refused(alice, '/participants/alice/remove', {'expected_generation': generation}, 409, 'The creator stays a participant of this agent.')
        refused(bob, '/participants/carol/remove', {'expected_generation': generation}, 403, 'This agent’s creator manages its participants.')
        refused(alice, '/participants/olga/remove', {'expected_generation': generation}, 400, 'Add this member as a participant first.')
        value = done(alice, '/participants/carol/remove', {'expected_generation': generation})
        assert [item['member_id'] for item in value['participants']] == ['alice', 'bob']
        assert any(item['member_id'] == 'carol' for item in value['available_members'])
        removed = post(bob, key, '/participants/bob/remove', {'expected_generation': generation + 1})
        # His own change answers with its result; from then on the block is the participants' to read.
        assert removed.status_code == 200 and [item['member_id'] for item in removed.json()['participants']] == ['alice']
        assert (removed.json()['can_manage'], removed.json()['available_members'], removed.json()['generation']) == (False, [], generation + 2)
        assert removed.json() == {**block(alice, key), 'can_manage': False, 'available_members': [],
            'participants': [{**row(block(alice, key), 'alice'), 'is_self': False}]}
        assert (bob.get(f'/api/agents/{key}/phone').status_code, bob.get(f'/api/agents/{key}/phone').json()) == (404, NOT_FOUND)
        assert post(bob, key, '/participants/bob', {'enabled': True, 'expected_generation': generation + 2}).json() == NOT_FOUND


def test_the_block_shows_each_participants_last_delivery_and_the_spend_state(tmp_path, monkeypatch):
    import time
    app, commission = app_fixture(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        sign_in(alice, app, 'alice'); sign_in(bob, app, 'bob')
        host_spend = app.state.workspaces['demo']['messaging'].spend  # the reader the host installs; its record is absent
        selected, service, provider, sent, now = commission()
        provider.config = replace(provider.config, utility_templates=(Template('rocoto_monitoring_alert_v1', 'en_US',
            'UTILITY', 'APPROVED', 'Alert from {{1}}: {{2}} Open {{3}}'),))
        verify_over_http(alice, sent, 'alice', '+34611111111')
        key = promote_chat(alice, selected, 'alice', 'track')
        selected['phone'].add_participant(key, 'bob', by='alice')
        # With no spend record on the host the bound reads unavailable and every delivery is held.
        service.spend = host_spend
        value = block(alice, key)
        assert value['spend'] == {'state': 'unavailable', 'agent': None, 'company': None, 'resets_at': value['spend']['resets_at']}
        delivery = selected['phone_delivery']
        delivery.origin = 'https://rocoto.example'
        written = delivery.fan_out(key, alert_id='event-1', summary='Six people are waiting at checkout two.')
        assert [item['member_id'] for item in written] == ['alice', 'bob']
        def settled(member, status):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                last = row(block(alice, key), member)['last_delivery']
                if last and last['status'] == status:
                    return last
                time.sleep(.05)
            raise AssertionError(row(block(alice, key), member))
        first = settled('alice', 'held')
        assert first == {'id': written[0]['id'], 'channel': 'whatsapp', 'status': 'held', 'held_reason': 'spend_unreadable',
            'updated_at': first['updated_at']}
        assert settled('bob', 'held')['held_reason'] == 'number_required' and len(sent) == 1
        # With the owner's recorded caps the next alert reaches the creator's phone as text and one link.
        service.spend = lambda: {'company_daily_messages': 20, 'agent_daily_messages': 1, 'stopped': False}
        second = delivery.fan_out(key, alert_id='event-2', summary='Six people are waiting at checkout two.')
        shown = settled('alice', 'sent')
        assert (shown['id'], shown['channel'], shown['held_reason']) == (second[0]['id'], 'whatsapp', None)
        assert [value['text'] for value in sent[-1]['template']['components'][0]['parameters']] == [
            'Queue watch', 'Six people are waiting at checkout two.', 'https://rocoto.example/?chat=track&alert=event-2']
        value = block(alice, key)
        assert value['spend'] == {'state': 'cap_reached', 'agent': {'used': 1, 'cap': 1}, 'company': {'used': 1, 'cap': 20},
            'resets_at': value['spend']['resets_at']}
        # The alert is one entry in the Tracking Agent's chat with its delivery states.
        def entries():
            return [m for m in selected['agent'].children['track']['messages'] if m.get('alert')]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and [item['status'] for item in entries()[-1]['channel_deliveries']] != ['sent', 'held']:
            time.sleep(.05)
        assert [entry['alert'] for entry in entries()] == [{'id': 'event-1', 'revision': 1}, {'id': 'event-2', 'revision': 1}]
        assert [(item['member_id'], item['status']) for item in entries()[1]['channel_deliveries']] == [('alice', 'sent'), ('bob', 'held')]
        # While the conversation's turn is active the phone alert still leaves; its entry joins once the turn has drained.
        service.spend = lambda: {'company_daily_messages': 20, 'agent_daily_messages': 5, 'stopped': False}
        selected['agent'].active['track'] = {'status': 'running'}
        third = delivery.fan_out(key, alert_id='event-3', summary='Two people are waiting.')
        assert settled('alice', 'sent')['id'] == third[0]['id']
        time.sleep(1.2)
        assert [entry['alert']['id'] for entry in entries()] == ['event-1', 'event-2']
        del selected['agent'].active['track']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(entries()) < 3:
            time.sleep(.05)
        assert [entry['alert']['id'] for entry in entries()] == ['event-1', 'event-2', 'event-3']
        assert [item['status'] for item in entries()[2]['channel_deliveries']] == ['sent', 'held']
        # The host's Stop shows as the spend state.
        service.spend = lambda: {'company_daily_messages': 20, 'agent_daily_messages': 5, 'stopped': True}
        assert block(alice, key)['spend']['state'] == 'stopped'


def test_a_participant_reads_the_block_while_their_location_assignments_cover_the_chat(tmp_path, monkeypatch):
    import scope_host
    app, commission = app_fixture(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        sign_in(alice, app, 'alice'); sign_in(bob, app, 'bob')
        selected, service, provider, sent, now = commission()
        key = promote_chat(alice, selected, 'alice', 'track')
        selected['phone'].add_participant(key, 'bob', by='alice')
        assert block(bob, key)['chat_id'] == 'track'
        original, asked = scope_host.history_allowed, []
        def assignments(workspace, record, principal):
            asked.append((record['id'], principal))
            return principal != 'bob' and original(workspace, record, principal)
        monkeypatch.setattr(scope_host, 'history_allowed', assignments)
        # Participation covers the conversation while the participant's own assignments cover its scope.
        assert (bob.get(f'/api/agents/{key}/phone').status_code, bob.get(f'/api/agents/{key}/phone').json()) == (404, NOT_FOUND)
        assert ('track', 'bob') in asked
        assert post(bob, key, '/participants/bob', {'enabled': False, 'expected_generation': 2}).json() == NOT_FOUND
        assert row(block(alice, key), 'bob')['enabled'] is True
        monkeypatch.setattr(scope_host, 'history_allowed', original)
        assert block(bob, key)['chat_id'] == 'track'
        # A member who left the company reads nothing, with their session still open.
        with app.state.access.connect() as db:
            db.execute("DELETE FROM principal_environments WHERE principal_id='bob'")
        assert bob.get(f'/api/agents/{key}/phone').status_code in {403, 404}
        assert 'participants' not in bob.get(f'/api/agents/{key}/phone').text
