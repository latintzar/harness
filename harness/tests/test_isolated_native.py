"""Opt-in adapter lifecycle with real private listeners, funded mock upstream."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys

import httpx
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from deploy.migration.legacy.isolated_native import IsolatedNativeLauncher, IsolatedLaunchError
from agent_auth import AgentAuth
from native_executor import NativeScope, NativeLease, ExecutorError
from openai_gateway import ResponsesGateway
from test_openai_gateway import fund

SCOPE=NativeScope('demo','alice','parent-chat')

class Broker:
    def __init__(self,tmp_path):
        self.path=tmp_path/'sockets';self.events=[];self.started=0;self.stopped=0;self.failure=None;self.stop_check=None
    async def prepare(self,scope):
        self.path.mkdir(mode=0o700)
        self.events.append('prepare')
        return NativeLease(scope.key,1,'b'*48,str(self.path))
    async def start(self,scope,lease,capability):
        self.events.append('start');self.started+=1
        assert {p.name for p in self.path.iterdir()}=={'model.sock','mcp.sock'}
        assert all(p.is_socket() for p in self.path.iterdir())
        assert len(capability)>=32
        if self.failure=='start':raise ExecutorError('fixture_start_failed')
        stderr=asyncio.StreamReader();stderr.feed_data(b'untrusted native stderr');stderr.feed_eof()
        async def stop():return await self.stop(scope,lease)
        return SimpleNamespace(stderr=stderr,stop=stop)
    async def stop(self,scope,lease):
        self.events.append('stop');self.stopped+=1
        if self.stop_check:await self.stop_check()
        if self.failure=='stop':raise ExecutorError('failed_stop_fence')
        return {'status':'stopped','generation':lease.generation}


class MCP:
    def __init__(self,scope=SCOPE):
        self.scope=SimpleNamespace(company_id=scope.company_id,owner_principal_id=scope.owner_id,root_chat_id=scope.root_chat_id,root_thread_id='verified-native-root')
        self.revoked=False;self.opened=False;self.closed=False
    def issue(self,ttl_seconds):
        assert ttl_seconds==1800
        return SimpleNamespace(token='synthetic-mcp-grant')
    async def provider_configs(self):return []
    def revoke_all(self):self.revoked=True
    @asynccontextmanager
    async def lifespan(self):
        self.opened=True
        try:yield
        finally:self.closed=True
    async def __call__(self,scope,receive,send):
        await send({'type':'http.response.start','status':401 if self.revoked else 200,'headers':[]})
        await send({'type':'http.response.body','body':b'fixture'})


def auth(tmp_path):
    fund(tmp_path)
    return AgentAuth(tmp_path,mode='openai_api',environ={'ROCOTO_OPENAI_API_KEY':'private-provider'},runtime_host='rocoto-staging')


@pytest.mark.anyio
async def test_adapter_opt_in_and_company_binding_precede_broker_admission(tmp_path):
    broker=Broker(tmp_path)
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',client=broker)
    with pytest.raises(IsolatedLaunchError,match='disabled'):
        async with launcher.lease(SCOPE,run_id='r'):pass
    launcher.enabled=True
    with pytest.raises(IsolatedLaunchError,match='scope_mismatch'):
        async with launcher.lease(replace(SCOPE,company_id='other'),run_id='r'):pass
    assert broker.events==[]


@pytest.mark.anyio
async def test_empty_mcp_bootstrap_bind_once_and_stop_revokes_before_container_removal(tmp_path):
    broker=Broker(tmp_path);mcp=MCP();launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=broker)
    async with launcher.lease(SCOPE,run_id='r') as session:
        transport=httpx.AsyncHTTPTransport(uds=str(broker.path/'mcp.sock'))
        async with httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1:8766') as http:
            assert (await http.post('/mcp',json={})).status_code==503
            config=await session.bind_mcp(mcp)
            assert config['value']['rocoto']['url']=='http://127.0.0.1:8766/mcp'
            assert config['value']['rocoto']['http_headers']=={'Authorization':'Bearer synthetic-mcp-grant'}
            assert (await http.post('/mcp',json={})).status_code==200
            with pytest.raises(IsolatedLaunchError,match='binding_closed'):await session.bind_mcp(MCP())
            params=session.thread_params('Rocoto instructions.',[])
            assert 'baseInstructions' not in params and params['cwd']=='/workspace'
            assert params['config']['model_providers.rocoto_metered.base_url']=='http://127.0.0.1:8765/v1'
            async def stopped():
                assert session.launch.gateway.token=='' and mcp.revoked
            broker.stop_check=stopped
            # User Stop can arrive on a different task without exiting another
            # task's ContextVar/AnyIO context managers.
            await asyncio.create_task(session.stop())
            assert session.stopped
    assert mcp.closed and broker.stopped==1 and not any(broker.path.iterdir())


@pytest.mark.anyio
async def test_wrong_owner_mcp_does_not_attach_or_issue_grant(tmp_path):
    broker=Broker(tmp_path);launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=broker)
    async with launcher.lease(SCOPE,run_id='r') as session:
        mcp=MCP(replace(SCOPE,owner_id='bob'))
        with pytest.raises(IsolatedLaunchError,match='scope_mismatch'):await session.bind_mcp(mcp)
        assert not mcp.opened and session.mcp is None


@pytest.mark.anyio
async def test_failed_worker_start_closes_model_socket_and_reconciles_prepared_generation(tmp_path):
    broker=Broker(tmp_path);broker.failure='start';captured=[]
    def gateway(*args,**kwargs):
        value=ResponsesGateway(*args,**kwargs);captured.append(value);return value
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=broker,gateway_factory=gateway)
    with pytest.raises(ExecutorError,match='start_failed'):
        async with launcher.lease(SCOPE,run_id='r'):pass
    assert broker.stopped==1 and captured[0].token=='' and captured[0]._api_key==''
    assert not any(broker.path.iterdir())


@pytest.mark.anyio
async def test_failed_actual_stop_surfaces_fence_after_revocation(tmp_path):
    broker=Broker(tmp_path);broker.failure='stop';mcp=MCP()
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=broker)
    with pytest.raises(ExecutorError,match='failed_stop_fence'):
        async with launcher.lease(SCOPE,run_id='r') as session:
            await session.bind_mcp(mcp)
    assert mcp.revoked and mcp.closed and session.launch.gateway.token==''
    assert not session.stopped


@pytest.mark.anyio
async def test_disconnected_consumer_cleans_up_native_worker(tmp_path):
    broker=Broker(tmp_path);mcp=MCP();ready=asyncio.Event()
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=broker)
    async def consumer():
        async with launcher.lease(SCOPE,run_id='r') as session:
            await session.bind_mcp(mcp);ready.set();await asyncio.Event().wait()
    task=asyncio.create_task(consumer());await ready.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert broker.stopped==1 and mcp.revoked and mcp.closed and not any(broker.path.iterdir())


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.mark.anyio
async def test_stop_during_mcp_binding_cannot_publish_late_grant(tmp_path):
    broker=Broker(tmp_path);ready=asyncio.Event();release=asyncio.Event()
    class SlowMCP(MCP):
        @asynccontextmanager
        async def lifespan(self):
            ready.set();await release.wait()
            try:yield
            finally:self.closed=True
    mcp=SlowMCP();launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=broker)
    async with launcher.lease(SCOPE,run_id='r') as session:
        async def stop_after_binding_begins():
            await ready.wait();await session.stop();release.set()
        stopping=asyncio.create_task(stop_after_binding_begins())
        with pytest.raises(IsolatedLaunchError,match='binding_closed'):await session.bind_mcp(mcp)
        await stopping
        assert mcp.revoked and session.listener.app is None
        with pytest.raises(IsolatedLaunchError,match='session_closed'):session.thread_params('rules',[])


@pytest.mark.anyio
async def test_real_mcp_transport_lifespan_and_host_scope_on_actual_uds(tmp_path):
    from native_mcp import NativeMCP,NativeMCPScope
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import json
    calls=[]
    async def handler(name,args,dataset,chat_id):
        calls.append((name,dataset,chat_id))
        return [{'type':'inputText','text':json.dumps({'camera':'front'})}],None,True
    agent=SimpleNamespace(children={'parent-chat':{'owner_principal_id':'alice','dataset':'cafe'}},
        retrieval=SimpleNamespace(),registry={'parent-chat':'native-root'},active={'parent-chat':{'cancel':asyncio.Event()}},_tool=handler)
    mcp=NativeMCP(agent,NativeMCPScope('demo','alice','cafe','parent-chat','native-root'),company_id='demo')
    broker=Broker(tmp_path);launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=broker)
    async with launcher.lease(SCOPE,run_id='r') as session:
        config=await session.bind_mcp(mcp)
        headers=config['value']['rocoto']['http_headers']
        transport=httpx.AsyncHTTPTransport(uds=str(broker.path/'mcp.sock'))
        async with httpx.AsyncClient(transport=transport,headers=headers) as http:
            async with streamable_http_client('http://127.0.0.1:8766/mcp',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    assert 'list_cameras' in {tool.name for tool in (await client.list_tools()).tools}
                    result=await client.call_tool('list_cameras',{})
                    assert not result.isError and json.loads(result.content[0].text)=={'camera':'front'}
    assert calls==[('list_cameras','cafe','parent-chat')]
    assert broker.stopped==1 and not mcp._running
