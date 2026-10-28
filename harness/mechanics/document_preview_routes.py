"""Authenticated local page-preview routes. Host registration enables no decoder."""
from access import page_request
import asyncio
import json
import sqlite3
import threading
from urllib.parse import urlsplit
from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse,Response
from chat_permissions import ChatPermissions,workspace_writable
from document_previews import DocumentPreviews,DocumentPreviewError,offload
from source_media import _drain

HEADERS={'Cache-Control':'private, no-store'}
SLOTS=threading.BoundedSemaphore(2)

def deny(status,code):raise HTTPException(status,detail=code,headers=HEADERS)

def identity(request,write=False):
    session=getattr(request.state,'session',None);workspace=getattr(request.state,'workspace',None)
    if not session or not session.get('authenticated'):deny(401,'sign_in_required')
    principal=ChatPermissions.principal(session)
    if not principal or not isinstance(workspace,dict):deny(403,'workspace_access_required')
    if request.query_params:deny(400,'invalid_query')
    if write:
        if not workspace_writable(session):deny(403,'read_only_workspace')
        if not page_request(request.headers):deny(403,'csrf_required')
        origins=request.headers.getlist('origin')
        if len(origins)>1:deny(400,'invalid_headers')
        if origins:
            parsed=urlsplit(origins[0])
            if (parsed.scheme not in {'http','https'} or parsed.username or parsed.password
                or parsed.netloc!=request.headers.get('host') or parsed.path or parsed.query or parsed.fragment):
                deny(403,'same_origin_required')
    return workspace,principal

async def empty_body(request):
    if request.headers.get('content-encoding','identity')!='identity':deny(415,'unsupported_content_encoding')
    if len(request.headers.getlist('content-length'))!=1 or request.headers.get('transfer-encoding'):deny(400,'invalid_headers')
    try:length=int(request.headers['content-length'])
    except ValueError:deny(400,'invalid_headers')
    if not 0<=length<=32:deny(413,'request_size_limit')
    try:
        async with asyncio.timeout(5):
            data=b''
            async for part in request.stream():
                data+=part
                if len(data)>32:deny(413,'request_size_limit')
        if len(data)!=length or json.loads(data or b'{}')!={}:deny(400,'empty_request_required')
    except (ValueError,TimeoutError):deny(400,'empty_request_required')

def register_document_preview_routes(app,*,previews_for_workspace,owner_for_file=None):
    async def selected(request,file_id,write=False):
        workspace,owner=identity(request,write)
        if owner_for_file is not None:owner=await offload(owner_for_file,request,file_id,write)
        try:previews=await offload(previews_for_workspace,workspace)
        except (ValueError,OSError,sqlite3.Error):deny(503,'document_preview_unavailable')
        if not isinstance(previews,DocumentPreviews):deny(503,'document_preview_unavailable')
        # A host factory cannot accidentally return another company's file store.
        files=workspace.get('files')
        if files is None or previews.files.path.absolute()!=files.path.absolute():deny(503,'document_preview_unavailable')
        return previews,owner

    @app.get('/api/files/{file_id}/page-preview')
    async def metadata(file_id:str,request:Request):
        previews,owner=await selected(request,file_id)
        try:return JSONResponse(await offload(previews.content,file_id,owner=owner),headers=HEADERS)
        except (ValueError,OSError,sqlite3.Error):deny(404,'document_preview_unavailable')

    @app.post('/api/files/{file_id}/preview')
    async def prepare(file_id:str,request:Request):
        previews,owner=await selected(request,file_id,True);await empty_body(request)
        if not previews.enabled:deny(503,'document_preview_unavailable')
        try:await offload(previews.original,file_id,owner)
        except (ValueError,OSError,sqlite3.Error):deny(404,'document_preview_unavailable')
        if not SLOTS.acquire(blocking=False):deny(429,'document_preview_busy')
        task=asyncio.create_task(previews.prepare(file_id,owner=owner))
        async def disconnected():
            while not task.done():
                if await request.is_disconnected():
                    if not task.cancelling():task.cancel()
                    return
                await asyncio.sleep(.1)
        watcher=asyncio.create_task(disconnected())
        try:
            result=await asyncio.shield(task)
            return JSONResponse(result,headers=HEADERS)
        except asyncio.CancelledError:
            if not task.cancelling():task.cancel()
            try:await _drain(task)
            except BaseException:pass
            raise
        except (ValueError,OSError,RuntimeError,sqlite3.Error):deny(503,'document_preview_unavailable')
        finally:
            watcher.cancel()
            try:await _drain(watcher)
            except BaseException:pass
            SLOTS.release()

    @app.get('/api/files/{file_id}/pages/{page}')
    async def page(file_id:str,page:int,request:Request):
        previews,owner=await selected(request,file_id)
        try:body=await offload(previews.page,file_id,page,owner=owner)
        except (ValueError,OSError,sqlite3.Error):deny(404,'document_page_unavailable')
        return Response(body,media_type='image/png',headers={**HEADERS,'Content-Disposition':'inline',
            'X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; sandbox; frame-ancestors 'self'"})
