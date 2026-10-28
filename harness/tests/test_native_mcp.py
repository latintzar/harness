"""Actual MCP transport proves the inherited tools retain host authorization."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent, tool_specs
from execution_policy import FRESH_INSPECTION_ADMISSION, RETRIEVAL_VIEW
from native_mcp import NativeMCP, NativeMCPScope, READ_TOOLS


def fixture():
    calls = []
    async def handler(name, arguments, scope, chat_id):
        calls.append((name, arguments, scope, chat_id, RETRIEVAL_VIEW.get(), FRESH_INSPECTION_ADMISSION.get()))
        if name == 'inspect_clip':
            return [{'type':'inputText', 'text':'{"clip":"sample"}'},
                    {'type':'inputImage', 'imageUrl':'data:image/png;base64,aW1hZ2U='}], None, True
        return [{'type':'inputText', 'text':json.dumps({'dataset':scope, 'chat':chat_id})}], None, True
    agent = SimpleNamespace(children={
        'root': {'owner_principal_id':'alice', 'dataset':'cafe'},
        'child': {'owner_principal_id':'alice', 'dataset':'cafe', 'parent_id':'root'},
        'other': {'owner_principal_id':'bob', 'dataset':'cafe'},
    }, registry={'root':'native-root', 'child':'native-child', 'other':'native-other'},
        active={name:{'cancel':asyncio.Event()} for name in ('root','child','other')},
        retrieval=object(), _scoped_tool=handler, automation_store=SimpleNamespace(
            agent_classes={'tracker':{'authority':'participant_reach'},'action':{'authority':'granted_effects'}}))
    scope = NativeMCPScope('rocoto', 'alice', 'cafe', 'root', 'native-root')
    return agent, scope, calls


@asynccontextmanager
async def client(app, token):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            headers={'Authorization':'Bearer ' + token}) as http:
        async with streamable_http_client('http://127.0.0.1:8766/mcp', http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def test_real_mcp_initialize_list_calls_images_and_canonical_schemas():
    async def scenario():
        agent, scope, calls = fixture()
        app = NativeMCP(agent, scope, company_id='rocoto')
        grant = app.issue()
        async with app.lifespan():
            async with client(app, grant.token) as session:
                listed = (await session.list_tools()).tools
                canonical = {spec['name']:spec for spec in tool_specs(False)}
                assert {tool.name for tool in listed} == READ_TOOLS.intersection(canonical)
                for tool in listed:
                    assert tool.inputSchema == canonical[tool.name]['inputSchema']
                result = await session.call_tool('list_cameras', {})
                assert not result.isError and json.loads(result.content[0].text)['chat'] == 'root'
                observed = await session.call_tool('list_tracker_events', {})
                assert not observed.isError and json.loads(observed.content[0].text)['chat']=='root'
                retired = await session.call_tool('inspect_clip', {'clip_id':'sample'})
                assert retired.isError and all(call[0]!='inspect_clip' for call in calls)
        assert calls[0][2:4] == ('cafe','root')
        assert grant.token not in repr(grant) and grant.token not in repr(app)
    asyncio.run(scenario())


def test_grants_capture_host_context_and_keep_concurrent_actors_separate():
    async def scenario():
        agent, scope, calls = fixture()
        app = NativeMCP(agent, scope, company_id='rocoto')
        snapshot, admission = object(), lambda _: None
        from host import Admission
        from scope_host import bind_admission
        @contextmanager
        def bind():
            first = RETRIEVAL_VIEW.set(snapshot)
            second = FRESH_INSPECTION_ADMISSION.set(admission)
            try:
                yield
            finally:
                RETRIEVAL_VIEW.reset(first)
                FRESH_INSPECTION_ADMISSION.reset(second)
        with bind_admission(Admission(lambda: True, bind)):
            parent = app.issue()
            child = app.issue(actor_chat_id='child', actor_thread_id='native-child')
        async def call(grant):
            async with client(app, grant.token) as session:
                return await session.call_tool('list_connections', {})
        async with app.lifespan():
            results = await asyncio.gather(call(parent), call(child))
        assert all(not result.isError for result in results)
        assert {call[3] for call in calls} == {'root','child'}
        assert all(call[4] is snapshot and call[5] is admission for call in calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('name,args', [
    ('create_file', {'document':{'name':'x.txt','content':'secret'}}),
    ('plan_investigation', {}), ('retrieve_investigation', {'plan_id':'arbitrary'}),
    ('cosmos_inspect', {}), ('request_custom_connector_credentials', {}),
    ('list_cameras', {'owner_principal_id':'bob'}),
    ('list_cameras', {'chat_id':'other'}),
    ('list_camera_context', {'dataset':'another-business'}),
    ('inspect_clip', {'clip_id':['private-input']}),
    ('read_connection', {'provider':'gmail','tool':'read','arguments':{},'thread_id':'native-other'}),
])
def test_root_writes_identity_injection_and_invalid_arguments_are_denied(name, args):
    async def scenario():
        agent, scope, calls = fixture()
        app = NativeMCP(agent, scope, company_id='rocoto')
        grant = app.issue()
        async with app.lifespan():
            async with client(app, grant.token) as session:
                result = await session.call_tool(name, args)
                assert result.isError and 'private-input' not in str(result)
        assert calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['company','owner','dataset','root_thread','actor_thread','unrelated','cycle'])
def test_issue_refuses_unverified_scope_or_descendant(change):
    agent, scope, _ = fixture()
    app = NativeMCP(agent, scope, company_id='rocoto')
    kwargs = {'actor_chat_id':'child','actor_thread_id':'native-child'}
    if change == 'company': app.company_id = 'another-company'
    if change == 'owner': agent.children['child']['owner_principal_id'] = 'bob'
    if change == 'dataset': agent.children['child']['dataset'] = 'other'
    if change == 'root_thread': agent.registry['root'] = 'different-thread'
    if change == 'actor_thread': kwargs['actor_thread_id'] = 'forged-thread'
    if change == 'unrelated': agent.children['child'].pop('parent_id')
    if change == 'cycle': agent.children['child']['parent_id'] = 'child'
    with pytest.raises(ValueError, match='active workspace'):
        app.issue(**kwargs)


def test_missing_wrong_expired_revoked_tokens_and_http_boundary():
    async def scenario():
        agent, scope, calls = fixture()
        now = [100.0]
        app = NativeMCP(agent, scope, company_id='rocoto', clock=lambda:now[0])
        grant = app.issue(ttl_seconds=5)
        async with app.lifespan():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8766') as http:
                for token in ['', 'wrong-token']:
                    response = await http.post('/mcp', headers={'Authorization':'Bearer '+token}, json={})
                    assert response.status_code == 401
                headers = {'Authorization':'Bearer '+grant.token}
                assert (await http.post('/mcp?token='+grant.token, headers=headers)).status_code == 404
                assert (await http.post('/mcp', headers={**headers,'Origin':'https://untrusted.example'})).status_code == 403
                now[0] = 106.0
                assert (await http.post('/mcp', headers=headers)).status_code == 401
                fresh = app.issue()
                app.revoke(fresh.id)
                assert (await http.post('/mcp', headers={'Authorization':'Bearer '+fresh.token})).status_code == 401
        assert not calls
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['owner','dataset','native_thread','root_turn','stop','revoke'])
def test_scope_changes_and_stop_revoke_existing_grants(change):
    async def scenario():
        agent, scope, calls = fixture()
        app = NativeMCP(agent, scope, company_id='rocoto')
        grant = app.issue(actor_chat_id='child', actor_thread_id='native-child')
        if change == 'owner': agent.children['root']['owner_principal_id'] = 'bob'
        if change == 'dataset': agent.children['child']['dataset'] = 'other'
        if change == 'native_thread': agent.registry['child'] = 'new-thread'
        if change == 'root_turn': agent.active['root'] = {'cancel':asyncio.Event()}
        if change == 'stop': agent.active['root']['cancel'].set()
        if change == 'revoke': app.revoke(grant.id)
        async with app.lifespan():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
                result = await http.post('http://127.0.0.1:8766/mcp', headers={'Authorization':'Bearer '+grant.token})
                assert result.status_code == 401
        assert calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize('stop', ['revoke','parent_stop'])
def test_inflight_read_is_cancelled_and_late_result_withheld(stop):
    async def scenario():
        agent, scope, calls = fixture()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def handler(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return [{'type':'inputText','text':'late-private-result'}], None, True
        agent._scoped_tool = handler
        app = NativeMCP(agent, scope, company_id='rocoto')
        grant = app.issue(actor_chat_id='child', actor_thread_id='native-child')
        async with app.lifespan():
            async with client(app, grant.token) as session:
                pending = asyncio.create_task(session.call_tool('list_cameras', {}))
                await asyncio.wait_for(started.wait(), 2)
                if stop == 'revoke': app.revoke(grant.id)
                else: agent.active['root']['cancel'].set()
                result = await asyncio.wait_for(pending, 2)
                assert result.isError and 'late-private-result' not in str(result)
                assert cancelled.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize('raises', [True, False])
def test_handler_error_never_echoes_secret_or_host_path(raises):
    async def scenario():
        agent, scope, _ = fixture()
        async def handler(*args):
            if raises: raise RuntimeError('provider-key-at-/private/host')
            return [{'type':'inputText','text':'provider-key-at-/private/host'}], None, False
        agent._scoped_tool = handler
        app = NativeMCP(agent, scope, company_id='rocoto')
        grant = app.issue()
        async with app.lifespan():
            async with client(app, grant.token) as session:
                result = await session.call_tool('list_cameras', {})
                assert result.isError and 'provider-key' not in str(result) and '/private/host' not in str(result)
    asyncio.run(scenario())


def test_existing_native_handler_keeps_dataset_and_connection_owner(tmp_path):
    async def scenario():
        agent, scope, _ = fixture()
        agent.retrieval = SimpleNamespace(get_clip=lambda _: {'dataset':'another-business'})
        agent.corpus = lambda:[{'id':'mine','dataset':'cafe'}, {'id':'theirs','dataset':'another-business'}]
        owners = []
        def catalogue(owner):
            owners.append(owner)
            return [{'id':'gmail','owner':owner}]
        agent._connections = lambda:SimpleNamespace(catalogue=catalogue)
        from tool_test_support import retail_agent
        actual = CodexVideoAgent(tmp_path, agent.retrieval, agent.corpus, tool_only=True)
        actual.children, actual.registry, actual.active = agent.children, agent.registry, agent.active
        actual._connections = agent._connections
        agent = retail_agent(actual)
        app = NativeMCP(agent, scope, company_id='rocoto')
        grant = app.issue(actor_chat_id='child', actor_thread_id='native-child')
        async with app.lifespan():
            async with client(app, grant.token) as session:
                cameras = await session.call_tool('list_cameras', {})
                assert json.loads(cameras.content[0].text) == [{'id':'mine','dataset':'cafe'}]
                connections = await session.call_tool('list_connections', {})
                assert json.loads(connections.content[0].text)[0]['owner'] == 'alice'
                foreign = await session.call_tool('inspect_clip', {'clip_id':'other-owner-clip'})
                assert foreign.isError
        assert owners == ['alice']
    asyncio.run(scenario())


def test_real_mcp_client_over_unix_socket(tmp_path):
    """Same socket/path/header transport consumed by the image-owned relay."""
    import uvicorn
    async def scenario():
        agent, scope, calls = fixture()
        app = NativeMCP(agent, scope, company_id='rocoto')
        grant = app.issue()
        socket = tmp_path / 'mcp.sock'
        config = uvicorn.Config(app, uds=str(socket), log_level='critical', lifespan='on', access_log=False)
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve())
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done(): await serving
                    await asyncio.sleep(0.01)
            socket.chmod(0o600)
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)),
                    headers={'Authorization':'Bearer '+grant.token}) as http:
                async with streamable_http_client('http://127.0.0.1:8766/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        info = await session.initialize()
                        assert info.serverInfo.name == 'rocoto-workspace'
                        assert (await session.list_tools()).tools
                        result = await session.call_tool('list_cameras', {})
                        assert not result.isError
            assert calls[0][2:4] == ('cafe','root')
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
        assert not app._bindings
    asyncio.run(scenario())


def test_argument_result_and_concurrency_bounds():
    async def scenario():
        agent, scope, calls = fixture()
        app = NativeMCP(agent, scope, company_id='rocoto')
        grant = app.issue()
        async with app.lifespan():
            async with client(app, grant.token) as session:
                oversized = await session.call_tool('search_video', {'query':'x'*(129*1024)})
                assert oversized.isError and calls == []
                started, release = asyncio.Event(), asyncio.Event()
                async def handler(*args):
                    calls.append(args)
                    if len(calls) == 8: started.set()
                    await release.wait()
                    return [{'type':'inputText','text':'ok'}], None, True
                agent._scoped_tool = handler
                pending = [asyncio.create_task(session.call_tool('list_cameras', {})) for _ in range(8)]
                await asyncio.wait_for(started.wait(), 2)
                busy = await session.call_tool('list_cameras', {})
                assert busy.isError and len(calls) == 8
                release.set()
                assert all(not result.isError for result in await asyncio.gather(*pending))
        malformed = NativeMCP._result([{'type':'inputImage','imageUrl':'https://private/image'}])
        assert malformed.isError
    asyncio.run(scenario())
