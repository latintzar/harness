"""Source inspection stays independent of historical model narratives by default."""
import asyncio
import base64
import json
from pathlib import Path
import sys

import pytest
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent, tool_specs
import mcp_server
import retail_mcp_services


@pytest.fixture
def evidence(tmp_path):
    frame = tmp_path / 'source.jpg'
    frame.write_bytes(b'fixture-source-frame')
    records = {name: {'id': name, 'dataset': dataset, 'video_id': name + '-source',
                     'camera': 'Source view', 'start': 12, 'end': 18,
                     'path': '/private/source.mp4', 'thumbnail_path': '/private/thumb.jpg'}
               for name, dataset in [('clip-a', 'cafe'), ('clip-b', 'warehouse')]}
    history = [{'model': 'nvidia/Cosmos3-Edge', 'text': 'Prior model narrative: the person serves a cup.',
                'question': 'Describe the scene.', 'created_at': '2026-10-04T12:00:00Z',
                'verification': 'model_generated_unverified'},
               {'model': 'nvidia/Cosmos3-Super', 'text': 'Later model narrative: a seated person approaches.',
                'question': 'Describe the scene.', 'created_at': '2026-10-04T13:00:00Z',
                'verification': 'model_generated_unverified'}]
    path = tmp_path / 'descriptions' / 'clip-a.json'
    path.parent.mkdir()
    path.write_text(json.dumps(history, indent=2))
    original = path.read_bytes()
    calls = []
    class Index:
        clips = list(records.values())
        def get_clip(self, key):
            return records.get(key)
        def inspect_clip(self, key):
            calls.append(key)
            return {'clip': records[key], 'frames': [{'path': str(frame), 'time': 13.5}]}
        def status(self):
            return {'videos': [], 'datasets': ['cafe', 'warehouse']}
    return tmp_path, Index(), history, path, original, calls


@pytest.mark.parametrize('option', [None, False, True])
def test_native_inspection_history_is_explicit_and_preserves_original_outputs(evidence, option):
    state, index, history, path, original, _ = evidence
    agent = CodexVideoAgent(state, index, lambda: [])
    arguments = {'clip_id': 'clip-a'}
    if option is not None:
        arguments['include_observations'] = option
    content, _, success = asyncio.run(agent._tool('inspect_clip', arguments, 'cafe'))
    assert success
    metadata = json.loads(content[0]['text'])
    assert metadata['sampled_frame_times'] == [13.5]
    assert (metadata['start'], metadata['end']) == (12, 18)
    assert 'path' not in metadata and 'thumbnail_path' not in metadata
    assert base64.b64decode(content[1]['imageUrl'].split(',', 1)[1]) == b'fixture-source-frame'
    if option:
        assert metadata['cosmos_observations'] == history
    else:
        assert 'cosmos_observations' not in metadata
        assert all(item['text'] not in content[0]['text'] for item in history)
    assert path.read_bytes() == original


@pytest.mark.parametrize('include', [False, True])
def test_native_collection_boundary_applies_before_history_and_frames(evidence, include):
    state, index, _, _, _, calls = evidence
    agent = CodexVideoAgent(state, index, lambda: [])
    content, _, success = asyncio.run(agent._tool('inspect_clip',
        {'clip_id': 'clip-a', 'include_observations': include}, 'warehouse'))
    assert not success
    assert 'selected video collection' in content[0]['text']
    assert calls == []


@pytest.mark.parametrize('option', [None, False, True])
def test_mcp_inspection_uses_same_explicit_history_and_source_timestamps(evidence, monkeypatch, option):
    state, index, history, path, original, _ = evidence
    monkeypatch.setattr(retail_mcp_services, 'QdrantRetrievalIndex', lambda _: index)
    mcp = mcp_server.make_server(state)
    arguments = {'clip_id': 'clip-a', 'dataset': 'cafe'}
    if option is not None:
        arguments['include_observations'] = option
    content = asyncio.run(mcp.call_tool('inspect_clip', arguments))
    metadata = json.loads(content[0].text)
    assert metadata['sampled_frame_times'] == [13.5]
    assert (metadata['start'], metadata['end']) == (12, 18)
    assert base64.b64decode(content[1].data) == b'fixture-source-frame'
    if option:
        assert metadata['cosmos_observations'] == history
    else:
        assert 'cosmos_observations' not in metadata
        assert all(item['text'] not in content[0].text for item in history)
    assert path.read_bytes() == original


@pytest.mark.parametrize('include', [False, True])
def test_mcp_collection_boundary_applies_before_history_and_frames(evidence, monkeypatch, include):
    state, index, _, _, _, calls = evidence
    monkeypatch.setattr(retail_mcp_services, 'QdrantRetrievalIndex', lambda _: index)
    mcp = mcp_server.make_server(state)
    with pytest.raises(ToolError, match='selected video collection'):
        asyncio.run(mcp.call_tool('inspect_clip',
            {'clip_id': 'clip-a', 'include_observations': include, 'dataset': 'warehouse'}))
    assert calls == []


def test_native_history_option_is_optional_boolean_default_false():
    schema = next(tool for tool in tool_specs() if tool['name'] == 'inspect_clip')['inputSchema']
    assert schema['properties']['include_observations']['type'] == 'boolean'
    assert schema['properties']['include_observations']['default'] is False
    assert 'include_observations' not in schema['required']
