import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sys
import os
import pytest
HERE=Path(__file__).parents[1];BASE=Path(os.environ.get('ROCOTO_SOURCE_TEST_BASE',HERE))
sys.path[:0]=[str(HERE),str(BASE),str(BASE/'tests')]
from source_intake import SourceStore,IntakeActor,IntakeError
from source_routes import SourceRoutePolicy
from link_intake import LinkIntake,LinkError
from link_download import CHUNK,classify,validate_url,media_magic

@pytest.fixture
def anyio_backend():return'asyncio'
VIDEO=b'\x00\x00\x00\x18ftypisom'+b'x'*(CHUNK+4000)
class Host:
 def __init__(self,state):self.state=state;self.count=0;self.ready=True
 def capabilities(self,actor):return{'can_upload':self.ready,'can_manage_imports':True}
 def policy(self):return SourceRoutePolicy(intake_enabled=self.ready,worker_ready=self.ready,empty_workspace_collection='shop')
 def collections(self,actor):return{'shop'}
 def authorize(self,*args,**kwargs):pass
 def begin_remote(self,store,actor,source_id,**kwargs):
  self.count+=1;return store.begin_upload(actor,source_id,**kwargs)
class Transport:
 def __init__(self):self.calls=0;self.data=VIDEO;self.fail=False;self.wait=None;self.entered=asyncio.Event();self.stopped=0;self.etag='"v1"'
 @asynccontextmanager
 async def open(self,url,*,current,max_bytes):
  self.calls+=1;self.current=current;self.metadata={'filename':'clip.mp4','size_bytes':len(self.data),'content_type':'video/mp4','etag':self.etag}
  try:yield self
  finally:self.stopped+=1
 async def chunks(self):
  for offset in range(0,len(self.data),CHUNK):
   if offset:
    self.entered.set()
    if self.wait:await self.wait.wait()
    if self.fail:self.fail=False;raise ConnectionError('secret signed URL value')
   yield self.data[offset:offset+CHUNK]

def setup(tmp_path,**kw):
 state=tmp_path/'company';store=SourceStore(state);host=Host(state);transport=Transport();allowed=[True]
 service=LinkIntake(state,store=store,host=host,authorized=lambda owner,collection:allowed[0]and owner=='alice'and collection=='shop',enabled=True,transport=transport,**kw)
 return service,IntakeActor('alice'),host,transport,allowed

def create(service,actor,url='https://cdn.example.com/clip.mp4?signature=private-shared-value',key='one'):
 return service.create(actor,collection='shop',url=url,idempotency_key=key)

@pytest.mark.parametrize('url',['http://host.example.com/a','https://u:p@host.example.com/a','https://127.0.0.1/a','https://169.254.169.254/a','https://10.0.0.1/a','https://[::1]/a','https://host.example.com:444/a','https://host.example.com/a#token','https://host.example.com/\n'])
def test_reject_unsafe_urls(url):
 with pytest.raises(LinkError):validate_url(url)

def test_provider_routes_preserve_dropbox_key_and_surface_browser_steps():
 provider,url,action=classify('https://www.dropbox.com/scl/fi/abc/clip.mp4?rlkey=private&dl=0')
 assert provider=='dropbox'and 'rlkey=private'in url and 'dl=1'in url and action is None
 for link in ['https://drive.google.com/file/d/abc/view?resourcekey=secret','https://we.tl/abc','https://wetransfer.com/downloads/abc']:
  assert classify(link)[2]=='browser_download_required'
 assert classify('https://drive.usercontent.google.com/download?id=abc&export=download')[2]is None

@pytest.mark.anyio
async def test_seals_real_bytes_same_pipeline_and_private_url(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);row=create(service,actor);result=await service.run(actor,row['id'])
 assert result['status']=='received'and result['received_bytes']==len(VIDEO)
 view=result['import'];assert view['asset']['status']=='received'and view['upload']['sha256']==hashlib.sha256(VIDEO).hexdigest()
 assert service.store.claim_job(worker_id='test').stage=='prepare'
 assert (service.store.blobs/view['upload']['sha256']).read_bytes()==VIDEO
 assert'private-shared-value'not in json.dumps(result)
 assert b'private-shared-value'not in service.database.read_bytes()
 assert not(service.root/'links.sqlite3-wal').exists()
 assert create(service,actor)['id']==row['id']
 await service.run(actor,row['id']);assert transport.calls==1
 await service.close()

@pytest.mark.anyio
async def test_interrupted_download_replays_hashes_and_resumes_committed_chunks(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);transport.fail=True;row=create(service,actor)
 first=await service.run(actor,row['id']);assert first['status']=='interrupted'and first['received_bytes']==CHUNK
 service2=LinkIntake(service.state,store=service.store,host=host,authorized=service.authorized,enabled=True,transport=transport)
 await service2.reconcile();last=await service2.run(actor,row['id'])
 assert last['status']=='received'and last['import_id']==first['import_id']and len(service.store.list_imports(actor))==1
 assert transport.calls==2
 await service.close();await service2.close()

@pytest.mark.anyio
async def test_changed_bytes_or_validator_never_seal_old_partial(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);transport.fail=True;row=create(service,actor);await service.run(actor,row['id'])
 transport.data=VIDEO[:20]+b'z'+VIDEO[21:]
 result=await service.run(actor,row['id']);assert result['status']!='received'and result['import']['asset']is None
 transport.etag='"v2"';result=await service.run(actor,row['id']);assert result['action']['code']=='remote_changed'
 await service.close()

@pytest.mark.anyio
async def test_pages_and_magic_do_not_reserve_allowance(tmp_path):
 service,actor,host,transport,_=setup(tmp_path)
 row=create(service,actor,'https://we.tl/abc');assert row['status']=='needs_action';await service.run(actor,row['id']);assert transport.calls==0
 transport.data=b'<html>login private-shared-value</html>';row=create(service,actor,key='html')
 result=await service.run(actor,row['id']);assert result['action']['code']=='video_file_required'and host.count==0
 assert not service.store.list_imports(actor)
 await service.close()

@pytest.mark.anyio
async def test_stop_drains_active_and_other_instance_stops_before_more_chunks(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);transport.wait=asyncio.Event();row=create(service,actor)
 await service.start(actor,row['id']);await transport.entered.wait()
 cancelled=await service.cancel(actor,row['id']);assert cancelled['status']=='cancelled'and transport.stopped==1
 assert cancelled['import']['upload']['status']=='cancelled'
 await service.close()

@pytest.mark.anyio
async def test_membership_revocation_fences_chunk_and_funding(tmp_path):
 service,actor,host,transport,allowed=setup(tmp_path);transport.wait=asyncio.Event();row=create(service,actor)
 task=asyncio.create_task(service.run(actor,row['id']));await transport.entered.wait();allowed[0]=False;transport.wait.set()
 with pytest.raises(LinkError,match='workspace_access_required'):await task
 assert service.store.list_imports(actor)[0]['upload']['committed_bytes']==CHUNK
 await service.close()

def test_owner_observer_company_and_idempotency_boundaries(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);row=create(service,actor)
 with pytest.raises(LinkError):service.get(IntakeActor('bob'),row['id'])
 with pytest.raises(IntakeError):create(service,IntakeActor('alice',True),key='observer')
 other,_,_,_,_=setup(tmp_path/'other')
 with pytest.raises(LinkError):other.get(actor,row['id'])
 with pytest.raises(LinkError,match='idempotency_conflict'):create(service,actor,url='https://different.example.com/a.mp4')

@pytest.mark.anyio
async def test_two_cross_process_slots_and_waiter_does_not_cancel_owner(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);transport.wait=asyncio.Event();rows=[create(service,actor,key=str(n))for n in range(3)]
 tasks=[asyncio.create_task(service.run(actor,rows[0]['id']))];await asyncio.wait_for(transport.entered.wait(),3)
 transport.entered.clear();tasks.append(asyncio.create_task(service.run(actor,rows[1]['id'])));await asyncio.wait_for(transport.entered.wait(),3)
 with pytest.raises(LinkError,match='download_capacity'):await service.run(actor,rows[2]['id'])
 with pytest.raises(LinkError,match='download_capacity'):await service.run(actor,rows[0]['id'])
 assert all(not t.done()for t in tasks)
 transport.wait.set();await asyncio.gather(*tasks);await service.close()

@pytest.mark.anyio
async def test_lost_admission_reply_stop_recovers_upload_and_drains_repeated_cancel(tmp_path):
 import threading
 service,actor,host,transport,_=setup(tmp_path);row=create(service,actor)
 admitted=threading.Event();release=threading.Event();original=host.begin_remote
 def delayed(*args,**kwargs):
  result=original(*args,**kwargs);admitted.set();assert release.wait(3);return result
 host.begin_remote=delayed
 await service.start(actor,row['id']);assert await asyncio.to_thread(admitted.wait,2)
 stopping=asyncio.create_task(service.cancel(actor,row['id']))
 await asyncio.sleep(.02);stopping.cancel();await asyncio.sleep(.02);stopping.cancel()
 assert not stopping.done();release.set()
 with pytest.raises(asyncio.CancelledError):await stopping
 last=service.get(actor,row['id'])
 assert last['status']=='cancelled'and last['import_id']and last['import']['upload']['status']=='cancelled'
 assert transport.stopped==1 and service.tasks[(actor.principal_id,row['id'])].done()
 await service.close()

@pytest.mark.anyio
async def test_orphan_reconcile_and_idempotent_retry_keep_original_receipt(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);row=create(service,actor)
 service._update(actor,row['id'],status='downloading')
 await service.reconcile();assert service.get(actor,row['id'])['status']=='interrupted'
 last=await service.run(actor,row['id']);assert last['status']=='received'
 # Crash after SourceStore seal but before the public row catches up.
 service._update(actor,row['id'],status='downloading')
 await service.reconcile();assert service.get(actor,row['id'])['status']=='received'
 await service.run(actor,row['id']);assert transport.calls==1
 await service.close()

@pytest.mark.anyio
async def test_quota_and_funding_fail_before_appending_media(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);host.ready=False;row=create(service,actor)
 result=await service.run(actor,row['id']);assert result['status']=='interrupted'and transport.calls==0
 host.ready=True
 from dataclasses import replace
 service.store.limits=replace(service.store.limits,max_storage_bytes=len(VIDEO),max_file_bytes=len(VIDEO),max_chunk_bytes=CHUNK)
 source=service.store.ensure_upload_source(actor,collection='shop')
 service.store.begin_upload(actor,source['id'],filename='prior.mp4',size=1,idempotency_key='prior')
 result=await service.run(actor,row['id']);assert len(service.store.list_imports(actor))==1 and result['import_id']is None
 assert result['status']!='received'
 await service.close()

@pytest.mark.anyio
async def test_cross_process_held_lease_returns_pending_then_repeat_stop_confirms(tmp_path,monkeypatch):
 import link_intake
 monkeypatch.setattr(link_intake,'STOP_WAIT_SECONDS',.025)
 service,actor,host,transport,_=setup(tmp_path);row=create(service,actor)
 with service._lease(row['id']):
  pending=await service.cancel(actor,row['id'])
  assert pending['status']=='cancelling'
  assert not service._current(actor,row['id'])
 last=await service.cancel(actor,row['id']);assert last['status']=='cancelled'
 await service.close()

@pytest.mark.anyio
async def test_remote_stop_intent_is_completed_by_the_running_acquirer(tmp_path):
 service,actor,host,transport,_=setup(tmp_path);transport.wait=asyncio.Event();row=create(service,actor)
 task=asyncio.create_task(service.run(actor,row['id']));await transport.entered.wait()
 service._update(actor,row['id'],status='cancelling',error='acquisition_stopped');transport.wait.set()
 result=await task
 assert result['status']=='cancelled'and result['import']['upload']['status']=='cancelled'and transport.stopped==1
 await service.close()
