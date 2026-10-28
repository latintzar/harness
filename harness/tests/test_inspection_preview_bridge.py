"""Exact model frames reach saved tool trails; cancellation never publishes them."""
import asyncio
import base64
import io
import json
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent
from inspection_previews import InspectionPreviews, PreviewError, tool_previews


@pytest.fixture
def sample(tmp_path):
    images = []
    for color in ('red', 'green', 'blue'):
        stream = io.BytesIO(); Image.new('RGB', (12, 8), color).save(stream, format='JPEG')
        images.append(stream.getvalue())
    clip = {'id': 'clip-a', 'dataset': 'cafe', 'video_id': 'video-a', 'start': 2., 'end': 5.}
    class Index:
        clips = [clip]
        def get_clip(self, key): return clip if key == clip['id'] else None
        def inspect_clip(self, key):
            return {'clip': clip, 'frames': [{'image_bytes': raw, 'time': 2.125 + i,
                'basis': 'decoded_frame', 'width': 12, 'height': 8} for i, raw in enumerate(images)]}
    agent = CodexVideoAgent(tmp_path, Index(), lambda: [])
    agent.children['chat'] = {'id': 'chat', 'owner_principal_id': 'alice', 'dataset': 'cafe', 'messages': []}
    agent.active['chat'] = {'cancel': asyncio.Event(), 'generation': 'turn-one'}
    return agent, images


def test_exact_frames_are_reopenable_and_persist_with_tool_event(sample):
    agent, images = sample
    content, _, success = asyncio.run(agent._tool('inspect_clip', {'clip_id': 'clip-a'}, 'cafe', 'chat'))
    assert success
    frames = tool_previews('inspect_clip', content, success)
    assert len(frames) == 3
    for i, descriptor in enumerate(frames):
        receipt = descriptor['url'].split('/')[-2]
        saved = InspectionPreviews(agent.state).get(owner_principal_id='alice', receipt_id=receipt, index=i)
        assert saved['image_bytes'] == images[i] == base64.b64decode(content[i+1]['imageUrl'].split(',')[1])
        assert saved['time'] == 2.125+i and saved['chat_id'] == 'chat'
    answer = {'tools': [], 'content': '', 'results': []}
    agent._record_event(answer, {'type': 'tool_start', 'id': '1', 'name': 'inspect_clip', 'arguments': {'clip_id': 'clip-a'}})
    agent._record_event(answer, {'type': 'tool_end', 'id': '1', 'name': 'inspect_clip', 'success': True, 'frame_previews': frames})
    agent.children['chat']['messages'] = [answer]; agent._save_child(agent.children['chat'])
    reopened = CodexVideoAgent(agent.state, agent.retrieval, lambda: [])
    assert reopened.children['chat']['messages'][0]['tools'][0]['frame_previews'] == frames
    assert 'base64' not in json.dumps(answer)


@pytest.mark.parametrize('change', ['stop', 'replace', 'owner', 'scope'])
def test_late_inspection_has_no_preview_admission(sample, change):
    agent, _ = sample
    inspect = agent.retrieval.inspect_clip
    def late(key):
        result = inspect(key)
        if change == 'stop': agent.active['chat']['cancel'].set()
        elif change == 'replace': agent.active['chat'] = {'cancel': asyncio.Event(), 'generation': 'turn-two'}
        elif change == 'owner': agent.children['chat']['owner_principal_id'] = 'bob'
        else: agent.children['chat']['dataset'] = 'warehouse'
        return result
    agent.retrieval.inspect_clip = late
    content, _, success = asyncio.run(agent._tool('inspect_clip', {'clip_id': 'clip-a'}, 'cafe', 'chat'))
    assert tool_previews('inspect_clip', content, success) == []
    assert not (agent.state/'inspection-previews.sqlite').exists()


def test_preview_storage_limit_preserves_model_inspection(sample, monkeypatch):
    agent, images = sample
    def full(*args, **kwargs): raise PreviewError('The inspection previews reached their storage allowance.')
    monkeypatch.setattr(InspectionPreviews, 'save', full)
    content, _, success = asyncio.run(agent._tool('inspect_clip', {'clip_id': 'clip-a'}, 'cafe', 'chat'))
    assert success and len(content) == 4
    assert json.loads(content[0]['text'])['frame_previews_status'] == 'unavailable'
    assert base64.b64decode(content[1]['imageUrl'].split(',')[1]) == images[0]


def test_successful_dynamic_tool_projects_frames(sample, tmp_path):
    from test_isolated_turn import tree_fixture
    agent, _ = sample
    _, run, connection, tree = tree_fixture(tmp_path/'tree')
    tree.agent = agent; agent.active['chat'] = run
    tree.allowed['inspect_clip'] = {'inputSchema': {'type': 'object', 'properties': {'clip_id': {'type': 'string'}}, 'required': ['clip_id']}}
    asyncio.run(tree.tool({'id': 6, 'params': {'tool': 'inspect_clip', 'arguments': {'clip_id': 'clip-a'}}}, 'chat'))
    event = next(e for e in list(run['tool_events']._queue) if e['type'] == 'tool_end')
    assert len(event['frame_previews']) == 3
    assert connection.writes[-1]['result']['success'] is True
