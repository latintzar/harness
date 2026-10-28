"""Derivative receipts prove authority, opaque-byte integrity and cleanup."""
import asyncio
import copy
from dataclasses import asdict
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time

import pytest
BASE=Path(os.environ.get('ROCOTO_SOURCE_TEST_BASE',Path(__file__).parents[1]))
BROKER=Path(os.environ.get('ROCOTO_DERIVE_TEST_BASE',BASE))
HERE=Path(__file__).parents[1]
sys.path.insert(0,str(BASE/'tests'));sys.path.insert(0,str(BASE));sys.path.insert(0,str(BROKER));sys.path.insert(0,str(HERE))
from source_derivatives import SourceDerivatives,DerivativeError
from source_media import MediaLease,DerivationRequest,DERIVE_PROFILE,DERIVE_RECIPE,DERIVE_IMAGE
from workspace_identity import workspace_scope
from test_imported_media import imported as prepared_import,deny_decoders


@pytest.fixture
def imported(prepared_import):
    data=prepared_import
    with data['store']._write() as db:
        data['store']._queue_job(db,data['row']['asset_id'],'publish')
        db.execute("UPDATE jobs SET status='succeeded',result=? WHERE asset_id=? AND stage='publish'",
            (json.dumps(dict(generation_id='fixture-generation',qdrant_collection='fixture',qdrant_fingerprint='a'*64)),
             data['row']['asset_id']))
    return data


def sha(data):return hashlib.sha256(data).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'))


class FakeMedia:
    def __init__(self):
        self.calls=0;self.handles={};self.results={};self.alive=set();self.stops=0;self.releases=0
        self.gate=asyncio.Event();self.gate.set();self.admitted=asyncio.Event();self.stop_gate=asyncio.Event();self.stop_gate.set()
        self.cancel_admit=False;self.corrupt=None;self.mutate=None;self.stop_error=False;self.release_error=False
        self.log=None

    async def derive(self,scope,**values):
        self.calls+=1
        if self.log:
            with open(self.log,'a') as stream:stream.write('admit\n')
        original=os.pread(values['original_fd'],values['original_bytes'],0)
        assert sha(original)==values['original_sha256']
        lease=MediaLease(scope.key,values['generation'],'a'*48)
        self.handles[(scope.key,lease.generation)]=lease;self.alive.add(scope.key)
        request=DerivationRequest.parse(values['derivation']);req=request.manifest_request(scope,values['collection'],sha(original))
        frames=[dict(source_frame_index=i,original_pts=300+i,original_pts_seconds=12+i/25,
            source_elapsed_seconds=i/25,duration_seconds=1/25) for i in range(100)]
        files=[];data={}
        if request.purpose=='inspect_frames_v1':
            for number,offset in enumerate(request.frame_seconds):
                name=f'frame-{number:02d}.jpg';value=b'opaque jpeg'+str(number).encode();data[name]=value
                files.append(dict(file=name,size_bytes=len(value),sha256=sha(value),mime_type='image/jpeg',width=1920,height=1080,
                    requested_clip_seconds=offset,requested_source_seconds=request.start+offset,**frames[int(offset*25)]))
        else:
            value=b'opaque video content';data['clip.mp4']=value;detail=request.purpose=='detail_video_v1';fps=4 if detail else 25
            files.append(dict(file='clip.mp4',size_bytes=len(value),sha256=sha(value),mime_type='video/mp4',width=1920,height=1080,
                video_codec='h264',average_frame_rate=f'{fps}/1',nominal_frame_rate=f'{fps}/1',duration_seconds=4.,frame_count=4*fps,
                start_time_seconds=0.,audio_codecs=[],audio_start_seconds=[],time_base='1/25',frame_pts_seconds=[i/fps for i in range(4*fps)],
                source_frames=frames,actual_source_first_frame_seconds=0.,actual_source_end_seconds=4.,actual_original_first_frame_pts=300,
                requested_interval_is_frame_aligned=True,rotation_applied=True,
                mapping_basis='nominal_source_elapsed_4fps_resampling' if detail else 'preserved_source_frame_pts',
                resampling_rounding_bound_seconds=.25 if detail else 0))
        manifest=dict(schema_version=1,profile=DERIVE_PROFILE,recipe=DERIVE_RECIPE,purpose=request.purpose,
            asset_id=scope.asset_id,collection=values['collection'],clip_id=request.clip_id,
            original=dict(sha256=sha(original),size_bytes=len(original)),request=req,request_sha256=sha(canonical(req).encode()),
            source=dict(capture_utc=None,capture_clock='unknown',duration_seconds=4.,origin_seconds=12.,origin_pts=300,
                frame_count=100,time_base='1/25',coded_width=1920,coded_height=1080,rotation_degrees=0),
            clock='source_relative',capture_utc=None,files=files,requested_original_pts_start_seconds=12.,requested_original_pts_end_seconds=16.)
        if self.mutate:self.mutate(manifest)
        data['manifest.json']=json.dumps(manifest).encode()
        descriptions=[dict(index=i,path=name,size_bytes=len(value),sha256=sha(value)) for i,(name,value) in enumerate(sorted(data.items()))]
        result=dict(schema_version=1,profile=DERIVE_PROFILE,image_digest=DERIVE_IMAGE,purpose=request.purpose,scope=asdict(scope),
            generation=lease.generation,manifest=manifest,files=descriptions,total_bytes=sum(len(value) for value in data.values()))
        self.results[scope.key]=(result,{row['index']:data[row['path']] for row in descriptions})
        self.admitted.set()
        if self.cancel_admit:await asyncio.Future()
        return lease

    async def recover(self,scope,generation):return self.handles.get((scope.key,generation))
    async def status(self,scope,lease):
        if not self.gate.is_set():return dict(status='running',cleanup_confirmed=False)
        self.alive.discard(scope.key)
        return dict(status='succeeded',cleanup_confirmed=True,result=copy.deepcopy(self.results[scope.key][0]))
    async def heartbeat(self,*args,**kwargs):return {'status':'running'}
    async def stop(self,scope,lease):
        self.stops+=1;await self.stop_gate.wait()
        if self.stop_error:raise RuntimeError('injected_stop_failure')
        self.alive.discard(scope.key);return {'cleanup_confirmed':True,'status':'stopped'}
    async def release(self,scope,lease):
        self.releases+=1
        if self.release_error:raise RuntimeError('injected_release_failure')
        return {'cleanup_confirmed':True,'status':'released'}
    async def read_output(self,scope,lease,index):
        result,files=self.results[scope.key];data=files[index]
        if self.corrupt=='truncate':data=data[:-1]
        elif self.corrupt=='bytes':data=b'X'*len(data)
        for start in range(0,len(data),41):yield data[start:start+41]


@pytest.fixture
def anyio_backend():return 'asyncio'


def helper(imported,media=None,*,enabled=True):
    target=SourceDerivatives(imported['state'],company_id=workspace_scope(imported['state']),media_client=media,enabled=enabled)
    target.poll_seconds=.005;target.wait_seconds=2
    return target


@pytest.mark.anyio
@pytest.mark.parametrize('purpose', ['detail_video_v1','source_cut_v1','inspect_frames_v1'])
async def test_opaque_derived_files_cache_across_readonly_instance(imported,monkeypatch,purpose):
    media=FakeMedia();target=helper(imported,media);deny_decoders(monkeypatch)
    samples=[0.,1.,3.9] if purpose=='inspect_frames_v1' else []
    first=await target.derive(imported['row'],purpose,owner_principal_id='owner',frame_seconds=samples)
    second=await helper(imported).derive(imported['row'],purpose,owner_principal_id='owner',frame_seconds=samples)
    assert second.receipt_id==first.receipt_id and media.calls==1 and not media.alive
    reader=helper(imported,enabled=False)
    saved=reader.read_receipt(first.receipt_id,owner_principal_id='owner',purpose=purpose)
    assert saved.manifest==first.manifest
    assert len(saved.files)==len(samples or [0])
    assert all(file.read_bytes().startswith(b'opaque ') for file in saved.files)
    assert all(file.path.stat().st_mode&0o222==0 for file in saved.files)
    assert media.stops==1 and media.releases==1


@pytest.mark.anyio
@pytest.mark.parametrize('change',[{'start':1.},{'end':3.},{'original_sha256':'0'*64},{'path':'/etc/passwd'},
    {'prepared_manifest_sha256':'1'*64},{'dataset':'other'},{'asset_id':'asset-'+'b'*32}])
async def test_tampered_record_fails_before_admission(imported,change):
    media=FakeMedia();target=helper(imported,media)
    with pytest.raises(ValueError):await target.derive(imported['row']|change,'detail_video_v1',owner_principal_id='owner')
    assert media.calls==0


@pytest.mark.anyio
async def test_cross_owner_company_purpose_and_receipt_reference_rejected(imported):
    media=FakeMedia();target=helper(imported,media)
    with pytest.raises(ValueError):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='foreign')
    saved=await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    for owner,purpose,key in [('foreign','detail_video_v1',saved.receipt_id),('owner','source_cut_v1',saved.receipt_id),
        ('owner','detail_video_v1','../../etc/passwd')]:
        with pytest.raises(ValueError):target.read_receipt(key,owner_principal_id=owner,purpose=purpose)
    with pytest.raises(ValueError):SourceDerivatives(imported['state'],company_id='other')
    with imported['store']._write() as db:db.execute("UPDATE assets SET status='cancelled' WHERE id=?",(imported['row']['asset_id'],))
    with pytest.raises(ValueError):target.read_receipt(saved.receipt_id,owner_principal_id='owner',purpose='detail_video_v1')


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['truncate','bytes'])
async def test_partial_or_changed_output_never_publishes_receipt(imported,kind):
    media=FakeMedia();media.corrupt=kind;target=helper(imported,media)
    with pytest.raises(ValueError):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert not media.alive and media.stops==1 and media.releases==1
    with target._db() as db:assert db.execute('SELECT status FROM receipts').fetchone()[0]=='cancelled'


@pytest.mark.anyio
@pytest.mark.parametrize('field,value',[('profile','other'),('recipe','other'),('purpose','source_cut_v1'),
    ('asset_id','asset-'+'b'*32),('clip_id','foreign'),('request_sha256','b'*64),('collection','other')])
async def test_manifest_provenance_cannot_authorize_result(imported,field,value):
    media=FakeMedia();media.mutate=lambda m:m.__setitem__(field,value);target=helper(imported,media)
    with pytest.raises(ValueError):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert not media.alive and media.releases==1


@pytest.mark.anyio
async def test_cached_reopen_checks_actual_bytes_symlinks_and_permissions(imported):
    media=FakeMedia();target=helper(imported,media)
    receipt=await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    file=receipt.files[0];original=file.path.read_bytes();file.path.chmod(0o600);file.path.write_bytes(b'X'*len(original));file.path.chmod(0o400)
    with pytest.raises(ValueError):target.read_receipt(receipt.receipt_id,owner_principal_id='owner',purpose='detail_video_v1')
    file.path.chmod(0o600);file.path.write_bytes(original)
    with pytest.raises(ValueError):target.read_receipt(receipt.receipt_id,owner_principal_id='owner',purpose='detail_video_v1')
    file.path.parent.chmod(0o700);file.path.unlink();file.path.symlink_to('/etc/passwd')
    with pytest.raises(ValueError):target.read_receipt(receipt.receipt_id,owner_principal_id='owner',purpose='detail_video_v1')


@pytest.mark.anyio
async def test_concurrent_same_key_deduplicates_and_cancelled_waiter_leaves_owner_running(imported):
    media=FakeMedia();media.gate.clear();target=helper(imported,media)
    first=asyncio.create_task(target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner'))
    await media.admitted.wait()
    waiter=asyncio.create_task(helper(imported,media).derive(imported['row'],'detail_video_v1',owner_principal_id='owner'))
    await asyncio.sleep(.03);waiter.cancel()
    with pytest.raises(asyncio.CancelledError):await waiter
    assert media.alive and not media.stops and media.calls==1
    second=asyncio.create_task(helper(imported,media).derive(imported['row'],'detail_video_v1',owner_principal_id='owner'))
    media.gate.set();one,two=await asyncio.gather(first,second)
    assert one.receipt_id==two.receipt_id and media.calls==1


@pytest.mark.anyio
async def test_cancel_after_accepted_submit_recovers_handle_and_waits_for_whole_stop(imported):
    media=FakeMedia();media.cancel_admit=True;media.stop_gate.clear();target=helper(imported,media)
    pending=asyncio.create_task(target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner'))
    await media.admitted.wait();pending.cancel();await asyncio.sleep(.02)
    assert not pending.done() and media.alive
    pending.cancel();await asyncio.sleep(.02);assert not pending.done()
    media.stop_gate.set()
    with pytest.raises(asyncio.CancelledError):await pending
    assert not media.alive and media.stops==1 and media.releases==1
    media.cancel_admit=False
    result=await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert result.receipt_id and media.calls==2
    with target._db() as db:assert db.execute('SELECT generation FROM receipts').fetchone()[0]==2


@pytest.mark.anyio
async def test_uncertain_stop_fences_new_admission_until_reconciliation(imported):
    media=FakeMedia();media.cancel_admit=True;media.stop_error=True;target=helper(imported,media)
    pending=asyncio.create_task(target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner'))
    await media.admitted.wait();pending.cancel()
    with pytest.raises(DerivativeError):await pending
    with pytest.raises(DerivativeError):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert media.calls==1 and media.alive
    media.stop_error=False
    with pytest.raises(DerivativeError,match='reconciled_retry'):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert media.calls==1 and not media.alive


@pytest.mark.anyio
async def test_crash_after_sealed_copy_reconciles_cached_receipt_without_decoder_retry(imported):
    media=FakeMedia();media.release_error=True;target=helper(imported,media)
    with pytest.raises(DerivativeError):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    with target._db() as db:assert db.execute('SELECT status FROM receipts').fetchone()[0]=='needs_reconciliation'
    media.release_error=False
    restored=await helper(imported,media).derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert restored.files[0].read_bytes()==b'opaque video content' and media.calls==1


@pytest.mark.anyio
async def test_unconfigured_helper_never_creates_job_or_calls_broker(imported):
    target=helper(imported,enabled=False)
    with pytest.raises(DerivativeError,match='unavailable'):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert not target.path.exists()


def _process_derive(state,row,log,queue):
    async def main():
        media=FakeMedia();media.log=log
        target=SourceDerivatives(state,company_id=workspace_scope(state),media_client=media,enabled=True)
        result=await target.derive(row,'detail_video_v1',owner_principal_id='owner')
        queue.put(result.receipt_id)
    asyncio.run(main())


def test_cross_process_same_key_has_one_admission(imported,tmp_path):
    context=multiprocessing.get_context('fork');queue=context.Queue();log=tmp_path/'admissions.log'
    processes=[context.Process(target=_process_derive,args=(imported['state'],imported['row'],log,queue)) for _ in range(2)]
    for process in processes:process.start()
    try:
        results=[queue.get(timeout=10) for _ in processes]
        for process in processes:process.join(10);assert process.exitcode==0
        assert results[0]==results[1] and log.read_text()=='admit\n'
    finally:
        for process in processes:
            if process.is_alive():process.kill();process.join()


@pytest.mark.anyio
async def test_actual_unix_broker_receipt_roundtrip(imported,tmp_path):
    import importlib.util
    from source_media import MediaBroker,MediaServer,MediaClient,MediaScope
    spec=importlib.util.spec_from_file_location('derivative_broker_fixtures',BROKER/'tests/test_source_media.py')
    fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
    class Backend(fixture.FakeBackend):
        async def wait(self,spec):
            synthetic=FakeMedia()
            scope=MediaScope(workspace_scope(imported['state']),'owner',spec.asset_id,'job-'+'c'*32)
            with spec.original.open('rb') as stream:
                lease=await synthetic.derive(scope,generation=1,collection=spec.collection,
                    original_sha256=sha(imported['original']),original_bytes=len(imported['original']),
                    original_fd=stream.fileno(),derivation=asdict(spec.derivation))
            result,data=synthetic.results[scope.key]
            output=spec.output/'derived';output.mkdir()
            for item in result['files']:(output/item['path']).write_bytes(data[item['index']])
            return 0
    backend=Backend(tmp_path/'volumes')
    broker=MediaBroker(tmp_path/'broker',backend,app_uid=os.geteuid(),app_gid=os.getegid(),worker_uid=os.geteuid())
    server=MediaServer(broker,tmp_path/'media.sock');await server.start()
    try:
        media=MediaClient(tmp_path/'media.sock',server_uid=os.geteuid());target=helper(imported,media)
        for purpose in ('detail_video_v1','inspect_frames_v1','source_cut_v1'):
            result=await target.derive(imported['row'],purpose,owner_principal_id='owner',
                frame_seconds=[.2,2.] if purpose=='inspect_frames_v1' else [])
            assert result.files and all(file.read_bytes().startswith(b'opaque ') for file in result.files)
            assert target.read_receipt(result.receipt_id,owner_principal_id='owner',purpose=purpose).manifest==result.manifest
        assert backend.start_count==3 and not backend.alive
        assert all(row['status']=='released' for row in broker.db.execute('SELECT * FROM jobs'))
    finally:await server.close()


@pytest.mark.anyio
async def test_ready_label_requires_successful_publish_receipt(imported):
    media=FakeMedia();target=helper(imported,media)
    with imported['store']._write() as db:db.execute("UPDATE jobs SET status='queued' WHERE stage='publish'")
    with pytest.raises(DerivativeError,match='publication'):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert media.calls==0


@pytest.mark.anyio
@pytest.mark.parametrize('terminal',[True,False])
async def test_heartbeat_completion_race_rechecks_terminal_state(imported,terminal):
    from dataclasses import replace
    from source_media import MediaError
    media=FakeMedia();media.gate.clear();target=helper(imported,media)
    target.limits=replace(target.limits,heartbeat_seconds=.01)
    async def heartbeat(*args,**kwargs):
        if terminal:media.gate.set()
        raise MediaError('media_lease_expired')
    media.heartbeat=heartbeat
    if terminal:
        result=await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
        assert result.files and media.calls==1
    else:
        with pytest.raises(DerivativeError):await target.derive(imported['row'],'detail_video_v1',owner_principal_id='owner')
    assert not media.alive and media.releases==1


@pytest.mark.anyio
async def test_artifact_quota_rejects_before_copy_and_cleans_worker(imported):
    from dataclasses import replace
    media=FakeMedia();target=helper(imported,media)
    target.limits=replace(target.limits,artifact_storage_bytes=1)
    with pytest.raises(DerivativeError):await target.derive(imported['row'],'source_cut_v1',owner_principal_id='owner')
    assert not media.alive and media.releases==1


@pytest.mark.anyio
@pytest.mark.parametrize('samples', [[0,0],[-1],[4],[0,1,2,3],[float('nan')]])
async def test_invalid_sample_request_never_admits(imported,samples):
    media=FakeMedia();target=helper(imported,media)
    with pytest.raises(DerivativeError):await target.derive(imported['row'],'inspect_frames_v1',owner_principal_id='owner',frame_seconds=samples)
    assert media.calls==0
