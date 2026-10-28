"""Monitored named jobs share preallocated funding and the parent wall deadline."""
import json

import pytest

from host import Admission
from managed_policy import ManagedPolicy
from scope_host import bind_admission
from test_delegated_jobs import fixture


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_monitored_parent_and_named_job_keep_one_reserved_dollar(tmp_path, monkeypatch):
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path, monkeypatch)
    runtime.policy = ManagedPolicy(spending_mode='monitored')
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
    assert events[-1]['status'] == 'completed'
    assert len(clients) == 2 and calls
    assert all('spend_control' not in client.created[0][0] for client in clients)
    with pool.connect() as db:
        receipts = [json.loads(row[0]) for row in db.execute('SELECT receipt FROM managed_monitors')]
    root = next(value for value in receipts if value['target_cents'] == 80)
    child = next(value for value in receipts if value['target_cents'] == 20)
    assert child['deadline_at'] <= root['deadline_at']
    assert all(value['billed_cents'] is None for value in receipts)
    assert pool.summary()['reserved_usd'] == '1.000000'
    assert pool.summary()['provider_actual_cost_usd'] == '0.000000'


@pytest.mark.anyio
async def test_expired_restart_cancels_original_parent_and_named_child_without_input(tmp_path, monkeypatch):
    import asyncio
    import copy
    import shutil
    from company_usage import CompanyUsage
    from managed_runtime import ManagedRuntime
    from managed_mcp import ManagedMCPHost
    from managed_usage import ManagedUsage
    from test_managed_runtime import Client
    from test_managed_runtime import fixture as agent_fixture
    agent, runtime, pool, clients, roles, calls = fixture(tmp_path / 'original', monkeypatch)
    runtime.policy = ManagedPolicy(spending_mode='monitored')
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
        deadline = agent.active['chat']['spend_monitor'].deadline_at
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    restored, _, _, _ = agent_fixture(recovered_state)
    restored.children, restored.vertical = saved_children, None
    restored.tool_registry = agent.tool_registry
    restored.managed_mcp_host = ManagedMCPHost(recovered_state, public_origin='https://harness.example.test')
    resumed = []
    def recovered_client():
        client = Client()
        client.__dict__.update(saved_clients[len(resumed)])
        client.created, client.sent, client.cancelled = [], [], []
        original = client.interrupt
        async def stop(session_id, *, submission_key):
            await original(session_id, submission_key=submission_key)
            client.session['required_actions'] = []
        client.interrupt = stop
        resumed.append(client)
        return client
    runtime = ManagedRuntime(recovered_client, ManagedPolicy(spending_mode='monitored'))
    restored.managed_runtime = runtime
    monkeypatch.setattr('managed_policy.time.time', lambda: deadline + 1)
    with bind_admission(Admission(lambda: True)):
        with pytest.raises(ValueError, match='stopped monitored'):
            _ = [event async for event in runtime.run(restored, '', 'chat', 'company', recovering=True)]
    assert len(resumed) == 2
    assert all(client.cancelled and not client.created and not client.sent for client in resumed)
    assert all(client.session['status'] == 'idle' for client in resumed)
    usage = ManagedUsage(CompanyUsage.agent_pool(recovered_state))
    with usage.pool.connect() as db:
        saved = [json.loads(row[0]) for row in db.execute('SELECT receipt FROM managed_monitors')]
    assert max(value['deadline_at'] for value in saved) == deadline
    assert usage.pool.summary()['reserved_usd'] == '1.000000'
    assert not restored.active
