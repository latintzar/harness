"""Catalog import and expiring upload-only phone handoff HTTP adapters."""
import asyncio
import csv
import io
import json
import re
from fastapi import HTTPException,Request,UploadFile,Form
from fastapi.responses import HTMLResponse,Response,FileResponse
from pydantic import BaseModel,ConfigDict,Field
from chat_permissions import workspace_writable
from catalog_setup import MAX_BYTES


class LocationRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    collection:str
    location_id:str


class ImportRequest(LocationRequest):
    rows:list[dict]=Field(min_length=1,max_length=500)
    source_name:str=Field(min_length=1,max_length=160)
    source_kind:str='catalog_file'


PAGE='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shelf photos · Rocoto</title><link rel="stylesheet" href="/static/harness/theme.css"><link rel="stylesheet" href="/static/retail/catalog-upload.css"></head><body><main><a class="brand" href="/" aria-label="Rocoto"><img src="/static/harness/brand/rocoto-mark.svg" alt="">Rocoto</a><h1>Shelf photos</h1><p>Photograph each shelf clearly, including product names and labels.</p><form id="upload"><label>Choose photos <input id="photos" type="file" accept="image/jpeg,image/png,image/webp" capture="environment" multiple required></label><p>Rocoto’s image service identifies visible products and prepares a provisional catalog from these photos.</p><button>Upload and identify products</button></form><p id="status" role="status">Up to 8 photos · 10 MB each</p></main><script src="/static/retail/catalog-upload.js"></script></body></html>'''
SCRIPT='''const form=document.getElementById('upload'),status=document.getElementById('status'),token=location.hash.slice(1);history.replaceState(null,'',location.pathname);form.addEventListener('submit',async event=>{event.preventDefault();const files=[...document.getElementById('photos').files];if(!/^[a-f0-9]{64}$/.test(token)){status.textContent='Open a fresh upload link from Rocoto.';return;}if(files.length>8||files.some(f=>f.size>10485760)){status.textContent='Choose up to 8 photos, 10 MB each.';return;}form.querySelector('button').disabled=true;try{for(let i=0;i<files.length;i++){status.textContent=`Uploading photo ${i+1} of ${files.length}…`;const body=new FormData();body.append('file',files[i]);body.append('extraction_consent','true');const response=await fetch('/pages/retail/catalog-upload/'+location.pathname.split('/').pop(),{method:'POST',headers:{Authorization:'Bearer '+token,'X-Rocoto-Page':'1'},body});if(!response.ok)throw Error('Open a fresh link or choose a valid shelf photo.');}status.textContent='Photos saved. Continue in Rocoto.';form.reset();}catch(error){status.textContent=error.message;}finally{form.querySelector('button').disabled=false;}});'''


def register_catalog_setup_routes(app,workspace,access):
    from retail_urls import retail_router
    app = retail_router(app)
    def actor(request,write=False):
        selected=request.state.workspace;owner=selected['chats'].principal(request.state.session)
        if not owner or write and not workspace_writable(request.state.session):raise HTTPException(403,'Use your own location workspace.')
        return selected,owner
    def failed(exc):return HTTPException(400,str(exc))
    @app.get('/api/retail/catalog/setup')
    async def setup(request:Request,collection:str,location_id:str):
        selected,owner=actor(request)
        try:return selected['catalog_setup'].overview(owner,collection,location_id)
        except ValueError as exc:raise failed(exc) from exc
    @app.post('/api/retail/catalog/import')
    async def import_catalog(body:ImportRequest,request:Request):
        selected,owner=actor(request,True)
        try:return await asyncio.to_thread(selected['catalog_setup'].import_products,owner,body.collection,body.location_id,body.rows,source_name=body.source_name,source_kind=body.source_kind)
        except ValueError as exc:raise failed(exc) from exc
    @app.post('/api/retail/catalog/import-file')
    async def import_file(request:Request,file:UploadFile,collection:str,location_id:str):
        selected,owner=actor(request,True)
        try:
            payload=await file.read(2*1024*1024+1)
            if len(payload)>2*1024*1024:raise ValueError('Choose a catalog file up to 2 MB.')
            text=payload.decode('utf-8-sig')
            rows=json.loads(text) if (file.filename or '').lower().endswith('.json') else list(csv.DictReader(io.StringIO(text)))
            return await asyncio.to_thread(selected['catalog_setup'].import_products,owner,collection,location_id,rows,source_name=(file.filename or 'Catalog')[:160])
        except (ValueError,UnicodeError,csv.Error) as exc:raise HTTPException(400,'Choose a CSV or JSON catalog with SKU and name columns.') from exc
        finally:await file.close()
    @app.post('/api/retail/catalog/photo-handoff')
    async def handoff(body:LocationRequest,request:Request):
        selected,owner=actor(request,True)
        try:return selected['catalog_setup'].create_handoff(owner,body.collection,body.location_id,origin=str(request.base_url).rstrip('/'))
        except ValueError as exc:raise failed(exc) from exc
    @app.get('/api/retail/catalog/shelf-crops/{key}/{index}/image')
    async def shelf_crop(key:str,index:int,request:Request):
        selected,owner=actor(request)
        from stream_runtime import setup_shelf_catalog
        service=setup_shelf_catalog(selected)
        try:path=service.image(owner,key,index)
        except ValueError as exc:raise HTTPException(404,'Choose a saved shelf product image.') from exc
        def current():
            try:return service.image(owner,key,index)==path
            except ValueError:return False
        request.state.scope_guards.append(current)
        return FileResponse(path,media_type='image/png',headers={'Cache-Control':'private, no-store'})
    @app.get('/api/retail/catalog/photos/{key}/image')
    async def photo(key:str,request:Request):
        selected,owner=actor(request)
        try:value,path=selected['catalog_setup'].photo(owner,key)
        except ValueError as exc:raise HTTPException(404,'Choose a shelf photo in this location.') from exc
        def current():
            try:return selected['catalog_setup'].photo(owner,key)[0]['sha256']==value['sha256']
            except ValueError:return False
        request.state.scope_guards.append(current)
        return FileResponse(path,media_type='image/png',headers={'Cache-Control':'private, no-store'})
    @app.get('/static/retail/catalog-upload.css')
    async def mobile_style():
        return Response('body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 var(--font)}main{max-width:440px;margin:0 auto;padding:32px 24px}.brand{display:flex;align-items:center;gap:8px;color:inherit;text-decoration:none;font-size:24px;font-weight:600}.brand img{width:28px}h1{font-size:30px;letter-spacing:-.8px;margin:32px 0 12px}p{color:var(--muted)}form{display:grid;gap:24px;margin:28px 0}label{display:grid;gap:12px}input{max-width:100%;font:inherit}button{font:inherit;font-weight:600;border:0;border-radius:12px;padding:14px 18px;color:var(--paper);background:var(--ink)}button:disabled{opacity:.5}',media_type='text/css')
    @app.get('/static/retail/catalog-upload.js')
    async def mobile_script():return Response(SCRIPT,media_type='application/javascript',headers={'Cache-Control':'no-store'})
    @app.get('/pages/retail/catalog-upload/{environment}')
    async def mobile_page(environment:str):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',environment):raise HTTPException(404)
        return HTMLResponse(PAGE,headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; img-src 'self'; style-src 'self'; font-src 'self'; script-src 'self'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"})
    @app.post('/pages/retail/catalog-upload/{environment}')
    async def mobile_upload(environment:str,request:Request,file:UploadFile,extraction_consent:bool=Form(False)):
        try:
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',environment):raise ValueError('Use the current upload link.')
            token=request.headers.get('authorization','').removeprefix('Bearer ')
            from catalog_setup import token_hash
            token_hash(token)
            with access.connect() as db:
                if not db.execute('SELECT 1 FROM environments WHERE id=?',(environment,)).fetchone():raise ValueError('Use the current upload link.')
            selected=workspace(environment)
            data=await file.read(MAX_BYTES+1)
            return await asyncio.to_thread(selected['catalog_setup'].upload,token,data,extraction_consent=extraction_consent)
        except ValueError as exc:raise HTTPException(403,'Open a current upload link and choose a valid shelf photo.') from exc
        finally:await file.close()
