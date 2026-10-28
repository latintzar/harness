import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import socket
import sys
import threading

import asyncssh
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from source_intake import SourceStore,IntakeActor,IntakeLimits,IntakeError
from sftp_transport import SFTPConfig,SFTPCredentials,SFTPError,connect,public_socket,validate_target,digest
from sftp_intake import SFTPAcquirer
from workspace_identity import workspace_scope

@pytest.fixture
def anyio_backend():return 'asyncio'

@asynccontextmanager
async def fixture(tmp_path,*,content=b'recording bytes',mode='one_time',limits=None):
 remote=tmp_path/'remote';remote.mkdir();(remote/'recording.mp4').write_bytes(content)
 hostkey=asyncssh.generate_private_key('ssh-ed25519');auth=[];operations=[]
 class Server(asyncssh.SSHServer):
  def begin_auth(self,username):return True
  def password_auth_supported(self):return True
  def validate_password(self,username,password):auth.append((username,password));return username=='camera'and password=='synthetic-private-value'
  def session_requested(self):return False
  def connection_requested(self,*args):raise AssertionError('Port forwarding requested')
 class SFTP(asyncssh.SFTPServer):
  def __init__(self,channel):super().__init__(channel,chroot=str(remote))
  def open(self,path,pflags,attrs):
   operations.append(('open',pflags));assert pflags==asyncssh.FXF_READ
   return super().open(path,pflags,attrs)
 server=await asyncssh.listen('127.0.0.1',0,server_factory=Server,server_host_keys=[hostkey],sftp_factory=SFTP)
 clock=[1000.0];store=SourceStore(tmp_path/'company',limits=limits,clock=lambda:clock[0]);actor=IntakeActor('alice')
 target=validate_target({'host':'recordings.example.com','port':22,'root':'/','host_key':hostkey.export_public_key().decode()})
 source=store.ensure_connection_source(actor,collection='shop',connection_id='connection',target_sha256=digest(target),name='Recordings')
 config=SFTPConfig(workspace_scope(store.root.parent),'alice','shop',source['id'],1,**target,mode=mode,stable_seconds=30,poll_seconds=30)
 creds=SFTPCredentials(config.sha256,'camera',password='synthetic-private-value')
 sockets=[]
 async def connected(cfg,current):
  assert current();sock=socket.socket();sock.setblocking(False);sockets.append(sock)
  await asyncio.get_running_loop().sock_connect(sock,('127.0.0.1',server.get_port()));return sock
 @asynccontextmanager
 async def transport(cfg,bundle,current):
  async with connect(cfg,bundle,current,socket_factory=connected)as reader:yield reader
 acquirer=SFTPAcquirer(store,connector=transport,clock=lambda:clock[0])
 try:yield locals()
 finally:
  await acquirer.close();server.close();await server.wait_closed()
  assert all(s.fileno()==-1 for s in sockets)

async def pull(f,**options):
 return await f['acquirer'].pull(options.pop('config',f['config']),actor=options.pop('actor',f['actor']),
  credentials=options.pop('credentials',lambda cfg:replace(f['creds'],config_sha256=cfg.sha256)),current=options.pop('current',lambda cfg:True),
  cancelled=options.pop('cancelled',asyncio.Event()),**options)

@pytest.mark.anyio
async def test_real_sftp_stability_seal_dedupe_and_prepare_queue(tmp_path):
 async with fixture(tmp_path)as f:
  first=await pull(f);assert first['status']=='waiting'and first['last_check']==1000 and not f['store'].list_uploads(f['actor'],f['source']['id'])
  f['clock'][0]+=31
  result=await pull(f);assert result['status']=='complete'and result['last_import']==1031
  rows=f['store'].list_uploads(f['actor'],f['source']['id']);assert len(rows)==1 and rows[0]['status']=='sealed'
  assert (f['store'].blobs/rows[0]['sha256']).read_bytes()==b'recording bytes'
  assert f['store'].get_asset(f['actor'],rows[0]['asset_id'])['status']=='received'
  assert f['store'].claim_job(worker_id='synthetic',asset_id=rows[0]['asset_id']).stage=='prepare'
  auth=len(f['auth']);f['clock'][0]+=31;await pull(f);assert len(f['auth'])==auth
  assert all(value==asyncssh.FXF_READ for _,value in f['operations'])

@pytest.mark.anyio
async def test_host_key_mismatch_happens_before_credentials(tmp_path):
 async with fixture(tmp_path)as f:
  config=replace(f['config'],host_key=asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode())
  with pytest.raises(SFTPError,match='host_key_mismatch'):await pull(f,config=config)
  assert not f['auth']and not f['operations']

@pytest.mark.anyio
async def test_owner_company_observer_and_revoked_never_connect(tmp_path):
 async with fixture(tmp_path)as f:
  for cfg,actor in [(f['config'],IntakeActor('bob')),(replace(f['config'],company_id='other'),f['actor']),
      (replace(f['config'],collection='other'),f['actor']),(f['config'],IntakeActor('alice',True))]:
   with pytest.raises((SFTPError,IntakeError)):await pull(f,config=cfg,actor=actor)
  with pytest.raises(SFTPError,match='authority_changed'):await pull(f,current=lambda cfg:False)
  assert not f['auth']

@pytest.mark.anyio
async def test_watch_late_name_changes_and_temporary_symlink_ignored(tmp_path):
 async with fixture(tmp_path,mode='watch')as f:
  (f['remote']/'unfinished.mp4.part').write_bytes(b'partial');(f['remote']/'link.mp4').symlink_to('recording.mp4')
  await pull(f);f['clock'][0]+=31;await pull(f)
  (f['remote']/'a-late.mp4').write_bytes(b'late')
  f['clock'][0]+=31;await pull(f);f['clock'][0]+=31;await pull(f)
  rows=f['store'].list_uploads(f['actor'],f['source']['id']);assert {r['filename']for r in rows}=={'a-late.mp4','recording.mp4'}
  (f['remote']/'recording.mp4').write_bytes(b'new longer recording')
  f['clock'][0]+=31;await pull(f);f['clock'][0]+=31;await pull(f);f['clock'][0]+=31;await pull(f)
  rows=f['store'].list_uploads(f['actor'],f['source']['id']);assert len(rows)==3
  assert len({r['object_id']for r in rows})==3 and len({r['sha256']for r in rows})==3

@pytest.mark.anyio
async def test_disconnect_resumes_committed_offset_and_whole_file_is_verified(tmp_path):
 content=b'a'*1024*1024+b'b'*4096
 async with fixture(tmp_path,content=content)as f:
  await pull(f);f['clock'][0]+=31;transport=f['transport'];failed=[False]
  @asynccontextmanager
  async def broken(cfg,creds,current):
   async with transport(cfg,creds,current)as reader:
    read=reader.read
    async def interrupt(handle,offset,length):
     if offset==1024*1024 and not failed[0]:failed[0]=True;raise ConnectionError('synthetic secret')
     return await read(handle,offset,length)
    reader.read=interrupt;yield reader
  f['acquirer'].connector=broken
  with pytest.raises(SFTPError,match='connection_failed'):await pull(f)
  rows=f['store'].list_uploads(f['actor'],f['source']['id']);assert len(rows)==1 and rows[0]['committed_bytes']==1024*1024
  await f['acquirer'].close()
  f['acquirer']=SFTPAcquirer(f['store'],connector=transport,clock=lambda:f['clock'][0]);f['clock'][0]+=31
  assert(await pull(f))['status']=='complete'
  rows=f['store'].list_uploads(f['actor'],f['source']['id']);assert len(rows)==1 and rows[0]['sha256']==hashlib.sha256(content).hexdigest()

@pytest.mark.anyio
async def test_same_stat_mutation_during_transfer_rejected_by_full_reread(tmp_path):
 async with fixture(tmp_path,content=b'a'*100)as f:
  await pull(f);f['clock'][0]+=31;transport=f['transport'];changed=[False]
  @asynccontextmanager
  async def changing(cfg,creds,current):
   async with transport(cfg,creds,current)as reader:
    read=reader.read
    async def mutate(handle,offset,length):
     value=await read(handle,offset,length)
     if not changed[0]:
      changed[0]=True;path=f['remote']/'recording.mp4';old=path.stat();path.write_bytes(b'b'*100);os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
     return value
    reader.read=mutate;yield reader
  f['acquirer'].connector=changing
  with pytest.raises(IntakeError,match='checksum_mismatch'):await pull(f)
  assert not list(f['store'].blobs.iterdir())
  assert f['store'].list_uploads(f['actor'],f['source']['id'])[0]['status']=='cancelled'

@pytest.mark.anyio
async def test_quota_refusal_precedes_read_and_errors_hide_auth(tmp_path):
 limits=IntakeLimits(max_file_bytes=8,max_storage_bytes=8,max_chunk_bytes=8)
 async with fixture(tmp_path,content=b'x'*20,limits=limits)as f:
  await pull(f);f['clock'][0]+=31
  with pytest.raises(SFTPError,match='file_limit'):await pull(f)
  assert not f['operations']
  assert 'synthetic-private-value'not in repr(f['creds'])
  assert 'synthetic-private-value'not in str(await f['acquirer'].status(f['config'],actor=f['actor']))

@pytest.mark.anyio
async def test_stopped_transfer_drains_and_restart_keeps_cursor(tmp_path):
 async with fixture(tmp_path,content=b'a'*1024*1024+b'b'*4096)as f:
  await pull(f);f['clock'][0]+=31;transport=f['transport'];entered=asyncio.Event()
  @asynccontextmanager
  async def waiting(cfg,creds,current):
   async with transport(cfg,creds,current)as reader:
    read=reader.read
    async def pause(handle,offset,length):
     if offset==1024*1024:entered.set();await asyncio.Event().wait()
     return await read(handle,offset,length)
    reader.read=pause;yield reader
  f['acquirer'].connector=waiting;task=asyncio.create_task(pull(f));await asyncio.wait_for(entered.wait(),5)
  await f['acquirer'].stop(f['config'],actor=f['actor'])
  with pytest.raises(asyncio.CancelledError):await task
  assert not f['acquirer'].active
  f['acquirer'].connector=transport;f['clock'][0]+=31;assert(await pull(f))['status']=='complete'

def test_connection_source_retry_target_identity_and_owner_are_transactional(tmp_path):
 store=SourceStore(tmp_path/'company');actor=IntakeActor('alice')
 options={'collection':'shop','connection_id':'connection','target_sha256':'a'*64,'name':'Recordings'}
 first=store.ensure_connection_source(actor,**options)
 assert store.ensure_connection_source(actor,**options)['id']==first['id']
 assert store.ensure_connection_source(actor,**{**options,'target_sha256':'b'*64})['id']!=first['id']
 assert store.ensure_connection_source(IntakeActor('bob'),**options)['id']!=first['id']
 with pytest.raises(IntakeError):store.ensure_connection_source(IntakeActor('alice',True),**options)

@pytest.mark.anyio
async def test_source_store_late_stop_under_write_lock_blocks_begin_and_append(tmp_path):
 store=SourceStore(tmp_path/'company');actor=IntakeActor('alice');source=store.create_source(actor,collection='shop',name='Recordings')
 current=[True];entered=threading.Event();release=threading.Event()
 original=store._connect
 def blocked():entered.set();release.wait(3);return original()
 store._connect=blocked
 task=asyncio.create_task(asyncio.to_thread(store.begin_upload,actor,source['id'],filename='a.mp4',size=1,idempotency_key='first',current=lambda:current[0]))
 await asyncio.to_thread(entered.wait,2);current[0]=False;release.set()
 with pytest.raises(IntakeError,match='acquisition_stopped'):await task
 store._connect=original;assert not store.list_uploads(actor,source['id'])

@pytest.mark.parametrize('change',[{'root':'/../secret'},{'root':'/path//extra'},{'host':'host$(command)'},{'port':0},{'root':'relative'},{'host_key':'invalid'},{'host':'host\nsecret'}])
def test_configuration_rejects_paths_commands_and_bad_keys(change):
 value={'host':'recordings.example.com','port':22,'root':'/recordings','host_key':asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()}
 with pytest.raises(SFTPError):validate_target({**value,**change})

@pytest.mark.anyio
@pytest.mark.parametrize('addresses',[['127.0.0.1'],['10.0.0.1'],['169.254.169.254'],['::1'],['::ffff:127.0.0.1'],['93.184.216.34','10.0.0.1']])
async def test_production_socket_rejects_every_private_resolution_before_connect(monkeypatch,addresses):
 import sftp_transport
 async def resolve(host):return addresses
 monkeypatch.setattr(sftp_transport,'public_addresses',resolve)
 with pytest.raises(ValueError):await public_socket(type('Config',(),{'host':'recordings.example.com','port':22})(),lambda:True)

@pytest.mark.anyio
async def test_pagination_restart_replays_and_eventually_acquires_every_file(tmp_path):
 async with fixture(tmp_path)as f:
  for n in range(65):(f['remote']/f'archive-{n:03d}.mp4').write_bytes(str(n).encode())
  await pull(f)
  assert f['acquirer'].journal.cursor(f['config'])
  await f['acquirer'].close()
  f['acquirer']=SFTPAcquirer(f['store'],connector=f['transport'],clock=lambda:f['clock'][0])
  for n in range(30):
   f['clock'][0]+=31;result=await pull(f)
   if result['status']=='complete':break
  else:pytest.fail('The durable scan cursor did not finish.')
  rows=f['store'].list_uploads(f['actor'],f['source']['id'])
  assert len(rows)==66 and all(row['status']=='sealed'for row in rows)
  assert len({row['object_id']for row in rows})==66

@pytest.mark.anyio
async def test_server_credential_echo_never_reaches_durable_metadata(tmp_path):
 async with fixture(tmp_path)as f:
  (f['remote']/'synthetic-private-value.mp4').write_bytes(b'opaque')
  with pytest.raises(SFTPError,match='credential_echo'):await pull(f)
  assert b'synthetic-private-value'not in f['acquirer'].journal.path.read_bytes()
  assert not f['store'].list_uploads(f['actor'],f['source']['id'])

@pytest.mark.anyio
async def test_production_socket_connects_only_the_resolved_public_literal(monkeypatch):
 import sftp_transport
 calls=[]
 async def resolve(host):calls.append(('dns',host));return ['93.184.216.34']
 async def dial(sock,address):calls.append(('connect',address))
 monkeypatch.setattr(sftp_transport,'public_addresses',resolve)
 monkeypatch.setattr(asyncio.get_running_loop(),'sock_connect',dial)
 sock=await public_socket(type('Config',(),{'host':'recordings.example.com','port':2222})(),lambda:True)
 sock.close();assert calls==[('dns','recordings.example.com'),('connect',('93.184.216.34',2222))]

@pytest.mark.anyio
async def test_source_guard_rolls_back_late_append_and_seal(tmp_path,monkeypatch):
 store=SourceStore(tmp_path/'company');actor=IntakeActor('alice');source=store.create_source(actor,collection='shop',name='recordings')
 upload=store.begin_upload(actor,source['id'],filename='a.mp4',size=3,idempotency_key='guard')
 original=os.fsync;alive=[True]
 def stop_after_flush(fd):original(fd);alive[0]=False
 monkeypatch.setattr(os,'fsync',stop_after_flush)
 with pytest.raises(IntakeError,match='acquisition_stopped'):store.append_chunk(actor,upload['id'],offset=0,data=b'abc',current=lambda:alive[0])
 assert store.get_upload(actor,upload['id'])['committed_bytes']==0
 monkeypatch.setattr(os,'fsync',original);store.recover()
 assert (store.quarantine/upload['id']).stat().st_size==0
 store.append_chunk(actor,upload['id'],offset=0,data=b'abc')
 original_digest=hashlib.file_digest;alive[0]=True
 def stop_after_hash(*args,**kwargs):value=original_digest(*args,**kwargs);alive[0]=False;return value
 monkeypatch.setattr(hashlib,'file_digest',stop_after_hash)
 with pytest.raises(IntakeError,match='acquisition_stopped'):store.seal_upload(actor,upload['id'],current=lambda:alive[0])
 assert store.get_upload(actor,upload['id'])['status']=='uploading'and not list(store.blobs.iterdir())

@pytest.mark.anyio
async def test_failed_host_admission_leaves_scan_retriable_and_no_source_bytes(tmp_path):
 async with fixture(tmp_path)as f:
  admitted=[]
  async def funded(*args,**kwargs):admitted.append(kwargs);raise IntakeError('embedding_allowance_required')
  f['acquirer'].begin_upload=funded
  await pull(f);f['clock'][0]+=31
  with pytest.raises(IntakeError,match='embedding_allowance_required'):await pull(f)
  assert admitted[0]['object_key']=='recording.mp4'and callable(admitted[0]['current'])
  assert not f['operations']and not f['store'].list_uploads(f['actor'],f['source']['id'])
  f['acquirer'].begin_upload=f['store'].begin_upload;f['clock'][0]+=31
  assert(await pull(f))['status']=='complete'

@pytest.mark.anyio
async def test_scoped_parallel_admission_is_shared_across_coordinators(tmp_path):
 async with fixture(tmp_path)as f:
  entered=asyncio.Queue();release=asyncio.Event();other=[];tasks=[]
  @asynccontextmanager
  async def waiting(config,creds,current):
   async with f['transport'](config,creds,current)as reader:
    await entered.put(True);await release.wait();yield reader
  try:
   for n in range(3):
    source=f['store'].ensure_connection_source(f['actor'],collection='shop',connection_id=f'parallel-{n}',target_sha256=f['config'].target_sha256,name='Archive')
    cfg=replace(f['config'],source_id=source['id']);acq=SFTPAcquirer(f['store'],connector=waiting,clock=lambda:f['clock'][0]);other.append(acq)
    task=asyncio.create_task(acq.pull(cfg,actor=f['actor'],credentials=lambda c:replace(f['creds'],config_sha256=c.sha256),current=lambda c:True,cancelled=asyncio.Event()));tasks.append(task)
    if n<2:await asyncio.wait_for(entered.get(),5)
   with pytest.raises(SFTPError,match='capacity'):await tasks[-1]
   assert len(f['auth'])==2
  finally:
   for task in tasks:task.cancel()
   await asyncio.gather(*tasks,return_exceptions=True)
   for acq in other:await acq.close()

@pytest.mark.anyio
async def test_oversized_recording_does_not_starve_other_files(tmp_path):
 limits=IntakeLimits(max_file_bytes=8,max_storage_bytes=32,max_chunk_bytes=8)
 async with fixture(tmp_path,content=b'big'*20,limits=limits)as f:
  (f['remote']/'z-small.mp4').write_bytes(b'ok')
  await pull(f);f['clock'][0]+=31
  with pytest.raises(SFTPError,match='file_limit'):await pull(f)
  rows=f['store'].list_uploads(f['actor'],f['source']['id'])
  assert len(rows)==1 and rows[0]['filename']=='z-small.mp4'and rows[0]['status']=='sealed'
  f['clock'][0]+=31;assert(await pull(f))['status']=='needs_attention'
  assert len(f['store'].list_uploads(f['actor'],f['source']['id']))==1

@pytest.mark.anyio
async def test_expired_partial_retires_before_new_admission(tmp_path):
 async with fixture(tmp_path,content=b'a'*1024*1024+b'b'*4096,limits=IntakeLimits(upload_ttl_seconds=35))as f:
  await pull(f);f['clock'][0]+=31;transport=f['transport']
  @asynccontextmanager
  async def interrupted(cfg,creds,current):
   async with transport(cfg,creds,current)as reader:
    original=reader.read
    async def read(handle,offset,length):
     if offset:raise ConnectionError('Synthetic interruption')
     return await original(handle,offset,length)
    reader.read=read;yield reader
  f['acquirer'].connector=interrupted
  with pytest.raises(SFTPError):await pull(f)
  f['acquirer'].connector=transport;f['clock'][0]+=40
  with pytest.raises(SFTPError,match='transfer_restart_required'):await pull(f)
  assert f['store'].list_uploads(f['actor'],f['source']['id'])[0]['status']=='cancelled'
  f['clock'][0]+=31;assert(await pull(f))['status']=='complete'
  rows=f['store'].list_uploads(f['actor'],f['source']['id']);assert len(rows)==2 and sum(row['status']=='sealed'for row in rows)==1

@pytest.mark.anyio
async def test_real_storage_authority_callbacks_drive_and_revoke_sftp(tmp_path):
 from custom_connectors import CustomConnectors
 from storage_connections import StorageConnections
 async with fixture(tmp_path,mode='watch')as f:
  private=CustomConnectors(f['store'].root.parent);authority=StorageConnections(private,f['store'])
  record=private.create(name='Recordings',description='Import stored recordings.',kind='sftp',collection='shop',owner='alice')
  private.save_package(record['id'],{'summary':'Read the approved folder.','instructions':'Use the fixed storage reader.',
   'secret_fields':[{'name':name,'label':name,'description':'Private login field.'}for name in ('SFTP_USER','SFTP_PASSWORD')],
   'files':[{'path':'README.md','content':'Use the approved storage destination.'}]},expected_revision=1,owner='alice')
  target={name:getattr(f['config'],name)for name in ('host','port','root','host_key','mode','stable_seconds','poll_seconds')}
  target['authentication']={'username_field':'SFTP_USER','password_field':'SFTP_PASSWORD'}
  proposal=authority.propose(record['id'],target,'Read completed recordings.',expected_revision=2,owner='alice',collection='shop')
  authority.approve(record['id'],proposal['id'],owner='alice',collection='shop')
  private.set_secret(record['id'],'SFTP_USER','camera',owner='alice')
  private.set_secret(record['id'],'SFTP_PASSWORD','synthetic-private-value',owner='alice')
  snapshot=authority.snapshot(record['id'],owner='alice',collection='shop');f['config']=snapshot.config
  callbacks={'credentials':lambda cfg:authority.credentials(snapshot),'current':lambda cfg:authority.current(snapshot)}
  await pull(f,**callbacks);f['clock'][0]+=31;await pull(f,**callbacks)
  rows=f['store'].list_uploads(f['actor'],snapshot.config.source_id);assert len(rows)==1 and rows[0]['status']=='sealed'
  authority.pause(record['id'],expected_generation=1,owner='alice',collection='shop')
  before=len(f['auth'])
  with pytest.raises(SFTPError,match='authority_changed'):await pull(f,**callbacks)
  assert len(f['auth'])==before

@pytest.mark.parametrize('crash',[False,True])
def test_accepted_sealing_intent_finishes_or_recovers_once_after_pause(tmp_path,monkeypatch,crash):
 store=SourceStore(tmp_path/'company');actor=IntakeActor('alice')
 source=store.create_source(actor,collection='shop',name='Recordings')
 upload=store.begin_upload(actor,source['id'],filename='archive.mp4',size=3,idempotency_key='accepted')
 store.append_chunk(actor,upload['id'],offset=0,data=b'abc')
 original=store._finish_seal;alive=[True]
 def pause_after_intent(db,row):
  assert row['status']=='sealing'
  alive[0]=False
  if crash:raise OSError('Synthetic interruption after durable intent')
  return original(db,row)
 monkeypatch.setattr(store,'_finish_seal',pause_after_intent)
 if crash:
  with pytest.raises(IntakeError,match='storage_unavailable'):store.seal_upload(actor,upload['id'],current=lambda:alive[0])
  assert store.get_upload(actor,upload['id'])['status']=='sealing'
 else:assert store.seal_upload(actor,upload['id'],current=lambda:alive[0])['status']=='sealed'
 restored=SourceStore(tmp_path/'company');restored.recover();restored.recover()
 result=restored.get_upload(actor,upload['id'])
 assert result['status']=='sealed'and result['sha256']==hashlib.sha256(b'abc').hexdigest()
 asset=restored.get_asset(actor,result['asset_id'])
 assert asset['status']=='received'and len(asset['jobs'])==1 and asset['jobs'][0]['stage']=='prepare'
 assert len(restored.list_uploads(actor,source['id']))==1

@pytest.mark.anyio
async def test_source_wal_lifetime_is_read_only_fresh_and_bounded_by_acquirer(tmp_path):
 import sqlite3
 async with fixture(tmp_path)as f:
  acquirer=f['acquirer'];anchor=acquirer._source_anchor
  wal=Path(str(f['store'].database)+'-wal');inode=wal.stat().st_ino
  assert not anchor.in_transaction
  with pytest.raises(sqlite3.OperationalError,match='readonly'):
   anchor.execute('CREATE TABLE forbidden_anchor_write(value)')
  before=anchor.execute('SELECT count(*) FROM sources').fetchone()[0]
  f['store'].create_source(f['actor'],collection='shop',name='New approved source')
  assert anchor.execute('SELECT count(*) FROM sources').fetchone()[0]==before+1
  assert not anchor.in_transaction
  # Ordinary short-lived store connections cannot tear down/recreate WAL while
  # transport authority checks run. No transaction freezes those fresh checks.
  for _ in range(10):
   f['store'].get_source(f['actor'],f['source']['id'])
   assert wal.stat().st_ino==inode
  await acquirer.close();await acquirer.close()
  with pytest.raises(sqlite3.ProgrammingError,match='closed'):
   anchor.execute('SELECT 1')

@pytest.mark.anyio
async def test_source_anchor_closes_after_repeated_cancel_transport_drain(tmp_path):
 import sqlite3
 async with fixture(tmp_path)as f:
  acquirer=f['acquirer'];anchor=acquirer._source_anchor
  entered=asyncio.Event();cleanup=asyncio.Event();release=asyncio.Event();transport=f['transport']
  @asynccontextmanager
  async def held(*args):
   async with transport(*args)as reader:
    entered.set()
    try:await asyncio.Event().wait();yield reader
    finally:cleanup.set();await release.wait()
  acquirer.connector=held;task=asyncio.create_task(pull(f))
  await asyncio.wait_for(entered.wait(),5)
  closing=asyncio.create_task(acquirer.close());await asyncio.wait_for(cleanup.wait(),5)
  for _ in range(3):
   closing.cancel();await asyncio.sleep(0)
   assert not closing.done()and anchor.execute('SELECT 1').fetchone()==(1,)
  release.set();await asyncio.wait_for(closing,5)
  with pytest.raises(asyncio.CancelledError):await task
  with pytest.raises(sqlite3.ProgrammingError,match='closed'):anchor.execute('SELECT 1')
  assert not acquirer.active


def test_source_anchor_initial_read_failure_closes_connection(tmp_path,monkeypatch):
 import sqlite3
 import sftp_intake
 store=SourceStore(tmp_path/'company');original=sqlite3.connect;closed=[]
 class BrokenAnchor:
  def execute(self,*args):raise sqlite3.OperationalError('Synthetic failed first read')
  def close(self):closed.append(True)
 def connect(*args,**kwargs):
  if kwargs.get('uri')and args[0]==store.database.resolve().as_uri()+'?mode=ro':return BrokenAnchor()
  return original(*args,**kwargs)
 monkeypatch.setattr(sftp_intake.sqlite3,'connect',connect)
 with pytest.raises(sqlite3.OperationalError,match='Synthetic failed first read'):SFTPAcquirer(store)
 assert closed==[True]
