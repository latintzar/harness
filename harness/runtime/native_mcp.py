"""Inherited native Codex tools through the standard MCP Streamable HTTP protocol.

The host creates one application per active root run, serves it on that run's
private Unix socket, and installs a short-lived bearer in native MCP config.
The sandbox relay forwards /mcp unchanged; it supplies no trusted identity.
The default grant can be inherited by children and contains only read tools.
An actor-specific grant requires host-verified saved chat/native thread lineage.
Neither tool arguments nor HTTP headers select the owner, actor or collection.

Root writes stay on the existing dynamic-tool path: current threaded handlers
have no transactional cancellation fence. Cancellation here stops async reads
and withholds late results; an already-running read/cache worker may finish.
This module is a transport adapter, not an agent loop or a provider client.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import secrets
import time
from typing import Callable

from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from agent_kind_policy import has_participant_reach_context


def __getattr__(name):
    if name == 'READ_TOOLS':
        from agent_bridge import workspace_registry
        return workspace_registry().read_names('native')
    raise AttributeError(name)


MAX_ARGUMENT_BYTES = 128 * 1024
MAX_RESULT_BYTES = 32 * 1024 * 1024
_REQUEST_GRANT: ContextVar[object | None] = ContextVar('rocoto_native_mcp_grant', default=None)


@dataclass(frozen=True)
class NativeMCPScope:
    company_id: str
    owner_principal_id: str
    dataset: str
    root_chat_id: str
    root_thread_id: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not 1 <= len(value) <= 256
               for value in vars(self).values()):
            raise ValueError('A saved workspace scope is required.')


@dataclass(frozen=True)
class NativeMCPGrant:
    id: str
    token: str = field(repr=False)
    expires_at_monotonic: float


@dataclass(eq=False)
class _Binding:
    id: str
    digest: str = field(repr=False)
    expires: float
    lineage: tuple = field(repr=False)
    admission: object = field(repr=False)
    revoked: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    provider: object | None = field(default=None, repr=False)


def _error(message='This tool is available through the active parent workspace.'):
    return types.CallToolResult(isError=True, content=[types.TextContent(type='text', text=message)])


class NativeMCP:
    """Host-owned ASGI app, with explicit lifetime and in-memory revocable grants.

    Issue grants inside the admitted native run's context to capture its immutable
    retrieval view and compute-admission callback. Host code must call revoke_all
    on Stop before releasing the sandbox; run cancellation is also watched here.
    The owner/company authorization remains the host's responsibility at creation.
    No issuance, identity selection or credential setup API is exposed over MCP.
    """
    def __init__(self, agent, scope: NativeMCPScope, *, company_id: str,
                 clock: Callable[[], float] = time.monotonic, call_timeout: float = 120, provider_relay=None):
        if not 0 < call_timeout <= 120:
            raise ValueError('Choose a tool timeout up to 120 seconds.')
        self.agent, self.scope, self.company_id = agent, scope, company_id
        if provider_relay is not None:
            from native_provider_relay import NativeProviderRelay
            if (not isinstance(provider_relay, NativeProviderRelay) or provider_relay.company_id != company_id
                    or provider_relay.principal_id != scope.owner_principal_id):
                raise ValueError('Choose a provider relay for this exact workspace owner.')
        self.provider_relay = provider_relay
        self._clock, self._call_timeout = clock, call_timeout
        self._root_run = agent.active.get(scope.root_chat_id)
        self._bindings: dict[str, _Binding] = {}
        self._calls: set[asyncio.Task] = set()
        self._running = False
        from agent_bridge import workspace_registry
        self._specs = {spec['name']: spec for spec in workspace_registry(agent).specs(surface='native')}
        self._validators = {name:Draft202012Validator(spec['inputSchema']) for name, spec in self._specs.items()}
        self.server = Server('rocoto-workspace', version='1.0.0')

        @self.server.list_tools()
        async def list_tools():
            binding = _REQUEST_GRANT.get()
            if not self._valid(binding):
                return []
            if binding.provider is not None:
                return await self._provider_read(binding, lambda: self.provider_relay.tools(binding.provider)) or []
            return [types.Tool(name=name, description=spec['description'], inputSchema=deepcopy(spec['inputSchema']),
                               annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
                    for name, spec in self._specs.items()]

        # SDK default validation includes invalid argument values in its errors.
        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            context=self.server.request_context
            metadata=context.meta.model_dump(mode='json',by_alias=True,exclude_none=True) if context.meta else {}
            return await self._call(_REQUEST_GRANT.get(), name, arguments, metadata=metadata)

        self.manager = StreamableHTTPSessionManager(self.server, stateless=True, json_response=True,
            max_request_body_size=MAX_ARGUMENT_BYTES + 8192, max_sessions=64,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                allowed_hosts=['127.0.0.1:8766', 'localhost:8766'], allowed_origins=[]))

    def _lineage(self, chat_id, thread_id):
        if self.company_id != self.scope.company_id or self._root_run is None:
            raise ValueError('Choose a verified active workspace.')
        rows, seen = [], set()
        while chat_id and len(rows) < 8 and chat_id not in seen:
            seen.add(chat_id)
            record = self.agent.children.get(chat_id, {})
            run = self.agent.active.get(chat_id)
            native_id = self.agent.registry.get(chat_id)
            if (record.get('owner_principal_id') != self.scope.owner_principal_id
                    or record.get('dataset') != self.scope.dataset or not native_id
                    or run is None or not isinstance(run.get('cancel'), asyncio.Event)
                    or run['cancel'].is_set() or (not rows and native_id != thread_id)):
                break
            rows.append((chat_id, native_id, record.get('parent_id'), run))
            if chat_id == self.scope.root_chat_id:
                if (not record.get('parent_id') and native_id == self.scope.root_thread_id
                        and run is self._root_run):
                    return tuple(rows)
                break
            chat_id = record.get('parent_id')
        raise ValueError('Choose a verified active workspace.')

    def issue(self, *, actor_chat_id=None, actor_thread_id=None, ttl_seconds=900) -> NativeMCPGrant:
        """Host-only grant creation; default is the inherited read-only root grant."""
        if isinstance(ttl_seconds, bool) or not 0 < ttl_seconds <= 1800:
            raise ValueError('Choose a grant lifetime up to 1800 seconds.')
        if (actor_chat_id is None) != (actor_thread_id is None):
            raise ValueError('Choose a verified active workspace.')
        lineage = self._lineage(actor_chat_id or self.scope.root_chat_id, actor_thread_id or self.scope.root_thread_id)
        for binding in list(self._bindings.values()):
            if not self._valid(binding):
                self.revoke(binding.id)
        if len(self._bindings) >= 64:
            raise ValueError('The active workspace has reached its tool grant limit.')
        token = secrets.token_urlsafe(48)
        digest = hashlib.sha256(token.encode()).hexdigest()
        from host import Admission
        from scope_host import current_admission, active_scope_current
        admission = current_admission() or Admission(lambda: active_scope_current(self.agent, self.scope.root_chat_id))
        binding = _Binding(secrets.token_hex(16), digest, self._clock() + ttl_seconds, lineage, admission)
        self._bindings[digest] = binding
        return NativeMCPGrant(binding.id, token, binding.expires)

    async def issue_provider(self, server_name, *, ttl_seconds=900):
        """Host-only: bind an original server to its package or direct policy."""
        if self.provider_relay is None:
            raise ValueError('Choose an installed provider for this workspace.')
        self._lineage(self.scope.root_chat_id, self.scope.root_thread_id)
        provider = await self.provider_relay.grant(server_name)
        grant = self.issue(ttl_seconds=ttl_seconds)
        self._bindings[hashlib.sha256(grant.token.encode()).hexdigest()].provider = provider
        return grant

    async def provider_configs(self):
        """Private native config writes; credentials remain in the host broker."""
        configs = []
        if self.provider_relay is None: return configs
        for name in self.provider_relay.server_names:
            try: grant = await self.issue_provider(name, ttl_seconds=1800)
            except ValueError: continue
            configs.append({'keyPath':'mcp_servers.'+name, 'mergeStrategy':'replace', 'value':{
                'url':'http://127.0.0.1:8766/mcp',
                'http_headers':{'Authorization':'Bearer '+grant.token},
                **(await self.provider_relay.prompt_policy(self._bindings[hashlib.sha256(grant.token.encode()).hexdigest()].provider))}})
        return configs

    async def _provider_read(self, binding, operation):
        if not self._valid(binding) or self.provider_relay is None or len(self._calls) >= 8:
            return None
        async def invoke():
            if not await self.provider_relay.valid(binding.provider): return None
            result = await operation()
            return result if await self.provider_relay.valid(binding.provider) else None
        # Reserve before the first async metadata read, including tools/list.
        work = asyncio.create_task(invoke()); self._calls.add(work)
        watchers = [asyncio.create_task(binding.revoked.wait())]
        watchers.extend(asyncio.create_task(row[3]['cancel'].wait()) for row in binding.lineage)
        try:
            timeout = min(self._call_timeout, max(0, binding.expires-self._clock()))
            done, _ = await asyncio.wait([work, *watchers], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if work not in done or any(watcher in done for watcher in watchers) or not self._valid(binding): return None
            return work.result()
        except Exception: return None
        finally:
            async def drain():
                for task in [work, *watchers]:
                    if not task.done(): task.cancel()
                await asyncio.gather(work, *watchers, return_exceptions=True)
            cleanup = asyncio.create_task(drain()); cancelled = False
            try:
                while not cleanup.done():
                    try: await asyncio.shield(cleanup)
                    except asyncio.CancelledError: cancelled = True
                cleanup.result()
            finally: self._calls.discard(work)
            if cancelled: raise asyncio.CancelledError

    def _participant_reach_context(self,binding):
        return has_participant_reach_context(self.agent,self.scope.root_chat_id) or any(
            has_participant_reach_context(self.agent,row[0]) for row in binding.lineage)

    async def _provider_write(self,binding,name,arguments,metadata):
        if not self._valid(binding) or self._participant_reach_context(binding) or len(self._calls)>=8:
            return _error('Use the active parent provider action.')
        ended=asyncio.Event()
        alive=lambda:not ended.is_set() and self._valid(binding) and not self._participant_reach_context(binding)
        cancel=binding.lineage[0][3]['cancel']
        work=asyncio.create_task(self.provider_relay.call(binding.provider,name,arguments,
            metadata=metadata,cancelled=cancel,alive=alive))
        self._calls.add(work)
        watchers=[asyncio.create_task(binding.revoked.wait())]
        watchers.extend(asyncio.create_task(row[3]['cancel'].wait()) for row in binding.lineage)
        interrupted=False
        try:
            timeout=min(self._call_timeout,max(0,binding.expires-self._clock()))
            done,_=await asyncio.wait([work,*watchers],timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
            if work in done and alive() and not any(w in done for w in watchers):
                result=work.result()
                if await self.provider_relay.valid(binding.provider) and alive():return result
            return _error('Review the saved provider action outcome.')
        except asyncio.CancelledError:
            interrupted=True
            raise
        except Exception:
            return _error('Review the saved provider action outcome before continuing.')
        finally:
            # Stop/caller cancellation closes admission immediately. A dispatched
            # mutation retains its fixed timeout, slot and durable outcome task.
            ended.set()
            for watcher in watchers:watcher.cancel()
            async def drain():await asyncio.gather(work,*watchers,return_exceptions=True)
            cleanup=asyncio.create_task(drain())
            try:
                while not cleanup.done():
                    try:await asyncio.shield(cleanup)
                    except asyncio.CancelledError:interrupted=True
                cleanup.result()
            finally:self._calls.discard(work)
            if interrupted:raise asyncio.CancelledError

    def _valid(self, binding):
        from scope_host import active_scope_current
        if not active_scope_current(self.agent,self.scope.root_chat_id):return False
        if not isinstance(binding, _Binding) or self._bindings.get(binding.digest) is not binding:
            return False
        if binding.revoked.is_set() or self._clock() >= binding.expires or not binding.admission.current():
            return False
        try:
            actual = self._lineage(binding.lineage[0][0], binding.lineage[0][1])
            return len(actual) == len(binding.lineage) and all(
                a[:3] == b[:3] and a[3] is b[3] for a, b in zip(actual, binding.lineage))
        except ValueError:
            return False

    def revoke(self, grant_id: str):
        for digest, binding in list(self._bindings.items()):
            if binding.id == grant_id:
                binding.revoked.set()
                self._bindings.pop(digest, None)

    def revoke_all(self):
        for binding in self._bindings.values():
            binding.revoked.set()
        self._bindings.clear()

    async def _call(self, binding, name, arguments, *, metadata=None):
        if self._valid(binding) and binding.provider is not None:
            kind=await self._provider_read(binding,lambda:self.provider_relay.operation_kind(binding.provider,name))
            if kind=='write':
                if self._participant_reach_context(binding):return _error('Use an agent granted external actions or your own chat for connected-account changes.')
                return await self._provider_write(binding,name,arguments,metadata or {})
            if kind!='read':return _error('Use the current connected provider tool.')
            result = await self._provider_read(binding, lambda: self.provider_relay.call(binding.provider, name, arguments))
            return result if result is not None else _error('Use an available read tool from this connected account.')
        if not self._valid(binding) or name not in self._specs:
            return _error()
        try:
            if (not isinstance(arguments, dict)
                    or len(json.dumps(arguments, allow_nan=False).encode()) > MAX_ARGUMENT_BYTES
                    or not self._validators[name].is_valid(arguments)
                    or arguments.get('dataset', self.scope.dataset) != self.scope.dataset):
                return _error('Use the declared tool fields within this workspace.')
        except (TypeError, ValueError, RecursionError):
            return _error('Use the declared tool fields within this workspace.')
        if len(self._calls) >= 8:
            return _error('The workspace is processing its current tool requests. Try again shortly.')
        from scope_host import bind_admission
        async def invoke():
            with bind_admission(binding.admission):
                return await self.agent._scoped_tool(name, deepcopy(arguments), self.scope.dataset, binding.lineage[0][0])
        work = None
        watchers = []
        try:
            # ContextVars are explicitly restored here because the HTTP server is
            # outside the admitted root turn's original asyncio task tree.
            work = asyncio.create_task(invoke())
            self._calls.add(work)
            watchers = [asyncio.create_task(binding.revoked.wait())]
            watchers.extend(asyncio.create_task(row[3]['cancel'].wait()) for row in binding.lineage)
            timeout = min(self._call_timeout, max(0, binding.expires - self._clock()))
            done, _ = await asyncio.wait([work, *watchers], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if work not in done or any(watcher in done for watcher in watchers) or not self._valid(binding):
                return _error('Continue from the active parent workspace.')
            parts, _results, success = work.result()
            if not success:
                return _error('The workspace tool could not complete this request.')
            return self._result(parts)
        except Exception:
            # Host/provider exception strings can contain credential values/paths.
            return _error('The workspace tool could not complete this request.')
        finally:
            for task in [work, *watchers]:
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in [work, *watchers] if task is not None), return_exceptions=True)
            self._calls.discard(work)

    @staticmethod
    def _result(parts):
        if not isinstance(parts, list) or len(parts) > 64:
            return _error('The workspace tool returned an unsupported response.')
        content, size = [], 0
        for part in parts:
            if not isinstance(part, dict):
                return _error('The workspace tool returned an unsupported response.')
            if part.get('type') == 'inputText' and isinstance(part.get('text'), str):
                size += len(part['text'].encode())
                content.append(types.TextContent(type='text', text=part['text']))
            elif part.get('type') == 'inputImage' and isinstance(part.get('imageUrl'), str):
                header, _, data = part['imageUrl'].partition(',')
                if header not in {'data:image/jpeg;base64', 'data:image/png;base64', 'data:image/webp;base64'}:
                    return _error('The workspace tool returned an unsupported response.')
                size += len(data)
                if size > MAX_RESULT_BYTES:
                    return _error('Request a smaller selection of source evidence.')
                try:
                    base64.b64decode(data, validate=True)
                except ValueError:
                    return _error('The workspace tool returned an unsupported response.')
                content.append(types.ImageContent(type='image', data=data, mimeType=header[5:-7]))
            else:
                return _error('The workspace tool returned an unsupported response.')
            if size > MAX_RESULT_BYTES:
                return _error('Request a smaller selection of source evidence.')
        return types.CallToolResult(content=content, isError=False)

    @asynccontextmanager
    async def lifespan(self):
        async with self.manager.run():
            self._running = True
            try:
                yield self
            finally:
                self._running = False
                self.revoke_all()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await receive()
            async with self.lifespan():
                await send({'type':'lifespan.startup.complete'})
                await receive()
            await send({'type':'lifespan.shutdown.complete'})
            return
        if scope['type'] != 'http':
            return
        if scope.get('path') != '/mcp' or scope.get('query_string'):
            await self._http_error(send, 404)
            return
        headers = scope.get('headers', [])
        if any(name.lower() == b'origin' for name, _ in headers):
            await self._http_error(send, 403)
            return
        authorization = [value for name, value in headers if name.lower() == b'authorization']
        binding = None
        if len(authorization) == 1 and authorization[0].startswith(b'Bearer ') and len(authorization[0]) <= 256:
            digest = hashlib.sha256(authorization[0][7:]).hexdigest()
            binding = self._bindings.get(digest)
        if not self._valid(binding):
            await self._http_error(send, 401)
            return
        if not self._running:
            await self._http_error(send, 503)
            return
        token = _REQUEST_GRANT.set(binding)
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            _REQUEST_GRANT.reset(token)

    @staticmethod
    async def _http_error(send, status):
        headers = [(b'content-type', b'application/json'), (b'cache-control', b'no-store')]
        if status == 401:
            headers.append((b'www-authenticate', b'Bearer'))
        await send({'type':'http.response.start', 'status':status, 'headers':headers})
        await send({'type':'http.response.body', 'body':b'{"error":"workspace_access_required"}'})
