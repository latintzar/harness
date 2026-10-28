"""Real private ledger/store with explicit synthetic runtime adapters; zero providers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE=Path(__file__).parents[1];BASE=Path(os.environ.get('ROCOTO_SOURCE_TEST_BASE',HERE))
sys.path[:0]=[str(HERE),str(BASE),str(BASE/'tests')]
import source_service as service
from source_intake import IntakeActor,IntakeError,SourceStore
from source_worker import SourceWorker,WorkerLimits,WorkerError
from source_routes import register_source_routes
from fastapi.testclient import TestClient
from test_server_access import fixture_app
from test_workflow_routes import login,HEADERS
spec=importlib.util.spec_from_file_location('source_worker_fixture',BASE/'tests/test_source_worker.py')
worker_fixture=importlib.util.module_from_spec(spec);sys.modules[spec.name]=worker_fixture;spec.loader.exec_module(worker_fixture)
OWNER=IntakeActor('alice')

@pytest.fixture
def anyio_backend():return 'asyncio'

def write(path,value):
    path.write_text(json.dumps(value));path.chmod(0o600)


def setup(tmp_path,*,enabled=True,clock=None):
    state=tmp_path/'company';state.mkdir(mode=0o700)
    compute=state/'compute';compute.mkdir(mode=0o700)
    now=datetime.now(timezone.utc);approval=compute/'embedding.json'
    write(approval,{'schema_version':1,'approval_id':'source-service-fixture','purpose':service.PURPOSE,
        'provider':'modal','workspace':service.WORKSPACE,'verified_by':service.compute_guard.OWNER,
        'verification_source':'https://modal.com/settings/usage','user_verified':True,
        'verified_at':now.isoformat(),'expires_at':(now+timedelta(hours=1)).isoformat(),
        'net_spend_limit_usd':'0','credit_remaining_usd':'20','reserved_credit_usd':'10','approved_run_gross_usd':'1.00'})
    config={'schema_version':1,'enabled':enabled,'company_id':'rocoto','principal_id':'alice','collection':'cafe',
        'approval_path':str(approval),'approval_sha256':hashlib.sha256(approval.read_bytes()).hexdigest(),
        'receipt_sha256':'a'*64,'modal_python':'/fixture/python','modal_config':'/fixture/modal.toml'}
    write(compute/'source-intake-service.json',config)
    host=service.SourceHost(state,company_id='rocoto',**({'clock':clock} if clock else {}));store=SourceStore(state,**({'clock':clock} if clock else {}))
    health(host)
    return host,store,config


def health(host,*,ready=True):
    config=host.configuration();now=host.clock()
    write(host.root/'heartbeat.json',{'schema_version':1,'company_id':host.company,'ready':ready,
        'config_sha256':config.fingerprint,'code_sha256':host.code_sha256,
        'checked_at':now,'expires_at':now+service.HEARTBEAT_TTL})


def begin(host,store,key='browser-one',**kwargs):
    return host.begin(store,OWNER,collection='cafe',filename='camera.mp4',size=8,idempotency_key=key,**kwargs)


def seal(store,row):
    store.append_chunk(OWNER,row['id'],offset=0,data=b'abcdefgh')
    store.seal_upload(OWNER,row['id'])


async def probe():return {'media':True,'derivatives':True,'qdrant':True,'embedding_runtime':True,'commission_proof_sha256':'b'*64}


def runtime(host,store):
    media=worker_fixture.Media();embedding=worker_fixture.Embedding(store);api=worker_fixture.FakeQdrant()
    publisher=worker_fixture.worker.IndexPublisher(host.state/'index',resolver_ready=lambda:True,client=api)
    worker=SourceWorker(store,company_id='rocoto',media_client=media,media_image=service.IMAGE,
        embedding_adapter=embedding,publisher=publisher,enabled=True,scope_factory=worker_fixture.Scope,
        limits=WorkerLimits(heartbeat_seconds=.05,poll_seconds=.02,embedding_batch_clips=48,
            embedding_import_clips=48,embedding_import_bytes=64*1024**2))
    return service.SourceService(host,store,worker,probe,sleep_seconds=.01),media,embedding


def test_default_host_and_disabled_config_never_admit(tmp_path):
    state=tmp_path/'unconfigured';state.mkdir(mode=0o700)
    host=service.SourceHost(state,company_id='rocoto')
    assert host.policy().intake_enabled is False and host.capabilities(OWNER)['can_upload'] is False
    other=tmp_path/'configured';other.mkdir();host,store,_=setup(other,enabled=False)
    with pytest.raises(IntakeError,match='unavailable'):begin(host,store)
    assert not host.records()


def test_readiness_binds_actual_config_and_expires(tmp_path):
    now=[100.];host,store,config=setup(tmp_path,clock=lambda:now[0])
    assert host.healthy() and host.capabilities(OWNER)['can_upload']
    now[0]+=16;assert not host.healthy()
    with pytest.raises(IntakeError,match='unavailable'):begin(host,store)
    health(host);config['max_file_bytes']=16;write(host.state/'compute/source-intake-service.json',config)
    assert not host.healthy()
    health(host);assert host.healthy()


def test_legacy_config_only_heartbeat_requires_worker_restart(tmp_path):
    host,store,_=setup(tmp_path)
    value=json.loads((host.root/'heartbeat.json').read_text())
    value.pop('code_sha256',None);write(host.root/'heartbeat.json',value)
    assert not host.healthy()
    with pytest.raises(IntakeError,match='unavailable'):begin(host,store)
    assert not host.records()


@pytest.mark.anyio
@pytest.mark.parametrize('completed_stages',[0,1,2])
async def test_worker_source_change_stops_before_claiming_next_stage(tmp_path,monkeypatch,completed_stages):
    host,store,_=setup(tmp_path);row=begin(host,store);seal(store,row)
    target,media,embedding=runtime(host,store);target.ready=True
    for _ in range(completed_stages):assert (await target.step())['status']=='succeeded'
    before=store.get_asset(OWNER,row['asset_id'])
    calls=(len(media.submissions),len(embedding.admitted))
    monkeypatch.setattr(service,'code_identity',lambda:'f'*64,raising=False)
    assert await target.step() is None
    assert not host.healthy() and calls==(len(media.submissions),len(embedding.admitted))
    assert store.get_asset(OWNER,row['asset_id'])==before


def test_new_host_rejects_old_worker_even_when_config_matches(tmp_path,monkeypatch):
    host,store,_=setup(tmp_path);target,_,_=runtime(host,store)
    target.ready=True;target.recovering=False;target.write_health()
    assert host.healthy()
    monkeypatch.setattr(service,'LOADED_CODE_SHA256','f'*64,raising=False)
    monkeypatch.setattr(service,'code_identity',lambda:'f'*64,raising=False)
    new_host=service.SourceHost(host.state,company_id=host.company)
    target.write_health()
    assert not new_host.healthy()
    assert json.loads((host.root/'heartbeat.json').read_text())['code_sha256']==host.code_sha256


@pytest.mark.parametrize('changed',['source_worker','source_embedding','modal_embed','compute_guard',
    'cosmos_retrieval','qdrant_retrieval','source_media','workspace_identity'])
def test_worker_identity_covers_admission_media_and_publication_sources(tmp_path,monkeypatch,changed):
    monkeypatch.setattr(service,'__file__',str(tmp_path/'source_service.py'))
    before=service.code_identity()
    (tmp_path/(changed+'.py')).write_text('# changed worker dependency\n')
    assert service.code_identity()!=before


def test_one_allowance_one_import_duplicate_replies_and_private_continuation(tmp_path):
    host,store,_=setup(tmp_path);row=begin(host,store)
    assert begin(host,store)==row
    assert not host.capabilities(OWNER)['can_upload'] and host.capabilities(OWNER)['can_manage_imports']
    with pytest.raises(IntakeError,match='reserved'):begin(host,store,key='second')
    assert len(store.list_imports(OWNER))==1
    # Existing uploads remain resumable after budget claim and worker outage.
    service.compute_guard.claim_approval(host.approval(host.configuration()));health(host,ready=False)
    assert begin(host,store)==row
    host.authorize(OWNER,store.get_import(OWNER,row['id']),operation='content');seal(store,row)
    host.authorize(OWNER,store.get_import(OWNER,row['id']),operation='cancel')
    assert store.cancel_import(OWNER,row['id'])['asset']['status']=='cancelled'


def test_admission_is_atomic_between_http_processes(tmp_path):
    host,store,_=setup(tmp_path)
    other=service.SourceHost(host.state,company_id='rocoto')
    def call(key):
        try:return begin(other,store,key=key)['id']
        except IntakeError as error:return str(error)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(call,['one','two']))
    assert sum(value.startswith('upload-') for value in results)==1
    assert 'embedding_allowance_reserved' in results


def test_recover_slot_after_lost_store_receipt_returns_same_import(tmp_path,monkeypatch):
    host,store,_=setup(tmp_path);save=host.save
    def interrupted(record):
        if record.get('upload_id'):raise OSError('synthetic lost receipt')
        save(record)
    monkeypatch.setattr(host,'save',interrupted)
    with pytest.raises(OSError):begin(host,store)
    assert len(store.list_imports(OWNER))==1
    restored=service.SourceHost(host.state,company_id='rocoto');restored.recover_admissions(store)
    row=begin(restored,store)
    assert row['id']==store.list_imports(OWNER)[0]['upload']['id'] and len(restored.records())==1


def test_invalid_filename_or_source_does_not_consume_allowance(tmp_path):
    host,store,_=setup(tmp_path)
    with pytest.raises(IntakeError):host.begin(store,OWNER,collection='cafe',filename='../bad.mp4',size=8,idempotency_key='bad')
    assert not host.records()
    with pytest.raises(IntakeError):begin(host,store,source_id='source-'+'e'*32)
    assert not host.records();assert begin(host,store)['status']=='uploading'


def test_wrong_principal_collection_readonly_and_changed_approval_are_closed(tmp_path):
    host,store,config=setup(tmp_path)
    for actor,collection in [(IntakeActor('bob'),'cafe'),(IntakeActor('alice',True),'cafe'),(OWNER,'warehouse')]:
        with pytest.raises(IntakeError):host.begin(store,actor,collection=collection,filename='camera.mp4',size=8,idempotency_key='one')
    assert not host.records()
    Path(config['approval_path']).write_text('{}')
    assert not host.capabilities(OWNER)['can_upload']
    with pytest.raises(IntakeError,match='allowance_required'):begin(host,store)


def test_host_json_has_no_provider_activity_or_private_receipt_projection(tmp_path):
    host,store,_=setup(tmp_path);begin(host,store)
    caps=host.capabilities(OWNER)
    assert set(caps)=={'can_upload','can_manage_imports','notice'}
    assert 'approval_path' not in json.dumps(caps) and 'modal' not in json.dumps(caps)
    assert host.capabilities(IntakeActor('bob'))=={'can_upload':False,'can_manage_imports':False}


@pytest.mark.anyio
async def test_service_only_claims_bound_asset_and_publishes_three_stages(tmp_path):
    host,store,_=setup(tmp_path);unapproved=worker_fixture.new_asset(store,key='older-unapproved')
    row=begin(host,store);seal(store,row);target,media,embedding=runtime(host,store)
    target.ready=True
    assert (await target.step())['stage']=='prepare'
    assert (await target.step())['stage']=='embed'
    assert (await target.step())['stage']=='publish'
    assert store.get_asset(OWNER,row['asset_id'])['status']=='ready'
    assert store.get_asset(OWNER,unapproved['asset_id'])['status']=='received'
    assert len(media.submissions)==len(embedding.called)==1


@pytest.mark.anyio
async def test_import_scope_limit_rejects_before_any_embedding_admission(tmp_path,monkeypatch):
    host,store,_=setup(tmp_path);row=begin(host,store);seal(store,row);target,media,embedding=runtime(host,store)
    target.ready=True;assert (await target.step())['stage']=='prepare'
    original=target.worker.prepared
    def too_many(context):
        path,manifest,receipt=original(context);manifest['clips']=[dict(manifest['clips'][0]) for _ in range(49)];return path,manifest,receipt
    monkeypatch.setattr(target.worker,'prepared',too_many)
    result=await target.step()
    assert result['error']=='embedding_authorized_scope_exceeded'
    assert not embedding.admitted and not embedding.called


@pytest.mark.anyio
async def test_recovery_waits_for_lease_fence_then_confirms_stop(tmp_path):
    now=[100.];host,store,_=setup(tmp_path,clock=lambda:now[0]);row=begin(host,store);seal(store,row)
    target,media,embedding=runtime(host,store);lease=store.claim_job(worker_id='dead-worker',asset_id=row['asset_id'])
    scope=service.MediaScope(company_id='rocoto',owner_id='alice',asset_id=row['asset_id'],job_id=lease.job_id)
    store.checkpoint_job(lease,checkpoint={'phase':'media_submit_intent','scope':scope.__dict__,'generation':lease.generation})
    assert await target.reconcile() is False
    now[0]+=61
    assert await target.reconcile() is True
    assert store.get_asset(OWNER,row['asset_id'])['jobs'][0]['status']=='failed'
    assert not media.submissions and not embedding.called


@pytest.mark.anyio
async def test_cancel_reconciliation_releases_only_confirmed_owned_media(tmp_path):
    host,store,_=setup(tmp_path);row=begin(host,store);seal(store,row);target,media,_=runtime(host,store)
    lease=store.claim_job(worker_id='old',asset_id=row['asset_id']);scope=service.MediaScope(company_id='rocoto',owner_id='alice',asset_id=row['asset_id'],job_id=lease.job_id)
    handle=worker_fixture.Lease(lease.job_id,lease.generation,'private-token');media.results[handle]=({}, {})
    store.checkpoint_job(lease,checkpoint={'phase':'media_running','scope':scope.__dict__,'generation':lease.generation})
    store.cancel_import(OWNER,row['id'])
    assert await target.reconcile() is True
    assert media.stops==media.releases==1
    assert store.get_asset(OWNER,row['asset_id'])['status']=='cancelled'


@pytest.mark.anyio
async def test_cleanup_uncertainty_keeps_hold_and_worker_unready(tmp_path):
    host,store,_=setup(tmp_path);row=begin(host,store);seal(store,row);target,media,_=runtime(host,store)
    lease=store.claim_job(worker_id='old',asset_id=row['asset_id']);scope=service.MediaScope(company_id='rocoto',owner_id='alice',asset_id=row['asset_id'],job_id=lease.job_id)
    handle=worker_fixture.Lease(lease.job_id,lease.generation,'private-token');media.results[handle]=({}, {})
    async def uncertain(*args):return {'cleanup_confirmed':False}
    media.stop=uncertain
    store.checkpoint_job(lease,checkpoint={'phase':'media_running','scope':scope.__dict__,'generation':lease.generation})
    store.cancel_import(OWNER,row['id'])
    assert await target.reconcile() is False
    assert store.get_asset(OWNER,row['asset_id'])['jobs'][0]['status']=='needs_reconciliation'
    assert media.releases==0 and host.records()


@pytest.mark.anyio
async def test_daemon_shutdown_drains_active_preparation_and_closes_gate(tmp_path):
    host,store,_=setup(tmp_path);row=begin(host,store);seal(store,row);target,media,_=runtime(host,store)
    media.hold=True
    task=asyncio.create_task(target.run());await asyncio.wait_for(media.submitted.wait(),2)
    target.stopping.set();await asyncio.wait_for(task,3)
    assert media.stops>=1 and not host.healthy()
    assert store.get_asset(OWNER,row['asset_id'])['jobs'][0]['status'] in {'failed','cancelled'}


def test_asset_filter_validates_scope_and_never_claims_other_queued_asset(tmp_path):
    host,store,_=setup(tmp_path);older=worker_fixture.new_asset(store,key='unapproved');row=begin(host,store);seal(store,row)
    lease=store.claim_job(worker_id='authorized',asset_id=row['asset_id'])
    assert lease.asset_id==row['asset_id']
    assert store.get_asset(OWNER,older['asset_id'])['jobs'][0]['status']=='queued'
    with pytest.raises(IntakeError):store.claim_job(worker_id='bad',asset_id='../asset')


def test_http_gate_closes_new_admission_but_preserves_replay_resume_and_stop(tmp_path,monkeypatch):
    host,store,_=setup(tmp_path);(tmp_path/'http').mkdir()
    import server
    monkeypatch.setattr(server,'register_source_routes',lambda *args,**kwargs:None,raising=False)
    app=fixture_app(tmp_path/'http',monkeypatch)
    register_source_routes(app,store_for_workspace=lambda _:store,policy_for_workspace=lambda _:host.policy(),
        admission_for_workspace=lambda _:host)
    body={'collection':'cafe','filename':'camera.mp4','size':8};headers={**HEADERS,'Idempotency-Key':'actual-browser'}
    with TestClient(app) as client:
        login(client,app,'alice');initial=client.get('/api/library/imports').json()['intake'];assert initial['can_upload']
        response=client.post('/api/library/imports',json=body,headers=headers);assert response.status_code==201,response.text
        row=response.json()['import'];path='/api/library/imports/'+row['id']
        cap=client.get('/api/library/imports').json()['intake'];assert not cap['can_upload'] and cap['can_manage_imports']
        assert client.post('/api/library/imports',json=body,headers={**headers,'Idempotency-Key':'second'}).status_code==409
        health(host,ready=False)
        cap=client.get('/api/library/imports').json()['intake'];assert not cap['available'] and cap['can_manage_imports']
        assert client.post('/api/library/imports',json=body,headers=headers).json()['import']==row
        assert client.post('/api/library/imports',json=body,headers={**headers,'Idempotency-Key':'new'}).status_code==503
        assert client.patch(path+'/content',content=b'abcdefgh',headers={**HEADERS,'Content-Type':'application/octet-stream','Upload-Offset':'0'}).status_code==200
        assert client.post(path+'/complete',json={},headers=HEADERS).status_code==200
        assert client.delete(path,headers=HEADERS).json()['import']['status']=='cancelled'
        login(client,app,'bob');assert client.get(path).status_code==404


@pytest.mark.anyio
@pytest.mark.parametrize('new_closure',['legacy','mounts','documents'])
async def test_health_probe_checks_actual_root_receipt_sources_broker_and_qdrant(tmp_path,monkeypatch,new_closure):
    host,store,config=setup(tmp_path);root=tmp_path/'installed';root.mkdir()
    sources=[]
    for name in ['source_media.py','native_executor.py','broker_service.py']+(['native_package_mount.py'] if new_closure!='legacy' else [])+(['document_preview_contract.py'] if new_closure=='documents' else []):
        raw=('published fixture '+name).encode();(root/name).write_bytes(raw);(root/name).chmod(0o644)
        sources.append({'path':name,'sha256':hashlib.sha256(raw).hexdigest()})
    profiles=[{'purpose':'prepare','profile':service.PROFILE,'recipe':'rocoto-cfr24-h264-segments-v2','image_digest':service.IMAGE}]
    profiles.extend({'purpose':purpose,'profile':service.DERIVE_PROFILE,'recipe':'rocoto-source-derive-v1','image_digest':service.DERIVE_IMAGE}
        for purpose in ['detail_video_v1','inspect_frames_v1','source_cut_v1'])
    receipt=root/'readiness-receipt.json';receipt.write_text(json.dumps({'schema_version':1,'profiles':profiles,'sources':sources,
        'preview_uid':os.geteuid(),'preview_gid':os.getegid(),'installed_proof_sha256':'b'*64}));receipt.chmod(0o644)
    config['receipt_sha256']=hashlib.sha256(receipt.read_bytes()).hexdigest();write(host.state/'compute/source-intake-service.json',config)
    monkeypatch.setattr(service,'runtime',lambda *args:None)
    class Media:
        calls=0
        async def recover(self,scope,generation):self.calls+=1;assert scope.company_id=='rocoto';assert generation==1;return None
    class Qdrant:
        def request(self,method,path):assert (method,path)==('GET','/collections');return {'collections':[]}
    if os.geteuid()!=0:
        with pytest.raises(IntakeError,match='invalid_commission'):service.root_bytes(receipt)
    media=Media();probe=service.HealthProbe(host,host.configuration(),media,Qdrant(),receipt_path=receipt,
        root_reader=lambda path:service.root_bytes(path,expected_uid=os.geteuid()))
    assert (await probe())['commission_proof_sha256']=='b'*64 and media.calls==1
    (root/('document_preview_contract.py' if new_closure=='documents' else 'native_package_mount.py' if new_closure=='mounts' else 'source_media.py')).write_text('changed deployed code')
    with pytest.raises(IntakeError,match='source_changed'):await probe()
    assert media.calls==1
    receipt.chmod(0o666)
    with pytest.raises(IntakeError,match='invalid_commission'):await probe()


@pytest.mark.anyio
async def test_shutdown_ready_pulse_and_single_process_lock_are_real(tmp_path):
    host,store,_=setup(tmp_path);target,_,_=runtime(host,store)
    with service.lock(host.root/'service.lock',exclusive=True):
        with pytest.raises(BlockingIOError):await target.run()
    task=asyncio.create_task(target.run())
    for _ in range(100):
        if host.healthy():break
        await asyncio.sleep(.01)
    target.stopping.set();await task
    assert not host.healthy() and json.loads((host.root/'heartbeat.json').read_text())['ready'] is False


@pytest.mark.parametrize('key',[None,'',' ','x'*161,'bad\nkey',False])
def test_invalid_admission_key_never_reserves_the_allowance(tmp_path,key):
    host,store,_=setup(tmp_path)
    with pytest.raises(IntakeError,match='invalid_idempotency_key'):begin(host,store,key=key)
    assert not host.records() and not store.list_imports(OWNER)
    assert begin(host,store)['status']=='uploading'


@pytest.mark.anyio
async def test_claimed_approval_can_finish_verified_publication_but_cannot_start_embedding(tmp_path):
    host,store,_=setup(tmp_path);row=begin(host,store);seal(store,row);target,_,embedding=runtime(host,store);target.ready=True
    assert (await target.step())['stage']=='prepare'
    approval=host.approval(host.configuration());service.compute_guard.claim_approval(approval)
    assert await target.step() is None and not embedding.admitted
    # Existing verified embedding is synthetic here; actual adapter consumes the
    # approval in admit(). The worker's stored publication inputs are unchanged.
    assert (await target.worker.run_once(worker_id='fixture-embed',stages=('embed',),asset_id=row['asset_id']))['stage']=='embed'
    assert (await target.step())['stage']=='publish'
    assert store.get_asset(OWNER,row['asset_id'])['status']=='ready'
    assert len(embedding.admitted)==1


@pytest.mark.anyio
async def test_incomplete_health_proof_keeps_admission_closed(tmp_path):
    host,store,_=setup(tmp_path);target,_,_=runtime(host,store)
    async def incomplete():return {'media':True}
    target.probe=incomplete;target.recovering=False
    task=asyncio.create_task(target.pulse())
    await asyncio.sleep(.025);target.stopping.set();await task
    assert not host.healthy() and not host.capabilities(OWNER)['can_upload']


def test_removed_configuration_retains_owned_collection_resume_and_cancel_authority(tmp_path):
    host,store,_=setup(tmp_path);row=begin(host,store)
    (host.state/'compute/source-intake-service.json').unlink()
    assert not host.policy().intake_enabled and not host.capabilities(OWNER)['can_upload']
    assert host.capabilities(OWNER)['can_manage_imports'] and host.collections(OWNER)=={'cafe'}
    assert host.collections(IntakeActor('bob'))==set()
    assert begin(host,store)['id']==row['id']
    host.authorize(OWNER,store.get_import(OWNER,row['id']),operation='content')
    store.append_chunk(OWNER,row['id'],offset=0,data=b'abcd')
    host.authorize(OWNER,store.get_import(OWNER,row['id']),operation='cancel')
    assert store.cancel_import(OWNER,row['id'])['upload']['status']=='cancelled'


@pytest.mark.anyio
async def test_host_disable_is_seen_before_the_next_job_not_only_the_next_pulse(tmp_path):
    host,store,config=setup(tmp_path);row=begin(host,store);seal(store,row);target,media,embedding=runtime(host,store);target.ready=True
    config['enabled']=False;write(host.state/'compute/source-intake-service.json',config)
    assert await target.step() is None
    assert store.get_asset(OWNER,row['asset_id'])['jobs'][0]['status']=='queued'
    assert not host.healthy() and not embedding.admitted


@pytest.mark.anyio
async def test_cli_constructs_small_profile_worker_for_the_same_cosmos_directory_as_web(tmp_path,monkeypatch):
    host,store,_=setup(tmp_path,enabled=False);captured={}
    class InspectService:
        def __init__(self,received_host,received_store,worker,probe):
            assert received_host is host;self.stopping=asyncio.Event();self.ready=False;self.recovering=True
            captured.update(worker=worker,probe=probe)
        async def run(self):captured['ran']=True
    monkeypatch.setattr(service,'SourceHost',lambda state:host)
    monkeypatch.setattr(service,'SourceService',InspectService)
    loop=asyncio.get_running_loop()
    monkeypatch.setattr(loop,'add_signal_handler',lambda *args:None)
    await service.main_async(host.state)
    worker=captured['worker']
    assert worker.publisher.state==host.state/'cosmos'
    assert worker.embedding.profile=='small' and worker.enabled is False
    assert worker.limits.embedding_batch_clips==48 and worker.limits.embedding_import_clips==48
    assert worker.limits.embedding_import_bytes==64*1024**2 and captured['ran']
