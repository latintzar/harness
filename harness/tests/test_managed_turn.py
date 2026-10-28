"""Provider-free managed execution: authority, recovery and durable effects."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from host import Admission
from managed_codex import ManagedEvent
from managed_sessions import ManagedSessions
from managed_turn import ManagedTurn


@pytest.fixture
def anyio_backend():
    return "asyncio"


def action(call_id='call-1', turn_id='turn-1'):
    return {'type': 'function_call', 'turn_id': turn_id, 'call_id': call_id,
            'name': 'save_value', 'arguments': {'value': 1}}


class Client:
    def __init__(self):
        self.session = {'id': 'session-1', 'agent': {'id': 'root-agent'}, 'required_actions': [action()]}
        self.turns = {'turn-1': {'id': 'turn-1', 'session_id': 'session-1', 'subagent_id': None,
                                 'status': 'in_progress'}}
        self.sent = []
        self.fail = False
        self.on_submit = None

    async def get_session(self, session_id):
        return self.session

    async def get_turn(self, session_id, turn_id):
        return self.turns[turn_id]

    async def submit_events(self, session_id, events, *, submission_key):
        if self.on_submit:
            self.on_submit()
        self.sent.append((events, submission_key))
        if self.fail:
            raise RuntimeError('connection lost after acceptance')
        return {}


class Fixture:
    def __init__(self, path, *, generation='generation-1'):
        self.ledger = ManagedSessions(path)
        self.binding = self.ledger.binding(company_id='company', chat_id='chat', owner='owner',
                                          collection='company', configuration={'model': 'fixture'})
        self.ledger.created(self.binding['id'], 'session-1')
        self.binding = self.ledger.get(self.binding['id'])
        self.client = Client()
        self.calls = 0
        self.current = True
        self.run = {'cancel': asyncio.Event(), 'generation': generation}
        self.admission = Admission(lambda: self.current)
        self.registry = SimpleNamespace(reads=frozenset({'read_value'}), writes=frozenset({'save_value'}))
        async def execute(name, arguments):
            self.calls += 1
            return [{'type': 'inputText', 'text': 'Saved revision 2'}], None, True
        self.engine = ManagedTurn(client=self.client, ledger=self.ledger, binding=self.binding,
                                  run=self.run, admission=self.admission, registry=self.registry,
                                  execute=execute)

    async def actions(self):
        return [event async for event in self.engine.required_actions()]


@pytest.mark.anyio
async def test_effect_receipt_precedes_result_submission_and_replays_after_disconnect(tmp_path):
    first = Fixture(tmp_path)
    first.client.fail = True
    with pytest.raises(RuntimeError):
        await first.actions()
    assert first.calls == 1
    second = Fixture(tmp_path, generation='generation-2')
    await second.actions()
    assert second.calls == 0
    assert second.client.sent == first.client.sent
    assert second.client.sent[0][0][0]['output'] == 'Saved revision 2'


@pytest.mark.anyio
async def test_historical_function_item_never_executes(tmp_path):
    selected = Fixture(tmp_path)
    selected.client.session['required_actions'] = []
    events = await selected.engine.event(ManagedEvent({'type': 'agent.session.turn.item.done',
        'session_id': 'session-1', 'item': {'id': 'item-1', **action(), 'status': 'completed'}}))
    assert selected.calls == 0 and selected.client.sent == [] and events == []


@pytest.mark.anyio
async def test_revocation_during_effect_keeps_receipt_and_withholds_provider_result(tmp_path):
    selected = Fixture(tmp_path)
    async def execute(name, arguments):
        selected.calls += 1
        selected.current = False
        return [{'type': 'inputText', 'text': 'Saved revision 2'}], None, True
    selected.engine.execute = execute
    with pytest.raises(ValueError):
        await selected.actions()
    assert selected.calls == 1 and selected.client.sent == []
    restarted = Fixture(tmp_path, generation='generation-2')
    await restarted.actions()
    assert restarted.calls == 0


@pytest.mark.anyio
async def test_child_function_and_terminal_turn_never_execute(tmp_path):
    selected = Fixture(tmp_path)
    selected.client.turns['turn-1']['subagent_id'] = 'child-1'
    await selected.actions()
    assert selected.calls == 0
    result = selected.client.sent[0][0][0]
    assert result['success'] is False and 'error' in result and 'output' not in result
    selected.client.session['required_actions'] = [action('call-2')]
    selected.client.turns['turn-1']['subagent_id'] = None
    selected.client.turns['turn-1']['status'] = 'completed'
    await selected.actions()
    assert selected.calls == 0 and len(selected.client.sent) == 1


@pytest.mark.anyio
async def test_foreign_session_or_missing_turn_attribution_is_rejected(tmp_path):
    selected = Fixture(tmp_path)
    selected.client.session['id'] = 'foreign'
    with pytest.raises(ValueError):
        await selected.actions()
    selected.client.session['id'] = 'session-1'
    selected.client.turns['turn-1'].pop('subagent_id')
    with pytest.raises(ValueError):
        await selected.actions()
    assert selected.calls == 0


@pytest.mark.anyio
async def test_done_text_replaces_delta_and_completed_item_ignores_stale_deltas(tmp_path):
    selected = Fixture(tmp_path)
    def event(kind, **extra):
        return ManagedEvent({'type': 'agent.session.turn.output_text.' + kind,
            'session_id': 'session-1', 'turn_id': 'turn-1', 'item_id': 'item-1', **extra})
    assert await selected.engine.event(event('delta', delta='Par')) == []
    assert await selected.engine.event(event('done', text='Final')) == [{'type': 'delta', 'text': 'Final', 'managed_item_id': 'item-1'}]
    assert await selected.engine.event(event('delta', delta='stale')) == []
    assert await selected.engine.event(event('done', text='Final')) == []


@pytest.mark.anyio
async def test_root_terminal_sets_confirmation_only_for_bound_session(tmp_path):
    selected = Fixture(tmp_path)
    child = {'id': 'child-turn', 'subagent_id': 'child-1', 'status': 'completed'}
    assert await selected.engine.event(ManagedEvent({'type': 'agent.session.turn.completed',
        'session_id': 'session-1', 'turn': child})) == []
    assert not selected.engine.terminal.is_set()
    root = {'id': 'turn-1', 'subagent_id': None, 'status': 'cancelled'}
    result = await selected.engine.event(ManagedEvent({'type': 'agent.session.turn.cancelled',
        'session_id': 'session-1', 'turn': root}))
    assert result[-1]['status'] == 'interrupted' and selected.engine.terminal.is_set()


@pytest.mark.anyio
async def test_text_waiting_for_attribution_is_withheld_after_revocation(tmp_path):
    selected = Fixture(tmp_path)
    async def revoke_during_lookup(session_id, turn_id):
        selected.current = False
        return selected.client.turns[turn_id]
    selected.client.get_turn = revoke_during_lookup
    event = ManagedEvent({'type': 'agent.session.turn.output_text.done',
        'session_id': 'session-1', 'turn_id': 'turn-1', 'item_id': 'private-item',
        'text': 'Private answer'})
    with pytest.raises(ValueError, match='current workspace admission'):
        await selected.engine.event(event)
    assert 'private-item' not in selected.engine.finished_items


@pytest.mark.anyio
@pytest.mark.parametrize('revoked,oversized', [(True, False), (False, True)])
async def test_recovered_text_requires_current_admission_and_bounded_size(tmp_path, revoked, oversized):
    selected = Fixture(tmp_path)
    selected.current = not revoked
    item = {'id': 'private-item', 'turn_id': 'turn-1', 'type': 'message',
            'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'x' * 4_000_001 if oversized else 'Private'}]}
    with pytest.raises(ValueError):
        await selected.engine.snapshot(selected.client.session, [item], list(selected.client.turns.values()))
    assert 'private-item' not in selected.engine.finished_items


@pytest.mark.anyio
async def test_managed_call_identity_exists_only_during_admitted_execution(tmp_path):
    from managed_turn import current_managed_call
    fixture=Fixture(tmp_path);seen=[]
    async def execute(name,arguments):
        seen.append(current_managed_call())
        seen[-1]['call_id']='caller-mutated-copy'
        assert current_managed_call()['call_id']=='call-1'
        return [{'type':'inputText','text':'Recorded'}],None,True
    fixture.engine.execute=execute
    assert current_managed_call() is None
    async for _ in fixture.engine.required_actions():pass
    assert seen and seen[0]['binding_id']==fixture.binding['id']
    assert current_managed_call() is None
    async for _ in fixture.engine.required_actions():pass
    assert len(seen)==1  # replayed receipts never re-enter an effect callback


@pytest.mark.anyio
async def test_openai_hosted_connection_waits_then_executes_original_function(tmp_path):
    selected = Fixture(tmp_path)
    selected.client.session['environment'] = {'id': 'environment-1', 'type': 'openai_hosted'}
    selected.client.session['required_actions'] = [{'type': 'environment_connection', 'environment_id': 'environment-1'}]
    assert await selected.actions() == []
    assert selected.run['managed_environment_pending'] is True
    assert selected.calls == 0 and selected.client.sent == []
    selected.client.session['required_actions'] = [action()]
    await selected.actions()
    assert selected.calls == 1 and selected.run['managed_environment_pending'] is False


@pytest.mark.anyio
@pytest.mark.parametrize('environment', [
    {'id': 'environment-1', 'type': 'self_hosted'},
    {'id': 'other-environment', 'type': 'openai_hosted'},
    {},
])
async def test_environment_connection_never_launches_or_accepts_other_environment(tmp_path, environment):
    selected = Fixture(tmp_path)
    selected.client.session['environment'] = environment
    selected.client.session['required_actions'] = [{'type': 'environment_connection', 'environment_id': 'environment-1'}]
    with pytest.raises(ValueError, match='bound OpenAI-hosted'):
        await selected.actions()
    assert selected.calls == 0 and selected.client.sent == []


@pytest.mark.anyio
async def test_waiting_turn_executes_required_action_once_and_replays_saved_receipt(tmp_path):
    selected = Fixture(tmp_path)
    selected.client.turns['turn-1']['status'] = 'waiting'
    await selected.actions()
    await selected.actions()
    assert selected.calls == 1
    assert len(selected.client.sent) == 2 and selected.client.sent[0] == selected.client.sent[1]
    assert selected.ledger.turns(selected.binding['id'])[0]['status'] == 'waiting'
    assert not selected.engine.terminal.is_set()
    selected.client.turns['turn-1']['status'] = 'completed'
    await selected.actions()
    assert selected.calls == 1 and len(selected.client.sent) == 2


@pytest.mark.anyio
async def test_waiting_event_keeps_root_turn_active_and_terminal_receipt_monotonic(tmp_path):
    selected = Fixture(tmp_path)
    turn = dict(selected.client.turns['turn-1'], status='waiting')
    event = ManagedEvent({'type': 'agent.session.turn.waiting', 'session_id': 'session-1', 'turn': turn})
    assert event.category == 'turn'
    assert await selected.engine.event(event) == []
    assert selected.engine.root_turn_id == 'turn-1' and not selected.engine.terminal.is_set()
    selected.ledger.turn(selected.binding['id'], dict(turn, status='completed'), 'chat')
    selected.ledger.turn(selected.binding['id'], turn, 'chat')
    assert selected.ledger.turns(selected.binding['id'])[0]['status'] == 'completed'


@pytest.mark.anyio
async def test_queued_turn_cannot_execute_function(tmp_path):
    selected = Fixture(tmp_path)
    selected.client.turns['turn-1']['status'] = 'queued'
    with pytest.raises(ValueError, match='active provider turn'):
        await selected.actions()
    assert selected.calls == 0 and selected.client.sent == []
