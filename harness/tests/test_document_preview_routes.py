from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
import httpx
from fastapi import FastAPI,Request
import pytest
from document_preview_routes import register_document_preview_routes
from test_document_previews import setup

@pytest.fixture
def anyio_backend():return 'asyncio'

@asynccontextmanager
async def fixture(tmp_path):
    async with setup(tmp_path) as (files,record,backend,broker,previews):
        app=FastAPI();state={'session':{'authenticated':True,'can_query':True,'role':'owner',
            'principal':{'id':'alice'},'environment':{'id':'company'}},'workspace':{'files':files}}
        @app.middleware('http')
        async def context(request:Request,call_next):
            request.state.session=deepcopy(state['session']);request.state.workspace=state['workspace'];return await call_next(request)
        register_document_preview_routes(app,previews_for_workspace=lambda selected:previews)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://rocoto.example') as client:
            yield client,state,files,record,backend,previews

@pytest.mark.anyio
async def test_private_owner_routes_and_png_headers(tmp_path):
    async with fixture(tmp_path) as (client,state,files,record,backend,previews):
        url='/api/files/'+record['id'];metadata=await client.get(url+'/page-preview')
        assert metadata.status_code==200 and metadata.json()['preview_status']=='available'
        response=await client.post(url+'/preview',json={},headers={'x-rocoto-page':'1','origin':'https://rocoto.example'})
        assert response.status_code==200 and response.json()['preview_status']=='ready'
        page=await client.get(url+'/pages/1');assert page.status_code==200 and page.content.startswith(b'\x89PNG')
        assert page.headers['content-type']=='image/png' and page.headers['cache-control']=='private, no-store'
        assert 'sandbox' in page.headers['content-security-policy']
        assert (await client.get(url+'/pages/2')).status_code==404
        state['session']['principal']['id']='bob'
        assert (await client.get(url+'/page-preview')).status_code==404
        assert (await client.get(url+'/pages/1')).status_code==404
        assert (await client.post(url+'/preview',json={},headers={'x-rocoto-page':'1'})).status_code==404

@pytest.mark.anyio
async def test_observer_cached_reads_and_mutation_denial(tmp_path):
    async with fixture(tmp_path) as (client,state,files,record,backend,previews):
        url='/api/files/'+record['id'];await previews.prepare(record['id'],owner='alice')
        state['session']['principal']['id']='operator';state['session']['viewing_as']={'principal':{'id':'alice'},'environment':{'id':'company'}}
        assert (await client.get(url+'/page-preview')).status_code==200
        assert (await client.get(url+'/pages/1')).status_code==200
        assert (await client.post(url+'/preview',json={},headers={'x-rocoto-page':'1'})).status_code==403
        state['session']['viewing_as']['environment']['id']='other'
        assert (await client.get(url+'/pages/1')).status_code==403

@pytest.mark.anyio
async def test_closed_csrf_origin_body_and_browse_gates(tmp_path):
    async with fixture(tmp_path) as (client,state,files,record,backend,previews):
        url='/api/files/'+record['id']+'/preview'
        assert (await client.post(url,json={})).status_code==403
        assert (await client.post(url,json={},headers={'x-rocoto-page':'1','origin':'https://evil.example'})).status_code==403
        secret='synthetic-private-document-secret'
        response=await client.post(url,json={'value':secret},headers={'x-rocoto-page':'1'})
        assert response.status_code in {400,413} and secret not in response.text
        previews.enabled=False
        assert (await client.post(url,json={},headers={'x-rocoto-page':'1'})).status_code==503
        state['session']['can_query']=False
        assert (await client.get('/api/files/'+record['id']+'/page-preview')).status_code==403
        state['session']['authenticated']=False
        assert (await client.post(url,json={},headers={'x-rocoto-page':'1'})).status_code==401
        assert backend.start_count==0

@pytest.mark.anyio
async def test_wrong_company_store_and_revoked_source(tmp_path):
    async with fixture(tmp_path) as (client,state,files,record,backend,previews):
        from chat_files import ChatFiles
        url='/api/files/'+record['id'];await previews.prepare(record['id'],owner='alice')
        state['workspace']['files']=ChatFiles(tmp_path/'other-company')
        assert (await client.get(url+'/pages/1')).status_code==503
        state['workspace']['files']=files
        with files.connect() as db:db.execute('DELETE FROM files WHERE id=?',(record['id'],))
        assert (await client.get(url+'/pages/1')).status_code==404

@pytest.mark.anyio
@pytest.mark.parametrize('disconnect',[False,True])
async def test_repeated_http_cancellation_holds_slot_until_worker_cleanup(tmp_path,monkeypatch,disconnect):
    import asyncio,threading
    import document_preview_routes as routes
    async with setup(tmp_path) as (files,record,backend,broker,previews):
        app=FastAPI();register_document_preview_routes(app,previews_for_workspace=lambda _:previews)
        endpoint=next(route.endpoint for route in app.routes if getattr(route,'methods',set())=={'POST'})
        slots=threading.BoundedSemaphore(1);monkeypatch.setattr(routes,'SLOTS',slots)
        started=asyncio.Event();cleanup=asyncio.Event();release=asyncio.Event();ended=asyncio.Event();gone=False
        async def preparing(*args,**kwargs):
            started.set()
            try:await asyncio.Event().wait()
            finally:
                cleanup.set();await release.wait();ended.set()
        monkeypatch.setattr(previews,'prepare',preparing)
        request=Request({'type':'http','method':'POST','path':'/','query_string':b'',
            'headers':[(b'host',b'rocoto.example'),(b'x-rocoto-page',b'1'),(b'content-length',b'2')],
            'state':{'session':{'authenticated':True,'can_query':True,'role':'owner','principal':{'id':'alice'},'environment':{'id':'company'}},'workspace':{'files':files}}})
        async def stream():yield b'{}'
        async def disconnected():return gone
        request.stream=stream;request.is_disconnected=disconnected
        task=asyncio.create_task(endpoint(record['id'],request));await started.wait()
        if disconnect:gone=True
        else:task.cancel()
        await asyncio.wait_for(cleanup.wait(),1)
        for _ in range(3):
            task.cancel();await asyncio.sleep(.01)
            assert not task.done() and not ended.is_set()
            assert not slots.acquire(blocking=False)
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert ended.is_set() and slots.acquire(blocking=False)
        slots.release()


def test_actual_server_owns_preview_store_and_keeps_uncommissioned_renderer_off(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    monkeypatch.delenv('ROCOTO_DOCUMENT_PREVIEWS_ENABLED',raising=False)
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        session=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()
        owner=session['principal']['id'];files=app.state.agent.file_store
        record=files.create({'name':'Review.docx','content':'# Review\n\nExact original.'},owner_principal_id=owner,chat_id='preview-chat',dataset='cafe')
        root='/api/files/'+record['id']
        response=client.get(root+'/page-preview')
        assert response.status_code==200 and response.json()['preview_status']=='unavailable'
        assert response.headers['cache-control']=='private, no-store'
        assert client.post(root+'/preview',json={},headers=HEADERS).status_code==503
        assert client.get(root+'/download').content==files.download(record['id'],owner_principal_id=owner)[1]
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        assert client.get(root+'/page-preview').status_code==404
