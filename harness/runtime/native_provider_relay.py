"""Original provider tools under host authority and a consenting owner.

The host uses the sealed runtime selection or an explicitly reviewed direct
service policy. Direct services carry policy identity, never package metadata. The
model supplies only original tool arguments. No endpoint, principal, permission
or credential is accepted from provider annotations or tool arguments.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from copy import deepcopy
from contextlib import asynccontextmanager
import hashlib
import json
import re

from mcp import types
from mcp_connections import MCPConnections, ConnectionError, MAX_ARGUMENT_BYTES, MAX_RESULT_BYTES
from native_packages import NativePackageStore, RuntimePackages


class ProviderRelayError(ValueError): pass


async def _read(function,*args,**kwargs):
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:await asyncio.shield(task)
            except asyncio.CancelledError:continue
            except Exception:break
        if task.done() and not task.cancelled():task.exception()
        raise


@dataclass(frozen=True)
class ProviderReadGrant:
    server_name:str
    plugin_id:str|None
    provider:str
    connection_generation:int
    policy_sha256:str|None=None


@dataclass(frozen=True)
class _Route:
    package:object|None
    provider:str
    policy_revision:str|None=None
    policy_sha256:str|None=None


def _canonical_policy(value):
    if isinstance(value,dict):return {key:_canonical_policy(item) for key,item in value.items()}
    if isinstance(value,(set,frozenset)):return sorted(_canonical_policy(item) for item in value)
    if isinstance(value,(tuple,list)):return [_canonical_policy(item) for item in value]
    return value


class NativeProviderRelay:
    def __init__(self,store,connections,selection,*,company_id,principal_id):
        if (not isinstance(connections,MCPConnections) or not isinstance(principal_id,str) or not principal_id
                or connections.company_scope!=company_id
                or (store is not None and (not isinstance(store,NativePackageStore) or store.company_scope!=company_id))
                or (selection is not None and (not isinstance(selection,RuntimePackages) or store is None
                    or selection.company_scope!=company_id
                    or selection.owner_binding!=hashlib.sha256(principal_id.encode()).hexdigest()))):
            raise ProviderRelayError('provider_scope_mismatch')
        self.store,self.connections,self.selection=store,connections,selection
        self.company_id,self.principal_id=company_id,principal_id
        self.write_transport=None
        self._routes={};ambiguous=set()
        for package in selection.packages if selection is not None else ():
            entry=store.entries.get(package.id,{})
            if entry.get('supported_api') is not True:continue
            for server in entry.get('mcp_servers',[]):
                name=server.get('name')
                if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name)
                        or name=='rocoto' or server.get('transport') not in {'http','streamable-http'}):continue
                matches=[provider for provider,policy in connections.provider_policies().items()
                         if server.get('url')==policy['endpoint'] and connections.provider_available(provider)
                         and policy.get('native_direct') is not True
                         and (not policy.get('remote_descriptor') or
                              (package.id==policy['plugin_id'] and name==policy['server_name']
                               and package.version==policy['package_version']
                               and package.upstream_revision==policy['upstream_revision']
                               and store.snapshot_catalogue['revision']==policy['upstream_revision']))]
                if len(matches)!=1:continue
                if name in self._routes:ambiguous.add(name)
                self._routes[name]=_Route(package,matches[0])
        for provider,policy in connections.provider_policies().items():
            if policy.get('native_direct') is not True or not connections.provider_available(provider):continue
            name='rocoto_'+provider.replace('-','_')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name):continue
            if name in self._routes:ambiguous.add(name)
            revision,sha=self._direct_identity(provider)
            self._routes[name]=_Route(None,provider,revision,sha)
        for name in ambiguous:self._routes.pop(name,None)

    @property
    def server_names(self):return tuple(sorted(self._routes))

    @property
    def providers(self):return frozenset(route.provider for route in self._routes.values())

    def _direct_identity(self,provider):
        policy=self.connections.provider_policy(provider)
        if policy.get('native_direct') is not True or not self.connections.provider_available(provider):
            raise ProviderRelayError('provider_policy_changed')
        revision=self.connections.provider_policy_revision(provider)
        value={'provider':provider,'policy_revision':revision,'policy':_canonical_policy(policy)}
        return revision,hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    def _direct_current(self,route):
        if self._direct_identity(route.provider)!=(route.policy_revision,route.policy_sha256):
            raise ProviderRelayError('provider_policy_changed')

    def display_name(self,server_name):
        route=self._routes[server_name]
        if route.package is None:return self.connections.provider_policy(route.provider)['name']
        return self.store.entries.get(route.package.id,{}).get('name',route.provider)

    async def _current(self,name,expected=None):
        route=self._routes.get(name)
        if route is None:raise ProviderRelayError('provider_not_available')
        package,provider=route.package,route.provider
        if package is None:await _read(self._direct_current,route)
        else:
            current=await _read(self.store.list,principal_id=self.principal_id)
            installed=next((row for row in current['plugins'] if row['id']==package.id),None)
            if (current['generation']!=self.selection.generation or not installed
                    or installed.get('package_sha256')!=package.tree_sha256
                    or installed.get('upstream_revision')!=package.upstream_revision
                    or installed.get('version')!=package.version or not installed.get('enabled')):
                raise ProviderRelayError('provider_selection_changed')
        generation=await _read(self.connections.read_generation,provider,principal_id=self.principal_id)
        if package is None:await _read(self._direct_current,route)
        actual=ProviderReadGrant(name,package.id if package else None,provider,generation,route.policy_sha256)
        if expected is not None and actual!=expected:raise ProviderRelayError('provider_connection_changed')
        return actual

    async def grant(self,server_name):return await self._current(server_name)

    async def valid(self,grant):
        if not isinstance(grant,ProviderReadGrant):return False
        try:return await self._current(grant.server_name,grant)==grant
        except Exception:return False

    async def tools(self,grant):
        await self._current(grant.server_name,grant)
        response=await self.connections.tools(grant.provider,principal_id=self.principal_id,
                                              expected_generation=grant.connection_generation)
        await self._current(grant.server_name,grant)
        # Preserve the native Tool contract: name/title/description, input/output
        # schemas, original annotations, icons and other SDK metadata.
        return [types.Tool.model_validate(deepcopy(row)) for row in response['tools']]

    async def call(self,grant,name,arguments,*,metadata=None,cancelled=None,alive=lambda:True):
        if not isinstance(arguments,dict) or len(json.dumps(arguments,allow_nan=False).encode())>MAX_ARGUMENT_BYTES:
            raise ProviderRelayError('provider_arguments_invalid')
        await self._current(grant.server_name,grant)
        if self.write_transport is not None:
            prepared=await self.connections.execution_binding(grant.provider,name,principal_id=self.principal_id,expected_generation=grant.connection_generation)
            if not prepared['read_authorized']:
                return await self.write_transport.execute(grant.server_name,name,deepcopy(arguments),metadata,
                    cancelled=cancelled,alive=alive)
        response=await self.connections.call(grant.provider,name,deepcopy(arguments),principal_id=self.principal_id,
                                             expected_generation=grant.connection_generation)
        await self._current(grant.server_name,grant)
        result=response['result']
        if len(json.dumps(result,allow_nan=False).encode())>MAX_RESULT_BYTES:raise ProviderRelayError('provider_result_limit')
        return types.CallToolResult.model_validate(result)


    def configure_writes(self, approvals, scope, *, enabled=False, timeout=35):
        """Host commissioning seam; selecting read_write alone does not enable it."""
        from native_provider_write import NativeWriteTransport
        if scope.company_id!=self.company_id or scope.owner!=self.principal_id:
            raise ProviderRelayError('provider_scope_mismatch')
        self.write_transport=NativeWriteTransport(approvals,scope,prepare=self.prepare_write,
            admission=self.write_admission,credentials=self.write_credentials,recheck=self.write_recheck,
            enabled=enabled,timeout=timeout)
        return self.write_transport

    async def operation_kind(self,grant,name):
        if self.write_transport is None:return 'read'
        await self._current(grant.server_name,grant)
        data=await self.connections.execution_binding(grant.provider,name,principal_id=self.principal_id,
            expected_generation=grant.connection_generation)
        return 'read' if data['read_authorized'] else 'write'

    async def prepare_write(self,server,name):
        from native_approvals import ApprovalBinding
        from native_provider_write import PreparedWrite
        grant=await self._current(server)
        data=await self.connections.execution_binding(grant.provider,name,principal_id=self.principal_id,
            expected_generation=grant.connection_generation)
        await self._current(server,grant)
        route=self._routes[server];package=route.package
        binding=ApprovalBinding(grant.provider,server,name,data['endpoint'],data['connection_generation'],
            self.selection.generation if package else None,package.tree_sha256 if package else None,
            data['schema_sha256'],data['access_mode'],tuple(data['oauth_scopes']),
            bool(data.get('write_enabled') is True and self.write_transport is not None and self.write_transport.enabled),
            **({'authority_kind':'direct_provider','policy_revision':route.policy_revision,
                'policy_sha256':route.policy_sha256} if package is None else {}))
        return PreparedWrite(binding,types.Tool.model_validate(data['tool_contract']))

    async def approval_binding(self,server,name):
        return (await self.prepare_write(server,name)).binding

    @asynccontextmanager
    async def _authority_admission(self,server_name):
        route=self._routes.get(server_name)
        if route is None:raise ProviderRelayError('provider_not_available')
        if route.package is None:
            await _read(self._direct_current,route)
            yield
            await _read(self._direct_current,route)
        else:
            async with self.store.runtime_selection(principal_id=self.principal_id) as selection:
                if selection!=self.selection:raise ProviderRelayError('provider_selection_changed')
                yield

    @asynccontextmanager
    async def write_admission(self,binding):
        # Package changes and provider policy changes have their own shared
        # stores. Hold package then connection authority in a fixed order.
        async with self._authority_admission(binding.server_name):
            async with self.connections.dispatch_admission(binding.provider,principal_id=self.principal_id,
                    expected_generation=binding.connection_generation):
                self.write_recheck(binding)
                yield

    def write_credentials(self,binding):
        self.write_recheck(binding)
        credentials=self.connections.dispatch_credentials(binding.provider,principal_id=self.principal_id,
            expected_generation=binding.connection_generation,minimum_validity_seconds=self.write_transport.timeout+5)
        if (credentials.endpoint!=binding.endpoint or credentials.access_mode!=binding.access_mode
                or credentials.oauth_scopes!=binding.oauth_scopes):
            raise ProviderRelayError('provider_connection_changed')
        return credentials

    def write_recheck(self,binding):
        # SDK's HTTP writer is a child task, so it may check fresh metadata but
        # never asks for the parent task's credential again. The admission lease
        # remains held by the parent throughout this synchronous dispatch check.
        route=self._routes.get(binding.server_name)
        if route is None or route.provider!=binding.provider:raise ProviderRelayError('provider_policy_changed')
        if route.package is None:
            self._direct_current(route)
            if (binding.authority_kind!='direct_provider' or binding.policy_revision!=route.policy_revision
                    or binding.policy_sha256!=route.policy_sha256):raise ProviderRelayError('provider_policy_changed')
        elif binding.authority_kind!='native_package':raise ProviderRelayError('provider_policy_changed')
        generation=self.connections.read_generation(binding.provider,principal_id=self.principal_id)
        if generation!=binding.connection_generation:raise ProviderRelayError('provider_connection_changed')


    async def prompt_policy(self,grant):
        if self.write_transport is None or not self.write_transport.enabled:return {}
        from provider_tool_policy import provider_prompt_policy
        await self._current(grant.server_name,grant)
        response=await self.connections.tools(grant.provider,principal_id=self.principal_id,
            expected_generation=grant.connection_generation)
        await self._current(grant.server_name,grant)
        permissions=response.get('tool_permissions',{})
        return provider_prompt_policy({tool['name']:'read' if permissions.get(tool['name'])=='read' else 'unknown'
            for tool in response['tools']})

    async def provenance(self,server_name):
        """Host identity for provider artifacts; result fields supply no authority."""
        grant=await self._current(server_name)
        route=self._routes[server_name];package=route.package
        if package is None:
            return {'provider':grant.provider,'server_name':grant.server_name,'authority_kind':'direct_provider',
                'policy_revision':route.policy_revision,'policy_sha256':route.policy_sha256,
                'connection_generation':grant.connection_generation}
        return {'provider':grant.provider,'plugin_id':grant.plugin_id,'server_name':grant.server_name,
            'package_sha256':package.tree_sha256,'package_generation':self.selection.generation,
            'connection_generation':grant.connection_generation}

    @asynccontextmanager
    async def artifact_admission(self,server_name,expected_provenance):
        """Keep the package/account lease while persisting a provider result.

        The yielded synchronous guard is safe in a storage worker. Disconnect
        publishes revocation before waiting for this lease, so callers check it
        inside their transaction and again before exposing any saved card.
        """
        actual=await self.provenance(server_name)
        if actual!=expected_provenance:raise ProviderRelayError('provider_connection_changed')
        async with self._authority_admission(server_name):
            async with self.connections.admission(actual['provider'],principal_id=self.principal_id,
                    expected_generation=actual['connection_generation']):
                def current():
                    try:
                        route=self._routes[server_name]
                        if route.package is None:self._direct_current(route)
                        return self.connections.read_generation(actual['provider'],principal_id=self.principal_id)==actual['connection_generation']
                    except Exception:return False
                if not current():raise ProviderRelayError('provider_connection_changed')
                yield current
                if not current():raise ProviderRelayError('provider_connection_changed')
