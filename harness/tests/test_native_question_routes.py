"""Ordinary native replies can target only the authenticated parent's question."""
import asyncio

from fastapi.testclient import TestClient

from test_native_questions import NativeQuestions, opened
from test_server_access import fixture_app, FixtureAgent
from test_workflow_routes import HEADERS, login


def test_question_reply_route_owner_observer_parent_validation_and_resolution(tmp_path, monkeypatch):
    calls = []
    store = NativeQuestions(tmp_path)
    record = opened(store)
    async def respond(self, chat_id, request_id, *, owner, action, answers=None):
        async def deliver(native_id, payload):
            calls.append((native_id, payload))
        return await store.answer(request_id, owner=owner, root_chat_id=chat_id, generation="run:1",
            action=action, answers=answers, deliver=deliver, cancelled=asyncio.Event())
    monkeypatch.setattr(FixtureAgent, "resolve_native_question", respond, raising=False)
    app = fixture_app(tmp_path, monkeypatch)
    app.state.agent.children["root"] = {"id":"root", "dataset":"cafe", "owner_principal_id":"alice",
        "parent_id":None, "title":"Question", "messages":[]}
    app.state.agent.children["child"] = {**app.state.agent.children["root"], "id":"child", "parent_id":"root"}
    path = f"/api/chats/root/questions/{record['id']}"
    body = {"action":"submit", "answers":{"format":["PDF"]}}
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as observer:
        login(alice, app, "alice"); login(bob, app, "bob"); login(observer, app, "owner", owner=True)
        assert alice.post(path, json=body).status_code == 403
        assert bob.post(path, json=body, headers=HEADERS).status_code == 403
        assert alice.post(path.replace("/root/", "/child/"), json=body, headers=HEADERS).status_code == 403
        observer.post("/api/observer", json={"environment_id":"demo", "principal_id":"alice"}, headers=HEADERS)
        assert observer.post(path, json=body, headers=HEADERS).status_code == 403
        for invalid in ({"action":"submit", "answers":{}}, {**body,"native_request_id":0},
                {"action":"skip","answers":{"format":["PDF"]}}, [body]):
            assert alice.post(path, json=invalid, headers=HEADERS).status_code == 400
        assert not calls
        result = alice.post(path, json=body, headers=HEADERS)
        assert result.status_code == 200 and result.json()["request"]["status"] == "submitted"
        assert result.headers["cache-control"] == "private, no-store"
        assert "native_request_id" not in result.text
        assert alice.post(path, json=body, headers=HEADERS).status_code == 200
        assert len(calls) == 1
        later = opened(store, native_request_id=1)
        store.retire_generation("root", "run:1", "stopped")
        stale = alice.post(f"/api/chats/root/questions/{later['id']}", json=body, headers=HEADERS)
        assert stale.status_code == 409 and stale.json()["request"]["status"] == "interrupted"
        assert alice.post(path, content=b"x"*65537, headers=HEADERS).status_code == 413


def test_timeout_route_uses_host_deadline_and_exact_body(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    store=NativeQuestions(tmp_path);record=opened(store);sent=[]
    async def respond(self,chat_id,request_id,*,owner,action,answers=None):
        async def deliver(native_id,payload):sent.append(payload)
        return await store.answer(request_id,owner=owner,root_chat_id=chat_id,generation='run:1',action=action,
            answers=answers,deliver=deliver,cancelled=asyncio.Event())
    monkeypatch.setattr(FixtureAgent,'resolve_native_question',respond,raising=False)
    app=fixture_app(tmp_path,monkeypatch)
    app.state.agent.children['root']={'id':'root','dataset':'cafe','owner_principal_id':'alice','messages':[]}
    path=f"/api/chats/root/questions/{record['id']}"
    with TestClient(app) as client:
        login(client,app,'alice')
        assert client.post(path,json={'action':'timeout'},headers=HEADERS).status_code==400
        assert client.post(path,json={'action':'timeout','answers':{}},headers=HEADERS).status_code==400
        assert sent==[]
        with store._db() as db:db.execute('UPDATE questions SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat(),record['id']))
        response=client.post(path,json={'action':'timeout'},headers=HEADERS)
        assert response.status_code==200 and response.json()['request']['status']=='timed_out'
        assert sent==[{'answers':{}}]
        assert client.post(path,json={'action':'timeout'},headers=HEADERS).status_code==200 and len(sent)==1
