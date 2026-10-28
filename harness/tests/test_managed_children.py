"""Provider-attested child ancestry keeps every public child inside its root scope."""
import asyncio
from types import SimpleNamespace

import pytest

from host import Admission
from managed_children import ManagedChildren


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Fixture:
    def __init__(self):
        self.current = True
        self.children = [
            {'id': 'child-1', 'session_id': 'session-1', 'parent_agent_id': 'root-agent', 'name': 'Research'},
            {'id': 'child-2', 'session_id': 'session-1', 'parent_agent_id': 'child-1', 'name': 'Check'},
        ]
        async def list_children(session_id, **parameters):
            return {'data': self.children, 'has_more': False}
        self.client = SimpleNamespace(list_subagents=list_children)
        self.agent = SimpleNamespace(children={'chat': {'id': 'chat', 'dataset': 'company',
            'owner_principal_id': 'owner', 'messages': []}}, active={}, _save_child=lambda record: None)
        self.run = {'generation': 'generation', 'cancel': asyncio.Event(), 'child_ids': set()}
        self.agent.active['chat'] = self.run
        self.tree = ManagedChildren(self.agent, 'chat', self.run,
            {'id': 'session-1', 'agent': {'id': 'root-agent'}}, self.client,
            Admission(lambda: self.current))


@pytest.mark.anyio
async def test_nested_children_inherit_attested_parent_and_exact_root_scope():
    fixture = Fixture()
    child_id = await fixture.tree.admit('child-2')
    child = fixture.agent.children[child_id]
    parent = fixture.agent.children[child['parent_id']]
    assert parent['parent_id'] == 'chat'
    assert child['owner_principal_id'] == parent['owner_principal_id'] == 'owner'
    assert child['dataset'] == parent['dataset'] == 'company'
    assert child['managed_root_id'] == 'chat'
    assert child['managed_subagent_id'] == 'child-2'
    assert await fixture.tree.admit('child-2') == child_id


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['foreign_session', 'cycle', 'unknown_parent', 'revoked'])
async def test_unverified_or_revoked_child_cannot_create_a_chat(change):
    fixture = Fixture()
    if change == 'foreign_session':
        fixture.children[0]['session_id'] = 'foreign'
    elif change == 'cycle':
        fixture.children[0]['parent_agent_id'] = 'child-2'
    elif change == 'unknown_parent':
        fixture.children[0]['parent_agent_id'] = 'unknown'
    else:
        fixture.current = False
    with pytest.raises(ValueError):
        await fixture.tree.admit('child-2')
    assert list(fixture.agent.children) == ['chat']


@pytest.mark.anyio
async def test_stopped_parent_blocks_child_and_replacement_generation_cannot_reuse_authority():
    fixture = Fixture()
    fixture.agent.active['chat'] = dict(fixture.run)
    with pytest.raises(ValueError):
        await fixture.tree.admit('child-1')
    fixture.agent.active['chat'] = fixture.run
    fixture.run['cancel'].set()
    with pytest.raises(ValueError):
        await fixture.tree.admit('child-1')


@pytest.mark.anyio
async def test_recovered_child_history_saves_exact_text_once():
    from agent_bridge import CodexVideoAgent
    fixture = Fixture()
    fixture.agent._record_event = CodexVideoAgent._record_event
    turn = {'id': 'child-turn', 'subagent_id': 'child-2', 'status': 'completed'}
    item = {'id': 'child-item', 'turn_id': 'child-turn', 'type': 'message',
            'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'Verified result'}]}
    async def turns(session_id, child_id, **kwargs):
        return {'data': [turn] if child_id == 'child-2' else [], 'has_more': False}
    async def items(session_id, *, subagent_id, **kwargs):
        return [item] if subagent_id == 'child-2' else []
    fixture.client.list_subagent_turns = turns
    fixture.client.collect_items = items
    await fixture.tree.recover_history()
    await fixture.tree.recover_history()
    record = fixture.agent.children[fixture.tree.bound['child-2']]
    assert record['status'] == 'completed' and record['running'] is False
    assert record['messages'][0]['content'] == 'Verified result'
    assert record['messages'][0]['managed_items'] == ['child-item']
