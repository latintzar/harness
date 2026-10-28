import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
import native_executor as native
import native_package_mount as mount
from native_packages import PackageError
from test_native_executor import broker, sockets, TOKEN
from test_native_packages import package, snapshot, make

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.fixture
def tmp_path():
    import tempfile
    with tempfile.TemporaryDirectory(prefix='npx-') as directory:yield Path(directory)

async def fixture(tmp_path,package,snapshot):
    store,_=make(tmp_path,package,snapshot)
    await store.install('notion',principal_id='alice')
    selected=store.snapshot(principal_id='alice')
    scope=native.NativeScope(selected.company_scope,'alice','chat')
    host=broker(tmp_path)
    server=native.BrokerServer(host,tmp_path/'broker.sock');await server.start()
    return store,selected,scope,host,server,native.NativeClient(server.path)

@pytest.mark.anyio
async def test_actual_protocol_freezes_complete_original_tree_and_explicit_policy(tmp_path,package,snapshot):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    try:
        lease=await client.prepare(scope)
        result=await client.install_packages(scope,lease,selected)
        assert result['plugins']=={'notion@rocoto-pinned-api':{'enabled':True,'mcp_servers':{'notion':{'enabled':False}}}}
        spec=host._spec(host._row(scope.key));tree=spec.packages/mount.MARKETPLACE/'notion/0.1.7'
        assert tree.exists() and tree.stat().st_uid==os.getuid()
        for item in selected.packages[0].files:
            path=tree/item.path
            assert hashlib.sha256(path.read_bytes()).hexdigest()==item.sha256
            assert path.stat().st_mode&0o222==0
        flags=native.DockerBackend.flags(spec)
        assert f'type=bind,src={spec.packages},dst=/codex-state/plugins/cache,readonly' in flags
        listeners=sockets(lease)
        try:
            process=await client.start(scope,lease,TOKEN)
            await process.stop()
        finally:
            for s in listeners:s.close()
        assert not spec.packages.exists()
        assert host.db.execute('select count(*) from package_selections').fetchone()[0]==0
    finally:await server.close()

@pytest.mark.anyio
async def test_owner_company_tamper_never_reserves_stream(tmp_path,package,snapshot):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    try:
        lease=await client.prepare(scope)
        for wrong in [replace(selected,owner_binding='0'*64),replace(selected,company_scope='wrong')]:
            with pytest.raises(mount.PackageMountError):await client.install_packages(scope,lease,wrong)
        assert host._row(scope.key)['status']=='prepared'
        await client.stop(scope,lease)
    finally:await server.close()

@pytest.mark.anyio
async def test_changed_source_hash_stops_and_cleans_up_accepted_upload(tmp_path,package,snapshot):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    path=selected.packages[0].root/'LICENSE';path.chmod(0o600);path.write_bytes(b'X'*path.stat().st_size)
    try:
        lease=await client.prepare(scope)
        with pytest.raises((native.ExecutorError,ConnectionError)):await client.install_packages(scope,lease,selected)
        assert host._row(scope.key)['status']=='stopped'
        assert not host.uploads and not list((host.root/'packages').glob('*'))
    finally:await server.close()

@pytest.mark.anyio
async def test_stop_drains_partial_stream_before_freeing_slot(tmp_path,package,snapshot):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    try:
        lease=await client.prepare(scope)
        _,reader,writer=await client._connect('install_packages',scope,lease)
        await native._write_frame(writer,5,mount.encoded(mount.selection_metadata(selected)))
        await asyncio.sleep(.01)
        assert host._row(scope.key)['status']=='uploading'
        await client.stop(scope,lease)
        assert host._row(scope.key)['status']=='stopped' and not host.uploads
        assert not list((host.root/'packages').glob('*'))
        assert await reader.read()==b''
        writer.close();await writer.wait_closed()
    finally:await server.close()

@pytest.mark.anyio
async def test_client_cancel_after_broker_acceptance_confirms_cleanup(tmp_path,package,snapshot,monkeypatch):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    accepted=asyncio.Event();old=native._write_frame
    async def delayed(writer,kind,body):
        if kind==5:accepted.set();await asyncio.Event().wait()
        return await old(writer,kind,body)
    monkeypatch.setattr(native,'_write_frame',delayed)
    try:
        lease=await client.prepare(scope)
        task=asyncio.create_task(client.install_packages(scope,lease,selected));await accepted.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert host._row(scope.key)['status']=='stopped' and not host.uploads
    finally:await server.close()

@pytest.mark.anyio
async def test_oversize_frame_rejected_before_allocation_and_reconcile_cleans(tmp_path,package,snapshot):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    try:
        lease=await client.prepare(scope)
        _,reader,writer=await client._connect('install_packages',scope,lease)
        writer.write(native._FRAME.pack(5,mount.METADATA_BYTES+1));await writer.drain()
        response=json.loads(await reader.readline())
        assert response['error']=='package_frame_limit'
        assert host._row(scope.key)['status']=='failed_stop'
        writer.close();await writer.wait_closed()
        await host.reconcile();assert host._row(scope.key)['status']=='stopped'
    finally:await server.close()

@pytest.mark.anyio
async def test_slow_upload_has_single_aggregate_deadline(tmp_path,package,snapshot,monkeypatch):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    monkeypatch.setattr(mount,'UPLOAD_SECONDS',.05)
    try:
        lease=await client.prepare(scope)
        _,reader,writer=await client._connect('install_packages',scope,lease)
        assert json.loads(await reader.readline())['error']=='invalid_request'
        assert host._row(scope.key)['status']=='failed_stop'
        writer.close();await writer.wait_closed();await client.stop(scope,lease)
    finally:await server.close()

@pytest.mark.anyio
async def test_runtime_selection_serializes_uninstall_through_launch(tmp_path,package,snapshot):
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='alice')
    async with store.runtime_selection(principal_id='alice') as selected:
        removing=asyncio.create_task(store.uninstall('notion',principal_id='alice'))
        await asyncio.sleep(.05);assert not removing.done()
        store.validate_snapshot(selected,principal_id='alice')
    await removing
    with pytest.raises(PackageError):store.validate_snapshot(selected,principal_id='alice')

@pytest.mark.anyio
async def test_mount_target_symlink_denied_and_failed_stop_fenced(tmp_path,package,snapshot):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    try:
        lease=await client.prepare(scope);await client.install_packages(scope,lease,selected)
        volume=host.backend.volume_path(scope.key)
        (volume/'codex-state/plugins').symlink_to(tmp_path,target_is_directory=True)
        listeners=sockets(lease)
        try:
            with pytest.raises(native.ExecutorError,match='invalid_package_mount_target'):await client.start(scope,lease,TOKEN)
        finally:
            for s in listeners:s.close()
        assert not host.backend.started and host._row(scope.key)['status']=='failed_stop'
        await client.stop(scope,lease)
    finally:await server.close()

@pytest.mark.anyio
async def test_complete_payload_duplicate_and_aggregate_limits(tmp_path,package,snapshot,monkeypatch):
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='alice')
    selected=store.snapshot(principal_id='alice');value=mount.selection_metadata(selected)
    monkeypatch.setattr(mount,'SELECTION_BYTES',1)
    with pytest.raises(mount.PackageMountError,match='limit'):mount.validate_metadata(value,selected.company_scope,'alice')
    monkeypatch.setattr(mount,'SELECTION_BYTES',128*1024*1024)
    value['packages'].append(value['packages'][0])
    with pytest.raises(mount.PackageMountError,match='limit'):mount.validate_metadata(value,selected.company_scope,'alice')

@pytest.mark.anyio
async def test_revoke_owner_plugin_keeps_failed_sessions_retryable():
    from deploy.migration.legacy.isolated_native import IsolatedNativeLauncher
    from types import SimpleNamespace
    launcher=IsolatedNativeLauncher(SimpleNamespace(),company_id='company')
    class Session:
        def __init__(self,owner,plugin):self.scope=SimpleNamespace(owner_id=owner);self.plugin_ids={plugin};self.calls=0;self.fail=False
        async def stop(self):
            self.calls+=1
            if self.fail:raise native.ExecutorError('failed_stop_fence')
    a=Session('alice','notion');b=Session('bob','notion');c=Session('alice','other')
    launcher._sessions={a,b,c};a.fail=True
    with pytest.raises(native.ExecutorError):await launcher.revoke_plugin('alice','notion')
    assert a in launcher._sessions and not b.calls and not c.calls
    a.fail=False
    assert await launcher.revoke_plugin('alice','notion')=={'cleanup_confirmed':True}
    assert a not in launcher._sessions and a.calls==2

def test_native_cli_object_overrides_are_toml_maps():
    import tomllib
    value={'notion@rocoto-pinned-api':{'enabled':True,'mcp_servers':{'notion':{'enabled':False}}}}
    assert tomllib.loads('plugins='+mount.toml_value(value))['plugins']==value

@pytest.mark.anyio
async def test_selected_native_policy_and_cli_enable_only_sealed_snapshot(tmp_path,package,snapshot,monkeypatch):
    from deploy.migration.legacy.native_policy import isolated_thread_params
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    try:
        lease=await client.prepare(scope);result=await client.install_packages(scope,lease,selected)
        spec=host._spec(host._row(scope.key));calls=[]
        async def command(*args,**kwargs):calls.append(args);return b''
        async def process(*args,**kwargs):return object()
        monkeypatch.setattr(native,'_command',command);monkeypatch.setattr(native.asyncio,'create_subprocess_exec',process)
        await native.DockerBackend(None).start(spec,TOKEN)
        import tomllib
        arguments=calls[0]
        config=tomllib.loads('\n'.join(arguments[i+1] for i,x in enumerate(arguments[:-1]) if x=='-c'))
        assert config['plugins']==result['plugins'] and config['features']['plugins'] is True
        assert not config['features']['apps'] and not config['features']['hooks']
        assert isolated_thread_params('',[],plugins=result['plugins'])['config']['plugins']==result['plugins']
        assert isolated_thread_params('',[])['config']['plugins']=={}
    finally:await server.close()

@pytest.mark.anyio
async def test_repeated_cancellation_cannot_cancel_accepted_upload_cleanup(tmp_path,package,snapshot,monkeypatch):
    store,selected,scope,host,server,client=await fixture(tmp_path,package,snapshot)
    accepted=asyncio.Event();stopping=asyncio.Event();release=asyncio.Event();oldwrite=native._write_frame;oldstop=client.stop
    async def delayed(writer,kind,body):
        if kind==5:accepted.set();await asyncio.Event().wait()
        return await oldwrite(writer,kind,body)
    async def stop(*args):
        stopping.set();await release.wait();return await oldstop(*args)
    monkeypatch.setattr(native,'_write_frame',delayed);monkeypatch.setattr(client,'stop',stop)
    try:
        lease=await client.prepare(scope)
        task=asyncio.create_task(client.install_packages(scope,lease,selected));await accepted.wait();task.cancel();await stopping.wait()
        for _ in range(3):task.cancel();await asyncio.sleep(.005)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert host._row(scope.key)['status']=='stopped' and not host.uploads
    finally:release.set();await server.close()
