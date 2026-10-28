import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_approvals import NativeApprovals,NativeApprovalRouter,ApprovalError
from test_native_provider_write import SCOPE,BINDING,ARGS

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
@pytest.mark.parametrize('state',['current','old_generation','stopped','resolved','wrong_owner'])
async def test_decline_uses_current_owner_and_native_scope_without_provider_lookup(tmp_path,state):
 store=NativeApprovals(tmp_path);row=store.open(scope=SCOPE,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=BINDING,tool_arguments=ARGS)
 calls=[];sent=[];cancel=asyncio.Event()
 async def unavailable(server,tool):calls.append(True);raise RuntimeError('provider unavailable')
 async def write(value,guard=None):
  if guard and not guard():raise RuntimeError('ended')
  sent.append(value)
 scope=replace(SCOPE,generation='run:2') if state=='old_generation' else SCOPE
 if state=='stopped':cancel.set()
 if state=='resolved':store.resolve_native(SCOPE,0)
 router=NativeApprovalRouter(store,scope,resolve_binding=unavailable,cancelled=cancel,write=write)
 if state=='wrong_owner':
  with pytest.raises(ApprovalError):await router.answer(row['id'],owner='bob',action='decline')
 else:
  result=await router.answer(row['id'],owner='alice',action='decline')
  assert result['request']['status']=={'current':'declined','old_generation':'invalidated','stopped':'cancelled','resolved':'cancelled'}[state]
 assert not calls
 assert sent==([{'id':0,'result':{'action':'decline','content':None}}] if state=='current' else [])

@pytest.mark.anyio
async def test_allow_preflight_failure_closes_widget_and_native_wait_without_details(tmp_path):
 store=NativeApprovals(tmp_path);row=store.open(scope=SCOPE,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=BINDING,tool_arguments=ARGS)
 sent=[]
 async def unavailable(*args):raise RuntimeError('private provider diagnostic and token')
 async def write(value,guard=None):sent.append(value)
 router=NativeApprovalRouter(store,SCOPE,resolve_binding=unavailable,cancelled=asyncio.Event(),write=write)
 result=await router.answer(row['id'],owner='alice',action='allow_once')
 assert result['request']['status']=='invalidated' and result['request']['resolution_reason']=='provider_unavailable'
 assert 'private provider' not in str(result)
 assert sent==[{'id':0,'result':{'action':'cancel','content':None}}]
 assert (await router.answer(row['id'],owner='alice',action='allow_once'))==result and len(sent)==1


def test_provider_disconnect_closes_exact_owner_across_roots_and_preserves_others(tmp_path):
 store=NativeApprovals(tmp_path)
 scopes=[SCOPE,replace(SCOPE,root_chat_id='other-root',root_thread_id='other-native'),replace(SCOPE,owner='bob',root_chat_id='bob'),replace(SCOPE,company_id='other-company',root_chat_id='elsewhere')]
 rows=[store.open(scope=scope,actor_chat_id=scope.root_chat_id,native_request_id=0,turn_id='turn',call_id='call',binding=BINDING,tool_arguments=ARGS)for scope in scopes]
 assert len(store.invalidate(company_id='company',owner='alice',provider='fixture',reason='disconnected'))==2
 assert [store.get(row['id'],scope=scope)['status']for row,scope in zip(rows,scopes)]==['cancelled','cancelled','pending','pending']
 with pytest.raises(ApprovalError):store.invalidate(company_id='company',provider='fixture')

@pytest.mark.anyio
@pytest.mark.parametrize('reason',['preflight','expiry'])
async def test_native_resolution_while_cancel_reply_queued_sends_zero(tmp_path,reason):
 now=[100.0];store=NativeApprovals(tmp_path,clock=lambda:now[0]);store.open(scope=SCOPE,actor_chat_id='root',native_request_id=0,turn_id='turn',call_id='call',binding=BINDING,tool_arguments=ARGS)
 row=store.for_chat(scope=SCOPE)[0];entered=asyncio.Event();lock=asyncio.Lock();await lock.acquire();sent=[]
 async def unavailable(*args):raise ValueError('provider unavailable')
 async def write(value,guard=None):
  entered.set()
  async with lock:
   if guard and not guard():raise RuntimeError('ended')
   sent.append(value)
 router=NativeApprovalRouter(store,SCOPE,resolve_binding=unavailable,cancelled=asyncio.Event(),write=write)
 if reason=='expiry':now[0]=401;task=asyncio.create_task(router.expire())
 else:task=asyncio.create_task(router.answer(row['id'],owner='alice',action='allow_once'))
 await entered.wait();store.resolve_native(SCOPE,0);lock.release();await task
 assert sent==[]

@pytest.mark.anyio
@pytest.mark.parametrize('resolution',['normal','already_invalidated','native_resolved','stopped'])
async def test_provider_revoke_cancels_exact_unanswered_native_request_once(tmp_path,resolution):
 store=NativeApprovals(tmp_path);row=store.open(scope=SCOPE,actor_chat_id='root',native_request_id=0,turn_id='turn',call_id='call',binding=BINDING,tool_arguments=ARGS)
 other_scope=replace(SCOPE,owner='bob',root_chat_id='bob')
 other=store.open(scope=other_scope,actor_chat_id='bob',native_request_id=0,turn_id='turn',call_id='call',binding=BINDING,tool_arguments=ARGS)
 entered=asyncio.Event();lock=asyncio.Lock();await lock.acquire();sent=[];cancel=asyncio.Event()
 async def unavailable(*args):raise AssertionError('Revocation must not contact the provider')
 async def write(value,guard=None):
  entered.set()
  async with lock:
   if guard and not guard():raise RuntimeError('ended')
   sent.append(value)
 router=NativeApprovalRouter(store,SCOPE,resolve_binding=unavailable,cancelled=cancel,write=write)
 if resolution=='already_invalidated':store.invalidate(company_id=SCOPE.company_id,owner=SCOPE.owner,provider='fixture',reason='disconnected')
 task=asyncio.create_task(router.revoke_provider('fixture'))
 await asyncio.wait_for(entered.wait(),1)
 if resolution=='native_resolved':store.resolve_native(SCOPE,0)
 if resolution=='stopped':cancel.set()
 lock.release();events=await task
 assert events and events[0]['request']['status']=='cancelled'
 await router.revoke_provider('fixture')
 assert sent==([{'id':0,'result':{'action':'cancel','content':None}}] if resolution in {'normal','already_invalidated'} else [])
 assert store.get(other['id'],scope=other_scope)['status']=='pending'
