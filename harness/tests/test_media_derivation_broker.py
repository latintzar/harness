"""Host broker treats both video and JPEG worker output as bounded opaque bytes."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
import source_media as media
from test_source_media import FakeBackend,broker,scope,original,terminal,anyio_backend


def sha(value):return hashlib.sha256(value).hexdigest()


def request(scope,purpose='detail_video_v1'):
    return dict(purpose=purpose,clip_id=scope.asset_id+'-clip-000000',start=0.,end=1.,
        frame_seconds=[.1,.5,.9] if purpose=='inspect_frames_v1' else [])


def derived_manifest(spec,original):
    d=spec.derivation;scope=media.MediaScope('company','owner',spec.asset_id,'job-'+'b'*32)
    req=d.manifest_request(scope,spec.collection,sha(original))
    frames=[dict(source_frame_index=i,original_pts=i,original_pts_seconds=i/24,
        source_elapsed_seconds=i/24,duration_seconds=1/24) for i in range(24)]
    output=[];data={}
    if d.purpose=='inspect_frames_v1':
        for number,stamp in enumerate(d.frame_seconds):
            content=b'opaque-JPEG-'+str(number).encode();name=f'frame-{number:02d}.jpg';data[name]=content
            output.append(dict(file=name,sha256=sha(content),size_bytes=len(content),mime_type='image/jpeg',width=640,height=360,
                requested_clip_seconds=stamp,requested_source_seconds=stamp,**frames[int(stamp*24)]))
    else:
        content=b'opaque-MP4';data['clip.mp4']=content
        detail=d.purpose=='detail_video_v1';count=4 if detail else 24;fps=4 if detail else 24
        output.append(dict(file='clip.mp4',sha256=sha(content),size_bytes=len(content),mime_type='video/mp4',video_codec='h264',
            width=640,height=360,average_frame_rate=f'{fps}/1',nominal_frame_rate=f'{fps}/1',duration_seconds=1.,frame_count=count,
            start_time_seconds=0.,audio_codecs=[],audio_start_seconds=[],time_base='1/24',frame_pts_seconds=[i/fps for i in range(count)],
            source_frames=frames,actual_source_first_frame_seconds=0.,actual_source_end_seconds=1.,actual_original_first_frame_pts=0,
            requested_interval_is_frame_aligned=True,rotation_applied=True,
            mapping_basis='nominal_source_elapsed_4fps_resampling' if detail else 'preserved_source_frame_pts',
            resampling_rounding_bound_seconds=.25 if detail else 0))
    manifest=dict(schema_version=1,profile=media.DERIVE_PROFILE,recipe=media.DERIVE_RECIPE,purpose=d.purpose,
        asset_id=spec.asset_id,collection=spec.collection,clip_id=d.clip_id,
        original={'sha256':sha(original),'size_bytes':len(original)},request=req,
        request_sha256=sha(media._json(req).encode()),source=dict(capture_utc=None,capture_clock='unknown',duration_seconds=4.,
            origin_seconds=0.,origin_pts=0,frame_count=96,time_base='1/24',coded_width=640,coded_height=360,rotation_degrees=0),
        clock='source_relative',capture_utc=None,files=output,requested_original_pts_start_seconds=0.,requested_original_pts_end_seconds=1.)
    return manifest,data


class DerivedBackend(FakeBackend):
    async def wait(self,spec):
        if not spec.derivation:return await super().wait(spec)
        await self.gate.wait()
        prepared=spec.output/'derived';prepared.mkdir()
        manifest,data=derived_manifest(spec,spec.original.read_bytes())
        for name,content in data.items():(prepared/name).write_bytes(content)
        if self.mutate:self.mutate(prepared,manifest)
        (prepared/'manifest.json').write_text(json.dumps(manifest))
        return 0


async def admit(target,scope,original,derivation=None,**overrides):
    values=dict(generation=1,collection='cafe',original_bytes=original.stat().st_size,
        original_sha256=sha(original.read_bytes()),expires_at=time.time()+60,
        derivation=derivation or request(scope))
    values.update(overrides)
    with original.open('rb') as stream:return await target.derive(scope,original_fd=stream.fileno(),**values)


@pytest.mark.anyio
@pytest.mark.parametrize('purpose',sorted(media.DERIVE_PURPOSES))
async def test_fixed_derive_scope_and_result_preserve_original(tmp_path,scope,original,purpose):
    backend=DerivedBackend(tmp_path/'volumes');target=broker(tmp_path,backend)
    lease=await admit(target,scope,original,request(scope,purpose));status=await terminal(target,scope,lease)
    assert status['status']=='succeeded' and status['cleanup_confirmed']
    result=status['result'];assert result['purpose']==purpose and result['image_digest']==media.DERIVE_IMAGE
    assert result['profile']==media.DERIVE_PROFILE and result['scope']==asdict(scope)
    for file in result['files']:
        stream,_=await target.output_file(scope,lease,file['index'])
        with stream:assert sha(stream.read())==file['sha256']
    assert original.read_bytes()==b'trusted-synthetic-original' and not backend.alive
    await target.release(scope,lease);await target.close()


@pytest.mark.anyio
async def test_real_unix_fd_client_roundtrip_and_opaque_download(tmp_path,scope,original):
    backend=DerivedBackend(tmp_path/'volumes');target=broker(tmp_path,backend)
    server=media.MediaServer(target,tmp_path/'media.sock');await server.start()
    try:
        client=media.MediaClient(tmp_path/'media.sock',server_uid=os.geteuid())
        lease=await admit(client,scope,original,request(scope,'inspect_frames_v1'))
        status=await terminal(client,scope,lease);assert status['status']=='succeeded'
        entry=next(x for x in status['result']['files'] if x['path']=='frame-00.jpg')
        data=b''.join([chunk async for chunk in client.read_output(scope,lease,entry['index'])])
        assert data==b'opaque-JPEG-0'
        await client.release(scope,lease)
    finally:await server.close()


@pytest.mark.anyio
async def test_request_is_part_of_retry_identity_and_scope_is_fenced(tmp_path,scope,original):
    backend=DerivedBackend(tmp_path/'volumes');backend.gate.clear();target=broker(tmp_path,backend)
    lease=await admit(target,scope,original)
    assert await admit(target,scope,original)==lease
    for change in [{'purpose':'source_cut_v1'},{'clip_id':'other'},{'end':.75}]:
        with pytest.raises(media.MediaError):await admit(target,scope,original,request(scope)|change)
    other=media.MediaScope('other','owner',scope.asset_id,scope.job_id)
    with pytest.raises(media.MediaError):await target.status(other,lease)
    with pytest.raises(media.MediaError):await admit(target,scope,original,generation=2)
    await target.stop(scope,lease);await target.release(scope,lease)
    with pytest.raises(media.MediaError):await admit(target,scope,original,generation=0)
    await target.close()


@pytest.mark.anyio
async def test_stop_during_worker_blocks_output_until_actual_absence(tmp_path,scope,original):
    backend=DerivedBackend(tmp_path/'volumes');backend.gate.clear();backend.stop_gate.clear()
    target=broker(tmp_path,backend);lease=await admit(target,scope,original)
    await backend.started.wait();stopping=asyncio.create_task(target.stop(scope,lease));await backend.stop_started.wait()
    assert backend.alive and not stopping.done()
    with pytest.raises(media.MediaError):await target.output_file(scope,lease,0)
    backend.stop_gate.set();status=await stopping
    assert status['cleanup_confirmed'] and not backend.alive
    await target.close()


@pytest.mark.parametrize('changed',[
    {'purpose':'arbitrary'}, {'image':'untrusted'}, {'start':True}, {'end':float('nan')},
    {'start':-1}, {'end':9}, {'clip_id':'../../file'},
    {'frame_seconds':[0]}, {'purpose':'inspect_frames_v1','frame_seconds':[0,1,2,3]},
    {'purpose':'inspect_frames_v1','frame_seconds':[.5,.1]},
    {'purpose':'inspect_frames_v1','frame_seconds':[1.]}, {'purpose':'inspect_frames_v1','frame_seconds':[]},
])
def test_exact_typed_request_rejects_options_and_invalid_bounds(scope,changed):
    with pytest.raises(media.MediaError):media.DerivationRequest.parse(request(scope)|changed)


@pytest.mark.anyio
@pytest.mark.parametrize('mutation',[
    lambda d,m:m.update(purpose='source_cut_v1'),
    lambda d,m:m.update(asset_id='asset-'+'f'*32),
    lambda d,m:m.update(recipe='other'),
    lambda d,m:m['request'].update(source_elapsed_start_seconds=.1),
    lambda d,m:m['files'][0].update(sha256='a'*64),
    lambda d,m:m['files'][0].update(width=3840),
    lambda d,m:m['files'][0].update(frame_count=10000),
    lambda d,m:m['files'][0].update(frame_pts_seconds=[0,1,2,3]),
    lambda d,m:m['files'][0].update(audio_codecs=['aac']),
    lambda d,m:m['files'][0]['source_frames'][0].update(original_pts=999),
    lambda d,m:(d/'outside.txt').write_bytes(b'worker output outside contract'),
])
async def test_worker_claims_and_files_are_checked_before_readable(tmp_path,scope,original,mutation):
    backend=DerivedBackend(tmp_path/'volumes');backend.mutate=mutation;target=broker(tmp_path,backend)
    lease=await admit(target,scope,original);status=await terminal(target,scope,lease)
    assert status['status']=='failed' and status['result'] is None and status['cleanup_confirmed']
    with pytest.raises(media.MediaError):await target.output_file(scope,lease,0)
    await target.close()


def test_both_images_and_cli_arguments_are_fixed(tmp_path,scope):
    legacy=media.MediaSpec(scope.key,1,scope.asset_id,'cafe',tmp_path/'job',tmp_path/'volume')
    derived=media.MediaSpec(scope.key,1,scope.asset_id,'cafe',tmp_path/'job',tmp_path/'volume',media.DerivationRequest.parse(request(scope)))
    assert legacy.image==media.IMAGE and derived.image==media.DERIVE_IMAGE
    assert legacy.arguments==['--asset-id',scope.asset_id,'--collection','cafe']
    assert '--purpose' in derived.arguments and '--start' in derived.arguments
    assert '--input' not in derived.arguments and '--command' not in derived.arguments


@pytest.mark.anyio
async def test_additive_request_table_keeps_legacy_jobs_schema(tmp_path,scope,original):
    backend=DerivedBackend(tmp_path/'volumes');target=broker(tmp_path,backend)
    lease=await admit(target,scope,original);await terminal(target,scope,lease);await target.release(scope,lease)
    assert len(target.db.execute('PRAGMA table_info(jobs)').fetchall())==13
    saved=target.db.execute('SELECT body FROM job_derivations WHERE key=?',(scope.key,)).fetchone()[0]
    assert json.loads(saved)==request(scope)
    await target.close()
    # Reopening preserves the exact request fence across a broker restart.
    reopened=broker(tmp_path,backend)
    assert (await reopened.status(scope,lease))['status']=='released'
    with pytest.raises(media.MediaError,match='changed_submission'):
        await admit(reopened,scope,original,request(scope,'source_cut_v1'))
    await reopened.close()


@pytest.mark.anyio
async def test_source_cut_larger_than_legacy_transport_limit(tmp_path,scope,original):
    backend=DerivedBackend(tmp_path/'volumes')
    def large(directory,manifest):
        data=b'fixed opaque derived bytes'*((33*1024**2)//26+1)
        (directory/'clip.mp4').write_bytes(data)
        manifest['files'][0].update(size_bytes=len(data),sha256=sha(data))
    backend.mutate=large;target=broker(tmp_path,backend)
    server=media.MediaServer(target,tmp_path/'media.sock');await server.start()
    try:
        client=media.MediaClient(tmp_path/'media.sock',server_uid=os.geteuid())
        lease=await admit(client,scope,original,request(scope,'source_cut_v1'))
        status=await terminal(client,scope,lease);assert status['status']=='succeeded'
        entry=next(x for x in status['result']['files'] if x['path']=='clip.mp4')
        assert 32*1024**2<entry['size_bytes']<64*1024**2
        received=0;digest=hashlib.sha256()
        async for part in client.read_output(scope,lease,entry['index']):received+=len(part);digest.update(part)
        assert received==entry['size_bytes'] and digest.hexdigest()==entry['sha256']
        await client.release(scope,lease)
    finally:await server.close()
