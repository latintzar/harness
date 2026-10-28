"""Declared roles use the same managed loop with separate, preallocated sessions."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from host import Admission
from managed_codex import ManagedEvent
from managed_policy import ManagedPolicy
from managed_mcp import ManagedMCPHost
from managed_runtime import ManagedRuntime
from managed_sessions import ManagedSessions
from scope_host import bind_admission, current_admission
from tool_families import ToolRegistry, ToolContext
from workspace_identity import workspace_scope
from test_managed_usage import funded
from test_managed_runtime import Client, fixture as agent_fixture
import delegated_jobs
import pack


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def role():
    return {'id': 'reader', 'title': 'Workspace reader', 'job': 'Read workspace facts', 'instructions': 'Use workspace_read.',
        'launched_by': ['chat'], 'tools': ['workspace_read'], 'model': 'gpt-6-astra', 'effort': 'low',
        'limit_cents': 20, 'per_turn': 1, 'timeout_seconds': 10}


def fixture(tmp_path, monkeypatch):
    roles = [role()]
    original_vertical = pack.active()
    monkeypatch.setattr(pack, 'active', lambda: SimpleNamespace(sub_agents=lambda: copy.deepcopy(roles),
        import_tool_families=original_vertical.import_tool_families))
    agent, _, _, _ = agent_fixture(tmp_path)
    agent.vertical = None
    pool = funded(tmp_path)
    agent.managed_mcp_host = ManagedMCPHost(tmp_path, public_origin='https://harness.example.test')
    calls = []
    def read(context, name, arguments):
        calls.append((context.actor, context.data_owner, context.collection, context.chat_id))
        return {'name': 'Fixture company'}
    family = SimpleNamespace(READ_NAMES=frozenset({'workspace_read'}), WRITE_NAMES=frozenset(),
        specs=lambda write=True: [{'name': 'workspace_read', 'description': 'Read workspace facts',
            'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}], read=read)
    agent.tool_registry = ToolRegistry((family, delegated_jobs.FAMILY))
    from agent_bridge import CodexVideoAgent
    agent._record_event = CodexVideoAgent._record_event
    async def tool(name, arguments, dataset, chat_id):
        context = ToolContext(agent.state, workspace_scope(agent.state), 'owner', 'owner', dataset,
            chat_id, agent.children[chat_id], agent.active[chat_id], agent=agent, admission=current_admission())
        result = await agent.tool_registry.invoke(context, name, arguments)
        return [{'type': 'inputText', 'text': __import__('json').dumps(result)}], None, True
    agent._scoped_tool = tool
    clients = []
    def client_factory():
        client = Client()
        number = len(clients)
        sid, tid = f'session-{number}', f'turn-{number}'
        client.session.update(id=sid, agent={'id': f'agent-{number}'}, spend_control={'limit': 80 if number == 0 else 20, 'consumed': 1})
        client.turns = [{'id': tid, 'status': 'in_progress', 'subagent_id': None}]
        client.session['required_actions'] = [{'type': 'function_call', 'turn_id': tid,
            'call_id': f'call-{number}', 'name': 'delegate_job' if number == 0 else 'workspace_read',
            'arguments': {'role_id': 'reader', 'task': 'Hello'} if number == 0 else {}}]
        client.events = [ManagedEvent({'type': 'agent.session.turn.output_text.done', 'session_id': sid,
            'turn_id': tid, 'item_id': f'item-{number}', 'text': 'Verified workspace facts'}),
            ManagedEvent({'type': 'agent.session.turn.completed', 'session_id': sid,
                'turn': {'id': tid, 'subagent_id': None, 'status': 'completed'}})]
        client.results = []
        async def submit(session_id, events, **kwargs):
            client.results.extend(events)
            client.session['required_actions'] = []
        client.submit_events = submit
        interrupt = client.interrupt
        async def stop(session_id, *, submission_key, original=interrupt, selected=client):
            await original(session_id, submission_key=submission_key)
            selected.session['required_actions'] = []
        client.interrupt = stop
        clients.append(client)
        return client
    runtime = ManagedRuntime(client_factory, ManagedPolicy(), timeout=30)
    agent.managed_runtime = runtime
    return agent, runtime, pool, clients, roles, calls


@pytest.mark.anyio
async def test_declared_job_runs_separate_managed_session_with_exact_tools_model_cost_and_owner(tmp_path, monkeypatch):
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path, monkeypatch)
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert events[-1]['status'] == 'completed'
    assert len(clients) == 2
    root, child = [client.created[0][0] for client in clients]
    assert root['spend_control']['limit'] == 80
    assert child['spend_control']['limit'] == 20
    assert child['environment'] == {'type': 'none'}
    assert child['agent']['multi_agent'] == {'enabled': False}
    assert child['agent']['model'] == roles[0]['model']
    assert child['agent']['reasoning']['effort'] == 'low'
    assert [tool['name'] for tool in child['agent']['tools']] == ['workspace_read']
    assert calls == [('owner', 'owner', 'company', next(key for key in agent.children if key != 'chat'))]
    assert pool.summary()['reserved_usd'] == '1.000000'
    record = agent.children[calls[0][-1]]
    assert record['kind'] == 'managed_job_child' and record['managed_root_id'] == 'chat'
    assert record['messages'][-1]['content'] == 'Verified workspace facts'
    assert any(event['type'] == 'subchat' for event in events)
    assert clients[0].results[0]['success'] is True


@pytest.mark.anyio
async def test_role_removal_during_child_read_withdraws_result(tmp_path, monkeypatch):
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path, monkeypatch)
    original = agent._scoped_tool
    async def revoke(name, arguments, dataset, chat_id):
        result = await original(name, arguments, dataset, chat_id)
        if name == 'workspace_read':
            roles.clear()
        return result
    agent._scoped_tool = revoke
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert len(clients) == 2 and clients[1].cancelled
    assert not clients[1].results
    assert clients[0].results[0]['success'] is False
    assert pool.summary()['reserved_usd'] == '1.000000'


def test_organization_job_child_visibility_follows_verified_saved_root(tmp_path):
    from organization_chats import OrganizationChats
    records = {}
    organization = OrganizationChats(tmp_path, environment_id='demo', member=lambda principal: {'id': principal}, records=lambda: records)
    root_id = organization.identity('company', 'organization')
    records[root_id] = {'id': root_id, 'kind': 'organization_chat', 'organization_purpose': 'organization',
        'environment_id': 'demo', 'dataset': 'company', 'owner_principal_id': organization.owner}
    child = {'id': 'managed-job-one', 'parent_id': root_id, 'managed_root_id': root_id,
        'kind': 'managed_job_child', 'owner_principal_id': organization.owner, 'dataset': 'company'}
    records[child['id']] = child
    assert organization.owns(child)
    assert not organization.owns(child | {'managed_root_id': 'foreign'})
    assert not organization.owns(child | {'dataset': 'foreign'})


@pytest.mark.anyio
async def test_parent_stop_confirms_child_provider_stop_before_acknowledging(tmp_path, monkeypatch):
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path, monkeypatch)
    started = asyncio.Event()
    original = agent._scoped_tool
    async def blocked(name, arguments, dataset, chat_id):
        if name == 'workspace_read':
            started.set()
            await asyncio.Event().wait()
        return await original(name, arguments, dataset, chat_id)
    agent._scoped_tool = blocked
    async def consume():
        with bind_admission(Admission(lambda: True)):
            return [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 3)
        result = await runtime.control(agent, 'chat', 'stop')
        assert result['result']['stopped'] is True
        assert clients[1].cancelled, 'Stop acknowledged while the declared job provider was still running'
        events = await asyncio.wait_for(task, 3)
        assert events[-1]['status'] == 'interrupted'
        assert pool.summary()['reserved_usd'] == '1.000000'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_restart_resumes_original_parent_and_child_without_new_input_or_allowance(tmp_path, monkeypatch):
    import shutil
    from company_usage import CompanyUsage
    from managed_usage import ManagedUsage
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path / 'original', monkeypatch)
    started = asyncio.Event()
    original_tool = agent._scoped_tool
    async def blocked(name, arguments, dataset, chat_id):
        if name == 'workspace_read':
            started.set()
            await asyncio.Event().wait()
        return await original_tool(name, arguments, dataset, chat_id)
    agent._scoped_tool = blocked
    async def consume():
        with bind_admission(Admission(lambda: True)):
            return [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    running = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 3)
        recovered_state = tmp_path / 'recovered'
        shutil.copytree(agent.state, recovered_state)
        saved_children = copy.deepcopy(agent.children)
        saved_clients = [{key: copy.deepcopy(value) for key, value in client.__dict__.items() if not callable(value)} for client in clients]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    restored, _, _, _ = agent_fixture(recovered_state)
    restored.children = saved_children
    restored.vertical = None
    restored.tool_registry = agent.tool_registry
    restored._record_event = agent._record_event
    restored.managed_mcp_host = ManagedMCPHost(recovered_state, public_origin='https://harness.example.test')
    async def tool(name, arguments, dataset, chat_id):
        context = ToolContext(restored.state, workspace_scope(restored.state), 'owner', 'owner', dataset,
            chat_id, restored.children[chat_id], restored.active[chat_id], agent=restored, admission=current_admission())
        result = await restored.tool_registry.invoke(context, name, arguments)
        return [{'type': 'inputText', 'text': __import__('json').dumps(result)}], None, True
    restored._scoped_tool = tool
    resumed = []
    def recovered_client():
        selected = Client()
        selected.__dict__.update(saved_clients[len(resumed)])
        selected.created, selected.sent, selected.results = [], [], []
        async def submit(session_id, events, **kwargs):
            selected.results.extend(events)
            selected.session['required_actions'] = []
        selected.submit_events = submit
        resumed.append(selected)
        return selected
    restored.managed_runtime = ManagedRuntime(recovered_client, ManagedPolicy(), timeout=30)
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in restored.managed_runtime.run(restored, '', 'chat', 'company', recovering=True)]
    assert events[-1]['status'] == 'completed'
    assert len(resumed) == 2 and all(not client.created and not client.sent for client in resumed)
    assert CompanyUsage.agent_pool(recovered_state).summary()['reserved_usd'] == '1.000000'
    jobs = delegated_jobs.DelegatedJobs(recovered_state)
    parent = jobs.ledger.for_chat(workspace_scope(recovered_state), 'chat')
    child = jobs.children(parent['id'])[0]
    assert child['status'] == 'completed'
    assert resumed[0].results[0]['success'] is True


@pytest.mark.anyio
async def test_second_call_cannot_exceed_declared_per_turn_count(tmp_path, monkeypatch):
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path, monkeypatch)
    factory = runtime.client_factory
    def duplicate():
        client = factory()
        if len(clients) == 1:
            first = copy.deepcopy(client.session['required_actions'][0])
            client.session['required_actions'].append(first | {'call_id': 'another-call'})
        return client
    runtime.client_factory = duplicate
    with bind_admission(Admission(lambda: True)):
        _ = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert len(clients) == 2
    assert [result['success'] for result in clients[0].results] == [True, False]
    assert pool.summary()['reserved_usd'] == '1.000000'


@pytest.mark.anyio
async def test_role_cannot_gain_a_write_tool_from_its_manifest(tmp_path, monkeypatch):
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path, monkeypatch)
    roles[0]['tools'] = ['delegate_job']
    with bind_admission(Admission(lambda: True)):
        _ = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert len(clients) == 1 and clients[0].results[0]['success'] is False
    assert pool.summary()['reserved_usd'] == '0.800000'


@pytest.mark.anyio
async def test_unknown_child_creation_holds_parent_uncertain_until_reconciled(tmp_path, monkeypatch):
    from managed_codex import ManagedCodexError
    from managed_usage import ManagedUsage
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path, monkeypatch)
    factory = runtime.client_factory
    def ambiguous():
        client = factory()
        if len(clients) == 2:
            async def uncertain(*args, **kwargs):
                raise ManagedCodexError('transport_error')
            client.create_session = uncertain
        return client
    runtime.client_factory = ambiguous
    with bind_admission(Admission(lambda: True)):
        with pytest.raises(RuntimeError, match='delegated job'):
            _ = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    ledger = ManagedSessions(agent.state)
    assert ledger.pending_binding(workspace_scope(agent.state), 'chat') is not None
    assert pool.summary()['reserved_usd'] == '1.000000'
    with pool.connect() as db:
        assert db.execute("SELECT count(*) FROM managed_role_slots WHERE state='started'").fetchone()[0] == 1


@pytest.mark.anyio
async def test_job_expiry_withdraws_child_authority_during_a_read(tmp_path, monkeypatch):
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path, monkeypatch)
    original = agent._scoped_tool
    async def expired(name, arguments, dataset, chat_id):
        result = await original(name, arguments, dataset, chat_id)
        if name == 'workspace_read':
            job = agent.active[chat_id]['delegated_grant'].job
            job['expires_at'] = 0
        return result
    agent._scoped_tool = expired
    with bind_admission(Admission(lambda: True)):
        _ = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert clients[1].cancelled and not clients[1].results
    assert clients[0].results[0]['success'] is False
