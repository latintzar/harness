"""Actual catalogue and private transport readiness; zero provider calls."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sys

from fastapi import FastAPI, HTTPException
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from native_executor import IMAGE
from native_install_client import BrokerPluginInstaller, InstallerClient
from native_install_readiness import NativeInstallerReadiness, PROFILE
from native_packages import NativePackageStore
from plugin_catalogue import catalogue
from plugin_routes import register_plugin_routes
from test_plugin_routes import package, snapshot, request

@pytest.fixture
def anyio_backend():return 'asyncio'

class Peer:
    def __init__(self):self.value=None;self.calls=[];self.error=None;self.wait=None;self.closed=False
    async def capabilities(self,scope):
        self.calls.append(scope)
        try:
            if self.wait:await self.wait.wait()
            if self.error:raise self.error
            return self.value
        finally:self.closed=True


def ready(tmp_path,snapshot,*,enabled=True):
    peer=Peer();store=NativePackageStore(tmp_path,catalogue_snapshot=snapshot,materializer=BrokerPluginInstaller(tmp_path))
    probe=NativeInstallerReadiness(tmp_path,enabled=enabled,client=peer)
    peer.value={'schema_version':1,'available':True,'profile':PROFILE,'image':IMAGE,
      'worker_sha256':probe.worker_sha256,'catalogue_revisions':[snapshot['revision']],'supported_plugin_ids':['notion']}
    return peer,store,probe

@pytest.mark.anyio
async def test_scope_and_current_catalogue_commissioned_subset(tmp_path):
    snap=catalogue();peer,store,probe=ready(tmp_path,snap)
    from native_install_source import source_spec
    expected=[]
    for entry in snap['plugins']:
        try:source_spec(entry,snap)
        except ValueError:continue
        expected.append(entry['id'])
    assert len(expected)==50
    peer.value['supported_plugin_ids']=expected
    result=await probe.probe(store,principal_id='alice')
    assert result=={'available':True,'installable_ids':sorted(expected)}
    assert peer.calls[0].company_id==probe.company and peer.calls[0].owner_id=='alice'
    assert peer.calls[0].root_chat_id=='plugin-readiness'
    peer.value['supported_plugin_ids']=['notion']
    assert (await probe.probe(store,principal_id='bob'))['installable_ids']==['notion']
    assert peer.calls[1].owner_id=='bob'

@pytest.mark.anyio
@pytest.mark.parametrize('field,value',[
 ('schema_version',0),('available',False),('profile','future'),('image','sha256:wrong'),
 ('worker_sha256','0'*64),('catalogue_revisions',['b'*40]),('supported_plugin_ids','notion'),
 ('supported_plugin_ids',['notion','notion']),('supported_plugin_ids',[None]),
])
async def test_mismatched_broker_fails_closed(tmp_path,snapshot,field,value):
    peer,store,probe=ready(tmp_path,snapshot);peer.value[field]=value
    assert await probe.probe(store,principal_id='alice')=={'available':False,'installable_ids':[]}

@pytest.mark.anyio
async def test_unpinned_or_non_api_ids_never_gain_support_from_broker(tmp_path,snapshot):
    peer,store,probe=ready(tmp_path,snapshot)
    peer.value['supported_plugin_ids']=['notion','external','gmail','escape','missing']
    assert await probe.probe(store,principal_id='alice')=={'available':True,'installable_ids':['notion']}

@pytest.mark.anyio
async def test_disabled_wrong_state_or_materializer_never_probes(tmp_path,snapshot):
    peer,store,probe=ready(tmp_path/'first',snapshot,enabled=False)
    assert not (await probe.probe(store,principal_id='alice'))['available'] and not peer.calls
    probe.enabled=True
    other=NativePackageStore(tmp_path/'other',catalogue_snapshot=snapshot)
    assert not (await probe.probe(other,principal_id='alice'))['available'] and not peer.calls
    store.materializer=object()
    assert not (await probe.probe(store,principal_id='alice'))['available'] and not peer.calls

@pytest.mark.anyio
async def test_offline_old_and_revoked_broker_have_no_positive_cache(tmp_path,snapshot):
    peer,store,probe=ready(tmp_path,snapshot)
    assert (await probe.probe(store,principal_id='alice'))['available']
    peer.error=OSError('/private/internal/provider-token')
    assert await probe.probe(store,principal_id='alice')=={'available':False,'installable_ids':[]}
    peer.error=None;peer.value={'error':'unknown_operation'}
    assert not (await probe.probe(store,principal_id='alice'))['available']

@pytest.mark.anyio
async def test_timeout_and_caller_cancel_close_readiness_transport(tmp_path,snapshot,monkeypatch):
    import native_install_readiness
    peer,store,probe=ready(tmp_path,snapshot);peer.wait=asyncio.Event()
    monkeypatch.setattr(native_install_readiness,'CAPABILITY_TIMEOUT',.02)
    assert not (await probe.probe(store,principal_id='alice'))['available'] and peer.closed
    peer.closed=False
    task=asyncio.create_task(probe.probe(store,principal_id='alice'));await asyncio.sleep(0);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert peer.closed

@pytest.mark.anyio
async def test_real_uds_client_closed_on_timeout_without_broker_allocation(tmp_path,snapshot,monkeypatch):
    import native_install_readiness
    peer,store,probe=ready(tmp_path,snapshot);received=[];closed=asyncio.Event()
    async def connection(reader,writer):
        try:
            received.append(json.loads(await reader.readline()))
            assert await reader.read()==b''
        finally:writer.close();await writer.wait_closed();closed.set()
    server=await asyncio.start_unix_server(connection,path=tmp_path/'control.sock')
    probe.client=InstallerClient(tmp_path/'control.sock')
    monkeypatch.setattr(native_install_readiness,'CAPABILITY_TIMEOUT',.03)
    try:
        assert not (await probe.probe(store,principal_id='alice'))['available']
        await asyncio.wait_for(closed.wait(),1)
        assert received[0]['operation']=='plugin_capabilities'
        assert set(received[0])=={'scope','operation'}
    finally:server.close();await server.wait_closed()

@pytest.mark.anyio
async def test_routes_obey_actual_ids_and_bound_principal(tmp_path,snapshot,package):
    peer,store,probe=ready(tmp_path,snapshot);calls=[]
    @asynccontextmanager
    async def acquire(entry,snap,*,principal_id):
        calls.append(principal_id);yield package,{'version':'0.1.7'}
    store.materializer.acquire_for_principal=acquire
    selected={'state':store.state};app=FastAPI()
    async def capabilities(workspace,*,principal_id,store):
        assert workspace is selected
        return await probe.probe(store,principal_id=principal_id)
    async def revoke(workspace,*,principal_id,plugin_id):return {'cleanup_confirmed':True}
    register_plugin_routes(app,store_for_workspace=lambda _:store,installation_capabilities=capabilities,revoke_plugin=revoke)
    endpoints={r.name:r.endpoint for r in app.routes if hasattr(r,'endpoint')}
    listing=json.loads((await endpoints['installed'](request(selected,method='GET',content=b''))).body)
    assert listing['available'] and listing['installation_available'] and listing['installable_ids']==['notion']
    peer.value['supported_plugin_ids']=[]
    with pytest.raises(HTTPException) as unsupported:await endpoints['install']('notion',request(selected))
    assert unsupported.value.status_code==409 and calls==[]
    peer.value['supported_plugin_ids']=['notion']
    assert (await endpoints['install']('notion',request(selected))).status_code==200 and calls==['alice']
    peer.error=OSError('broker stopped')
    listing=json.loads((await endpoints['installed'](request(selected,method='GET',content=b''))).body)
    assert listing['available'] is False and listing['installable_ids']==[] and len(listing['plugins'])==1
    assert (await endpoints['remove']('notion',request(selected,method='DELETE',content=b''))).status_code==200

@pytest.mark.anyio
async def test_scope_changed_during_readiness_cannot_install(tmp_path,snapshot,package):
    peer,store,probe=ready(tmp_path,snapshot);selected={'state':store.state};req=request(selected);calls=[]
    @asynccontextmanager
    async def acquire(*args,**kwargs):calls.append(1);yield package,{}
    store.materializer.acquire_for_principal=acquire
    async def capabilities(workspace,*,principal_id,store):
        value=await probe.probe(store,principal_id=principal_id)
        req.state.session['principal']={'id':'bob'}
        return value
    app=FastAPI();register_plugin_routes(app,store_for_workspace=lambda _:store,installation_capabilities=capabilities)
    install=next(r.endpoint for r in app.routes if r.name=='install')
    with pytest.raises(HTTPException) as error:await install('notion',req)
    assert error.value.status_code==403 and calls==[]


@pytest.mark.anyio
async def test_host_disables_readiness_while_probe_waits(tmp_path,snapshot):
    peer,store,probe=ready(tmp_path,snapshot);peer.wait=asyncio.Event()
    task=asyncio.create_task(probe.probe(store,principal_id='alice'));await asyncio.sleep(0)
    assert peer.calls
    probe.enabled=False;peer.wait.set()
    assert await task=={'available':False,'installable_ids':[]}

@pytest.mark.anyio
async def test_external_metadata_does_not_expand_previously_commissioned_ids(tmp_path):
    snap=catalogue();peer,store,probe=ready(tmp_path,snap)
    external={'crowdstrike-falcon-foundry','crowdstrike-falcon-fusion','qodo'}
    peer.value['supported_plugin_ids']=[entry['id'] for entry in snap['plugins'] if entry['supported_api'] and entry['id'] not in external]
    result=await probe.probe(store,principal_id='alice')
    assert result['available'] and len(result['installable_ids'])==47
    assert not external.intersection(result['installable_ids'])
