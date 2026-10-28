"""Managed provider actions retain exact approval, authority and dispatch receipts."""
import asyncio
from types import SimpleNamespace
import time

import pytest
from managed_connections import ManagedConnections
from native_approvals import NativeApprovals
from native_provider_write import NativeWriteTransport
from test_native_provider_write import Provider,Authority,ARGS


def fixture(tmp_path):
    provider=Provider();authority=Authority()
    class Relay:
        server_names=('fixture-server',)
        _routes={'fixture-server':SimpleNamespace(provider='fixture')}
        async def grant(self,name):return SimpleNamespace(server_name=name)
        async def operation_kind(self,grant,tool):return 'write'
        async def approval_binding(self,server,tool):return (await authority.prepare(server,tool)).binding
        def configure_writes(self,store,scope,enabled):
            self.transport=NativeWriteTransport(store,scope,prepare=authority.prepare,
                admission=authority.admission,credentials=authority.credentials,
                enabled=enabled,transport_factory=provider.transport)
            return self.transport
        async def call(self,grant,tool,arguments,**kwargs):
            return await self.transport.execute(grant.server_name,tool,arguments,kwargs['metadata'],
                cancelled=kwargs['cancelled'],alive=kwargs['alive'])
    admitted=[True]
    run={'managed':True,'generation':'generation-1','cancel':asyncio.Event(),
        'deadline':time.monotonic()+15,'tool_events':asyncio.Queue(),
        'admission':SimpleNamespace(current=lambda:admitted[0]),
        'managed_engine':SimpleNamespace(session_id='managed-session',binding={'id':'binding-1'},request={'id':'request-1'})}
    agent=SimpleNamespace(state=tmp_path,children={'root':{'id':'root','owner_principal_id':'alice','dataset':'company'}},
        active={'root':run},native_approvals=NativeApprovals(tmp_path))
    adapter=ManagedConnections(agent,'root',run,Relay())
    identity={'binding_id':'binding-1','request_id':'request-1','turn_id':'turn-1','call_id':'call-1'}
    return provider,authority,admitted,run,adapter,identity


def test_managed_action_requires_exact_human_approval_and_dispatches_once(tmp_path):
    async def run_test():
        provider,authority,admitted,run,adapter,identity=fixture(tmp_path)
        async with provider.app.router.lifespan_context(provider.app):
            task=asyncio.create_task(adapter.execute('fixture','create_document',ARGS,identity))
            event=await asyncio.wait_for(run['tool_events'].get(),2)
            assert event['type']=='approval_request' and provider.effects==[]
            request=event['request']
            assert request['arguments_preview']==ARGS
            with pytest.raises(ValueError):await adapter.router.answer(request['id'],owner='bob',action='allow_once')
            decision=await adapter.router.answer(request['id'],owner='alice',action='allow_once')
            assert decision['request']['status']=='approved'
            result=await asyncio.wait_for(task,3)
            assert result['result']['structuredContent']=={'id':'document-1'}
            assert provider.effects==['Report']
            receipt=await run['tool_events'].get()
            assert receipt['request']['status']=='succeeded'
            with pytest.raises(ValueError):await adapter.execute('fixture','create_document',ARGS,identity)
            assert provider.effects==['Report']
            await adapter.close()
    asyncio.run(run_test())


@pytest.mark.parametrize('change',['decline','stop','revoked','changed_connection'])
def test_withdrawn_managed_action_makes_no_provider_request(tmp_path,change):
    async def run_test():
        provider,authority,admitted,run,adapter,identity=fixture(tmp_path)
        task=asyncio.create_task(adapter.execute('fixture','create_document',ARGS,identity))
        event=await asyncio.wait_for(run['tool_events'].get(),2)
        request=event['request']
        if change=='stop':run['cancel'].set()
        elif change=='revoked':admitted[0]=False
        else:
            if change=='changed_connection':authority.available=False
            await adapter.router.answer(request['id'],owner='alice',action='decline' if change=='decline' else 'allow_once')
        with pytest.raises(ValueError):await asyncio.wait_for(task,2)
        assert provider.requests==[]
        assert adapter.router.store.get(request['id'],scope=adapter.router.scope)['status']!='approved'
        await adapter.close()
    asyncio.run(run_test())


def test_wrong_managed_call_and_tracker_context_cannot_open_approval(tmp_path):
    async def run_test():
        provider,authority,admitted,run,adapter,identity=fixture(tmp_path)
        with pytest.raises(ValueError):await adapter.execute('fixture','create_document',ARGS,{**identity,'request_id':'other'})
        adapter.agent.children['root']['agent_kind']='tracker'
        with pytest.raises(ValueError):await adapter.execute('fixture','create_document',ARGS,identity)
        assert run['tool_events'].empty() and provider.requests==[]
        await adapter.close()
    asyncio.run(run_test())


@pytest.mark.parametrize('fence', ['hosted_selection', 'binding_current'])
def test_generation_withdrawal_while_approval_waits_blocks_direct_provider_dispatch(tmp_path, fence):
    async def run_test():
        provider, authority, admitted, run, adapter, identity = fixture(tmp_path)
        current = [True]
        run[fence] = SimpleNamespace(current=lambda: current[0]) if fence == 'hosted_selection' else lambda: current[0]
        async with provider.app.router.lifespan_context(provider.app):
            task = asyncio.create_task(adapter.execute('fixture', 'create_document', ARGS, identity))
            request = (await asyncio.wait_for(run['tool_events'].get(), 2))['request']
            current[0] = False
            await adapter.router.answer(request['id'], owner='alice', action='allow_once')
            with pytest.raises(ValueError):
                await asyncio.wait_for(task, 2)
            assert provider.requests == [] and provider.effects == []
            await adapter.close()
    asyncio.run(run_test())


def test_concurrent_first_calls_share_one_router_and_transport(tmp_path, monkeypatch):
    from contextlib import AsyncExitStack, asynccontextmanager
    import managed_connections
    import managed_turn
    import native_provider_relay
    async def run_test():
        provider, authority, admitted, run, original, identity = fixture(tmp_path)
        agent = original.agent
        created = []
        class Store:
            @asynccontextmanager
            async def runtime_selection(self, **kwargs):
                await asyncio.sleep(0)
                yield None
        agent.plugin_package_store = Store()
        agent._connections = lambda: None
        run['lifetime'] = AsyncExitStack()
        monkeypatch.setattr(managed_turn, 'current_managed_call', lambda: identity)
        monkeypatch.setattr(native_provider_relay, 'NativeProviderRelay', lambda *args, **kwargs: original.relay)
        class Adapter:
            def __init__(self, *args):
                created.append(self)
            async def execute(self, *args):
                await asyncio.sleep(0)
                return self
            async def close(self):
                pass
        monkeypatch.setattr(managed_connections, 'ManagedConnections', Adapter)
        arguments = {'provider': 'fixture', 'tool': 'create_document', 'arguments': ARGS}
        results = await asyncio.gather(*(managed_connections.write_connection(agent, 'root', arguments) for _ in range(2)))
        assert len(created) == 1 and results[0] is results[1]
        await run['lifetime'].aclose()
        await original.close()
    asyncio.run(run_test())


@pytest.mark.parametrize('decision', ['allow', 'stop'])
def test_actual_managed_function_streams_approval_and_saves_one_provider_effect(tmp_path, decision):
    """Full managed turn; only OpenAI HTTP is doubled, provider MCP is real ASGI."""
    from host import Admission
    from managed_codex import ManagedEvent
    from managed_sessions import ManagedSessions
    from scope_host import bind_admission
    from test_managed_runtime import fixture as runtime_fixture, turn_event
    from workspace_identity import workspace_scope
    from managed_connections import SPEC
    async def run_test():
        provider, authority, admitted, original_run, original, identity = fixture(tmp_path)
        agent, runtime, policy, client = runtime_fixture(tmp_path)
        agent.native_approvals = original.agent.native_approvals
        agent.children['chat']['owner_principal_id'] = 'alice'
        agent._authority_principal = lambda chat: 'alice'
        agent.tool_registry = SimpleNamespace(reads=frozenset(), writes=frozenset({'write_connection'}),
            specs=lambda **kwargs: [SPEC])
        async def execute(name, arguments, dataset, chat_id):
            from managed_turn import current_managed_call
            run = agent.active[chat_id]
            adapter = ManagedConnections(agent, chat_id, run, original.relay)
            run['managed_connections'] = adapter
            run['lifetime'].push_async_callback(adapter.close)
            result = await adapter.execute(arguments['provider'], arguments['tool'], arguments['arguments'], current_managed_call())
            return [{'type': 'inputText', 'text': str(result)}], None, True
        agent._scoped_tool = execute
        created = client.create_session
        action = {'type': 'function_call', 'turn_id': 'turn-1', 'call_id': 'call-1',
            'name': 'write_connection', 'arguments': {'provider': 'fixture', 'tool': 'create_document', 'arguments': ARGS}}
        async def create(config, *, submission_key):
            session = await created(config, submission_key=submission_key)
            client.turns = [turn_event('in_progress').raw['turn']]
            session['required_actions'] = [action]
            return session
        client.create_session = create
        interrupt = client.interrupt
        async def stop(session_id, *, submission_key):
            await interrupt(session_id, submission_key=submission_key)
            client.session['required_actions'] = []
        client.interrupt = stop
        submitted = []
        async def submit(session_id, events, *, submission_key):
            submitted.extend(events)
            client.session['required_actions'] = []
        client.submit_events = submit
        client.events = [turn_event('completed')]
        async with provider.app.router.lifespan_context(provider.app):
            with bind_admission(Admission(lambda: True)):
                async with asyncio.timeout(5):
                    events = []
                    async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture'):
                        events.append(event)
                        if event['type'] == 'approval_request':
                            assert not provider.requests
                            run = agent.active['chat']
                            if decision == 'stop':
                                await runtime.control(agent, 'chat', 'stop')
                            else:
                                await run['approval_router'].answer(event['request']['id'], owner='alice', action='allow_once')
            if decision == 'stop':
                assert events[-1]['status'] == 'interrupted'
                assert provider.requests == [] and submitted == []
                statuses = [event['request']['status'] for event in events if event['type'] == 'approval_resolved']
                assert 'cancelled' in statuses
                assert not agent.active
                await original.close()
                return
            assert events[-1]['status'] == 'completed'
            assert provider.effects == ['Report']
            assert submitted[0]['call_id'] == 'call-1' and submitted[0]['success'] is True
            ledger = ManagedSessions(tmp_path)
            binding = ledger.for_chat(workspace_scope(tmp_path), 'chat')
            with ledger.transaction() as db:
                call = db.execute('SELECT * FROM calls WHERE binding_id=?', (binding['id'],)).fetchone()
                assert call['call_id'] == 'call-1' and call['state'] == 'completed'
            statuses = [event['request']['status'] for event in events if event['type'] == 'approval_resolved']
            assert 'succeeded' in statuses
            assert not agent.active
            await original.close()
    asyncio.run(run_test())


def test_custom_declared_class_changes_restrict_managed_effects_immediately(tmp_path):
    provider,authority,admitted,run,adapter,identity=fixture(tmp_path)
    record=adapter.agent.children['root'];record['agent_kind']='fulfillment'
    adapter.agent.automation_store=SimpleNamespace(agent_classes={'fulfillment':{'authority':'granted_effects'}})
    assert adapter.current()
    adapter.agent.automation_store.agent_classes['fulfillment']['authority']='participant_reach'
    assert not adapter.current() and provider.requests==[]
