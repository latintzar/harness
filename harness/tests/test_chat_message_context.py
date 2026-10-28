"""Host-added context reaches the model while user history stays verbatim."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent_bridge as bridge
from test_skill_runtime import HEADERS, make_app


def capture_native(monkeypatch, agent):
    calls = []
    async def native(message, chat_id, dataset, *args, **kwargs):
        calls.append({"message": message, "chat_id": chat_id, "dataset": dataset, **kwargs})
        yield {"type": "delta", "text": "The answer."}
        yield {"type": "done", "status": "completed"}
    monkeypatch.setattr(agent, "_turn", native)
    return calls


def test_http_new_and_continued_messages_keep_exact_user_text(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    calls = capture_native(monkeypatch, app.state.agent)
    messages = ["Show me the queue.\nKeep the timestamps.",
                "Explain the phrase Selected source scope: cafe. Limit searches to this dataset."]
    with TestClient(app) as client:
        client.post("/api/session", json={"code": app.state.access.issue("demo")}, headers=HEADERS)
        for message in messages:
            response = client.post("/api/chat", json={"chat_id": "queue-chat", "dataset": "cafe",
                "message": message, "model_context": "A browser must not supply host context."}, headers=HEADERS)
            assert response.status_code == 200
        record = client.get("/api/chats/queue-chat").json()
    assert [row["content"] for row in record["messages"] if row["role"] == "user"] == messages
    assert [call["message"] for call in calls] == messages
    assert all(call["dataset"] == "cafe" for call in calls)
    assert all(call["model_context"] == "Selected source scope: cafe. Limit searches to this dataset."
               for call in calls)
    restored = bridge.CodexVideoAgent(tmp_path / "state", app.state.index, lambda: {})
    assert restored.children["queue-chat"]["messages"] == record["messages"]


def test_http_linked_followup_keeps_report_in_model_context(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    agent = app.state.agent
    calls = capture_native(monkeypatch, agent)
    report_messages = [{"role": "assistant", "content": "Observed cup handoff",
                        "results": [{"id": "demo-clip"}]}]
    with TestClient(app) as client:
        session = client.post("/api/session", json={"code": app.state.access.issue("demo")}, headers=HEADERS).json()
        store = agent.automation_store
        definition = {"title": "Service review", "instruction": "Review visible service.",
                      "dataset": "cafe", "mode": "ad_hoc", "fresh_inspections_per_run": 0}
        saved = store.create(definition, ["cafe"], owner_principal_id=session["principal"]["id"])
        store.activate(saved["id"], time.time() + 1000)
        run = store.claim(saved["id"], {"source": "demo-clip"})
        store.finish(run["id"], "completed", report_messages=report_messages)
        prior = {"id": run["chat_id"], "kind": "agent_run", "dataset": "cafe", "running": False,
                 "agent_id": saved["id"], "run_id": run["id"], "messages": report_messages,
                 "owner_principal_id": session["principal"]["id"]}
        agent.children[prior["id"]] = prior
        agent._save_child(prior)
        response = client.post("/api/chat", json={"chat_id": "followup", "dataset": "cafe",
            "message": "Show the handoff", "followup_run_chat_id": prior["id"]}, headers=HEADERS)
        assert response.status_code == 200
        record = client.get("/api/chats/followup").json()
        assert record["messages"][0]["content"] == "Show the handoff"
        assert "Observed cup handoff" not in json.dumps(record)
        assert client.get(f"/api/chats/{prior['id']}").json()["messages"] == report_messages
    assert calls[0]["message"] == "Show the handoff"
    assert "Observed cup handoff" in calls[0]["model_context"]
    assert "Selected source scope: cafe." in calls[0]["model_context"]
    assert store.run_report(saved["id"], run["id"])["report_messages"] == report_messages


def test_managed_input_keeps_context_separate_and_retains_host_scope(tmp_path):
    from managed_test_support import attach_runtime
    from scope_host import bind_admission
    from host import Admission
    async def scenario():
        agent = bridge.HarnessAgent(tmp_path)
        agent.children['chat'] = {'id': 'chat', 'owner_principal_id': 'owner', 'dataset': 'cafe', 'messages': []}
        readiness = {'recurring_agents': {'ready': False}, 'footage': {'search_ready': True}}
        setup = {'template': {'id': 'queue-watch', 'instructions': 'Compare periods'}}
        agent.capability_snapshot = lambda *args: readiness
        agent.agent_setup_context = lambda *args: setup
        client, _ = attach_runtime(agent)
        context = 'Selected source scope: cafe. Limit searches to this dataset.'
        with bind_admission(Admission(lambda: True)):
            events = [event async for event in agent.turn('My exact question', 'chat', 'cafe', model_context=context)]
        assert events[-1]['status'] == 'completed'
        content = client.created[0][0]['input'][0]['content']
        assert content[0]['text'] == 'My exact question'
        assert content[1]['text'] == context
        assert json.loads(content[2]['text'].split('\n', 1)[1]) == readiness
        assert json.loads(content[3]['text'].split('\n', 1)[1]) == setup
        assert agent.children['chat']['messages'][0]['content'] == 'My exact question'
        assert agent.children['chat']['dataset'] == 'cafe'
        denied = [event async for event in agent._turn('Change collection', 'chat', 'warehouse')]
        assert denied[0]['type'] == 'error' and agent.children['chat']['dataset'] == 'cafe'
        assert len(client.created) == 1 and not client.sent
    asyncio.run(scenario())
