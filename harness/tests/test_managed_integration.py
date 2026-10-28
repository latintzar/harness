"""Real chat, tools, money and MCP authority with a provider-only protocol double."""
import json
from types import SimpleNamespace

import pytest

from agent_bridge import CodexVideoAgent
from host import Admission
from managed_codex import ManagedEvent
from managed_mcp import ManagedMCPHost
from managed_policy import ManagedPolicy
from managed_runtime import ManagedRuntime
from scope_host import bind_admission
from test_managed_runtime import Client, turn_event
from test_managed_usage import funded
from tool_families import ToolRegistry


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_actual_bridge_records_scoped_effect_and_retains_provider_hold(tmp_path):
    pool = funded(tmp_path)
    agent = CodexVideoAgent(tmp_path)
    agent.children['chat'] = {'id': 'chat', 'title': 'Workspace', 'owner_principal_id': 'peter',
                              'dataset': 'company', 'messages': [], 'kind': 'chat'}
    agent._save_child(agent.children['chat'])
    calls = []
    def save(context, name, arguments):
        calls.append((context.actor, context.data_owner, context.collection, context.chat_id))
        (tmp_path / 'saved-value.json').write_text(json.dumps(arguments))
        return {'saved': arguments['value']}
    family = SimpleNamespace(READ_NAMES=frozenset(), WRITE_NAMES=frozenset({'save_value'}),
        call=save, specs=lambda write: [{'name': 'save_value', 'description': 'Save a value.',
            'inputSchema': {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                            'required': ['value'], 'additionalProperties': False}}] if write else [])
    agent.tool_registry = ToolRegistry((family,))
    agent.managed_mcp_host = ManagedMCPHost(tmp_path, public_origin='https://harness.example.test')
    client = Client()
    client.session['spend_control'] = {'limit': 100, 'consumed': 2}
    client.turns = [turn_event('in_progress').raw['turn']]
    client.session['required_actions'] = [{'type': 'function_call', 'turn_id': 'turn-1',
        'call_id': 'call-1', 'name': 'save_value', 'arguments': {'value': 7}}]
    outputs = []
    async def submit(session_id, events, *, submission_key):
        outputs.extend(events)
        client.session['required_actions'] = []
        return {}
    client.submit_events = submit
    client.events = [ManagedEvent({'type': 'agent.session.turn.output_text.done',
        'session_id': 'session-1', 'turn_id': 'turn-1', 'item_id': 'answer-1', 'text': 'Saved.'}),
        turn_event('completed')]
    agent.managed_runtime = ManagedRuntime(lambda: client, ManagedPolicy(), timeout=5)
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in agent.turn('Hello', 'chat', 'company')]
    assert events[-1]['type'] == 'done', events
    assert calls == [('peter', 'peter', 'company', 'chat')]
    assert json.loads(outputs[0]['output']) == {'saved': 7}
    saved = json.loads((tmp_path / 'chats' / 'chat.json').read_text())
    assert saved['messages'][-1]['content'] == 'Saved.'
    assert saved['messages'][-1]['managed_items'] == ['answer-1']
    assert saved['messages'][-1]['tools'][0]['status'] == 'complete'
    assert pool.summary()['reserved_usd'] == '1.000000'
    assert pool.summary()['provider_actual_cost_usd'] == '0.000000'
    assert all(not lease.current() for lease in agent.managed_mcp_host._leases.values())
