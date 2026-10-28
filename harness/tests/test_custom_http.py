import asyncio
import base64
import json
import ssl
import threading
from datetime import datetime,timedelta,timezone
from pathlib import Path
from contextlib import asynccontextmanager
import sys
import pytest
import httpx
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpcore import AnyIOBackend
sys.path.insert(0,str(Path(__file__).parents[1]))
from custom_http import CustomHTTP,HTTPGatewayError,validated_addresses,MAX_RESPONSE_BYTES
from custom_execution import CustomExecution,RuntimeProfile,IMAGE
from custom_connectors import CustomConnectors
from test_custom_execution import package,target

@pytest.fixture
def anyio_backend():return 'asyncio'

TOKEN='synthetic-private-customer-token'

class Network:
 def __init__(self,port):self.port=port;self.calls=[];self.base=AnyIOBackend()
 async def connect_tcp(self,host,port,**kwargs):
  self.calls.append((host,port));assert host=='93.184.216.34'and port==443
  return await self.base.connect_tcp('127.0.0.1',self.port,**kwargs)

@asynccontextmanager
async def tls_server(tmp_path,*,certificate_host='inventory.example.com'):
 key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,certificate_host)])
 cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
  .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(days=1))
  .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName(certificate_host)]),critical=False).sign(key,hashes.SHA256()))
 cert_path=tmp_path/'cert.pem';key_path=tmp_path/'key.pem'
 cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM));key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));key_path.chmod(0o600)
 server_ssl=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_ssl.load_cert_chain(cert_path,key_path)
 state={'requests':[],'sni':[],'body':b'{"items":["verified"]}','status':200,'headers':[], 'entered':asyncio.Event(),'release':asyncio.Event(),'tasks':set()};state['release'].set()
 server_ssl.set_servername_callback(lambda sock,name,ctx:state['sni'].append(name))
 async def handler(reader,writer):
  state['tasks'].add(asyncio.current_task())
  try:
   head=await reader.readuntil(b'\r\n\r\n');state['requests'].append(head);state['entered'].set();await state['release'].wait()
   body=state['body'];headers=[('Content-Type','application/json'),('Content-Length',str(len(body))),('Connection','close'),*state['headers']]
   wire=f"HTTP/1.1 {state['status']} Test\r\n"+''.join(f'{k}: {v}\r\n'for k,v in headers)+'\r\n'
   writer.write(wire.encode()+ (b''if head.startswith(b'HEAD ')else body));await writer.drain()
  except (asyncio.CancelledError,ConnectionError,asyncio.IncompleteReadError):pass
  finally:
   writer.close()
   try:await writer.wait_closed()
   except (ConnectionError,ssl.SSLError):pass
   state['tasks'].discard(asyncio.current_task())
 server=await asyncio.start_server(handler,'127.0.0.1',0,ssl=server_ssl)
 client_ssl=ssl.create_default_context(cafile=str(cert_path));network=Network(server.sockets[0].getsockname()[1])
 try:yield state,client_ssl,network
 finally:
  state['release'].set();server.close();await server.wait_closed()
  for task in tuple(state['tasks']):task.cancel()
  await asyncio.gather(*tuple(state['tasks']),return_exceptions=True)

async def setup(tmp_path,*,auth='bearer',method='GET',query_schema=None,**gateway_kwargs):
 store=CustomConnectors(tmp_path/'state');record=store.create(name='Inventory',description='Read inventory.',kind='api',collection='shop',owner='alice')
 spec=package()
 if auth=='basic':spec['secret_fields']=[{'name':'USERNAME','label':'User','description':'Account user.'},{'name':'PASSWORD','label':'Password','description':'Read-only account.'}]
 if auth=='none':spec['secret_fields']=[]
 store.save_package(record['id'],spec,expected_revision=1,owner='alice')
 for f in spec['secret_fields']:store.set_secret(record['id'],f['name'],TOKEN if f['name']!='USERNAME'else'fixture-user',owner='alice')
 manager=CustomExecution(store,runtime=RuntimeProfile(IMAGE,'a'*64));grant=target();grant['operations'][0]['method']=method
 if query_schema:grant['operations'][0]['query_schema']=query_schema
 grant['authentication']={'kind':'none'}if auth=='none'else {'kind':'header','name':'X-Api-Key','credential':'API_TOKEN'}if auth=='header'else {'kind':'basic','username_field':'USERNAME','password_field':'PASSWORD'}if auth=='basic'else grant['authentication']
 manager.grant_target(record['id'],grant,owner='alice',collection='shop',expected_generation=0)
 snapshot=manager.snapshot(record['id'],owner='alice',collection='shop',expected_revision=2)
 current=[True];dns=[];cancel=asyncio.Event()
 async def resolver(host):dns.append(host);return ('93.184.216.34',)
 gateway=CustomHTTP(manager.company,resolver=resolver,**gateway_kwargs)
 binding=await gateway.issue(snapshot,'list_inventory',validate=lambda s:manager.validate_snapshot(s,owner='alice',collection='shop'),
  credentials=lambda s:manager.host_credentials(s,owner='alice',collection='shop'),current=lambda s:current[0]and manager.authority_current(s,owner='alice',collection='shop'),cancelled=cancel)
 return gateway,binding,manager,store,snapshot,current,dns,cancel

@pytest.mark.anyio
@pytest.mark.parametrize('auth',['bearer','header','basic','none'])
async def test_real_tls_pins_address_preserves_sni_host_and_private_auth(tmp_path,auth):
 async with tls_server(tmp_path)as(server,context,network):
  gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path,auth=auth,ssl_context=context,network_backend=network)
  result=await gateway.request(grant.token,{'operation':'inventory','query':{}})
  assert result.body==server['body']and result.status==200
  assert network.calls==[('93.184.216.34',443)]and dns==['inventory.example.com']and server['sni']==['inventory.example.com']
  head=server['requests'][0];assert head.startswith(b'GET /v1/inventory HTTP/1.1\r\n')and b'host: inventory.example.com\r\n'in head
  assert (b'authorization: Bearer '+TOKEN.encode()in head)==(auth=='bearer')
  assert (b'x-api-key: '+TOKEN.encode()in head)==(auth=='header')
  assert(b'authorization: Basic '+base64.b64encode(('fixture-user:'+TOKEN).encode())in head)==(auth=='basic')
  assert TOKEN not in repr(grant)+repr(result)+json.dumps(result.document())+snapshot.document.decode()
  assert result.receipt['snapshot_sha256']==snapshot.sha256
  await gateway.close()

@pytest.mark.parametrize('addresses',[['127.0.0.1'],['10.0.0.1'],['169.254.169.254'],['::1'],['fe80::1'],['fc00::1'],['::ffff:127.0.0.1'],['224.0.0.1'],['ff02::1'],['93.184.216.34','10.0.0.1'],['fe80::1%eth0'],[],['not-an-ip']])
def test_public_dns_policy_rejects_private_special_mixed_and_invalid(addresses):
 with pytest.raises(HTTPGatewayError):validated_addresses(addresses)

@pytest.mark.anyio
async def test_tls_certificate_hostname_remains_original_and_proxy_env_unused(tmp_path,monkeypatch):
 monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1');monkeypatch.setenv('ALL_PROXY','http://127.0.0.1:1')
 async with tls_server(tmp_path,certificate_host='other.example.com')as(server,context,network):
  gateway,grant,*_=await setup(tmp_path,ssl_context=context,network_backend=network)
  with pytest.raises(HTTPGatewayError,match='request_failed'):await gateway.request(grant.token,{'operation':'inventory','query':{}})
  assert not server['requests']and network.calls==[('93.184.216.34',443)]and server['sni']==['inventory.example.com']
  await gateway.close()

@pytest.mark.anyio
@pytest.mark.parametrize('document',[{'operation':'other','query':{}},{'operation':'inventory','query':{},'url':'https://other.example.com'},{'operation':'inventory','query':{},'method':'POST'},{'operation':'inventory','query':{'Host':'evil'}},{'operation':'inventory','query':{},'headers':{'Authorization':TOKEN}}])
async def test_invalid_operation_or_request_never_calls_dns_or_network(tmp_path,document):
 gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path)
 with pytest.raises(ValueError):await gateway.request(grant.token,document)
 assert dns==[]and gateway.work==set();await gateway.close()

@pytest.mark.anyio
@pytest.mark.parametrize('mode',['current','credential','target','runtime','cancel','expired'])
async def test_revoked_scope_produces_zero_network(tmp_path,mode):
 gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path)
 if mode=='current':current[0]=False
 elif mode=='credential':store.set_secret(snapshot.metadata['connector_id'],'API_TOKEN','replacement-token',owner='alice')
 elif mode=='target':manager.revoke(snapshot.metadata['connector_id'],owner='alice',collection='shop')
 elif mode=='runtime':manager.runtime=RuntimeProfile(IMAGE,'b'*64)
 elif mode=='cancel':cancel.set()
 else:gateway.clock=lambda:grant.expires_at+1
 with pytest.raises(ValueError):await gateway.request(grant.token,{'operation':'inventory','query':{}})
 assert dns==[];await gateway.close()

@pytest.mark.anyio
@pytest.mark.parametrize('mode',['redirect','encoding','limit','echo'])
async def test_bounded_responses_redirects_and_credential_echo_are_private(tmp_path,mode):
 async with tls_server(tmp_path)as(server,context,network):
  gateway,grant,*_=await setup(tmp_path,ssl_context=context,network_backend=network)
  if mode=='redirect':server['status']=307;server['headers']=[('Location','https://inventory.example.com/other')]
  elif mode=='encoding':server['headers']=[('Content-Encoding','gzip')]
  elif mode=='limit':server['body']=b'x'*(MAX_RESPONSE_BYTES+1)
  else:server['body']=TOKEN.encode()
  with pytest.raises(HTTPGatewayError)as error:await gateway.request(grant.token,{'operation':'inventory','query':{}})
  assert TOKEN not in str(error.value)and len(network.calls)==1 and len(server['requests'])==1
  await gateway.close()

@pytest.mark.anyio
async def test_stop_cancels_pending_response_and_releases_admission(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  server['release'].clear();gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path,ssl_context=context,network_backend=network)
  task=asyncio.create_task(gateway.request(grant.token,{'operation':'inventory','query':{}}));await server['entered'].wait()
  cancel.set()
  with pytest.raises((HTTPGatewayError,asyncio.CancelledError)):await asyncio.wait_for(task,2)
  assert not gateway.work;await gateway.close()

@pytest.mark.anyio
async def test_repeated_cancellation_retains_bounded_worker_admission(tmp_path):
 gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path,max_calls=1)
 entered=threading.Event();release=threading.Event()
 def blocked(snapshot):entered.set();assert release.wait(5);return True
 job=gateway._job(grant.token);job.validate=blocked
 task=asyncio.create_task(gateway.request(grant.token,{'operation':'inventory','query':{}}));assert await asyncio.to_thread(entered.wait,2)
 task.cancel();await asyncio.sleep(.01);task.cancel();await asyncio.sleep(.01)
 assert not task.done()and gateway.work
 with pytest.raises(HTTPGatewayError,match='capacity'):await gateway.request(grant.token,{'operation':'inventory','query':{}})
 release.set()
 with pytest.raises(asyncio.CancelledError):await task
 assert not gateway.work and dns==[];await gateway.close()

@pytest.mark.anyio
async def test_asgi_capability_route_forwards_only_scoped_data(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  gateway,grant,*_=await setup(tmp_path,ssl_context=context,network_backend=network)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway),base_url='http://fixture')as client:
   assert(await client.post('/request',json={'operation':'inventory','query':{}})).status_code==401
   response=await client.post('/request',json={'operation':'inventory','query':{}},headers={'Authorization':'Bearer '+grant.token})
   assert response.status_code==200 and response.headers['cache-control']=='no-store'
   assert base64.b64decode(response.json()['body_base64'])==server['body']and TOKEN not in response.text
  await gateway.close()

@pytest.mark.anyio
async def test_generation_change_during_connect_sends_zero_authenticated_requests(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  entered=asyncio.Event();release=asyncio.Event();connect=network.connect_tcp
  async def held(*args,**kwargs):
   stream=await connect(*args,**kwargs);entered.set();await release.wait();return stream
  network.connect_tcp=held
  gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path,ssl_context=context,network_backend=network)
  task=asyncio.create_task(gateway.request(grant.token,{'operation':'inventory','query':{}}));await entered.wait()
  current[0]=False;release.set()
  with pytest.raises(HTTPGatewayError):await task
  assert server['requests']==[]
  await gateway.close()

@pytest.mark.anyio
async def test_credential_change_before_output_suppresses_entire_response(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  server['release'].clear();gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path,ssl_context=context,network_backend=network)
  task=asyncio.create_task(gateway.request(grant.token,{'operation':'inventory','query':{}}));await server['entered'].wait()
  store.set_secret(snapshot.metadata['connector_id'],'API_TOKEN','changed-private-token',owner='alice');server['release'].set()
  with pytest.raises(HTTPGatewayError):await task
  assert len(server['requests'])==1;await gateway.close()

@pytest.mark.anyio
async def test_dns_is_resolved_once_and_pinned_then_rechecked_for_next_operation(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  gateway,grant,*_=await setup(tmp_path,ssl_context=context,network_backend=network);answers=[]
  async def rebinding(host):answers.append(host);return ('93.184.216.34',)if len(answers)==1 else('127.0.0.1',)
  gateway.resolver=rebinding
  assert(await gateway.request(grant.token,{'operation':'inventory','query':{}})).status==200
  with pytest.raises(HTTPGatewayError,match='public_dns'):await gateway.request(grant.token,{'operation':'inventory','query':{}})
  assert len(answers)==2 and network.calls==[('93.184.216.34',443)]and len(server['requests'])==1
  await gateway.close()

@pytest.mark.anyio
async def test_head_and_bounded_query_encoding_stay_on_exact_operation_path(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  schema={'type':'object','properties':{'q':{'type':'string','maxLength':16384},'ids':{'type':'array','items':{'type':'integer'},'maxItems':4}},'additionalProperties':False}
  gateway,grant,*_=await setup(tmp_path,method='HEAD',query_schema=schema,ssl_context=context,network_backend=network)
  result=await gateway.request(grant.token,{'operation':'inventory','query':{'q':'../admin?x=y&host=evil','ids':[1,2]}})
  assert result.body==b''and result.receipt['method']=='HEAD'
  assert server['requests'][0].startswith(b'HEAD /v1/inventory?q=..%2Fadmin%3Fx%3Dy%26host%3Devil&ids=1&ids=2 HTTP/1.1')
  with pytest.raises(HTTPGatewayError,match='query_limit'):await gateway.request(grant.token,{'operation':'inventory','query':{'q':'x'*9000}})
  assert len(server['requests'])==1;await gateway.close()

@pytest.mark.anyio
async def test_per_job_request_cap_and_revoked_capability_are_finite(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  gateway,grant,*_=await setup(tmp_path,ssl_context=context,network_backend=network)
  for _ in range(16):assert(await gateway.request(grant.token,{'operation':'inventory','query':{}})).status==200
  with pytest.raises(HTTPGatewayError,match='request_limit'):await gateway.request(grant.token,{'operation':'inventory','query':{}})
  await gateway.revoke(grant.id)
  with pytest.raises(HTTPGatewayError,match='unauthorized'):await gateway.request(grant.token,{'operation':'inventory','query':{}})
  assert len(server['requests'])==16 and len(network.calls)==16;await gateway.close()

@pytest.mark.anyio
async def test_snapshot_or_tool_from_another_company_never_issues_capability(tmp_path):
 gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path)
 other=CustomHTTP('different-company')
 with pytest.raises(HTTPGatewayError,match='scope_changed'):
  await other.issue(snapshot,'list_inventory',validate=lambda s:True,credentials=lambda s:{},current=lambda s:True,cancelled=cancel)
 with pytest.raises(HTTPGatewayError,match='operation_denied'):
  await gateway.issue(snapshot,'undeclared',validate=lambda s:True,credentials=lambda s:{},current=lambda s:True,cancelled=cancel)
 assert dns==[] and not other.jobs;await gateway.close();await other.close()

@pytest.mark.anyio
async def test_known_capability_slow_body_admission_is_bounded(tmp_path):
 gateway,grant,*_=await setup(tmp_path,max_calls=1,per_job_calls=1)
 entered=asyncio.Event();release=asyncio.Event();sent=[]
 scope={'type':'http','path':'/request','method':'POST','headers':[(b'authorization',('Bearer '+grant.token).encode())]}
 async def slow():entered.set();await release.wait();return {'type':'http.request','body':b'{}'}
 async def send(value):sent.append(value)
 first=asyncio.create_task(gateway(scope,slow,send));await entered.wait()
 second=[]
 async def send_second(value):second.append(value)
 async def must_not_read():raise AssertionError('capacity must reject before another body read')
 await gateway(scope,must_not_read,send_second)
 assert second[0]['status']==429
 first.cancel()
 with pytest.raises(asyncio.CancelledError):await first
 assert gateway.incoming==0;await gateway.close()

@pytest.mark.anyio
async def test_json_escaped_credential_echo_is_rejected_before_worker_output(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  gateway,grant,*_=await setup(tmp_path,ssl_context=context,network_backend=network)
  server['body']=('{"echo":"'+''.join('\\u%04x'%ord(c)for c in TOKEN)+'"}').encode()
  with pytest.raises(HTTPGatewayError,match='credential_echo'):await gateway.request(grant.token,{'operation':'inventory','query':{}})
  await gateway.close()

@pytest.mark.anyio
async def test_request_deadline_and_asgi_shutdown_drain_without_late_result(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  server['release'].clear();gateway,grant,*_=await setup(tmp_path,ssl_context=context,network_backend=network,request_seconds=.08)
  with pytest.raises(HTTPGatewayError):await gateway.request(grant.token,{'operation':'inventory','query':{}})
  assert len(server['requests'])==1 and not gateway.work
  events=asyncio.Queue();events.put_nowait({'type':'lifespan.startup'});events.put_nowait({'type':'lifespan.shutdown'});sent=[]
  async def send(value):sent.append(value)
  await gateway({'type':'lifespan'},events.get,send)
  assert [e['type']for e in sent]==['lifespan.startup.complete','lifespan.shutdown.complete']and gateway.closed and not gateway.jobs

@pytest.mark.anyio
async def test_supplied_credentials_must_match_exact_snapshot_authority(tmp_path):
 from dataclasses import replace
 gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path)
 real=manager.host_credentials(snapshot,owner='alice',collection='shop')
 gateway._job(grant.token).credentials=lambda s:replace(real,owner_id='bob')
 with pytest.raises(HTTPGatewayError,match='credentials_unavailable'):await gateway.request(grant.token,{'operation':'inventory','query':{}})
 assert dns==[];await gateway.close()

@pytest.mark.anyio
async def test_host_receipts_are_independent_of_worker_output_and_retained_on_revoke(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  gateway,grant,*_=await setup(tmp_path,ssl_context=context,network_backend=network)
  result=await gateway.request(grant.token,{'operation':'inventory','query':{}})
  result.receipt['status']=999
  journal=gateway.observations(grant.id)
  assert len(journal)==1 and journal[0]['status']==200 and len(journal[0]['receipt_sha256'])==64
  journal[0]['status']=888
  server['status']=401
  second=await gateway.request(grant.token,{'operation':'inventory','query':{}})
  assert second.status==401
  receipts=await gateway.revoke(grant.id)
  assert [row['status']for row in receipts]==[200,401]
  with pytest.raises(HTTPGatewayError):gateway.observations(grant.id)
  await gateway.close()

@pytest.mark.anyio
async def test_grant_issuance_and_shutdown_retain_authorization_thread_admission(tmp_path):
 gateway,grant,manager,store,snapshot,current,dns,cancel=await setup(tmp_path,max_calls=1)
 entered=threading.Event();release=threading.Event()
 def blocked(s):entered.set();assert release.wait(5);return s
 kwargs=dict(validate=blocked,credentials=lambda s:None,current=lambda s:True,cancelled=cancel)
 first=asyncio.create_task(gateway.issue(snapshot,'list_inventory',**kwargs));assert await asyncio.to_thread(entered.wait,2)
 with pytest.raises(HTTPGatewayError,match='capacity'):await gateway.issue(snapshot,'list_inventory',**kwargs)
 shutdown=asyncio.create_task(gateway.close());await asyncio.sleep(.02)
 assert not shutdown.done()and gateway.issuance
 release.set();await shutdown
 with pytest.raises(asyncio.CancelledError):await first
 assert not gateway.issuance and gateway.closed and not gateway.jobs and dns==[]
