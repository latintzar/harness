import asyncio
from dataclasses import dataclass,replace
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import threading
import pytest
import httpx
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from custom_coordinator import CustomCoordinator,CoordinatorError,GatewaySocket
from custom_execution import ExecutionError,encoded,digest
from test_custom_execution import setup,activate,package,target

@pytest.fixture
def anyio_backend():return 'asyncio'

@dataclass
class Lease:
 id:str
 token:str
 scope_key:str
 socket_dir:str

class Gateway:
 def __init__(self):self.grants={};self.revoked=[];self.closed=False;self.status=200;self.observed=True;self.events=[]
 async def issue(self,snapshot,tool,**kwargs):
  from types import SimpleNamespace
  grant=SimpleNamespace(id=secrets.token_hex(16),token=secrets.token_hex(32));self.grants[grant.id]=(grant,snapshot,tool,kwargs)
  await asyncio.to_thread(kwargs['validate'],snapshot);await asyncio.to_thread(kwargs['credentials'],snapshot)
  assert kwargs['current'](snapshot);return grant
 async def revoke(self,identifier):
  self.events.append('revoke');self.revoked.append(identifier);grant,snapshot,tool,callbacks=self.grants.pop(identifier)
  if not self.observed:return ()
  definition=next(t for t in snapshot.metadata['manifest']['tools']if t['name']==tool)
  return tuple({'operation':operation,'snapshot_sha256':snapshot.sha256,'status':self.status,'receipt_sha256':digest([operation,self.status])}for operation in definition['operations'])
 async def close(self):self.closed=True

class Listener:
 def __init__(self,gateway,directory):self.gateway=gateway;self.directory=directory
 async def start(self):return self
 async def close(self):self.gateway.events.append('socket_close')

class Broker:
 def __init__(self,manager,gateway):
  self.manager=manager;self.gateway=gateway;self.root=tempfile.TemporaryDirectory(prefix='cc-');self.jobs={};self.count=0
  self.entered=asyncio.Event();self.release_prepare=asyncio.Event();self.release_prepare.set()
  self.running=asyncio.Event();self.finish=asyncio.Event();self.finish.set();self.stop_entered=asyncio.Event();self.stop_release=asyncio.Event();self.stop_release.set()
  self.lost_prepare=False;self.uncertain=False;self.mutate=None;self.result_value={};self.corrupt=None;self.calls=[]
 async def capabilities(self,scope):return {'available':True,**self.manager.runtime.document()}
 async def prepare(self,scope,request,snapshot,tool,arguments):
  self.calls.append('prepare');self.count+=1;directory=Path(self.root.name)/request[:8];directory.mkdir(mode=0o700)
  lease=Lease(secrets.token_hex(24),secrets.token_hex(32),scope.key,str(directory))
  self.jobs[request]={'lease':lease,'scope':scope,'snapshot':snapshot,'tool':tool,'arguments':arguments,'status':'prepared'}
  self.entered.set();await self.release_prepare.wait()
  if self.lost_prepare:raise ConnectionError('synthetic untrusted secret')
  return lease
 async def recover(self,scope,request,*,retire=False):
  self.calls.append('recover');row=self.jobs.get(request)
  if row:assert row['scope']==scope
  return row['lease']if row else None
 def row(self,lease):return next(r for r in self.jobs.values()if r['lease']==lease)
 async def upload(self,scope,lease,snapshot,tool,arguments):
  self.calls.append('upload');assert self.row(lease)['snapshot']==snapshot;return {'status':'sealed'}
 async def start(self,scope,lease,gateway_token):
  self.calls.append('start');self.row(lease)['status']='running';self.running.set()
  if self.mutate:self.mutate()
  return {'status':'running'}
 async def status(self,scope,lease):
  if self.finish.is_set():self.row(lease)['status']='succeeded';return {'status':'succeeded','cleanup_confirmed':True}
  return {'status':'running','cleanup_confirmed':False}
 async def result(self,scope,lease,snapshot,tool,arguments):
  raw=encoded(self.result_value);result={'schema_version':1,'snapshot_sha256':snapshot.sha256,'tool':tool,'arguments_sha256':digest(arguments),
   'result_sha256':hashlib.sha256(raw).hexdigest(),'result_bytes':len(raw),**self.manager.runtime.document(),'broker_run_id':'custom-'+lease.id,'result':self.result_value,'cleanup_confirmed':True}
  if self.corrupt:self.corrupt(result)
  return result
 async def stop(self,scope,lease):
  self.calls.append('stop');self.gateway.events.append('stop');self.stop_entered.set();await self.stop_release.wait()
  if self.uncertain:return {'status':'uncertain','cleanup_confirmed':False}
  self.row(lease)['status']='stopped';return {'status':'stopped','cleanup_confirmed':True}
 async def release(self,scope,lease):
  self.calls.append('release');self.row(lease)['status']='released';return {'status':'released','cleanup_confirmed':True}

@pytest.fixture
def fixture(tmp_path):
 store,manager,key=setup(tmp_path/'state');gateway=Gateway();broker=Broker(manager,gateway)
 coordinator=CustomCoordinator(manager,gateway,broker,poll_seconds=.01,socket_factory=Listener)
 yield store,manager,key,gateway,broker,coordinator
 broker.root.cleanup()

async def run(coordinator,key,**kwargs):return await coordinator.test(key,owner='alice',collection='shop',expected_revision=2,cancelled=kwargs.pop('cancelled',asyncio.Event()),**kwargs)

@pytest.mark.anyio
async def test_success_requires_host_observations_schema_and_cleanup_then_active_invokes(fixture):
 store,manager,key,gateway,broker,c=fixture
 result=await run(c,key);assert result['status']=='passed'
 assert gateway.events==['revoke','socket_close','stop']and broker.calls[-1]=='release'
 saved=manager.test_result(result['id'],owner='alice',collection='shop')
 assert saved['evidence']['checks'][0]['operation_receipts']=={'inventory':digest(['inventory',200])}
 assert saved['evidence']['broker_run_id'].startswith('batch-')
 assert manager.status(key,owner='alice',collection='shop')['active_status']=='unconfigured'
 manager.activate(result['id'],owner='alice',collection='shop',expected_active_generation=0)
 assert(await c.invoke(key,'list_inventory',{},owner='alice',collection='shop',cancelled=asyncio.Event()))['result']=={}
 assert (await c.status(key,owner='alice',collection='shop'))['execution_status']=='idle'
 assert not c.active

@pytest.mark.anyio
@pytest.mark.parametrize('status',[401,403,500])
async def test_fabricated_worker_success_after_failed_http_never_passes(fixture,status):
 _,manager,key,gateway,broker,c=fixture;gateway.status=status
 with pytest.raises(CoordinatorError,match='operation_unverified'):await run(c,key)
 assert broker.calls[-1]=='release'and c.journal.pending()==[]
 with manager.store.db()as db:assert db.execute('SELECT status FROM custom_execution_tests').fetchone()[0]=='failed'

@pytest.mark.anyio
async def test_no_host_observation_even_with_valid_worker_receipt_is_unverified(fixture):
 _,_,key,gateway,broker,c=fixture;gateway.observed=False
 broker.corrupt=lambda result:result.update(receipt_sha256='a'*64,operation_receipts={'inventory':'b'*64})
 with pytest.raises(CoordinatorError,match='operation_unverified'):await run(c,key)

@pytest.mark.anyio
@pytest.mark.parametrize('field',['snapshot_sha256','tool','arguments_sha256','result_sha256','result_bytes','profile','worker_sha256','image_digest','broker_run_id','cleanup_confirmed','schema_version','result'])
async def test_result_identity_and_schema_are_host_validated(fixture,field):
 _,_,key,_,broker,c=fixture;broker.corrupt=lambda result:result.update({field:'forged'})
 with pytest.raises((CoordinatorError,ExecutionError)):await run(c,key)
 assert broker.calls[-1]=='release'

@pytest.mark.anyio
async def test_wrong_owner_dataset_unknown_tool_and_unconfigured_fail_before_prepare(fixture):
 _,manager,key,_,broker,c=fixture
 for owner,collection in [('bob','shop'),('alice','other')]:
  with pytest.raises(ValueError):await c.test(key,owner=owner,collection=collection,expected_revision=2,cancelled=asyncio.Event())
 assert broker.count==0
 with pytest.raises(ExecutionError):await c.invoke(key,'list_inventory',{},owner='alice',collection='shop',cancelled=asyncio.Event())
 activate(manager,key)
 with pytest.raises(CoordinatorError,match='input_schema'):await c.invoke(key,'other',{},owner='alice',collection='shop',cancelled=asyncio.Event())
 assert broker.count==0

@pytest.mark.anyio
async def test_accepted_prepare_lost_reply_recovers_exact_scope_and_stops(fixture):
 _,manager,key,_,broker,c=fixture;broker.lost_prepare=True
 with pytest.raises(CoordinatorError,match='failed'):await run(c,key)
 assert broker.calls==['prepare','recover','stop','release']
 assert not c.journal.pending()
 assert all(r['status']=='released'for r in broker.jobs.values())

@pytest.mark.anyio
async def test_cancelled_accepted_prepare_recovers_before_return(fixture):
 _,_,key,_,broker,c=fixture;broker.release_prepare.clear()
 task=asyncio.create_task(run(c,key));await broker.entered.wait();task.cancel();await asyncio.sleep(.02);assert not task.done();broker.release_prepare.set()
 with pytest.raises(asyncio.CancelledError):await task
 assert broker.calls==['prepare','stop','release']and not c.active

@pytest.mark.anyio
async def test_parent_stop_drains_repeated_cancel_before_admission_released(fixture):
 _,_,key,gateway,broker,c=fixture;broker.finish.clear();broker.stop_release.clear();cancel=asyncio.Event()
 task=asyncio.create_task(run(c,key,cancelled=cancel));await broker.running.wait();cancel.set();await broker.stop_entered.wait()
 for _ in range(3):task.cancel();await asyncio.sleep(.01);assert not task.done()
 assert len(c.active)==1 and gateway.events[:2]==['revoke','socket_close']
 broker.stop_release.set()
 with pytest.raises(asyncio.CancelledError):await task
 assert not c.active and broker.calls[-1]=='release'

@pytest.mark.anyio
async def test_failed_stop_persistently_blocks_invoke_and_direct_activation_until_reconcile(fixture):
 _,manager,key,gateway,broker,c=fixture
 passed=await run(c,key);manager.activate(passed['id'],owner='alice',collection='shop',expected_active_generation=0)
 broker.uncertain=True
 with pytest.raises((CoordinatorError,asyncio.CancelledError)):
  await c.invoke(key,'list_inventory',{},owner='alice',collection='shop',cancelled=asyncio.Event())
 assert(await c.status(key,owner='alice',collection='shop'))['execution_status']=='cleanup_uncertain'
 with pytest.raises(ExecutionError):manager.activate(passed['id'],owner='alice',collection='shop',expected_active_generation=1)
 with pytest.raises(ExecutionError):manager.active_snapshot(key,owner='alice',collection='shop')
 reopened=CustomCoordinator(manager,gateway,broker,poll_seconds=.01,socket_factory=Listener)
 with pytest.raises(CoordinatorError,match='cleanup_required'):await reopened.invoke(key,'list_inventory',{},owner='alice',collection='shop',cancelled=asyncio.Event())
 broker.uncertain=False;assert all(row['cleanup_confirmed']for row in await reopened.reconcile())
 assert(await reopened.invoke(key,'list_inventory',{},owner='alice',collection='shop',cancelled=asyncio.Event()))['result']=={}

@pytest.mark.anyio
async def test_failed_test_cleanup_reconciles_then_new_test_passes(fixture):
 _,manager,key,_,broker,c=fixture;broker.uncertain=True
 with pytest.raises((CoordinatorError,asyncio.CancelledError)):await run(c,key)
 with manager.store.db()as db:assert db.execute('SELECT status FROM custom_execution_tests').fetchone()[0]=='cleanup_uncertain'
 broker.uncertain=False;assert all(row['cleanup_confirmed']for row in await c.reconcile())
 assert(await run(c,key))['status']=='passed'

@pytest.mark.anyio
async def test_revoke_cancels_matching_worker_and_preserves_other_owner(fixture):
 _,manager,key,_,broker,c=fixture;broker.finish.clear();task=asyncio.create_task(run(c,key));await broker.running.wait()
 result=await c.revoke(key,owner='alice',collection='shop')
 with pytest.raises(asyncio.CancelledError):await task
 assert result['active_status']=='revoked'and result['execution_status']=='idle'
 assert broker.calls[-1]=='release'

@pytest.mark.anyio
async def test_changed_credentials_during_worker_suppress_result(fixture):
 store,_,key,_,broker,c=fixture;broker.finish.clear();broker.mutate=lambda:store.set_secret(key,'API_TOKEN','rotated',owner='alice')
 with pytest.raises(asyncio.CancelledError):await run(c,key)
 assert broker.calls[-1]=='release'

@pytest.mark.anyio
async def test_batch_deadline_stops_job_and_sanitizes_errors(fixture):
 _,manager,key,gateway,broker,_=fixture;broker.finish.clear()
 c=CustomCoordinator(manager,gateway,broker,poll_seconds=.01,socket_factory=Listener,batch_seconds=.08)
 with pytest.raises(CoordinatorError,match='^custom_execution_failed$'):await run(c,key)
 assert broker.calls[-1]=='release'and not c.active

@pytest.mark.anyio
async def test_cross_instance_connector_lock_refuses_concurrent_job(fixture):
 _,manager,key,gateway,broker,c=fixture;broker.finish.clear();first=asyncio.create_task(run(c,key));await broker.running.wait()
 other=CustomCoordinator(manager,gateway,broker,poll_seconds=.01,socket_factory=Listener)
 with pytest.raises(CoordinatorError,match='busy'):await run(other,key)
 assert broker.count==1;broker.finish.set();assert(await first)['status']=='passed'

@pytest.mark.anyio
async def test_close_stops_active_worker_and_closes_gateway(fixture):
 _,_,key,gateway,broker,c=fixture;broker.finish.clear();task=asyncio.create_task(run(c,key));await broker.running.wait();await c.close()
 with pytest.raises(asyncio.CancelledError):await task
 assert gateway.closed and broker.calls[-1]=='release'
 with pytest.raises(CoordinatorError,match='unavailable'):await run(c,key)

@pytest.mark.anyio
async def test_multiple_declared_tests_have_separate_durable_jobs_and_batch_evidence(fixture):
 store,manager,key,_,broker,c=fixture;spec=package()
 schema={'type':'object','properties':{'page':{'type':'integer'}},'required':['page'],'additionalProperties':False}
 spec['execution']['tools'][0]['input_schema']=schema
 spec['execution']['tests']=[{'tool':'list_inventory','arguments':{'page':n}}for n in(1,2)]
 store.save_package(key,spec,expected_revision=2,owner='alice')
 result=await c.test(key,owner='alice',collection='shop',expected_revision=3,cancelled=asyncio.Event())
 assert broker.count==2
 saved=manager.test_result(result['id'],owner='alice',collection='shop');jobs=c.journal.jobs(saved['evidence']['broker_run_id'])
 assert len(jobs)==2 and all(j['state']=='confirmed'and j['receipt']and j['check_json']for j in jobs)
 assert len(saved['evidence']['checks'])==2

@pytest.mark.anyio
async def test_real_uds_gateway_host_journal_tls_and_private_auth(fixture,tmp_path):
 from custom_http import CustomHTTP
 from test_custom_http import tls_server,TOKEN
 store,manager,key,_,_,_=fixture
 class UDSBroker(Broker):
  async def start(self,scope,lease,gateway_token):
   await super().start(scope,lease,gateway_token)
   async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(Path(lease.socket_dir)/'custom-http.sock')))as client:
    response=await client.post('http://localhost/request',headers={'authorization':'Bearer '+gateway_token},json={'operation':'inventory','query':{}})
    assert response.status_code==200 and response.json()['status']==200,response.text
   return {'status':'running'}
 async with tls_server(tmp_path)as(server,ssl_context,network):
  async def resolve(host):return ('93.184.216.34',)
  gateway=CustomHTTP(manager.company,resolver=resolve,network_backend=network,ssl_context=ssl_context)
  broker=UDSBroker(manager,gateway);c=CustomCoordinator(manager,gateway,broker,poll_seconds=.01)
  # Real gateway lacks fixture event log; only Broker test bookkeeping uses it.
  gateway.events=[]
  try:
   result=await run(c,key);assert result['status']=='passed'
   assert b'authorization: Bearer private-customer-token' in server['requests'][0]
   assert network.calls==[('93.184.216.34',443)]
   saved=manager.test_result(result['id'],owner='alice',collection='shop')
   assert 'private-customer-token'not in json.dumps(saved)
   assert not gateway.jobs
  finally:await c.close();broker.root.cleanup()

@pytest.mark.anyio
async def test_cancel_during_queued_prepare_drains_until_admission_is_definitive(fixture):
 _,_,key,_,broker,c=fixture;waiting=asyncio.Event();accept=asyncio.Event();original=broker.prepare;workers=[]
 async def prepare(*args):
  async def remote():waiting.set();await accept.wait();return await original(*args)
  pending=asyncio.create_task(remote());workers.append(pending)
  return await asyncio.shield(pending)
 broker.prepare=prepare
 task=asyncio.create_task(run(c,key));await waiting.wait();task.cancel();await asyncio.sleep(.03)
 try:assert not task.done(),'Caller returned before queued broker admission settled.'
 finally:
  accept.set();await asyncio.gather(*workers,return_exceptions=True)
  try:await task
  except asyncio.CancelledError:pass
 assert broker.calls[-2:]==['stop','release']
 assert all(row['status']=='released'for row in broker.jobs.values())

@pytest.mark.anyio
async def test_stop_during_test_begin_retires_accepted_lease(fixture,monkeypatch):
 _,manager,key,_,broker,c=fixture;original=c._begin;entered=threading.Event();release=threading.Event()
 def begin(*args):
  original(*args);entered.set();release.wait(3)
 monkeypatch.setattr(c,'_begin',begin)
 task=asyncio.create_task(run(c,key));assert await asyncio.to_thread(entered.wait,1)
 task.cancel();await asyncio.sleep(.02);assert not task.done();release.set()
 with pytest.raises(asyncio.CancelledError):await task
 assert not c.journal.pending()and broker.count==0
 with manager.store.db()as db:assert db.execute('SELECT status FROM custom_execution_tests').fetchone()[0]=='stopped'

@pytest.mark.anyio
async def test_frozen_socket_path_is_broker_cleanup_responsibility(tmp_path,monkeypatch):
 from types import SimpleNamespace
 gateway=Gateway();listener=GatewaySocket(gateway,tmp_path);listener.path=tmp_path/'custom-http.sock';listener.inode=123
 original=Path.lstat
 def inaccessible(path,*args,**kwargs):
  if path==listener.path:raise PermissionError('broker-owned mount')
  return original(path,*args,**kwargs)
 monkeypatch.setattr(Path,'lstat',inaccessible)
 await listener.close()

@pytest.mark.anyio
async def test_reconcile_caller_cancellation_drains_exact_cleanup(fixture):
 _,manager,key,gateway,broker,c=fixture;broker.uncertain=True
 with pytest.raises((CoordinatorError,asyncio.CancelledError)):await run(c,key)
 broker.uncertain=False;broker.stop_entered.clear();broker.stop_release.clear()
 task=asyncio.create_task(c.reconcile());await broker.stop_entered.wait();task.cancel();await asyncio.sleep(.02)
 try:assert not task.done(),'Reconciliation returned while Stop was pending.'
 finally:
  broker.stop_release.set()
  try:await task
  except asyncio.CancelledError:pass
 assert not c.journal.pending()

@pytest.mark.anyio
async def test_full_foundation_history_fails_without_irrecoverable_coordinator_fence(fixture,monkeypatch):
 _,manager,key,_,broker,c=fixture
 original=manager.admit_job
 def denied(*args,**kwargs):raise ExecutionError('Review retained execution history.')
 monkeypatch.setattr(manager,'admit_job',denied)
 with pytest.raises(ExecutionError):await run(c,key)
 assert broker.count==0
 assert not c.journal.pending()
 monkeypatch.setattr(manager,'admit_job',original)
 assert(await run(c,key))['status']=='passed'

@pytest.mark.anyio
async def test_owner_stop_from_other_instance_drains_without_revoking_target(fixture):
 _,manager,key,gateway,broker,c=fixture;broker.finish.clear()
 task=asyncio.create_task(run(c,key));await broker.running.wait()
 other=CustomCoordinator(manager,gateway,broker,poll_seconds=.01,socket_factory=Listener)
 before=manager.status(key,owner='alice',collection='shop')['target_generation']
 with pytest.raises(ValueError):await other.stop(key,owner='bob',collection='shop')
 assert not task.done()
 result=await other.stop(key,owner='alice',collection='shop')
 with pytest.raises(asyncio.CancelledError):await task
 assert result['execution_status']=='idle'and result['target_generation']==before and result['active_status']!='revoked'
 broker.finish.set();assert(await run(c,key))['status']=='passed'

@pytest.mark.anyio
async def test_stop_revoke_api_caller_cancellation_drains_worker(fixture):
 _,_,key,_,broker,c=fixture;broker.finish.clear();broker.stop_release.clear()
 running=asyncio.create_task(run(c,key));await broker.running.wait()
 stopping=asyncio.create_task(c.stop(key,owner='alice',collection='shop'));await broker.stop_entered.wait();stopping.cancel()
 await asyncio.sleep(.02);assert not stopping.done();broker.stop_release.set()
 with pytest.raises(asyncio.CancelledError):await stopping
 with pytest.raises(asyncio.CancelledError):await running
 assert not c.journal.pending()

@pytest.mark.anyio
async def test_revoke_before_admit_does_not_leave_unrecoverable_absent_job(fixture,monkeypatch):
 _,manager,key,_,broker,c=fixture;original=manager.admit_job
 def revoke_first(*args,**kwargs):
  manager.revoke(key,owner='alice',collection='shop');return original(*args,**kwargs)
 monkeypatch.setattr(manager,'admit_job',revoke_first)
 with pytest.raises((ExecutionError,asyncio.CancelledError)):await run(c,key)
 assert broker.count==0 and not c.journal.pending()
 monkeypatch.setattr(manager,'admit_job',original)
 manager.grant_target(key,target(),owner='alice',collection='shop',expected_generation=1)
 assert(await run(c,key))['status']=='passed'

@pytest.mark.anyio
async def test_stop_during_accepted_gateway_issuance_revokes_capability(fixture):
 _,_,key,gateway,broker,c=fixture;original=gateway.issue;issued=asyncio.Event();release=asyncio.Event()
 async def issue(*args,**kwargs):
  grant=await original(*args,**kwargs);issued.set();await release.wait();return grant
 gateway.issue=issue
 task=asyncio.create_task(run(c,key));await issued.wait();task.cancel();await asyncio.sleep(.02)
 assert not task.done();release.set()
 with pytest.raises(asyncio.CancelledError):await task
 assert len(gateway.revoked)==1 and not gateway.grants and broker.calls[-1]=='release'

@pytest.mark.anyio
async def test_broker_runtime_mismatch_never_prepares_worker(fixture):
 _,_,key,_,broker,c=fixture
 async def bad(scope):return {'available':True,'profile':'elsewhere'}
 broker.capabilities=bad
 with pytest.raises(CoordinatorError,match='runtime_mismatch'):await run(c,key)
 assert broker.count==0 and not c.journal.pending()

@pytest.mark.anyio
async def test_actual_uds_non2xx_constant_worker_output_cannot_pass(fixture,tmp_path):
 from custom_http import CustomHTTP
 from test_custom_http import tls_server
 _,manager,key,_,_,_=fixture
 class ConstantBroker(Broker):
  async def start(self,scope,lease,gateway_token):
   await super().start(scope,lease,gateway_token)
   async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(Path(lease.socket_dir)/'custom-http.sock')))as client:
    response=await client.post('http://localhost/request',headers={'authorization':'Bearer '+gateway_token},json={'operation':'inventory','query':{}})
    assert response.status_code==200 and response.json()['status']==401
   return {'status':'running'}
 async with tls_server(tmp_path)as(server,ssl_context,network):
  server['status']=401
  async def resolve(host):return ('93.184.216.34',)
  gateway=CustomHTTP(manager.company,resolver=resolve,network_backend=network,ssl_context=ssl_context);gateway.events=[]
  broker=ConstantBroker(manager,gateway);c=CustomCoordinator(manager,gateway,broker,poll_seconds=.01)
  try:
   with pytest.raises(CoordinatorError,match='operation_unverified'):await run(c,key)
   assert len(server['requests'])==1 and broker.calls[-1]=='release'
  finally:await c.close();broker.root.cleanup()

@pytest.mark.anyio
async def test_job_listener_preserves_app_signal_handlers(tmp_path):
 import signal
 with tempfile.TemporaryDirectory(prefix='ccsig-')as directory:
  listener=GatewaySocket(Gateway(),directory);before={sig:signal.getsignal(sig)for sig in(signal.SIGINT,signal.SIGTERM)}
  try:
   await listener.start()
   assert all(signal.getsignal(sig)==handler for sig,handler in before.items())
  finally:await listener.close()

@pytest.mark.anyio
async def test_listener_rejects_overlong_socket_before_binding(tmp_path):
 directory=tmp_path/('x'*80);directory.mkdir(mode=0o700)
 listener=GatewaySocket(Gateway(),directory)
 with pytest.raises(CoordinatorError,match='socket_path_limit'):await listener.start()
 assert not(list(directory.iterdir()))


def test_commissioned_custom_socket_root_fits_linux_limit():
 path=Path('/run/rocoto-native-broker/runs')/('custom-'+'a'*48)/'custom-http.sock'
 assert len(str(path).encode())<108
