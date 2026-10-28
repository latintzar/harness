"""Workspace funding and revocable tool authority around OpenAI-managed execution."""
from __future__ import annotations

from contextlib import asynccontextmanager
import copy
import json
import time
from pathlib import Path

from company_usage import CompanyUsage
from managed_usage import ManagedUsage
from managed_spend import MAX_ACTIVE_SECONDS, SpendMonitor, estimate_cents, spending_mode as resolve_spending_mode
from public_mcp import HTTPMCPServices
from workspace_identity import workspace_scope

MODEL='gpt-6-astra'


class FundedMCP:
    """Bind both authority and money to the one provider-issued session."""
    def __init__(self,authority,usage,binding_id):
        self.authority=authority;self.usage=usage;self.binding_id=binding_id

    def created(self,session_id):
        self.usage.created(self.binding_id,session_id)
        self.authority.created(session_id)

    def current(self):return self.authority.current()


class ManagedLease:
    def __init__(self,configuration,mcp,generation,usage,request_id,*,steering=False,binding_id=None):
        self.configuration=configuration;self.mcp=mcp;self.generation=generation
        self.usage=usage;self.request_id=request_id;self.steering=steering;self.withdrawn=False
        self.binding_id=binding_id
        self.monitor = None

    def created(self, session_id):
        if self.mcp is None:self.usage.created(self.binding_id, session_id)
        else:self.mcp.created(session_id)

    def revoke(self,*,disconnected=False):
        if self.steering or self.withdrawn:return
        self.withdrawn=True
        if self.mcp is None:return
        if disconnected:self.mcp.authority.end(self.generation,disconnected=True)
        else:self.mcp.authority.revoke(self.generation)

    def persist_monitor(self, receipt):
        self.usage.save_monitor(self.request_id, receipt)

    async def finish(self,snapshot,*,terminal_status=None):
        if self.monitor is not None and not self.steering:
            if snapshot is not None:
                try:
                    self.monitor.observe(snapshot)
                except ValueError:
                    self.monitor.stop_reason = 'monitor_failed'
            self.persist_monitor(self.monitor.receipt())
        if snapshot is not None:
            if self.mcp is None:self.usage.created(self.binding_id,snapshot['id'])
            else:self.mcp.created(snapshot['id'])
        self.usage.finish(self.request_id,snapshot,terminal_status)
        if terminal_status and self.mcp is not None and not self.steering:
            self.usage.release_unused_roles(self.request_id)


class ManagedPolicy:
    """Finite per-turn authority backed by the owning workspace's funded API pool.

    Base configuration is a secret-free immutable identity. Stable MCP credentials
    are filled only in the provider request. OpenAI provisions the core and sandbox.
    Approximate spend chooses a stricter next limit, never a settled customer cost.
    """
    def __init__(self,*,turn_limit_cents=100,spending_mode=None):
        if type(turn_limit_cents) is not int or not 0<turn_limit_cents<=100:
            raise ValueError('Use the approved per-turn managed spending allowance.')
        self.turn_limit_cents=turn_limit_cents
        self.spending_mode = resolve_spending_mode(spending_mode)

    @staticmethod
    def _require(agent,chat_id,run,admission):
        if (agent.active.get(chat_id) is not run or run['cancel'].is_set()
                or run.get('spend_monitor') is not None and (run['spend_monitor'].deadline_at <= time.time()
                    or run['spend_monitor'].stop_reason is not None)
                or admission is None or not admission.current()
                or run.get('hosted_selection') is not None and not run['hosted_selection'].current()
                or run.get('binding_current') is not None and not run['binding_current']()):
            raise ValueError('Use the current authorized workspace turn.')
        grant=run.get('delegated_grant')
        if grant is not None and not grant.current():
            raise ValueError('Use the current parent grant for this declared job.')
        actor=grant.actor if grant is not None else agent._authority_principal(chat_id)
        if not actor:raise ValueError('Use the actual admitted conversation participant.')
        return actor

    def base_configuration(self,agent,chat_id,tools,instructions):
        from delegated_jobs import role_plan
        roles=role_plan(agent,chat_id)
        if roles:instructions += '\nDeclared managed read roles (use delegate_job):\n' + json.dumps(roles,sort_keys=True)
        instructions += (
            '\nParent function tools belong to the coordinator. Give stock subagents workspace reads '
            'through their advertised Rocoto MCP namespace, and include the exact advertised tool '
            'name in the delegated task. For example, use mcp__rocoto__get_harness with '
            'section="layers" when that tool is advertised. Have children promptly return unavailable '
            'reads to the coordinator so it can use its own tools.')
        host=getattr(agent,'managed_mcp_host',None)
        if host is None or host.transport is None:
            raise ValueError('Commission the workspace managed MCP endpoint before agent execution.')
        # This descriptor deliberately excludes the credential. Its bound owner,
        # collection and immutable server identity are hashed by ManagedSessions.
        remote={'type':'mcp','server_label':'rocoto','connection_origin':'service','required':True,
                'allowed_tools':sorted(agent.tool_registry.read_names('public')),
                'transport':{'type':'http','server_url':host.url}}
        return {**({'metadata': {'rocoto_spending_mode': 'monitored'}} if self.spending_mode == 'monitored' else {}),
                'agent':{'model':MODEL,'instructions':instructions,
                'reasoning':{'effort':'medium','summary':'concise'},'service_tier':'default',
                'multi_agent':{'enabled':True,'max_concurrent_subagents':2},
                'tools':copy.deepcopy(tools)+[{'type':'web_search'},remote]},
                'environment':{'type':'openai_hosted','container_size':'small','network':{'access':'enabled'}}}

    def _monitor(self, usage, request_id, run, target, model, *, recovering=False, baseline=None):
        saved = usage.monitor(request_id)
        if self.spending_mode != 'monitored':
            if saved is not None:
                raise ValueError('Recover using the original managed spending mode.')
            return None
        if saved is None:
            if recovering:
                raise ValueError('Recover the original monitored deadline and target.')
            deadline = min(time.time() + MAX_ACTIVE_SECONDS,
                           time.time() + run['deadline'] - time.monotonic())
            grant = run.get('delegated_grant')
            if grant is not None:
                parent = grant.parent_run.get('managed_lease')
                if parent is None or parent.monitor is None:
                    raise ValueError('Use the original parent spending monitor for this role.')
                deadline = min(deadline, parent.monitor.deadline_at)
            initial = SpendMonitor(target, deadline, model, baseline)
            saved = usage.monitor(request_id, initial.receipt())
        monitor = SpendMonitor.from_receipt(saved)
        if monitor.model != model or monitor.target_cents != target:
            raise ValueError('Recover the original monitored model and target.')
        if monitor.deadline_at <= time.time() or monitor.stop_reason or monitor.observed_cents >= monitor.target_cents:
            raise ValueError('Reconcile the original stopped monitored request.')
        run['deadline'] = min(run['deadline'], time.monotonic() + monitor.deadline_at - time.time())
        run['spend_monitor'] = monitor
        return monitor

    @asynccontextmanager
    async def _role_turn(self, agent, chat_id, *, session_id, request_id, binding_id, admission, run, recovering):
        grant = run['delegated_grant']
        actor = self._require(agent, chat_id, run, admission)
        configuration = copy.deepcopy(run['base_configuration'])
        expected = grant.configuration(run['tools'])
        if self.spending_mode == 'monitored':
            expected['metadata'] = {'rocoto_spending_mode': 'monitored'}
        if configuration != expected:
            raise ValueError('Use the original read role configuration.')
        usage = ManagedUsage(CompanyUsage.agent_pool(agent.state))
        usage.activate_role(parent_request=grant.job['funded_parent_request'], role_id=grant.role['id'],
            ordinal=grant.job['ordinal'], binding_id=binding_id, request_id=request_id, actor=actor,
            model=grant.role['model'], limit_cents=grant.role['limit_cents'],
            session_id=session_id, recovering=recovering)
        monitor = self._monitor(usage, request_id, run, grant.role['limit_cents'], grant.role['model'], recovering=recovering)
        if monitor is None:
            configuration['spend_control'] = {'limit': grant.role['limit_cents']}
        lease = ManagedLease(configuration, None, None, usage, request_id, binding_id=binding_id)
        lease.monitor = monitor
        try:
            self._require(agent, chat_id, run, admission)
            yield lease
        finally:
            lease.revoke()

    @asynccontextmanager
    async def turn(self,agent,chat_id,tools,instructions,*,session_id,request_id,binding_id,
                   admission,run,recovering=False,steering=False):
        if run.get('delegated_grant') is not None:
            if steering:raise ValueError('Continue a declared job through its parent request.')
            async with self._role_turn(agent, chat_id, session_id=session_id, request_id=request_id,
                    binding_id=binding_id, admission=admission, run=run, recovering=recovering) as lease:
                yield lease
            return
        actor=self._require(agent,chat_id,run,admission)
        record=agent.children[chat_id]
        configuration=copy.deepcopy(run.get('base_configuration')) if run.get('base_configuration') is not None else self.base_configuration(agent,chat_id,tools,instructions)
        pool=CompanyUsage.agent_pool(agent.state)
        if pool.state.resolve()!=CompanyUsage.agent_pool_path(agent.state).resolve():
            raise ValueError('Use only the owning workspace’s commissioned API pool.')
        usage=ManagedUsage(pool)
        prior=usage.request(request_id)
        roles=usage.role_plan(request_id) if recovering else run.get('role_plan', [])
        roles=roles if roles is not None else []
        if not steering:run['role_plan']=roles
        allowance=self.turn_limit_cents-sum(role['limit_cents']*role['per_turn'] for role in roles)
        if allowance <= 0:raise ValueError('Reserve parent capacity beside declared role allowances.')
        baseline = None
        if prior:
            limit=prior['limit_cents']
        elif recovering:
            raise ValueError('Recover the original funded request before another submission.')
        elif steering:
            primary=run.get('managed_lease')
            if primary is None or not primary.mcp.current():
                raise ValueError('Steer the current authorized managed turn.')
            # Steering shares the root turn's remaining allowance and MCP epoch;
            # extra messages never renew a background wake's monetary cap.
            limit=primary.usage.request(primary.request_id)['limit_cents']
        elif session_id is None:
            limit=allowance
        else:
            if run.get('provider_quiesced') is not True:
                raise ValueError('Reconcile the previous root and children before new allowance.')
            snapshot=await run['managed_client'].get_session(session_id)
            if self.spending_mode == 'monitored':
                if snapshot.get('id') != session_id:
                    raise ValueError('Monitor the original funded session.')
                baseline = estimate_cents(snapshot, configuration['agent']['model'])
                limit = usage.capacity(binding_id, session_id) + allowance
            else:
                limit = self._provider_limit(snapshot, session_id, usage, binding_id, allowance, roles)
        self._require(agent,chat_id,run,admission)
        authority=agent.managed_mcp_host.bind_session(binding_id,
            company_id=workspace_scope(agent.state),data_owner=record['owner_principal_id'],
            root_chat=chat_id,collection=record['dataset'],services=HTTPMCPServices(Path(agent.state),agent))
        reserve=usage.reserve_parent if roles and not steering else usage.reserve
        reserve(binding_id=binding_id,request_id=request_id,actor=actor,model=configuration['agent']['model'],
            limit_cents=limit,session_id=session_id,recovering=recovering,**({'roles':roles} if roles and not steering else {}))
        monitor = (run['managed_lease'].monitor if steering else self._monitor(usage, request_id, run, allowance,
            configuration['agent']['model'], recovering=recovering, baseline=baseline))
        generation=None
        if not steering:
            generation=authority.begin(actor=actor,admission=admission,cancel=run['cancel'],deadline=run['deadline'],
                current_run=lambda:agent.active.get(chat_id) is run
                    and (run.get('hosted_selection') is None or run['hosted_selection'].current())
                    and (run.get('binding_current') is None or run['binding_current']()),quiesced=run.get('provider_quiesced') is True,
                run=run,request_id=request_id,recovering=recovering)
        lease=None
        try:
            original_tools=configuration['agent']['tools']
            configuration['agent']['tools']=[tool for tool in original_tools if not (tool.get('type')=='mcp' and tool.get('server_label')=='rocoto')]+authority.tools
            if monitor is None:
                configuration['spend_control']={'limit':limit}
            lease=ManagedLease(configuration,FundedMCP(authority,usage,binding_id),generation,usage,request_id,steering=steering)
            lease.monitor = monitor
            self._require(agent,chat_id,run,admission)
            yield lease
        finally:
            if lease is not None:
                # Runtime explicitly finishes/revokes successful and stopped turns.
                # An uncertain provider outcome keeps the same request recoverable.
                lease.revoke(disconnected=True)
            elif generation is not None:
                authority.revoke(generation)
            if not steering:await authority.drain()

    @staticmethod
    def _provider_limit(snapshot, session_id, usage, binding_id, allowance, roles):
        control = snapshot.get('spend_control', {})
        consumed, previous = control.get('consumed'), control.get('limit')
        if (snapshot.get('id') != session_id or type(previous) is not int or previous <= 0
                or consumed is not None and (type(consumed) is not int or consumed < 0)):
            raise ValueError('Read the funded session’s finite provider spending control.')
        capacity = usage.capacity(binding_id, session_id)
        if previous > capacity or consumed is not None and consumed > capacity:
            raise ValueError('Reconcile provider spending beyond the original funded authority.')
        if roles and consumed is None:
            raise ValueError('Read current provider consumption before allocating delegated work.')
        return previous if consumed is None else consumed + allowance
