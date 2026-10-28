"""Own-domain, read-only MCP transport over existing company retrieval services.

The host resolver alone maps an immutable grant to authenticated company services.
This module creates no Codex runtime, text encoder, model session or media worker.
"""
from __future__ import annotations
import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import inspect
import json
import math
import threading
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend,RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings,TransportSecurityMiddleware
from pydantic import AnyHttpUrl
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse,Response

from mcp_access import ReadGrant
from workspace_identity import workspace_scope

MAX_BODY=128*1024
MAX_RESULT=32*1024*1024


def __getattr__(name):
    if name == 'READ_TOOLS':
        from agent_bridge import workspace_registry
        return workspace_registry().read_names('public')
    raise AttributeError(name)


@dataclass(frozen=True)
class HTTPMCPServices:
    state: Path
    agent: object

    @property
    def registry(self):
        from agent_bridge import workspace_registry
        return workspace_registry(self.agent)

    def admit(self, grant):
        from tool_families import ToolContext
        if Path(self.agent.state).absolute() != Path(self.state).absolute():
            raise ValueError('Use services belonging to this workspace.')
        admission = self.agent.vertical.read_admission(grant.owner_principal_id, grant.dataset)
        return ToolContext(state=Path(self.state), company_id=grant.company_id,
            actor=grant.owner_principal_id, data_owner=grant.owner_principal_id,
            collection=grant.dataset, agents=getattr(self.agent, 'automation_store', None),
            agent=self.agent, admission=admission, extension=self.agent.vertical)


def _public(value,depth=0):
    if depth>24:raise ValueError('Choose a smaller evidence result.')
    if isinstance(value,dict):
        return {key:_public(item,depth+1) for key,item in value.items() if isinstance(key,str)
            and key not in {'path','root','owner','owner_principal_id','prepared_manifest_sha256','receipt_id',
                            'credential','credentials','authorization','access_token','refresh_token','secret'}
            and not key.endswith(('_path','_root','_directory'))}
    if isinstance(value,(list,tuple)):return [_public(item,depth+1) for item in value]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    raise ValueError('Choose a supported evidence result.')


class ReadTools:
    """Run the selected read family under one captured, current admission."""
    def __init__(self, services):
        self.services = services

    def call(self, grant, name, args, *, scope_guards=None):
        from scope_host import bind_admission
        context = self.services.admit(grant)
        if scope_guards is not None:
            scope_guards.append(context.admission.current)
        async def invoke():
            with bind_admission(context.admission):
                return await self.services.registry.invoke(context, name, args, surface='public')
        result = asyncio.run(invoke())
        if isinstance(result, tuple) and len(result) == 3:
            content, _, success = result
            if not success:
                raise ValueError('The workspace read could not complete.')
            from native_mcp import NativeMCP
            return NativeMCP._result(content)
        encoded = json.dumps(_public(result), allow_nan=False)
        if len(encoded.encode()) > MAX_RESULT:
            raise ValueError('Choose a smaller workspace result.')
        return types.CallToolResult(content=[types.TextContent(type='text', text=encoded)], isError=False)


class HTTPMCP:
    """ASGI callable plus lifespan(); root supplies current-access resolver.

    resolve_scope(ReadGrant) -> HTTPMCPServices | None may be async. It must check
    present company membership and collection access on every invocation. Calling
    grant-store methods alone never authorizes access to a company's services.
    """
    def __init__(self,store,resolve_scope,*,resource_url,call_timeout=60,recheck_seconds=.25,
                 max_sessions=128,max_calls=16,max_calls_per_key=4,idle_timeout=300,
                 resource_path='/mcp',permission='video:read'):
        url=urlsplit(resource_url)
        if resource_path not in {'/mcp','/runtime/mcp'} or permission not in {'video:read','workspace:read'}:
            raise ValueError('Choose the declared MCP transport boundary.')
        if url.scheme!='https' or not url.netloc or url.username or url.password or url.query or url.fragment or url.path!=resource_path:raise ValueError('Configure the exact HTTPS MCP resource URL.')
        self.resource_path=resource_path;self.permission=permission
        if (type(call_timeout) not in {int,float} or not math.isfinite(call_timeout) or not 0<call_timeout<=60
                or type(recheck_seconds) not in {int,float} or not math.isfinite(recheck_seconds) or not .05<=recheck_seconds<=1):raise ValueError('Choose bounded MCP call deadlines.')
        if (type(max_sessions) is not int or not 1<=max_sessions<=128 or type(idle_timeout) not in {int,float}
                or not math.isfinite(idle_timeout) or not .05<=idle_timeout<=300):raise ValueError('Choose bounded MCP session lifetimes.')
        if type(max_calls) is not int or not 1<=max_calls<=32 or type(max_calls_per_key) is not int or not 1<=max_calls_per_key<=4:raise ValueError('Choose bounded MCP read concurrency.')
        self.store=store;self.resolve_scope=resolve_scope;self.resource_url=resource_url;self.timeout=call_timeout;self.recheck_seconds=recheck_seconds
        self.max_calls=max_calls;self.max_calls_per_key=max_calls_per_key;self.running=False;self.calls={};self.adapters={};self.adapter_lock=threading.Lock()
        self.max_auth_checks=32;self.auth_timeout=5;self.auth_checks=set()
        self.server=Server('Rocoto',version='1.0.0')
        from agent_bridge import workspace_registry
        self.specs={s['name']:s for s in workspace_registry().specs(surface='public')}
        self.validators={n:Draft202012Validator(s['inputSchema']) for n,s in self.specs.items()}
        @self.server.list_tools()
        async def listing():
            if await self._request_scope() is None:return []
            return [types.Tool(name=n,description=s['description'],inputSchema=s['inputSchema'],annotations=types.ToolAnnotations(readOnlyHint=True,destructiveHint=False)) for n,s in self.specs.items()]
        @self.server.call_tool(validate_input=False)
        async def call(name,arguments):return await self._call(name,arguments)
        settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=[url.netloc],allowed_origins=[url.scheme+'://'+url.netloc])
        self.security=TransportSecurityMiddleware(settings)
        self.manager=StreamableHTTPSessionManager(self.server,stateless=False,json_response=True,max_request_body_size=MAX_BODY,
            max_sessions=max_sessions,session_idle_timeout=idle_timeout,security_settings=settings)
        self.auth=AuthenticationMiddleware(RequireAuthMiddleware(self._endpoint,[self.permission]),backend=BearerAuthBackend(self,resource_server_url=AnyHttpUrl(resource_url)))

    async def _resolve(self,grant):
        if grant is None:return None
        try:
            if inspect.iscoroutinefunction(self.resolve_scope):value=await self.resolve_scope(grant)
            else:
                value=await asyncio.to_thread(self.resolve_scope,grant)
                if inspect.isawaitable(value):value=await value
            if (not isinstance(value,HTTPMCPServices)
                    or Path(value.state).absolute()!=Path(value.agent.state).absolute()
                    or await asyncio.to_thread(workspace_scope,value.state)!=grant.company_id):return None
            return value
        except Exception:return None

    async def _authorization(self,lookup,value):
        # Admission precedes every DB lookup and host callback. Auth happens
        # before SDK session admission, so it needs its own bounded work set.
        if not self.running or len(self.auth_checks)>=self.max_auth_checks:return None
        async def resolve():
            try:
                grant=await asyncio.to_thread(lookup,value)
                services=await self._resolve(grant)
                return (grant,services) if services and self.running else None
            except Exception:return None
        work=asyncio.create_task(resolve());self.auth_checks.add(work)
        def finished(task):
            self.auth_checks.discard(task)
            if not task.cancelled():task.exception()
        work.add_done_callback(finished)
        # wait() never cancels its child. A stopped/expired HTTP request leaves
        # the slot held until the actual callback and its read threads finish.
        done,_=await asyncio.wait([work],timeout=self.auth_timeout)
        return work.result() if work in done else None

    async def verify_token(self,token):
        try:
            selected=await self._authorization(self.store.authenticate,token)
            if selected is None:return None
            grant,_=selected
            if grant.permission!=self.permission:return None
            return AccessToken(token=token,client_id=getattr(grant,'client_id','rocoto-key:'+grant.id),subject=grant.owner_principal_id,
                scopes=[grant.permission],expires_at=int(grant.expires_at),resource=self.resource_url,
                claims={'iss':self.resource_url.rsplit('/',1)[0],'grant_id':grant.id})
        except Exception:return None

    async def _current(self,key):
        return await self._authorization(self.store.active,key)

    async def _request_scope(self):
        try:key=self.server.request_context.request.user.access_token.claims['grant_id']
        except (AttributeError,KeyError):return None
        return await self._current(key)

    @staticmethod
    def error():return types.CallToolResult(isError=True,content=[types.TextContent(type='text',text='Use an available read tool within this connection’s collection.')])

    async def _call(self,name,args):
        selected=await self._request_scope()
        if selected is None or name not in self.specs:return self.error()
        grant,services=selected
        try:self.server.request_context.request.scope["rocoto.read_binding"]=selected
        except (LookupError,AttributeError):pass
        try:
            if (not isinstance(args,dict) or len(json.dumps(args,allow_nan=False).encode())>MAX_BODY
                    or not self.validators[name].is_valid(args) or args.get('dataset',grant.dataset)!=grant.dataset):return self.error()
        except (ValueError,TypeError,RecursionError):return self.error()
        if len(self.calls)>=self.max_calls or sum(value==grant.id for value in self.calls.values())>=self.max_calls_per_key:return self.error()
        # Reserve before awaiting adapter creation or a worker thread.
        admission=object();self.calls[admission]=grant.id;work=None;watch=None
        try:
            async def changed():
                while True:
                    await asyncio.sleep(self.recheck_seconds)
                    if await self._current(grant.id)!=(grant,services):return
            work=asyncio.create_task(asyncio.to_thread(self._read,grant,services,name,deepcopy(args)))
            watch=asyncio.create_task(changed())
            done,_=await asyncio.wait([work,watch],timeout=self.timeout,return_when=asyncio.FIRST_COMPLETED)
            if work not in done or watch in done or await self._current(grant.id)!=(grant,services):return self.error()
            return work.result()
        except Exception:return self.error()
        finally:
            async def drain():
                if watch:watch.cancel()
                if work is not None:
                    # CPU/read threads cannot be killed safely. Retain admission
                    # until actual completion; cancellation suppresses results.
                    await asyncio.gather(work,return_exceptions=True)
                if watch:await asyncio.gather(watch,return_exceptions=True)
            cleanup=asyncio.create_task(drain());cancelled=False
            try:
                while not cleanup.done():
                    try:await asyncio.shield(cleanup)
                    except asyncio.CancelledError:cancelled=True
                cleanup.result()
            finally:self.calls.pop(admission,None)
            if cancelled:raise asyncio.CancelledError

    def _read(self,grant,services,name,args):
        with self.adapter_lock:
            adapter=self.adapters.get(grant.company_id)
            if adapter is None or adapter.services!=services:
                if len(self.adapters)>=64 and grant.company_id not in self.adapters:
                    raise ValueError('The service is processing its current companies.')
                adapter=ReadTools(services);self.adapters[grant.company_id]=adapter
        try:guards=self.server.request_context.request.scope.get("rocoto.scope_guards")
        except (LookupError,AttributeError):guards=None
        return adapter.call(grant,name,args,scope_guards=guards)

    async def _endpoint(self,scope,receive,send):
        request=Request(scope,receive)
        denied=await self.security.validate_request(request,is_post=scope['method']=='POST')
        if denied is not None:await denied(scope,receive,send);return
        if scope['path']!=self.resource_path or scope.get('query_string'):
            await Response(status_code=404)(scope,receive,send);return
        if scope['method']=='GET':await Response(status_code=405)(scope,receive,send);return
        if scope['method']=='POST':
            # Validate before the SDK's permissive JSON-RPC union can classify a
            # malformed request as a notification and log submitted values.
            body=bytearray()
            try:
                async with asyncio.timeout(5):
                    while True:
                        message=await receive()
                        if message['type']=='http.disconnect':return
                        body.extend(message.get('body',b''))
                        if len(body)>MAX_BODY:
                            await JSONResponse({'error':'request_too_large'},413)(scope,receive,send);return
                        if not message.get('more_body'):break
                document=json.loads(body,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                if not isinstance(document,dict) or document.get('jsonrpc')!='2.0':raise ValueError()
                if 'id' in document:
                    identity=document['id']
                    if type(identity) not in {str,int} or isinstance(identity,str) and len(identity)>128:raise ValueError()
                    types.ClientRequest.model_validate(document)
                else:types.ClientNotification.model_validate(document)
            except (ValueError,TypeError,RecursionError,TimeoutError):
                await JSONResponse({'error':'invalid_mcp_request'},400)(scope,receive,send);return
            original_receive=receive;replayed=False
            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed=True;return {'type':'http.request','body':bytes(body),'more_body':False}
                return await original_receive()
            receive=replay
        # The worker appends its exact admitted scope fence into this same ASGI
        # request object. Keep it through SDK result serialization and send, even
        # when the company/service object is unchanged after partial revocation.
        guards=scope.setdefault("rocoto.scope_guards",[])
        def scope_current():
            try:return all(check() is True for check in guards)
            except Exception:return False
        key=request.user.access_token.claims["grant_id"]
        async def binding_current():
            current=await self._current(key)
            expected=scope.get("rocoto.read_binding")
            return current is not None and (expected is None or current==expected)
        blocked=False;error_status=None;started=False
        async def guarded(message):
            nonlocal blocked,error_status,started
            if blocked:return
            if message['type']=='http.response.start':
                key=request.user.access_token.claims['grant_id']
                if not await binding_current() or not scope_current():
                    blocked=True;await JSONResponse({'error':'invalid_token'},401,headers={'WWW-Authenticate':'Bearer','Cache-Control':'no-store'})(scope,receive,send);return
                if message['status']>=400:error_status=message['status']
                else:message=dict(message,headers=[*message.get('headers',[]),(b'cache-control',b'no-store')])
            if error_status:
                if message['type']=='http.response.body':
                    blocked=True;await JSONResponse({'error':'mcp_request_failed'},error_status,headers={'Cache-Control':'no-store'})(scope,receive,send)
                return
            if message["type"]=="http.response.body" and (not await binding_current() or not scope_current()):
                blocked=True
                if started:await send({"type":"http.response.body","body":b"","more_body":False})
                return
            if message["type"]=="http.response.start":started=True
            await send(message)
        await self.manager.handle_request(scope,receive,guarded)

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return
        auth=[value for key,value in scope.get('headers',[]) if key.lower()==b'authorization']
        if len(auth)!=1 or len(auth[0])>256:
            await JSONResponse({'error':'invalid_token'},401,headers={'WWW-Authenticate':'Bearer'})(scope,receive,send);return
        await self.auth(scope,receive,send)

    @asynccontextmanager
    async def lifespan(self):
        async with self.manager.run():
            self.running=True
            try:yield self
            finally:
                self.running=False
                # Keep authorization callbacks alive through their actual drain;
                # cancelling a to_thread task cannot stop its underlying read.
                drain=asyncio.gather(*tuple(self.auth_checks),return_exceptions=True)
                while not drain.done():
                    try:await asyncio.shield(drain)
                    except asyncio.CancelledError:continue
                drain.result()
