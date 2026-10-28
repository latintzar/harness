"""Real SourceStore/files/index publication; fake explicitly admitted providers."""
import asyncio
import copy
from dataclasses import asdict, dataclass, replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading

import numpy as np
import pytest

HERE=Path(__file__).parents[1]
BASE=Path(os.environ.get('ROCOTO_SOURCE_TEST_BASE',HERE))
sys.path.insert(0,str(BASE));sys.path.insert(0,str(HERE))
import source_worker as worker
from source_intake import SourceStore, IntakeActor, IntakeError
import cosmos_retrieval as cosmos

spec=importlib.util.spec_from_file_location('qdrant_test_fixture',BASE/'tests/test_qdrant_retrieval.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
FakeQdrant=fixture.FakeQdrant
sys.path.insert(0,str(HERE))

IMAGE='sha256:5511c79cf8319967542048d9adcc77950f690ab0a54dc8f4ae74a17dc00315db'
OWNER=IntakeActor('alice')

@pytest.fixture
def anyio_backend():return 'asyncio'


def sha(data):return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Scope:
    company_id:str
    owner_id:str
    asset_id:str
    job_id:str


@dataclass(frozen=True)
class Lease:
    scope_key:str
    generation:int
    token:str


def new_asset(store,*,actor=OWNER,key='one'):
    source=store.ensure_upload_source(actor,collection='cafe')
    data=b'synthetic sealed original '+key.encode()
    upload=store.begin_upload(actor,source['id'],filename='camera.mp4',size=len(data),idempotency_key=key)
    store.append_chunk(actor,upload['id'],offset=0,data=data)
    return store.seal_upload(actor,upload['id'])


def payload(scope,*,generation,collection,original_sha256,original_bytes):
    timing=json.dumps({'time_base':'1/24','frames':[{'pts':168+n,'duration_ticks':1} for n in range(240)]}).encode()
    files={'frame-timing.json':timing};clips=[]
    for n,length in enumerate((8.,2.)):
        name=f'clip-{n:06d}'
        media=(scope.asset_id+name+'opaque video bytes').encode();image=b'opaque jpeg bytes'+name.encode()
        files[name+'.mp4']=media;files[name+'.jpg']=image
        clips.append({'id':scope.asset_id+'-'+name,'asset_id':scope.asset_id,'collection':collection,
            'file':name+'.mp4','sha256':sha(media),'size_bytes':len(media),'duration_seconds':length,
            'width':640,'height':360,'source_elapsed_start_seconds':n*8.,'source_elapsed_end_seconds':n*8.+length,
            'original_pts_start_seconds':7+n*8.,'original_pts_end_seconds':7+n*8.+length,
            'thumbnail':{'file':name+'.jpg','sha256':sha(image),'size_bytes':len(image)}})
    manifest={'schema_version':1,'recipe':worker.RECIPE,'asset_id':scope.asset_id,'collection':collection,
        'original':{'sha256':original_sha256,'size_bytes':original_bytes},
        'source':{'duration_seconds':10.,'origin_seconds':7.,'capture_utc':None,'capture_clock':'unknown',
            'origin_pts':168,'frame_count':240,'time_base':'1/24'},
        'normalization':{'fps':24,'window_seconds':8,'rounding_bound_seconds':1/24,'rotation_applied':True,
            'mapping':'Original presentation clock'},
        'frame_timing':{'file':'frame-timing.json','sha256':sha(timing)},'clips':clips,
        'embedding_batches':[[c['id'] for c in clips]]}
    files['manifest.json']=json.dumps(manifest,allow_nan=False).encode()
    catalogue=[{'index':i,'path':name,'size_bytes':len(data),'sha256':sha(data)} for i,(name,data) in enumerate(files.items())]
    result={'manifest':manifest,'files':catalogue,'total_bytes':sum(len(x) for x in files.values()),
        'scope':asdict(scope),'generation':generation,'profile':worker.PROFILE,'image_digest':IMAGE,
        'purpose':'prepare','schema_version':1}
    return result,{entry['index']:files[entry['path']] for entry in catalogue}


class Media:
    def __init__(self):
        self.submissions=[];self.stops=0;self.releases=0;self.results={};self.hold=False;self.mutate=None
        self.submitted=asyncio.Event();self.read_mutation=None
    async def submit(self,scope,**values):
        assert scope.company_id=='rocoto' and scope.owner_id in {'alice','bob'}
        fd=values.pop('original_fd');assert sha(os.pread(fd,values['original_bytes'],0))==values['original_sha256']
        assert os.fstat(fd).st_mode&0o777==0o400
        values.pop('expires_at');self.submissions.append((scope,values.copy()))
        handle=Lease(scope.job_id,values['generation'],'private synthetic token')
        self.results[handle]=payload(scope,**values)
        if self.mutate:self.mutate(self.results[handle][0])
        self.submitted.set();return handle
    async def status(self,scope,handle):
        return {'status':'running' if self.hold else 'succeeded','cleanup_confirmed':True,'result':self.results[handle][0]}
    async def heartbeat(self,*args,**kwargs):pass
    async def recover(self,scope,generation):
        return next((lease for lease in self.results if lease.scope_key==scope.job_id and lease.generation==generation),None)
    async def read_output(self,scope,handle,index):
        data=self.results[handle][1][index]
        if self.read_mutation:data=self.read_mutation(data)
        for start in range(0,len(data),11):yield data[start:start+11]
    async def stop(self,*args):self.stops+=1;return {'cleanup_confirmed':True,'status':'stopped'}
    async def release(self,*args):self.releases+=1


class Embedding:
    def __init__(self,store):
        self.store=store;self.admitted=[];self.called=[];self.denied=False;self.mutate=None
        self.hold=False;self.started=asyncio.Event()
    async def admit(self,context,batch,*,idempotency_key):
        if self.denied:raise worker.WorkerError('funding_admission_denied')
        self.admitted.append(idempotency_key)
        return {'reservation_id':'reservation-'+idempotency_key,'attempt_id':'attempt-'+idempotency_key}
    async def run(self,admission,inputs,*,cancelled):
        # Before any external work, its actual reservation/attempt is durable.
        with self.store._read() as db:
            checkpoint=json.loads(db.execute("SELECT result FROM jobs WHERE stage='embed' AND status='running'").fetchone()[0])
        assert checkpoint['phase']=='embedding_admitted' and admission in checkpoint['admissions']
        assert all(set(item)=={'id','sha256','duration_seconds','content'} for item in inputs)
        self.called.append(admission);self.started.set()
        if self.hold:
            await cancelled.wait()
            raise worker.WorkerError('embedding_cancelled')
        result={'model':cosmos.MODEL_ID,'model_revision':cosmos.MODEL_REVISION,'dimensions':256,'frames_per_clip':8,
            'attempt_id':admission['attempt_id'],'clips':[{'id':row['id'],'sha256':sha(row['content']),
                'embedding':np.eye(1,256,k=i,dtype=np.float32)[0].tolist()} for i,row in enumerate(inputs)]}
        if self.mutate:self.mutate(result)
        return result


def setup(tmp_path,**limits):
    store=SourceStore(tmp_path/'company');media=Media();embedding=Embedding(store);api=FakeQdrant()
    publisher=worker.IndexPublisher(tmp_path/'index',resolver_ready=lambda:True,client=api)
    instance=worker.SourceWorker(store,company_id='rocoto',media_client=media,media_image=IMAGE,
        embedding_adapter=embedding,publisher=publisher,enabled=True,scope_factory=Scope,
        limits=worker.WorkerLimits(heartbeat_seconds=.05,poll_seconds=.02,**limits))
    return instance,store,media,embedding,api


async def through_embed(instance):
    assert (await instance.run_once(stages=('prepare',)))['status']=='succeeded'
    assert (await instance.run_once(stages=('embed',)))['status']=='succeeded'


@pytest.mark.anyio
async def test_three_stages_publish_opaque_media_with_original_clock_and_private_provenance(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store)
    await through_embed(instance)
    assert store.get_asset(OWNER,upload['asset_id'])['status']=='indexing'
    assert (await instance.run_once(stages=('publish',)))['status']=='succeeded'
    assert store.get_asset(OWNER,upload['asset_id'])['status']=='ready'
    rows,vectors,report,marker,_=cosmos.load_generation(instance.publisher.state)
    assert len(rows)==2 and vectors.shape==(2,256) and np.allclose(np.linalg.norm(vectors,axis=1),1)
    assert rows[1]['start']==8 and rows[1]['original_pts_start_seconds']==15
    for row in rows:
        assert row['storage_kind']=='imported_source' and row['preparation_profile']=='source-media-prepare-v2'
        assert row['path']==str(store.blobs/upload['sha256']) and sha(Path(row['path']).read_bytes())==upload['sha256']
        assert row['asset_id']==upload['asset_id'] and row['original_sha256']==upload['sha256']
        assert sha(Path(row['prepared_path']).read_bytes())==row['prepared_sha256']
        assert Path(row['prepared_path']).stat().st_mode&0o777==0o400
        assert row['prepared_start']==0 and row['prepared_end']<=8.25 and row['frames_embedded']==8
    assert media.releases==1 and media.stops==0 and len(embedding.called)==1 and api.aliases
    public=store.get_asset(OWNER,upload['asset_id'])
    assert 'private synthetic token' not in json.dumps(public) and str(tmp_path) not in json.dumps(public)


@pytest.mark.anyio
async def test_default_activation_and_resolver_gate_allocate_no_work(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store)
    instance.enabled=False
    with pytest.raises(worker.WorkerError,match='unavailable'):await instance.run_once()
    assert not media.submissions and not embedding.called
    instance.enabled=True;await through_embed(instance)
    instance.publisher.resolver_ready=lambda:False
    result=await instance.run_once(stages=('publish',))
    assert result['status']=='needs_reconciliation' and not api.calls
    assert not (instance.publisher.state/cosmos.GENERATION_FILE).exists()
    assert store.get_asset(OWNER,upload['asset_id'])['status']=='needs_attention'


@pytest.mark.anyio
@pytest.mark.parametrize('mutation',[
    lambda r:r.update(profile='source-media-prepare-v1'),
    lambda r:r['scope'].update(company_id='other'),
    lambda r:r.update(generation=999),
    lambda r:r['manifest']['normalization'].update(window_seconds=10),
    lambda r:r['manifest']['original'].update(sha256='0'*64),
    lambda r:r['manifest']['clips'][0].update(original_pts_start_seconds=float('nan')),
    lambda r:r['manifest']['clips'][0].update(original_pts_start_seconds='seven'),
    lambda r:r['files'][0].update(path='../escape'),
    lambda r:r['files'][1].update(size_bytes=123456),
    lambda r:r['manifest']['clips'][0]['thumbnail'].update(size_bytes=0),
])
async def test_preparation_claims_fail_closed_and_stop_worker(tmp_path,mutation):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store);media.mutate=mutation
    result=await instance.run_once(stages=('prepare',))
    assert result['status']=='needs_reconciliation' and media.stops==1 and not embedding.called
    assert store.get_asset(OWNER,upload['asset_id'])['status']=='needs_attention'
    assert await instance.run_once() is None


@pytest.mark.anyio
async def test_download_hash_and_storage_quota_gate_before_embedding(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);new_asset(store)
    media.read_mutation=lambda data:data+b'changed'
    assert (await instance.run_once())['status']=='needs_reconciliation'
    assert media.stops==1 and not embedding.called
    other,store2,media2,emb2,_=setup(tmp_path/'quota',artifact_storage_bytes=1);new_asset(store2)
    assert (await other.run_once())['error']=='artifact_storage_quota' and not emb2.called


@pytest.mark.anyio
async def test_embedding_requires_durable_funding_admission(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);new_asset(store)
    assert (await instance.run_once(stages=('prepare',)))['status']=='succeeded'
    embedding.denied=True
    result=await instance.run_once(stages=('embed',))
    assert result['error']=='funding_admission_denied' and not embedding.called
    assert await instance.run_once() is None


@pytest.mark.anyio
@pytest.mark.parametrize('mutation',[
    lambda r:r.update(model_revision='wrong'),lambda r:r.update(frames_per_clip=3),
    lambda r:r['clips'][0].update(sha256='0'*64),lambda r:r['clips'][0].update(embedding=[0]*256),
    lambda r:r['clips'][0].update(embedding=[float('nan')]*256),
])
async def test_embedding_result_provenance_and_vector_checks(tmp_path,mutation):
    instance,store,media,embedding,api=setup(tmp_path);new_asset(store)
    assert (await instance.run_once())['status']=='succeeded';embedding.mutate=mutation
    assert (await instance.run_once())['status']=='needs_reconciliation' and not api.calls
    with store._read() as db: checkpoint=json.loads(db.execute("SELECT result FROM jobs WHERE stage='embed'").fetchone()[0])
    assert checkpoint['admissions'][0]['reservation_id']


@pytest.mark.anyio
async def test_cancel_prepare_revokes_generation_and_requires_confirmed_cleanup(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store);media.hold=True
    task=asyncio.create_task(instance.run_once(stages=('prepare',)))
    await media.submitted.wait()
    await asyncio.to_thread(store.cancel_import,OWNER,upload['id'])
    result=await asyncio.wait_for(task,2)
    assert result['status']=='needs_reconciliation' and media.stops==1 and not embedding.called
    view=store.get_import(OWNER,upload['id'])
    assert view['asset']['status']=='needs_attention' and view['asset']['jobs'][0]['error']=='cancel_requested'


@pytest.mark.anyio
async def test_cancel_embedding_preserves_reserved_attempt_for_reconciliation(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store)
    assert (await instance.run_once())['status']=='succeeded';embedding.hold=True
    task=asyncio.create_task(instance.run_once(stages=('embed',)))
    await embedding.started.wait();await asyncio.to_thread(store.cancel_import,OWNER,upload['id'])
    assert (await asyncio.wait_for(task,2))['status']=='needs_reconciliation'
    with store._read() as db: row=db.execute("SELECT * FROM jobs WHERE stage='embed'").fetchone()
    assert row['status']=='needs_reconciliation' and json.loads(row['result'])['admissions']
    assert not api.calls and await instance.run_once() is None


@pytest.mark.anyio
async def test_failed_qdrant_keeps_previous_marker_then_recovers_without_provider_retry(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path)
    prior=new_asset(store,key='old');await through_embed(instance);assert (await instance.run_once())['status']=='succeeded'
    before=cosmos.load_generation(instance.publisher.state)[3]['generation_id']
    aliases=api.aliases.copy()
    upload=new_asset(store,key='new');await through_embed(instance)
    api.fail_upsert=True
    assert (await instance.run_once())['status']=='needs_reconciliation'
    assert cosmos.load_generation(instance.publisher.state)[3]['generation_id']==before
    asset=store.get_asset(OWNER,upload['asset_id']);job=next(x for x in asset['jobs'] if x['stage']=='publish')
    assert asset['status']=='needs_attention' and api.aliases==aliases and await instance.run_once() is None
    # Explicit host recovery after checking the immutable local generation and actual Qdrant state.
    store.reconcile_job(job['id'],expected_generation=job['generation'],outcome='retry',evidence_reference=before)
    api.fail_upsert=False
    assert (await instance.run_once())['status']=='succeeded'
    assert cosmos.load_generation(instance.publisher.state)[3]['generation_id']!=before
    assert len(embedding.called)==2 and len(media.submissions)==2
    assert store.get_asset(OWNER,upload['asset_id'])['status']=='ready'


@pytest.mark.anyio
async def test_cancel_cannot_enter_between_index_side_effect_and_ready(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store);await through_embed(instance)
    entered=threading.Event();resume=threading.Event();original=api.request
    def blocked(method,path,*args,**kwargs):
        if path=='/collections/aliases':
            entered.set();assert resume.wait(5)
        return original(method,path,*args,**kwargs)
    api.request=blocked
    publication=asyncio.create_task(instance.run_once(stages=('publish',)))
    assert await asyncio.to_thread(entered.wait,2)
    cancellation=asyncio.create_task(asyncio.to_thread(store.cancel_import,OWNER,upload['id']))
    await asyncio.sleep(.08);assert not cancellation.done()
    resume.set();assert (await publication)['status']=='succeeded'
    with pytest.raises(IntakeError,match='import_finished'):await cancellation
    assert store.get_asset(OWNER,upload['asset_id'])['status']=='ready'


@pytest.mark.anyio
async def test_concurrent_imports_keep_previous_and_both_new_assets(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path)
    prior=new_asset(store,key='old');await through_embed(instance);assert (await instance.run_once())['status']=='succeeded'
    one=new_asset(store,key='a');await through_embed(instance)
    two=new_asset(store,actor=IntakeActor('bob'),key='b');await through_embed(instance)
    other=worker.SourceWorker(store,company_id='rocoto',media_client=media,media_image=IMAGE,
        embedding_adapter=embedding,publisher=instance.publisher,enabled=True,scope_factory=Scope)
    results=await asyncio.gather(instance.run_once(stages=('publish',)),other.run_once(stages=('publish',)))
    assert all(result['status']=='succeeded' for result in results)
    rows=cosmos.load_generation(instance.publisher.state)[0]
    assert {row['asset_id'] for row in rows}=={u['asset_id'] for u in (prior,one,two)} and len(rows)==6


@pytest.mark.anyio
async def test_restart_preserves_checkpoint_and_expired_generation_stays_fenced(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store)
    lease=store.claim_job(worker_id='crashed',lease_seconds=1)
    store.checkpoint_job(lease,checkpoint={'phase':'media_submit_intent','generation':lease.generation})
    restarted=SourceStore(tmp_path/'company',clock=lambda:store.clock()+2)
    restarted.recover()
    assert restarted.claim_job(worker_id='new') is None
    with restarted._read() as db: row=db.execute('SELECT * FROM jobs WHERE id=?',(lease.job_id,)).fetchone()
    assert row['generation']>lease.generation and json.loads(row['result'])['phase']=='media_submit_intent'
    with pytest.raises(IntakeError,match='stale_job_lease'):
        restarted.checkpoint_job(lease,checkpoint={'phase':'late-result'})


@pytest.mark.anyio
async def test_sealed_derivative_tampering_stops_before_embedding(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);new_asset(store)
    assert (await instance.run_once())['status']=='succeeded'
    file=next(instance.artifacts.root.glob('asset-*/prepare-*/*.mp4'))
    file.chmod(0o600);file.write_bytes(b'tampered')
    assert (await instance.run_once())['status']=='needs_reconciliation' and not embedding.called


@pytest.mark.anyio
async def test_crash_after_alias_commit_recovers_exact_generation_and_ready_receipt(tmp_path,monkeypatch):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store);await through_embed(instance)
    original=store._finish_job_locked
    def interrupted(db,row,result):
        if row['stage']=='publish':raise OSError('synthetic process interruption after external commit')
        return original(db,row,result)
    monkeypatch.setattr(store,'_finish_job_locked',interrupted)
    assert (await instance.run_once())['status']=='needs_reconciliation'
    generation=cosmos.load_generation(instance.publisher.state)[3]['generation_id']
    assert api.aliases and store.get_asset(OWNER,upload['asset_id'])['status']=='needs_attention'
    with store._read() as db:
        row=db.execute("SELECT * FROM jobs WHERE stage='publish'").fetchone()
        assert json.loads(row['result'])['phase']=='publication_intent'
    monkeypatch.setattr(store,'_finish_job_locked',original)
    store.reconcile_job(row['id'],expected_generation=row['generation'],outcome='retry',evidence_reference=generation)
    assert (await instance.run_once())['status']=='succeeded'
    assert cosmos.load_generation(instance.publisher.state)[3]['generation_id']==generation
    assert len(embedding.called)==1 and store.get_asset(OWNER,upload['asset_id'])['status']=='ready'


@pytest.mark.anyio
async def test_process_cancellation_confirms_media_stop_before_return(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);new_asset(store);media.hold=True
    cleanup=asyncio.Event();resume=asyncio.Event()
    async def stopping(*args):
        cleanup.set();await resume.wait();media.stops+=1
        return {'cleanup_confirmed':True}
    media.stop=stopping
    task=asyncio.create_task(instance.run_once());await media.submitted.wait();task.cancel()
    await cleanup.wait();task.cancel();await asyncio.sleep(.02)
    assert not task.done();resume.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert media.stops==1 and await instance.run_once() is None


@pytest.mark.anyio
async def test_cancelled_disk_reservation_releases_its_eventual_flock(tmp_path):
    instance,*_=setup(tmp_path)
    first=instance.artifacts.acquire(1)
    async def allocate():
        async with instance.artifacts.allocation(1):raise AssertionError('cancelled reservation entered')
    task=asyncio.create_task(allocate());await asyncio.sleep(.05);task.cancel()
    os.close(first)
    with pytest.raises(asyncio.CancelledError):await task
    # A leaked descriptor would leave this file locked by the cancelled task.
    second=await asyncio.wait_for(asyncio.to_thread(instance.artifacts.acquire,1),1)
    os.close(second)


@pytest.mark.anyio
async def test_atomic_publication_rejects_expired_deadline_after_external_commit(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);upload=new_asset(store);await through_embed(instance)
    lease=store.claim_job(worker_id='bounded-local-publisher');clock=store.clock
    effect=[]
    def committed():
        effect.append('committed');store.clock=lambda:clock()+1000
        return {'generation_id':'immutable-synthetic-generation'}
    with pytest.raises(IntakeError,match='stale_job_lease'):
        store.finish_publication(lease,publish=committed)
    assert effect and store.get_asset(OWNER,upload['asset_id'])['status']=='indexing'
    store.recover();assert store.get_asset(OWNER,upload['asset_id'])['status']=='needs_attention'


@pytest.mark.anyio
@pytest.mark.parametrize('failure',['create','upsert','count','alias','alias_ack'])
async def test_qdrant_stage_failures_keep_public_marker_and_older_pinned_queries(tmp_path,failure):
    instance,store,media,embedding,api=setup(tmp_path)
    old=new_asset(store,key='old');await through_embed(instance);assert (await instance.run_once())['status']=='succeeded'
    pinned=worker.QdrantRetrievalIndex(instance.publisher.state,client=api)
    marker=(instance.publisher.state/cosmos.GENERATION_FILE).read_bytes()
    new=new_asset(store,key='new');await through_embed(instance)
    original=api.request;switched=False
    def failing(method,path,body=None,**kwargs):
        nonlocal switched
        if failure=='create' and method=='PUT' and len(path.strip('/').split('/'))==2:
            raise OSError('synthetic create interruption')
        if failure=='upsert' and '/points?' in path:raise OSError('synthetic upsert interruption')
        if failure=='count' and path.endswith('/count'):return {'count':0}
        if failure=='alias' and path=='/collections/aliases':raise OSError('synthetic alias interruption')
        if path=='/collections/aliases':switched=True
        if failure=='alias_ack' and path=='/aliases' and switched:raise OSError('synthetic lost alias acknowledgment')
        return original(method,path,body,**kwargs)
    api.request=failing
    assert (await instance.run_once())['status']=='needs_reconciliation'
    assert (instance.publisher.state/cosmos.GENERATION_FILE).read_bytes()==marker
    api.request=original
    hits=pinned.search_vector(np.eye(1,256,dtype=np.float32)[0],limit=6,dataset='cafe')
    assert len(hits)==2 and {row['video_id'] for row in hits}=={old['asset_id']}
    assert store.get_asset(OWNER,new['asset_id'])['status']=='needs_attention'


@pytest.mark.anyio
async def test_alias_then_marker_failure_preserves_old_reader_and_reconciles(tmp_path,monkeypatch):
    instance,store,media,embedding,api=setup(tmp_path)
    old=new_asset(store,key='old');await through_embed(instance);assert (await instance.run_once())['status']=='succeeded'
    pinned=worker.QdrantRetrievalIndex(instance.publisher.state,client=api)
    previous=(instance.publisher.state/cosmos.GENERATION_FILE).read_bytes()
    new=new_asset(store,key='new');await through_embed(instance)
    write=cosmos._atomic_write
    def failed_marker(path,data):
        if path.name==cosmos.GENERATION_FILE:raise OSError('synthetic crash after alias acknowledgment')
        return write(path,data)
    monkeypatch.setattr(cosmos,'_atomic_write',failed_marker)
    assert (await instance.run_once())['status']=='needs_reconciliation'
    assert (instance.publisher.state/cosmos.GENERATION_FILE).read_bytes()==previous
    assert list(api.aliases.values())!=[pinned.collection]
    assert len(pinned.search_vector(np.eye(1,256,dtype=np.float32)[0],limit=6))==2
    monkeypatch.setattr(cosmos,'_atomic_write',write)
    job=next(x for x in store.get_asset(OWNER,new['asset_id'])['jobs'] if x['stage']=='publish')
    store.reconcile_job(job['id'],expected_generation=job['generation'],outcome='retry',evidence_reference='verified-staged-alias')
    assert (await instance.run_once())['status']=='succeeded'
    assert len(cosmos.load_generation(instance.publisher.state)[0])==4 and len(embedding.called)==2


@pytest.mark.anyio
async def test_cancel_during_accepted_submission_recovers_handle_and_confirms_stop(tmp_path):
    instance,store,media,embedding,api=setup(tmp_path);new_asset(store)
    submit=media.submit
    async def delayed_submit(*args,**kwargs):
        await submit(*args,**kwargs)
        # Real MediaClient drains accepted admission but cancellation occurs
        # before the caller receives its returned private token.
        await asyncio.Event().wait()
    media.submit=delayed_submit
    task=asyncio.create_task(instance.run_once());await media.submitted.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert media.stops==1 and not embedding.called and await instance.run_once() is None


@pytest.mark.anyio
@pytest.mark.parametrize('terminal',[True,False])
async def test_prepare_heartbeat_completion_race_rechecks_terminal_state(tmp_path,terminal):
    from source_media import MediaError
    instance,store,media,embedding,api=setup(tmp_path);new_asset(store)
    media.hold=True;renewals=[]
    async def heartbeat(*args,**kwargs):
        renewals.append(kwargs)
        if terminal:media.hold=False
        raise MediaError('media_lease_expired')
    media.heartbeat=heartbeat
    result=await instance.run_once(stages=('prepare',))
    assert len(renewals)==1
    assert result['status']==('succeeded' if terminal else 'needs_reconciliation')
    assert media.releases==(1 if terminal else 0)
    assert media.stops==(0 if terminal else 1)
