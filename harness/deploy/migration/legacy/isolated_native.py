"""Opt-in native worker lifecycle; the bridge still owns JSON-RPC and thread routing.

The host derives NativeScope from persisted authorization. This adapter admits one
broker worker, serves its private sockets, and revokes capabilities before stop.
It neither starts model turns nor implements an agent loop. A separate native
thread/read check must bind an MCP root before bind_mcp is called.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
import os
import socket
import stat
import re

import uvicorn

from native_executor import NativeClient, NativeScope
from deploy.migration.legacy.native_policy import isolated_thread_params
from openai_gateway import KEY_ENV, _LoopbackServer


class IsolatedLaunchError(RuntimeError):
    pass


class _MCPListener:
    """An empty private socket exists before worker start; host binds it once."""
    def __init__(self, path):
        self.path=Path(path)
        self.app=None
        self.server=None
        self.task=None
        self.socket=None
        self.identity=None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            await send({'type':'http.response.start','status':503,
                        'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'{"error":"Native scope binding pending"}'})
        else:
            await self.app(scope,receive,send)

    async def __aenter__(self):
        info=self.path.parent.lstat()
        if (not self.path.is_absolute() or self.path.name!='mcp.sock' or not stat.S_ISDIR(info.st_mode)
                or info.st_uid!=os.geteuid() or info.st_mode&0o077
                or self.path.exists() or self.path.is_symlink()):
            raise IsolatedLaunchError('invalid_mcp_socket')
        self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            self.socket.bind(str(self.path));self.path.chmod(0o600)
            info=self.path.lstat();self.identity=(info.st_dev,info.st_ino)
            self.socket.listen(16);self.socket.setblocking(False)
            self.server=_LoopbackServer(uvicorn.Config(self,log_level='critical',access_log=False,
                lifespan='off',timeout_graceful_shutdown=2))
            self.task=asyncio.create_task(self.server.serve(sockets=[self.socket]))
            for _ in range(200):
                if self.server.started:return self
                if self.task.done():await self.task
                await asyncio.sleep(.01)
            raise IsolatedLaunchError('mcp_listener_start_timeout')
        except BaseException:
            await self.__aexit__()
            raise

    async def __aexit__(self,*args):
        self.app=None
        if self.server:self.server.should_exit=True
        if self.task:
            try:await asyncio.wait_for(self.task,5)
            except (TimeoutError,asyncio.CancelledError):
                self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self.socket:self.socket.close()
        if self.identity:
            with suppress(OSError):
                info=self.path.lstat()
                if (info.st_dev,info.st_ino)==self.identity:self.path.unlink()


class IsolatedSession:
    def __init__(self,scope,lease,launch,client,listener,stack,*,storage_scope=None):
        self.scope,self.lease,self.launch=scope,lease,launch
        self.storage_scope=storage_scope or scope
        self.client,self.listener,self.stack=client,listener,stack
        self.process=None
        self.mcp=None
        self.stopped=False
        self._closing=False
        self._owner_task=asyncio.current_task()
        self._stop_lock=asyncio.Lock()
        self._stderr_task=None
        self.plugin_ids=frozenset()
        self.plugin_config={}
        self.runtime_packages=None

    def thread_params(self,instructions,tools):
        if self._closing:raise IsolatedLaunchError('native_session_closed')
        params=isolated_thread_params(instructions,tools,plugins=self.plugin_config)
        params['config'].update(self.launch.cli_config)
        params.update(model=self.launch.cli_config['model'],modelProvider='rocoto_metered')
        return params

    async def bind_mcp(self,app):
        """Called once, in the lease task, after host verifies the native root ID.

        Returns only the configuration to send privately through native
        config/value/write then config/mcpServer/reload. The public UI receives
        neither the grant nor this configuration. Child-safe server tools only.
        """
        if asyncio.current_task() is not self._owner_task:
            raise IsolatedLaunchError('mcp_binding_task_mismatch')
        if self._closing or self.mcp is not None:
            raise IsolatedLaunchError('mcp_binding_closed')
        bound=getattr(app,'scope',None)
        if (bound is None or bound.company_id!=self.scope.company_id
                or bound.owner_principal_id!=self.scope.owner_id or bound.root_chat_id!=self.scope.root_chat_id
                or not isinstance(bound.root_thread_id,str) or not bound.root_thread_id):
            raise IsolatedLaunchError('mcp_scope_mismatch')
        # NativeMCP's constructor/issue independently checks the host's admitted
        # active run and root/child registry; a model cannot manufacture this app.
        grant=app.issue(ttl_seconds=1800)
        try:
            await self.stack.enter_async_context(app.lifespan())
        except BaseException:
            app.revoke_all()
            raise
        if self._closing:
            app.revoke_all()
            raise IsolatedLaunchError('mcp_binding_closed')
        self.mcp=app
        try:
            providers=await app.provider_configs()
            # Stop can complete while a package/account metadata read is in
            # flight. Revoke any later-issued grants before publishing config.
            if self._closing:raise IsolatedLaunchError('mcp_binding_closed')
            servers={'rocoto':{'url':'http://127.0.0.1:8766/mcp',
                'http_headers':{'Authorization':'Bearer '+grant.token}}}
            servers.update({item['keyPath'].removeprefix('mcp_servers.'):item['value'] for item in providers})
            self.listener.app=app
            # Resumed native state may retain a disconnected provider. Replace
            # the entire map, including the ordinary Rocoto-only/empty case.
            return {'keyPath':'mcp_servers','mergeStrategy':'replace','value':servers}
        except BaseException:
            app.revoke_all()
            raise

    async def drain_stderr(self):
        # Native stderr can contain untrusted paths or connector output. Consume
        # it for backpressure without projecting it into chat or provider logs.
        while await self.process.stderr.read(65536):pass

    async def stop(self):
        """Safe from a separate Stop task; context managers exit on their owner."""
        async with self._stop_lock:
            if self.stopped:return {'status':'stopped','generation':self.lease.generation}
            self._closing=True
            self.launch.gateway.revoke()
            if self.mcp:self.mcp.revoke_all()
            # Stop confirmation belongs to the broker, not an attached CLI or EOF.
            if self.process is not None:
                result=await self.process.stop()
            else:
                result=await self.client.stop(self.storage_scope,self.lease)
            self.stopped=True
            self.runtime_packages=None
            if self._stderr_task:
                self._stderr_task.cancel()
                await asyncio.gather(self._stderr_task,return_exceptions=True)
            return result


class IsolatedNativeLauncher:
    """The default is disabled; enabling it never changes service configuration."""
    def __init__(self,auth,*,company_id,enabled=False,client=None,gateway_factory=None,package_store=None):
        self.auth,self.company_id,self.enabled=auth,company_id,enabled is True
        self.client=client or NativeClient()
        self.gateway_factory=gateway_factory
        self.package_store=package_store
        self._sessions=set()

    async def revoke_plugin(self,principal_id,plugin_id):
        """HTTP removes selection first; failed Stops remain tracked for retry.

        One launcher belongs to one company process. A multi-web-process service
        must route this to each launcher before acknowledging removal.
        """
        sessions=[session for session in self._sessions if session.scope.owner_id==principal_id
                  and plugin_id in session.plugin_ids]
        for session in sessions:
            await session.stop()
            self._sessions.discard(session)
        return {'cleanup_confirmed':True}

    @asynccontextmanager
    async def _selection(self,scope):
        if self.package_store is None:
            yield None
            return
        # The same cross-process lock serializes selection changes. Hold it only
        # through bounded transfer/start; never during an inference turn.
        async with self.package_store.runtime_selection(principal_id=scope.owner_id) as selected:
            if selected.company_scope!=scope.company_id:
                raise IsolatedLaunchError('plugin_company_mismatch')
            yield selected

    @asynccontextmanager
    async def lease(self,scope:NativeScope,*,run_id,workspace_binding=None):
        if not self.enabled:raise IsolatedLaunchError('isolated_native_disabled')
        if not isinstance(scope,NativeScope) or scope.company_id!=self.company_id:
            raise IsolatedLaunchError('native_scope_mismatch')
        if self.auth.mode!='openai_api':raise IsolatedLaunchError('isolated_native_requires_api')
        # Funding/configuration is checked before consuming scarce worker capacity.
        if not self.auth.status()['inference_enabled']:raise IsolatedLaunchError('native_funding_unavailable')
        storage_scope=scope
        if workspace_binding is not None:
            if not isinstance(workspace_binding,str) or not re.fullmatch(r'[a-f0-9]{64}',workspace_binding):
                raise IsolatedLaunchError('invalid_workspace_binding')
            storage_scope=NativeScope(scope.company_id,scope.owner_id,scope.root_chat_id+':scope:'+workspace_binding)
        lease=await self.client.prepare(storage_scope)
        session=None
        async with AsyncExitStack() as stack:
            try:
                launch=await stack.enter_async_context(self.auth.lease(run_id=run_id,
                    gateway_factory=self.gateway_factory,unix_socket=Path(lease.socket_dir)/'model.sock',
                    activity_owner=scope.owner_id))
                listener=await stack.enter_async_context(_MCPListener(Path(lease.socket_dir)/'mcp.sock'))
                session=IsolatedSession(scope,lease,launch,self.client,listener,stack,storage_scope=storage_scope)
                async with self._selection(scope) as selection:
                    if selection and selection.packages:
                        await self.package_store.validate_runtime_snapshot(selection,principal_id=scope.owner_id)
                        session.plugin_ids=frozenset(p.id for p in selection.packages)
                        self._sessions.add(session)
                        result=await self.client.install_packages(storage_scope,lease,selection)
                        session.plugin_config=result['plugins']
                        await self.package_store.validate_runtime_snapshot(selection,principal_id=scope.owner_id)
                        if session._closing:raise IsolatedLaunchError('native_session_closed')
                        session.runtime_packages=selection
                    if session._closing:raise IsolatedLaunchError('native_session_closed')
                    session.process=await self.client.start(storage_scope,lease,launch.child_env[KEY_ENV])
                    if session._closing:raise IsolatedLaunchError('native_session_closed')
                    session._stderr_task=asyncio.create_task(session.drain_stderr())
                yield session
            finally:
                # Finish whole-worker Stop even when the chat consumer disconnects.
                # Any failed removal remains fenced by the broker and is surfaced.
                cleanup=asyncio.create_task(session.stop() if session is not None else self.client.stop(storage_scope,lease))
                cancelled=False
                try:
                    while not cleanup.done():
                        try:await asyncio.shield(cleanup)
                        except asyncio.CancelledError:cancelled=True
                    cleanup.result()
                    if cancelled:raise asyncio.CancelledError
                finally:
                    if session is not None and session.stopped:self._sessions.discard(session)
