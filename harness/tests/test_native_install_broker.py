import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
import native_executor as native
import native_install_broker as install
from native_install_client import InstallerClient,BrokerPluginInstaller
from native_install_source import InstallSource
from native_packages import NativePackageStore
from test_native_executor import broker
from test_native_install import archive,Native,SOURCE

@pytest.fixture
def tmp_path():
    import tempfile
    with tempfile.TemporaryDirectory(prefix='ip-') as d:yield Path(d)
@pytest.fixture
def anyio_backend():return 'asyncio'

class Backend:
    def __init__(self,root):self.root=root;self.started=0;self.stopped=0;self.live=False;self.wait=None;self.fail_stop=False;self.entered=asyncio.Event()
    def volume_path(self,key):return self.root/key
    async def run(self,key,incoming):
        self.started+=1;self.live=True;self.entered.set()
        if self.wait:await self.wait.wait()
        workspace=self.volume_path(key)/'workspace';workspace.mkdir(parents=True)
        document=json.loads((incoming/'request.json').read_text())
        install.worker.materialize(incoming/'source.tar.gz',document,workspace,execute=Native())
    async def stop(self,key):
        self.stopped+=1
        if self.fail_stop:raise native.ExecutorError('fixture_unavailable')
        self.live=False
    async def unmount(self,key):
        shutil.rmtree(self.volume_path(key),ignore_errors=True)

async def setup(tmp_path):
    host=broker(tmp_path);backend=Backend(tmp_path/'installer-volumes')
    module=tmp_path/'worker.py';module.write_text('# immutable fixture worker\n');module.chmod(0o644)
    installer=install.Installer(host,allowed_sources=[SOURCE],backend=backend,worker_path=module)
    server=native.BrokerServer(host,tmp_path/'control.sock');await server.start()
    scope=native.NativeScope('company','alice','install-root');client=InstallerClient(server.path)
    path,document=archive(tmp_path)
    return host,backend,installer,server,scope,client,path,document

async def terminal(client,scope,lease):
    async with asyncio.timeout(3):
        while True:
            result=await client.request('plugin_status',scope,lease)
            if result['status'] not in {'prepared','receiving','running'}:return result
            await asyncio.sleep(.01)

@pytest.mark.anyio
async def test_real_ipc_upload_materialize_read_release_and_owner_partition(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,document)
        assert await client.prepare(scope,'a'*32,document)==lease
        with pytest.raises(native.ExecutorError,match='scope_mismatch'):await client.request('plugin_status',replace(scope,owner_id='bob'),lease)
        await client.upload(scope,lease,path,document)
        status=await terminal(client,scope,lease)
        assert status['status']=='succeeded' and status['cleanup_confirmed'] and not backend.live
        result=await client.read(scope,lease,tmp_path/'download',document)
        assert (tmp_path/'download/LICENSE').read_bytes()==b'MIT fixture\n'
        assert len(result['files'])==3
        await client.request('plugin_release',scope,lease)
        assert (await client.request('plugin_status',scope,lease))['status']=='released'
        assert installer.live_count()==0 and not (installer.root/'jobs'/lease['id']).exists()
    finally:await server.close()

@pytest.mark.anyio
async def test_source_and_archive_binding_before_capacity(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        wrong=json.loads(json.dumps(document));wrong['source']['revision']='b'*40
        with pytest.raises(native.ExecutorError,match='uncommissioned'):await client.prepare(scope,'a'*32,wrong)
        assert installer.live_count()==0
        lease=await client.prepare(scope,'a'*32,document)
        wrong=json.loads(json.dumps(document));wrong['archive_sha256']='0'*64
        with pytest.raises(native.ExecutorError,match='changed'):await client.prepare(scope,'a'*32,wrong)
        assert installer.live_count()==1
        await client.request('plugin_release',scope,lease)
    finally:await server.close()

@pytest.mark.anyio
async def test_installer_and_native_workers_share_two_slot_ceiling(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        first=await host.prepare(replace(scope,root_chat_id='ordinary'))
        lease=await client.prepare(scope,'a'*32,document)
        with pytest.raises(native.ExecutorError,match='capacity'):await host.prepare(replace(scope,root_chat_id='third'))
        with pytest.raises(native.ExecutorError,match='capacity'):await client.prepare(scope,'b'*32,document)
        await client.request('plugin_release',scope,lease)
        second=await host.prepare(replace(scope,root_chat_id='third'))
        await host.stop(replace(scope,root_chat_id='ordinary'),first);await host.stop(replace(scope,root_chat_id='third'),second)
    finally:await server.close()

@pytest.mark.anyio
async def test_partial_upload_stop_drains_receiver_and_never_runs(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,document)
        _,reader,writer=await client._connect('plugin_upload',scope,lease=lease)
        await native._write_frame(writer,6,path.read_bytes()[:2])
        await client.request('plugin_stop',scope,lease)
        assert not installer.uploads and backend.started==0 and installer.live_count()==0
        assert await reader.read()==b''
        writer.close();await writer.wait_closed();await client.request('plugin_release',scope,lease)
    finally:await server.close()

@pytest.mark.anyio
async def test_archive_tamper_fences_before_native_execution(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,document);raw=path.read_bytes();path.write_bytes(b'X'+raw[1:])
        with pytest.raises(native.ExecutorError,match='mismatch'):await client.upload(scope,lease,path,document)
        assert installer.live_count()==1 and backend.started==0
        await client.request('plugin_release',scope,lease);assert installer.live_count()==0
    finally:await server.close()

@pytest.mark.anyio
async def test_running_stop_waits_for_whole_worker_and_failed_stop_retries(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path);backend.wait=asyncio.Event()
    try:
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document);await backend.entered.wait()
        backend.fail_stop=True
        with pytest.raises(native.ExecutorError,match='uncertain'):await client.request('plugin_stop',scope,lease)
        assert installer.live_count()==1 and backend.live
        backend.fail_stop=False
        status=await client.request('plugin_release',scope,lease)
        assert status['cleanup_confirmed'] and not backend.live and installer.live_count()==0
    finally:await server.close()

@pytest.mark.anyio
async def test_reconcile_orphan_receiving_and_running_jobs_before_admission(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,document)
        installer._status(lease['id'],'receiving')
        (installer.root/'jobs'/lease['id']/'input/source.tar.gz').write_bytes(b'partial')
        await installer.reconcile()
        assert (await client.request('plugin_status',scope,lease))['cleanup_confirmed']
        assert not (installer.root/'jobs'/lease['id']/'input/source.tar.gz').exists()
        await client.request('plugin_release',scope,lease)
    finally:await server.close()

@pytest.mark.anyio
async def test_store_uses_scoped_materializer_and_cleanup_completes_before_selection(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    async def fetch(spec,destination):
        shutil.copyfile(path,destination)
        return {'source':spec.document(),'sha256':document['archive_sha256'],'size_bytes':document['archive_bytes']}
    state=tmp_path/'company';state.mkdir()
    materializer=BrokerPluginInstaller(state,client=client,fetch=fetch)
    catalogue={'revision':'a'*40,'plugins':[{'id':'notion','supported_api':True,'source_url':'https://github.com/openai/plugins','manifest':{},'catalogue_entry':{'source':{'source':'local','path':'./plugins/notion'}}}]}
    store=NativePackageStore(state,catalogue_snapshot=catalogue,materializer=materializer)
    try:
        result=await store.install('notion',principal_id='alice')
        assert result['installed'] and not result['connected']
        assert not store.list(principal_id='bob')['plugins']
        assert installer.db.execute("select count(*) from jobs where status!='released'").fetchone()[0]==0
        assert not list(state.glob('.plugin-install-*'))
    finally:await server.close()

@pytest.mark.anyio
async def test_materializer_recovers_accepted_lost_prepare_reply_then_releases(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    old=client.prepare;calls=0
    async def lost(*args):
        nonlocal calls
        calls+=1;result=await old(*args)
        if calls==1:raise OSError('fixture lost response')
        return result
    client.prepare=lost
    async def fetch(spec,destination):
        shutil.copyfile(path,destination);return {'sha256':document['archive_sha256'],'size_bytes':document['archive_bytes']}
    state=tmp_path/'company';state.mkdir();adapter=BrokerPluginInstaller(state,client=client,fetch=fetch)
    entry={'id':'notion','supported_api':True,'catalogue_entry':{'source':{'source':'local','path':'./plugins/notion'}}}
    try:
        with pytest.raises(OSError):
            async with adapter.acquire_for_principal(entry,{'revision':'a'*40},principal_id='alice'):pass
        assert calls==2 and backend.started==0 and installer.live_count()==0
        assert installer.db.execute("select count(*) from jobs where status!='released'").fetchone()[0]==0
    finally:await server.close()

@pytest.mark.anyio
async def test_cancelled_materializer_drains_worker_and_keeps_store_empty(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path);backend.wait=asyncio.Event()
    async def fetch(spec,destination):
        shutil.copyfile(path,destination);return {'sha256':document['archive_sha256'],'size_bytes':document['archive_bytes']}
    state=tmp_path/'company';state.mkdir();adapter=BrokerPluginInstaller(state,client=client,fetch=fetch)
    entry={'id':'notion','supported_api':True,'catalogue_entry':{'source':{'source':'local','path':'./plugins/notion'}}}
    async def acquire():
        async with adapter.acquire_for_principal(entry,{'revision':'a'*40},principal_id='alice'):raise AssertionError('unexpected result')
    try:
        task=asyncio.create_task(acquire());await backend.entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert not backend.live and installer.live_count()==0 and not installer.runs
        assert installer.db.execute("select count(*) from jobs where status!='released'").fetchone()[0]==0
    finally:await server.close()

@pytest.mark.anyio
async def test_corrupt_readback_fails_before_package_is_yielded(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document)
        assert (await terminal(client,scope,lease))['status']=='succeeded'
        file=installer.root/'jobs'/lease['id']/'package/LICENSE';file.chmod(0o600);file.write_bytes(b'X'*file.stat().st_size)
        with pytest.raises(native.ExecutorError,match='mismatch'):await client.read(scope,lease,tmp_path/'partial',document)
        await client.request('plugin_release',scope,lease)
        assert not (installer.root/'jobs'/lease['id']).exists()
    finally:await server.close()

@pytest.mark.anyio
async def test_capabilities_are_provider_free_and_slot_free(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        value=await client.capabilities(scope)
        assert value['available'] and value['supported_plugin_ids']==['notion'] and value['image']==native.IMAGE
        assert not installer.live_count() and backend.started==0
    finally:await server.close()

@pytest.mark.anyio
async def test_orphan_output_expiry_and_restart_release_delivery_buffers(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document)
        assert (await terminal(client,scope,lease))['status']=='succeeded'
        installer.db.execute('update jobs set created=0 where id=?',(lease['id'],))
        await installer.expire()
        assert (await client.request('plugin_status',scope,lease))['status']=='released'
        second=await client.prepare(scope,'b'*32,document)
        await installer.reconcile()
        assert (await client.request('plugin_status',scope,second))['status']=='released'
        assert not list((installer.root/'jobs').iterdir())
    finally:await server.close()

@pytest.mark.anyio
async def test_expiry_cleanup_failure_retains_fence_and_retries_without_killing_ticker(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,document)
        installer.db.execute('update jobs set created=0 where id=?',(lease['id'],))
        backend.fail_stop=True
        await host.expire_prepared()
        assert installer._row(lease['id'])['status']=='uncertain' and installer.live_count()==1
        backend.fail_stop=False
        await host.expire_prepared()
        assert installer._row(lease['id'])['status']=='released' and installer.live_count()==0
    finally:await server.close()

@pytest.mark.anyio
async def test_expiry_preserves_active_bounded_read_until_it_finishes(tmp_path,monkeypatch):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    entered=asyncio.Event();resume=asyncio.Event()
    class Sink:
        def write(self,data):pass
        async def drain(self):pass
    sink=Sink();original=install.native.BrokerServer._reply
    async def pause(writer,value):
        if writer is sink:entered.set();await resume.wait()
        else:await original(writer,value)
    try:
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document)
        assert (await terminal(client,scope,lease))['status']=='succeeded'
        monkeypatch.setattr(install.native.BrokerServer,'_reply',pause)
        reader=asyncio.create_task(installer.read(scope,lease,sink));await entered.wait()
        installer.db.execute('update jobs set created=0 where id=?',(lease['id'],))
        await installer.expire()
        assert installer._row(lease['id'])['status']=='succeeded'
        resume.set();await reader
        await installer.expire()
        assert installer._row(lease['id'])['status']=='released'
    finally:resume.set();await server.close()

@pytest.mark.anyio
async def test_release_fences_new_readers_before_draining_existing_reader(tmp_path,monkeypatch):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    entered=asyncio.Event();cancelled=asyncio.Event();drained=asyncio.Event()
    class Sink:
        def write(self,data):pass
        async def drain(self):pass
    sink=Sink();original=install.native.BrokerServer._reply
    async def pause(writer,value):
        if writer is not sink:return await original(writer,value)
        entered.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:cancelled.set();await drained.wait();raise
    try:
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document)
        assert (await terminal(client,scope,lease))['status']=='succeeded'
        monkeypatch.setattr(install.native.BrokerServer,'_reply',pause)
        reader=asyncio.create_task(installer.read(scope,lease,sink));await entered.wait()
        release=asyncio.create_task(installer.release(scope,lease));await cancelled.wait()
        with pytest.raises(native.ExecutorError,match='unavailable'):await installer.read(scope,lease,Sink())
        assert not release.done()
        drained.set();assert (await release)['status']=='released'
        with pytest.raises(asyncio.CancelledError):await reader
    finally:drained.set();await server.close()

@pytest.mark.anyio
async def test_large_valid_inventory_uses_framed_read_not_status_control_reply(tmp_path):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    try:
        path,document=archive(tmp_path,[('.codex-plugin/plugin.json',b'{"name":"notion","version":"0.1.7"}')]+[(f'data/{i:04}.txt',b'x') for i in range(500)])
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document)
        assert (await terminal(client,scope,lease))=={'status':'succeeded','cleanup_confirmed':True}
        assert len(installer._row(lease['id'])['result'].encode())>native.CONTROL_BYTES
        result=await client.read(scope,lease,tmp_path/'download',document)
        assert len(result['files'])==501
        assert (await client.request('plugin_stop',scope,lease))['cleanup_confirmed']
        assert (await client.request('plugin_release',scope,lease))['cleanup_confirmed']
    finally:await server.close()

@pytest.mark.anyio
async def test_slow_freeze_keeps_other_control_requests_responsive(tmp_path,monkeypatch):
    import time
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    original=installer._freeze;delays=[];finished=asyncio.Event()
    def slow(*args):time.sleep(.3);return original(*args)
    monkeypatch.setattr(installer,'_freeze',slow)
    async def heartbeat():
        while not finished.is_set():
            start=time.monotonic();await client.capabilities(scope);await asyncio.sleep(.01)
            delays.append(time.monotonic()-start)
    pulse=asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(.03)
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document)
        assert (await terminal(client,scope,lease))['status']=='succeeded'
        finished.set();await pulse
        assert max(delays)<.2
        await client.request('plugin_release',scope,lease)
    finally:finished.set();await asyncio.gather(pulse,return_exceptions=True);await server.close()

@pytest.mark.anyio
async def test_stop_during_freeze_waits_for_copy_and_suppresses_success(tmp_path,monkeypatch):
    import threading
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    original=installer._freeze;entered=threading.Event();resume=threading.Event()
    def slow(*args):
        entered.set()
        assert resume.wait(3)
        return original(*args)
    monkeypatch.setattr(installer,'_freeze',slow)
    try:
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document)
        assert await asyncio.to_thread(entered.wait,2)
        stop=asyncio.create_task(client.request('plugin_stop',scope,lease))
        async with asyncio.timeout(2):
            while installer._row(lease['id'])['status']!='stopping':await asyncio.sleep(.01)
        await asyncio.sleep(.03)
        assert not stop.done() and backend.volume_path(lease['id']).exists()
        assert (await client.capabilities(scope))['available']
        resume.set();assert (await stop)=={'status':'stopped','cleanup_confirmed':True}
        assert not backend.volume_path(lease['id']).exists()
        assert installer._row(lease['id'])['result'] is None
        await client.request('plugin_release',scope,lease)
        assert not (installer.root/'jobs'/lease['id']).exists()
    finally:resume.set();await server.close()

@pytest.mark.anyio
async def test_stop_during_final_unmount_also_suppresses_success(tmp_path,monkeypatch):
    host,backend,installer,server,scope,client,path,document=await setup(tmp_path)
    original=backend.unmount;entered=asyncio.Event();resume=asyncio.Event()
    async def paused(key):entered.set();await resume.wait();await original(key)
    monkeypatch.setattr(backend,'unmount',paused)
    try:
        lease=await client.prepare(scope,'a'*32,document);await client.upload(scope,lease,path,document);await entered.wait()
        stop=asyncio.create_task(client.request('plugin_stop',scope,lease))
        async with asyncio.timeout(2):
            while installer._row(lease['id'])['status']!='stopping':await asyncio.sleep(.01)
        resume.set();assert (await stop)['status']=='stopped'
        assert installer._row(lease['id'])['result'] is None
        await client.request('plugin_release',scope,lease)
    finally:resume.set();await server.close()
