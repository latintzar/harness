"""Real HTTP/session/CSRF plus private files; zero media or provider allocation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

HERE=Path(__file__).parents[1]
VIDEO=HERE if (HERE/'server.py').is_file() else Path(os.environ['ROCOTO_SOURCE_TEST_BASE'])
sys.path[:0]=[str(HERE),str(VIDEO),str(VIDEO/'tests')]
import source_routes
from source_routes import register_source_routes, SourceRoutePolicy
from source_intake import SourceStore, IntakeActor, IntakeLimits, IntakeError
from test_server_access import fixture_app
from test_workflow_routes import login
HEADERS={"X-Rocoto-Page":"1"}

READY=SourceRoutePolicy(intake_enabled=True,worker_ready=True)
BODY={'collection':'cafe','filename':'recording.mp4','size':8}
CREATE_HEADERS={**HEADERS,'Idempotency-Key':'first-upload'}
PATCH_HEADERS={**HEADERS,'Content-Type':'application/octet-stream','Upload-Offset':'0'}


def setup(tmp_path,monkeypatch,*,policy=READY,limits=None):
    # This adapter suite supplies its own host policy. The application's actual
    # SourceHost registration is covered by test_source_host_routes.py.
    monkeypatch.setattr(source_routes, 'register_source_routes', lambda *args, **kwargs: None)
    app=fixture_app(tmp_path,monkeypatch)
    stores={};lock=threading.Lock()
    policy_box=[policy]
    def store_for(selected):
        state=Path(selected['state'])
        with lock:
            if state not in stores: stores[state]=SourceStore(state,limits=limits)
            return stores[state]
    register_source_routes(app,store_for_workspace=store_for,
        policy_for_workspace=(lambda _: policy_box[0]) if policy is not None else None)
    return app,stores,policy_box


def begin(client, *, key='first-upload', **body):
    result=client.post('/api/library/imports',json={**BODY,**body},headers={**HEADERS,'Idempotency-Key':key})
    assert result.status_code==201,result.text
    return result.json()['import']


def patch(client,record,data,offset=0,**headers):
    return client.patch(f"/api/library/imports/{record['id']}/content",content=data,
        headers={**PATCH_HEADERS,'Upload-Offset':str(offset),**headers})


def complete(client,record,**body):
    return client.post(f"/api/library/imports/{record['id']}/complete",json=body,headers=HEADERS)


def test_http_chunks_reload_seal_dedupe_and_public_projection(tmp_path,monkeypatch):
    app,stores,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        row=begin(client)
        repeated=begin(client)
        assert repeated==row
        first=patch(client,row,b'abcd')
        assert first.status_code==200 and first.headers['upload-offset']=='4'
        assert patch(client,row,b'abcd').json()==first.json()
        assert patch(client,row,b'xxxx').status_code==409
        assert patch(client,row,b'e',offset=5).status_code==409
        assert complete(client,row).status_code==409
        store=next(iter(stores.values()))
        # Simulate process reload while retaining the authenticated company state.
        stores.clear();reloaded=client.get(f"/api/library/imports/{row['id']}").json()['import']
        assert reloaded['committed_bytes']==4
        assert patch(client,row,b'efgh',offset=4).status_code==200
        receipt=complete(client,row,sha256=hashlib.sha256(b'abcdefgh').hexdigest())
        assert receipt.status_code==200 and receipt.json()['import']['status']=='received'
        assert complete(client,row).json()==receipt.json()
        assert receipt.headers['cache-control']=='private, no-store'
        assert receipt.headers['upload-length']=='8'
        assert not any(value in receipt.text for value in ['token','worker','generation','object_id',str(tmp_path)])
        second=begin(client,key='distinct-occurrence')
        assert second['id']!=row['id'] and second['asset_id']!=row['asset_id']
        assert second['source_id']==row['source_id']
        assert patch(client,second,b'abcdefgh').status_code==200
        assert complete(client,second).status_code==200
        assert len(list(store.blobs.iterdir()))==1
        assert {r['id'] for r in client.get('/api/library/imports').json()['imports']}=={row['id'],second['id']}
        sources=client.get('/api/sources').json()['sources']
        assert len(sources)==1 and sources[0]['kind']=='upload' and sources[0]['status']=='configured'


def test_owner_company_observer_and_browse_boundaries(tmp_path,monkeypatch):
    app,stores,_=setup(tmp_path,monkeypatch)
    app.state.access.create_environment('other-company','Other')
    with TestClient(app) as alice,TestClient(app) as bob,TestClient(app) as owner,TestClient(app) as other:
        assert alice.get('/api/library/imports').status_code==401
        login(alice,app,'alice');login(bob,app,'bob');login(owner,app,'owner',owner=True)
        other.post('/api/session',json={'code':app.state.access.issue('other-company',principal_id='alice')},headers=HEADERS)
        record=begin(alice);path=f"/api/library/imports/{record['id']}"
        for foreign in (bob,other):
            assert foreign.get(path).status_code==404
            assert foreign.get('/api/sources/'+record['source_id']).status_code==404
            assert foreign.get('/api/library/imports').json()['imports']==[]
            assert patch(foreign,record,b'abcd').status_code==404
            assert foreign.delete(path,headers=HEADERS).status_code==404
            assert foreign.post('/api/library/imports',json={**BODY,'source_id':record['source_id']},headers=CREATE_HEADERS).status_code==404
        owner.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS)
        assert owner.get(path).status_code==200
        assert owner.get('/api/sources/'+record['source_id']).status_code==200
        assert owner.get('/api/library/imports').json()['intake']['can_upload'] is False
        assert owner.post('/api/library/imports',json=BODY,headers=CREATE_HEADERS).status_code==403
        assert patch(owner,record,b'abcd').status_code==403
        assert owner.delete(path,headers=HEADERS).status_code==403
        alice.post('/api/session/demo',headers=HEADERS)
        assert alice.get(path).status_code==403 and patch(alice,record,b'abcd').status_code==403


@pytest.mark.parametrize('policy',[None,SourceRoutePolicy(),replace(READY,intake_enabled=False),replace(READY,worker_ready=False)])
def test_uncommissioned_writes_are_disabled_before_store_creation(tmp_path,monkeypatch,policy):
    app,stores,box=setup(tmp_path,monkeypatch,policy=policy)
    with TestClient(app) as client:
        login(client,app,'alice')
        assert client.post('/api/library/imports',json=BODY,headers=CREATE_HEADERS).status_code==503
        assert client.post('/api/sources',json={'collection':'cafe','name':'Uploads'},headers=HEADERS).status_code==503
        assert not stores
        listing=client.get('/api/library/imports').json()
        assert listing['imports']==[] and listing['intake']['available'] is False


def test_paused_pipeline_keeps_existing_reads_and_blocks_all_writes(tmp_path,monkeypatch):
    app,_,box=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice');record=begin(client)
        box[0]=replace(READY,worker_ready=False)
        path=f"/api/library/imports/{record['id']}"
        assert client.get(path).status_code==200
        assert patch(client,record,b'abcd').status_code==503
        assert complete(client,record).status_code==503
        assert client.delete(path,headers=HEADERS).status_code==503


def test_patch_and_post_enforce_csrf_and_same_origin(tmp_path,monkeypatch):
    app,_,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice');record=begin(client)
        assert client.post('/api/library/imports',json=BODY).status_code==403
        url=f"/api/library/imports/{record['id']}/content"
        assert client.patch(url,content=b'abcd',headers={'Upload-Offset':'0','Content-Type':'application/octet-stream'}).status_code==403
        assert patch(client,record,b'abcd',Origin='https://other.invalid').status_code==403
        assert patch(client,record,b'abcd',Origin='http://testserver').status_code==200


def test_collection_and_explicit_empty_company_scope(tmp_path,monkeypatch):
    app,_,box=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        for selected in ('ungranted','company','../state'):
            assert client.post('/api/library/imports',json={**BODY,'collection':selected},headers=CREATE_HEADERS).status_code==404
        app.state.index.status=lambda:{'datasets':[],'ready':False}
        assert client.post('/api/library/imports',json={**BODY,'collection':'company'},headers=CREATE_HEADERS).status_code==404
        box[0]=replace(READY,empty_workspace_collection='company')
        assert begin(client,collection='company')['collection']=='company'
        assert client.post('/api/library/imports',json={**BODY,'collection':'other'},headers=CREATE_HEADERS).status_code==404
        app.state.index.status=lambda:{'datasets':['cafe'],'ready':True}
        assert client.post('/api/library/imports',json={**BODY,'collection':'company'},headers=CREATE_HEADERS).status_code==404


@pytest.mark.parametrize('body',[
    {**BODY,'size':True},{**BODY,'size':'8'},{**BODY,'size':0},{**BODY,'owner':'bob'},
    {**BODY,'state':'/private/path'},{**BODY,'filename':'../secret.mp4'},{**BODY,'filename':'a\\b.mp4'},
    {**BODY,'source_id':'source-forged'},{**BODY,'size':1e300},[BODY],None])
def test_bounded_strict_json_rejects_extra_identity_paths_and_types(tmp_path,monkeypatch,body):
    app,_,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        result=client.post('/api/library/imports',json=body,headers=CREATE_HEADERS)
        assert result.status_code in {400,415}
        assert '/private/path' not in result.text and '../secret.mp4' not in result.text


def test_malformed_duplicate_oversize_json_and_idempotency_conflict(tmp_path,monkeypatch):
    app,_,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        for raw in ('{','{"collection":"cafe","collection":"other"}','{"size":NaN}', '['*1100+'0'+']'*1100):
            assert client.post('/api/library/imports',content=raw,headers={**CREATE_HEADERS,'Content-Type':'application/json'}).status_code==400
        assert client.post('/api/library/imports',content=b'x'*16385,headers={**CREATE_HEADERS,'Content-Type':'application/json'}).status_code==413
        assert client.post('/api/library/imports',json=BODY,headers=HEADERS).status_code==400
        row=begin(client)
        assert client.post('/api/library/imports',json={**BODY,'filename':'different.mp4'},headers=CREATE_HEADERS).status_code==409
        assert client.get('/api/library/imports?owner=bob').status_code==400
        assert client.get('/api/library/imports?limit=201').status_code==400
        assert client.get('/api/library/imports?limit=1&limit=2').status_code==400
        assert client.get('/api/library/imports?limit=1').json()['imports'][0]['id']==row['id']


@pytest.mark.parametrize('headers,expected',[
    ({'Upload-Offset':'-1'},400),({'Upload-Offset':'1.5'},400),({'Upload-Offset':'abc'},400),
    ({'Content-Length':'-1'},400),({'Content-Length':'0'},400),({'Content-Length':'99999999'},413),
    ({'Content-Length':'9'},413),({'Content-Length':'3'},413),({'Content-Length':'5'},400),
    ({'Content-Encoding':'gzip'},415),({'Transfer-Encoding':'chunked'},400),
    ({'Content-Type':'application/json'},415),({'X-Chunk-SHA256':'invalid'},400),
    ({'X-Chunk-SHA256':'0'*64},409)])
def test_chunk_headers_length_checksum_and_no_committed_bytes_on_failure(tmp_path,monkeypatch,headers,expected):
    app,stores,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice');row=begin(client)
        result=patch(client,row,b'abcd',**headers)
        assert result.status_code==expected,result.text
        assert client.get(f"/api/library/imports/{row['id']}").json()['import']['committed_bytes']==0
        assert patch(client,row,b'abcdefgh').status_code==200
        assert complete(client,row).status_code==200


def test_duplicate_required_headers_and_missing_length(tmp_path,monkeypatch):
    app,_,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice');row=begin(client)
        headers=list(PATCH_HEADERS.items())+[('Upload-Offset','0')]
        result=client.patch(f"/api/library/imports/{row['id']}/content",content=b'abcd',headers=headers)
        assert result.status_code==400
        request=client.build_request('PATCH',f"/api/library/imports/{row['id']}/content",content=b'abcd',headers=PATCH_HEADERS)
        del request.headers['content-length']
        assert client.send(request).status_code==411


def test_large_chunk_spools_privately_and_absolute_http_cap(tmp_path,monkeypatch):
    app,stores,_=setup(tmp_path,monkeypatch,limits=IntakeLimits(max_chunk_bytes=16*1024**2))
    opened=[];original=source_routes.tempfile.SpooledTemporaryFile
    def capture(*args,**kwargs):
        result=original(*args,**kwargs);opened.append((result,kwargs));return result
    monkeypatch.setattr(source_routes.tempfile,'SpooledTemporaryFile',capture)
    with TestClient(app) as client:
        login(client,app,'alice');row=begin(client,size=16*1024**2)
        data=b'generated'*12000
        assert patch(client,row,data).status_code==200
        stream,options=opened[0]
        assert stream._rolled and stream.closed
        assert options['max_size']==64*1024 and options['dir']==next(iter(stores.values())).root
        assert patch(client,row,b'x',offset=len(data),**{'Content-Length':str(8*1024**2+1)}).status_code==413
        assert client.get(f"/api/library/imports/{row['id']}").json()['import']['committed_bytes']==len(data)


def test_limits_and_cancel_release_admission_without_worker_calls(tmp_path,monkeypatch):
    limits=IntakeLimits(max_file_bytes=16,max_storage_bytes=16,max_chunk_bytes=8,max_pending_uploads=1)
    app,stores,_=setup(tmp_path,monkeypatch,limits=limits)
    with TestClient(app) as client:
        login(client,app,'alice')
        assert client.post('/api/library/imports',json={**BODY,'size':17},headers=CREATE_HEADERS).status_code==413
        row=begin(client)
        assert client.post('/api/library/imports',json=BODY,headers={**HEADERS,'Idempotency-Key':'second'}).status_code==429
        path=f"/api/library/imports/{row['id']}"
        assert client.delete(path,headers=HEADERS).json()['import']['status']=='cancelled'
        assert client.delete(path,headers=HEADERS).json()['import']['status']=='cancelled'
        second=begin(client,key='second',size=16)
        assert not (next(iter(stores.values())).quarantine/row['id']).exists()
        assert second['status']=='uploading'


def test_status_tracks_actual_jobs_and_active_cancel_retains_reconciliation(tmp_path,monkeypatch):
    app,stores,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice');row=begin(client)
        assert patch(client,row,b'abcdefgh').status_code==200
        assert complete(client,row).json()['import']['status']=='received'
        store=next(iter(stores.values()));path=f"/api/library/imports/{row['id']}"
        prepare=store.claim_job(worker_id='synthetic-preparer')
        assert client.get(path).json()['import']['status']=='preparing'
        store.finish_job(prepare,result={'private_path':'/private/prepared','synthetic':True})
        embed=store.claim_job(worker_id='synthetic-embedder')
        assert client.get(path).json()['import']['status']=='indexing'
        stopped=client.delete(path,headers=HEADERS)
        assert stopped.json()['import']['status']=='cancelling'
        assert '/private/prepared' not in stopped.text and embed.token not in stopped.text and embed.job_id not in stopped.text
        assert client.delete(path,headers=HEADERS).json()==stopped.json()
        with pytest.raises(IntakeError,match='lease'):
            store.finish_job(embed,result={'synthetic':True})
        job=store.get_asset(IntakeActor('alice'),row['asset_id'])['jobs'][-1]
        store.reconcile_job(job['id'],expected_generation=job['generation'],outcome='cancelled',evidence_reference='synthetic-stop-confirmation')
        assert client.get(path).json()['import']['status']=='cancelled'


def test_ready_requires_publish_and_queued_cancel_cannot_be_claimed(tmp_path,monkeypatch):
    app,stores,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice');row=begin(client)
        patch(client,row,b'abcdefgh');complete(client,row)
        store=next(iter(stores.values()));path=f"/api/library/imports/{row['id']}"
        for stage in ['prepare','embed','publish']:
            lease=store.claim_job(worker_id='synthetic-'+stage)
            assert lease.stage==stage
            assert client.get(path).json()['import']['status']!='ready'
            store.finish_job(lease,result={'generation_id':'synthetic-verified-commit'} if stage=='publish' else {'synthetic':True})
        assert client.get(path).json()['import']['status']=='ready'
        assert client.delete(path,headers=HEADERS).status_code==409
        row=begin(client,key='queued');patch(client,row,b'abcdefgh');complete(client,row)
        assert client.delete(f"/api/library/imports/{row['id']}",headers=HEADERS).json()['import']['status']=='cancelled'
        assert store.claim_job(worker_id='synthetic') is None


def test_manual_sources_are_private_and_remote_setup_is_not_faked(tmp_path,monkeypatch):
    app,_,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        result=client.post('/api/sources',json={'collection':'cafe','name':'Front recordings'},headers=HEADERS)
        assert result.status_code==201 and result.json()['status']=='configured'
        assert begin(client,source_id=result.json()['id'])['source_id']==result.json()['id']
        for body in ({'collection':'cafe','name':'Remote','kind':'sftp'},
                     {'collection':'cafe','name':'Remote','path':'/host/private'}):
            assert client.post('/api/sources',json=body,headers=HEADERS).status_code==400


def test_default_source_creation_is_atomic_across_store_instances(tmp_path):
    actor=IntakeActor('alice');store=SourceStore(tmp_path)
    def create(_): return SourceStore(tmp_path).ensure_upload_source(actor,collection='cafe')
    with ThreadPoolExecutor(max_workers=6) as pool: records=list(pool.map(create,range(12)))
    assert len({r['id'] for r in records})==1
    assert len(store.list_sources(actor))==1
    assert store.ensure_upload_source(IntakeActor('bob'),collection='cafe')['id']!=records[0]['id']
    with pytest.raises(IntakeError,match='read_only'):
        store.ensure_upload_source(IntakeActor('alice',read_only=True),collection='cafe')


def test_stream_deadline_and_four_receivers_leave_offset_recoverable(tmp_path,monkeypatch):
    app,stores,_=setup(tmp_path,monkeypatch,policy=replace(READY,body_timeout_seconds=.15))
    async def scenario():
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url='http://testserver') as client:
            await client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='alice')},headers=HEADERS)
            opened=await client.post('/api/library/imports',json={**BODY,'size':1},headers=CREATE_HEADERS)
            row=opened.json()['import'];url=f"/api/library/imports/{row['id']}/content"
            active=0;started=asyncio.Event();release=asyncio.Event()
            async def delayed():
                nonlocal active
                active+=1
                if active==4: started.set()
                await release.wait()
                yield b'x'
            headers={**PATCH_HEADERS,'Content-Length':'1'}
            tasks=[asyncio.create_task(client.patch(url,content=delayed(),headers=headers)) for _ in range(4)]
            await asyncio.wait_for(started.wait(),2)
            rejected=await client.patch(url,content=b'x',headers=headers)
            assert rejected.status_code==429
            results=await asyncio.gather(*tasks)
            assert all(result.status_code==408 for result in results)
            detail=await client.get(f"/api/library/imports/{row['id']}")
            assert detail.json()['import']['committed_bytes']==0
            assert (await client.patch(url,content=b'x',headers=headers)).status_code==200
    asyncio.run(scenario())


def test_import_stage_projection_follows_jobs_without_exposing_private_receipts(tmp_path,monkeypatch):
    app,stores,_=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice');row=begin(client)
        path=f"/api/library/imports/{row['id']}"
        assert client.get(path).json()['import']['stage'] is None
        patch(client,row,b'abcdefgh');complete(client,row)
        store=next(iter(stores.values()))
        for stage in ['prepare','embed','publish']:
            pending=client.get(path).json()['import']
            assert pending['stage']==stage and pending['stage_status']=='queued'
            lease=store.claim_job(worker_id='fixture-'+stage)
            active=client.get(path).json()['import']
            assert active['stage']==stage and active['stage_status']=='running'
            assert lease.token not in str(active) and lease.job_id not in str(active)
            store.finish_job(lease,result={'generation_id':'synthetic-verified-commit'} if stage=='publish' else {'private_path':'/private/derivative'})
        ready=client.get(path).json()['import']
        assert ready['status']=='ready' and ready['stage'] is None and ready['stage_status'] is None



def test_import_intake_projects_only_host_allowed_empty_company_default(tmp_path,monkeypatch):
    app,_,box=setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        app.state.index.status=lambda:{'datasets':['cafe'],'ready':True}
        box[0]=replace(READY,empty_workspace_collection='company')
        normal=client.get('/api/library/imports').json()['intake']
        assert normal['collections']==['cafe'] and normal['default_collection'] is None
        app.state.index.status=lambda:{'datasets':[],'ready':False}
        empty=client.get('/api/library/imports').json()['intake']
        assert empty['collections']==['company'] and empty['default_collection']=='company'
