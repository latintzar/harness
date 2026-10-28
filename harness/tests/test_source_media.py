import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import source_media as media
from source_media import MediaBroker, MediaClient, MediaScope, MediaLease, MediaError, MediaServer

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.fixture
def scope():
    return MediaScope('company', 'owner', 'asset-' + 'a'*32, 'job-' + 'b'*32)


def sha(value):
    return hashlib.sha256(value).hexdigest()


class FakeBackend:
    def __init__(self, root):
        self.root = root
        self.gate = asyncio.Event()
        self.gate.set()
        self.stop_gate = asyncio.Event(); self.stop_gate.set()
        self.stop_started = asyncio.Event()
        self.started = asyncio.Event()
        self.alive = False
        self.stop_failure = False
        self.unmount_failure = False
        self.start_failure = False
        self.start_count = 0
        self.mutate = None

    def volume_path(self, key, generation):
        return self.root / f'{key}-{generation}' / 'mount'

    async def mount(self, spec):
        spec.output.mkdir(parents=True)

    async def start(self, spec):
        self.alive = True; self.start_count += 1; self.started.set()
        if self.start_failure: raise RuntimeError('created_then_start_failed')

    async def wait(self, spec):
        await self.gate.wait()
        prepared = spec.output / 'prepared'; prepared.mkdir()
        clip, thumb = b'opaque-mp4-from-isolated-worker', b'opaque-jpeg-from-isolated-worker'
        frames = {'time_base':'1/24','frames':[{'pts':i,'duration_ticks':1} for i in range(24)]}
        timing = json.dumps(frames).encode()
        original = spec.original.read_bytes()
        manifest = {'schema_version':1,'recipe':media.RECIPE,'asset_id':spec.asset_id,'collection':spec.collection,
            'original':{'sha256':sha(original),'size_bytes':len(original)},
            'source':{'capture_utc':None,'capture_clock':'unknown','duration_seconds':1.,'origin_seconds':0.,
                'origin_pts':0,'frame_count':24,'time_base':'1/24'},
            'normalization':{'fps':24,'window_seconds':media.WINDOW_SECONDS,'rounding_bound_seconds':1/24,'rotation_applied':True,
                'mapping':'Prepared elapsed time maps to source presentation timestamps.'},
            'frame_timing':{'file':'frame-timing.json','sha256':sha(timing)},
            'clips':[{'id':spec.asset_id+'-clip-000000','asset_id':spec.asset_id,'collection':spec.collection,
                'file':'clip-000000.mp4','sha256':sha(clip),'size_bytes':len(clip),'duration_seconds':1.,
                'width':640,'height':360,'source_elapsed_start_seconds':0.,'source_elapsed_end_seconds':1.,
                'original_pts_start_seconds':0.,'original_pts_end_seconds':1.,
                'thumbnail':{'file':'clip-000000.jpg','sha256':sha(thumb),'size_bytes':len(thumb)}}],
            'embedding_batches':[[spec.asset_id+'-clip-000000']]}
        for name, value in {'clip-000000.mp4':clip,'clip-000000.jpg':thumb,'frame-timing.json':timing}.items():
            (prepared/name).write_bytes(value)
        if self.mutate: self.mutate(prepared, manifest)
        (prepared/'manifest.json').write_text(json.dumps(manifest))
        return 0

    async def stop(self, spec):
        self.stop_started.set(); await self.stop_gate.wait()
        if self.stop_failure: raise RuntimeError('docker_unavailable')
        self.alive = False

    async def unmount(self, spec):
        if self.unmount_failure: raise RuntimeError('mounted_busy')
        if spec.volume.parent.exists():
            import shutil
            shutil.rmtree(spec.volume.parent)


@pytest.fixture
def original(tmp_path):
    path = tmp_path/'sealed-source'; path.write_bytes(b'trusted-synthetic-original'); path.chmod(0o400)
    return path


def broker(tmp_path, backend):
    return MediaBroker(tmp_path/'broker', backend, app_uid=os.geteuid(), app_gid=os.getegid(), worker_uid=os.geteuid())


async def submit(target, scope, original, **override):
    values = dict(generation=1, collection='cafe', original_bytes=original.stat().st_size,
        original_sha256=sha(original.read_bytes()), expires_at=time.time()+60)
    values.update(override)
    with original.open('rb') as stream:
        return await target.submit(scope, original_fd=stream.fileno(), **values)


async def terminal(target, scope, lease):
    for _ in range(200):
        value = await target.status(scope, lease)
        if value['status'] not in {'starting','running'}: return value
        await asyncio.sleep(.01)
    raise AssertionError('job did not finish')


def test_scope_rejects_path_identity():
    with pytest.raises(MediaError): MediaScope('company','owner','../../secret','job-'+'b'*32)


def test_volume_size_is_instance_local(tmp_path):
    from native_executor import Ext4Volumes, VOLUME_BYTES
    a, b = Ext4Volumes(tmp_path/'native'), Ext4Volumes(tmp_path/'media',size_bytes=media.OUTPUT_BYTES)
    assert a.size_bytes == VOLUME_BYTES == 256*1024**2
    assert b.size_bytes == 512*1024**2
    with pytest.raises(RuntimeError): Ext4Volumes(tmp_path/'bad',size_bytes=True)


def test_docker_profile_is_fixed(tmp_path, scope):
    backend = media.MediaDocker(tmp_path)
    spec = media.MediaSpec(scope.key,1,scope.asset_id,'cafe',tmp_path/'job',tmp_path/'volume')
    flags = backend.flags(spec)
    for flag,value in [('--runtime','rocoto-runsc'),('--network','none'),('--cpus','2'),('--memory','1073741824'),
                       ('--memory-swap','1073741824'),('--pids-limit','64'),('--user','10001:10001')]:
        assert flags[flags.index(flag)+1] == value
    assert '--init' in flags and '--read-only' in flags
    assert sum(value=='--mount' for value in flags)==2
    assert 'readonly' in next(v for v in flags if 'dst=/input/original' in v)
    assert all('codex' not in v and 'socket' not in v and 'env' not in v for v in flags)


@pytest.mark.anyio
async def test_success_preserves_exact_hashes_and_timing(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original)
    value=await terminal(target,scope,lease)
    assert value['status']=='succeeded' and value['cleanup_confirmed'] is True
    assert not backend.alive
    result=value['result']; assert result['image_digest']==media.IMAGE and result['scope']==asdict(scope)
    for entry in result['files']:
        stream,metadata=await target.output_file(scope,lease,entry['index'])
        with stream: data=stream.read()
        assert len(data)==entry['size_bytes'] and sha(data)==entry['sha256']
        if entry['path']=='manifest.json': assert json.loads(data)==result['manifest']
    assert result['manifest']['source']['capture_utc'] is None
    assert original.read_bytes()==b'trusted-synthetic-original'
    await target.close()


@pytest.mark.anyio
async def test_retry_and_changed_generation_fencing(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original)
    assert await submit(target,scope,original)==lease
    with pytest.raises(MediaError,match='changed_submission'): await submit(target,scope,original,collection='other')
    with pytest.raises(MediaError,match='prior_worker_unconfirmed'): await submit(target,scope,original,generation=2)
    stopped=await target.stop(scope,lease)
    assert stopped['cleanup_confirmed']
    await target.release(scope,lease)
    second=await submit(target,scope,original,generation=2)
    with pytest.raises(MediaError,match='stale_media_lease'): await target.status(scope,lease)
    with pytest.raises(MediaError,match='stale_media_lease'): await submit(target,scope,original,generation=1)
    await target.stop(scope,second); await target.close()


@pytest.mark.anyio
async def test_stop_admission_race_never_claims_false_success(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); backend.stop_gate.clear()
    target=broker(tmp_path,backend); lease=await submit(target,scope,original)
    stopping=asyncio.create_task(target.stop(scope,lease))
    await backend.stop_started.wait(); await asyncio.sleep(.02)
    assert not stopping.done()
    backend.stop_gate.set(); value=await stopping
    assert value['cleanup_confirmed'] and not backend.alive
    await target.close()


@pytest.mark.anyio
async def test_stop_failure_fences_new_generation(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); backend.stop_failure=True
    target=broker(tmp_path,backend); lease=await submit(target,scope,original)
    await backend.started.wait()
    with pytest.raises(MediaError,match='media_cleanup_unconfirmed'): await target.stop(scope,lease)
    value=await target.status(scope,lease)
    assert value['status']=='uncertain' and not value['cleanup_confirmed']
    with pytest.raises(MediaError,match='prior_worker_unconfirmed'): await submit(target,scope,original,generation=2)
    backend.stop_failure=False
    assert (await target.stop(scope,lease))['cleanup_confirmed']
    await target.close()


@pytest.mark.anyio
async def test_partial_docker_start_is_removed(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.start_failure=True; target=broker(tmp_path,backend)
    lease=await submit(target,scope,original); value=await terminal(target,scope,lease)
    assert value['status']=='failed' and value['cleanup_confirmed'] and not backend.alive
    await target.close()


@pytest.mark.anyio
async def test_unmount_failure_is_not_completion(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.unmount_failure=True; target=broker(tmp_path,backend)
    lease=await submit(target,scope,original); value=await terminal(target,scope,lease)
    assert value['status']=='uncertain' and not value['cleanup_confirmed'] and value['result'] is None
    backend.unmount_failure=False; await target.close()


@pytest.mark.anyio
async def test_expired_lease_stops_worker(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original,expires_at=time.time()+.05)
    value=await terminal(target,scope,lease)
    assert value['status']=='failed' and value['cleanup_confirmed'] and not backend.alive
    with pytest.raises(MediaError,match='media_lease_expired'):
        await target.heartbeat(scope,lease,expires_at=time.time()+60)
    await target.close()


@pytest.mark.anyio
async def test_wall_limit_not_extended_by_heartbeat(tmp_path,scope,original,monkeypatch):
    monkeypatch.setattr(media,'WALL_SECONDS',.05)
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original)
    await target.heartbeat(scope,lease,expires_at=time.time()+80)
    value=await terminal(target,scope,lease)
    assert value['error']=='media_wall_timeout' and value['cleanup_confirmed']
    await target.close()


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['checksum','writable','hardlink','size','too_large','wrong_owner','write_fd'])
async def test_invalid_original_never_launches(tmp_path,scope,original,kind):
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    options={}
    if kind=='checksum': options['original_sha256']='0'*64
    if kind=='writable': original.chmod(0o600)
    if kind=='hardlink': os.link(original,tmp_path/'link')
    if kind=='size': options['original_bytes']=original.stat().st_size+1
    if kind=='too_large': options['original_bytes']=media.INPUT_BYTES+1
    if kind=='wrong_owner': target.app_uid+=1
    with pytest.raises(MediaError):
        if kind=='write_fd':
            original.chmod(0o600)
            with original.open('r+b') as stream:
                original.chmod(0o400)
                await target.submit(scope,generation=1,collection='cafe',original_bytes=original.stat().st_size,
                    original_sha256=sha(original.read_bytes()),expires_at=time.time()+60,original_fd=stream.fileno())
        else: await submit(target,scope,original,**options)
    assert backend.start_count==0
    await target.close()


def mutation(kind):
    def mutate(prepared, manifest):
        if kind=='traversal': manifest['clips'][0]['file']='../../secret'
        elif kind=='hash': manifest['clips'][0]['sha256']='0'*64
        elif kind=='asset': manifest['asset_id']='asset-'+'f'*32
        elif kind=='collection': manifest['collection']='other'
        elif kind=='source_hash': manifest['original']['sha256']='0'*64
        elif kind=='capture_utc': manifest['source']['capture_utc']='2026-01-01T00:00:00Z'
        elif kind=='time_mapping': manifest['clips'][0]['original_pts_end_seconds']=2
        elif kind=='duration': manifest['clips'][0]['duration_seconds']=11
        elif kind=='extra_file': (prepared/'secrets.txt').write_text('invalid')
        elif kind=='symlink': (prepared/'clip-000000.mp4').unlink(); (prepared/'clip-000000.mp4').symlink_to('/etc/passwd')
        elif kind=='hardlink': os.link(prepared/'clip-000000.mp4',prepared/'clip-000001.mp4')
        elif kind=='fifo': (prepared/'clip-000000.mp4').unlink(); os.mkfifo(prepared/'clip-000000.mp4')
        elif kind=='oversize':
            with (prepared/'clip-000000.mp4').open('r+b') as f: f.truncate(media.CLIP_BYTES+1)
        elif kind=='timing':
            payload={'time_base':'1/24','frames':[{'pts':0,'duration_ticks':1}]*24}
            data=json.dumps(payload).encode(); (prepared/'frame-timing.json').write_bytes(data)
            manifest['frame_timing']['sha256']=sha(data)
        elif kind=='batches': manifest['embedding_batches']=[['another-asset']]
    return mutate


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['traversal','hash','asset','collection','source_hash','capture_utc','time_mapping',
    'duration','extra_file','symlink','hardlink','fifo','oversize','timing','batches'])
async def test_malicious_output_is_never_published(tmp_path,scope,original,kind):
    backend=FakeBackend(tmp_path/'volumes'); backend.mutate=mutation(kind); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original); value=await terminal(target,scope,lease)
    assert value['status']=='failed' and value['cleanup_confirmed'] and value['result'] is None
    with pytest.raises(MediaError,match='output_unavailable'): await target.output_file(scope,lease,0)
    await target.close()


@pytest.mark.anyio
async def test_company_owner_token_isolation(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original); await terminal(target,scope,lease)
    for other in [MediaScope('other',scope.owner_id,scope.asset_id,scope.job_id),
                  MediaScope(scope.company_id,'other',scope.asset_id,scope.job_id)]:
        with pytest.raises(MediaError,match='stale_media_lease'): await target.status(other,lease)
    with pytest.raises(MediaError,match='stale_media_lease'):
        await target.output_file(scope,MediaLease(scope.key,1,'0'*48),0)
    await target.close()


@pytest.mark.anyio
async def test_restart_recovers_and_confirms_worker_removal(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original); await backend.started.wait()
    # Simulate crash after persisting running: cancel coroutine while cleanup is unavailable.
    backend.stop_failure=True; await target.close()
    recovered=broker(tmp_path,backend)
    await recovered.recover(); assert (await recovered.status(scope,lease))['status']=='uncertain'
    backend.stop_failure=False; await recovered.recover()
    assert (await recovered.status(scope,lease))['cleanup_confirmed'] and not backend.alive
    assert await submit(recovered,scope,original)==lease
    await recovered.close()


@pytest.mark.anyio
async def test_seqpacket_actual_descriptor_roundtrip(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    server=MediaServer(target,tmp_path/'control.sock'); await server.start()
    client=MediaClient(server.path,server_uid=os.geteuid())
    try:
        lease=await submit(client,scope,original); value=await terminal(client,scope,lease)
        assert value['status']=='succeeded'
        assert await submit(client,scope,original)==lease
        for entry in value['result']['files']:
            data=b''.join([chunk async for chunk in client.read_output(scope,lease,entry['index'])])
            assert sha(data)==entry['sha256']
        assert (await client.stop(scope,lease))['cleanup_confirmed']
        assert (await client.release(scope,lease))['status']=='released'
    finally: await server.close()


@pytest.mark.anyio
async def test_real_peer_uid_denied(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend); target.app_uid+=1
    server=MediaServer(target,tmp_path/'control.sock'); await server.start()
    client=MediaClient(server.path,server_uid=os.geteuid())
    try:
        with pytest.raises((MediaError,ConnectionResetError),match='media_access_denied|reset'):
            await submit(client,scope,original)
        assert backend.start_count==0
    finally: await server.close()


@pytest.mark.anyio
async def test_read_output_detects_later_corruption(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    server=MediaServer(target,tmp_path/'control.sock'); await server.start()
    client=MediaClient(server.path,server_uid=os.geteuid())
    try:
        lease=await submit(client,scope,original); value=await terminal(client,scope,lease)
        entry=value['result']['files'][0]
        path=target._spec(target._row(scope.key)).directory/'sealed'/str(entry['index'])
        path.chmod(0o600); path.write_bytes(b'x'*entry['size_bytes']); path.chmod(0o400)
        with pytest.raises(MediaError,match='output_integrity_failed'):
            _=b''.join([chunk async for chunk in client.read_output(scope,lease,entry['index'])])
    finally: await server.close()


@pytest.mark.anyio
async def test_recovery_lookup_after_source_expiry_needs_no_descriptor(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); target=broker(tmp_path,backend)
    server=MediaServer(target,tmp_path/'control.sock'); await server.start()
    client=MediaClient(server.path,server_uid=os.geteuid())
    try:
        assert await client.recover(scope,1) is None
        lease=await submit(client,scope,original,expires_at=time.time()+.05)
        await terminal(client,scope,lease)
        original.unlink()
        assert await client.recover(scope,1)==lease
        assert (await client.stop(scope,lease))['cleanup_confirmed']
        with pytest.raises(MediaError,match='stale_media_lease'): await client.recover(scope,2)
    finally: await server.close()


@pytest.mark.anyio
async def test_release_required_before_reusing_retained_slot(tmp_path,scope,original,monkeypatch):
    monkeypatch.setattr(media,'MAX_RETAINED',1)
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original); await terminal(target,scope,lease)
    other=MediaScope('company','owner','asset-'+'c'*32,'job-'+'d'*32)
    with pytest.raises(MediaError,match='media_retention_limit'): await submit(target,other,original)
    with pytest.raises(MediaError,match='release_previous_output'): await submit(target,scope,original,generation=2)
    await target.release(scope,lease)
    new=await submit(target,other,original); await target.stop(other,new); await target.close()


@pytest.mark.anyio
async def test_active_capacity_is_bounded(tmp_path,scope,original,monkeypatch):
    monkeypatch.setattr(media,'MAX_ACTIVE',1)
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original)
    other=MediaScope('company','owner','asset-'+'c'*32,'job-'+'d'*32)
    with pytest.raises(MediaError,match='media_capacity'): await submit(target,other,original)
    await target.stop(scope,lease); await target.close()


@pytest.mark.anyio
async def test_expiry_during_copy_discards_all_results(tmp_path,scope,original,monkeypatch):
    import threading
    entered,finish=threading.Event(),threading.Event()
    actual=media._snapshot
    def paused(*args,**kwargs):
        entered.set(); finish.wait(timeout=2); return actual(*args,**kwargs)
    monkeypatch.setattr(media,'_snapshot',paused)
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original)
    await asyncio.to_thread(entered.wait,2)
    target.clock=lambda: time.time()+120
    finish.set(); value=await terminal(target,scope,lease)
    assert value['status']=='failed' and value['cleanup_confirmed'] and value['result'] is None
    assert not (target._spec(target._row(scope.key)).directory/'sealed').exists()
    await target.close()


@pytest.mark.anyio
async def test_socket_rejects_extra_command_mount_or_image(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    server=MediaServer(target,tmp_path/'control.sock'); await server.start()
    client=MediaClient(server.path,server_uid=os.geteuid())
    try:
        for key in ('command','image','mount','source_path','url'):
            with original.open('rb') as source:
                with pytest.raises(MediaError,match='invalid_request'):
                    await client.submit(scope,generation=1,collection='cafe',original_bytes=original.stat().st_size,
                        original_sha256=sha(original.read_bytes()),expires_at=time.time()+60,
                        original_fd=source.fileno(),**{key:'/etc/passwd'})
        assert backend.start_count==0
    finally: await server.close()


@pytest.mark.anyio
async def test_duplicate_broker_cannot_remove_live_socket(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    first=MediaServer(target,tmp_path/'control.sock'); await first.start()
    duplicate_broker=broker(tmp_path,backend)
    second=MediaServer(duplicate_broker,tmp_path/'control.sock')
    try:
        with pytest.raises(MediaError,match='broker_already_running'): await second.start()
        await second.close()
        client=MediaClient(first.path,server_uid=os.geteuid())
        lease=await submit(client,scope,original)
        assert (await terminal(client,scope,lease))['status']=='succeeded'
    finally:
        duplicate_broker.db.close()
        await first.close()


@pytest.mark.anyio
async def test_shutdown_waits_until_actual_stop(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes'); backend.gate.clear(); backend.stop_gate.clear()
    target=broker(tmp_path,backend); lease=await submit(target,scope,original)
    await backend.started.wait()
    closing=asyncio.create_task(target.close())
    await backend.stop_started.wait(); await asyncio.sleep(.01)
    assert not closing.done()
    backend.stop_gate.set(); await closing
    assert not backend.alive


@pytest.mark.anyio
async def test_expiry_cancels_blocked_startup(tmp_path,scope,original):
    backend=FakeBackend(tmp_path/'volumes')
    entered=asyncio.Event()
    async def blocked_mount(spec):
        entered.set(); await asyncio.Event().wait()
    backend.mount=blocked_mount
    target=broker(tmp_path,backend)
    lease=await submit(target,scope,original,expires_at=time.time()+.05)
    await entered.wait(); value=await terminal(target,scope,lease)
    assert value['status']=='failed' and value['error']=='media_lease_expired' and value['cleanup_confirmed']
    assert backend.start_count==0
    await target.close()


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['old_recipe','old_window','nonfinite_metadata','total_output'])
async def test_fixed_v2_profile_and_global_output_budget(tmp_path,scope,original,kind,monkeypatch):
    backend=FakeBackend(tmp_path/'volumes')
    def change(prepared, manifest):
        if kind=='old_recipe': manifest['recipe']='rocoto-cfr24-h264-segments-v1'
        elif kind=='old_window': manifest['normalization']['window_seconds']=10
        elif kind=='nonfinite_metadata': manifest['optional_untrusted_metadata']=float('nan')
    backend.mutate=change
    if kind=='total_output': monkeypatch.setattr(media,'OUTPUT_BYTES',40)
    target=broker(tmp_path,backend)
    lease=await submit(target,scope,original); value=await terminal(target,scope,lease)
    assert value['status']=='failed' and value['cleanup_confirmed'] and value['result'] is None
    await target.close()


@pytest.mark.anyio
async def test_repeated_stop_waits_for_private_copy_drain(tmp_path,scope,original,monkeypatch):
    import threading
    entered,finish=threading.Event(),threading.Event()
    actual=media._snapshot
    def paused(*args,**kwargs):
        entered.set(); finish.wait(timeout=2); return actual(*args,**kwargs)
    monkeypatch.setattr(media,'_snapshot',paused)
    backend=FakeBackend(tmp_path/'volumes'); target=broker(tmp_path,backend)
    lease=await submit(target,scope,original)
    await asyncio.to_thread(entered.wait,2)
    first=asyncio.create_task(target.stop(scope,lease)); await asyncio.sleep(.01)
    second=asyncio.create_task(target.stop(scope,lease)); await asyncio.sleep(.02)
    try:
        assert not first.done() and not second.done(), 'Stop returned while a private copy still held output descriptors'
    finally:
        finish.set()
        values=await asyncio.gather(first,second)
        await target.close()
    assert all(v['cleanup_confirmed'] and v['result'] is None for v in values)
