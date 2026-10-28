"""Exercise managed session lifecycle with a local protocol fixture and funded-policy double."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from host import Admission
from managed_codex import ManagedEvent, RecoveredSession
from managed_runtime import ManagedRuntime
from scope_host import bind_admission


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Policy:
    def __init__(self):
        self.requests = []
        self.finished = []
        self.revoked = 0

    def base_configuration(self, agent, chat_id, tools, instructions):
        return {'agent': {'model': 'fixture', 'instructions': instructions, 'tools': tools,
                          'multi_agent': {'enabled': False}}, 'environment': {'type': 'none'}}

    @asynccontextmanager
    async def turn(self, agent, chat_id, tools, instructions, **context):
        assert context['request_id']
        self.requests.append(context)
        async def finish(snapshot, *, terminal_status):
            self.finished.append((snapshot['id'] if snapshot else None, terminal_status))
        def revoke(**kwargs):
            self.revoked += 1
        yield SimpleNamespace(configuration={**context['run'].get('base_configuration', self.base_configuration(agent, chat_id, tools, instructions)),
                    'spend_control': {'limit': 10}}, mcp=None, finish=finish, revoke=revoke)


class Client:
    def __init__(self):
        self.created = []
        self.sent = []
        self.updates = []
        self.cancelled = []
        self.session = {'id': 'session-1', 'agent': {'id': 'agent-1'}, 'status': 'idle', 'required_actions': []}
        self.turns = []
        self.events = []
        self.recoveries = 0
        self.stream_wait = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def create_session(self, config, *, submission_key):
        self.created.append((config, submission_key))
        assert config['input'][0]['content'][0]['text'] == 'Hello'
        self.session['status'] = 'in_progress'
        return self.session

    async def update_session(self, session_id, config, *, submission_key):
        self.updates.append((config, submission_key))
        return self.session

    async def get_session(self, session_id):
        return self.session

    async def get_turn(self, session_id, turn_id):
        return next(turn for turn in self.turns if turn['id'] == turn_id)

    async def list_subagents(self, session_id, **kwargs):
        return {'data': [], 'has_more': False}

    async def list_turns(self, session_id, **kwargs):
        return {'data': self.turns, 'has_more': False}

    async def send_message(self, session_id, text, *, submission_key, images=()):
        self.sent.append((text, submission_key, images))
        self.session['status'] = 'in_progress'

    async def interrupt(self, session_id, *, submission_key):
        self.cancelled.append(submission_key)
        self.session['status'] = 'idle'
        for turn in self.turns:
            if turn['status'] == 'in_progress':
                turn['status'] = 'cancelled'

    @asynccontextmanager
    async def recover_session(self, session_id, **kwargs):
        self.recoveries += 1
        async def events():
            for event in self.events:
                if event.category == 'turn':
                    self.turns[:] = [event.raw['turn']]
                    if event.raw['turn']['status'] in {'completed', 'failed', 'cancelled'}:
                        self.session['status'] = 'idle'
                yield event
            if self.stream_wait:
                await asyncio.Event().wait()
        yield RecoveredSession(self.session, [], list(self.turns), events())


def fixture(tmp_path):
    policy, client = Policy(), Client()
    registry = SimpleNamespace(specs=lambda **kwargs: [], reads=frozenset(), writes=frozenset())
    agent = SimpleNamespace(state=tmp_path, children={'chat': {'id': 'chat', 'owner_principal_id': 'owner',
        'dataset': 'company', 'title': 'Existing', 'messages': [{'role': 'user', 'content': 'Hello'},
        {'role': 'assistant', 'content': '', 'tools': [], 'results': []}]}}, active={}, scopes={},
        automation_store=None, tool_registry=registry, _save_child=lambda child: None,
        capability_snapshot=lambda *args: None, agent_setup_context=lambda *args: None,
        _authority_principal=lambda chat: 'owner', result_fields=lambda *args: {}, tool_result_fields=lambda *args: {},
        _input=lambda message, *args: [{'type': 'text', 'text': message}], _skill_input=lambda content, skills: content)
    runtime = ManagedRuntime(lambda: client, policy, timeout=5)
    return agent, runtime, policy, client


def turn_event(status):
    return ManagedEvent({'type': 'agent.session.turn.' + status, 'session_id': 'session-1',
        'turn': {'id': 'turn-1', 'subagent_id': None, 'status': status}})


@pytest.mark.anyio
async def test_first_none_session_carries_input_and_terminal_receipt(tmp_path):
    agent, runtime, policy, client = fixture(tmp_path)
    client.events = [turn_event('in_progress'), ManagedEvent({
        'type': 'agent.session.turn.output_text.done', 'session_id': 'session-1',
        'turn_id': 'turn-1', 'item_id': 'item-1', 'text': 'Hello back'}), turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert client.created and not client.sent
    assert events[-1]['status'] == 'completed'
    assert {'type': 'delta', 'text': 'Hello back', 'managed_item_id': 'item-1'} in events
    assert policy.finished == [('session-1', 'completed')]
    assert not agent.active and policy.revoked == 1


@pytest.mark.anyio
async def test_disconnect_recovers_terminal_snapshot_without_resubmitting_input(tmp_path):
    agent, runtime, policy, client = fixture(tmp_path)
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert len(client.created) == 1 and not client.sent
    assert events[-1]['status'] == 'completed'


@pytest.mark.anyio
async def test_stop_revokes_immediately_and_confirms_provider_terminal(tmp_path):
    agent, runtime, policy, client = fixture(tmp_path)
    client.events = [turn_event('in_progress')]
    client.stream_wait = True
    with bind_admission(Admission(lambda: True)):
        async def consume():
            return [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
        task = asyncio.create_task(consume())
        for _ in range(100):
            await asyncio.sleep(0)
            if agent.active.get('chat', {}).get('managed_engine'):
                break
        result = await runtime.control(agent, 'chat', 'stop')
        assert result['result']['stopped'] is True
        assert policy.revoked >= 1 and client.cancelled
        await task


@pytest.mark.anyio
async def test_reconnect_reads_completed_snapshot_and_never_resubmits_input(tmp_path):
    agent, runtime, policy, client = fixture(tmp_path)
    original = client.recover_session
    @asynccontextmanager
    async def recovering(session_id, **kwargs):
        if client.recoveries == 1:
            client.turns = [turn_event('completed').raw['turn']]
            client.session['status'] = 'idle'
        async with original(session_id, **kwargs) as value:
            yield value
    client.recover_session = recovering
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert client.recoveries == 2 and len(client.created) == 1 and not client.sent
    assert events[-1]['status'] == 'completed'


@pytest.mark.anyio
async def test_consumer_cancellation_confirms_upstream_and_keeps_receipts(tmp_path):
    agent, runtime, policy, client = fixture(tmp_path)
    client.events = [turn_event('in_progress')]
    client.stream_wait = True
    with bind_admission(Admission(lambda: True)):
        async def consume():
            return [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
        task = asyncio.create_task(consume())
        for _ in range(100):
            await asyncio.sleep(0)
            if client.turns:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert client.cancelled and policy.finished == [('session-1', 'cancelled')]
    assert not agent.active


@pytest.mark.anyio
async def test_next_input_reuses_session_after_quiescence_and_funded_update(tmp_path):
    agent, runtime, policy, client = fixture(tmp_path)
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        first = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    agent.children['chat']['messages'].extend([
        {'role': 'user', 'content': 'Again'}, {'role': 'assistant', 'content': ''}])
    second_turn = {'id': 'turn-2', 'subagent_id': None, 'status': 'completed'}
    client.events = [ManagedEvent({'type': 'agent.session.turn.completed', 'session_id': 'session-1',
                                  'turn': second_turn})]
    with bind_admission(Admission(lambda: True)):
        second = [event async for event in runtime.run(agent, 'Again', 'chat', 'company', instructions='Fixture')]
    assert len(client.created) == 1 and client.sent[0][0] == 'Again'
    assert client.updates[0][0] == {'agent': {'model': 'fixture'}, 'spend_control': {'limit': 10}}
    assert first[-1]['status'] == second[-1]['status'] == 'completed'
    assert len(policy.requests) == 2


@pytest.mark.anyio
async def test_unknown_create_resumes_same_outbox_and_creation_key(tmp_path):
    from managed_codex import ManagedCodexError
    agent, runtime, policy, client = fixture(tmp_path)
    create = client.create_session
    first = True
    async def create_once_unknown(config, *, submission_key):
        nonlocal first
        session = await create(config, submission_key=submission_key)
        if first:
            first = False
            raise ManagedCodexError('transport_error')
        return session
    client.create_session = create_once_unknown
    with bind_admission(Admission(lambda: True)):
        with pytest.raises(ManagedCodexError):
            [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company',
                                                       instructions='Fixture', recovering=True)]
    assert events[-1]['status'] == 'completed'
    assert client.created[0] == client.created[1]
    assert policy.requests[0]['request_id'] == policy.requests[1]['request_id']
    assert policy.requests[1]['recovering'] is True
    assert policy.finished == [(None, None), ('session-1', 'completed')]


@pytest.mark.anyio
async def test_recovery_revocation_withholds_text_and_still_settles_terminal(tmp_path):
    agent, runtime, policy, client = fixture(tmp_path)
    current = True
    @asynccontextmanager
    async def recovering(session_id, **kwargs):
        nonlocal current
        client.turns = [turn_event('completed').raw['turn']]
        client.session['status'] = 'idle'
        current = False
        item = {'id': 'private-item', 'turn_id': 'turn-1', 'type': 'message',
                'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'Private answer'}]}
        async def empty():
            if False:
                yield
        yield RecoveredSession(client.session, [item], client.turns, empty())
    client.recover_session = recovering
    events = []
    with bind_admission(Admission(lambda: current)):
        with pytest.raises(ValueError, match='current workspace admission'):
            async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture'):
                events.append(event)
    assert not any(event['type'] == 'delta' for event in events)
    assert policy.finished == [('session-1', 'cancelled')]
    assert not agent.active


@pytest.mark.anyio
async def test_recovery_funding_rejection_cancels_known_provider_before_engine_exists(tmp_path):
    from managed_sessions import ManagedSessions
    from workspace_identity import workspace_scope
    agent, runtime, policy, client = fixture(tmp_path)
    ledger = ManagedSessions(tmp_path)
    binding = ledger.binding(company_id=workspace_scope(tmp_path), chat_id='chat', owner='owner',
        collection='company', configuration=policy.base_configuration(agent, 'chat', [], 'Fixture'))
    ledger.created(binding['id'], 'session-1')
    request = ledger.request(binding['id'], 'original-request',
                             [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hello'}]}])
    ledger.request_state(request['id'], 'submitted')
    client.turns = [turn_event('in_progress').raw['turn']]
    client.session['status'] = 'in_progress'
    @asynccontextmanager
    async def expired(*args, **kwargs):
        raise ValueError('Operator allocation expired')
        yield
    policy.turn = expired
    with bind_admission(Admission(lambda: True)):
        with pytest.raises(ValueError, match='allocation expired'):
            [event async for event in runtime.run(agent, 'Hello', 'chat', 'company',
                instructions='Fixture', recovering=True)]
    assert client.cancelled and client.turns[0]['status'] == 'cancelled'
    assert ledger.pending(binding['id']) is None
    assert not client.created and not client.sent and not client.updates
    assert not agent.active


@pytest.mark.anyio
async def test_recovery_membership_denial_still_withdraws_bound_provider(tmp_path, monkeypatch):
    from managed_sessions import ManagedSessions
    import scope_host
    from workspace_identity import workspace_scope
    agent, runtime, policy, client = fixture(tmp_path)
    ledger = ManagedSessions(tmp_path)
    binding = ledger.binding(company_id=workspace_scope(tmp_path), chat_id='chat', owner='owner',
        collection='company', configuration=policy.base_configuration(agent, 'chat', [], 'Fixture'))
    ledger.created(binding['id'], 'session-1')
    request = ledger.request(binding['id'], 'original-request',
                             [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hello'}]}])
    ledger.request_state(request['id'], 'submitted')
    agent.children['chat']['messages'][0]['managed_request_id'] = 'original-request'
    client.turns = [turn_event('in_progress').raw['turn']]
    client.session['status'] = 'in_progress'
    @asynccontextmanager
    async def denied(*args, **kwargs):
        raise ValueError('Membership withdrawn')
        yield
    monkeypatch.setattr(scope_host, 'agent_scope', denied)
    with pytest.raises(ValueError, match='Membership withdrawn'):
        [event async for event in runtime.recover(agent, 'chat')]
    assert client.cancelled and ledger.pending(binding['id']) is None
    assert not policy.requests and not client.created and not client.sent
    assert await runtime.recover_request(agent, 'chat', 'original-request') == {'status': 'cancelled', 'turn_id': None}
    assert await runtime.recover_request(agent, 'chat', 'missing') == {'status': 'missing', 'turn_id': None}


@pytest.mark.anyio
async def test_environment_change_creates_new_generation_after_old_root_and_children_stop(tmp_path):
    from managed_sessions import ManagedSessions
    from workspace_identity import workspace_scope
    agent, runtime, policy, client = fixture(tmp_path)
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='First')]
    ledger = ManagedSessions(tmp_path)
    first = ledger.for_chat(workspace_scope(tmp_path), 'chat')
    agent.children['chat']['messages'].extend([
        {'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': ''}])
    original_create = client.create_session
    async def create_second(config, *, submission_key):
        client.session = {'id': 'session-2', 'agent': {'id': 'agent-2'}, 'status': 'idle', 'required_actions': []}
        client.turns = []
        client.events = [ManagedEvent({'type': 'agent.session.turn.completed', 'session_id': 'session-2',
            'turn': {'id': 'turn-2', 'subagent_id': None, 'status': 'completed'}})]
        return await original_create(config, submission_key=submission_key)
    client.create_session = create_second
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Second')]
    second = ledger.for_chat(workspace_scope(tmp_path), 'chat')
    assert second['generation'] == 2 and second['id'] != first['id']
    assert len(client.created) == 2 and not client.sent
    assert events[-1]['status'] == 'completed'
    assert 'Previous ordinary conversation' in str(client.created[-1][0]['input'])
    assert ledger.get(first['id'])['session_id'] == 'session-1'


@pytest.mark.anyio
async def test_unknown_create_recovers_original_configuration_after_policy_change(tmp_path):
    from managed_codex import ManagedCodexError
    agent, runtime, policy, client = fixture(tmp_path)
    original = client.create_session
    first = True
    async def create(config, *, submission_key):
        nonlocal first
        result = await original(config, submission_key=submission_key)
        if first:
            first = False
            raise ManagedCodexError('transport_error')
        return result
    client.create_session = create
    with bind_admission(Admission(lambda: True)):
        with pytest.raises(ManagedCodexError):
            [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Original')]
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        [event async for event in runtime.run(agent, 'Hello', 'chat', 'company',
            instructions='Changed selection', recovering=True)]
    assert client.created[0] == client.created[1]
    assert policy.requests[-1]['run']['base_configuration']['agent']['instructions'] == 'Original'


@pytest.mark.anyio
async def test_function_wait_publishes_host_prompt_before_answer(tmp_path):
    agent, runtime, policy, client = fixture(tmp_path)
    agent.tool_registry = SimpleNamespace(reads=frozenset(), writes=frozenset({'ask'}),
        specs=lambda **kwargs: [{'name': 'ask', 'inputSchema': {'type': 'object', 'properties': {}}}])
    answered = asyncio.Event()
    executed = []
    async def execute(name, arguments, dataset, chat_id):
        agent.active[chat_id]['tool_events'].put_nowait({'type': 'user_input', 'request': {'id': 'question-1'}})
        await answered.wait()
        executed.append(name)
        return [{'type': 'inputText', 'text': 'Confirmed'}], None, True
    agent._scoped_tool = execute
    action = {'type': 'function_call', 'turn_id': 'turn-1', 'call_id': 'call-1', 'name': 'ask', 'arguments': {}}
    create = client.create_session
    async def created(config, *, submission_key):
        session = await create(config, submission_key=submission_key)
        client.turns = [turn_event('in_progress').raw['turn']]
        session['required_actions'] = [action]
        return session
    client.create_session = created
    async def submit(*args, **kwargs):
        client.session['required_actions'] = []
    client.submit_events = submit
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        async with asyncio.timeout(3):
            events = []
            async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture'):
                events.append(event)
                if event['type'] == 'user_input':
                    assert not executed
                    answered.set()
    assert answered.is_set() and executed == ['ask'] and events[-1]['status'] == 'completed'


@pytest.mark.anyio
async def test_plugin_revocation_retires_idle_session_and_unknown_creation_blocks_cleanup(tmp_path):
    from managed_sessions import ManagedSessions
    from workspace_identity import workspace_scope
    agent, runtime, policy, client = fixture(tmp_path)
    ledger = ManagedSessions(tmp_path)
    values = dict(company_id=workspace_scope(tmp_path), owner='owner', collection='company',
        configuration={}, capabilities={'selection': {'packages': [{'id': 'docs'}]}})
    binding = ledger.binding(chat_id='chat', **values)
    ledger.created(binding['id'], 'session-1')
    assert await runtime.revoke_plugin(agent, 'other-owner', 'docs') is True
    assert not client.cancelled
    assert await runtime.revoke_plugin(agent, 'owner', 'docs') is True
    assert client.cancelled and ledger.retired(binding['id'])
    unknown = ledger.binding(chat_id='unknown-chat', **values)
    request = ledger.request(unknown['id'], 'original-create', [])
    ledger.request_state(request['id'], 'submitted')
    assert await runtime.revoke_plugin(agent, 'owner', 'docs') is False
    assert ledger.pending(unknown['id'])['id'] == request['id']


@pytest.mark.anyio
async def test_hosted_company_skill_upload_survives_unknown_create_and_changed_selection(tmp_path):
    from managed_codex import ManagedCodexError
    from managed_sessions import ManagedSessions
    from workspace_identity import workspace_scope
    agent, runtime, policy, client = fixture(tmp_path)
    original_base = policy.base_configuration
    def hosted(*args):
        result = original_base(*args)
        result['environment'] = {'type': 'openai_hosted', 'container_size': 'small'}
        return result
    policy.base_configuration = hosted
    original_create = client.create_session
    first = True
    async def create(config, *, submission_key):
        nonlocal first
        result = await original_create(config, submission_key=submission_key)
        if first:
            first = False
            raise ManagedCodexError('transport_error')
        return result
    client.create_session = create
    skills = {'skills': [{'id': 'skill-1', 'name': 'Routine', 'revision': 3,
        'description': 'Follow our routine.', 'instructions': 'Exact company instructions.'}]}
    with bind_admission(Admission(lambda: True)):
        with pytest.raises(ManagedCodexError):
            [event async for event in runtime.run(agent, 'Hello', 'chat', 'company',
                resolved_skills=skills, instructions='Fixture')]
    saved = ManagedSessions(tmp_path).for_chat(workspace_scope(tmp_path), 'chat')
    assert saved['configuration_body']['environment']['skills'][0]['source']['type'] == 'rocoto_archive'
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        [event async for event in runtime.run(agent, 'Hello', 'chat', 'company',
            instructions='Changed', recovering=True)]
    assert client.created[0] == client.created[1]
    assert client.created[0][0]['environment']['skills'][0]['type'] == 'inline'
    assert '$rocoto-' in str(client.created[0][0]['input'])


@pytest.mark.anyio
async def test_failed_capability_recovery_keeps_unknown_create_pending(tmp_path):
    from managed_codex import ManagedCodexError
    from managed_sessions import ManagedSessions
    from workspace_identity import workspace_scope
    agent, runtime, policy, client = fixture(tmp_path)
    original_base = policy.base_configuration
    def hosted(*args):
        result = original_base(*args)
        result['environment'] = {'type': 'openai_hosted'}
        return result
    policy.base_configuration = hosted
    async def unknown(config, *, submission_key):
        client.created.append((config, submission_key))
        raise ManagedCodexError('transport_error')
    client.create_session = unknown
    skills = {'skills': [{'id': 'skill', 'name': 'Routine', 'revision': 1,
        'description': 'Routine', 'instructions': 'Original instructions.'}]}
    with bind_admission(Admission(lambda: True)):
        with pytest.raises(ManagedCodexError):
            [event async for event in runtime.run(agent, 'Hello', 'chat', 'company',
                resolved_skills=skills, instructions='Fixture')]
    next((tmp_path / 'hosted-capabilities').glob('*.zip')).write_bytes(b'changed')
    with bind_admission(Admission(lambda: True)):
        with pytest.raises(ValueError, match='archive'):
            [event async for event in runtime.run(agent, 'Hello', 'chat', 'company',
                instructions='Fixture', recovering=True)]
    ledger = ManagedSessions(tmp_path)
    binding = ledger.pending_binding(workspace_scope(tmp_path), 'chat')
    assert binding and binding['session_id'] is None
    assert ledger.pending(binding['id'])['status'] == 'submitted'
    assert len(client.created) == 1 and len(policy.requests) == 1


@pytest.mark.anyio
async def test_changed_history_scope_cancels_original_recovery_before_new_funding(tmp_path):
    from managed_sessions import ManagedSessions
    from workspace_identity import workspace_scope
    agent, runtime, policy, client = fixture(tmp_path)
    ledger = ManagedSessions(tmp_path)
    binding = ledger.binding(company_id=workspace_scope(tmp_path), chat_id='chat', owner='owner',
        collection='company', configuration=policy.base_configuration(agent, 'chat', [], 'Fixture'),
        capabilities={'history_scope': 'original-admission'})
    ledger.created(binding['id'], 'session-1')
    request = ledger.request(binding['id'], 'original-request',
        [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hello'}]}])
    ledger.request_state(request['id'], 'submitted')
    client.turns = [turn_event('in_progress').raw['turn']]
    client.session['status'] = 'in_progress'
    child_status = ['in_progress']
    checked = []
    interrupt = client.interrupt
    async def stop(session_id, **kwargs):
        await interrupt(session_id, **kwargs)
        child_status[0] = 'cancelled'
    async def children(session_id, **kwargs):
        return {'data': [{'id': 'child', 'session_id': session_id}], 'has_more': False}
    async def turns(session_id, subagent_id, **kwargs):
        checked.append(subagent_id)
        return {'data': [{'id': 'child-turn', 'status': child_status[0]}], 'has_more': False}
    client.interrupt, client.list_subagents, client.list_subagent_turns = stop, children, turns
    with bind_admission(Admission(lambda: True, history_scope='replacement-admission')):
        with pytest.raises(ValueError, match='original admitted history scope'):
            [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', recovering=True)]
    assert client.cancelled and checked == ['child']
    assert ledger.pending(binding['id']) is None and not agent.active
    assert not policy.requests and not client.created and not client.sent and not client.updates


@pytest.mark.anyio
@pytest.mark.parametrize('current_scope', ['scope-b', None])
async def test_rollover_cannot_replay_earlier_scoped_history(tmp_path, current_scope):
    import json
    from managed_sessions import ManagedSessions
    from workspace_identity import workspace_scope
    agent, runtime, policy, client = fixture(tmp_path)
    ledger = ManagedSessions(tmp_path)
    values = dict(company_id=workspace_scope(tmp_path), chat_id='chat', owner='owner', collection='company')
    first = ledger.binding(**values, configuration={'old': 1}, capabilities={'history_scope': 'scope-a'})
    ledger.retire(first['id'], 'configuration_changed')
    second = ledger.rollover(**values, configuration={'old': 2}, capabilities={'history_scope': current_scope},
        previous_id=first['id'], quiesced=True)
    ledger.created(second['id'], 'session-previous')
    current_session = client.get_session
    async def session(session_id):
        if session_id == 'session-previous':
            return {'id': session_id, 'status': 'idle', 'required_actions': []}
        return await current_session(session_id)
    client.get_session = session
    agent.children['chat']['messages'].insert(0, {'role': 'assistant', 'content': 'scope-a-private-secret'})
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True, history_scope=current_scope)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Current')]
    assert events[-1]['status'] == 'completed'
    assert 'scope-a-private-secret' not in json.dumps(client.created[0][0]['input'])
    assert len(client.created) == 1 and not client.sent


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['target', 'transport', 'deadline'])
async def test_monitored_runtime_cancels_provider_while_stream_waits(tmp_path, failure):
    import time
    from managed_spend import SpendMonitor
    agent, runtime, original, client = fixture(tmp_path)
    client.events = [turn_event('in_progress')]
    client.stream_wait = True
    receipts = []
    monitor = SpendMonitor(100, time.time() + (0.08 if failure == 'deadline' else 2))
    class MonitoredPolicy(Policy):
        @asynccontextmanager
        async def turn(self, *args, **context):
            async with super().turn(*args, **context) as lease:
                lease.configuration.pop('spend_control')
                lease.monitor = monitor
                lease.persist_monitor = receipts.append
                yield lease
    policy = MonitoredPolicy()
    runtime.policy = policy
    client.session['usage'] = None if failure == 'deadline' else {'input_tokens': 0, 'output_tokens': 20_000}
    original_read = client.get_session
    async def read(session_id):
        if failure == 'transport' and not client.cancelled:
            raise OSError('private provider response')
        return await original_read(session_id)
    client.get_session = read
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert events[-1]['status'] == 'interrupted'
    assert client.cancelled and not agent.active
    assert 'spend_control' not in client.created[0][0]
    assert receipts[-1]['stop_reason'] == {'target': 'target_reached', 'transport': 'monitor_failed', 'deadline': 'deadline'}[failure]
    assert policy.finished == [('session-1', 'cancelled')]


@pytest.mark.anyio
async def test_hosted_provisioning_polls_until_function_even_without_stream_notification(tmp_path):
    from test_managed_turn import Fixture, action
    selected = Fixture(tmp_path)
    client, engine = selected.client, selected.engine
    client.session['environment'] = {'id': 'environment-1', 'type': 'openai_hosted'}
    client.session['required_actions'] = [{'type': 'environment_connection', 'environment_id': 'environment-1'}]
    polls = 0
    async def get(session_id):
        nonlocal polls
        polls += 1
        if polls >= 2:
            client.session['required_actions'] = [action()]
        return client.session
    @asynccontextmanager
    async def recover(session_id):
        async def events():
            await asyncio.Event().wait()
            yield
        yield RecoveredSession(client.session, [], [], events())
    client.get_session, client.recover_session = get, recover
    client.on_submit = engine.terminal.set
    runtime = ManagedRuntime(lambda: client, None)
    async with asyncio.timeout(3):
        events = [event async for event in runtime._observe(client, engine, created=True,
            input_value=[], submission_key='fixture', baseline=set())]
    assert polls == 2 and selected.calls == 1
    assert any(event['type'] == 'tool_end' for event in events)
    assert selected.run['managed_environment_pending'] is False


@pytest.mark.anyio
@pytest.mark.parametrize('waiting_agent', ['root', 'child'])
async def test_waiting_root_or_child_is_not_quiescent(waiting_agent):
    from managed_runtime import quiescent
    client = Client()
    client.turns = [{'id': 'root-turn', 'status': 'waiting' if waiting_agent == 'root' else 'completed'}]
    async def children(session_id, **kwargs):
        return {'data': [{'id': 'child', 'session_id': session_id}], 'has_more': False}
    async def child_turns(session_id, child_id, **kwargs):
        return {'data': [{'id': 'child-turn', 'status': 'waiting'}], 'has_more': False}
    client.list_subagents, client.list_subagent_turns = children, child_turns
    quiet, _ = await quiescent(client, 'session-1')
    assert quiet is False


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['enforced', 'monitored'])
async def test_followup_update_uses_update_reasoning_schema_over_real_transport(mode):
    import copy
    import json
    import httpx
    from managed_codex import ManagedCodexClient
    config = {'agent': {'model': 'gpt-6-astra', 'instructions': 'Create-only instructions',
        'reasoning': {'effort': 'medium', 'summary': 'concise'}, 'service_tier': 'default',
        'multi_agent': {'enabled': True}, 'tools': [{'type': 'web_search'}]},
        'environment': {'type': 'openai_hosted'}, 'spend_control': {'limit': 160}}
    original = copy.deepcopy(config)
    bodies = []
    async def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if set(body['agent'].get('reasoning', {})) - {'effort'}:
            return httpx.Response(400, json={'error': {'param': 'agent.reasoning.summary'}})
        return httpx.Response(200, json={'id': 'session'})
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handle), spending_mode=mode) as client:
        await ManagedRuntime._update(client, SimpleNamespace(session_id='session'), config, 'original-input')
    assert bodies == [{'agent': {'model': 'gpt-6-astra', 'reasoning': {'effort': 'medium'},
                                'service_tier': 'default'},
                       **({'spend_control': {'limit': 160}} if mode == 'enforced' else {})}]
    assert config == original
