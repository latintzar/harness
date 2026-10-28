"""Typed credential input keeps secrets outside chat and wakes the exact native tool."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from custom_connectors import CustomConnectors
from test_custom_connectors import package, create
from agent_bridge import CodexVideoAgent, tool_specs


def prepared(tmp_path, owner='alice', collection='warehouse'):
    store = CustomConnectors(tmp_path)
    record = store.create(name='Deliveries', description='Find deliveries.', kind='sql',
                          collection=collection, owner=owner)
    store.save_package(record['id'], package(), expected_revision=1, owner=owner)
    return store, record


def request(store, record, owner='alice', chat_id='chat'):
    return store.request_credentials(record['id'], owner=owner, chat_id=chat_id,
                                     collection=record['collection'], reason='Read the delivery records.')


def test_atomic_private_submit_and_idempotent_duplicate_never_overwrite_credentials(tmp_path):
    store, saved = prepared(tmp_path)
    pending = request(store, saved)
    assert pending['status'] == 'pending' and pending['connector_revision'] == 2
    private = 'postgresql://account:private-password@db.internal/orders'
    result = store.resolve_credential_request(pending['id'], owner='alice', chat_id='chat',
        action='submit', values={'DATABASE_URL': private})
    assert result['status'] == 'submitted' and result['fields'][0]['configured']
    same = store.resolve_credential_request(pending['id'], owner='alice', chat_id='chat',
        action='submit', values={'DATABASE_URL': 'different-secret'})
    assert same == result
    with store.db() as db:
        ciphertext = db.execute('SELECT ciphertext FROM credentials').fetchone()[0]
        assert store._cipher().decrypt(ciphertext).decode() == private
    assert private not in json.dumps(result) and private.encode() not in store.database.read_bytes()
    assert 'different-secret' not in store.database.read_bytes().decode(errors='ignore')
    assert CustomConnectors(tmp_path).credential_request(pending['id'], owner='alice', chat_id='chat') == result


def test_missing_fields_unknown_fields_owner_scope_and_revision_drift(tmp_path):
    store, saved = prepared(tmp_path)
    pending = request(store, saved)
    for values in [{}, {'UNDECLARED': 'private'}, {'DATABASE_URL': ['private']}]:
        with pytest.raises(ValueError):
            store.resolve_credential_request(pending['id'], owner='alice', chat_id='chat', action='submit', values=values)
    for owner, chat in [('bob', 'chat'), ('alice', 'other')]:
        with pytest.raises(ValueError):
            store.resolve_credential_request(pending['id'], owner=owner, chat_id=chat,
                action='submit', values={'DATABASE_URL': 'private-value'})
    assert not store.get(saved['id'], owner='alice')['secret_fields'][0]['configured']
    store.save_package(saved['id'], package(), expected_revision=2, owner='alice')
    result = store.resolve_credential_request(pending['id'], owner='alice', chat_id='chat',
        action='submit', values={'DATABASE_URL': 'obsolete-private-value'})
    assert result['status'] == 'cancelled' and result['resolution_reason'] == 'connector_changed'
    assert not store.get(saved['id'], owner='alice')['secret_fields'][0]['configured']
    assert b'obsolete-private-value' not in store.database.read_bytes()


def test_configured_fields_may_be_omitted_skip_and_cancel_are_terminal(tmp_path):
    store, saved = prepared(tmp_path)
    store.set_secret(saved['id'], 'DATABASE_URL', 'already-private', owner='alice')
    pending = request(store, saved)
    assert pending['fields'][0]['configured']
    assert store.resolve_credential_request(pending['id'], owner='alice', chat_id='chat', action='submit', values={})['status'] == 'submitted'
    skipped = request(store, saved)
    result = store.resolve_credential_request(skipped['id'], owner='alice', chat_id='chat', action='skip', reason='I need access from the administrator.')
    assert result['status'] == 'skipped' and result['skip_reason'] == 'I need access from the administrator.'
    with pytest.raises(ValueError):
        store.resolve_credential_request(skipped['id'], owner='alice', chat_id='chat', action='submit', values={})
    pending = request(store, saved)
    store.cancel_credential_requests(chat_id='chat', owner='alice', reason='stopped')
    assert store.credential_request(pending['id'], owner='alice', chat_id='chat')['resolution_reason'] == 'stopped'


def agent_fixture(tmp_path):
    agent = CodexVideoAgent(tmp_path, object(), lambda: {})
    store, saved = prepared(tmp_path)
    agent.custom_connector_store = store
    agent.children['chat'] = {'id': 'chat', 'owner_principal_id': 'alice', 'dataset': 'warehouse',
                             'messages': [{'role': 'assistant', 'content': '', 'tools': [], 'results': []}]}
    run = {'cancel': asyncio.Event(), 'status': 'running', 'tool_events': asyncio.Queue()}
    agent.active['chat'] = run
    return agent, store, saved, run


@pytest.mark.parametrize('action', ['submit', 'skip'])
def test_same_waiting_tool_resumes_with_metadata_only(tmp_path, action):
    async def scenario():
        agent, store, saved, run = agent_fixture(tmp_path)
        task = asyncio.create_task(agent._tool('request_custom_connector_credentials',
            {'connector_id': saved['id'], 'reason': 'Read delivery records.'}, 'warehouse', 'chat'))
        event = await asyncio.wait_for(run['tool_events'].get(), 2)
        assert event['type'] == 'credential_request' and not task.done()
        pending = event['request']
        assert agent.children['chat']['messages'][-1]['credential_requests'] == [pending]
        private = 'private-database-value'
        body = {'action': 'submit', 'values': {'DATABASE_URL': private}} if action == 'submit' else {'action': 'skip', 'reason': 'Ask the warehouse team.'}
        result = await agent.resolve_credential_request('chat', pending['id'], owner='alice', **body)
        parts, _, success = await asyncio.wait_for(task, 2)
        assert success and json.loads(parts[0]['text'])['status'] == ('submitted' if action == 'submit' else 'skipped')
        resolved = await asyncio.wait_for(run['tool_events'].get(), 2)
        assert resolved == {'type': 'credential_request_resolved', 'request': result}
        serialized = json.dumps([parts, event, resolved, agent.children['chat']])
        assert private not in serialized
        assert private.encode() not in (agent.chat_dir / 'chat.json').read_bytes()
        if action == 'skip':
            assert 'Ask the warehouse team.' in parts[0]['text']
        assert json.loads((agent.chat_dir / 'chat.json').read_text())['messages'][-1]['credential_requests'][0]['status'] == result['status']
    asyncio.run(scenario())


def test_subagent_wrong_scope_and_inactive_tool_are_denied(tmp_path):
    async def scenario():
        agent, store, saved, run = agent_fixture(tmp_path)
        args = {'connector_id': saved['id'], 'reason': 'Read the database.'}
        assert 'request_custom_connector_credentials' not in {x['name'] for x in tool_specs(False)}
        assert not (await agent._tool('request_custom_connector_credentials', args, 'cafe', 'chat'))[2]
        agent.children['chat']['parent_id'] = 'parent'
        assert not (await agent._tool('request_custom_connector_credentials', args, 'warehouse', 'chat'))[2]
        agent.children['chat'].pop('parent_id')
        agent.active.clear()
        assert not (await agent._tool('request_custom_connector_credentials', args, 'warehouse', 'chat'))[2]
    asyncio.run(scenario())


def test_managed_stop_and_restart_cancel_persisted_private_requests(tmp_path):
    from managed_test_support import attach_runtime
    from scope_host import bind_admission
    from host import Admission
    async def scenario():
        agent, store, saved, _ = agent_fixture(tmp_path)
        agent.active.clear()
        client, _ = attach_runtime(agent, actions=[{'name': 'request_custom_connector_credentials',
            'arguments': {'connector_id': saved['id'], 'reason': 'Read the database.'}}])
        events = []
        with bind_admission(Admission(lambda: True)):
            async for event in agent.turn('Connect the database', 'chat', 'warehouse'):
                events.append(event)
                if event['type'] == 'credential_request':
                    pending = event['request']
                    assert (await agent.control('chat', 'stop'))['result']['stopped']
        assert client.cancelled and not agent.active
        assert events[-1]['status'] == 'interrupted'
        current = store.credential_request(pending['id'], owner='alice', chat_id='chat')
        assert current['status'] == 'cancelled' and current['resolution_reason'] == 'stopped'
        assert not client.tool_results
        orphan = request(store, saved)
        agent.children['chat']['messages'][-1]['credential_requests'].append(orphan)
        agent._save_child(agent.children['chat'])
        reopened = CodexVideoAgent(tmp_path)
        restored = reopened.children['chat']['messages'][-1]['credential_requests'][-1]
        assert restored['status'] == 'cancelled' and restored['resolution_reason'] == 'runtime_restarted'
    asyncio.run(scenario())


def test_http_private_input_scope_csrf_validation_and_no_values_in_errors(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app, HEADERS
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        session = client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS).json()
        owner = session['principal']['id']
        agent = app.state.agent
        store, saved = prepared(tmp_path/'state', owner, 'cafe')
        agent.custom_connector_store = store
        agent.children['credential-chat'] = {'id': 'credential-chat', 'owner_principal_id': owner, 'dataset': 'cafe',
            'messages': [{'role': 'assistant', 'content': '', 'tools': [], 'results': []}]}
        pending = request(store, saved, owner, 'credential-chat')
        agent.children['credential-chat']['messages'][-1]['credential_requests'] = [pending]
        agent._save_child(agent.children['credential-chat'])
        url = f"/api/chats/credential-chat/credential-requests/{pending['id']}"
        private = 'value-that-must-never-appear-in-output'
        assert client.post(url, json={'action': 'submit', 'values': {'DATABASE_URL': private}}).status_code == 403
        for body in [{'action':'submit','values':{'DATABASE_URL':[private]}}, {'action':'submit','values':{'DATABASE_URL':private},'extra':private}]:
            result = client.post(url, json=body, headers=HEADERS)
            assert result.status_code == 400 and private not in result.text
        # A restored pending request can be submitted only while its exact native tool is still waiting.
        result = client.post(url, json={'action':'submit','values':{'DATABASE_URL':private}}, headers=HEADERS)
        assert result.status_code == 409 and private not in result.text
        child = {**agent.children['credential-chat'], 'id':'child', 'parent_id':'credential-chat'}
        agent.children['child'] = child
        assert client.post(url.replace('credential-chat/','child/'), json={'action':'skip','reason':'Later'}, headers=HEADERS).status_code == 403
        client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS)
        assert client.post(url, json={'action':'submit','values':{'DATABASE_URL':private}}, headers=HEADERS).status_code == 403


def test_interrupted_key_creation_recovers_without_partial_key(tmp_path, monkeypatch):
    import custom_connectors
    store, saved = prepared(tmp_path)
    original = custom_connectors.Fernet.generate_key
    def interrupted():raise OSError('synthetic disk interruption')
    monkeypatch.setattr(custom_connectors.Fernet, 'generate_key', interrupted)
    with pytest.raises(OSError):store.set_secret(saved['id'], 'DATABASE_URL', 'private-credential', owner='alice')
    assert not store.key_path.exists() and not list(store.root.glob('.credential-key-*'))
    monkeypatch.setattr(custom_connectors.Fernet, 'generate_key', original)
    assert CustomConnectors(tmp_path).set_secret(saved['id'], 'DATABASE_URL', 'private-credential', owner='alice')['configured']


def test_expiry_revision_change_and_tool_cancellation_wake_without_a_model_call(tmp_path):
    async def scenario():
        agent, store, saved, run = agent_fixture(tmp_path)
        task = asyncio.create_task(agent._tool('request_custom_connector_credentials',
            {'connector_id': saved['id'], 'reason': 'Read the database.'}, 'warehouse', 'chat'))
        pending = (await asyncio.wait_for(run['tool_events'].get(), 2))['request']
        store.save_package(saved['id'], package(), expected_revision=2, owner='alice')
        parts, _, success = await asyncio.wait_for(task, 2)
        assert not success and json.loads(parts[0]['text'])['resolution_reason'] == 'connector_changed'
        # Every durable request transition is local; no auth lease/provider fixture exists here.
        pending = request(store, saved)
        with store.db() as db:
            pending['expires_at'] = '2020-01-01T00:00:00+00:00'
            store._save_request(db, pending)
        assert store.credential_request(pending['id'], owner='alice', chat_id='chat')['resolution_reason'] == 'expired'
        task = asyncio.create_task(agent._tool('request_custom_connector_credentials',
            {'connector_id': saved['id'], 'reason': 'Read the database.'}, 'warehouse', 'chat'))
        while True:
            event = await asyncio.wait_for(run['tool_events'].get(), 2)
            if event['request']['status'] == 'pending':break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert store.credential_request(event['request']['id'], owner='alice', chat_id='chat')['resolution_reason'] == 'turn_ended'
    asyncio.run(scenario())


def test_all_fields_commit_together_and_skip_reason_cannot_echo_saved_secret(tmp_path):
    store, saved = prepared(tmp_path)
    source = package()
    source['secret_fields'].append({'name':'API_TOKEN','label':'API token','description':'Token for reading records.'})
    store.save_package(saved['id'], source, expected_revision=2, owner='alice')
    pending = request(store, saved)
    with pytest.raises(ValueError):
        store.resolve_credential_request(pending['id'], owner='alice', chat_id='chat', action='submit',
            values={'DATABASE_URL':'private-database-value','API_TOKEN':'Read delivery'})  # appears in saved summary
    assert not any(x['configured'] for x in store.get(saved['id'], owner='alice')['secret_fields'])
    store.set_secret(saved['id'], 'DATABASE_URL', 'existing-private-secret', owner='alice')
    with pytest.raises(ValueError):
        store.resolve_credential_request(pending['id'], owner='alice', chat_id='chat', action='skip', reason='Use existing-private-secret')
    assert store.credential_request(pending['id'], owner='alice', chat_id='chat')['status'] == 'pending'


def test_managed_dispatch_streams_private_prompt_while_same_function_waits(tmp_path):
    from managed_test_support import attach_runtime
    from scope_host import bind_admission
    from host import Admission
    async def scenario():
        agent = CodexVideoAgent(tmp_path)
        store, saved = prepared(tmp_path)
        agent.custom_connector_store = store
        agent.children['chat'] = {'id': 'chat', 'owner_principal_id': 'alice',
            'dataset': 'warehouse', 'messages': []}
        client, _ = attach_runtime(agent, actions=[{'name': 'request_custom_connector_credentials',
            'arguments': {'connector_id': saved['id'], 'reason': 'Read deliveries.'}}])
        events = []
        private = 'actual-private-form-value'
        with bind_admission(Admission(lambda: True)):
            async for event in agent.turn('Connect the database.', 'chat', 'warehouse'):
                events.append(event)
                if event['type'] == 'credential_request':
                    assert not client.tool_results
                    await agent.resolve_credential_request('chat', event['request']['id'], owner='alice',
                        action='submit', values={'DATABASE_URL': private})
        assert len(client.tool_results) == 1
        types = [event['type'] for event in events]
        assert types.index('credential_request') < types.index('credential_request_resolved') < types.index('tool_end') < types.index('done')
        assert types.count('credential_request_resolved') == 1
        assert private not in json.dumps([events, client.tool_results, agent.children])
        assert json.loads(client.tool_results[0]['output'])['status'] == 'submitted'
    asyncio.run(scenario())


@pytest.mark.parametrize('action',['submit','skip'])
def test_http_response_wakes_live_waiter_and_observer_can_only_read(tmp_path,monkeypatch,action):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        access=app.state.access
        client.post('/api/session',json={'code':access.issue('demo',principal_id='alice')},headers=HEADERS)
        agent=app.state.agent;store,saved=prepared(tmp_path/'state','alice','cafe');agent.custom_connector_store=store
        agent.children['chat']={'id':'chat','owner_principal_id':'alice','dataset':'cafe',
            'messages':[{'role':'assistant','content':'','tools':[],'results':[]}]}
        async def start():
            run={'cancel':asyncio.Event(),'status':'running','tool_events':asyncio.Queue()}
            agent.active['chat']=run
            task=asyncio.create_task(agent._tool('request_custom_connector_credentials',
                {'connector_id':saved['id'],'reason':'Read deliveries.'},'cafe','chat'))
            event=await asyncio.wait_for(run['tool_events'].get(),2)
            return task,event['request']
        task,pending=client.portal.call(start)
        url=f"/api/chats/chat/credential-requests/{pending['id']}"
        assert client.get('/api/chats/chat').json()['messages'][-1]['credential_requests'][0]==pending
        # Same browser owner changes into a read-only customer view.
        client.post('/api/session',json={'code':access.issue('demo',owner=True,principal_id='peter')},headers=HEADERS)
        client.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS)
        assert client.get('/api/chats/chat').status_code==200
        assert client.post(url,json={'action':'skip','reason':'Observed'},headers=HEADERS).status_code==403
        client.post('/api/session',json={'code':access.issue('demo',principal_id='alice')},headers=HEADERS)
        private='private-http-form-submission'
        body={'action':'submit','values':{'DATABASE_URL':private}} if action=='submit' else {'action':'skip','reason':'The administrator has the key.'}
        reply=client.post(url,json=body,headers=HEADERS)
        assert reply.status_code==200,reply.text
        assert reply.headers['cache-control']=='private, no-store'
        assert private not in reply.text
        async def finish():return await asyncio.wait_for(task,2)
        result=client.portal.call(finish)
        assert json.loads(result[0][0]['text'])['status']==reply.json()['status']
        assert private not in json.dumps(result)
        restored=client.get('/api/chats/chat').json()['messages'][-1]['credential_requests'][0]
        assert restored==reply.json()
        assert client.post(url,json=body,headers=HEADERS).json()==restored


def test_http_get_reconciles_expired_request_before_copying_history(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='alice')},headers=HEADERS)
        store,saved=prepared(tmp_path/'state','alice','cafe')
        agent=app.state.agent;agent.custom_connector_store=store
        pending=request(store,saved)
        agent.children['chat']={'id':'chat','owner_principal_id':'alice','dataset':'cafe',
            'messages':[{'role':'assistant','content':'','tools':[],'results':[],'credential_requests':[deepcopy(pending)]}]}
        with store.db() as db:
            pending['expires_at']='2020-01-01T00:00:00+00:00';store._save_request(db,pending)
        result=client.get('/api/chats/chat').json()['messages'][-1]['credential_requests'][0]
        assert result['status']=='timed_out' and result['resolution_reason']=='expired'
