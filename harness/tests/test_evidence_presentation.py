import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent, tool_specs
from evidence_presentation import build_presentation


class Index:
    def get_clip(self, key):
        if key == 'missing':
            return None
        return {'id': key, 'dataset': 'warehouse' if key == 'foreign' else 'cafe',
                'camera': 'Counter', 'start': 0, 'end': 8, 'path': '/private/source.png'}


def args(mode='players', ids=('one',)):
    return {'mode': mode, 'items': [{'clip_id': key, 'caption': 'The box stays beside the counter.'} for key in ids]}


@pytest.mark.parametrize('arguments', [args(ids=('1', '2', '3', '4')), args(ids=('one', 'one')), args(ids=('foreign',)),
    args(ids=('missing',)), {'mode': 'gallery', 'items': []}, args('unknown'),
    {'mode': 'gallery', 'items': [{'clip_id': 'one', 'caption': 'line\nbreak'}]},
    {**args(), 'owner': 'other'}, args([])])
def test_invalid_or_out_of_scope_selection_is_rejected(arguments):
    with pytest.raises(ValueError):
        build_presentation(arguments, index=Index(), collection='cafe', public_clip=CodexVideoAgent._public_clip)


def test_captioned_presentation_is_host_scoped_persisted_and_parent_only(tmp_path):
    async def scenario():
        agent = CodexVideoAgent(tmp_path, Index(), lambda: {})
        agent.children = {'root': {'id': 'root', 'dataset': 'cafe', 'owner_principal_id': 'alice'},
                          'child': {'id': 'child', 'dataset': 'cafe', 'owner_principal_id': 'alice', 'parent_id': 'root'}}
        agent.active['root'] = {'cancel': asyncio.Event(), 'tool_events': asyncio.Queue()}
        assert 'present_evidence' in {item['name'] for item in tool_specs()}
        assert 'present_evidence' not in {item['name'] for item in tool_specs(False)}
        parts, results, ok = await agent._tool('present_evidence', args('gallery', ('one', 'two', 'three', 'four')), 'cafe', 'root')
        assert ok and results is None, parts
        shown = json.loads(parts[0]['text'])
        assert len(shown['items']) == 4 and all(item['caption_kind'] == 'assistant_summary' for item in shown['items'])
        assert 'path' not in shown['items'][0]
        event = agent.active['root']['tool_events'].get_nowait()
        answer = {'results': [], 'tools': []}
        agent._record_event(answer, event)
        assert answer['evidence_presentation'] == shown
        for chat, scope in [('child', 'cafe'), ('root', 'warehouse'), ('missing', 'cafe')]:
            assert not (await agent._tool('present_evidence', args(), scope, chat))[2]
        assert agent.active['root']['tool_events'].empty()
        agent.active['root']['cancel'].set()
        assert not (await agent._tool('present_evidence', args(), 'cafe', 'root'))[2]
    asyncio.run(scenario())


def test_new_user_message_timestamp_survives_saved_history(tmp_path):
    async def scenario():
        agent = CodexVideoAgent(tmp_path, Index(), lambda: {})
        agent.children['root'] = {'id': 'root', 'dataset': 'cafe', 'owner_principal_id': 'alice', 'messages': []}
        async def turn(*args, **kwargs):
            yield {'type': 'done', 'status': 'completed'}
        agent._turn = turn
        async for _ in agent._recorded_turn('Review this', 'root', 'cafe'):
            pass
        restored = CodexVideoAgent(tmp_path, Index(), lambda: {})
        message = restored.children['root']['messages'][0]
        assert message['content'] == 'Review this' and message['created_at'].endswith('+00:00')
    asyncio.run(scenario())
