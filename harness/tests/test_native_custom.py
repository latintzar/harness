import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
import custom_worker as worker
import native_executor as native
import native_install_broker as install
from native_custom_broker import CustomExecutor,CustomDocker
from native_custom_client import CustomClient,request_for
from custom_execution import RuntimeProfile
from test_custom_execution import setup as authority_setup
from test_native_executor import broker

@pytest.fixture
def tmp_path():
    import tempfile
    with tempfile.TemporaryDirectory(prefix='cxb-')as d:yield Path(d)
@pytest.fixture
def anyio_backend():return 'asyncio'

class Backend:
    def __init__(self,root):
        self.root=root;self.live=False;self.started=0;self.fail_stop=False;self.wait=None
        self.entered=asyncio.Event();self.value={'items':['fixture']};self.symlink=False
    def volume_path(self,key):return self.root/key
    async def run(self,key,root,sockets):
        self.started+=1;self.live=True;self.entered.set()
        if self.wait:await self.wait.wait()
        workspace=self.volume_path(key)/'workspace';workspace.mkdir(parents=True)
        if self.symlink:(workspace/'result.json').symlink_to('/etc/passwd')
        else:(workspace/'result.json').write_bytes(worker.canonical(self.value))
    async def stop(self,key):
        if self.fail_stop:raise native.ExecutorError('fixture_stop_failed')
        self.live=False
    async def unmount(self,key):shutil.rmtree(self.volume_path(key),ignore_errors=True)

async def setup(tmp_path):
    store,manager,key=authority_setup(tmp_path/'company')
    host=broker(tmp_path/'broker');backend=Backend(tmp_path/'custom-volumes')
    script=Path(worker.__file__);executor=CustomExecutor(host,worker_path=script,backend=backend)
    manager.runtime=RuntimeProfile(native.IMAGE,executor.worker_sha256)
    snapshot=manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)
    server=native.BrokerServer(host,tmp_path/'control.sock');await server.start()
    scope=native.NativeScope(manager.company,'alice','custom:'+key);client=CustomClient(server.path)
    return host,backend,executor,server,scope,client,snapshot

def listener(lease):
    sock=socket.socket(socket.AF_UNIX);sock.bind(str(Path(lease.socket_dir)/'custom-http.sock'));sock.listen();return sock

async def terminal(client,scope,lease):
    async with asyncio.timeout(3):
        while True:
            result=await client.status(scope,lease)
            if result['status']not in {'prepared','sealed','receiving','running','starting'}:return result
            await asyncio.sleep(.01)

async def admit(client,scope,snapshot):
    lease=await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
    await client.upload(scope,lease,snapshot,'list_inventory',{});sock=listener(lease)
    await client.start(scope,lease,'fixture_'+48*'x')
    return lease,sock

@pytest.mark.anyio
async def test_real_ipc_sealed_source_result_recovery_owner_and_cleanup(tmp_path):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path);sock=None
    try:
        assert await client.recover(scope,'a'*32)is None
        lease,sock=await admit(client,scope,snapshot)
        assert await client.recover(scope,'a'*32)==lease
        assert await client.recover(replace(scope,owner_id='bob'),'a'*32)is None
        with pytest.raises(native.ExecutorError,match='scope'):await client.status(replace(scope,company_id='other'),lease)
        assert(await terminal(client,scope,lease))=={'status':'succeeded','cleanup_confirmed':True}
        result=await client.result(scope,lease,snapshot,'list_inventory',{})
        assert result['result']==backend.value and result['snapshot_sha256']==snapshot.sha256
        assert result['worker_sha256']==executor.worker_sha256 and result['cleanup_confirmed']and not backend.live
        assert lease.token not in repr(lease)
        assert (await client.release(scope,lease))['status']=='released'
        assert not(executor.root/'jobs'/lease.id).exists()and not Path(lease.socket_dir).exists()
    finally:
        if sock:sock.close()
        await server.close()

@pytest.mark.anyio
async def test_prepare_is_idempotent_and_changed_intent_cannot_reuse_job(tmp_path):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
        assert await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})==lease
        with pytest.raises(native.ExecutorError,match='changed'):await client.prepare(scope,'a'*32,snapshot,'list_inventory',{'different':1})
        assert executor.live_count()==1 and not backend.started
        await client.release(scope,lease)
    finally:await server.close()

@pytest.mark.anyio
@pytest.mark.parametrize('change',['owner','source','runtime','metadata','path'])
async def test_upload_binding_rejects_before_worker_start(tmp_path,change):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
        _,reader,writer=await client._connect('custom_upload',scope,lease=__import__('dataclasses').asdict(lease))
        data=snapshot.metadata
        if change=='owner':data['owner']='bob'
        elif change=='runtime':data['runtime']['worker_sha256']='b'*64
        elif change=='path':data['files'][0]['path']='../escape.py'
        elif change=='metadata':data['manifest']['entrypoint']='/etc/passwd'
        await native._write_frame(writer,5,worker.canonical({'snapshot':data,'arguments':{}}))
        if change=='source':
            for item in snapshot.files:await native._write_frame(writer,6,b'x'*len(item.content))
            await native._write_frame(writer,7,b'')
        response=json.loads(await reader.readline());assert response.get('error')
        writer.close();await writer.wait_closed()
        assert backend.started==0 and executor.live_count()==1
        await client.release(scope,lease);assert executor.live_count()==0
    finally:await server.close()

@pytest.mark.anyio
async def test_all_native_installer_custom_admission_paths_share_capacity(tmp_path):
    from test_native_install import SOURCE
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path)
    installer=install.Installer(host,allowed_sources=[SOURCE],backend=backend)
    doc={'source':SOURCE,'archive_sha256':'a'*64,'archive_bytes':1}
    try:
        custom=await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
        normal_scope=replace(scope,root_chat_id='normal');normal=await host.prepare(normal_scope)
        with pytest.raises(native.ExecutorError,match='capacity'):await installer.prepare(scope,'b'*32,doc)
        with pytest.raises(native.ExecutorError,match='capacity'):await client.prepare(scope,'b'*32,snapshot,'list_inventory',{})
        await host.stop(normal_scope,normal)
        installed=await installer.prepare(scope,'b'*32,doc)
        with pytest.raises(native.ExecutorError,match='capacity'):await host.prepare(normal_scope)
        await installer.release(scope,installed);await client.release(scope,custom)
        assert host.live_count()==0
    finally:await server.close()

@pytest.mark.anyio
async def test_partial_upload_stop_reconcile_and_expiry_are_fenced(tmp_path):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path)
    try:
        lease=await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
        _,reader,writer=await client._connect('custom_upload',scope,lease=__import__('dataclasses').asdict(lease))
        await native._write_frame(writer,5,worker.canonical({'snapshot':snapshot.metadata,'arguments':{}}))
        await native._write_frame(writer,6,snapshot.files[0].content[:1])
        assert(await client.stop(scope,lease))['cleanup_confirmed']and not executor.uploads
        writer.close();await writer.wait_closed();await client.release(scope,lease)
        second=await client.prepare(scope,'b'*32,snapshot,'list_inventory',{})
        executor._status(second.id,'receiving');backend.fail_stop=True
        await executor.reconcile();assert(await client.status(scope,second))['status']=='uncertain'
        assert executor.live_count()==1
        await executor.expire();assert executor.live_count()==1
        backend.fail_stop=False;await executor.expire();assert executor.live_count()==0
        assert backend.started==0
    finally:await server.close()

@pytest.mark.anyio
async def test_running_stop_and_failed_cleanup_keep_shared_slot(tmp_path):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path);backend.wait=asyncio.Event();sock=None
    try:
        lease,sock=await admit(client,scope,snapshot);await backend.entered.wait();backend.fail_stop=True
        with pytest.raises(native.ExecutorError,match='uncertain'):await client.stop(scope,lease)
        assert backend.live and executor.live_count()==1
        backend.fail_stop=False
        assert(await client.release(scope,lease))['cleanup_confirmed']and not backend.live and not executor.runs
    finally:
        if sock:sock.close()
        await server.close()

@pytest.mark.anyio
@pytest.mark.parametrize('bad',['symlink','oversize','list'])
async def test_untrusted_output_is_bounded_regular_json(tmp_path,bad):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path);sock=None
    if bad=='symlink':backend.symlink=True
    elif bad=='oversize':backend.value={'x':'a'*(worker.RESULT_BYTES+1)}
    else:backend.value=[]
    try:
        lease,sock=await admit(client,scope,snapshot)
        assert(await terminal(client,scope,lease))=={'status':'failed','cleanup_confirmed':True}
        with pytest.raises(native.ExecutorError,match='unavailable'):await client.result(scope,lease,snapshot,'list_inventory',{})
        await client.release(scope,lease)
    finally:
        if sock:sock.close()
        await server.close()

@pytest.mark.anyio
async def test_slow_copy_keeps_control_responsive_and_stop_suppresses_publish(tmp_path):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path);sock=None
    entered=threading.Event();release=threading.Event();original=executor._freeze_output
    def slow(*args):entered.set();assert release.wait(5);return original(*args)
    executor._freeze_output=slow
    try:
        lease,sock=await admit(client,scope,snapshot);assert await asyncio.to_thread(entered.wait,2)
        assert(await asyncio.wait_for(client.capabilities(scope),.5))['available']
        stopping=asyncio.create_task(client.stop(scope,lease));await asyncio.sleep(.03)
        assert not stopping.done()and executor.live_count()==1
        release.set();assert(await stopping)['cleanup_confirmed']
        assert(await client.status(scope,lease))['status']=='stopped'
        with pytest.raises(native.ExecutorError):await client.result(scope,lease,snapshot,'list_inventory',{})
        await client.release(scope,lease)
    finally:
        release.set()
        if sock:sock.close()
        await server.close()

def test_worker_invokes_only_fixed_python_and_keeps_credentials_out(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'adapter.py').write_text('print({})')
    workspace=tmp_path/'workspace';workspace.mkdir();seen=[]
    def execute(argv,**kwargs):
        seen.append((argv,kwargs));payload=json.load(kwargs['stdin'])
        assert payload=={'tool':'inventory','arguments':{},'gateway':{'socket':worker.SOCKET,'token':'x'*48}}
        kwargs['stdout'].write(b'{"items":[]}')
        return type('Result',(),{'returncode':0})()
    config={'snapshot_sha256':'a'*64,'entrypoint':'adapter.py','tool':'inventory','arguments':{},'gateway_token':'x'*48}
    assert worker.run(source,config,workspace,execute=execute)=={'items':[]}
    assert seen[0][0]==['/usr/local/bin/python3','-I',str(source/'adapter.py')]
    assert set(seen[0][1]['env'])=={'PATH','LANG','HOME','TMPDIR'}

@pytest.mark.anyio
async def test_fixed_docker_command_profile_and_separate_quota(tmp_path,monkeypatch):
    import native_custom_broker
    backend=CustomDocker(tmp_path/'volumes',tmp_path/'worker.py');commands=[]
    async def mount(key):return tmp_path/'volume'
    async def command(*args,**kwargs):commands.append((args,kwargs));return b''
    backend.volumes.mount=mount;monkeypatch.setattr(native_custom_broker,'silent_command',command)
    await backend.run('a'*48,tmp_path/'sealed',tmp_path/'sockets')
    argv,options=commands[0]
    assert '--network'in argv and argv[argv.index('--network')+1]=='none'
    assert argv[argv.index('--runtime')+1]==native.RUNTIME and native.IMAGE in argv
    assert argv[argv.index('--pids-limit')+1]=='64'and options['timeout']==45
    assert backend.volumes.size_bytes==64*1024*1024
    assert '--read-only'in argv and '--cap-drop'in argv and '--log-driver'in argv


@pytest.mark.anyio
async def test_cancelled_accepted_start_recovers_exact_job_and_confirms_cleanup(tmp_path):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path);backend.wait=asyncio.Event();sock=None
    try:
        lease=await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
        await client.upload(scope,lease,snapshot,'list_inventory',{});sock=listener(lease)
        accepted=asyncio.Event();release=asyncio.Event();original=client._request
        async def delayed(operation,*args,**kwargs):
            value=await original(operation,*args,**kwargs)
            if operation=='custom_start':accepted.set();await release.wait()
            return value
        client._request=delayed
        task=asyncio.create_task(client.start(scope,lease,'fixture_'+48*'x'));await accepted.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        recovered=await client.recover(scope,'a'*32);assert recovered==lease
        assert(await client.stop(scope,recovered))['cleanup_confirmed']and not backend.live
        await client.release(scope,recovered)
    finally:
        if sock:sock.close()
        await server.close()


@pytest.mark.anyio
async def test_cancelled_upload_retains_intent_until_owner_stop(tmp_path,monkeypatch):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path)
    entered=asyncio.Event();hold=asyncio.Event();write=native._write_frame
    async def delayed(writer,kind,raw):
        await write(writer,kind,raw)
        if kind==5:entered.set();await hold.wait()
    monkeypatch.setattr(native,'_write_frame',delayed)
    try:
        lease=await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
        task=asyncio.create_task(client.upload(scope,lease,snapshot,'list_inventory',{}));await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert(await client.stop(scope,lease))['cleanup_confirmed']and backend.started==0
        await client.release(scope,lease);assert not executor.uploads
    finally:await server.close()


@pytest.mark.anyio
async def test_stop_closes_receipt_read_admission_and_drains_current_reader(tmp_path,monkeypatch):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path);sock=None
    entered=asyncio.Event();hold=asyncio.Event();write=native._write_frame
    try:
        lease,sock=await admit(client,scope,snapshot);assert(await terminal(client,scope,lease))['status']=='succeeded'
        async def delayed(writer,kind,raw):
            if kind==5:entered.set();await hold.wait()
            await write(writer,kind,raw)
        monkeypatch.setattr(native,'_write_frame',delayed)
        reading=asyncio.create_task(client.result(scope,lease,snapshot,'list_inventory',{}));await entered.wait()
        assert(await client.stop(scope,lease))['cleanup_confirmed']
        with pytest.raises((ValueError,native.ExecutorError,asyncio.IncompleteReadError)):await reading
        with pytest.raises(native.ExecutorError):await client.result(scope,lease,snapshot,'list_inventory',{})
        assert not executor.readers[lease.id];await client.release(scope,lease)
    finally:
        hold.set()
        if sock:sock.close()
        await server.close()


@pytest.mark.anyio
async def test_source_prefix_collision_and_injected_socket_path_rejected(tmp_path):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path)
    try:
        data=snapshot.metadata;data['files'].append({'path':'src','size_bytes':1,'sha256':'a'*64})
        request={**request_for(snapshot,'list_inventory',{}),'snapshot_sha256':worker.digest(data),
            'document_bytes':len(worker.canonical(data)),'source_files':len(data['files']),
            'source_bytes':sum(f['size_bytes']for f in data['files'])}
        with pytest.raises(ValueError):worker.bundle({'snapshot':data,'arguments':{}},request,company=scope.company_id,owner=scope.owner_id,image=native.IMAGE,worker_sha256=executor.worker_sha256)
        lease=await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
        with pytest.raises(native.ExecutorError,match='scope'):await client.status(scope,replace(lease,socket_dir='/etc'))
        await client.release(scope,lease)
    finally:await server.close()


@pytest.mark.anyio
async def test_container_console_is_discarded_and_cancelled_cli_is_reaped(monkeypatch):
    import native_custom_broker
    seen={};entered=asyncio.Event();released=asyncio.Event()
    class Process:
        returncode=None
        def kill(self):seen['killed']=True;released.set()
        async def wait(self):entered.set();await released.wait();self.returncode=-9;return self.returncode
    async def spawn(*args,**kwargs):seen.update(kwargs);return Process()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    task=asyncio.create_task(native_custom_broker.silent_command('fixed-docker','run',timeout=2));await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert seen['stdout']==asyncio.subprocess.DEVNULL and seen['stderr']==asyncio.subprocess.DEVNULL and seen['stdin']==asyncio.subprocess.DEVNULL
    assert seen['killed']and released.is_set()


@pytest.mark.anyio
async def test_cleanup_absence_tombstone_fences_delayed_prepare_after_reply_loss(tmp_path):
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path)
    arrived=asyncio.Event();release=asyncio.Event();original=executor.prepare
    async def delayed(*args,**kwargs):arrived.set();await release.wait();return await original(*args,**kwargs)
    executor.prepare=delayed
    try:
        preparing=asyncio.create_task(client.prepare(scope,'a'*32,snapshot,'list_inventory',{}));await arrived.wait()
        preparing.cancel()
        with pytest.raises(asyncio.CancelledError):await preparing
        assert await client.recover(scope,'a'*32,retire=True)is None
        release.set();await asyncio.sleep(.05)
        assert await client.recover(scope,'a'*32)is None and executor.live_count()==0 and backend.started==0
        assert not list((executor.root/'jobs').glob('*'))
        with pytest.raises(native.ExecutorError,match='changed'):await client.prepare(scope,'a'*32,snapshot,'list_inventory',{})
        # Another owner's same request ID remains an independent namespace.
        other=replace(scope,owner_id='bob');assert await client.recover(other,'a'*32)is None
    finally:release.set();await server.close()


@pytest.mark.skipif(os.geteuid()!=0,reason='commissioning verifies actual root ownership')
def test_optional_service_custom_commission_requires_exact_root_source_closure(tmp_path,monkeypatch):
    import importlib.util,native_custom_broker
    path=Path(__file__).parents[1]/'deploy/native-executor/broker_service.py'
    spec=importlib.util.spec_from_file_location('fixture_custom_service',path);service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)
    seen=[];monkeypatch.setattr(native_custom_broker,'CustomExecutor',lambda broker:seen.append(broker))
    host=object();service.configure_custom(host,tmp_path);assert not seen
    names={'native_executor.py','native_package_mount.py','native_custom_broker.py','custom_worker.py','broker_service.py'};sources={}
    for name in names:
        source=tmp_path/name;source.write_text('# synthetic root source\n');source.chmod(0o644);sources[name]=hashlib.sha256(source.read_bytes()).hexdigest()
    record={'schema_version':1,'profile':worker.PROFILE,'runtime':native.RUNTIME,'image':native.IMAGE,'sources':sources}
    receipt=tmp_path/'custom-commissioning.json';receipt.write_text(json.dumps(record));receipt.chmod(0o644)
    service.configure_custom(host,tmp_path);assert seen==[host]
    (tmp_path/'custom_worker.py').write_text('# changed\n')
    with pytest.raises(SystemExit,match='source changed'):service.configure_custom(host,tmp_path)
    assert seen==[host]


# ── Retention: released job rows leave the history after one day ──

@pytest.mark.anyio
async def test_released_rows_older_than_a_day_leave_the_history_and_free_its_bound(tmp_path,monkeypatch):
    import native_custom_broker
    host,backend,executor,server,scope,client,snapshot=await setup(tmp_path);sock=None
    rows=lambda:[(r['request_id'][0],r['status'])for r in executor.db.execute('SELECT request_id,status FROM jobs ORDER BY request_id')]
    age=lambda request:executor.db.execute('UPDATE jobs SET created=created-? WHERE request_id=?',(native_custom_broker.RELEASED_SECONDS+60,request))
    try:
        lease,sock=await admit(client,scope,snapshot);await terminal(client,scope,lease);await client.release(scope,lease);sock.close();sock=None
        assert await client.recover(scope,'b'*32,retire=True)is None
        assert rows()==[('a','released'),('b','released')]
        # Inside the day both identities stay, and the tombstone still fences a delayed prepare.
        await executor.expire();assert rows()==[('a','released'),('b','released')]
        with pytest.raises(native.ExecutorError,match='changed'):await client.prepare(scope,'b'*32,snapshot,'list_inventory',{})
        monkeypatch.setattr(native_custom_broker,'MAX_HISTORY',2)
        with pytest.raises(native.ExecutorError,match='custom_history_capacity'):await client.prepare(scope,'c'*32,snapshot,'list_inventory',{})
        # A day later the released row goes, and the history takes a job again.
        age('a'*32);await executor.expire();assert rows()==[('b','released')]
        third=await client.prepare(scope,'c'*32,snapshot,'list_inventory',{})
        assert rows()==[('b','released'),('c','prepared')]
        # Age alone removes nothing that is still the broker's to clear: a job whose cleanup is unconfirmed keeps its row.
        executor._status(third.id,'receiving');backend.fail_stop=True;age('c'*32)
        await executor.reconcile();await executor.expire()
        assert rows()==[('b','released'),('c','uncertain')]and executor.live_count()==1
        backend.fail_stop=False;await executor.expire()
        assert rows()==[('b','released')]and executor.live_count()==0 and not list((executor.root/'jobs').glob('*'))
        assert native_custom_broker.RELEASED_SECONDS==86400
    finally:
        if sock:sock.close()
        await server.close()


def retention_upgrade():
    import importlib.util
    path=Path(__file__).parents[1]/'deploy/native-executor/upgrade_custom_retention.py'
    spec=importlib.util.spec_from_file_location('retention_upgrade',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_the_retention_upgrade_pins_the_installed_closure_and_the_one_replacement():
    upgrade=retention_upgrade();folder=Path(__file__).parents[1]/'deploy/native-executor'
    record=json.loads((folder/'custom-retention-release.json').read_text())
    installed=json.loads((folder/'TOOLING-PUBLIC-2026-10-05.json').read_text())['host']
    assert upgrade.PREVIOUS==record['previous_sources']==installed['broker_sources']and upgrade.IMAGE==record['image']==installed['image']==native.IMAGE
    assert upgrade.CURRENT==record['sources']and upgrade.REPLACEMENTS==record['replacements']
    assert upgrade.REPLACEMENTS=={'native_custom_broker.py':hashlib.sha256((Path(__file__).parents[1]/'mechanics/native_custom_broker.py').read_bytes()).hexdigest()}
    assert upgrade.CHANGED=={'native_custom_broker.py','custom-commissioning.json'}
    assert upgrade.COMMON_SHA==hashlib.sha256((folder/'upgrade_custom.py').read_bytes()).hexdigest()
    # The record in the checkout names no proof yet: the operator writes the hash after the proof ran on the host, and until then the upgrade refuses it.
    assert record['proof_sha256']is None
    from types import SimpleNamespace
    u=SimpleNamespace(PROFILE=record['profile'],RUNTIME=record['runtime'],root_file=lambda p,h=None:None,load=lambda p,h=None:record)
    with pytest.raises(RuntimeError,match='retention_proof_unpinned'):upgrade.bundle_record(u,folder)


def retention_fixture(upgrade):
    from types import SimpleNamespace
    release={'schema_version':1,'profile':'profile','image':upgrade.IMAGE,'runtime':'runtime','previous_sources':upgrade.PREVIOUS,
        'sources':upgrade.CURRENT,'replacements':upgrade.REPLACEMENTS,'proof_sha256':'ab'*32}
    proof={'status':'passed','provider_calls':0,'model_calls':0,'image':upgrade.IMAGE,'runtime':'runtime','installed_service_changed':False,
        'remaining_jobs':0,'remaining_volumes':[],'remaining_containers':'','interrupted_upload_restart_cleanup':True,
        'source_sha256':{n:upgrade.CURRENT[n]for n in ('native_custom_broker.py','custom_worker.py','native_executor.py','native_package_mount.py')},
        'checks':{'retired_request_fenced':True,'successful_job':{'cleanup_confirmed':True,'worker_sha256':upgrade.CURRENT['custom_worker.py']},'stopped_job':{'whole_worker_stop':True}}}
    loaded={'retention-release.json':release,'retention-proof.json':proof}
    # `load` stands where the helper's stands: it checks a file's hash only when it is given one.
    seen=[]
    u=SimpleNamespace(PROFILE='profile',RUNTIME='runtime',root_file=lambda p,h=None:None,load=lambda p,h=None:(seen.append((p.name,h)),loaded[p.name])[1],seen=seen)
    return u,release,proof


def test_the_retention_upgrade_accepts_the_exact_release_and_its_candidate_proof(tmp_path):
    upgrade=retention_upgrade();u,release,proof=retention_fixture(upgrade)
    assert upgrade.bundle_record(u,tmp_path)==release
    assert u.seen==[('retention-release.json',None),('retention-proof.json','ab'*32)],'the proof is loaded with the hash the release names'


@pytest.mark.parametrize('fault',['source','worker','cleanup','provider','image','replacement','fence','unpinned','short','upper','number'])
def test_the_retention_upgrade_refuses_another_release_or_proof_before_any_change(tmp_path,fault):
    upgrade=retention_upgrade();u,release,proof=retention_fixture(upgrade)
    unpinned={'unpinned':None,'short':'ab'*31,'upper':'AB'*32,'number':0}
    if fault in unpinned:
        # A release that names no proof hash is one whose proof step was skipped: the gate stays closed and the proof file is left unread.
        release['proof_sha256']=unpinned[fault]
        with pytest.raises(RuntimeError,match='retention_proof_unpinned'):upgrade.bundle_record(u,tmp_path)
        assert [name for name,_ in u.seen]==['retention-release.json'];return
    if fault=='source':proof['source_sha256']['native_custom_broker.py']=upgrade.PREVIOUS['native_custom_broker.py']
    elif fault=='worker':proof['checks']['successful_job']['worker_sha256']='0'*64
    elif fault=='cleanup':proof['remaining_jobs']=1
    elif fault=='provider':proof['provider_calls']=1
    elif fault=='image':proof['image']='sha256:'+'0'*64
    elif fault=='replacement':release['replacements']={'custom_worker.py':'0'*64}
    else:proof['checks']['retired_request_fenced']=False
    with pytest.raises(RuntimeError):upgrade.bundle_record(u,tmp_path)


@pytest.mark.parametrize('fail_start',[False,True])
def test_the_retention_upgrade_replaces_two_files_or_restores_both(tmp_path,monkeypatch,fail_start):
    import copy
    from types import SimpleNamespace
    upgrade=retention_upgrade()
    dest=tmp_path/'installed';dest.mkdir();state=tmp_path/'state';state.mkdir();bundle=tmp_path/'bundle';bundle.mkdir()
    custom={'schema_version':1,'sources':{k:upgrade.PREVIOUS[k]for k in ['native_executor.py','native_package_mount.py','native_custom_broker.py','custom_worker.py','broker_service.py']}}
    records={'installer-commissioning.json':{'sources':{'native_executor.py':upgrade.PREVIOUS['native_executor.py']}},'custom-commissioning.json':custom}
    before={'native_custom_broker.py':b'installed broker\n','custom-commissioning.json':json.dumps(custom).encode()}
    for name,raw in before.items():(dest/name).write_bytes(raw)
    (dest/'installer-commissioning.json').write_text(json.dumps(records['installer-commissioning.json']))
    replacement=b'reviewed broker\n';digest=hashlib.sha256(replacement).hexdigest();(bundle/'native_custom_broker.py').write_bytes(replacement)
    monkeypatch.setattr(upgrade,'REPLACEMENTS',{'native_custom_broker.py':digest});monkeypatch.setattr(upgrade,'CURRENT',upgrade.PREVIOUS|{'native_custom_broker.py':digest})
    ledger=state/'jobs.sqlite3';ledger.write_bytes(b'existing-rows\n');seen=[]
    def run(args):
        seen.append(args)
        if args[1]=='start':
            with ledger.open('ab')as stream:stream.write(b'later-row\n')
        return 'active'if args[1]=='is-active'else ''
    def root_file(path,checksum=None):
        assert path.is_file()and(checksum is None or hashlib.sha256(path.read_bytes()).hexdigest()==checksum)
    control=tmp_path/'control';control.touch()
    u=SimpleNamespace(DEST=dest,STATE=state,SERVICE='native-only',CONTROL=control,RUNTIME='runtime',canonical_bundle=lambda p:p,
        quiescent=lambda:seen.append('idle'),service_pids=lambda:{'app':'1','media':'2','docker':'3'},run=run,root_file=root_file,
        atomic=lambda path,raw,mode=0o644,uid=0,gid=0:path.write_bytes(raw))
    monkeypatch.setattr(upgrade,'load_common',lambda:u);monkeypatch.setattr(upgrade,'installed',lambda u:copy.deepcopy(records))
    monkeypatch.setattr(upgrade,'bundle_record',lambda u,b:{'proof_sha256':'p'})
    def ready(u,updated):
        assert(dest/'native_custom_broker.py').read_bytes()==replacement
        written=json.loads((dest/'custom-commissioning.json').read_bytes())
        assert written==updated['custom-commissioning.json']and written['sources']['native_custom_broker.py']==digest
        assert written['sources']['custom_worker.py']==upgrade.PREVIOUS['custom_worker.py']and updated['installer-commissioning.json']==records['installer-commissioning.json']
        if fail_start:raise RuntimeError('synthetic_readiness_failure')
        return {'verified':True}
    monkeypatch.setattr(upgrade,'ready',ready);monkeypatch.setattr(upgrade.os,'getuid',lambda:0)
    monkeypatch.setattr(upgrade.socket,'gethostname',lambda:'rocoto-staging');monkeypatch.setattr(sys,'argv',['upgrade','--bundle',str(bundle),'--apply'])
    old=os.umask(0o022)
    try:
        if fail_start:
            with pytest.raises(RuntimeError,match='synthetic_readiness'):upgrade.main()
            assert all((dest/name).read_bytes()==raw for name,raw in before.items())
        else:
            upgrade.main();assert(dest/'native_custom_broker.py').read_bytes()==replacement
    finally:os.umask(old)
    assert ledger.read_bytes().startswith(b'existing-rows\n')and ledger.read_bytes().count(b'later-row')==(2 if fail_start else 1)
    assert all(args[-1]=='native-only'for args in seen if isinstance(args,list))and seen.count('idle')>=3
    # Away from the staging host, or as another user, the command stops before it reads anything.
    monkeypatch.setattr(upgrade.socket,'gethostname',lambda:'rocoto-dev')
    with pytest.raises(RuntimeError,match='staging_root_only'):upgrade.main()
