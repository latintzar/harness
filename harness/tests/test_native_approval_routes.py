"""Only the authenticated parent can decide an exact native provider intent."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_native_approvals import ARGS, BINDING, SCOPE, NativeApprovals, NativeApprovalRouter, open_request
from test_server_access import fixture_app, FixtureAgent
from test_workflow_routes import HEADERS, login
from agent_bridge import CodexVideoAgent
from workspace_identity import workspace_scope


def test_approval_route_owner_observer_body_and_idempotency(tmp_path, monkeypatch):
    store = NativeApprovals(tmp_path)
    approval = open_request(store)
    calls = []
    async def respond(self, chat_id, request_id, *, owner, action):
        assert owner == 'alice' and chat_id == 'root'
        async def deliver(native_id, payload, guard):
            if not guard():
                raise RuntimeError('closed')
            calls.append((native_id, payload))
        return await store.answer(request_id, scope=SCOPE, binding=BINDING, action=action,
            deliver=deliver, cancelled=asyncio.Event())
    monkeypatch.setattr(FixtureAgent, 'resolve_native_approval', respond, raising=False)
    app = fixture_app(tmp_path, monkeypatch)
    app.state.agent.children['root'] = {'id':'root', 'dataset':'cafe', 'owner_principal_id':'alice',
        'parent_id':None, 'title':'Document', 'messages':[]}
    app.state.agent.children['child'] = {**app.state.agent.children['root'], 'id':'child', 'parent_id':'root'}
    path = f"/api/chats/root/approvals/{approval['id']}"
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as observer:
        login(alice, app, 'alice'); login(bob, app, 'bob'); login(observer, app, 'owner', owner=True)
        assert alice.post(path, json={'action':'allow_once'}).status_code == 403
        assert bob.post(path, json={'action':'allow_once'}, headers=HEADERS).status_code == 403
        assert alice.post(path.replace('/root/', '/child/'), json={'action':'allow_once'}, headers=HEADERS).status_code == 403
        observer.post('/api/observer', json={'environment_id':'demo','principal_id':'alice'}, headers=HEADERS)
        assert observer.post(path, json={'action':'allow_once'}, headers=HEADERS).status_code == 403
        for body in ({'action':'accept'}, {'action':'allow_once','native_id':0}, {'action':'allow_once','arguments':ARGS}, [], {}):
            assert alice.post(path, json=body, headers=HEADERS).status_code == 400
        assert alice.post(path, content=b'x'*4097, headers=HEADERS).status_code == 413
        assert calls == []
        for _ in range(2):
            response = alice.post(path, json={'action':'allow_once'}, headers=HEADERS)
            assert response.status_code == 200 and response.json()['request']['status'] == 'approved'
            assert response.headers['cache-control'] == 'private, no-store'
        assert calls == [(0, {'action':'accept','content':{}})]
        assert alice.post(path, json={'action':'decline'}, headers=HEADERS).status_code == 400
        expired = open_request(store, native_request_id=2, call_id='call-2')
        store.invalidate(scope=SCOPE, reason='stopped')
        response = alice.post(path.replace(approval['id'], expired['id']), json={'action':'allow_once'}, headers=HEADERS)
        assert response.status_code == 409 and response.json()['request']['status'] == 'cancelled'
        assert len(calls) == 1


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.mark.anyio
async def test_bridge_persists_decision_and_reads_closed_history(tmp_path):
    agent = CodexVideoAgent(tmp_path, SimpleNamespace(), lambda: [])
    scope = replace(SCOPE, company_id=workspace_scope(tmp_path))
    approval = open_request(agent.native_approvals, scope=scope)
    agent.children['root'] = {'id':'root','owner_principal_id':'alice','messages':[{'role':'assistant','content':'','tools':[]}]}
    agent._record_event(agent.children['root']['messages'][0], {'type':'approval_request','request':approval})
    sent = []
    async def resolve(server, tool): return BINDING
    async def write(value, guard=None):
        if guard and not guard(): raise RuntimeError('closed')
        sent.append(value)
    cancelled = asyncio.Event()
    router = NativeApprovalRouter(agent.native_approvals, scope, resolve_binding=resolve, cancelled=cancelled, write=write)
    agent.active['root'] = {'approval_router':router,'generation':scope.generation,'cancel':cancelled,'tool_events':asyncio.Queue()}
    response = await agent.resolve_native_approval('root', approval['id'], owner='alice', action='decline')
    assert response['status'] == 'declined'
    assert agent.active['root']['tool_events'].get_nowait()['type'] == 'approval_resolved'
    agent.refresh_native_approvals('root')

    assert agent.children['root']['messages'][0]['native_approvals'][0]['status'] == 'declined'
    assert sent == [{'id':0,'result':{'action':'decline','content':None}}]
    with pytest.raises(ValueError):
        await agent.resolve_native_approval('root', approval['id'], owner='bob', action='allow_once')
    del agent.active['root']
    pending = open_request(agent.native_approvals, scope=scope, native_request_id=3, call_id='call-3')
    closed = await agent.resolve_native_approval('root', pending['id'], owner='alice', action='allow_once')
    assert closed['status'] == 'cancelled' and closed['resolution_reason'] == 'runtime_ended'
    assert len(sent) == 1
    agent.native_approvals = None  # auxiliary tool-only reader refresh is inert
    agent.refresh_native_approvals('root')


@pytest.mark.anyio
async def test_disconnect_closes_own_pending_approvals_and_wakes_native_wait(tmp_path):
    agent = CodexVideoAgent(tmp_path, SimpleNamespace(), lambda: [])
    sent = []; records = {}
    for chat_id, owner in [('root','alice'), ('closed','alice'), ('bob-root','bob')]:
        scope = replace(SCOPE, company_id=workspace_scope(tmp_path), owner=owner,
                        root_chat_id=chat_id, root_thread_id='thread-'+chat_id)
        approval = open_request(agent.native_approvals, scope=scope, actor_chat_id=chat_id)
        records[chat_id] = (scope, approval)
        agent.children[chat_id] = {'id':chat_id, 'owner_principal_id':owner,
            'messages':[{'role':'assistant','content':'','native_approvals':[approval]}]}
        if chat_id == 'closed':continue
        async def resolve(server, tool): return BINDING
        async def write(value, guard=None):
            if guard and not guard():raise RuntimeError('closed')
            sent.append(value)
        cancelled = asyncio.Event()
        router = NativeApprovalRouter(agent.native_approvals, scope, resolve_binding=resolve,
            cancelled=cancelled, write=write)
        agent.active[chat_id] = {'approval_router':router, 'generation':scope.generation,
            'cancel':cancelled, 'tool_events':asyncio.Queue()}
    await agent.revoke_provider_approvals(BINDING.provider, owner='alice')
    await agent.revoke_provider_approvals(BINDING.provider, owner='alice')
    for chat_id, (scope, approval) in records.items():
        expected = 'pending' if chat_id=='bob-root' else 'cancelled'
        assert agent.native_approvals.get(approval['id'], scope=scope)['status'] == expected
        assert agent.children[chat_id]['messages'][0]['native_approvals'][0]['status'] == expected
    assert sent == [{'id':0, 'result':{'action':'cancel','content':None}}]
    assert agent.active['root']['tool_events'].get_nowait()['type']=='approval_resolved'
    assert agent.active['bob-root']['tool_events'].empty()
