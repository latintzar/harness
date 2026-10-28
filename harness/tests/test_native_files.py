"""Generated documents are bound to their parent chat, with bounded model reads."""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent, tool_specs


def test_native_files_use_saved_owner_chat_and_business(tmp_path):
    async def scenario():
        agent = CodexVideoAgent(tmp_path, object(), lambda: {})
        agent.children = {"parent": {"id": "parent", "owner_principal_id": "owner", "dataset": "cafe"},
                          "other": {"id": "other", "owner_principal_id": "owner", "dataset": "cafe"},
                          "child": {"id": "child", "owner_principal_id": "owner", "dataset": "cafe", "parent_id": "parent"}}
        assert {"create_file", "list_files", "read_file"} <= {item["name"] for item in tool_specs()}
        assert "create_file" not in {item["name"] for item in tool_specs(False)}
        agent.active["parent"] = {"cancel": asyncio.Event()}
        content = "# Review\n" + "evidence\n" * 12000
        parts, _, success = await agent._tool("create_file", {"document": {"name": "review.md", "content": content},
            "owner_principal_id": "victim", "chat_id": "other"}, "cafe", "parent")
        assert not success and agent._files().list(owner_principal_id="victim") == []
        parts, _, success = await agent._tool("create_file", {
            "document": {"name": "review.md", "content": content}}, "cafe", "parent")
        assert success, parts
        created = json.loads(parts[0]["text"])
        assert created["chat_id"] == "parent" and created["dataset"] == "cafe"
        parts, _, success = await agent._tool("list_files", {}, "cafe", "parent")
        assert success and json.loads(parts[0]["text"])["files"][0]["id"] == created["id"]
        parts, _, success = await agent._tool("read_file", {"file_id": created["id"], "limit": 50000}, "cafe", "parent")
        page = json.loads(parts[0]["text"])
        assert success and page["content"] == content[:50000] and page["next_offset"] == 50000
        parts, _, success = await agent._tool("read_file", {"file_id": created["id"], "offset": 50000, "limit": 50000}, "cafe", "parent")
        assert success and json.loads(parts[0]["text"])["content"] == content[50000:100000]
        for chat, scope in [("other", "cafe"), ("child", "cafe"), ("missing", "cafe"), ("parent", "warehouse")]:
            assert not (await agent._tool("read_file", {"file_id": created["id"]}, scope, chat))[2]
        assert agent._files().list(owner_principal_id="victim") == []
    asyncio.run(scenario())


def test_turn_timing_and_artifacts_persist_through_the_public_event_reducer():
    answer = {"content": "", "tools": [], "results": []}
    artifact = {"id": "file-" + "a" * 32, "name": "report.md", "download_url": "/api/files/report/download"}
    events = [{"type": "timing", "started_at": "2026-10-04T12:00:00+00:00"},
              {"type": "artifact", "artifact": artifact},
              {"type": "artifact", "artifact": artifact},
              {"type": "done", "status": "completed", "started_at": "2026-10-04T12:00:00+00:00",
               "completed_at": "2026-10-04T12:01:30+00:00", "elapsed_ms": 90000}]
    for event in events:
        CodexVideoAgent._record_event(answer, event)
    assert answer["started_at"] == events[0]["started_at"]
    assert answer["completed_at"] == events[-1]["completed_at"] and answer["elapsed_ms"] == 90000
    assert answer["artifacts"] == [artifact]


def test_http_chat_rehydrates_managed_timing_and_generated_document(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app, HEADERS
    from managed_test_support import attach_runtime
    app = make_app(tmp_path, monkeypatch)
    protocol, _ = attach_runtime(app.state.agent, actions=[{'name': 'create_file',
        'arguments': {'document': {'name': 'report.md', 'content': '# The report'}}}])
    with TestClient(app) as client:
        client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS)
        response = client.post('/api/chat', json={'chat_id': 'file-chat', 'dataset': 'cafe',
            'message': 'Make the report'}, headers=HEADERS)
        assert response.status_code == 200
        events = [json.loads(row) for row in response.text.splitlines()]
        assert events[-1]['status'] == 'completed', events
        assert {'timing', 'artifact', 'done'} <= {event['type'] for event in events}
        record = client.get('/api/chats/file-chat').json()
    answer = record['messages'][-1]
    assert answer['elapsed_ms'] >= 0 and answer['completed_at'] >= answer['started_at']
    assert answer['artifacts'][0]['chat_id'] == 'file-chat'
    assert protocol.tool_results[0]['success'] is True
    stored = json.loads((tmp_path / 'state/chats/file-chat.json').read_text())
    assert stored['messages'][-1]['artifacts'] == answer['artifacts']


def test_managed_tool_dispatch_emits_created_file_and_measured_turn_boundaries(tmp_path):
    from managed_test_support import attach_runtime
    from scope_host import bind_admission
    from host import Admission
    async def scenario():
        agent = CodexVideoAgent(tmp_path)
        agent.children['chat'] = {'id': 'chat', 'owner_principal_id': 'owner',
            'dataset': 'cafe', 'messages': []}
        client, _ = attach_runtime(agent, actions=[{'name': 'create_file', 'arguments': {
            'document': {'name': 'result.csv', 'content': 'finding,source\nObservation,clip\n'}}}])
        with bind_admission(Admission(lambda: True)):
            events = [event async for event in agent.turn('Create the result table', 'chat', 'cafe')]
        timing = next(event for event in events if event['type'] == 'timing')
        assert events[-1]['status'] == 'completed'
        assert events[-1]['started_at'] == timing['started_at']
        assert events[-1]['completed_at'] >= events[-1]['started_at'] and events[-1]['elapsed_ms'] >= 0
        artifact = next(event['artifact'] for event in events if event['type'] == 'artifact')
        assert artifact['chat_id'] == 'chat' and artifact['name'] == 'result.csv'
        assert agent._files().get(artifact['id'], owner_principal_id='owner', chat_id='chat') == artifact
        assert client.tool_results[0]['success'] is True
        assert agent.children['chat']['messages'][-1]['artifacts'] == [artifact]
    asyncio.run(scenario())
