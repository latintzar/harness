"""Real native MCP client → host relay → fixed-token provider fixture."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import pytest
from mcp import types
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_approvals import NativeApprovals,ApprovalScope
from native_provider_relay import NativeProviderRelay
from native_packages import NativePackageStore
from mcp_connections import MCPConnections
from native_mcp import NativeMCP,NativeMCPScope
from test_native_packages import package,snapshot,Fake
from test_native_mcp import fixture as native_fixture,client
from test_native_provider_write import Provider,TOOL,SECRET,ARGS,META

@pytest.fixture
def anyio_backend():return 'asyncio'

@asynccontextmanager
async def setup_native(tmp_path,package,snapshot,monkeypatch):
 metadata=deepcopy(snapshot);metadata['plugins'][0]['mcp_servers']=[{'name':'notion','url':'https://mcp.notion.com/mcp','transport':'http'}]
 packages=NativePackageStore(tmp_path/'state',catalogue_snapshot=metadata,materializer=Fake(package))
 await packages.install('notion',principal_id='alice');selection=packages.snapshot(principal_id='alice')
 broker=MCPConnections(packages.state,public_origin='https://rocoto.example',write_providers={'notion'})
 generation=broker._begin('notion','alice',access_mode='read_write')
 broker._update('notion','alice',generation,status='connected',tokens=json.dumps({'access_token':SECRET,'token_type':'Bearer','scope':'default'}),expires_at=time.time()+3600,oauth_scopes=json.dumps(['default']))
 class Catalogue:
  async def initialize(self):pass
  async def list_tools(self,cursor=None):return types.ListToolsResult(tools=[TOOL])
 @asynccontextmanager
 async def session(*args,**kwargs):yield Catalogue()
 monkeypatch.setattr(broker,'_session',session)
 relay=NativeProviderRelay(packages,broker,selection,company_id=packages.company_scope,principal_id='alice')
 approvals=NativeApprovals(packages.state);scope=ApprovalScope(packages.company_scope,'alice','root','run:1','native-root')
 transport=relay.configure_writes(approvals,scope,enabled=True,timeout=3)
 provider=Provider();transport.transport_factory=provider.transport
 binding=await relay.approval_binding('notion',TOOL.name)
 request=approvals.open(scope=scope,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=binding,tool_arguments=ARGS)
 async def deliver(identifier,payload,guard):assert guard()
 await approvals.answer(request['id'],scope=scope,binding=binding,action='allow_once',deliver=deliver,cancelled=asyncio.Event())
 agent,_,_=native_fixture();mcp_scope=NativeMCPScope(packages.company_scope,'alice','cafe','root','native-root')
 app=NativeMCP(agent,mcp_scope,company_id=packages.company_scope,provider_relay=relay)
 grant=await app.issue_provider('notion')
 async with provider.app.router.lifespan_context(provider.app),app.lifespan():
  yield approvals,request,scope,provider,broker,agent,app,grant,relay
 await transport.close();await broker.close()

@pytest.mark.anyio
async def test_native_client_original_metadata_consumes_exact_permit(tmp_path,package,snapshot,monkeypatch):
 async with setup_native(tmp_path,package,snapshot,monkeypatch) as (store,row,scope,provider,broker,agent,app,grant,relay):
  async with client(app,grant.token) as session:
   tools=(await session.list_tools()).tools
   assert tools[0].model_dump(by_alias=True,exclude_none=True)==TOOL.model_dump(by_alias=True,exclude_none=True)
   result=await session.call_tool(TOOL.name,ARGS,meta=META)
   assert not result.isError and result.structuredContent=={'id':'document-1'} and result.meta=={'original':'preserved'}
   assert (await session.call_tool(TOOL.name,ARGS,meta=META))==result
  assert provider.effects==['Report'] and store.get(row['id'],scope=scope)['status']=='succeeded'

@pytest.mark.anyio
async def test_native_shared_provider_token_without_call_authority_denies(tmp_path,package,snapshot,monkeypatch):
 async with setup_native(tmp_path,package,snapshot,monkeypatch) as (store,row,scope,provider,broker,agent,app,grant,relay):
  async with client(app,grant.token) as session:
   result=await session.call_tool(TOOL.name,ARGS,meta={**META,'threadId':'native-child'})
   assert result.isError
  assert provider.effects==[] and store.get(row['id'],scope=scope)['status']=='approved'

@pytest.mark.anyio
async def test_native_stop_suppresses_output_but_drains_provider_receipt(tmp_path,package,snapshot,monkeypatch):
 async with setup_native(tmp_path,package,snapshot,monkeypatch) as (store,row,scope,provider,broker,agent,app,grant,relay):
  provider.release.clear()
  async with client(app,grant.token) as session:
   task=asyncio.create_task(session.call_tool(TOOL.name,ARGS,meta=META))
   await asyncio.wait_for(provider.entered.wait(),3)
   agent.active['root']['cancel'].set();store.invalidate(scope=scope,reason='stopped')
   await asyncio.sleep(.02);assert not task.done() and app._calls
   provider.release.set();result=await task
   assert result.isError and not app._calls
  assert provider.effects==['Report'] and store.get(row['id'],scope=scope)['status']=='succeeded'

@pytest.mark.anyio
async def test_real_connection_disconnect_drains_accepted_effect_and_closes_generation(tmp_path,package,snapshot,monkeypatch):
 async with setup_native(tmp_path,package,snapshot,monkeypatch) as (store,row,scope,provider,broker,agent,app,grant,relay):
  provider.release.clear()
  async with client(app,grant.token) as session:
   task=asyncio.create_task(session.call_tool(TOOL.name,ARGS,meta=META));await asyncio.wait_for(provider.entered.wait(),3)
   disconnect=asyncio.create_task(broker.disconnect('notion',principal_id='alice'))
   await asyncio.sleep(.02)
   assert not disconnect.done() and broker._record('notion','alice')['status'] in {'disconnecting','disconnected'}
   provider.release.set();result=await task;await disconnect
   assert result.isError
  assert provider.effects==['Report'] and store.get(row['id'],scope=scope)['status']=='succeeded'

@pytest.mark.anyio
async def test_artifact_lease_exposes_immediate_disconnect_fence(tmp_path,package,snapshot,monkeypatch):
 async with setup_native(tmp_path,package,snapshot,monkeypatch) as (store,row,scope,provider,broker,agent,app,grant,relay):
  from native_provider_relay import ProviderRelayError
  provenance=await relay.provenance('notion')
  with pytest.raises(ProviderRelayError,match='connection_changed'):
   async with relay.artifact_admission('notion',provenance) as current:
    assert current()
    disconnect=asyncio.create_task(broker.disconnect('notion',principal_id='alice'))
    for _ in range(50):
     if not current():break
     await asyncio.sleep(.005)
    assert not current() and not disconnect.done()
  await disconnect
  with pytest.raises(Exception):
   async with relay.artifact_admission('notion',provenance):raise AssertionError('stale admission')

@pytest.mark.anyio
async def test_artifact_lease_rejects_different_provider_provenance(tmp_path,package,snapshot,monkeypatch):
 async with setup_native(tmp_path,package,snapshot,monkeypatch) as (store,row,scope,provider,broker,agent,app,grant,relay):
  from native_provider_relay import ProviderRelayError
  provenance=await relay.provenance('notion')
  with pytest.raises(ProviderRelayError,match='connection_changed'):
   async with relay.artifact_admission('notion',{**provenance,'provider':'linear'}):raise AssertionError('wrong provenance')

@pytest.mark.anyio
async def test_disconnect_during_initialize_blocks_effect_at_http_boundary(tmp_path,package,snapshot,monkeypatch):
 import httpx
 async with setup_native(tmp_path,package,snapshot,monkeypatch) as (store,row,scope,provider,broker,agent,app,grant,relay):
  entered=asyncio.Event();release=asyncio.Event();original=relay.write_transport.transport_factory
  class InitializeGate(httpx.AsyncBaseTransport):
   def __init__(self,binding):self.inner=original(binding)
   async def handle_async_request(self,request):
    if request.method=='POST' and json.loads(request.content).get('method')=='initialize':
     entered.set();await release.wait()
    return await self.inner.handle_async_request(request)
   async def aclose(self):await self.inner.aclose()
  relay.write_transport.transport_factory=InitializeGate
  async with client(app,grant.token) as session:
   task=asyncio.create_task(session.call_tool(TOOL.name,ARGS,meta=META));await asyncio.wait_for(entered.wait(),3)
   disconnect=asyncio.create_task(broker.disconnect('notion',principal_id='alice'))
   for _ in range(50):
    if broker._record('notion','alice')['status']=='disconnecting':break
    await asyncio.sleep(.005)
   assert broker._record('notion','alice')['status']=='disconnecting' and not disconnect.done()
   release.set();result=await task;await disconnect
   assert result.isError
  assert provider.effects==[] and provider.requests.count('tools/call')==0
  assert store.get(row['id'],scope=scope)['status']=='approved'
