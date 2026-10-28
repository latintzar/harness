"""Scoped read-only HTTPS for isolated custom adapters; secrets stay on the host.

The host issues a short-lived job capability from its immutable execution
snapshot. Sandbox callers choose only a granted operation and bounded query.
Network adapters/resolvers below are host-only injection seams for local tests.
"""
from __future__ import annotations
import asyncio
import base64
from dataclasses import dataclass,field
import hashlib
import html
import inspect
import ipaddress
import json
import math
import secrets
import socket
import ssl
import time
from collections.abc import Mapping
from urllib.parse import urlsplit,urlencode,quote,unquote

import httpcore
from httpcore import AnyIOBackend
from jsonschema import Draft202012Validator
from custom_execution import ExecutionSnapshot,CredentialSnapshot,validate_target,PROFILE,encoded,digest

MAX_REQUEST_BYTES=16384
MAX_QUERY_BYTES=8192
MAX_RESPONSE_BYTES=512*1024
MAX_HEADER_BYTES=16384
MAX_JOB_REQUESTS=16
MAX_JOB_SECONDS=60
MAX_REQUEST_SECONDS=30
MAX_ACTIVE_JOBS=64

class HTTPGatewayError(ValueError):
    """Safe category only; arbitrary provider/credential diagnostics never escape."""


def require(value,category='custom_http_invalid_request'):
    if not value:raise HTTPGatewayError(category)


async def _drain(task):
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:continue
        except Exception:break
    if task.done()and not task.cancelled():task.exception()


async def _offload(fn,*args):
    task=asyncio.create_task(asyncio.to_thread(fn,*args))
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain(task);raise


async def _callback(fn,*args):
    if inspect.iscoroutinefunction(fn):return await fn(*args)
    value=await _offload(fn,*args)
    if inspect.isawaitable(value):return await value
    return value


async def public_addresses(host):
    rows=await _offload(socket.getaddrinfo,host,443,0,socket.SOCK_STREAM)
    return tuple(dict.fromkeys(row[4][0]for row in rows))


def validated_addresses(values):
    require(isinstance(values,(tuple,list))and 1<=len(values)<=16,'custom_http_public_dns_required')
    result=[]
    for value in values:
        require(isinstance(value,str)and '%'not in value,'custom_http_public_dns_required')
        try:address=ipaddress.ip_address(value)
        except ValueError:raise HTTPGatewayError('custom_http_public_dns_required')from None
        underlying=address.ipv4_mapped if isinstance(address,ipaddress.IPv6Address)else None
        for item in (address,underlying):
            if item is None:continue
            require(item.is_global and not any((item.is_private,item.is_loopback,item.is_link_local,
                item.is_multicast,item.is_reserved,item.is_unspecified)),'custom_http_public_dns_required')
        result.append(str(address))
    return tuple(dict.fromkeys(result))


def _credential_echo(raw,content_type,secrets_):
    # Supplement the primary boundary (credentials never enter the worker).
    # Inspect standard textual encodings, including JSON Unicode escapes; an
    # arbitrary remote encoding is not a universal information-flow guarantee.
    candidates=[raw.decode('utf-8',errors='replace'),content_type]
    try:
        stack=[json.loads(raw)]
        while stack:
            value=stack.pop()
            if isinstance(value,str):candidates.append(value)
            elif isinstance(value,dict):stack.extend(value.keys());stack.extend(value.values())
            elif isinstance(value,list):stack.extend(value)
    except (ValueError,UnicodeError,RecursionError):pass
    for candidate in candidates:
        decoded=html.unescape(unquote(candidate))
        for secret in secrets_:
            if secret in candidate or secret in decoded or base64.b64encode(secret.encode()).decode()in candidate:
                return True
    return False


class _FencedStream(httpcore.AsyncNetworkStream):
    def __init__(self,stream,current):self.stream,self.current=stream,current
    async def read(self,max_bytes,timeout=None):return await self.stream.read(max_bytes,timeout)
    async def write(self,buffer,timeout=None):
        require(self.current(),'custom_http_authority_changed')
        return await self.stream.write(buffer,timeout)
    async def aclose(self):await self.stream.aclose()
    async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
        stream=await self.stream.start_tls(ssl_context,server_hostname,timeout)
        if not self.current():await stream.aclose();raise HTTPGatewayError('custom_http_authority_changed')
        return _FencedStream(stream,self.current)
    def get_extra_info(self,info):return self.stream.get_extra_info(info)


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Connect once to a validated numeric address; HTTP/TLS retain the origin."""
    def __init__(self,host,addresses,current,backend=None):
        self.host,self.addresses,self.current=host,validated_addresses(addresses),current
        self.backend=backend or AnyIOBackend();self.connected=False
    async def connect_tcp(self,host,port,timeout=None,local_address=None,socket_options=None):
        require(host==self.host and port==443 and local_address is None and not self.connected,
            'custom_http_origin_changed')
        require(self.current(),'custom_http_authority_changed');self.connected=True
        stream=await self.backend.connect_tcp(self.addresses[0],443,timeout=timeout,
            local_address=None,socket_options=socket_options)
        return _FencedStream(stream,self.current)
    async def connect_unix_socket(self,*args,**kwargs):raise HTTPGatewayError('custom_http_origin_changed')
    async def sleep(self,seconds):await asyncio.sleep(seconds)


@dataclass(frozen=True)
class HTTPGrant:
    id:str
    token:str=field(repr=False)
    expires_at:float
    snapshot_sha256:str

@dataclass(frozen=True)
class HTTPResult:
    status:int
    content_type:str
    body:bytes=field(repr=False)
    receipt:dict
    def document(self):
        return {'status':self.status,'content_type':self.content_type,
            'body_base64':base64.b64encode(self.body).decode(),'receipt':dict(self.receipt)}

@dataclass
class _Job:
    grant:HTTPGrant
    snapshot:ExecutionSnapshot
    tool:str
    operations:dict
    target:dict
    validate:object
    credentials:object
    current:object
    cancelled:asyncio.Event
    count:int=0
    active:set=field(default_factory=set)
    revoked:bool=False
    incoming:int=0
    receipts:list=field(default_factory=list)


class CustomHTTP:
    """One company host gateway, reusable across bounded isolated jobs.

    validate(snapshot) rechecks owner/source/runtime/grant authority; credentials
    returns a host-only CredentialSnapshot after that validation.
    current(snapshot) is a synchronous final-generation fence. Both callbacks
    are required even for unauthenticated targets. HTTP cannot supply callbacks,
    snapshots, identity, URLs, headers or credential values.
    """
    def __init__(self,company_id,*,max_calls=8,per_job_calls=2,request_seconds=30,
                 resolver=public_addresses,network_backend=None,ssl_context=None,clock=time.monotonic):
        require(isinstance(company_id,str)and 1<=len(company_id)<=128)
        require(type(max_calls)is int and 1<=max_calls<=8 and type(per_job_calls)is int and 1<=per_job_calls<=2)
        require(type(request_seconds)in(int,float)and 0<request_seconds<=MAX_REQUEST_SECONDS)
        self.company_id,self.max_calls,self.per_job_calls=company_id,max_calls,per_job_calls
        self.request_seconds,self.resolver,self.backend,self.clock=request_seconds,resolver,network_backend,clock
        self.ssl=ssl_context or ssl.create_default_context()
        require(self.ssl.verify_mode==ssl.CERT_REQUIRED and self.ssl.check_hostname,'custom_http_verified_tls_required')
        self.jobs={};self.work=set();self.issuance=set();self.closed=False;self.incoming=0

    async def issue(self,snapshot,tool,*,validate,credentials,current,cancelled,ttl_seconds=60):
        require(not self.closed and len(self.issuance)<self.max_calls,'custom_http_capacity')
        task=asyncio.create_task(self._issue(snapshot,tool,validate=validate,credentials=credentials,
            current=current,cancelled=cancelled,ttl_seconds=ttl_seconds));self.issuance.add(task)
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel();await _drain(task);raise
        finally:
            if not task.done():task.cancel();await _drain(task)
            self.issuance.discard(task)

    async def _issue(self,snapshot,tool,*,validate,credentials,current,cancelled,ttl_seconds):
        require(not self.closed and isinstance(snapshot,ExecutionSnapshot)and isinstance(cancelled,asyncio.Event))
        require(all(callable(x)for x in(validate,credentials,current)))
        require(type(ttl_seconds)in(int,float)and 0<ttl_seconds<=MAX_JOB_SECONDS)
        data=snapshot.metadata
        require(data['company']==self.company_id and isinstance(data.get('runtime'),dict)
            and data['runtime'].get('profile')==PROFILE,'custom_http_scope_changed')
        require(current(snapshot)is True and not cancelled.is_set(),'custom_http_authority_changed')
        require(len(self.jobs)<MAX_ACTIVE_JOBS,'custom_http_capacity')
        # Validate before issuing authority. The host coordinator bounds issuance;
        # network request admission below is reserved before every callback await.
        actual=await _callback(validate,snapshot)
        require(not self.closed and len(self.jobs)<MAX_ACTIVE_JOBS,'custom_http_capacity')
        require(actual is True or actual==snapshot,'custom_http_authority_changed')
        require(current(snapshot)is True and not cancelled.is_set(),'custom_http_authority_changed')
        target=validate_target(data['target'],{f['name']for f in data['secret_fields']})
        require(target==data['target']and digest(target)==data['target_sha256'],'custom_http_scope_changed')
        selected=next((t for t in data['manifest']['tools']if t['name']==tool),None)
        require(selected is not None,'custom_http_operation_denied')
        operations={o['id']:o for o in target['operations']if o['id']in selected['operations']}
        require(set(operations)==set(selected['operations']),'custom_http_operation_denied')
        token=secrets.token_urlsafe(32);grant=HTTPGrant('http-'+secrets.token_hex(16),token,self.clock()+ttl_seconds,snapshot.sha256)
        self.jobs[hashlib.sha256(token.encode()).hexdigest()]=_Job(grant,snapshot,tool,operations,target,
            validate,credentials,current,cancelled)
        return grant

    def _alive(self,job):
        if self.closed or job.revoked or job.cancelled.is_set()or self.clock()>=job.grant.expires_at:return False
        try:return job.current(job.snapshot)is True
        except Exception:return False

    def _job(self,token):
        require(isinstance(token,str)and 20<=len(token)<=128,'custom_http_unauthorized')
        job=self.jobs.get(hashlib.sha256(token.encode()).hexdigest())
        require(job is not None,'custom_http_unauthorized')
        require(self._alive(job),'custom_http_authority_changed');return job

    def _request(self,job,document):
        require(isinstance(document,dict)and set(document)=={'operation','query'})
        require(len(encoded(document))<=MAX_REQUEST_BYTES)
        operation=job.operations.get(document['operation']) if isinstance(document['operation'],str)else None
        require(operation is not None,'custom_http_operation_denied');query=document['query']
        require(isinstance(query,dict)and Draft202012Validator(operation['query_schema']).is_valid(query))
        pairs=[]
        for key,value in query.items():
            require(isinstance(key,str)and 1<=len(key)<=128)
            values=value if isinstance(value,list)else[value];require(len(values)<=64)
            for part in values:
                require(part is None or type(part)in(str,bool,int,float))
                if isinstance(part,float):require(math.isfinite(part))
                text=''if part is None else ('true'if part else'false')if type(part)is bool else str(part)
                pairs.append((key,text))
        encoded_query=urlencode(pairs,quote_via=quote,safe='')
        require(len(encoded_query.encode())<=MAX_QUERY_BYTES,'custom_http_query_limit')
        url=job.target['origin']+operation['path']+('?' + encoded_query if encoded_query else '')
        return operation,url

    def _headers(self,job,values):
        require(isinstance(values,Mapping),'custom_http_credentials_unavailable')
        auth=job.target['authentication'];kind=auth['kind'];hidden=[]
        headers=[(b'accept',b'application/json, text/plain;q=0.9, */*;q=0.1'),(b'accept-encoding',b'identity')]
        def value(field):
            raw=values.get(field);require(isinstance(raw,str)and 1<=len(raw.encode())<=8192 and not any(ord(c)<32 or ord(c)==127 for c in raw),'custom_http_credentials_unavailable')
            hidden.append(raw);return raw
        if kind=='bearer':
            import re
            token=value(auth['credential']);require(re.fullmatch(r'[A-Za-z0-9._~+/=-]+',token),'custom_http_credentials_unavailable')
            headers.append((b'authorization',('Bearer '+token).encode()))
        elif kind=='header':
            name=auth['name'].lower();require(name not in {'accept','accept-encoding','content-type','user-agent','te','trailer'},'custom_http_auth_header_denied')
            headers.append((name.encode('ascii'),value(auth['credential']).encode('ascii')))
        elif kind=='basic':
            user,password=value(auth['username_field']),value(auth['password_field']);require(':'not in user,'custom_http_credentials_unavailable')
            secret=base64.b64encode((user+':'+password).encode()).decode();hidden.append(secret)
            headers.append((b'authorization',('Basic '+secret).encode()))
        require(sum(len(k)+len(v)+4 for k,v in headers)<=MAX_HEADER_BYTES,'custom_http_header_limit')
        return headers,hidden

    async def request(self,token,document):
        job=self._job(token);operation,url=self._request(job,document)
        require(len(self.work)<self.max_calls and len(job.active)<self.per_job_calls,'custom_http_capacity')
        require(job.count<MAX_JOB_REQUESTS,'custom_http_job_request_limit')
        job.count+=1
        task=asyncio.create_task(self._execute(job,operation,url));job.active.add(task);self.work.add(task)
        watcher=asyncio.create_task(job.cancelled.wait())
        try:
            done,_=await asyncio.wait((task,watcher),return_when=asyncio.FIRST_COMPLETED)
            if watcher in done or not self._alive(job):
                task.cancel();await _drain(task);raise HTTPGatewayError('custom_http_authority_changed')
            return task.result()
        except asyncio.CancelledError:
            task.cancel();await _drain(task);raise
        finally:
            watcher.cancel();await _drain(watcher)
            if not task.done():task.cancel();await _drain(task)
            job.active.discard(task);self.work.discard(task)

    async def _execute(self,job,operation,url):
        try:
            deadline=min(job.grant.expires_at,self.clock()+self.request_seconds)
            async with asyncio.timeout(max(0,deadline-self.clock())):
                require(self._alive(job),'custom_http_authority_changed')
                validated=await _callback(job.validate,job.snapshot)
                require((validated is True or validated==job.snapshot)and self._alive(job),'custom_http_authority_changed')
                bundle=await _callback(job.credentials,job.snapshot)
                data=job.snapshot.metadata
                require(isinstance(bundle,CredentialSnapshot)and bundle.snapshot_sha256==job.snapshot.sha256
                    and bundle.company_id==self.company_id and bundle.owner_id==data['owner']
                    and bundle.connector_id==data['connector_id']and bundle.credential_generation==data['credential_generation'],
                    'custom_http_credentials_unavailable')
                require(self._alive(job),'custom_http_authority_changed');headers,hidden=self._headers(job,bundle.as_mapping())
                host=urlsplit(job.target['origin']).hostname
                addresses=validated_addresses(await self.resolver(host))
                require(self._alive(job),'custom_http_authority_changed')
                backend=_PinnedBackend(host,addresses,lambda:self._alive(job),self.backend)
                async with httpcore.AsyncConnectionPool(ssl_context=self.ssl,network_backend=backend,
                    max_connections=1,max_keepalive_connections=0,http2=False,retries=0)as pool:
                    headers=[(b'host',host.encode('ascii')),*headers]
                    async with pool.stream(operation['method'],url,headers=headers,
                        extensions={'timeout':{'connect':5,'read':10,'write':5,'pool':1}})as response:
                        require(not 300<=response.status<400,'custom_http_redirect_denied')
                        require(len(response.headers)<=100 and sum(len(k)+len(v)+4 for k,v in response.headers)<=MAX_HEADER_BYTES,'custom_http_header_limit')
                        header_map={k.lower():v for k,v in response.headers}
                        require(header_map.get(b'content-encoding',b'identity').lower()in(b'',b'identity'),'custom_http_encoding_denied')
                        content_type=header_map.get(b'content-type',b'application/octet-stream').decode('ascii')
                        require(len(content_type)<=256 and not any(ord(c)<32 for c in content_type),'custom_http_header_limit')
                        body=bytearray()
                        async for chunk in response.aiter_stream():
                            require(len(body)+len(chunk)<=MAX_RESPONSE_BYTES,'custom_http_response_limit');body.extend(chunk)
                        require(self._alive(job),'custom_http_authority_changed')
                        final=await _callback(job.validate,job.snapshot)
                        require((final is True or final==job.snapshot)and self._alive(job),'custom_http_authority_changed')
                        raw=bytes(body)
                        require(not _credential_echo(raw,content_type,hidden),'custom_http_credential_echo')
                        receipt={'snapshot_sha256':job.snapshot.sha256,'operation':operation['id'],
                            'query_sha256':hashlib.sha256(url.encode()).hexdigest(),'method':operation['method'],
                            'status':response.status,'body_sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}
                        job.receipts.append(encoded(receipt))
                        return HTTPResult(response.status,content_type,raw,receipt)
        except asyncio.CancelledError:raise
        except HTTPGatewayError:raise
        except Exception:raise HTTPGatewayError('custom_http_request_failed')from None

    def observations(self,grant_id):
        """Host-only journal; caller binds grant ID to its admitted worker/test."""
        rows=[job for job in self.jobs.values()if job.grant.id==grant_id]
        require(len(rows)==1,'custom_http_unauthorized')
        return tuple({**json.loads(raw),'receipt_sha256':hashlib.sha256(raw).hexdigest()}for raw in rows[0].receipts)

    async def revoke(self,grant_id):
        jobs=[j for j in self.jobs.values()if j.grant.id==grant_id]
        for job in jobs:
            job.revoked=True
            for task in tuple(job.active):task.cancel()
        for job in jobs:
            for task in tuple(job.active):await _drain(task)
        receipts=tuple({**json.loads(raw),'receipt_sha256':hashlib.sha256(raw).hexdigest()}for job in jobs for raw in job.receipts)
        for key,job in list(self.jobs.items()):
            if job in jobs:self.jobs.pop(key,None)
        return receipts

    async def close(self):
        self.closed=True
        for job in self.jobs.values():job.revoked=True
        tasks=tuple(self.work|self.issuance)
        for task in tasks:task.cancel()
        for task in tasks:await _drain(task)
        self.jobs.clear()

    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan':
            while True:
                event=await receive()
                if event['type']=='lifespan.startup':await send({'type':'lifespan.startup.complete'})
                elif event['type']=='lifespan.shutdown':
                    await self.close();await send({'type':'lifespan.shutdown.complete'});return
            return
        if scope['type']!='http':raise HTTPGatewayError('custom_http_unsupported_transport')
        status=400;document={'error':'custom_http_invalid_request'};incoming=None
        try:
            require(scope.get('path')=='/request'and scope.get('method')=='POST')
            headers=scope.get('headers',[]);require(len(headers)<=40 and sum(len(k)+len(v)for k,v in headers)<=MAX_HEADER_BYTES)
            auth=[v for k,v in headers if k.lower()==b'authorization'];require(len(auth)==1 and auth[0].startswith(b'Bearer '),'custom_http_unauthorized')
            token=auth[0][7:].decode('ascii');job=self._job(token)
            require(self.incoming<self.max_calls and job.incoming<self.per_job_calls,'custom_http_capacity')
            self.incoming+=1;job.incoming+=1;incoming=job
            raw=bytearray()
            async with asyncio.timeout(5):
                while True:
                    item=await receive()
                    require(item.get('type')=='http.request')
                    raw.extend(item.get('body',b''));require(len(raw)<=MAX_REQUEST_BYTES)
                    if not item.get('more_body'):break
            result=await self.request(token,json.loads(raw));document=result.document();status=200
        except asyncio.CancelledError:raise
        except HTTPGatewayError as error:
            category=str(error);document={'error':category};status=401 if category=='custom_http_unauthorized'else 429 if category=='custom_http_capacity'else 400
        except Exception:document={'error':'custom_http_invalid_request'}
        finally:
            if incoming is not None:self.incoming-=1;incoming.incoming-=1
        body=encoded(document)
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),(b'cache-control',b'no-store'),(b'content-length',str(len(body)).encode())]})
        await send({'type':'http.response.body','body':body})
