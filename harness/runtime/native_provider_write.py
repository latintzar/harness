"""One approved native MCP mutation, fixed credentials and durable outcome.

Only host callbacks supply endpoints/credentials/current authority. Model input
supplies original arguments and correlation metadata, which grants no authority
without an existing native approval. The default constructor is uncommissioned.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

import httpx
from jsonschema import validators
from referencing import Registry
from referencing.exceptions import NoSuchResource
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from mcp_connections import _GuardedTransport, MAX_RESULT_BYTES
from native_approvals import ApprovalBinding, ApprovalScope, NativeApprovals, ApprovalError, canonical, digest


class WriteTransportError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedWrite:
    binding: ApprovalBinding
    tool: types.Tool

    def __post_init__(self):
        if (not isinstance(self.binding, ApprovalBinding) or not isinstance(self.tool, types.Tool)
                or self.tool.name != self.binding.tool
                or digest(self.tool.model_dump(mode='json',by_alias=True,exclude_none=True)) != self.binding.schema_sha256):
            raise WriteTransportError('Use the current original provider tool.')


async def _drain(task):
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    if task.done() and not task.cancelled():
        task.exception()
    return interrupted


async def _offload(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain(task)
        raise


class _OneEffect(httpx.AsyncBaseTransport):
    def __init__(self, inner, prepared, arguments, claim, alive):
        self.inner, self.prepared, self.arguments = inner, prepared, arguments
        self.claim, self.alive = claim, alive
        self.claimed = None
        self.replayed = None
        self.effect_attempts = 0

    async def handle_async_request(self, request):
        if str(request.url) != self.prepared.binding.endpoint:
            raise WriteTransportError('Use the reviewed provider endpoint.')
        if request.method == 'POST':
            body = await request.aread()
            if len(body) > 65536:
                raise WriteTransportError('Use bounded native tool arguments.')
            message = json.loads(body)
            method = message.get('method')
            if method == 'tools/call':
                params = message.get('params') or {}
                if (self.effect_attempts or params.get('name') != self.prepared.binding.tool
                        or digest(params.get('arguments') or {}) != digest(self.arguments)):
                    raise WriteTransportError('Use the exact approved provider action once.')
                if not self.alive():
                    raise WriteTransportError('Continue from the active parent workspace.')
                # Caller holds the shared authority fence. This synchronous claim
                # is the dispatch boundary before any effectful network request.
                claim = self.claim()
                if not claim['dispatch']:
                    self.replayed = claim['request']
                    raise WriteTransportError('This action already has an outcome.')
                self.claimed = claim
                self.effect_attempts += 1
            elif method not in {'initialize', 'notifications/initialized', 'notifications/cancelled'}:
                raise WriteTransportError('Use the fixed native provider operation.')
        elif request.method not in {'GET', 'DELETE'}:
            raise WriteTransportError('Use the fixed native provider transport.')
        response = await self.inner.handle_async_request(request)
        # SDK1.30 follows same-origin redirects itself, even with client
        # follow_redirects=False. Reject them before its redirect machinery.
        if response.headers.get('content-encoding','identity').lower() not in {'','identity'}:
            await response.aclose()
            raise WriteTransportError('Use a bounded uncompressed MCP response.')
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise WriteTransportError('The provider endpoint needs review.')
        return response

    async def aclose(self):
        await self.inner.aclose()


class NativeWriteTransport:
    """Host-only opt-in transport; prepare may refresh auth but cannot mutate.

    prepare(server,tool) returns PreparedWrite after read-only inventory refresh.
    admission(binding) is the shared connection policy/disconnect fence.
    credentials(binding) synchronously returns a repr-hidden fixed access_token.
    alive is rechecked at the effectful HTTP boundary. Cancellation after claim
    retains the finite inner task until its outcome and cleanup are durable.
    """
    def __init__(self, store, scope, *, prepare, admission, credentials,
                 enabled=False, timeout=35, max_calls=4, transport_factory=None, recheck=None):
        if (not isinstance(store, NativeApprovals) or not isinstance(scope, ApprovalScope)
                or type(enabled) is not bool or not 0 < timeout <= 60
                or type(max_calls) is not int or not 1 <= max_calls <= 8):
            raise WriteTransportError('Use the admitted host write transport.')
        self.store, self.scope = store, scope
        self.prepare, self.admission, self.credentials = prepare, admission, credentials
        self.recheck = recheck
        self.enabled, self.timeout, self.max_calls = enabled, timeout, max_calls
        self.transport_factory = transport_factory or self._transport
        self._work = set()
        self.directory = store.database.parent / 'results'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise WriteTransportError('Use private provider result storage.')

    @staticmethod
    def _transport(binding):
        return _GuardedTransport(frozenset({urlsplit(binding.endpoint).hostname}))

    def _path(self, request_id):
        if not re.fullmatch(r'approval-[a-f0-9]{32}', request_id):
            raise WriteTransportError('Use the saved provider action.')
        return self.directory / (request_id + '.json')

    def _save(self, row, binding, result):
        data = result.model_dump(mode='json', by_alias=True, exclude_none=True)
        encoded = canonical(data).encode()
        if len(encoded) > MAX_RESULT_BYTES:
            raise WriteTransportError('Request a bounded provider result.')
        receipt_hash = hashlib.sha256(encoded).hexdigest()
        envelope = canonical({'id': row['id'], 'scope': digest(vars(self.scope)),
            'binding': digest(vars(binding)), 'arguments': row['arguments_sha256'],
            'result_sha256': receipt_hash, 'result': data}).encode()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix='.result-', delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(envelope);stream.flush();os.fsync(stream.fileno())
            temporary.replace(self._path(row['id']))
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)
        return receipt_hash

    def _saved(self, row, binding):
        fd = os.open(self._path(row['id']), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_RESULT_BYTES + 4096):
                raise WriteTransportError('Use a verified saved provider result.')
            with os.fdopen(fd, 'rb', closefd=False) as stream: value = json.load(stream)
        finally: os.close(fd)
        if (value.get('id') != row['id'] or value.get('scope') != digest(vars(self.scope))
                or value.get('binding') != digest(vars(binding))
                or value.get('arguments') != row['arguments_sha256']
                or value.get('result_sha256') != digest(value.get('result'))):
            raise WriteTransportError('Use a verified saved provider result.')
        return types.CallToolResult.model_validate(value['result']), value['result_sha256']

    async def execute(self, server, tool, arguments, metadata, *, cancelled, alive=lambda: True):
        if not self.enabled or len(self._work) >= self.max_calls:
            raise WriteTransportError('This provider action is awaiting host commissioning or capacity.')
        if not isinstance(cancelled, asyncio.Event):
            raise WriteTransportError('Use the admitted parent cancellation scope.')
        caller_cancelled = False
        def active(): return not caller_cancelled and not cancelled.is_set() and alive()
        work = asyncio.create_task(self._execute(server, tool, arguments, metadata, cancelled, active))
        self._work.add(work)
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            caller_cancelled = True
            await _drain(work)
            raise
        finally:
            if not work.done():
                caller_cancelled = True
                await _drain(work)
            self._work.discard(work)

    async def _execute(self, server, tool, arguments, metadata, cancelled, alive):
        transport = None
        binding = None
        try:
            async with asyncio.timeout(self.timeout):
                prepared = await self.prepare(server, tool)
                if (not isinstance(prepared, PreparedWrite) or not prepared.binding.permitted
                        or prepared.binding.server_name != server or prepared.binding.tool != tool
                        or not alive()):
                    raise WriteTransportError('Use the current approved provider connection.')
                binding = prepared.binding
                def deny_reference(uri): raise NoSuchResource(ref=uri)
                validator=validators.validator_for(prepared.tool.inputSchema)
                validator.check_schema(prepared.tool.inputSchema)
                if not isinstance(arguments, dict) or not validator(prepared.tool.inputSchema,registry=Registry(retrieve=deny_reference)).is_valid(arguments):
                    raise WriteTransportError('Use the original provider tool input schema.')
                async with self.admission(binding):
                    if not alive(): raise WriteTransportError('Continue from the active parent workspace.')
                    credential = self.credentials(binding)
                    token = credential.access_token
                    if not isinstance(token, str) or not 1 <= len(token) <= 16384 or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', token):
                        raise WriteTransportError('Complete provider authorization first.')
                    def claim():
                        if self.store.provider_has_unresolved(scope=self.scope,provider=binding.provider,metadata=metadata):
                            raise WriteTransportError('Resolve the saved provider action before another write.')
                        if self.recheck is not None: self.recheck(binding)
                        return self.store.claim_dispatch(scope=self.scope, binding=binding,
                            metadata=metadata, tool_arguments=arguments, cancelled=cancelled)
                    transport = _OneEffect(self.transport_factory(binding), prepared, arguments, claim, alive)
                    async with httpx.AsyncClient(transport=transport, auth=None, headers={'Authorization': 'Bearer '+token,'Accept-Encoding':'identity'},
                            follow_redirects=False, max_redirects=0, trust_env=False, timeout=httpx.Timeout(min(30,self.timeout))) as client:
                        async with streamable_http_client(binding.endpoint, http_client=client) as (read, write, _):
                            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=min(30,self.timeout))) as session:
                                await session.initialize()
                                result = await session.send_request(types.ClientRequest(types.CallToolRequest(
                                    params=types.CallToolRequestParams(name=tool, arguments=arguments))), types.CallToolResult)
                    if transport.claimed is None: raise WriteTransportError('The provider action has no dispatch receipt.')
                    # A provider must never echo its credential into native output.
                    raw = canonical(result.model_dump(mode='json',by_alias=True,exclude_none=True))
                    if token in raw: result = types.CallToolResult.model_validate(json.loads(raw.replace(token,'[credential]')))
                    row = transport.claimed['request']
                    checksum = await _offload(self._save, row, binding, result)
                    self.store.outcome(row['id'], scope=self.scope,
                        status='uncertain' if result.isError else 'succeeded', receipt={'response_sha256': checksum})
                    return result
        except BaseException as error:
            if transport is not None and transport.replayed is not None:
                row = transport.replayed
                receipt_hash = (row.get('receipt') or {}).get('response_sha256')
                if receipt_hash and row['status'] in {'succeeded','uncertain'}:
                    try:
                        saved, checksum = await _offload(self._saved, row, binding)
                        if checksum == receipt_hash: return saved
                    except (OSError,ValueError): pass
            if transport is not None and transport.claimed is not None:
                row = self.store.get(transport.claimed['request']['id'], scope=self.scope)
                if row['status'] in {'dispatching','uncertain'}:
                    self.store.outcome(row['id'], scope=self.scope, status='uncertain')
            if isinstance(error, asyncio.CancelledError): raise
            raise WriteTransportError('Review the saved provider action outcome before continuing.') from None

    async def recover_saved_result(self, request_id, binding):
        """Host reconciliation under the original runtime scope; no network I/O."""
        row = self.store.get(request_id, scope=self.scope)
        if row['status'] not in {'dispatching','uncertain'}:
            raise WriteTransportError('Reconcile a dispatched provider action.')
        result, checksum = await _offload(self._saved, row, binding)
        return self.store.outcome(request_id, scope=self.scope,
            status='uncertain' if result.isError else 'succeeded', receipt={'response_sha256':checksum})

    async def close(self):
        self.enabled = False
        for work in tuple(self._work):
            await _drain(work)
