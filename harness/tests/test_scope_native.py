"""Actual host/native adapters with synthetic process/network fixtures only."""
import asyncio
from types import SimpleNamespace
import pytest
from test_scope_host import host_fixture
from business_scope import ScopeActor
from scope_host import agent_scope,scope_current
from agent_bridge import CodexVideoAgent
from deploy.migration.legacy.isolated_native import IsolatedNativeLauncher
from test_isolated_native import auth,Broker,MCP,SCOPE
from native_executor import NativeLease,NativeScope
from test_native_output_publication import fixture as publication_fixture

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
async def test_new_authority_resets_native_thread_and_old_context_is_not_injected(tmp_path):
    selected,store,_,_,_,k=host_fixture(tmp_path)
    agent=CodexVideoAgent(tmp_path,selected['index'],lambda:{})
    agent.children=selected['agent'].children
    agent.children['chat']['messages']=[]
    selected['agent']=agent;agent.business_scope_host=selected
    agent.managed_runtime=SimpleNamespace()
    agent.registry['chat']='old-unscoped-native-thread'
    async with agent_scope(agent,'chat','business',admit=True) as view:
        first=agent.children['chat']['scope_workspace_binding']
        assert agent.registry['chat'] is None and agent.children['chat']['scope_fresh_native']
        assert [r['id'] for r in view._resource.clips]==['0'] and scope_current(view)
    agent.registry['chat']='scoped-native-thread'
    async with agent_scope(agent,'chat','business',admit=True):
        assert agent.registry['chat']=='scoped-native-thread'
        assert agent.children['chat']['scope_workspace_binding']==first
    actor=ScopeActor('company','alice')
    store.bind_chat(actor,'chat','business',[],store.effective(actor,'business')['revision'])
    async with agent_scope(agent,'chat','business',admit=True) as view:
        assert view._resource.clips==[]
        assert agent.registry['chat'] is None
        assert agent.children['chat']['scope_workspace_binding']!=first
    assert len(agent.children['chat']['scope_continuations'])==2

@pytest.mark.anyio
async def test_revocation_after_await_cannot_return_tool_result(tmp_path,monkeypatch):
    selected,store,_,_,save,k=host_fixture(tmp_path)
    agent=CodexVideoAgent(tmp_path,selected['index'],lambda:{})
    agent.children=selected['agent'].children;agent.children['chat']['messages']=[]
    selected['agent']=agent;agent.business_scope_host=selected
    async def tool(*args):
        save('members',{'member_id':'alice','role':'alert_recipient','location_ids':[k['a']],'function_ids':[]})
        return [{'type':'inputText','text':'private-late-result'}],None,True
    monkeypatch.setattr(agent,'_scoped_tool',tool)
    parts,results,success=await agent._tool('inspect_clip',{'clip_id':'0'},'business','chat')
    assert not success and 'private-late-result' not in str(parts) and results is None

@pytest.mark.anyio
async def test_physical_native_workspace_changes_without_changing_logical_mcp_chat(tmp_path):
    seen=[]
    class ScopedBroker(Broker):
        async def prepare(self,scope):
            seen.append(scope)
            return await super().prepare(scope)
    for index,binding in enumerate(['a'*64,'b'*64]):
        directory=tmp_path/str(index);directory.mkdir()
        broker=ScopedBroker(directory)
        launcher=IsolatedNativeLauncher(auth(directory),company_id='demo',enabled=True,client=broker)
        async with launcher.lease(SCOPE,run_id='synthetic-'+str(index),workspace_binding=binding) as session:
            assert session.scope==SCOPE
            assert session.storage_scope!=SCOPE and session.storage_scope.key==session.lease.scope_key
            await session.bind_mcp(MCP())
        assert broker.stopped==1
    assert seen[0].key!=seen[1].key and all(value.key!=SCOPE.key for value in seen)

@pytest.mark.anyio
async def test_output_snapshot_uses_partition_and_file_retains_original_chat(tmp_path):
    store,broker,session,publisher=publication_fixture(tmp_path)
    logical=session.scope
    physical=NativeScope(logical.company_id,logical.owner_id,logical.root_chat_id+':scope:'+'a'*64)
    session.storage_scope=physical;session.lease=NativeLease(physical.key,1,'a'*48,'/private/sockets')
    value=await publisher.publish(session,actor_chat_id=logical.root_chat_id,path='report.docx')
    assert value['chat_id']==logical.root_chat_id and store.download(value['id'],owner_principal_id=logical.owner_id)[1]==broker.raw
    assert all(call[1]==physical for call in broker.calls)

@pytest.mark.anyio
async def test_scheduler_candidate_view_is_never_widened(tmp_path):
    from execution_policy import RETRIEVAL_VIEW
    from retail_scope_views import selected_index_view
    selected,_,_,_,_,_=host_fixture(tmp_path)
    agent=CodexVideoAgent(tmp_path,selected['index'],lambda:{})
    agent.children=selected['agent'].children;agent.children['chat']['messages']=[]
    selected['agent']=agent;agent.business_scope_host=selected
    # This scheduler has already screened out every clip. Location grants cannot
    # reintroduce even an otherwise authorized recording.
    screened=selected_index_view(selected['index'].snapshot('alice'),[])
    token=RETRIEVAL_VIEW.set(screened)
    try:
        agent.managed_runtime=SimpleNamespace()
        async with agent_scope(agent,'chat','business',admit=True) as view:
            assert view._resource.clips==[]
            # A server listener may receive a request in a fresh task context.
            # The host's admitted root view still limits its tool's retrieval.
            detached=RETRIEVAL_VIEW.set(None)
            try:
                async with agent_scope(agent,'chat','business') as tool_view:assert tool_view._resource.clips==[]
            finally:RETRIEVAL_VIEW.reset(detached)
    finally:RETRIEVAL_VIEW.reset(token)

@pytest.mark.anyio
async def test_managed_bootstrap_after_selector_change_keeps_prior_history_private(tmp_path):
    import copy
    import json
    from managed_test_support import ManagedClient
    from managed_runtime import ManagedRuntime
    from test_managed_runtime import Policy
    selected, store, _, _, _, _ = host_fixture(tmp_path)
    agent = CodexVideoAgent(tmp_path)
    agent.children = selected['agent'].children
    agent.children['chat']['messages'] = []
    selected['agent'] = agent
    agent.business_scope_host = selected
    agent.capability_snapshot = lambda *args: None
    agent.agent_setup_context = lambda *args: None
    class Configuration(Policy):
        revision = 1
        def base_configuration(self, *args):
            config = super().base_configuration(*args)
            config['agent']['instructions'] += f' Revision {self.revision}'
            return config
    class Protocol(ManagedClient):
        async def create_session(self, config, *, submission_key):
            if self.created:
                self.old_session = copy.deepcopy(self.session)
                self.old_turns = copy.deepcopy(self.turns)
                self.session = {'id': f'session-{len(self.created) + 1}',
                    'agent': {'id': f'agent-{len(self.created) + 1}'}, 'status': 'idle', 'required_actions': []}
                self.turns = []
                self._next_turn()
            return await super().create_session(config, submission_key=submission_key)
        async def get_session(self, session_id):
            return self.old_session if hasattr(self, 'old_session') and session_id == self.old_session['id'] else self.session
        async def list_turns(self, session_id, **kwargs):
            return {'data': self.old_turns if hasattr(self, 'old_session') and session_id == self.old_session['id'] else self.turns,
                'has_more': False}
    protocol = Protocol()
    policy = Configuration()
    agent.managed_runtime = ManagedRuntime(lambda: protocol, policy, timeout=5)
    for iteration in range(3):
        result = [event async for event in agent.turn('Review current selection', 'chat', 'business')]
        assert result[-1].get('status') == 'completed', result
        assert len(protocol.created) == iteration + 1
        assert not protocol.sent
        assert 'old-scope-private-evidence' not in json.dumps(protocol.created[-1][0]['input'])
        if iteration == 0:
            agent.children['chat']['messages'][-1]['content'] = 'old-scope-private-evidence'
            agent._save_child(agent.children['chat'])
            actor = ScopeActor('company', 'alice')
            store.bind_chat(actor, 'chat', 'business', [], store.effective(actor, 'business')['revision'])
        elif iteration == 1:
            policy.revision += 1

@pytest.mark.anyio
async def test_local_mcp_requires_live_host_membership_after_configuration(tmp_path,monkeypatch):
    import mcp_server
    from test_local_mcp_scope import Index,legacy,call
    from mcp.server.fastmcp.exceptions import ToolError
    from retail_business_scope import RetailBusinessScope as BusinessScope
    from workspace_identity import workspace_scope
    import retail_mcp_services
    monkeypatch.setattr(retail_mcp_services,'QdrantRetrievalIndex',lambda _:Index([legacy('sample','business')]))
    server=mcp_server.make_server(tmp_path,principal_id='alice',dataset='business')
    assert len((await call(server,'list_cameras'))['videos'])==1
    company=workspace_scope(tmp_path)
    store=BusinessScope(tmp_path,company_id=company,member=lambda _:{'role':'owner'})
    store.mutate(ScopeActor(company,'owner'),'business','locations',{'name':'North'},expected_revision=0)
    with pytest.raises(ToolError,match='authenticated workspace connection'):
        await call(server,'list_cameras')
