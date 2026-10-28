"""Original remote schemas through actual native MCP with synthetic provider IO."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys

import pytest
from mcp import types

sys.path.insert(0,str(Path(__file__).parents[1]))
from native_provider_relay import NativeProviderRelay,ProviderRelayError
from native_packages import NativePackageStore
from mcp_connections import MCPConnections,ConnectionError
from native_mcp import NativeMCP,NativeMCPScope
from test_native_packages import package,snapshot,Fake
from test_native_mcp import fixture as native_fixture,client

@pytest.fixture
def anyio_backend():return 'asyncio'

class Provider:
    def __init__(self):
        self.calls=[];self.entered=asyncio.Event();self.release=None;self.list_gate=None
        self.tool=types.Tool(name='notion-search',title='Search your workspace',description='Exact provider description.',
            inputSchema={'type':'object','properties':{'query':{'type':'string'},'dataset':{'type':'string'}},'required':['query'],'additionalProperties':False},
            outputSchema={'type':'object','properties':{'hits':{'type':'array'}},'required':['hits']},
            annotations=types.ToolAnnotations(title='Original title',readOnlyHint=True,destructiveHint=False,openWorldHint=True),
            icons=[types.Icon(src='https://provider.example/icon.png')],_meta={'provider_marker':'original-tool'})
    async def initialize(self):pass
    async def list_tools(self,cursor=None):
        if self.list_gate:await self.list_gate.wait()
        return types.ListToolsResult(tools=[self.tool,
            types.Tool(name='delete_all',description='Dangerous despite hint',inputSchema={'type':'object'},annotations=types.ToolAnnotations(readOnlyHint=True))])
    async def call_tool(self,name,arguments):
        self.calls.append((name,arguments));self.entered.set()
        if self.release:await self.release.wait()
        return types.CallToolResult(content=[types.TextContent(type='text',text='Exact provider result.')],structuredContent={'hits':['one']},isError=False,_meta={'provider_marker':'original-result'})

async def setup(tmp_path,package,snapshot,monkeypatch):
    metadata=deepcopy(snapshot);metadata['plugins'][0]['mcp_servers']=[{'name':'notion','url':'https://mcp.notion.com/mcp','transport':'http'}]
    store=NativePackageStore(tmp_path/'state',catalogue_snapshot=metadata,materializer=Fake(package))
    await store.install('notion',principal_id='alice');selection=store.snapshot(principal_id='alice')
    broker=MCPConnections(store.state,public_origin='https://rocoto.example')
    generation=broker._begin('notion','alice');broker._update('notion','alice',generation,status='connected')
    remote=Provider()
    @asynccontextmanager
    async def session(provider,principal,current,flow=None):
        assert provider=='notion' and principal=='alice'
        yield remote
    monkeypatch.setattr(broker,'_session',session)
    relay=NativeProviderRelay(store,broker,selection,company_id=store.company_scope,principal_id='alice')
    agent,_,calls=native_fixture();scope=NativeMCPScope(store.company_scope,'alice','cafe','root','native-root')
    app=NativeMCP(agent,scope,company_id=scope.company_id,provider_relay=relay)
    return store,broker,selection,remote,relay,app


@pytest.mark.anyio
async def test_native_provider_transport_preserves_original_tool_and_result(tmp_path,package,snapshot,monkeypatch):
    store,broker,selection,remote,relay,app=await setup(tmp_path,package,snapshot,monkeypatch)
    grant=await app.issue_provider('notion');base=app.issue()
    async with app.lifespan():
        async with client(app,grant.token) as session:
            tools=(await session.list_tools()).tools
            assert len(tools)==1 and tools[0].model_dump(by_alias=True)==remote.tool.model_dump(by_alias=True) and tools[0].meta==remote.tool.meta
            result=await session.call_tool('notion-search',{'query':'invoices','dataset':'external-provider-dataset'})
            assert result.structuredContent=={'hits':['one']} and result.content[0].text=='Exact provider result.'
            assert not result.isError and result.meta=={'provider_marker':'original-result'}
            denied=await session.call_tool('delete_all',{});assert denied.isError
        async with client(app,base.token) as session:
            tools=(await session.list_tools()).tools
            assert 'list_cameras' in {t.name for t in tools} and 'notion-search' not in {t.name for t in tools}
            assert (await session.call_tool('notion-search',{'query':'scope bypass'})).isError
    assert remote.calls==[('notion-search',{'query':'invoices','dataset':'external-provider-dataset'})]
    assert grant.token not in repr(grant)


@pytest.mark.anyio
async def test_private_config_keeps_original_server_name_and_host_only_token(tmp_path,package,snapshot,monkeypatch):
    _,_,_,_,relay,app=await setup(tmp_path,package,snapshot,monkeypatch)
    configs=await app.provider_configs()
    assert len(configs)==1 and configs[0]['keyPath']=='mcp_servers.notion'
    assert configs[0]['value']['url']=='http://127.0.0.1:8766/mcp'
    assert configs[0]['value']['http_headers']['Authorization'].startswith('Bearer ')
    assert len(app._bindings)==1
    assert next(iter(app._bindings.values())).expires-app._clock()>1790
    app.revoke_all();assert not app._bindings


@pytest.mark.anyio
async def test_owner_company_and_unconsented_account_cannot_reuse_provider(tmp_path,package,snapshot,monkeypatch):
    store,broker,selection,_,relay,app=await setup(tmp_path,package,snapshot,monkeypatch)
    for company,owner in [('b'*64,'alice'),(store.company_scope,'bob')]:
        with pytest.raises(ProviderRelayError):NativeProviderRelay(store,broker,selection,company_id=company,principal_id=owner)
    await store.install('notion',principal_id='bob')
    bob=NativeProviderRelay(store,broker,store.snapshot(principal_id='bob'),company_id=store.company_scope,principal_id='bob')
    with pytest.raises(ConnectionError):await bob.grant('notion')
    with pytest.raises(ValueError):NativeMCP(app.agent,replace(app.scope,owner_principal_id='bob'),company_id=store.company_scope,provider_relay=relay)


@pytest.mark.anyio
async def test_current_oauth_generation_fences_disconnected_and_reconnected_grants(tmp_path,package,snapshot,monkeypatch):
    _,broker,_,remote,relay,_=await setup(tmp_path,package,snapshot,monkeypatch)
    grant=await relay.grant('notion')
    await broker.disconnect('notion',principal_id='alice')
    assert not await relay.valid(grant)
    generation=broker._begin('notion','alice');broker._update('notion','alice',generation,status='connected')
    with pytest.raises(ProviderRelayError):await relay.call(grant,'notion-search',{'query':'private'})
    assert not remote.calls


@pytest.mark.anyio
async def test_connection_change_between_relay_check_and_broker_admission_never_calls_provider(tmp_path,package,snapshot,monkeypatch):
    _,broker,_,remote,relay,_=await setup(tmp_path,package,snapshot,monkeypatch)
    grant=await relay.grant('notion');actual=broker.call
    async def raced(*args,**kwargs):
        generation=broker._begin('notion','alice');broker._update('notion','alice',generation,status='connected')
        return await actual(*args,**kwargs)
    monkeypatch.setattr(broker,'call',raced)
    with pytest.raises(ConnectionError):await relay.call(grant,'notion-search',{'query':'private'})
    assert not remote.calls


@pytest.mark.anyio
@pytest.mark.parametrize('change',['disconnect','remove','reinstall'])
async def test_authorization_change_suppresses_late_provider_result(tmp_path,package,snapshot,monkeypatch,change):
    store,broker,_,remote,relay,app=await setup(tmp_path,package,snapshot,monkeypatch)
    grant=await app.issue_provider('notion');binding=next(iter(app._bindings.values()))
    remote.release=asyncio.Event()
    task=asyncio.create_task(app._call(binding,'notion-search',{'query':'private'}));await remote.entered.wait()
    if change=='disconnect':await broker.disconnect('notion',principal_id='alice')
    else:
        await store.uninstall('notion',principal_id='alice')
        if change=='reinstall':await store.install('notion',principal_id='alice')
    remote.release.set();result=await task
    assert result.isError and 'Exact provider result' not in str(result)
    assert not app._calls


@pytest.mark.anyio
async def test_native_stop_cancels_provider_work_and_withholds_result(tmp_path,package,snapshot,monkeypatch):
    _,_,_,remote,relay,app=await setup(tmp_path,package,snapshot,monkeypatch)
    await app.issue_provider('notion');binding=next(iter(app._bindings.values()));remote.release=asyncio.Event()
    task=asyncio.create_task(app._call(binding,'notion-search',{'query':'private'}));await remote.entered.wait()
    app.agent.active['root']['cancel'].set()
    result=await asyncio.wait_for(task,1)
    assert result.isError and not app._calls
    assert app.provider_relay is relay


@pytest.mark.anyio
async def test_metadata_list_is_fenced_and_undeclared_actor_arguments_rejected(tmp_path,package,snapshot,monkeypatch):
    store,broker,_,remote,relay,_=await setup(tmp_path,package,snapshot,monkeypatch)
    grant=await relay.grant('notion')
    with pytest.raises(ConnectionError):await relay.call(grant,'notion-search',{'query':'x','principal_id':'bob'})
    remote.list_gate=asyncio.Event()
    task=asyncio.create_task(relay.tools(grant));await asyncio.sleep(.01)
    await store.uninstall('notion',principal_id='alice');remote.list_gate.set()
    with pytest.raises(ProviderRelayError):await task
    assert not remote.calls


@pytest.mark.anyio
async def test_unreviewed_endpoint_or_ambiguous_server_name_never_issues_config(tmp_path,package,snapshot,monkeypatch):
    store,broker,selection,_,relay,_=await setup(tmp_path,package,snapshot,monkeypatch)
    store.entries['notion']['mcp_servers'][0]['url']='https://unreviewed.invalid/mcp'
    wrong=NativeProviderRelay(store,broker,selection,company_id=store.company_scope,principal_id='alice')
    assert wrong.server_names==()
    store.entries['notion']['mcp_servers']=[{'name':'notion','url':'https://mcp.notion.com/mcp','transport':'http'}]*2
    ambiguous=NativeProviderRelay(store,broker,selection,company_id=store.company_scope,principal_id='alice')
    assert ambiguous.server_names==()


@pytest.mark.anyio
async def test_external_output_schema_references_remain_closed(tmp_path,package,snapshot,monkeypatch):
    _,_,_,remote,relay,_=await setup(tmp_path,package,snapshot,monkeypatch)
    remote.tool.outputSchema={'$ref':'https://unreviewed.invalid/schema'}
    grant=await relay.grant('notion')
    with pytest.raises(ConnectionError):await relay.tools(grant)


@pytest.mark.anyio
async def test_provider_calls_share_bounded_native_admission_and_stop_clears_waiters(tmp_path,package,snapshot,monkeypatch):
    _,broker,_,remote,_,app=await setup(tmp_path,package,snapshot,monkeypatch)
    await app.issue_provider('notion');binding=next(iter(app._bindings.values()));remote.release=asyncio.Event()
    tasks=[asyncio.create_task(app._call(binding,'notion-search',{'query':str(i)})) for i in range(8)]
    await remote.entered.wait()
    assert len(app._calls)==8
    assert (await app._call(binding,'notion-search',{'query':'over capacity'})).isError
    app.revoke_all()
    results=await asyncio.wait_for(asyncio.gather(*tasks),2)
    assert all(result.isError for result in results) and not app._calls and not broker._active


@pytest.mark.anyio
async def test_repeated_consumer_cancellation_drains_provider_cleanup_before_releasing_slot(tmp_path,package,snapshot,monkeypatch):
    _,broker,_,remote,_,app=await setup(tmp_path,package,snapshot,monkeypatch)
    cleanup=asyncio.Event();release=asyncio.Event()
    @asynccontextmanager
    async def session(*args):
        try:yield remote
        finally:cleanup.set();await release.wait()
    monkeypatch.setattr(broker,'_session',session)
    await app.issue_provider('notion');binding=next(iter(app._bindings.values()));remote.release=asyncio.Event()
    task=asyncio.create_task(app._call(binding,'notion-search',{'query':'private'}))
    try:
        await remote.entered.wait();task.cancel();await cleanup.wait()
        for _ in range(3):task.cancel();await asyncio.sleep(.005)
        assert not task.done() and len(app._calls)==1
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert not app._calls and not broker._active
    finally:
        release.set();await asyncio.gather(task,return_exceptions=True)
