"""Real private broker IPC, source frames, gateway UDS, TLS and receipts.

Only the container runner is a deterministic fixture; no provider/model calls.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import pytest
import httpx
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from custom_coordinator import CustomCoordinator
from custom_execution import RuntimeProfile
from custom_http import CustomHTTP
from test_custom_execution import setup
from test_custom_http import tls_server

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
async def test_actual_broker_client_source_gateway_result_and_cleanup():
 import native_executor as native
 import custom_worker
 from native_custom_broker import CustomExecutor
 from native_custom_client import CustomClient
 from test_native_custom import Backend
 from test_native_executor import broker as host_broker
 class ReadBackend(Backend):
  async def run(self,key,root,sockets):
   try:return await self.perform(key,root,sockets)
   except BaseException as error:self.fixture_failure=error;raise
  async def perform(self,key,root,sockets):
   config=json.loads((root/'input/run.json').read_bytes())
   async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(sockets/'custom-http.sock')))as client:
    response=await client.post('http://localhost/request',headers={'authorization':'Bearer '+config['gateway_token']},json={'operation':'inventory','query':{}})
    assert response.status_code==200 and response.json()['status']==200,response.text
   self.value={};await super().run(key,root,sockets)
 with tempfile.TemporaryDirectory(prefix='ccipc-')as directory:
  path=Path(directory);store,manager,key=setup(path/'company');host=host_broker(path/'broker')
  backend=ReadBackend(path/'volumes');executor=CustomExecutor(host,backend=backend)
  manager.runtime=RuntimeProfile(native.IMAGE,executor.worker_sha256)
  server=native.BrokerServer(host,path/'control.sock');await server.start()
  async with tls_server(path)as(target,context,network):
   async def resolve(host):return ('93.184.216.34',)
   gateway=CustomHTTP(manager.company,resolver=resolve,network_backend=network,ssl_context=context)
   coordinator=CustomCoordinator(manager,gateway,CustomClient(server.path),poll_seconds=.01)
   try:
    result=await coordinator.test(key,owner='alice',collection='shop',expected_revision=2,cancelled=asyncio.Event())
    assert result['status']=='passed'and len(target['requests'])==1
    manager.activate(result['id'],owner='alice',collection='shop',expected_active_generation=0)
    called=await coordinator.invoke(key,'list_inventory',{},owner='alice',collection='shop',cancelled=asyncio.Event())
    assert called['result']=={}and len(target['requests'])==2
    assert host.live_count()==0 and executor.live_count()==0 and not backend.live
    assert executor.db.execute("SELECT count(*)FROM jobs WHERE status='released'").fetchone()[0]==2
    assert not list(executor.root.glob('jobs/*'))and not list(host.run_root.glob('custom-*'))
    assert not gateway.jobs and not coordinator.journal.pending()
   except BaseException:
    if getattr(backend,'fixture_failure',None) is not None:
     raise AssertionError('The fixture worker failed.')from backend.fixture_failure
    raise
   finally:await coordinator.close();await server.close()
