import asyncio
from contextlib import asynccontextmanager
from functools import partial
import json
import os
import threading
import time

import pytest
from test_sftp_intake import fixture as sftp_fixture
from test_source_host_routes import commission_fixture
from custom_connectors import CustomConnectors
from source_embedding import atomic_json
from source_intake import IntakeActor, IntakeError
from source_service import HEARTBEAT_TTL as SOURCE_TTL
from storage_connections import StorageConnections
from storage_runtime import StorageRuntime, Configuration, PROFILE, HEARTBEAT_TTL
from storage_service import StorageService
from sftp_intake import SFTPAcquirer

@pytest.fixture
def anyio_backend():return 'asyncio'

@asynccontextmanager
async def fixture(tmp_path,mode='one_time',content=b'complete recording'):
 async with sftp_fixture(tmp_path,mode=mode,content=content)as remote:
  state=remote['store'].root.parent;host=commission_fixture(state)
  # Rebind the synthetic funded configuration to this fixture's collection.
  config_path=state/'compute/source-intake-service.json';value=json.loads(config_path.read_text());value['collection']='shop';atomic_json(config_path,value)
  host.clock=lambda:remote['clock'][0]
  def heartbeat():
   now=host.clock();atomic_json(host.root/'heartbeat.json',{'schema_version':1,'company_id':host.company,'ready':True,
    'config_sha256':host.configuration().fingerprint,'code_sha256':host.code_sha256,
    'checked_at':now,'expires_at':now+SOURCE_TTL})
  heartbeat()
  private=CustomConnectors(state);authority=StorageConnections(private,remote['store'])
  key=private.create(name='Recording archive',description='Import completed recordings.',kind='sftp',collection='shop',owner='alice')['id']
  private.save_package(key,{'summary':'Read the approved folder.','instructions':'Use the fixed storage reader.',
   'secret_fields':[{'name':name,'label':name,'description':'Private login.'}for name in ('USER','PASSWORD')],
   'files':[{'path':'README.md','content':'Use the fixed storage reader.'}]},expected_revision=1,owner='alice')
  target={k:getattr(remote['config'],k)for k in ('host','port','root','host_key','mode','stable_seconds','poll_seconds')}
  target['authentication']={'username_field':'USER','password_field':'PASSWORD'}
  proposal=authority.propose(key,target,'Import these completed recordings.',expected_revision=2,owner='alice',collection='shop')
  private.set_secret(key,'USER','camera',owner='alice');private.set_secret(key,'PASSWORD','synthetic-private-value',owner='alice')
  atomic_json(state/'compute/storage-service.json',{'schema_version':1,'enabled':True,'company_id':authority.company,'profile':PROFILE,'access_state':str(state),'environment':'demo'})
  from access import AccessStore
  access=AccessStore(state);login_code=access.issue('demo',principal_id='alice');session,_=access.login(login_code)
  runtime=StorageRuntime(authority,source_host=host,clock=host.clock,access_state=state,environment='demo')
  acquirer=SFTPAcquirer(remote['store'],connector=remote['transport'],clock=host.clock,
   begin_upload=partial(host.begin_remote,remote['store']))
  service=StorageService(runtime,acquirer=acquirer,tick_seconds=.01)
  service.ready=True;service.health()
  try:yield locals()
  finally:await acquirer.close()

SCOPE={'owner':'alice','collection':'shop'}

async def approved(f):
 return await f['runtime'].approve(f['key'],f['proposal']['id'],**SCOPE)

async def settle(f):
 await f['service'].step()
 tasks=list(f['service'].active.values())
 if tasks:await asyncio.gather(*tasks,return_exceptions=True)
 await f['service'].step()

@pytest.mark.anyio
async def test_real_owner_approve_run_stability_funding_receipt_complete(tmp_path):
 async with fixture(tmp_path)as f:
  runtime=f['runtime'];key=f['key']
  assert runtime.describe(key,**SCOPE)['status']=='setup'
  assert not f['remote']['auth']
  configured=await approved(f);assert configured['status']=='configured'and configured['can_run']
  assert configured['host_key_fingerprint']==configured['proposal']['host_key_fingerprint']
  assert not f['remote']['auth']
  result=await runtime.run(key,expected_generation=1,**SCOPE);assert result['acquisition']['status']=='queued'
  await settle(f);assert runtime.describe(key,**SCOPE)['acquisition']['status']=='waiting'
  assert f['host'].records()==[]
  f['remote']['clock'][0]+=31;f['heartbeat']();f['service'].health()
  await settle(f)
  result=runtime.describe(key,**SCOPE)
  assert result['acquisition']['status']=='complete'and result['acquisition']['cleanup_confirmed']
  records=f['host'].records();assert len(records)==1 and records[0]['binding']['remote']['kind']=='sftp'
  uploads=f['remote']['store'].list_uploads(f['remote']['actor'],result['acquisition']['source_id'])
  assert len(uploads)==1 and uploads[0]['status']=='sealed'
  assert(f['remote']['store'].blobs/uploads[0]['sha256']).read_bytes()==b'complete recording'
  assert not result['can_run']and not result['funded_ready']
  assert 'synthetic-private-value'not in json.dumps(result)+f['runtime'].runs.path.read_bytes().decode(errors='ignore')

@pytest.mark.anyio
async def test_readiness_and_funding_gate_before_remote_auth(tmp_path):
 async with fixture(tmp_path)as f:
  await approved(f);runtime=f['runtime'];key=f['key']
  (runtime.runs.root/'heartbeat.json').unlink()
  assert not runtime.describe(key,**SCOPE)['runtime_available']
  with pytest.raises(IntakeError,match='worker_unavailable'):await runtime.run(key,expected_generation=1,**SCOPE)
  f['service'].health();(f['host'].root/'heartbeat.json').unlink()
  assert not runtime.describe(key,**SCOPE)['funded_ready']
  with pytest.raises(IntakeError,match='allowance'):await runtime.run(key,expected_generation=1,**SCOPE)
  assert not f['remote']['auth']and f['runtime'].runs.records()==[]

@pytest.mark.anyio
async def test_pause_waits_for_actual_transport_drain_and_fences_late_chunks(tmp_path):
 async with fixture(tmp_path,mode='watch')as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE)
  entered=asyncio.Event();cleanup=asyncio.Event();release=asyncio.Event();transport=f['remote']['transport']
  @asynccontextmanager
  async def held(*args):
   async with transport(*args)as reader:
    entered.set()
    try:await asyncio.Future();yield reader
    finally:cleanup.set();await release.wait()
  f['acquirer'].connector=held;await f['service'].step();await asyncio.wait_for(entered.wait(),3)
  task=asyncio.create_task(f['runtime'].pause(f['key'],expected_generation=1,**SCOPE))
  await asyncio.wait_for(cleanup.wait(),3);await asyncio.sleep(.05);assert not task.done()
  release.set();result=await asyncio.wait_for(task,3)
  assert result['status']=='paused'and result['acquisition']['cleanup_confirmed']and result['acquisition']['status']=='paused'
  assert not f['host'].records()
  await settle(f)

@pytest.mark.anyio
async def test_pause_reports_unconfirmed_until_owned_lease_is_released(tmp_path):
 async with fixture(tmp_path)as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE)
  row=f['runtime'].runs.records()[0];fd=f['runtime'].runs.lock(f['key'])
  f['runtime'].runs.update(row,'running',cleanup=False)
  try:
   result=await f['runtime'].pause(f['key'],expected_generation=1,timeout=.01,**SCOPE)
   assert result['acquisition']['status']=='stopping'and not result['acquisition']['cleanup_confirmed']and result['can_pause']
   again=await f['runtime'].pause(f['key'],expected_generation=2,timeout=.01,**SCOPE)
   assert again['generation']==2 and again['can_pause']
  finally:os.close(fd)
  result=f['runtime'].describe(f['key'],**SCOPE)
  assert result['acquisition']['status']=='paused'and result['acquisition']['cleanup_confirmed']

@pytest.mark.anyio
async def test_restart_fences_abandoned_running_intent_and_retains_queued(tmp_path):
 async with fixture(tmp_path)as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE)
  row=f['runtime'].runs.records()[0];f['runtime'].runs.update(row,'running',cleanup=False)
  reopened=StorageRuntime(f['authority'],source_host=f['host'],clock=f['host'].clock,access_state=f['state'],environment='demo')
  result=reopened.describe(f['key'],**SCOPE)
  assert result['acquisition']['status']=='needs_attention'and result['acquisition']['cleanup_confirmed']
  assert result['can_run']
  await reopened.run(f['key'],expected_generation=1,**SCOPE)
  assert reopened.runs.records()[0]['epoch']==2
  await settle(f);assert f['remote']['auth']

@pytest.mark.anyio
async def test_watch_second_file_cannot_reuse_one_file_allowance(tmp_path):
 async with fixture(tmp_path,mode='watch')as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE);await settle(f)
  f['remote']['clock'][0]+=31;f['heartbeat']();await settle(f)
  (f['remote']['remote']/'next.mp4').write_bytes(b'next')
  for _ in range(2):f['remote']['clock'][0]+=31;f['heartbeat']();await settle(f)
  result=f['runtime'].describe(f['key'],**SCOPE)
  assert result['acquisition']['status']=='needs_attention'
  assert result['acquisition']['error']=='embedding_allowance_reserved'
  assert len(f['host'].records())==1
  assert len(f['remote']['store'].list_uploads(f['remote']['actor'],result['acquisition']['source_id']))==1

@pytest.mark.anyio
async def test_scope_and_generation_denial_never_queues(tmp_path):
 async with fixture(tmp_path)as f:
  await approved(f)
  for scope in ({'owner':'bob','collection':'shop'},{'owner':'alice','collection':'other'}):
   with pytest.raises(ValueError):await f['runtime'].run(f['key'],expected_generation=1,**scope)
  with pytest.raises(ValueError):await f['runtime'].run(f['key'],expected_generation=0,**SCOPE)
  assert not f['runtime'].runs.records()and not f['remote']['auth']

@pytest.mark.anyio
async def test_changed_credential_fences_queued_request_before_connect(tmp_path):
 async with fixture(tmp_path)as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE)
  f['private'].set_secret(f['key'],'PASSWORD','changed-private-value',owner='alice')
  await settle(f)
  assert not f['remote']['auth']
  assert f['runtime'].describe(f['key'],**SCOPE)['acquisition']['error']=='storage_connection_changed'

@pytest.mark.anyio
async def test_reconcile_reloads_current_completed_pass_under_lease(tmp_path):
 async with fixture(tmp_path,mode='watch')as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE)
  runs=f['runtime'].runs;row=runs.records()[0];runs.update(row,'running',cleanup=False)
  stale=runs.records()[0]
  runs.update(row,'waiting',cleanup=True)
  assert runs.reconcile(stale)
  current=runs.records()[0]
  assert(current['status'],current['cleanup'],current['desired'])==('waiting',1,'run')

@pytest.mark.anyio
async def test_worker_identity_covers_funding_and_authority_source_changes(tmp_path,monkeypatch):
 from pathlib import Path
 async with fixture(tmp_path)as f:
  assert f['runtime'].healthy()
  original=Path.read_bytes
  for name in ('source_service.py','source_intake.py','custom_connectors.py','custom_execution.py','workspace_identity.py'):
   def changed(path,selected=name):
    value=original(path)
    return value+b'\n# synthetic new host revision'if path.name==selected else value
   monkeypatch.setattr(Path,'read_bytes',changed)
   upgraded=StorageRuntime(f['authority'],source_host=f['host'],clock=f['host'].clock,access_state=f['state'],environment='demo')
   assert not upgraded.healthy()
  monkeypatch.setattr(Path,'read_bytes',original)

@pytest.mark.anyio
async def test_new_approved_generation_can_run_after_prior_complete(tmp_path):
 async with fixture(tmp_path)as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE)
  row=f['runtime'].runs.records()[0];f['runtime'].runs.update(row,'complete',cleanup=True,retire=True)
  changed={**f['target'],'root':'/next-folder'}
  proposal=f['authority'].propose(f['key'],changed,'Import the next folder.',expected_revision=2,**SCOPE)
  value=await f['runtime'].approve(f['key'],proposal['id'],**SCOPE)
  assert value['generation']==2 and value['acquisition']['status']=='idle'and value['can_run']
  await f['runtime'].run(f['key'],expected_generation=2,**SCOPE)
  assert f['runtime'].runs.records()[0]['epoch']==2

@pytest.mark.anyio
@pytest.mark.parametrize('remove',['principal','membership'])
async def test_membership_removal_during_read_fences_chunks_and_drains(tmp_path,remove):
 async with fixture(tmp_path,mode='watch')as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE);await settle(f)
  f['remote']['clock'][0]+=31;f['heartbeat']();f['service'].health()
  entered=asyncio.Event();transport=f['remote']['transport']
  @asynccontextmanager
  async def pending(*args):
   async with transport(*args)as reader:
    async def read(handle,offset,length):entered.set();await asyncio.Future()
    reader.read=read;yield reader
  f['acquirer'].connector=pending;await f['service'].step();await asyncio.wait_for(entered.wait(),3)
  with f['access'].connect()as db:
   if remove=='principal':db.execute('DELETE FROM principals WHERE id=?',('alice',))
   else:db.execute('DELETE FROM principal_environments WHERE principal_id=? AND environment=?',('alice','demo'))
  await asyncio.wait_for(asyncio.gather(*f['service'].active.values(),return_exceptions=True),3)
  record=f['runtime'].runs.records()[0]
  assert record['cleanup']==1 and record['desired']=='idle'
  uploads=f['remote']['store'].list_uploads(f['remote']['actor'],f['host'].records()[0]['binding']['source_id'])
  assert len(uploads)==1 and uploads[0]['committed_bytes']==0
  assert not f['runtime'].funded('alice','shop')

@pytest.mark.anyio
async def test_normal_logout_keeps_durable_owner_approved_watch_authorized(tmp_path):
 async with fixture(tmp_path,mode='watch')as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE)
  f['access'].logout(f['session'])
  assert f['access'].session(f['session'])is None and f['runtime'].member_current('alice')
  await settle(f);f['remote']['clock'][0]+=31;f['heartbeat']();await settle(f)
  result=f['runtime'].describe(f['key'],**SCOPE)
  rows=f['remote']['store'].list_uploads(f['remote']['actor'],result['acquisition']['source_id'])
  assert len(rows)==1 and rows[0]['status']=='sealed'

@pytest.mark.anyio
async def test_explicit_access_binding_cannot_select_another_company(tmp_path):
 async with fixture(tmp_path)as f:
  config=Configuration.load(f['state'])
  path=f['state']/'compute/storage-service.json'
  original=json.loads(path.read_text());atomic_json(path,{**original,'access_state':str(tmp_path),'environment':'demo'})
  with pytest.raises(IntakeError,match='configuration'):Configuration.load(f['state'])
  assert not f['runtime'].healthy()
  atomic_json(path,original)
  foreign=StorageRuntime(f['authority'],source_host=f['host'],access_state=tmp_path,environment='demo')
  assert not foreign.healthy()and not foreign.member_current('alice')

@pytest.mark.anyio
async def test_actual_worker_loop_shutdown_drains_and_closes_readiness(tmp_path):
 async with fixture(tmp_path,mode='watch')as f:
  await approved(f);await f['runtime'].run(f['key'],expected_generation=1,**SCOPE)
  task=asyncio.create_task(f['service'].run())
  try:
   for _ in range(100):
    if f['remote']['auth']:break
    await asyncio.sleep(.01)
   assert f['remote']['auth']
  finally:f['service'].stopping.set();await asyncio.wait_for(task,3)
  assert not f['runtime'].healthy()and not f['service'].acquirer.active
  fd=f['runtime'].runs.lock(f['key']);assert fd is not None;os.close(fd)

@pytest.mark.anyio
async def test_standalone_entrypoint_pulses_and_sigterm_closes_without_network(tmp_path):
 import subprocess,sys,signal
 from pathlib import Path
 async with fixture(tmp_path)as f:
  # No run intent exists, so this real process can publish health without SSH.
  import layout
  entry=layout.ROOT/'run.py'
  f['runtime'].clock=time.time
  (f['runtime'].runs.root/'heartbeat.json').unlink()
  process=await asyncio.create_subprocess_exec(sys.executable,str(entry),'storage_service','--state',str(f['state']),
   stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
  try:
   for _ in range(200):
    if (f['runtime'].runs.root/'heartbeat.json').exists():break
    if process.returncode is not None:break
    await asyncio.sleep(.02)
   record=json.loads((f['runtime'].runs.root/'heartbeat.json').read_text())
   assert record['ready']is True and f['runtime'].healthy()and not f['remote']['auth']
   process.send_signal(signal.SIGTERM)
   _,stderr=await asyncio.wait_for(process.communicate(),5)
   assert process.returncode==0,stderr.decode()
   assert json.loads((f['runtime'].runs.root/'heartbeat.json').read_text())['ready']is False
  finally:
   if process.returncode is None:process.kill();await process.wait()

@pytest.mark.anyio
async def test_unresponsive_remote_file_close_is_bounded_before_ssh_abort(tmp_path,monkeypatch):
 import sftp_transport
 from sftp_transport import SFTPReader,RemoteFile
 from types import SimpleNamespace
 import stat
 attrs=SimpleNamespace(size=1,mtime=0,mtime_ns=0,permissions=stat.S_IFREG|0o400)
 entered=asyncio.Event();cancelled=asyncio.Event()
 class Handle:
  async def stat(self):return attrs
  async def close(self):
   entered.set()
   try:await asyncio.Future()
   finally:cancelled.set()
 class Client:
  async def realpath(self,path):return path
  async def lstat(self,path):return attrs
  async def open(self,*args,**kwargs):return Handle()
 reader=SFTPReader(SimpleNamespace(root='/'),Client(),lambda:True)
 monkeypatch.setattr(sftp_transport,'CLOSE_SECONDS',.03,raising=False)
 async def operation():
  async with reader.open(RemoteFile.from_attrs('recording.mp4',attrs)):pass
 started=time.monotonic()
 with pytest.raises(TimeoutError):await asyncio.wait_for(operation(),.5)
 assert time.monotonic()-started<.2
 assert entered.is_set()and cancelled.is_set()
