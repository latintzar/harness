"""Host-owned direct services use real MCP transport without invented packages."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

import pytest
from mcp import types

sys.path.insert(0, str(Path(__file__).parents[1]))
from mcp_connections import MCPConnections, CATALOGUE, ConnectionError
from native_approvals import NativeApprovals, ApprovalScope, ApprovalBinding, ApprovalError
from native_mcp import NativeMCP, NativeMCPScope
from native_provider_relay import NativeProviderRelay, ProviderRelayError
from test_native_mcp import fixture as native_fixture, client
from test_native_provider_relay import Provider as ReadProvider
from test_native_provider_write import Provider, TOOL, SECRET, ARGS, META


@pytest.fixture
def anyio_backend(): return 'asyncio'


@asynccontextmanager
async def direct_fixture(tmp_path, monkeypatch, *, mode='read', connected=True, commissioned=True):
    policy = deepcopy(CATALOGUE['notion'])
    policy.update(native_direct=True, name='Direct fixture', policy_revision='fixture-v1')
    monkeypatch.setitem(CATALOGUE, 'fixture-direct', policy)
    broker = MCPConnections(tmp_path, public_origin='https://rocoto.example',
                            write_providers={'fixture-direct'} if commissioned else set())
    # The fixture controls only host commissioning; normal account/generation,
    # original schema validation and encrypted credentials still use the broker.
    authority = {'available': True, 'revision': 'fixture-v1'}
    monkeypatch.setattr(broker, 'provider_available', lambda provider: provider=='fixture-direct' and authority['available'])
    monkeypatch.setattr(broker, 'provider_policy_revision', lambda provider: authority['revision'])
    if connected:
        generation = broker._begin('fixture-direct', 'alice', access_mode=mode)
        broker._update('fixture-direct', 'alice', generation, status='connected',
            tokens=json.dumps({'access_token': SECRET, 'token_type': 'Bearer', 'scope': 'default'}),
            expires_at=time.time()+3600, oauth_scopes=json.dumps(['default']))
    remote = ReadProvider()
    if mode == 'read_write': remote.tool = TOOL
    @asynccontextmanager
    async def session(provider, principal, *args, **kwargs):
        assert provider == 'fixture-direct' and principal == 'alice'
        yield remote
    monkeypatch.setattr(broker, '_session', session)
    relay = NativeProviderRelay(None, broker, None, company_id=broker.company_scope, principal_id='alice')
    agent, _, _ = native_fixture()
    app = NativeMCP(agent, NativeMCPScope(broker.company_scope, 'alice', 'cafe', 'root', 'native-root'),
                    company_id=broker.company_scope, provider_relay=relay)
    try: yield broker, authority, remote, relay, app
    finally:
        if relay.write_transport: await relay.write_transport.close()
        await broker.close()


@pytest.mark.anyio
async def test_direct_original_schema_result_and_private_native_config(tmp_path, monkeypatch):
    async with direct_fixture(tmp_path, monkeypatch) as (broker, authority, remote, relay, app):
        assert relay.server_names == ('rocoto_fixture_direct',)
        config = (await app.provider_configs())[0]
        assert config['keyPath'] == 'mcp_servers.rocoto_fixture_direct'
        assert config['value']['url'] == 'http://127.0.0.1:8766/mcp'
        assert SECRET not in json.dumps(config)
        token = config['value']['http_headers']['Authorization'].removeprefix('Bearer ')
        async with app.lifespan(), client(app, token) as session:
            tools = (await session.list_tools()).tools
            assert [t.model_dump(by_alias=True) for t in tools] == [remote.tool.model_dump(by_alias=True)]
            result = await session.call_tool(remote.tool.name, {'query':'report'})
            assert result.content[0].text == 'Exact provider result.'
            assert result.meta == {'provider_marker':'original-result'}
        provenance = await relay.provenance('rocoto_fixture_direct')
        assert provenance['authority_kind'] == 'direct_provider'
        assert provenance['policy_revision'] == 'fixture-v1'
        assert len(provenance['policy_sha256']) == 64
        assert not {'plugin_id','package_sha256','package_generation'} & provenance.keys()


@pytest.mark.anyio
async def test_direct_configuration_requires_host_and_exact_owner_consent(tmp_path, monkeypatch):
    async with direct_fixture(tmp_path, monkeypatch, connected=False) as (broker, authority, remote, relay, app):
        assert await app.provider_configs() == []
        assert not remote.calls
        with pytest.raises(ProviderRelayError):
            NativeProviderRelay(None, broker, None, company_id='b'*64, principal_id='alice')
        generation=broker._begin('fixture-direct','bob')
        broker._update('fixture-direct','bob',generation,status='connected')
        assert await app.provider_configs() == []
        authority['available']=False
        closed=NativeProviderRelay(None,broker,None,company_id=broker.company_scope,principal_id='bob')
        assert closed.server_names == ()


@pytest.mark.anyio
@pytest.mark.parametrize('change',['policy','endpoint','availability','disconnect','stop'])
async def test_direct_revocation_suppresses_late_results(tmp_path, monkeypatch, change):
    async with direct_fixture(tmp_path, monkeypatch) as (broker, authority, remote, relay, app):
        await app.issue_provider('rocoto_fixture_direct')
        binding=next(iter(app._bindings.values()));remote.release=asyncio.Event()
        task=asyncio.create_task(app._call(binding,remote.tool.name,{'query':'private'}))
        await remote.entered.wait()
        if change=='policy': authority['revision']='fixture-v2'
        elif change=='endpoint': broker._policies['fixture-direct']['endpoint']='https://different.example/mcp'
        elif change=='availability': authority['available']=False
        elif change=='disconnect': await broker.disconnect('fixture-direct',principal_id='alice')
        else: app.agent.active['root']['cancel'].set()
        remote.release.set();result=await asyncio.wait_for(task,3)
        assert result.isError and 'Exact provider result' not in str(result)
        assert not app._calls


@pytest.mark.anyio
async def test_direct_artifact_admission_rechecks_policy_inside_commit(tmp_path, monkeypatch):
    async with direct_fixture(tmp_path, monkeypatch) as (broker, authority, remote, relay, app):
        value=await relay.provenance('rocoto_fixture_direct')
        with pytest.raises(ProviderRelayError):
            async with relay.artifact_admission('rocoto_fixture_direct',value) as current:
                assert current()
                authority['revision']='fixture-v2'
                assert not current()


@pytest.mark.anyio
async def test_direct_exact_intent_write_uses_original_transport_and_parent_identity(tmp_path, monkeypatch):
    async with direct_fixture(tmp_path, monkeypatch,mode='read_write') as (broker, authority, remote, relay, app):
        store=NativeApprovals(tmp_path);scope=ApprovalScope(broker.company_scope,'alice','root','run:1','native-root')
        provider=Provider()
        relay.configure_writes(store,scope,enabled=True,timeout=3).transport_factory=provider.transport
        binding=await relay.approval_binding('rocoto_fixture_direct',TOOL.name)
        assert binding.authority_kind=='direct_provider' and binding.package_generation is None and binding.package_sha256 is None
        request=store.open(scope=scope,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=binding,tool_arguments=ARGS)
        async def deliver(identifier,payload,guard): assert identifier==0 and guard()
        await store.answer(request['id'],scope=scope,binding=binding,action='allow_once',deliver=deliver,cancelled=asyncio.Event())
        grant=await app.issue_provider('rocoto_fixture_direct')
        async with provider.app.router.lifespan_context(provider.app),app.lifespan(),client(app,grant.token) as session:
            assert (await session.call_tool(TOOL.name,ARGS,meta={**META,'threadId':'native-child'})).isError
            result=await session.call_tool(TOOL.name,ARGS,meta=META)
            assert not result.isError and result.structuredContent=={'id':'document-1'}
            assert (await session.call_tool(TOOL.name,ARGS,meta=META))==result
        assert provider.effects==['Report'] and provider.requests.count('tools/call')==1
        assert store.get(request['id'],scope=scope)['status']=='succeeded'


def test_direct_approval_identity_is_explicit_and_rejects_fake_package():
    value=ApprovalBinding('fixture','server','write','https://provider.example/mcp',1,None,None,'a'*64,
        'read_write',('scope',),True,authority_kind='direct_provider',policy_revision='v1',policy_sha256='b'*64)
    for change in [{'package_generation':0},{'package_sha256':'c'*64},{'policy_revision':None},
                   {'policy_sha256':None},{'authority_kind':'native_package'},{'authority_kind':'model'}]:
        with pytest.raises(ApprovalError): replace(value,**change)


def google_broker(state,*,writes=False,revision=1):
    from registered_oauth import RegisteredOAuthClients,RegisteredClient
    from workspace_identity import workspace_scope
    callback='https://rocoto.example/api/connections/oauth/callback'
    clients=RegisteredOAuthClients(workspace_scope(state),callback,tuple(
        RegisteredClient(provider,revision,callback,'synthetic.apps.googleusercontent.com','synthetic-host-client-secret')
        for provider in ('google-drive','google-docs')))
    broker=MCPConnections(state,public_origin='https://rocoto.example',registered_clients=clients,
        write_providers={'google-drive','google-docs'} if writes else set())
    return broker


def consent(broker,provider,*,mode='read',owner='alice'):
    requested=CATALOGUE[provider]['scopes_by_mode'][mode]
    generation=broker._begin(provider,owner,access_mode=mode)
    broker._update(provider,owner,generation,status='connected',oauth_scopes=json.dumps(requested),
        tokens=json.dumps({'access_token':SECRET,'token_type':'Bearer','scope':' '.join(requested)}),expires_at=time.time()+3600)


@pytest.mark.anyio
@pytest.mark.parametrize('writes',[False,True])
async def test_google_no_package_full_native_bootstrap_sets_approval_policy_before_thread(tmp_path,monkeypatch,writes):
    from test_isolated_turn import NativeProtocol,root_agent
    from deploy.migration.legacy.native_policy import isolated_thread_params
    broker=google_broker(tmp_path,writes=writes)
    for provider in ('google-drive','google-docs'):consent(broker,provider,mode='read_write' if writes else 'read')
    agent=root_agent(tmp_path);agent.connection_store=broker;protocol=NativeProtocol();bound=[]
    class Catalogue:
        def __init__(self,provider):self.provider=provider
        async def initialize(self):pass
        async def list_tools(self,cursor=None):return types.ListToolsResult(tools=[types.Tool(
            name='create_file' if self.provider=='google-drive' else 'read_doc',description='Synthetic original contract',inputSchema={'type':'object'})])
    @asynccontextmanager
    async def session(provider,owner,*args,**kwargs):assert owner=='alice';yield Catalogue(provider)
    monkeypatch.setattr(broker,'_session',session)
    class Launcher:
        enabled=True;company_id=broker.company_scope;package_store=None
        @asynccontextmanager
        async def lease(self,scope,run_id):
            class Session:
                process=protocol;runtime_packages=None;stopped=False;_closing=False
                def thread_params(self,instructions,tools):return isolated_thread_params(instructions,tools)
                async def bind_mcp(self,mcp):
                    assert mcp.provider_relay.store is None and mcp.provider_relay.selection is None
                    assert ('approval_router' in agent.active['chat']) is writes
                    configs=await mcp.provider_configs();bound.extend(configs)
                    return {'keyPath':'mcp_servers','mergeStrategy':'replace','value':{
                        config['keyPath'].split('.',1)[1]:config['value'] for config in configs}}
                async def stop(self):self._closing=True;self.stopped=True
            active=Session();active.scope=scope
            try:yield active
            finally:await active.stop()
    agent.isolated_launcher=Launcher()
    events=[event async for event in agent._turn('Use my connected Google content','chat','cafe')]
    assert not any(event['type']=='error' for event in events),events
    assert events[-1]['status']=='completed'
    assert {item['keyPath'] for item in bound}=={'mcp_servers.rocoto_google_drive','mcp_servers.rocoto_google_docs'}
    initialize=next(params for method,params in protocol.calls if method=='initialize')
    assert initialize['capabilities'].get('mcpServerOpenaiFormElicitation',False) is writes
    params=next(params for method,params in protocol.calls if method=='thread/start')
    assert 'baseInstructions' not in params
    assert params['approvalPolicy']==({'granular':{'sandbox_approval':False,'rules':False,'skill_approval':False,
        'request_permissions':False,'mcp_elicitations':True}} if writes else 'never')
    serialized=json.dumps(events)+agent.toolsets_path.read_text()
    assert SECRET not in serialized and 'synthetic-host-client-secret' not in serialized
    assert 'skills/list' not in {method for method,_ in protocol.calls}
    await broker.close()


@pytest.mark.anyio
async def test_google_registered_client_rotation_invalidates_direct_native_grant(tmp_path):
    first=google_broker(tmp_path,writes=True);consent(first,'google-drive',mode='read_write')
    relay=NativeProviderRelay(None,first,None,company_id=first.company_scope,principal_id='alice')
    old=await relay.grant('rocoto_google_drive')
    second=google_broker(tmp_path,writes=True,revision=2)
    try:
        assert not await relay.valid(old)
        assert NativeProviderRelay(None,first,None,company_id=first.company_scope,principal_id='alice').server_names==()
        replacement=NativeProviderRelay(None,second,None,company_id=second.company_scope,principal_id='alice')
        with pytest.raises(ConnectionError):await replacement.grant('rocoto_google_drive')
        consent(second,'google-drive',mode='read_write')
        new=await replacement.grant('rocoto_google_drive')
        assert new.policy_sha256!=old.policy_sha256 and new.connection_generation!=old.connection_generation
    finally:await first.close();await second.close()


@pytest.mark.anyio
@pytest.mark.parametrize('case',['success','lost','rotation','stop_before','disconnect_before','read','uncommissioned'])
async def test_google_original_create_file_through_native_client_and_approval(tmp_path,monkeypatch,case):
    import httpx
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from test_provider_outputs import GOOGLE_FILE
    broker=google_broker(tmp_path,writes=case!='uncommissioned')
    consent(broker,'google-drive',mode='read' if case=='read' else 'read_write')
    original=types.Tool(name='create_file',description='Synthetic instance of Google’s documented create_file contract.',
        inputSchema={'type':'object','properties':{'title':{'type':'string'},'contentMimeType':{'type':'string'},
            'textContent':{'type':'string'}},'required':['title','contentMimeType','textContent'],'additionalProperties':False},
        outputSchema={'type':'object','properties':{'id':{'type':'string'},'viewUrl':{'type':'string'}},'required':['id','viewUrl']},
        annotations=types.ToolAnnotations(readOnlyHint=False,idempotentHint=False,openWorldHint=True))
    class Catalogue:
        async def initialize(self):pass
        async def list_tools(self,cursor=None):return types.ListToolsResult(tools=[original])
    @asynccontextmanager
    async def session(*args,**kwargs):yield Catalogue()
    monkeypatch.setattr(broker,'_session',session)
    relay=NativeProviderRelay(None,broker,None,company_id=broker.company_scope,principal_id='alice')
    store=NativeApprovals(tmp_path);scope=ApprovalScope(broker.company_scope,'alice','root','run:1','native-root')
    transport=relay.configure_writes(store,scope,enabled=True,timeout=3)
    effects=[];requests=[];gate=asyncio.Event();release=asyncio.Event()
    provider=FastMCP('Synthetic official endpoint',stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=['drivemcp.googleapis.com'],allowed_origins=[]))
    @provider.tool(name='create_file')
    async def create_file(title:str,contentMimeType:str,textContent:str):
        effects.append({'title':title,'contentMimeType':contentMimeType,'textContent':textContent})
        return types.CallToolResult(content=[types.TextContent(type='text',text=json.dumps(GOOGLE_FILE))],
            structuredContent=deepcopy(GOOGLE_FILE),isError=False,_meta={'original':'google-fixture'})
    provider_app=provider.streamable_http_app()
    class HTTP(httpx.AsyncBaseTransport):
        def __init__(self):self.inner=httpx.ASGITransport(app=provider_app)
        async def handle_async_request(self,request):
            assert request.url.host=='drivemcp.googleapis.com' and request.url.path=='/mcp/v1'
            assert request.headers['Authorization']=='Bearer '+SECRET
            method=json.loads(request.content).get('method') if request.method=='POST' else request.method
            requests.append(method)
            if method=='initialize' and case in {'stop_before','disconnect_before'}:gate.set();await release.wait()
            # The local fixture owns /mcp; the provider request keeps its exact URL
            # until this test-only transport boundary. Production has no rewrite.
            local=httpx.Request(request.method,'https://drivemcp.googleapis.com/mcp',headers=request.headers,content=request.content)
            response=await self.inner.handle_async_request(local)
            if method=='tools/call' and case=='lost':await response.aclose();raise httpx.ReadError('lost synthetic response')
            return response
        async def aclose(self):await self.inner.aclose()
    transport.transport_factory=lambda binding:HTTP()
    args={'title':'Service report','contentMimeType':'text/plain','textContent':'Original content'}
    binding=None;row=None;second=None;disconnect=None
    if case not in {'read','uncommissioned'}:
        binding=await relay.approval_binding('rocoto_google_drive','create_file')
        assert binding.permitted and binding.authority_kind=='direct_provider'
        row=store.open(scope=scope,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=binding,tool_arguments=args)
        async def deliver(identifier,payload,guard):assert identifier==0 and guard()
        await store.answer(row['id'],scope=scope,binding=binding,action='allow_once',deliver=deliver,cancelled=asyncio.Event())
    agent,_,_=native_fixture();app=NativeMCP(agent,NativeMCPScope(broker.company_scope,'alice','cafe','root','native-root'),company_id=broker.company_scope,provider_relay=relay)
    grant=await app.issue_provider('rocoto_google_drive')
    try:
        async with provider_app.router.lifespan_context(provider_app),app.lifespan(),client(app,grant.token) as native:
            listed=(await native.list_tools()).tools
            if case=='read':assert listed==[]
            else:assert [tool.model_dump(by_alias=True) for tool in listed]==[original.model_dump(by_alias=True)]
            if case=='rotation':second=google_broker(tmp_path,writes=True,revision=2)
            task=asyncio.create_task(native.call_tool('create_file',args,meta=META))
            if case in {'stop_before','disconnect_before'}:
                await asyncio.wait_for(gate.wait(),3)
                if case=='stop_before':agent.active['root']['cancel'].set()
                else:
                    disconnect=asyncio.create_task(broker.disconnect('google-drive',principal_id='alice'))
                    for _ in range(100):
                        if broker._record('google-drive','alice')['status']=='disconnecting':break
                        await asyncio.sleep(.005)
                    assert broker._record('google-drive','alice')['status']=='disconnecting'
                release.set()
            result=await task
            if disconnect:await disconnect
            if case=='success':
                assert not result.isError and result.structuredContent==GOOGLE_FILE
                assert result.meta=={'original':'google-fixture'}
                assert (await native.call_tool('create_file',args,meta=META))==result
            else:assert result.isError
            if case=='lost':
                assert store.get(row['id'],scope=scope)['status']=='uncertain'
                changed={**args,'title':'Changed title is still a possible duplicate'}
                fresh=store.open(scope=scope,actor_chat_id='root',native_request_id=1,turn_id='turn-0',call_id='call-1',binding=binding,tool_arguments=changed)
                async def deliver_again(identifier,payload,guard):assert guard()
                await store.answer(fresh['id'],scope=scope,binding=binding,action='allow_once',deliver=deliver_again,cancelled=asyncio.Event())
                assert (await native.call_tool('create_file',changed,meta={**META,'callId':'call-1'})).isError
        assert effects==([args] if case in {'success','lost'} else [])
        assert requests.count('tools/call')==(1 if case in {'success','lost'} else 0)
    finally:
        release.set();await transport.close();await broker.close()
        if second:await second.close()
