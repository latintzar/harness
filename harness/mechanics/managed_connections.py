"""Managed function calls reuse workspace connection approvals and effect receipts."""
from __future__ import annotations

import asyncio
import time

from native_approvals import ApprovalScope, NativeApprovalRouter
from workspace_identity import workspace_scope


SPEC={'type':'function','name':'write_connection',
    'description':"Request one provider action using its original schema from list_connection_tools. The owning conversation displays the exact action for approval, and the host executes an approved action once.",
    'inputSchema':{'type':'object','properties':{'provider':{'type':'string','maxLength':100},
        'tool':{'type':'string','maxLength':200},'arguments':{'type':'object'}},
        'required':['provider','tool','arguments'],'additionalProperties':False}}


class ManagedApprovalRouter(NativeApprovalRouter):
    """Use host-admitted Agents function identities instead of native MCP events."""
    def __init__(self,store,scope,*,relay,run,current):
        self.run,self.current=run,current
        self.pending_calls=set()
        super().__init__(store,scope,resolve_binding=relay.approval_binding,
            cancelled=run['cancel'],write=self.deliver)

    async def deliver(self,item,*,guard=None):
        # This is the local approval handoff. Provider dispatch has a separate,
        # durable single-use claim in the unchanged connection transport.
        if item.get('id') not in self.pending_calls or not self.current() or guard and not guard():
            raise ValueError('Continue through the current conversation approval.')

    async def request(self,server,tool,arguments,identity):
        if not self.current():raise ValueError('Use the current managed function call.')
        binding=await self.resolve_binding(server,tool)
        if not self.current():raise ValueError('Use the current managed function call.')
        remaining=min(300,self.run['deadline']-time.monotonic())
        if remaining<=0:raise ValueError('Continue with a current turn allowance.')
        call_id=identity['call_id']
        record=self.store.open(scope=self.scope,actor_chat_id=self.scope.root_chat_id,
            native_request_id=call_id,turn_id=identity['turn_id'],call_id=call_id,
            binding=binding,tool_arguments=arguments,ttl_seconds=remaining)
        self.pending_calls.add(call_id)
        self.run['tool_events'].put_nowait({'type':'approval_request','request':record})
        try:
            async with asyncio.timeout(remaining):
                while True:
                    if not self.current():raise ValueError('Use the current conversation approval.')
                    record=await asyncio.to_thread(self.store.get,record['id'],scope=self.scope)
                    if not self.current():raise ValueError('Use the current conversation approval.')
                    if record['status']=='approved':return record
                    if record['status'] not in {'pending','delivering'}:
                        raise ValueError('Continue from the saved provider action decision.')
                    await asyncio.sleep(.1)
        except BaseException:
            record=self.store.interrupt(record['id'],scope=self.scope)
            self.run['tool_events'].put_nowait({'type':'approval_resolved','request':record})
            raise
        finally:
            self.pending_calls.discard(call_id)


class ManagedConnections:
    def __init__(self,agent,chat_id,run,relay):
        self.agent,self.chat_id,self.run,self.relay=agent,chat_id,run,relay
        record=agent.children[chat_id]
        scope=ApprovalScope(workspace_scope(agent.state),record['owner_principal_id'],chat_id,
            run['generation'],run['managed_engine'].session_id)
        self.router=ManagedApprovalRouter(agent.native_approvals,scope,relay=relay,run=run,current=self.current)
        self.transport=relay.configure_writes(agent.native_approvals,scope,enabled=True)
        run['approval_router']=self.router

    def current(self):
        try:
            from agent_kind_policy import has_participant_reach_context
            record=self.agent.children[self.chat_id]
            return (self.agent.active.get(self.chat_id) is self.run and not self.run['cancel'].is_set()
                and self.run['admission'].current() and time.monotonic()<self.run['deadline']
                and (self.run.get('hosted_selection') is None or self.run['hosted_selection'].current())
                and (self.run.get('binding_current') is None or self.run['binding_current']())
                and not record.get('parent_id') and not has_participant_reach_context(self.agent,self.chat_id))
        except (KeyError,ValueError):return False

    async def execute(self,provider,tool,arguments,identity):
        if not self.current():raise ValueError('Use the active parent conversation for provider actions.')
        engine=self.run['managed_engine']
        if identity['binding_id']!=engine.binding['id'] or identity['request_id']!=engine.request['id']:
            raise ValueError('Use this admitted managed function identity.')
        names=[name for name in self.relay.server_names if self.relay._routes[name].provider==provider]
        if len(names)!=1:raise ValueError('Choose a commissioned provider connection.')
        server=names[0];grant=await self.relay.grant(server)
        if not self.current():raise ValueError('Use the current managed provider connection.')
        if await self.relay.operation_kind(grant,tool)!='write':
            raise ValueError('Use read_connection for this provider read.')
        approval=await self.router.request(server,tool,arguments,identity)
        metadata={'threadId':engine.session_id,'callId':identity['call_id'],
            'x-codex-turn-metadata':{'turn_id':identity['turn_id']}}
        try:
            result=await self.relay.call(grant,tool,arguments,metadata=metadata,
                cancelled=self.run['cancel'],alive=self.current)
            return {'provider':provider,'tool':tool,'read_only':False,'authority':'untrusted_source_data',
                'result':result.model_dump(mode='json',by_alias=True,exclude_none=True)}
        finally:
            record=self.router.store.get(approval['id'],scope=self.router.scope)
            self.run['tool_events'].put_nowait({'type':'approval_resolved','request':record})

    async def close(self):
        for event in self.router.retire():self.run['tool_events'].put_nowait(event)
        await self.transport.close()


async def write_connection(agent,chat_id,arguments):
    from native_provider_relay import NativeProviderRelay
    from managed_turn import current_managed_call
    identity=current_managed_call()
    run=agent.active.get(chat_id)
    if identity is None or run is None or not run.get('managed'):
        raise ValueError('Use the admitted managed parent function for provider actions.')
    lock = run.setdefault('managed_connections_lock', asyncio.Lock())
    async with lock:
        adapter = run.get('managed_connections')
        if adapter is None:
            store = agent.plugin_package_store
            owner = agent.children[chat_id]['owner_principal_id']
            async with store.runtime_selection(principal_id=owner) as selection:
                relay = NativeProviderRelay(store, agent._connections(), selection,
                    company_id=workspace_scope(agent.state), principal_id=owner)
            adapter = ManagedConnections(agent, chat_id, run, relay)
            run['managed_connections'] = adapter
            run['lifetime'].push_async_callback(adapter.close)
    return await adapter.execute(arguments['provider'], arguments['tool'], arguments['arguments'], identity)
