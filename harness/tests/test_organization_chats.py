"""Company shared conversations and private channel enrollment: zero remote calls."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from types import MethodType

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_server_access import fixture_app
from test_messaging import fixture, incoming, webhook
from messaging import MessagingActor

HEADERS = {'X-Video-Demo':'1'}


def login(client, app, person, environment='demo', owner=False):
    value = client.post('/api/session', json={'code':app.state.access.issue(environment,
        owner=owner, principal_id=person, display_name=person.title())}, headers=HEADERS)
    assert value.status_code == 200


def create(client, purpose='organization', collection='cafe'):
    result = client.post('/api/organization/chats', json={'purpose':purpose,'collection':collection}, headers=HEADERS)
    assert result.status_code == 200, result.text
    return result.json()


def test_canonical_three_shared_chats_preserve_private_histories_and_current_membership(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        login(alice, app, 'alice'); login(bob, app, 'bob')
        selected = app.state.workspaces['demo']; agent = selected['agent']
        agent.children['private'] = {'id':'private','owner_principal_id':'alice','dataset':'cafe','messages':[{'content':'Private'}]}
        first = create(alice)
        assert create(bob)['id'] == first['id']
        assert first['owner_principal_id'] not in {'alice','bob'}
        assert first['messages'] == [] and first['organization_purpose'] == 'organization'
        rows = bob.get('/api/organization/chats?collection=cafe').json()['chats']
        assert {r['organization_purpose'] for r in rows} == {'organization','cameras','messaging'}
        assert len({r['id'] for r in rows}) == 3
        assert bob.get('/api/chats/private').status_code == 404
        assert bob.get('/api/chats/'+first['id']).status_code == 200
        with app.state.access.connect() as db:
            db.execute("DELETE FROM principal_environments WHERE principal_id='bob'")
        assert bob.get('/api/chats/'+first['id']).status_code == 403
        assert bob.post('/api/chat', json={'chat_id':first['id'],'dataset':'cafe','message':'Hello'},headers=HEADERS).status_code == 403


def test_shared_chat_environment_fixed_collection_and_observer_read_only(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    app.state.access.create_environment('other', 'Other')
    with TestClient(app) as alice, TestClient(app) as outsider, TestClient(app) as owner:
        login(alice, app, 'alice'); login(outsider, app, 'outsider', 'other')
        first = create(alice)
        assert outsider.get('/api/chats/'+first['id']).status_code == 404
        assert create(outsider)['owner_principal_id'] != first['owner_principal_id']
        assert alice.post('/api/chat',json={'chat_id':first['id'],'dataset':'company','message':'Hello'},headers=HEADERS).status_code == 400
        assert alice.post('/api/chat',json={'chat_id':first['id'],'dataset':'cafe','message':'Hello','attachments':['a'*32]},headers=HEADERS).status_code == 400
        login(owner, app, 'owner', owner=True)
        assert owner.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS).status_code == 200
        assert owner.get('/api/chats/'+first['id']).json()['read_only'] is True
        assert owner.post('/api/organization/chats',json={'collection':'cafe','purpose':'messaging'},headers=HEADERS).status_code == 403


def test_shared_file_access_requires_exact_org_owner_chat_and_collection(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        login(alice, app, 'alice'); login(bob, app, 'bob')
        chat = create(alice); selected = app.state.workspaces['demo']; files = selected['files']
        public = files.create({'name':'shared.txt','content':'Company evidence'}, owner_principal_id=chat['owner_principal_id'],chat_id=chat['id'],dataset='cafe')
        private = files.create({'name':'private.txt','content':'Private evidence'}, owner_principal_id='alice',chat_id='private',dataset='cafe')
        assert bob.get('/api/files/'+public['id']+'/download').content == b'Company evidence'
        assert bob.get('/api/files/'+private['id']+'/download').status_code == 404
        assert bob.get('/api/files?chat_id='+chat['id']).json()['files'][0]['id'] == public['id']
        assert bob.get('/api/files').json()['files'] == []
        with files.connect() as db:
            db.execute('UPDATE files SET dataset=? WHERE id=?', ('company', public['id']))
        assert bob.get('/api/files/'+public['id']).status_code == 404


def test_messaging_routes_are_private_and_readiness_is_actual(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/messaging').status_code == 401
        client.post('/api/session/demo',headers=HEADERS)
        assert client.get('/api/messaging').status_code == 403
        login(client, app, 'alice')
        body = client.get('/api/messaging?collection=cafe').json()
        assert body['readiness']['reason'] == 'provider_setup_required'
        assert body['members'] == [{'id':'alice','name':'Alice','display_name':'Alice','role':'operator','is_self':True,'cover':None}]
        assert body['setup_chat_id'].startswith('messaging-')
        # The selected vertical declares its channels, each with actual readiness.
        assert body['channels'] == [{'channel':channel,'label':label,'ready':False,'reason':'provider_setup_required','sender':None}
            for channel,label in (('whatsapp','WhatsApp'),)]
        assert not (tmp_path/'state/messaging.sqlite').exists()
        assert client.post('/api/messaging/verification',json={'member_id':'alice','collection':'cafe','phone':'+34611111111'},headers=HEADERS).status_code == 400
        assert client.post('/api/messaging/verification',json={}).status_code == 403
        assert client.get('/api/messaging/webhook/demo').status_code == 503


OPEN_SPEND = {'company_daily_messages': 100, 'agent_daily_messages': 100, 'stopped': False}


def commissioned(app, tmp_path):
    """The host commissioned WhatsApp for this company and recorded its phone spend authorization.

    The application host reads its spend record ahead of every phone send. A
    host with none sends nothing, security codes included, so commissioning
    records one here; a test of the missing record restores `service.spend`.
    """
    selected = app.state.workspaces['demo']; service = selected['messaging']
    _, _, provider, sent, now = fixture(tmp_path/'provider', clock=[time.time()])
    provider.config = replace(provider.config, company_id=service.company_id)
    service.provider = provider
    service.spend = lambda: dict(OPEN_SPEND)
    return selected, service, provider, sent, now


def test_actual_private_otp_hello_routes_member_self_claim_and_signed_reply(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as owner:
        login(alice, app, 'alice'); login(bob, app, 'bob'); login(owner, app, 'owner', owner=True)
        selected, service, provider, sent, now = commissioned(app, tmp_path)
        response = owner.post('/api/messaging/verification', json={'phone':'+34611111111','member_id':'alice','collection':'cafe','request_id':'otp-one'}, headers=HEADERS)
        assert response.status_code == 200, response.text
        challenge = response.json(); code = sent[-1]['template']['components'][0]['parameters'][0]['text']
        assert code not in response.text
        assert owner.get('/api/chats/'+challenge['setup_chat_id']).status_code == 404
        confirm = {'challenge_id':challenge['id'],'code':code,'member_id':'alice','collection':'cafe'}
        assert owner.post('/api/messaging/verification/confirm',json=confirm,headers=HEADERS).status_code == 400
        assert bob.post('/api/messaging/verification/confirm',json=confirm,headers=HEADERS).status_code == 403
        assert alice.post('/api/messaging/verification/confirm',json=confirm,headers=HEADERS).json()['verified'] is True
        hello = alice.post('/api/messaging/hello',json={'request_id':'hello-one','member_id':'alice','collection':'cafe'},headers=HEADERS)
        assert hello.status_code == 200, hello.text
        assert hello.json()['waiting_for_reply'] is True
        assert alice.get('/api/chats/'+challenge['setup_chat_id']).json()['waiting_for_reply'] is True
        assert len(sent) == 2
        overview = owner.get('/api/messaging?collection=cafe').json()
        assert overview['contacts'] == [{'member_id':'alice','phone':'+34611111111','verified':True,'channel':'whatsapp'}]
        assert [(row['channel'],row['ready'],row['sender']) for row in overview['channels']] == [
            ('whatsapp',True,'+34600000000')]
        assert bob.get('/api/messaging?collection=cafe').json()['contacts'] == []
        other = owner.get('/api/messaging?collection=cafe&member_id=alice').json()
        assert other['challenge'] is None and other['outbox'] == []
        assert alice.post('/api/messaging/hello',json={'request_id':'hello-one','member_id':'alice','collection':'cafe'},headers=HEADERS).status_code == 200
        assert len(sent) == 2
        raw, signature = webhook(provider, [incoming(now, context='wamid.out-2')])
        assert alice.post('/api/messaging/webhook/demo', content=raw).status_code == 403
        reply = alice.post('/api/messaging/webhook/demo',content=raw, headers={'x-hub-signature-256':signature})
        assert reply.status_code == 200 and reply.json()['accepted'] == 1
        assert service.pending_replies()[0]['chat_id'] == challenge['setup_chat_id']
        assert service.pending_replies()[0]['member_id'] == 'alice'
        assert alice.post('/api/messaging/webhook/demo',content=raw,headers={'x-hub-signature-256':signature}).json()['duplicates'] == 1
        # The verification request names its channel; without one it selects WhatsApp, the built transport.
        request = {'phone':'+34622222222','member_id':'bob','collection':'cafe'}
        pending = bob.post('/api/messaging/verification',json={**request,'channel':'whatsapp'},headers=HEADERS)
        assert pending.status_code == 200 and pending.json()['phone'] == '+34622222222', pending.text
        in_progress = owner.get('/api/messaging?collection=cafe').json()['contacts']
        assert {'member_id':'bob','phone':'+34622222222','verified':False,'channel':'whatsapp'} in in_progress
        prepared = bob.post('/api/messaging/verification',json={**request,'channel':'sms'},headers=HEADERS)
        assert prepared.status_code == 400 and prepared.json()['detail'] == 'Choose a channel enabled for this workspace.'
        unknown = bob.post('/api/messaging/verification',json={**request,'channel':'fax'},headers=HEADERS)
        assert unknown.status_code == 400 and unknown.json()['detail'] == 'Choose a channel enabled for this workspace.'
        assert len(sent) == 3


@pytest.mark.parametrize('channel',['whatsapp','viber','sms','email','voice'])
def test_chat_sender_and_channel_idempotency_persist_before_managed_admission(tmp_path,channel):
    from agent_test_support import root_agent
    from organization_chats import OrganizationChats
    agent = root_agent(tmp_path)
    from types import SimpleNamespace
    agent.messaging_store = SimpleNamespace(channel_names=(channel,))
    org = OrganizationChats(tmp_path,environment_id='demo',member=lambda p:p in {'alice','bob'})
    agent.organization_chats = org
    record = org.ensure(agent,'cafe','messaging')
    observed = []
    async def managed(self, message, chat_id, *args, **kwargs):
        saved = json.loads((self.chat_dir/(chat_id+'.json')).read_text())
        observed.append(saved['messages'][0])
        yield {'type':'delta','text':'Received'}
        yield {'type':'done','status':'completed'}
    agent._turn = MethodType(managed, agent)
    async def run():
        sender = {'principal_id':'bob','channel':channel,'admission_key':'reply-1'}
        first = [e async for e in agent.turn('Hello',record['id'],'cafe',sender=sender)]
        second = [e async for e in agent.turn('Hello',record['id'],'cafe',sender=sender)]
        denied = [e async for e in agent.turn('Impersonate',record['id'],'cafe',sender={'principal_id':'outsider','channel':'web'})]
        unknown = [e async for e in agent.turn('Hello',record['id'],'cafe',sender={'principal_id':'bob','channel':'telegram','admission_key':'reply-2'})]
        assert first[-1]['status'] == 'completed', first
        assert second == [{'type':'done','status':'already_admitted'}]
        assert denied[0]['type'] == 'error' and unknown[0]['type'] == 'error'
        agent.messaging_store.channel_names = ()
        disabled = [e async for e in agent.turn('Again', record['id'], 'cafe',
            sender={'principal_id': 'bob', 'channel': channel, 'admission_key': 'reply-3'})]
        assert disabled[0]['type'] == 'error'
    asyncio.run(run())
    assert len(observed) == 1 and observed[0]['sender_principal_id'] == 'bob'
    assert observed[0]['channel']['admission_key'] == 'reply-1' and observed[0]['channel']['channel'] == channel


def test_webhook_reply_uses_normal_saved_turn_once_and_waits_for_busy_chat(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    observed = []
    with TestClient(app) as alice:
        login(alice, app, 'alice')
        selected, service, provider, sent, now = commissioned(app, tmp_path)
        chat_id = alice.get('/api/messaging?collection=cafe').json()['setup_chat_id']
        agent = selected['agent']
        agent.active[chat_id] = {'status':'running'}
        actor = MessagingActor(service.company_id,'alice','cafe',chat_id,'alice')
        asyncio.run(service.request_code(actor,'+34611111111','otp-1'))
        code = sent[-1]['template']['components'][0]['parameters'][0]['text']
        service.verify_code(actor, service.describe(actor)['challenge']['id'], code)
        async def turn(self, message, identity, collection, *, sender, model_context=''):
            observed.append((identity, collection, sender))
            record = self.children[identity]
            record['messages'].append({'role':'user','content':message,'sender_principal_id':sender['principal_id'],'channel':sender})
            self._save_child(record)
            yield {'type':'done','status':'completed'}
        agent.turn = MethodType(turn, agent)
        agent._input = lambda *args: []
        raw, signature = webhook(provider,[incoming(now)])
        assert alice.post('/api/messaging/webhook/demo',content=raw,headers={'x-hub-signature-256':signature}).status_code == 200
        time.sleep(1.1)
        assert observed == [] and len(service.pending_replies()) == 1
        del agent.active[chat_id]
        deadline = time.monotonic() + 3
        while not observed and time.monotonic() < deadline: time.sleep(.02)
        assert len(observed) == 1
        assert observed[0][0:2] == (chat_id,'cafe')
        assert observed[0][2]['principal_id'] == 'alice'
        assert observed[0][2]['channel'] == 'whatsapp'
        assert alice.post('/api/messaging/webhook/demo',content=raw,headers={'x-hub-signature-256':signature}).json()['duplicates'] == 1
        time.sleep(1.1)
        assert len(observed) == 1


@pytest.mark.parametrize('lost_reply', [False, True])
@pytest.mark.parametrize('revoked_during_stop', [False, True])
def test_managed_message_receipt_stops_provider_and_preserves_sender(tmp_path, lost_reply, revoked_during_stop):
    from agent_test_support import root_agent
    from managed_test_support import attach_runtime
    from host import Admission
    from scope_host import bind_admission
    from messaging import MessagingActor
    from test_messaging import enroll
    agent = root_agent(tmp_path/'managed')
    service, _, provider, sent, now = fixture(tmp_path/'delivery')
    actor = MessagingActor('company', 'alice', 'cafe', 'chat', 'alice')
    async def scenario():
        await enroll(service, actor, sent)
        raw, signature = webhook(provider, [incoming(now)])
        service.handle_webhook(raw, signature)
        agent.messaging_store = service
        client, policy = attach_runtime(agent, actions=[
            {'name': 'send_message', 'arguments': {'text': 'Your report is ready.'}},
            {'call_id': 'duplicate', 'name': 'send_message', 'arguments': {'text': 'Duplicate'}}])
        # A receipt must interrupt before the provider emits another answer.
        client.events = []
        client.stream_wait = True
        if lost_reply:
            from managed_codex import ManagedCodexError
            submit = client.submit_events
            async def accepted_then_disconnected(*args, **kwargs):
                await submit(*args, **kwargs)
                raise ManagedCodexError('transport_error')
            client.submit_events = accepted_then_disconnected
        checked = []
        async def children(session_id, **kwargs):
            return {'data': [{'id': 'child-1', 'session_id': session_id}] if client.cancelled else [], 'has_more': False}
        async def child_turns(session_id, child_id, **kwargs):
            checked.append(child_id)
            assert 'chat' in agent.active
            status = 'in_progress' if len(checked) == 1 else 'cancelled'
            return {'data': [{'id': 'child-turn', 'status': status}], 'has_more': False}
        client.list_subagents = children
        client.list_subagent_turns = child_turns
        current = [True]
        interrupt = client.interrupt
        async def stop(session_id, **kwargs):
            await interrupt(session_id, **kwargs)
            if revoked_during_stop:
                current[0] = False
        client.interrupt = stop
        with bind_admission(Admission(lambda: current[0])):
            async with asyncio.timeout(3):
                events = [event async for event in agent.turn('Send my report', 'chat', 'cafe',
                    sender={'principal_id': 'alice', 'channel': 'web'})]
        receipts = [event['delivery'] for event in events if event['type'] == 'channel_delivery']
        assert agent.children['chat']['waiting_for_reply'] is True
        if revoked_during_stop:
            assert receipts == [] and events[-1]['status'] == 'interrupted'
            assert 'channel_delivery' not in agent.children['chat']['messages'][-1]
        else:
            assert len(receipts) == 1
            receipt = receipts[0]
            assert (receipt['channel'], receipt['status'], receipt['kind']) == ('whatsapp', 'sent', 'text')
            assert events[-1]['status'] == 'waiting_for_reply'
            assert agent.children['chat']['messages'][-1]['channel_delivery'] == receipt
        assert len(sent) == 2 and sent[-1]['text']['body'] == 'Your report is ready.'
        assert len(client.tool_results) == 1 and client.tool_results[0]['success'] is True
        assert len(checked) >= 2
        assert client.cancelled and policy.revoked and policy.finished == [('session-1', 'cancelled')]
        assert not client.sent and len(client.created) == 1
        assert not any(event['type'] == 'delta' for event in events)
        assert not agent.active
    asyncio.run(scenario())


@pytest.mark.parametrize('body',[{}, {'collection':[],'purpose':'organization'}, {'collection':'cafe','purpose':'bad'}, {'collection':'cafe','purpose':'organization','owner':'alice'}])
def test_organization_setup_rejects_untrusted_shapes(tmp_path, monkeypatch, body):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client, app, 'alice')
        assert client.post('/api/organization/chats',json=body,headers=HEADERS).status_code == 400


def test_org_inspection_preview_and_native_child_remain_exact_shared_records(tmp_path, monkeypatch):
    from inspection_previews import InspectionPreviews
    from test_inspection_preview_access import JPEG
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        login(alice, app, 'alice'); login(bob, app, 'bob')
        chat = create(alice); selected = app.state.workspaces['demo']
        child = {'id':'native-child-fixture','kind':'native_child','owner_principal_id':chat['owner_principal_id'],
            'parent_id':chat['id'],'native_root_id':chat['id'],'dataset':'cafe','messages':[]}
        selected['agent'].children[child['id']] = child
        assert bob.get('/api/chats/'+child['id']).status_code == 200
        frames = InspectionPreviews(selected['state']).save(owner_principal_id=chat['owner_principal_id'],
            chat_id=chat['id'], dataset='cafe', clip_id='demo-clip', frames=[{
                'time':.25,'image_bytes':JPEG,'basis':'source_sample','width':640,'height':360}])
        assert bob.get(frames[0]['url']).content == JPEG
        child['native_root_id'] = 'private'
        assert bob.get('/api/chats/'+child['id']).status_code == 404
        with app.state.access.connect() as db:
            db.execute("DELETE FROM principal_environments WHERE principal_id='bob'")
        assert bob.get(frames[0]['url']).status_code == 403


def test_org_office_preview_resolver_does_not_fall_back_to_private_file(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        login(alice, app, 'alice'); login(bob, app, 'bob')
        chat = create(alice); selected = app.state.workspaces['demo']; files = selected['files']
        public = files.create({'name':'shared.txt','content':'Company evidence'},owner_principal_id=chat['owner_principal_id'],chat_id=chat['id'],dataset='cafe')
        private = files.create({'name':'private.txt','content':'Private evidence'},owner_principal_id='alice',chat_id='private',dataset='cafe')
        preview = selected['document_previews']
        seen = []
        def content(file_id, *, owner):
            seen.append(owner)
            files.get(file_id,owner_principal_id=owner)
            return {'status':'unsupported','file_id':file_id}
        monkeypatch.setattr(preview,'content',content)
        assert bob.get('/api/files/'+public['id']+'/page-preview').status_code == 200
        assert seen[-1] == chat['owner_principal_id']
        assert bob.get('/api/files/'+private['id']+'/page-preview').status_code == 404
        assert seen[-1] == 'bob'


def test_shared_question_reply_uses_saved_owner_after_member_acl(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        login(alice, app, 'alice'); login(bob, app, 'bob')
        chat = create(alice); selected = app.state.workspaces['demo']
        seen = []
        async def resolve(chat_id, request_id, *, owner, action, answers=None):
            seen.append((chat_id, owner, action))
            return {'id':request_id,'status':'skipped'}
        selected['agent'].resolve_native_question = resolve
        path = '/api/chats/'+chat['id']+'/questions/question1'
        assert bob.post(path,json={'action':'skip'},headers=HEADERS).status_code == 200
        assert seen == [(chat['id'],chat['owner_principal_id'],'skip')]
        with app.state.access.connect() as db:
            db.execute("DELETE FROM principal_environments WHERE principal_id='bob'")
        assert bob.post(path,json={'action':'skip'},headers=HEADERS).status_code == 403
        assert len(seen) == 1


def test_full_managed_org_turn_keeps_company_owner_and_live_member_authority(tmp_path):
    """Actual managed dispatcher and workspace tools; provider protocol is synthetic."""
    from host import Admission
    from managed_runtime import ManagedRuntime
    from managed_sessions import ManagedSessions
    from scope_host import bind_admission
    from agent_test_support import root_agent
    from test_managed_runtime import Client, Policy, turn_event
    from organization_chats import OrganizationChats
    from test_business_maps import setup, file as map_file

    agent = root_agent(tmp_path)
    members = {'alice'}
    org = OrganizationChats(tmp_path, environment_id='demo', member=lambda person: person in members,
                            records=lambda: agent.children)
    agent.organization_chats = org
    record = org.ensure(agent, 'cafe', 'organization')
    business, files, maps = setup(tmp_path)
    agent.business_store, agent.file_store, agent.business_map_store = business, files, maps
    agent.business_map_authorized = lambda principal: principal in members
    source = map_file(files, owner=org.owner, chat=record['id'])
    private = files.create({'name': 'personal.txt', 'content': 'Member private content'},
                           owner_principal_id='alice', chat_id='personal', dataset='cafe')
    jobs = [('get_business_profile', {}),
            ('publish_business_map', {'file_id': source['id'], 'expected_profile_revision': 1}),
            ('create_file', {'document': {'name': 'team.txt', 'content': 'Organization output'}}),
            ('create_custom_connector', {'name': 'Camera archive',
                'description': 'Read saved clips from the company camera server.', 'kind': 'sftp'}),
            ('read_file', {'file_id': private['id']})]

    class Protocol(Client):
        def __init__(self):
            super().__init__()
            self.results = []
            self.actors = []
            self.events = [turn_event('completed')]

        async def create_session(self, config, *, submission_key):
            self.created.append((config, submission_key))
            self.session['status'] = 'in_progress'
            self.turns = [turn_event('in_progress').raw['turn']]
            self.session['required_actions'] = [
                {'type': 'function_call', 'turn_id': 'turn-1', 'call_id': f'call-{position}',
                 'name': name, 'arguments': arguments}
                for position, (name, arguments) in enumerate(jobs)]
            return self.session

        async def submit_events(self, session_id, events, *, submission_key):
            self.results.extend(events)
            self.actors.append(agent._authority_principal(record['id']))
            completed = {event['call_id'] for event in events}
            self.session['required_actions'] = [action for action in self.session['required_actions']
                                                if action['call_id'] not in completed]

    async def scenario():
        protocol, policy = Protocol(), Policy()
        agent.managed_runtime = ManagedRuntime(lambda: protocol, policy, timeout=5)
        with bind_admission(Admission(lambda: 'alice' in members)):
            events = [event async for event in agent.turn('Prepare company context', record['id'], 'cafe',
                                                         sender={'principal_id': 'alice', 'channel': 'web'})]
        assert events[-1]['type'] == 'done' and events[-1]['status'] == 'completed', events
        assert len(protocol.created) == 1 and not protocol.sent and not agent.active
        binding = ManagedSessions(tmp_path).for_chat(org.company_id, record['id'])
        assert (binding['owner'], binding['collection']) == (org.owner, 'cafe')
        assert protocol.actors == ['alice'] * len(jobs)
        assert [result['success'] for result in protocol.results] == [True, True, True, True, False]
        assert maps.get(business, owner=org.owner, collection='cafe')['status'] == 'ready'
        connector = json.loads(protocol.results[3]['output'])
        assert connector['chat_id'] == record['id'] and connector['collection'] == 'cafe'
        assert agent.custom_connector_store.get(connector['id'], owner=org.owner)['id'] == connector['id']
        published = json.loads(protocol.results[2]['output'])
        assert files.get(published['id'], owner_principal_id=org.owner)['chat_id'] == record['id']
        assert 'Member private content' not in json.dumps(events)
        members.clear()
        denied = [event async for event in agent.turn('Again', record['id'], 'cafe',
                                                     sender={'principal_id': 'alice', 'channel': 'web'})]
        assert denied[0]['type'] == 'error' and len(protocol.created) == 1

    asyncio.run(scenario())


def test_company_connector_metadata_and_private_entry_require_exact_org_record_and_owner_role(tmp_path, monkeypatch):
    from test_custom_connectors import package
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as owner:
        login(alice, app, 'alice'); login(owner, app, 'owner',owner=True)
        chat = create(alice, 'cameras'); selected = app.state.workspaces['demo']; store = selected['custom_connectors']
        saved = store.create(name='Camera storage',description='Read camera archive.',kind='sftp',collection='cafe',
            owner=chat['owner_principal_id'],chat_id=chat['id'])
        store.save_package(saved['id'],package(),expected_revision=1,owner=chat['owner_principal_id'])
        foreign = store.create(name='Personal',description='Personal account.',kind='sql',collection='cafe',owner='alice')
        key = saved['id']; url = '/api/connections/custom/'+key
        listing = owner.get('/api/connections/custom').json()['connections']
        assert [r['id'] for r in listing] == [key]
        assert listing[0]['organization_shared'] and listing[0]['can_configure']
        assert alice.get(url).json()['can_configure'] is False
        assert alice.get(url+'/package').status_code == 200
        assert owner.get('/api/connections/custom/'+foreign['id']).status_code == 404
        secret = {'name':'DATABASE_URL','value':'synthetic-private-credential'}
        assert alice.post(url+'/secrets',json=secret,headers=HEADERS).status_code == 403
        response = owner.post(url+'/secrets',json=secret,headers=HEADERS)
        assert response.status_code == 200 and secret['value'] not in response.text
        assert store.get(key,owner=chat['owner_principal_id'])['secret_fields'][0]['configured']
        for path,body in [('/execution/target',{'proposal_id':'untrusted'}),('/execution/test',{'expected_revision':2}),('/storage/approve',{'proposal_id':'untrusted'})]:
            assert alice.post(url+path,json=body,headers=HEADERS).status_code == 403
        assert owner.get(url+'/execution').status_code == 200
        # The origin conversation is an authority boundary, not a cosmetic URL.
        selected['agent'].children[chat['id']]['dataset'] = 'company'
        assert owner.get(url).status_code == 404
        assert owner.post(url+'/secrets',json=secret,headers=HEADERS).status_code == 400


def test_shared_business_map_read_uses_exact_company_pack_and_current_member(tmp_path, monkeypatch):
    from test_business_maps import file as map_file, document
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as alice, TestClient(app) as bob:
        login(alice, app, 'alice'); login(bob, app, 'bob')
        chat=create(alice); selected=app.state.workspaces['demo']
        selected['business'].update('cafe',0,{'name':'Fixture café','location':'One square'})
        source=map_file(selected['files'],owner=chat['owner_principal_id'],chat=chat['id'])
        saved=selected['business_maps'].publish(selected['files'],selected['business'],file_id=source['id'],
            expected_profile_revision=1,owner=chat['owner_principal_id'],chat_id=chat['id'],collection='cafe',current=lambda:True,created=[])
        for path in ['/api/business/map?collection=cafe','/api/business/map?collection=cafe&map_id='+saved['id']]:
            response=bob.get(path)
            assert response.status_code==200 and response.json()['data']==document()
        assert bob.get('/api/business/map?collection=company&map_id='+saved['id']).status_code==404
        with app.state.access.connect() as db:
            db.execute("DELETE FROM principal_environments WHERE principal_id='bob'")
        assert bob.get('/api/business/map?collection=cafe&map_id='+saved['id']).status_code==403


def test_a_signed_reply_to_a_host_alert_continues_the_tracking_chat_with_that_alert_as_context(tmp_path, monkeypatch):
    """A phone alert needs no model turn; the reply to it is an ordinary turn in the same conversation."""
    from messaging_provider import Template
    from test_phone_participants import promote_chat, verify_over_http
    app = fixture_app(tmp_path, monkeypatch)
    observed = []
    with TestClient(app) as alice:
        login(alice, app, 'alice')
        selected, service, provider, sent, now = commissioned(app, tmp_path)
        provider.config = replace(provider.config, utility_templates=(Template('rocoto_monitoring_alert_v1', 'en_US',
            'UTILITY', 'APPROVED', 'Alert from {{1}}: {{2}} Open {{3}}'),))
        service.spend = lambda: {'company_daily_messages': 20, 'agent_daily_messages': 10, 'stopped': False}
        delivery = selected['phone_delivery']
        delivery.origin = 'https://rocoto.example'
        verify_over_http(alice, sent, 'alice', '+34611111111')
        key = promote_chat(alice, selected, 'alice', 'track')
        agent = selected['agent']
        async def turn(self, message, identity, collection, *, sender, model_context=''):
            observed.append((identity, message, sender, model_context))
            record = self.children[identity]
            record['messages'].append({'role':'user','content':message,'sender_principal_id':sender['principal_id'],'channel':sender})
            self._save_child(record)
            yield {'type':'done','status':'completed'}
        agent.turn = MethodType(turn, agent)
        agent._input = lambda *args: []
        def wait(check):
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and not check():
                time.sleep(.05)
            assert check()
        written = delivery.fan_out(key, alert_id='event-1', summary='Six people are waiting at checkout two.')
        wait(lambda: delivery.deliveries(key)[0]['status'] == 'sent')
        assert observed == []  # the alert reached the phone with no model turn
        outgoing = delivery.deliveries(key)[0]
        raw, signature = webhook(provider, [incoming(now, id='wamid.reply-1', context=outgoing['provider_message_id'], text='Open a second till.')])
        assert alice.post('/api/messaging/webhook/demo', content=raw, headers={'x-hub-signature-256':signature}).json()['accepted'] == 1
        wait(lambda: len(observed) == 1)
        identity, message, sender, context = observed[0]
        assert (identity, message, sender['principal_id'], sender['channel']) == ('track', 'Open a second till.', 'alice', 'whatsapp')
        assert sender['provider_event_id'] == 'wamid.reply-1' and sender['admission_key'].startswith('whatsapp:')
        # The turn the reply starts receives the alert it answers as host-supplied context.
        assert context.startswith('Host-supplied context (reference data): this message answers the phone alert ')
        assert json.loads(context.split('phone alert ', 1)[1].splitlines()[0]) == {'alert_id':'event-1','alert_revision':1,
            'text':written[0]['text'],'link':'https://rocoto.example/?chat=track&alert=event-1'}
        # The alert entry sits in the conversation ahead of the reply to it.
        messages = agent.children['track']['messages']
        positions = [index for index, item in enumerate(messages) if item.get('alert') or item.get('channel', {}).get('provider_event_id')]
        assert [bool(messages[index].get('alert')) for index in positions] == [True, False]
        # A second copy of the same provider event starts no second turn.
        assert alice.post('/api/messaging/webhook/demo', content=raw, headers={'x-hub-signature-256':signature}).json()['duplicates'] == 1
        time.sleep(1.2)
        assert len(observed) == 1
