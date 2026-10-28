"""Browser upload adapter; host context owns company, actor and commissioning.

Registration creates no worker and makes no provider call. The host supplies a
SourceStore for its selected workspace and explicitly enables a ready pipeline.
Keep the application's existing session/origin/CSRF middleware around these
routes; the adapter also checks write capability and CSRF, including PATCH.
"""
from __future__ import annotations
from access import page_request

import asyncio
from dataclasses import dataclass
import json
import math
import re
import tempfile
from typing import Callable, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect

from chat_permissions import ChatPermissions, workspace_writable
from source_intake import IntakeActor, IntakeError, SourceStore
from intake_projection import public_import as _public, IntakeProjection, MAX_OFFSET

MAX_JSON_BYTES = 16 * 1024
MAX_CHUNK_BYTES = 8 * 1024**2
MAX_RECEIVING_REQUESTS = 4
PRIVATE_HEADERS = {'Cache-Control': 'private, no-store'}


@dataclass(frozen=True)
class SourceRoutePolicy:
    intake_enabled: bool = False
    worker_ready: bool = False
    empty_workspace_collection: str | None = None
    body_timeout_seconds: float = 30

    def __post_init__(self):
        if (type(self.intake_enabled) is not bool or type(self.worker_ready) is not bool
                or not isinstance(self.body_timeout_seconds, (int,float))
                or isinstance(self.body_timeout_seconds, bool)
                or not math.isfinite(self.body_timeout_seconds)
                or not 0 < self.body_timeout_seconds <= 120):
            raise ValueError('invalid_source_route_policy')
        if (self.empty_workspace_collection is not None and
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', self.empty_workspace_collection)):
            raise ValueError('invalid_source_route_policy')


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class CreateImport(StrictBody):
    collection: str = Field(min_length=1,max_length=160)
    filename: str = Field(min_length=1,max_length=255)
    size: int = Field(ge=1)
    source_id: str | None = Field(default=None,pattern=r'^source-[a-f0-9]{32}$')


class CompleteImport(StrictBody):
    sha256: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')


class CreateSource(StrictBody):
    collection: str = Field(min_length=1,max_length=160)
    name: str = Field(min_length=1,max_length=200)
    kind: Literal['upload'] = 'upload'


def _deny(status, code):
    raise HTTPException(status, detail=code, headers=PRIVATE_HEADERS)


def _header(request, name, *, required=False):
    values = request.headers.getlist(name)
    if len(values)>1 or (values and len(values[0])>256):
        _deny(400,'invalid_headers')
    if not values:
        if required: _deny(411 if name=='content-length' else 400,'missing_'+name.replace('-','_'))
        return None
    return values[0]


def _number_header(request, name, *, maximum, required=True):
    value = _header(request,name,required=required)
    if value is None: return None
    if not re.fullmatch(r'[0-9]{1,20}',value): _deny(400,'invalid_headers')
    number=int(value)
    if number>maximum: _deny(413,'request_size_limit' if name=='content-length' else 'invalid_offset')
    return number


def _encoding(request):
    if _header(request,'content-encoding') not in {None,'identity'}:
        _deny(415,'unsupported_content_encoding')
    if _header(request,'transfer-encoding') is not None:
        _deny(400,'use_content_length')


async def _offload(function, *args, **kwargs):
    """Keep private streams open until their finite disk transaction finishes."""
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        if task.done() and not task.cancelled(): task.exception()
        raise


async def _call(function,*args,**kwargs):
    try: return await _offload(function,*args,**kwargs)
    except IntakeError as error:
        code=str(error)
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',code): code='invalid_import_request'
        if code=='record_not_found': status=404
        elif code in {'read_only_workspace','workspace_access_required'}: status=403
        elif code in {'storage_quota'}: status=507
        elif code in {'source_limit','asset_limit','upload_capacity','upload_chunk_limit'}: status=429
        elif code in {'upload_size_exceeded'}: status=413
        elif code in {'storage_unavailable','invalid_storage','invalid_storage_file','original_integrity_failed',
                      'committed_bytes_missing','invalid_default_source','embedding_allowance_required','source_service_unavailable'}: status=503
        elif code in {'offset_mismatch','chunk_conflict','idempotency_conflict','upload_not_open','upload_incomplete',
                      'source_received_cancel_job_instead','import_finished','checksum_mismatch','embedding_allowance_reserved'}: status=409
        else: status=400
        _deny(status,code)


def _duplicates(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate key')
        result[key]=value
    return result


async def _json_body(request, model, policy, *, empty=False):
    _encoding(request)
    declared=_number_header(request,'content-length',maximum=MAX_JSON_BYTES,required=False)
    content_type=_header(request,'content-type')
    if (content_type or '').split(';',1)[0].strip().lower()!='application/json' and not (empty and declared==0):
        _deny(415,'use_application_json')
    raw=bytearray()
    try:
        async with asyncio.timeout(policy.body_timeout_seconds):
            async for chunk in request.stream():
                if len(raw)+len(chunk)>MAX_JSON_BYTES: _deny(413,'request_size_limit')
                raw.extend(chunk)
    except TimeoutError: _deny(408,'request_timeout')
    except ClientDisconnect: _deny(400,'incomplete_request')
    if declared is not None and len(raw)!=declared: _deny(400,'request_length_mismatch')
    try:
        value={} if empty and not raw else json.loads(raw,object_pairs_hook=_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return model.model_validate(value)
    except (ValueError,TypeError,UnicodeDecodeError,ValidationError,RecursionError):
        _deny(400,'invalid_request')


def register_source_routes(app: FastAPI, *, store_for_workspace: Callable,
                           policy_for_workspace: Callable | None = None,
                           admission_for_workspace: Callable | None = None,
                           projection_for_workspace: Callable | None = None):
    """Register once after the host's authenticated workspace middleware.

    All callbacks receive request.state.workspace, never a browser-selected path.
    The default policy permits reads while every write returns 503. Enable writes
    only after the media/index workers and funded admission are commissioned.
    """
    from retail_urls import retail_router
    app = retail_router(app)
    if getattr(app.state,'source_routes_registered',False):
        raise ValueError('source_routes_already_registered')
    app.state.source_routes_registered=True
    receiving=0

    async def context(request, *, write=False, continuation=False, projected=False):
        session=getattr(request.state,'session',None)
        selected=getattr(request.state,'workspace',None)
        if not session: _deny(401,'sign_in_required')
        principal=ChatPermissions.principal(session)
        if not principal or not isinstance(selected,dict): _deny(403,'workspace_access_required')
        writable=workspace_writable(session)
        if write:
            if not writable: _deny(403,'read_only_workspace')
            if not page_request(request.headers): _deny(403,'csrf_required')
            origin=_header(request,'origin')
            if origin and urlparse(origin).netloc!=request.headers.get('host'): _deny(403,'same_origin_required')
        projection=projection_for_workspace(selected) if projected and projection_for_workspace else None
        policy=await _call(projection.host.policy) if projection else policy_for_workspace(selected) if policy_for_workspace else SourceRoutePolicy()
        if not isinstance(policy,SourceRoutePolicy): _deny(503,'intake_unavailable')
        admission=projection.host if projection else admission_for_workspace(selected) if admission_for_workspace else None
        if write and not (continuation and admission is not None) and not (policy.intake_enabled and policy.worker_ready): _deny(503,'intake_unavailable')
        store=projection or await _call(store_for_workspace,selected)
        if not isinstance(store,(SourceStore,IntakeProjection)): _deny(503,'intake_unavailable')
        datasets=selected['index'].status().get('datasets',[])
        collections={value for value in datasets if isinstance(value,str)}
        if not projection and not collections and policy.empty_workspace_collection:
            collections.add(policy.empty_workspace_collection)
        actor=IntakeActor(principal,read_only=not writable)
        if projection:
            try: collections.update(await _call(projection.collections,actor))
            except (HTTPException,OSError,ValueError,KeyError,TypeError): pass  # The snapshot reports unavailable, never a fabricated empty success.
        elif admission is not None: collections.update(await _call(admission.collections,actor))
        return store,actor,policy,collections,admission

    def collection(value, allowed):
        if value not in allowed: _deny(404,'collection_not_found')

    async def view_for(store,actor,identity,allowed):
        if not re.fullmatch(r'upload-[a-f0-9]{32}',identity): _deny(404,'record_not_found')
        view=await _call(store.get_import,actor,identity)
        collection(view['source']['collection'],allowed)
        return view

    def response(view,store,*,status_code=200):
        value=_public(view)
        return JSONResponse({'import':value,'chunk_bytes_max':min(MAX_CHUNK_BYTES,store.limits.max_chunk_bytes)},
            status_code=status_code,headers={**PRIVATE_HEADERS,
                'Upload-Offset':str(value['committed_bytes']),'Upload-Length':str(value['expected_bytes'])})

    @app.get('/api/retail/library/imports')
    async def imports(request: Request):
        store,actor,policy,allowed,admission=await context(request,projected=True)
        if set(request.query_params)-{'collection','limit','offset'}: _deny(400,'invalid_query')
        if any(len(request.query_params.getlist(key))!=1 for key in request.query_params): _deny(400,'invalid_query')
        selected=request.query_params.get('collection')
        if selected is not None: collection(selected,allowed)
        try:
            raw_limit=request.query_params.get('limit','100');raw_offset=request.query_params.get('offset','0')
            if not re.fullmatch(r'[0-9]{1,6}',raw_limit) or not re.fullmatch(r'[0-9]{1,10}',raw_offset): raise ValueError()
            limit=int(raw_limit);offset=int(raw_offset)
            if not 1<=limit<=200 or not 0<=offset<=(MAX_OFFSET if isinstance(store,IntakeProjection) else store.limits.max_assets): raise ValueError()
        except ValueError: _deny(400,'invalid_query')
        if isinstance(store,IntakeProjection):
            data=await _call(store.snapshot,actor.principal_id,selected,read_only=actor.read_only,limit=limit,offset=offset)
            return JSONResponse(data,headers=PRIVATE_HEADERS)
        views=await _call(store.list_imports,actor,collection=selected,limit=limit,offset=offset)
        grants=await _call(admission.capabilities,actor) if admission is not None else {}
        return JSONResponse({'imports':[_public(view) for view in views if view['source']['collection'] in allowed],
            'offset':offset,'next_offset':offset+len(views) if len(views)==limit else None,
            'intake':{'available':policy.intake_enabled and policy.worker_ready,
                      'can_upload':policy.intake_enabled and policy.worker_ready and not actor.read_only and grants.get('can_upload',True),
                      'can_manage_imports':not actor.read_only and grants.get('can_manage_imports',policy.intake_enabled and policy.worker_ready),
                      'notice':grants.get('notice',''),
                      'collections':sorted(allowed),
                      'default_collection':policy.empty_workspace_collection if policy.empty_workspace_collection in allowed else None,
                      'chunk_bytes_max':min(MAX_CHUNK_BYTES,store.limits.max_chunk_bytes)}},headers=PRIVATE_HEADERS)

    @app.post('/api/retail/library/imports')
    async def begin(request: Request):
        store,actor,policy,allowed,admission=await context(request,write=True,continuation=True)
        body=await _json_body(request,CreateImport,policy)
        collection(body.collection,allowed)
        if body.size>store.limits.max_file_bytes: _deny(413,'file_size_limit')
        key=_header(request,'idempotency-key',required=True)
        if not key.strip() or len(key.encode())>160 or any(ord(c)<32 for c in key): _deny(400,'invalid_idempotency_key')
        if admission is not None:
            upload=await _call(admission.begin,store,actor,collection=body.collection,filename=body.filename,size=body.size,idempotency_key=key,source_id=body.source_id)
            return response(await view_for(store,actor,upload['id'],allowed),store,status_code=201)
        if body.source_id:
            source=await _call(store.get_source,actor,body.source_id)
            if source['collection']!=body.collection or source['kind']!='upload': _deny(404,'record_not_found')
        else: source=await _call(store.ensure_upload_source,actor,collection=body.collection)
        upload=await _call(store.begin_upload,actor,source['id'],filename=body.filename,size=body.size,idempotency_key=key)
        return response(await view_for(store,actor,upload['id'],allowed),store,status_code=201)

    @app.get('/api/retail/library/imports/{upload_id}')
    async def detail(upload_id: str,request: Request):
        store,actor,_,allowed,admission=await context(request)
        return response(await view_for(store,actor,upload_id,allowed),store)

    @app.patch('/api/retail/library/imports/{upload_id}/content')
    async def content(upload_id: str,request: Request):
        nonlocal receiving
        store,actor,policy,allowed,admission=await context(request,write=True,continuation=True)
        view=await view_for(store,actor,upload_id,allowed)
        if admission is not None: await _call(admission.authorize,actor,view,operation='content')
        _encoding(request)
        if (_header(request,'content-type') or '').split(';',1)[0].strip().lower() not in {
                'application/octet-stream','application/offset+octet-stream'}: _deny(415,'use_octet_stream')
        size=_number_header(request,'content-length',maximum=min(MAX_CHUNK_BYTES,store.limits.max_chunk_bytes))
        offset=_number_header(request,'upload-offset',maximum=store.limits.max_file_bytes)
        checksum=_header(request,'x-chunk-sha256')
        if checksum is not None and not re.fullmatch(r'[a-f0-9]{64}',checksum): _deny(400,'invalid_checksum')
        if size<1: _deny(400,'empty_chunk')
        upload=view['upload']
        if upload['status']!='uploading' or upload['expires']<=store.clock(): _deny(409,'upload_not_open')
        if offset>upload['committed_bytes']: _deny(409,'offset_mismatch')
        if offset+size>upload['expected_bytes']: _deny(413,'upload_size_exceeded')
        if receiving>=MAX_RECEIVING_REQUESTS: _deny(429,'upload_receive_capacity')
        receiving+=1
        try:
            with tempfile.SpooledTemporaryFile(max_size=64*1024,mode='w+b',dir=store.root) as stream:
                received=0
                try:
                    async with asyncio.timeout(policy.body_timeout_seconds):
                        async for chunk in request.stream():
                            received+=len(chunk)
                            if received>size: _deny(413,'chunk_length_exceeded')
                            if chunk: await _offload(stream.write,chunk)
                except TimeoutError: _deny(408,'request_timeout')
                except ClientDisconnect: _deny(400,'incomplete_request')
                if received!=size: _deny(400,'chunk_length_mismatch')
                stream.seek(0)
                await _call(store.append_stream,actor,upload_id,offset=offset,size=size,stream=stream,sha256=checksum)
        finally: receiving-=1
        return response(await view_for(store,actor,upload_id,allowed),store)

    @app.post('/api/retail/library/imports/{upload_id}/complete')
    async def complete(upload_id: str,request: Request):
        store,actor,policy,allowed,admission=await context(request,write=True,continuation=True)
        view=await view_for(store,actor,upload_id,allowed)
        if admission is not None: await _call(admission.authorize,actor,view,operation='complete')
        body=await _json_body(request,CompleteImport,policy,empty=True)
        await _call(store.seal_upload,actor,upload_id,expected_sha256=body.sha256)
        return response(await view_for(store,actor,upload_id,allowed),store)

    @app.delete('/api/retail/library/imports/{upload_id}')
    async def cancel(upload_id: str,request: Request):
        store,actor,_,allowed,admission=await context(request,write=True,continuation=True)
        view=await view_for(store,actor,upload_id,allowed)
        if admission is not None: await _call(admission.authorize,actor,view,operation='cancel')
        return response(await _call(store.cancel_import,actor,upload_id),store)

    @app.get('/api/retail/sources')
    async def sources(request: Request):
        store,actor,_,allowed,admission=await context(request)
        if set(request.query_params)-{'collection'} or len(request.query_params.getlist('collection'))>1:
            _deny(400,'invalid_query')
        selected=request.query_params.get('collection')
        if selected is not None: collection(selected,allowed)
        values=await _call(store.list_sources,actor,collection=selected)
        return JSONResponse({'sources':[value for value in values if value['collection'] in allowed]},headers=PRIVATE_HEADERS)

    @app.post('/api/retail/sources')
    async def add_source(request: Request):
        store,actor,policy,allowed,admission=await context(request,write=True)
        body=await _json_body(request,CreateSource,policy)
        collection(body.collection,allowed)
        result=await _call(store.create_source,actor,**body.model_dump())
        return JSONResponse(result,status_code=201,headers=PRIVATE_HEADERS)

    @app.get('/api/retail/sources/{source_id}')
    async def source(source_id: str,request: Request):
        store,actor,_,allowed,admission=await context(request)
        if not re.fullmatch(r'source-[a-f0-9]{32}',source_id): _deny(404,'record_not_found')
        result=await _call(store.get_source,actor,source_id)
        collection(result['collection'],allowed)
        return JSONResponse(result,headers=PRIVATE_HEADERS)
