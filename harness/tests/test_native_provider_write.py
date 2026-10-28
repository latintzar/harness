import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass,replace
from pathlib import Path
from types import SimpleNamespace
import sys

import httpx
import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_approvals import NativeApprovals,ApprovalScope,ApprovalBinding,digest
from native_provider_write import NativeWriteTransport,PreparedWrite,WriteTransportError

@pytest.fixture
def anyio_backend():return 'asyncio'

SCOPE=ApprovalScope('company','alice','root','run:1','native-root')
ARGS={'title':'Report'}
META={'callId':'call-0','threadId':'native-root','itemId':'different-model-item','x-codex-turn-metadata':{'turn_id':'turn-0'}}
SECRET='synthetic-fixed-provider-access'
TOOL=types.Tool(name='create_document',description='Original provider description',inputSchema={'type':'object','properties':{'title':{'type':'string'}},'required':['title'],'additionalProperties':False})
BINDING=ApprovalBinding('fixture','fixture-server',TOOL.name,'https://provider.example/mcp',1,1,'a'*64,digest(TOOL.model_dump(mode='json',by_alias=True,exclude_none=True)),'read_write',('documents.write',),True)

class Provider:
 def __init__(self):
  self.effects=[];self.requests=[];self.entered=asyncio.Event();self.release=asyncio.Event();self.release.set();self.mode='normal';self.echo=False
  mcp=FastMCP('Fixture',stateless_http=True,json_response=True,transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=['provider.example','mcp.notion.com'],allowed_origins=[]))
  @mcp.tool(name='create_document')
  async def create_document(title:str)->types.CallToolResult:
   self.effects.append(title);self.entered.set();await self.release.wait()
   return types.CallToolResult(content=[types.TextContent(type='text',text=SECRET if self.echo else 'Original result')],structuredContent={'id':'document-1'},isError=False,_meta={'original':'preserved'})
  self.app=mcp.streamable_http_app()
 def transport(self,binding):
  owner=self
  class FixtureTransport(httpx.AsyncBaseTransport):
   def __init__(self):self.inner=httpx.ASGITransport(app=owner.app)
   async def handle_async_request(self,request):
    import json
    assert request.headers['authorization']=='Bearer '+SECRET
    method=json.loads(request.content).get('method') if request.method=='POST' else request.method
    owner.requests.append(method)
    if method=='tools/call' and owner.mode in {'401','307'}:
     return httpx.Response(int(owner.mode),headers={'location':'https://provider.example/mcp/redirect'} if owner.mode=='307' else {},request=request)
    response=await self.inner.handle_async_request(request)
    if method=='tools/call' and owner.mode=='lost':
     await response.aclose();raise httpx.ReadError('synthetic lost response',request=request)
    return response
   async def aclose(self):await self.inner.aclose()
  return FixtureTransport()

class Authority:
 def __init__(self):self.binding=BINDING;self.available=True;self.lock=asyncio.Lock();self.entered=asyncio.Event();self.prepare_gate=None
 async def prepare(self,server,tool):
  if self.prepare_gate:await self.prepare_gate.wait()
  if not self.available:raise ValueError('disconnected')
  return PreparedWrite(self.binding,TOOL)
 @asynccontextmanager
 async def admission(self,binding):
  async with self.lock:
   if not self.available or binding!=self.binding:raise ValueError('changed')
   self.entered.set();yield
 def credentials(self,binding):
  if not self.available or binding!=self.binding:raise ValueError('changed')
  return SimpleNamespace(access_token=SECRET)

@asynccontextmanager
async def setup(tmp_path,*,timeout=2,enabled=True):
 store=NativeApprovals(tmp_path);row=store.open(scope=SCOPE,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=BINDING,tool_arguments=ARGS)
 async def deliver(identifier,payload,guard):assert guard()
 await store.answer(row['id'],scope=SCOPE,binding=BINDING,action='allow_once',deliver=deliver,cancelled=asyncio.Event())
 provider=Provider();authority=Authority();cancel=asyncio.Event()
 transport=NativeWriteTransport(store,SCOPE,prepare=authority.prepare,admission=authority.admission,credentials=authority.credentials,enabled=enabled,timeout=timeout,transport_factory=provider.transport)
 async with provider.app.router.lifespan_context(provider.app):
  yield store,row,provider,authority,cancel,transport
  await transport.close()

async def call(transport,cancel,**changes):
 kw=dict(server='fixture-server',tool='create_document',arguments=ARGS,metadata=META,cancelled=cancel);kw.update(changes)
 return await transport.execute(**kw)

@pytest.mark.anyio
async def test_actual_mcp_fixed_token_result_and_durable_single_dispatch(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  result=await call(transport,cancel)
  assert result.content[0].text=='Original result' and result.structuredContent=={'id':'document-1'} and result.meta=={'original':'preserved'}
  saved=store.get(row['id'],scope=SCOPE)
  assert saved['status']=='succeeded' and len(saved['receipt']['response_sha256'])==64
  again=await call(transport,cancel)
  assert again==result and provider.effects==['Report'] and provider.requests.count('tools/call')==1

@pytest.mark.anyio
@pytest.mark.parametrize('mode',['401','307','lost'])
async def test_auth_redirect_and_lost_response_never_repeat_effectful_request(tmp_path,mode):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  provider.mode=mode
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert provider.requests.count('tools/call')==1
  assert len(provider.effects)==(1 if mode=='lost' else 0)
  assert store.get(row['id'],scope=SCOPE)['status']=='uncertain'
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert provider.requests.count('tools/call')==1

@pytest.mark.anyio
async def test_stop_before_dispatch_sends_zero_effectful_requests(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  cancel.set()
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert provider.requests==[] and provider.effects==[]

@pytest.mark.anyio
async def test_repeated_cancellation_after_effect_drains_and_saves_receipt(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  provider.release.clear();task=asyncio.create_task(call(transport,cancel));await asyncio.wait_for(provider.entered.wait(),3)
  cancel.set();store.invalidate(scope=SCOPE,reason='stopped');task.cancel();await asyncio.sleep(0)
  assert not task.done() and transport._work and authority.lock.locked()
  task.cancel();await asyncio.sleep(0);task.cancel();await asyncio.sleep(0)
  provider.release.set()
  with pytest.raises(asyncio.CancelledError):await task
  assert not transport._work and not authority.lock.locked()
  assert store.get(row['id'],scope=SCOPE)['status']=='succeeded' and provider.effects==['Report']

@pytest.mark.anyio
async def test_disconnect_closes_admission_while_write_waits(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  await authority.lock.acquire();task=asyncio.create_task(call(transport,cancel));await asyncio.sleep(.02)
  authority.available=False;authority.lock.release()
  with pytest.raises(WriteTransportError):await task
  assert provider.effects==[] and provider.requests==[]

@pytest.mark.anyio
async def test_default_transport_needs_explicit_commissioning(tmp_path):
 async with setup(tmp_path,enabled=False) as (store,row,provider,authority,cancel,transport):
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert provider.requests==[]

@pytest.mark.anyio
async def test_provider_echoed_token_stays_out_of_saved_and_native_result(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  provider.echo=True;result=await call(transport,cancel)
  assert SECRET not in result.model_dump_json()
  assert SECRET not in transport._path(row['id']).read_text()

@pytest.mark.anyio
async def test_timeout_after_provider_acceptance_is_uncertain_and_never_repeated(tmp_path):
 async with setup(tmp_path,timeout=.2) as (store,row,provider,authority,cancel,transport):
  provider.release.clear()
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert provider.effects==['Report'] and store.get(row['id'],scope=SCOPE)['status']=='uncertain'
  provider.release.set()
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert provider.effects==['Report'] and not transport._work and not authority.lock.locked()

@pytest.mark.anyio
async def test_crash_after_saved_result_can_reconcile_without_network(tmp_path,monkeypatch):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  original=store.outcome;failed=[]
  def outage(*args,**kwargs):
   if kwargs.get('status')=='succeeded' and not failed:
    failed.append(True);raise OSError('synthetic commit outage')
   return original(*args,**kwargs)
  monkeypatch.setattr(store,'outcome',outage)
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert store.get(row['id'],scope=SCOPE)['status']=='uncertain' and transport._path(row['id']).exists()
  before=list(provider.requests)
  assert (await transport.recover_saved_result(row['id'],BINDING))['status']=='succeeded'
  assert provider.requests==before and provider.effects==['Report']
  assert (await call(transport,cancel)).content[0].text=='Original result'
  assert provider.effects==['Report']

@pytest.mark.anyio
async def test_process_restart_retains_uncertain_dispatch_and_blocks_new_effect(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  provider.mode='lost'
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  transport.store=NativeApprovals(tmp_path);transport.store.recover()
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert provider.effects==['Report']

@pytest.mark.anyio
@pytest.mark.parametrize('metadata',[{**META,'threadId':'child'}, {**META,'callId':'unknown'}, {**META,'callId':'different-model-item'}])
async def test_wrong_native_actor_or_call_cannot_dispatch(tmp_path,metadata):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  with pytest.raises(WriteTransportError):await call(transport,cancel,metadata=metadata)
  assert provider.effects==[] and provider.requests.count('tools/call')==0

@pytest.mark.anyio
async def test_changed_authority_invalidates_before_network_effect(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  authority.binding=replace(BINDING,connection_generation=2)
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert provider.effects==[] and store.get(row['id'],scope=SCOPE)['status']=='invalidated'

@pytest.mark.anyio
async def test_admission_remains_reserved_until_cancelled_write_drains(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  transport.max_calls=1;provider.release.clear();task=asyncio.create_task(call(transport,cancel));await asyncio.wait_for(provider.entered.wait(),3)
  task.cancel();await asyncio.sleep(0)
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  assert len(transport._work)==1
  provider.release.set()
  with pytest.raises(asyncio.CancelledError):await task
  assert not transport._work

@pytest.mark.anyio
async def test_uncertain_provider_fences_changed_arguments_and_new_native_call(tmp_path):
 async with setup(tmp_path) as (store,row,provider,authority,cancel,transport):
  provider.mode='lost'
  with pytest.raises(WriteTransportError):await call(transport,cancel)
  changed={'title':'Report with a different incidental title'}
  second=store.open(scope=SCOPE,actor_chat_id='root',native_request_id=1,turn_id='turn-1',call_id='call-1',binding=BINDING,tool_arguments=changed)
  async def deliver(identifier,payload,guard):assert guard()
  await store.answer(second['id'],scope=SCOPE,binding=BINDING,action='allow_once',deliver=deliver,cancelled=cancel)
  provider.mode='normal'
  with pytest.raises(WriteTransportError):await call(transport,cancel,arguments=changed,metadata={**META,'callId':'call-1','x-codex-turn-metadata':{'turn_id':'turn-1'}})
  assert provider.effects==['Report'] and provider.requests.count('tools/call')==1
  assert store.get(second['id'],scope=SCOPE)['status']=='approved'
