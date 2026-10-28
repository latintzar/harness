"""The shared bridge routes managed turns and records replayed items once."""
import asyncio
from types import SimpleNamespace

from agent_bridge import CodexVideoAgent


def test_managed_item_receipt_and_text_save_together():
    answer = {'role': 'assistant', 'content': '', 'tools': [], 'results': []}
    event = {'type': 'delta', 'text': 'Saved answer', 'managed_item_id': 'item-1'}
    CodexVideoAgent._record_event(answer, event)
    CodexVideoAgent._record_event(answer, event)
    assert answer['content'] == 'Saved answer'
    assert answer['managed_items'] == ['item-1']


def test_managed_turn_precedes_legacy_launch():
    called = []
    class Runtime:
        async def recover(self, agent, chat_id):
            if False:
                yield None
        async def run(self, agent, message, chat_id, dataset, *args, **kwargs):
            called.append((message, chat_id, dataset))
            yield {'type': 'delta', 'text': 'Managed answer'}
            yield {'type': 'done', 'status': 'completed'}
    agent = SimpleNamespace(managed_runtime=Runtime(), isolated_launcher=None, children={}, _turn_guests={})
    async def run():
        return [event async for event in CodexVideoAgent._turn(agent, 'Hello', 'chat', 'company')]
    result = asyncio.run(run())
    assert called == [('Hello', 'chat', 'company')] and result[-1]['status'] == 'completed'


def test_managed_controls_use_managed_runtime():
    called = []
    class Runtime:
        async def control(self, agent, chat_id, operation, *args, **kwargs):
            called.append((chat_id, operation))
            return {'ok': True, 'result': {'stopped': True}}
    agent = SimpleNamespace(active={'chat': {'managed': True, 'generation': 'gen'}}, children={},
        managed_runtime=Runtime(), _cancel_credential_waits=lambda *args: None)
    result = asyncio.run(CodexVideoAgent.control(agent, 'chat', 'stop'))
    assert result['result']['stopped'] and called == [('chat', 'stop')]
