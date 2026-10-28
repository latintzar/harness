"""Optional tree integration keeps default native workers write-disabled."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import time
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_approvals import NativeApprovals,NativeApprovalRouter,ApprovalScope
from deploy.migration.legacy.isolated_turn import NativeTree
from test_native_approvals import BINDING,started,elicitation

@pytest.fixture
def anyio_backend():return 'asyncio'

def tree_fixture(tmp_path,enabled=True):
    run={'generation':'run:1','cancel':asyncio.Event(),'ready':asyncio.Event(),'child_ids':set(),'tool_events':asyncio.Queue(),'deadline':time.monotonic()+30}
    class Connection:
        def __init__(self):self.writes=[]
        async def write(self,value,guard=None):
            if guard and not guard():raise RuntimeError('inactive')
            self.writes.append(value)
        async def rpc(self,method,params):
            if method=='thread/read':return {'thread':{'id':params['threadId'],'parentThreadId':'native-root','source':{'subAgent':{'thread_spawn':{'parent_thread_id':'native-root'}}},'status':{'type':'active'}}}
            if method=='thread/turns/list':return {'data':[]}
    connection=Connection();store=NativeApprovals(tmp_path)
    async def binding(server,tool):return BINDING
    router=NativeApprovalRouter(store,ApprovalScope('company','alice','root','run:1','native-root'),resolve_binding=binding,cancelled=run['cancel'],write=connection.write)
    if enabled:run['approval_router']=router
    agent=SimpleNamespace(children={'root':{'id':'root','owner_principal_id':'alice','dataset':'cafe','messages':[{'role':'assistant','content':''}]}},
        registry={'root':'native-root'},active={'root':run},_save_child=lambda *args:None,_remember=lambda *args:None,
        _record_event=lambda answer,event:answer.setdefault('events',[]).append(event))
    tree=NativeTree(agent,'root','cafe',run,connection,SimpleNamespace(scope=SimpleNamespace(company_id='company')),'native-root')
    return tree,router,connection

@pytest.mark.anyio
async def test_tree_routes_native_items_and_approval_without_enabling_provider_calls(tmp_path):
    tree,router,connection=tree_fixture(tmp_path)
    await tree.event(started());events=await tree.event(elicitation())
    assert len(events)==1 and events[0]['type']=='approval_request'
    assert not connection.writes
    await router.answer(events[0]['request']['id'],owner='alice',action='allow_once')
    assert connection.writes==[{'id':0,'result':{'action':'accept','content':{}}}]
    assert not tree.tools
    retired=tree.retire_questions('stopped')
    assert retired[0]['type']=='approval_resolved' and retired[0]['request']['status']=='cancelled'

@pytest.mark.anyio
async def test_default_tree_declines_write_approval_without_host_router(tmp_path):
    tree,_,connection=tree_fixture(tmp_path,enabled=False)
    assert await tree.event(elicitation())==[]
    assert connection.writes==[{'id':0,'result':{'action':'decline','content':None}}]

@pytest.mark.anyio
async def test_verified_native_child_approval_is_declined_at_tree_boundary(tmp_path):
    tree,router,connection=tree_fixture(tmp_path)
    await tree.event(started(thread='native-child'))
    assert 'native-child' in tree.threads and tree.threads['native-child']!='root'
    assert await tree.event(elicitation(thread='native-child'))==[]
    assert connection.writes[-1]['result']['action']=='decline'
    assert router.store.for_chat(scope=router.scope)==[]
