import asyncio
from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_approvals import NativeApprovals,NativeApprovalRouter,ApprovalScope,ApprovalBinding,ApprovalError

@pytest.fixture
def anyio_backend():return 'asyncio'

SCOPE=ApprovalScope('company','alice','root','run:1','native-root')
BINDING=ApprovalBinding('provider','original-server','create_document','https://provider.example/mcp',1,1,'a'*64,'b'*64,'read_write',('documents.write',),True)
ARGS={'title':'Quarterly report','content':'Original document content'}
META={'callId':'call-0','threadId':'native-root','itemId':'MODEL-ITEM-DIFFERENT','x-codex-turn-metadata':{'turn_id':'turn-0'}}

def open_request(store,**changes):
    kw=dict(scope=SCOPE,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=BINDING,tool_arguments=deepcopy(ARGS));kw.update(changes)
    return store.open(**kw)

async def answer(store,row,sent,**changes):
    async def deliver(identifier,payload,guard):
        if not guard():raise RuntimeError('inactive')
        sent.append((identifier,payload))
    kw=dict(scope=SCOPE,binding=BINDING,action='allow_once',deliver=deliver,cancelled=asyncio.Event());kw.update(changes)
    return await store.answer(row['id'],**kw)

def claim(store,**changes):
    kw=dict(scope=SCOPE,binding=BINDING,metadata=deepcopy(META),tool_arguments=deepcopy(ARGS),cancelled=asyncio.Event());kw.update(changes)
    return store.claim_dispatch(**kw)

def started(call='call-0',thread='native-root',turn='turn-0',args=None):
    return {'method':'item/started','params':{'threadId':thread,'turnId':turn,'item':{'type':'mcpToolCall','id':call,'server':'original-server','tool':'create_document','arguments':deepcopy(ARGS if args is None else args),'status':'inProgress'}}}

def elicitation(native_id=0,thread='native-root',turn='turn-0'):
    return {'id':native_id,'method':'mcpServer/elicitation/request','params':{'threadId':thread,'turnId':turn,'serverName':'original-server','mode':'form','_meta':{'codex_approval_kind':'mcp_tool_call','tool_params':deepcopy(ARGS)},'requestedSchema':{'type':'object','properties':{}},'message':'Untrusted presentation is not authority.'}}

def router(store,*,binding=BINDING,cancelled=None):
    sent=[]
    async def write(value,guard=None):
        if guard is not None and not guard():raise RuntimeError('inactive')
        sent.append(deepcopy(value))
    async def resolve(server,tool):return binding
    value=NativeApprovalRouter(store,SCOPE,resolve_binding=resolve,cancelled=cancelled or asyncio.Event(),write=write)
    return value,sent

@pytest.mark.anyio
async def test_zero_native_id_exact_intent_private_identity_and_single_dispatch(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);sent=[]
    assert open_request(store)==row and row['status']=='pending'
    for secret in ['native-root','run:1','documents.write','package_sha256','connection_generation','owner']:
        assert secret not in json.dumps(row)
    approved=await answer(store,row,sent)
    assert approved['status']=='approved' and sent==[(0,{'action':'accept','content':{}})]
    assert await answer(NativeApprovals(tmp_path),row,sent)==approved and len(sent)==1
    assert claim(store)['dispatch']
    assert not claim(NativeApprovals(tmp_path))['dispatch']
    result=store.outcome(row['id'],scope=SCOPE,status='succeeded',receipt={'resource_id':'doc-1','url':'https://provider.example/doc/1'})
    assert claim(store)['request']==result
    assert not claim(store)['dispatch']

@pytest.mark.anyio
async def test_numeric_and_string_native_request_ids_are_distinct(tmp_path):
    store=NativeApprovals(tmp_path)
    a=open_request(store)
    b=open_request(store,native_request_id='0',call_id='call-1')
    assert a['id']!=b['id']
    assert store.resolve_native(SCOPE,'0')['id']==b['id']

@pytest.mark.parametrize('field,value',[('owner','bob'),('root_chat_id','other'),('company_id','other')])
@pytest.mark.anyio
async def test_foreign_owners_cannot_answer_or_read(tmp_path,field,value):
    store=NativeApprovals(tmp_path);row=open_request(store);sent=[]
    with pytest.raises(ApprovalError):await answer(store,row,sent,scope=replace(SCOPE,**{field:value}))
    with pytest.raises(ApprovalError):store.get(row['id'],scope=replace(SCOPE,**{field:value}))
    assert sent==[]

@pytest.mark.parametrize('binding',[replace(BINDING,access_mode='read'),replace(BINDING,write_enabled=False)])
def test_readonly_and_uncommissioned_connections_cannot_open(tmp_path,binding):
    with pytest.raises(ApprovalError):open_request(NativeApprovals(tmp_path),binding=binding)

def test_child_cannot_create_approval_and_shared_metadata_is_insufficient(tmp_path):
    store=NativeApprovals(tmp_path)
    with pytest.raises(ApprovalError):open_request(store,actor_chat_id='child')
    with pytest.raises(ApprovalError):claim(store)
    open_request(store)
    with pytest.raises(ApprovalError):claim(store)

@pytest.mark.parametrize('change',[{'connection_generation':2},{'package_generation':2},{'package_sha256':'c'*64},{'schema_sha256':'d'*64},{'access_mode':'read'},{'oauth_scopes':('documents.read',)}])
@pytest.mark.anyio
async def test_changed_provider_contract_invalidates_without_native_accept(tmp_path,change):
    store=NativeApprovals(tmp_path);row=open_request(store);sent=[]
    result=await answer(store,row,sent,binding=replace(BINDING,**change))
    assert result['status']=='invalidated' and sent==[]
    with pytest.raises(ApprovalError):claim(store,binding=replace(BINDING,**change))

@pytest.mark.anyio
async def test_generation_change_stop_and_expiry_prevent_delivery(tmp_path):
    now=[1.0];store=NativeApprovals(tmp_path,clock=lambda:now[0]);row=open_request(store)
    sent=[];now[0]=500
    assert (await answer(store,row,sent))['status']=='expired'
    row=open_request(store,native_request_id=1,call_id='next')
    assert (await answer(store,row,sent,scope=replace(SCOPE,generation='run:2')))['status']=='invalidated'
    row=open_request(store,native_request_id=2,call_id='next2')
    stop=asyncio.Event();stop.set()
    assert (await answer(store,row,sent,cancelled=stop))['status']=='cancelled'
    assert not sent

@pytest.mark.anyio
async def test_duplicate_concurrent_answer_and_conflicting_decision(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);sent=[];entered=asyncio.Event();release=asyncio.Event()
    async def deliver(identifier,payload,guard):
        entered.set();await release.wait()
        if not guard():raise RuntimeError('inactive')
        sent.append(identifier)
    first=asyncio.create_task(answer(store,row,sent,deliver=deliver));await entered.wait()
    second=await answer(NativeApprovals(tmp_path),row,sent)
    assert second['status']=='delivering'
    with pytest.raises(ApprovalError):await answer(store,row,sent,action='decline')
    release.set();assert (await first)['status']=='approved' and sent==[0]

@pytest.mark.anyio
async def test_stop_while_native_write_lock_is_queued_sends_zero_bytes(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);lock=asyncio.Lock();await lock.acquire();entered=asyncio.Event();sent=[];stop=asyncio.Event()
    async def deliver(identifier,payload,guard):
        entered.set()
        async with lock:
            if not guard():raise RuntimeError('inactive')
            sent.append(identifier)
    task=asyncio.create_task(answer(store,row,sent,deliver=deliver,cancelled=stop));await entered.wait()
    stop.set();store.invalidate(scope=SCOPE,reason='stopped');lock.release()
    assert (await task)['status']=='cancelled' and sent==[]
    with pytest.raises(ApprovalError):claim(store)

@pytest.mark.anyio
async def test_callback_skipping_native_guard_never_authorizes(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store)
    async def bad(*args):pass
    assert (await answer(store,row,[],deliver=bad))['status']=='invalidated'
    with pytest.raises(ApprovalError):claim(store)

@pytest.mark.anyio
async def test_lost_accept_reply_is_not_replayed(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);sent=[]
    async def lost(identifier,payload,guard):
        assert guard();sent.append(identifier);raise OSError('synthetic disconnect')
    assert (await answer(store,row,sent,deliver=lost))['status']=='delivery_uncertain'
    assert (await answer(store,row,sent))['status']=='delivery_uncertain' and sent==[0]
    with pytest.raises(ApprovalError):claim(store)

@pytest.mark.anyio
async def test_claim_uses_native_callid_not_model_itemid(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);await answer(store,row,[])
    for change in [{'callId':META['itemId']},{'threadId':'child'},{'x-codex-turn-metadata':{'turn_id':'wrong'}}]:
        with pytest.raises(ApprovalError):claim(store,metadata={**META,**change})
    with pytest.raises(ApprovalError):claim(store,tool_arguments={**ARGS,'content':'changed'})
    assert claim(store)['dispatch']

@pytest.mark.anyio
async def test_cross_process_claim_race_consumes_once(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);await answer(store,row,[])
    results=await asyncio.gather(*[asyncio.to_thread(lambda:claim(NativeApprovals(tmp_path))) for _ in range(5)])
    assert sum(r['dispatch'] for r in results)==1

@pytest.mark.anyio
async def test_stop_after_dispatch_remains_uncertain_and_blocks_fresh_duplicate(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);await answer(store,row,[]);assert claim(store)['dispatch']
    retired=store.invalidate(scope=SCOPE,reason='stopped')
    assert retired[0]['status']=='uncertain' and not claim(store)['dispatch']
    with pytest.raises(ApprovalError):open_request(store,native_request_id=1,call_id='again')
    receipt=store.outcome(row['id'],scope=SCOPE,status='succeeded',receipt={'resource_id':'document-accepted-before-stop'})
    assert receipt['status']=='succeeded'
    assert open_request(store,native_request_id=1,call_id='intentional-next')['status']=='pending'

@pytest.mark.anyio
async def test_already_pending_duplicates_cannot_dispatch_while_effect_uncertain(tmp_path):
    store=NativeApprovals(tmp_path);first=open_request(store);second=open_request(store,native_request_id=1,call_id='other')
    await answer(store,first,[]);await answer(store,second,[])
    assert claim(store)['dispatch']
    with pytest.raises(ApprovalError):claim(store,metadata={**META,'callId':'other'})

@pytest.mark.anyio
async def test_restart_preserves_uncertain_outcome_and_retires_pending(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);await answer(store,row,[]);claim(store)
    pending=open_request(store,native_request_id=1,call_id='other',tool_arguments={'title':'Different'})
    restored=NativeApprovals(tmp_path);result=restored.recover()
    assert {r['status'] for r in result}=={'uncertain','cancelled'}
    assert not claim(restored)['dispatch']

@pytest.mark.anyio
async def test_router_native_protocol_preserves_jsonrpc_zero_and_original_arguments(tmp_path):
    store=NativeApprovals(tmp_path);bridge,sent=router(store)
    assert await bridge.event(started(),actor_chat_id='root')==[]
    events=await bridge.event(elicitation(),actor_chat_id='root');assert len(events)==1
    assert events[0]['type']=='approval_request' and events[0]['request']['arguments_preview']==ARGS
    result=await bridge.answer(events[0]['request']['id'],owner='alice',action='allow_once')
    assert result['type']=='approval_resolved' and result['request']['status']=='approved'
    assert sent==[{'id':0,'result':{'action':'accept','content':{}}}]
    resolved=await bridge.event({'method':'serverRequest/resolved','params':{'threadId':'native-root','requestId':0}},actor_chat_id='root')
    assert resolved[0]['request']['status']=='approved'

@pytest.mark.anyio
@pytest.mark.parametrize('case',['child','ambiguous','no_observed_item','wrong_arguments','unsupported_form','disabled'])
async def test_router_denies_without_exact_host_observed_parent_intent(tmp_path,case):
    store=NativeApprovals(tmp_path);bridge,sent=router(store,binding=replace(BINDING,write_enabled=False) if case=='disabled' else BINDING)
    actor='child' if case=='child' else 'root';thread='native-child' if case=='child' else 'native-root'
    if case!='no_observed_item':await bridge.event(started(thread=thread),actor_chat_id=actor)
    if case=='ambiguous':await bridge.event(started('call-1'),actor_chat_id='root')
    request=elicitation(thread=thread)
    if case=='wrong_arguments':request['params']['_meta']['tool_params']={'title':'changed'}
    if case=='unsupported_form':request['params']['requestedSchema']['properties']={'secret':{'type':'string'}}
    assert await bridge.event(request,actor_chat_id=actor)==[]
    assert sent==[{'id':0,'result':{'action':'decline','content':None}}]
    assert store.for_chat(scope=SCOPE)==[]

@pytest.mark.anyio
async def test_native_completed_item_cannot_receive_late_approval(tmp_path):
    store=NativeApprovals(tmp_path);bridge,sent=router(store)
    await bridge.event(started(),actor_chat_id='root');row=(await bridge.event(elicitation(),actor_chat_id='root'))[0]['request']
    ended=started();ended['method']='item/completed';ended['params']['item']['status']='failed'
    await bridge.event(ended,actor_chat_id='root')
    result=await bridge.answer(row['id'],owner='alice',action='allow_once')
    assert result['request']['status']=='cancelled' and sent==[]

@pytest.mark.anyio
async def test_child_resolution_cannot_close_parent_approval(tmp_path):
    store=NativeApprovals(tmp_path);bridge,sent=router(store)
    await bridge.event(started(),actor_chat_id='root');row=(await bridge.event(elicitation(),actor_chat_id='root'))[0]['request']
    assert await bridge.event({'method':'serverRequest/resolved','params':{'threadId':'child','requestId':0}},actor_chat_id='child')==[]
    assert store.get(row['id'],scope=SCOPE)['status']=='pending'

@pytest.mark.anyio
async def test_expiry_sends_native_cancel_once_and_blocks_dispatch(tmp_path):
    now=[1.0];store=NativeApprovals(tmp_path,clock=lambda:now[0]);bridge,sent=router(store)
    await bridge.event(started(),actor_chat_id='root');row=(await bridge.event(elicitation(),actor_chat_id='root'))[0]['request']
    now[0]=301
    assert (await bridge.expire())[0]['request']['status']=='expired'
    assert sent==[{'id':0,'result':{'action':'cancel','content':None}}]
    assert await bridge.expire()==[]
    with pytest.raises(ApprovalError):claim(store)


def test_native_policy_is_opt_in_and_preserves_original_tool_discovery():
    from deploy.migration.legacy.native_policy import isolated_thread_params,provider_prompt_policy
    assert isolated_thread_params('',[])['approvalPolicy']=='never'
    policy=isolated_thread_params('',[],provider_approvals=True)['approvalPolicy']['granular']
    assert policy['mcp_elicitations'] and not any(policy[k] for k in policy if k!='mcp_elicitations')
    setting=provider_prompt_policy({'original-search':'read','new-original-tool':'unknown','create_document':'write'})
    assert setting=={'default_tools_approval_mode':'writes','tools':{'new-original-tool':{'approval_mode':'prompt'},'create_document':{'approval_mode':'prompt'}}}
    assert 'enabled_tools' not in setting and 'disabled_tools' not in setting

@pytest.mark.anyio
async def test_native_cleared_approval_while_reply_queued_delivers_zero(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);lock=asyncio.Lock();await lock.acquire();entered=asyncio.Event();sent=[]
    async def deliver(identifier,payload,guard):
        entered.set()
        async with lock:
            if not guard():raise RuntimeError('inactive')
            sent.append(identifier)
    task=asyncio.create_task(answer(store,row,sent,deliver=deliver));await entered.wait()
    store.resolve_native(SCOPE,0);lock.release()
    assert (await task)['status']=='cancelled' and sent==[]

@pytest.mark.anyio
async def test_expired_accepted_request_is_not_replied_to_twice(tmp_path):
    now=[100.0];store=NativeApprovals(tmp_path,clock=lambda:now[0]);bridge,sent=router(store)
    await bridge.event(started(),actor_chat_id='root')
    row=(await bridge.event(elicitation(),actor_chat_id='root'))[0]['request']
    await bridge.answer(row['id'],owner='alice',action='allow_once')
    now[0]=401
    assert (await bridge.expire())[0]['request']['status']=='expired'
    assert len(sent)==1 and sent[0]['result']['action']=='accept'

@pytest.mark.anyio
async def test_dispatch_expiry_is_durable_even_when_dispatch_is_denied(tmp_path):
    now=[100.0];store=NativeApprovals(tmp_path,clock=lambda:now[0]);row=open_request(store)
    await answer(store,row,[]);now[0]=401
    with pytest.raises(ApprovalError):claim(store)
    assert store.get(row['id'],scope=SCOPE)['status']=='expired'

@pytest.mark.anyio
async def test_completion_during_binding_lookup_cannot_open_late_approval(tmp_path):
    store=NativeApprovals(tmp_path);bridge,sent=router(store);entered=asyncio.Event();release=asyncio.Event()
    async def binding(server,tool):entered.set();await release.wait();return BINDING
    bridge.resolve_binding=binding
    await bridge.event(started(),actor_chat_id='root')
    pending=asyncio.create_task(bridge.event(elicitation(),actor_chat_id='root'))
    await entered.wait()
    completed=started();completed['method']='item/completed'
    await bridge.event(completed,actor_chat_id='root');release.set()
    assert await pending==[]
    assert store.for_chat(scope=SCOPE)==[] and sent[-1]['result']['action']=='decline'

@pytest.mark.anyio
async def test_fresh_changed_binding_at_dispatch_durably_invalidates_approved_intent(tmp_path):
    store=NativeApprovals(tmp_path);row=open_request(store);await answer(store,row,[])
    with pytest.raises(ApprovalError):claim(store,binding=replace(BINDING,connection_generation=2))
    assert store.get(row['id'],scope=SCOPE)['status']=='invalidated'
    with pytest.raises(ApprovalError):claim(store)

@pytest.mark.anyio
@pytest.mark.parametrize('changed_scope',[replace(SCOPE,generation='run:2'),replace(SCOPE,root_thread_id='replacement-root')])
async def test_later_runtime_cannot_settle_historical_dispatch(tmp_path,changed_scope):
    store=NativeApprovals(tmp_path);row=open_request(store);await answer(store,row,[]);claim(store)
    store.invalidate(scope=SCOPE,reason='stopped')
    assert store.get(row['id'],scope=changed_scope)['status']=='uncertain'
    with pytest.raises(ApprovalError):store.outcome(row['id'],scope=changed_scope,status='succeeded',receipt={'resource_id':'foreign-runtime'})
    assert store.get(row['id'],scope=SCOPE)['status']=='uncertain'
    assert store.outcome(row['id'],scope=SCOPE,status='succeeded',receipt={'resource_id':'verified-old-runtime'})['status']=='succeeded'
