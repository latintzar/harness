"""Authenticated retail geography and per-chat location controls.

The host supplies actual current actors, allowed collections and chat ACL reads.
Neither body fields nor URL identifiers are company/owner authority.
"""
from access import page_request
import inspect
import json
import asyncio
import sqlite3
from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse
from chat_permissions import workspace_writable
from custom_coordinator import offload
from business_scope import ScopeActor, ScopeError, identifier
from starlette.requests import ClientDisconnect

PRIVATE={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}

async def call(callback,*args,**kwargs):
    result=callback(*args,**kwargs)
    return await result if inspect.isawaitable(result) else result

async def body(request,fields):
    if not workspace_writable(getattr(request.state,'session',None)) or not page_request(request.headers):
        raise HTTPException(403,'scope_read_only',headers=PRIVATE)
    if request.headers.get('content-type','').split(';')[0]!='application/json':
        raise HTTPException(415,'use_application_json',headers=PRIVATE)
    raw=bytearray()
    try:
        async with asyncio.timeout(10):
            async for part in request.stream():
                raw.extend(part)
                if len(raw)>32768:raise HTTPException(413,'scope_request_limit',headers=PRIVATE)
    except TimeoutError:raise HTTPException(408,'scope_request_timeout',headers=PRIVATE)from None
    except ClientDisconnect:raise HTTPException(400,'scope_request_incomplete',headers=PRIVATE)from None
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError()
            value[key]=item
        return value
    try:
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value,dict) or set(value)!=fields:raise ValueError()
        return value
    except (ValueError,TypeError,RecursionError):raise HTTPException(400,'invalid_scope_request',headers=PRIVATE)from None

async def result(function,*args,**kwargs):
    try:return await offload(function,*args,**kwargs)
    except ScopeError as error:
        code=str(error)
        status=409 if 'conflict' in code or 'fixed' in code else 403 if any(s in code for s in ('denied','access','read_only','authority')) else 404 if 'unavailable' in code else 400
        raise HTTPException(status,code,headers=PRIVATE)from None
    except (sqlite3.Error,OSError):raise HTTPException(503,'scope_store_unavailable',headers=PRIVATE)from None


def register_scope_routes(app,*,store_for_workspace,actor_for_request,collection_for_request,chat_for_request,describe_for_request=None):
    from retail_urls import retail_router
    app=retail_router(app)
    async def selected(request,collection,write=False):
        if not getattr(request.state,'session',{}).get('authenticated'):
            raise HTTPException(401,'scope_access_required',headers=PRIVATE)
        collection=await call(collection_for_request,request,collection)
        actor=await call(actor_for_request,request,write=write)
        store=store_for_workspace(request.state.workspace)
        return store,actor,collection

    @app.get('/api/retail/scope')
    async def graph(request:Request,collection:str):
        store,actor,collection=await selected(request,collection)
        value=await result(store.describe,actor,collection)
        if describe_for_request is not None:value=await call(describe_for_request,request,value)
        return JSONResponse(value,headers=PRIVATE)

    @app.get('/api/retail/scope/events')
    async def graph_events(request:Request,collection:str,after:int=0,limit:int=100):
        store,actor,collection=await selected(request,collection)
        return JSONResponse({'events':await result(store.events,actor,collection,after=after,limit=limit)},headers=PRIVATE)

    @app.post('/api/retail/scope/{kind}')
    async def change(kind:str,request:Request):
        value=await body(request,{'collection','expected_revision','value'})
        store,actor,collection=await selected(request,value['collection'],True)
        value=await result(store.mutate,actor,collection,kind,value['value'],value['expected_revision'])
        if describe_for_request is not None:value=await call(describe_for_request,request,value)
        return JSONResponse(value,headers=PRIVATE)

    @app.get('/api/retail/chats/{chat_id}/scope')
    async def chat_scope(chat_id:str,request:Request):
        chat=await call(chat_for_request,request,chat_id,write=False)
        store,actor,collection=await selected(request,chat['dataset'])
        return JSONResponse(await result(store.chat_scope,actor,chat_id,collection),headers=PRIVATE)

    @app.put('/api/retail/chats/{chat_id}/scope')
    async def change_chat_scope(chat_id:str,request:Request):
        value=await body(request,{'location_ids','expected_revision','expected_selection_revision'})
        chat=await call(chat_for_request,request,chat_id,write=True)
        store,actor,collection=await selected(request,chat['dataset'],True)
        return JSONResponse(await result(store.bind_chat,actor,chat_id,collection,value['location_ids'],value['expected_revision'],
            expected_selection_revision=value['expected_selection_revision']),headers=PRIVATE)

def register_host_routes(app):
    """Register private graph/selector APIs using the host's current session ACL."""
    async def actor(request,write=False):
        selected=request.state.workspace;session=request.state.session or {}
        principal=selected['chats'].principal(session)
        requester=(session.get('principal') or {}).get('id')
        store=selected['business_scope']
        if (not principal or not requester or not await asyncio.to_thread(store.member,requester)
                or write and not workspace_writable(session)):
            raise HTTPException(403,'scope_access_required',headers=PRIVATE)
        value=ScopeActor(store.company_id,principal,bool(session.get('viewing_as')))
        try:await asyncio.to_thread(store.authority,value,write=write)
        except ScopeError:raise HTTPException(403,'scope_access_required',headers=PRIVATE)from None
        return value

    async def collection(request,value):
        selected=request.state.workspace
        try:identifier(value)
        except ScopeError:raise HTTPException(400,'invalid_business_scope',headers=PRIVATE)from None
        principal = selected['chats'].principal(request.state.session)
        if not await asyncio.to_thread(selected['collections'].contains, value, principal):
            raise HTTPException(404,'scope_collection_unavailable',headers=PRIVATE)
        return value

    async def chat(request,key,write=False):
        await actor(request,write)
        selected=request.state.workspace;record=selected['agent'].children.get(key)
        allowed=selected['chats'].can_write if write else selected['chats'].can_read
        if not record or not allowed(record,request.state.session):
            raise HTTPException(404,'scope_chat_unavailable',headers=PRIVATE)
        if record.get('parent_id') or record.get('native_root_id') not in (None,key):
            raise HTTPException(403,'scope_root_chat_required',headers=PRIVATE)
        if write and (record.get('running') or key in selected['agent'].active):
            raise HTTPException(409,'scope_chat_running',headers=PRIVATE)
        if not record.get('dataset'):
            raise HTTPException(409,'scope_chat_business_required',headers=PRIVATE)
        return record

    async def describe(request,value):
        principal=request.state.workspace['chats'].principal(request.state.session)
        selected=request.state.workspace
        try:
            options=await asyncio.to_thread(selected['vertical'].source_options, principal, value['collection'])
        except (ScopeError,sqlite3.Error,OSError):
            raise HTTPException(503,'scope_store_unavailable',headers=PRIVATE)from None
        return {**value,'source_options':options}

    register_scope_routes(app,store_for_workspace=lambda selected:selected['business_scope'],
        actor_for_request=actor,collection_for_request=collection,chat_for_request=chat,
        describe_for_request=describe)
