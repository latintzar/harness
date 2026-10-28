"""Real private package stores and authenticated HTTP; materializer is local-only."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import threading

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

HERE=Path(__file__).parents[1]
VIDEO=HERE if (HERE/'server.py').is_file() else Path(os.environ['ROCOTO_PLUGIN_TEST_BASE'])
sys.path[:0]=[str(HERE),str(VIDEO),str(VIDEO/'tests')]
import plugin_routes
from plugin_routes import register_plugin_routes, PendingRemovals
from native_packages import NativePackageStore
from test_server_access import fixture_app
from test_workflow_routes import login, HEADERS


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.fixture
def package(tmp_path):
    p=tmp_path/'original';(p/'.codex-plugin').mkdir(parents=True)
    (p/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'notion','version':'0.1.7','license':'MIT','skills':'./skills'}))
    (p/'skills').mkdir();(p/'skills/SKILL.md').write_text('Exact original skill.\n')
    (p/'LICENSE').write_text('Exact original license.\n')
    return p


@pytest.fixture
def snapshot():
    return {'revision':'a'*40,'source_url':'https://github.com/openai/plugins/tree/'+'a'*40,'plugins':[
        {'id':'notion','supported_api':True,'source_url':'https://github.com/openai/plugins/tree/'+'a'*40+'/plugins/notion',
         'catalogue_entry':{'name':'notion','source':{'source':'local','path':'./plugins/notion'}},'manifest':{}},
        {'id':'gmail','supported_api':False,'catalogue_entry':{'name':'gmail','source':{'source':'local','path':'./plugins/gmail'}}},
        {'id':'external','supported_api':True,'catalogue_entry':{'name':'external','source':{'source':'git','url':'https://github.com/publisher/plugin'}}},
        {'id':'escape','supported_api':True,'catalogue_entry':{'name':'escape','source':{'source':'local','path':'../escape'}}}]}


class Materializer:
    def __init__(self, path):
        self.path=path;self.calls=0;self.started=threading.Event();self.release=None;self.fail=False
    @asynccontextmanager
    async def acquire(self,*args):
        self.calls+=1;self.started.set()
        while self.release is not None and not self.release.is_set(): await asyncio.sleep(.005)
        if self.fail: raise ValueError('/secret/internal/path private auth content')
        yield self.path, {'version':'0.1.7'}


def setup(tmp_path,monkeypatch,package,snapshot,*,available=True,missing_revoke=False):
    import server
    monkeypatch.setattr(server,'register_plugin_routes',lambda *a,**kw:None,raising=False)
    app=fixture_app(tmp_path,monkeypatch)
    metadata=[r for r in app.router.routes if getattr(r,'path',None)=='/api/plugins/{plugin_id}']
    for r in metadata: app.router.routes.remove(r)
    stores={};fake=Materializer(package);state={'available':available,'confirmed':True,'fail':False,'calls':[]}
    def store_for(selected):
        key=str(selected['state'])
        if key not in stores: stores[key]=NativePackageStore(key,catalogue_snapshot=snapshot,materializer=fake)
        return stores[key]
    async def revoke(selected,*,principal_id,plugin_id):
        store=store_for(selected)
        assert plugin_id not in {r['id'] for r in store.list(principal_id=principal_id)['plugins']}
        assert plugin_id in PendingRemovals(store).list(principal_id)
        state['calls'].append((str(store.state),principal_id,plugin_id))
        if state['fail']: raise RuntimeError('/private/runtime/token')
        return {'cleanup_confirmed':state['confirmed']}
    register_plugin_routes(app,store_for_workspace=store_for,
        installation_ready=(lambda _:state['available']) if available is not None else None,
        revoke_plugin=None if missing_revoke else revoke)
    app.router.routes.extend(metadata)
    return app,stores,fake,state


def add(client,identifier='notion',**kwargs):
    return client.post('/api/plugins/'+identifier+'/install',json={},headers=HEADERS,**kwargs)


def remove(client,identifier='notion'):
    return client.delete('/api/plugins/'+identifier+'/installation',headers=HEADERS)


def test_real_original_install_idempotence_and_removal_fence(tmp_path,monkeypatch,package,snapshot):
    app,stores,fake,state=setup(tmp_path,monkeypatch,package,snapshot)
    with TestClient(app) as client:
        login(client,app,'alice')
        row=add(client);assert row.status_code==200,row.text
        assert row.json()['installed'] and row.json()['connected'] is False and row.json()['callable'] is False
        assert row.json()['version']=='0.1.7'
        assert add(client).json()==row.json() and fake.calls==1
        listing=client.get('/api/plugins/installed');assert listing.status_code==200,listing.text
        assert listing.json()['plugins']==[row.json()] and listing.json()['installable_ids']==['notion']
        assert listing.headers['cache-control']=='private, no-store'
        store=next(iter(stores.values()));selection=store.snapshot(principal_id='alice')
        assert (selection.packages[0].root/'skills/SKILL.md').read_bytes()==(package/'skills/SKILL.md').read_bytes()
        assert remove(client).json()=={'removed':True,'plugin_id':'notion','cleanup_confirmed':True}
        assert client.get('/api/plugins/installed').json()['plugins']==[]
        assert remove(client).status_code==200 and len(state['calls'])==2
        paths=[getattr(r,'path','') for r in app.router.routes]
        assert paths.index('/api/plugins/installed')<paths.index('/api/plugins/{plugin_id}')
        assert str(tmp_path) not in listing.text


def test_principal_company_observer_browse_boundaries(tmp_path,monkeypatch,package,snapshot):
    app,stores,fake,state=setup(tmp_path,monkeypatch,package,snapshot)
    app.state.access.create_environment('other','Other')
    with TestClient(app) as alice,TestClient(app) as bob,TestClient(app) as owner,TestClient(app) as other:
        assert alice.get('/api/plugins/installed').status_code==401
        login(alice,app,'alice');login(bob,app,'bob');login(owner,app,'owner',owner=True)
        other.post('/api/session',json={'code':app.state.access.issue('other',principal_id='alice')},headers=HEADERS)
        assert add(alice).status_code==200
        for client in [bob,other]:
            assert client.get('/api/plugins/installed').json()['plugins']==[]
            assert remove(client).status_code==200
        assert len(alice.get('/api/plugins/installed').json()['plugins'])==1
        owner.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS)
        assert len(owner.get('/api/plugins/installed').json()['plugins'])==1
        assert add(owner).status_code==403 and remove(owner).status_code==403
        alice.post('/api/session/demo',headers=HEADERS)
        assert alice.get('/api/plugins/installed').status_code==403 and add(alice).status_code==403


@pytest.mark.parametrize('available',[None,False])
def test_default_closed_admission_and_remove_independent_readiness(tmp_path,monkeypatch,package,snapshot,available):
    app,stores,fake,state=setup(tmp_path,monkeypatch,package,snapshot,available=available)
    with TestClient(app) as client:
        login(client,app,'alice')
        assert add(client).status_code==503 and fake.calls==0 and not stores
        listing=client.get('/api/plugins/installed').json();assert listing['available'] is False
        store=next(iter(stores.values()));asyncio.run(store.install('notion',principal_id='alice'))
        assert remove(client).status_code==200 and state['calls'][-1][1:] == ('alice','notion')


@pytest.mark.parametrize('mode',['unconfirmed','exception','missing'])
def test_failed_cleanup_persists_across_store_reload_blocks_reinstall_and_retries(tmp_path,monkeypatch,package,snapshot,mode):
    app,stores,fake,state=setup(tmp_path,monkeypatch,package,snapshot,missing_revoke=mode=='missing')
    with TestClient(app) as alice,TestClient(app) as bob:
        login(alice,app,'alice');login(bob,app,'bob');assert add(alice).status_code==200
        if mode=='unconfirmed': state['confirmed']=False
        if mode=='exception': state['fail']=True
        result=remove(alice);assert result.status_code==503 and result.json()['removed'] is True
        assert result.json()['cleanup_confirmed'] is False and '/private' not in result.text
        store=next(iter(stores.values()));journal=PendingRemovals(store)
        assert journal.path.stat().st_mode&0o777==0o600
        stores.clear()
        assert alice.get('/api/plugins/installed').json()['cleanup_pending_ids']==['notion']
        assert bob.get('/api/plugins/installed').json()['cleanup_pending_ids']==[]
        assert add(alice).status_code==409 and fake.calls==1
        if mode!='missing':
            state.update(confirmed=True,fail=False);assert remove(alice).status_code==200
            assert alice.get('/api/plugins/installed').json()['cleanup_pending_ids']==[]
            assert add(alice).status_code==200


@pytest.mark.parametrize('data,headers,status',[
    ('{}',{},403),('{}',{**HEADERS,'Origin':'https://other.invalid','Content-Type':'application/json'},403),
    ('{}',{**HEADERS,'Origin':'null','Content-Type':'application/json'},403),
    ('{"principal_id":"other"}',{**HEADERS,'Content-Type':'application/json'},400),
    ('{"url":"https://untrusted.invalid"}',{**HEADERS,'Content-Type':'application/json'},400),
    ('[]',{**HEADERS,'Content-Type':'application/json'},400),('',{**HEADERS,'Content-Type':'application/json'},400),
    ('{}',{**HEADERS,'Content-Type':'text/plain'},415),(' '*1025,{**HEADERS,'Content-Type':'application/json'},413),
    ('{}',{**HEADERS,'Content-Type':'application/json','Content-Encoding':'gzip'},415),
])
def test_mutation_headers_and_empty_body(tmp_path,monkeypatch,package,snapshot,data,headers,status):
    app,_,fake,_=setup(tmp_path,monkeypatch,package,snapshot)
    with TestClient(app) as client:
        login(client,app,'alice')
        r=client.post('/api/plugins/notion/install',content=data,headers=headers)
        assert r.status_code==status,r.text
        assert fake.calls==0


def test_fixed_catalogue_unpinned_and_unknown_ids(tmp_path,monkeypatch,package,snapshot):
    app,_,fake,_=setup(tmp_path,monkeypatch,package,snapshot)
    with TestClient(app) as c:
        login(c,app,'alice')
        for identity in ['gmail','external','escape','missing']:
            assert add(c,identity).status_code==409
        assert remove(c,'missing').status_code==404
        assert c.get('/api/plugins/installed?principal_id=bob').status_code==400
        assert c.post('/api/plugins/notion/install?company=other',json={},headers=HEADERS).status_code==400
        assert c.request('DELETE','/api/plugins/notion/installation',content=b'{}',headers=HEADERS).status_code==415
        assert fake.calls==0


def request(selected,principal='alice',*,method='POST',content=b'{}'):
    async def receive(): return {'type':'http.request','body':content,'more_body':False}
    return Request({'type':'http','method':method,'path':'/','query_string':b'',
        'headers':[(b'host',b'testserver'),(b'x-video-demo',b'1'),(b'content-type',b'application/json'),(b'content-length',str(len(content)).encode())],
        'state':{'workspace':selected,'session':{'authenticated':True,'can_query':True,'role':'operator','principal':{'id':principal},'environment':{'id':'demo'}}}},receive)


def direct(tmp_path,package,snapshot,*,ready=True,revoke=None):
    fake=Materializer(package);store=NativePackageStore(tmp_path,catalogue_snapshot=snapshot,materializer=fake)
    app=FastAPI();register_plugin_routes(app,store_for_workspace=lambda selected:selected['store'],installation_ready=lambda _:ready,revoke_plugin=revoke)
    endpoints={r.name:r.endpoint for r in app.routes if hasattr(r,'endpoint')}
    return endpoints,{'state':store.state,'store':store},fake


@pytest.mark.anyio
async def test_cancelled_install_drains_thread_preserves_owner_and_global_admission(tmp_path,package,snapshot,monkeypatch):
    # Use one global slot to make leaked work or early release observable.
    monkeypatch.setattr(plugin_routes,'_REQUEST_SLOTS',threading.BoundedSemaphore(1))
    routes,selected,fake=direct(tmp_path/'state',package,snapshot)
    fake.release=threading.Event();req=request(selected)
    task=asyncio.create_task(routes['install']('notion',req))
    for _ in range(200):
        if fake.started.is_set(): break
        await asyncio.sleep(.005)
    assert fake.started.is_set()
    req.state.session['principal']={'id':'bob'}
    task.cancel();await asyncio.sleep(.02);assert not task.done()
    with pytest.raises(HTTPException) as busy: await routes['installed'](request(selected,method='GET',content=b''))
    assert busy.value.status_code==429
    fake.release.set()
    with pytest.raises(asyncio.CancelledError): await task
    result=await routes['installed'](request(selected,method='GET',content=b''))
    assert json.loads(result.body)['plugins'][0]['id']=='notion'
    assert selected['store'].list(principal_id='bob')['plugins']==[]
    assert (await routes['install']('notion',request(selected))).status_code==200 and fake.calls==1


@pytest.mark.anyio
async def test_concurrent_install_and_remove_fence_cross_registration(tmp_path,package,snapshot):
    routes,selected,fake=direct(tmp_path/'state',package,snapshot)
    fake.release=threading.Event();task=asyncio.create_task(routes['install']('notion',request(selected)))
    while not fake.started.is_set(): await asyncio.sleep(.005)
    # A second app/registration sharing company state sees the same flock.
    app=FastAPI();register_plugin_routes(app,store_for_workspace=lambda _:selected['store'],installation_ready=lambda _:True)
    remove_endpoint=next(r.endpoint for r in app.routes if r.name=='remove')
    with pytest.raises(HTTPException) as busy: await remove_endpoint('notion',request(selected,method='DELETE',content=b''))
    assert busy.value.status_code==409
    fake.release.set();await task
    assert PendingRemovals(selected['store']).list('alice')==[]


@pytest.mark.anyio
async def test_stop_during_revoke_retains_durable_intent_and_releases_fence(tmp_path,package,snapshot):
    entered=asyncio.Event();gate=asyncio.Event()
    async def revoke(*args,**kwargs): entered.set();await gate.wait();return {'cleanup_confirmed':True}
    routes,selected,fake=direct(tmp_path/'state',package,snapshot,revoke=revoke)
    await routes['install']('notion',request(selected))
    task=asyncio.create_task(routes['remove']('notion',request(selected,method='DELETE',content=b'')))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert selected['store'].list(principal_id='alice')['plugins']==[]
    assert PendingRemovals(selected['store']).list('alice')==['notion']
    with pytest.raises(HTTPException) as blocked: await routes['install']('notion',request(selected))
    assert blocked.value.detail=='plugin_removal_pending'
    gate.set();assert (await routes['remove']('notion',request(selected,method='DELETE',content=b''))).status_code==200


@pytest.mark.anyio
async def test_slow_request_timeout_never_starts_installer(tmp_path,package,snapshot,monkeypatch):
    monkeypatch.setattr(plugin_routes,'BODY_TIMEOUT',.01)
    routes,selected,fake=direct(tmp_path/'state',package,snapshot)
    r=request(selected)
    async def slow(): await asyncio.sleep(60)
    r._receive=slow
    with pytest.raises(HTTPException) as err: await routes['install']('notion',r)
    assert err.value.status_code==408 and fake.calls==0


def test_pending_removals_are_bounded_and_owner_scoped(tmp_path,package,snapshot):
    _,selected,_=direct(tmp_path/'state',package,snapshot);journal=PendingRemovals(selected['store'])
    for i in range(64): journal.begin('alice',f'p{i}')
    journal.begin('alice','p0')
    with pytest.raises(HTTPException) as capacity:journal.begin('alice','extra')
    assert capacity.value.status_code==429
    journal.begin('bob','p0');journal.clear('alice','p0')
    assert journal.list('bob')==['p0'] and len(journal.list('alice'))==63


def test_materializer_failure_exposes_only_bounded_public_error(tmp_path,monkeypatch,package,snapshot):
    app,_,fake,_=setup(tmp_path,monkeypatch,package,snapshot);fake.fail=True
    with TestClient(app) as client:
        login(client,app,'alice');response=add(client)
        assert response.status_code==503
        assert response.json()=={'detail':'plugin_installation_failed'}
        assert '/secret' not in response.text and 'private auth' not in response.text
        assert client.get('/api/plugins/installed').json()['plugins']==[]


@pytest.mark.anyio
async def test_adapter_itself_rejects_origin_csrf_and_identity_inputs(tmp_path,package,snapshot):
    routes,selected,fake=direct(tmp_path/'state',package,snapshot)
    for mode in ['csrf','origin','observer','foreign_observer','browse','query']:
        req=request(selected)
        if mode=='csrf': req.scope['headers']=[pair for pair in req.scope['headers'] if pair[0]!=b'x-video-demo']
        if mode=='origin': req.scope['headers'].append((b'origin',b'https://other.invalid'))
        if mode in ['observer','foreign_observer']:
            req.state.session.update(role='owner',can_query=False,viewing_as={'environment':{'id':'demo' if mode=='observer' else 'other'},'principal':{'id':'bob'}})
        if mode=='browse': req.state.session.update(role='browse',can_query=False)
        if mode=='query': req.scope['query_string']=b'principal_id=bob'
        with pytest.raises(HTTPException) as denied: await routes['install']('notion',req)
        assert denied.value.status_code==(400 if mode=='query' else 403)
    assert fake.calls==0


@pytest.mark.anyio
async def test_host_returning_different_company_store_is_closed(tmp_path,package,snapshot):
    routes,selected,fake=direct(tmp_path/'state',package,snapshot)
    selected['state']=tmp_path/'other'
    with pytest.raises(HTTPException) as denied: await routes['install']('notion',request(selected))
    assert denied.value.status_code==503 and fake.calls==0


@pytest.mark.anyio
async def test_runtime_cleanup_timeout_keeps_intent_and_selection_removed(tmp_path,package,snapshot,monkeypatch):
    monkeypatch.setattr(plugin_routes,'REVOKE_TIMEOUT',.01)
    async def revoke(*a,**kw):await asyncio.sleep(60)
    routes,selected,_=direct(tmp_path/'state',package,snapshot,revoke=revoke)
    await routes['install']('notion',request(selected))
    response=await routes['remove']('notion',request(selected,method='DELETE',content=b''))
    assert response.status_code==503 and json.loads(response.body)['removed'] is True
    assert PendingRemovals(selected['store']).list('alice')==['notion']
    assert selected['store'].list(principal_id='alice')['plugins']==[]
