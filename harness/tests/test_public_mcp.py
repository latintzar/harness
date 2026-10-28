import asyncio
import base64
from contextlib import asynccontextmanager
import copy
import json
import multiprocessing
import os
from pathlib import Path
import re
import sys
import threading
import time

import httpx
import numpy as np
import pytest
from mcp import ClientSession,types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

BASE=Path(os.environ.get('ROCOTO_HTTP_MCP_BASE',Path(__file__).parents[1]))
sys.path.insert(0,str(BASE/'tests'));sys.path.insert(0,str(BASE));sys.path.insert(0,str(Path(__file__).parents[1]))
from mcp_access import MCPGrantStore,GrantError
from public_mcp import HTTPMCP,HTTPMCPServices,READ_TOOLS
from source_visibility import ReadyIndex
from workspace_identity import workspace_scope
from test_source_derivatives import imported,prepared_import,anyio_backend
from test_imported_media import deny_decoders

URL='https://mcp.example.invalid/mcp'


class Index:
    def __init__(self,rows):
        self.clips=copy.deepcopy(rows);self.embeddings=np.ones((len(rows),2));self.report={}
    def get_clip(self,key):return next((row for row in self.clips if row['id']==key),None)
    def search(self,query,limit,dataset=None):return [dict(row) for row in self.clips if dataset is None or row['dataset']==dataset][:limit]


def legacy(key='sample',dataset='cafe'):
    return dict(id=key,video_id=key,dataset=dataset,camera=key,start=0.,end=4.,path='/private/demo.mp4')


def setup(state,rows=None,**kwargs):
    state.mkdir(exist_ok=True);raw=Index(rows or [legacy()]);view=ReadyIndex(raw,state)
    from tool_test_support import retail_services
    services=retail_services(state,view);store=MCPGrantStore(state);company=workspace_scope(state)
    grants={}
    def resolve(grant):return services if grant.company_id==company and grants.get(grant.id,True) else None
    app=HTTPMCP(store,resolve,resource_url=URL,**kwargs)
    return app,store,services,raw,grants


def issue(store,services,owner='alice',dataset='cafe'):
    return store.issue(company_id=workspace_scope(services.state),owner_principal_id=owner,dataset=dataset)


@asynccontextmanager
async def client(app,token):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={'Authorization':'Bearer '+token}) as http:
        async with streamable_http_client(URL,http_client=http) as (read,write,sid):
            async with ClientSession(read,write) as session:
                await session.initialize();yield session,sid,http


def headers(token,sid=None):
    result={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'}
    if sid:result['MCP-Session-Id']=sid
    return result

def rpc(method='tools/list',params=None):return {'jsonrpc':'2.0','id':24,'method':method,**({'params':params} if params is not None else {})}


def test_store_private_hashes_scope_restart_expiry_and_idempotent_revoke(tmp_path):
    now=[1000.];store=MCPGrantStore(tmp_path,clock=lambda:now[0]);company='a'*64
    grant,token=store.issue(company_id=company,owner_principal_id='alice',dataset='cafe',ttl_seconds=60)
    assert store.authenticate(token)==grant and token not in repr(grant)
    assert token.encode() not in store.path.read_bytes() and not {'digest','token'}&grant.public().keys()
    assert store.path.stat().st_mode&0o777==0o600
    for different in ({'company_id':'b'*64,'owner_principal_id':'alice'},{'company_id':company,'owner_principal_id':'bob'}):
        assert store.list(**different)==[]
        with pytest.raises(GrantError):store.revoke(grant.id,**different)
    reopened=MCPGrantStore(tmp_path,clock=lambda:now[0]);assert reopened.authenticate(token)==grant
    now[0]=1060;assert reopened.authenticate(token) is None
    now[0]=1050;first=reopened.revoke(grant.id,company_id=company,owner_principal_id='alice')
    now[0]=1055;assert reopened.revoke(grant.id,company_id=company,owner_principal_id='alice')==first
    assert store.authenticate(token) is None


@pytest.mark.parametrize('kwargs',[{'company_id':'../../other'},{'owner_principal_id':'../bob'},{'dataset':'../file'},
 {'ttl_seconds':float('nan')},{'ttl_seconds':True},{'ttl_seconds':31*86400}])
def test_store_rejects_invalid_host_binding(tmp_path,kwargs):
    store=MCPGrantStore(tmp_path)
    with pytest.raises(GrantError):store.issue(**(dict(company_id='a'*64,owner_principal_id='alice',dataset='cafe')|kwargs))


def test_store_active_key_limit_is_shared_across_instances(tmp_path,monkeypatch):
    a=MCPGrantStore(tmp_path);b=MCPGrantStore(tmp_path);monkeypatch.setattr(MCPGrantStore,'MAX_ACTIVE_PER_OWNER',1)
    grant,_=a.issue(company_id='a'*64,owner_principal_id='alice',dataset='cafe')
    with pytest.raises(GrantError):b.issue(company_id='a'*64,owner_principal_id='alice',dataset='other')
    b.revoke(grant.id,company_id='a'*64,owner_principal_id='alice')
    assert a.issue(company_id='a'*64,owner_principal_id='alice',dataset='other')[0].dataset=='other'


@pytest.mark.anyio
async def test_real_sdk_read_tools_fixed_scope_and_clean_reinitialize(tmp_path):
    app,store,services,raw,access=setup(tmp_path,[legacy(),legacy('outside','warehouse')]);grant,token=issue(store,services)
    async with app.lifespan():
        async with client(app,token) as (session,sid,http):
            old=sid();listing=(await session.list_tools()).tools
            assert {row.name for row in listing}==READ_TOOLS and all(row.annotations.readOnlyHint for row in listing)
            for name,args in [('list_cameras',{}),('list_monitoring_profiles',{}),('list_tracker_events',{}),('get_business_profile',{}),('get_business_context',{}),('list_camera_context',{})]:
                result=await session.call_tool(name,args)
                assert not result.isError,(name,result)
                assert '/private/' not in str(result) and 'outside' not in str(result)
            for name,args in [('cosmos_inspect',{}),('prepare_export',{}),('search_video',{'query':'x','dataset':'warehouse'}),('list_cameras',{'owner_principal_id':'bob'})]:
                assert (await session.call_tool(name,args)).isError
            # The harness describes itself to a customer's own agent: the template, the tools of this surface and each
            # state, with the lists that belong to a company's conversations closed.
            described=await session.call_tool('get_harness',{});assert not described.isError,described
            harness=json.loads(described.content[0].text);text=json.dumps(harness)
            assert harness['product']['template']['id']=='retail' and set(harness['tools']['customer_mcp'])==READ_TOOLS
            # The document a customer reads lists exactly these reads, with their count.
            document=(Path(__file__).resolve().parents[1]/'PUBLIC-MCP.md').read_text()
            boundary=' '.join(document.split('## Tool boundary')[1].split('Registry schemas are reused')[0].split())
            listed=set(re.findall(r'`([a-z_]+)`',boundary.split('The list is the read names')[0]))
            assert listed==READ_TOOLS and f'exposes the {len(READ_TOOLS)} reads listed below' in document
            assert f'only the {len(READ_TOOLS)} read tools' in document and f'The current {len(READ_TOOLS)} tools' in document
            assert {row['agents'] for row in harness['agent_classes']}=={'unknown on this surface'}
            assert {row['installed'] for row in harness['skills']}=={'unknown on this surface'}
            assert 'alice' not in text and '/private/' not in text and str(tmp_path) not in text
            assert set(json.loads((await session.call_tool('get_harness',{'section':'wakes'})).content[0].text))=={'schema','section','product','wakes','problems'}
            assert (await session.call_tool('get_harness',{'section':'everything'})).isError
            assert len(app.adapters)==1 and app.adapters[grant.company_id].services.agent.retrieval is services.agent.retrieval
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            assert (await http.post(URL,headers=headers(token,old),json=rpc())).status_code==404
        async with client(app,token) as (_,sid,_):assert sid()!=old
    assert not app.calls and not app.manager._server_instances


@pytest.mark.anyio
async def test_imported_ready_bytes_preserve_owner_and_never_start_worker(imported,monkeypatch):
    app,store,services,raw,access=setup(imported['state'],[imported['row']]);grant,token=issue(store,services,'owner','warehouse')
    deny_decoders(monkeypatch)
    other,other_token=issue(store,services,'foreign','warehouse')
    async with app.lifespan():
        async with client(app,token) as (session,_,_):
            result=await session.call_tool('list_cameras',{})
            assert not result.isError
            assert json.loads(result.content[0].text)[0]['video_id']==imported['row']['video_id']
            assert (await session.call_tool('inspect_clip',{'clip_id':imported['row']['id']})).isError
        async with client(app,other_token) as (session,_,_):
            result=await session.call_tool('list_cameras',{})
            assert not result.isError and json.loads(result.content[0].text)==[]


@pytest.mark.anyio
async def test_same_person_different_key_cannot_reuse_session(tmp_path):
    app,store,services,raw,access=setup(tmp_path,[legacy(),legacy('other','warehouse')]);grant,token=issue(store,services);_,other=issue(store,services,dataset='warehouse')
    async with app.lifespan(),client(app,token) as (session,sid,http):
        for method in ('POST','DELETE'):
            result=await http.request(method,URL,headers=headers(other,sid()),json=rpc() if method=='POST' else None)
            assert result.status_code==404
        assert not (await session.call_tool('list_cameras',{})).isError


@pytest.mark.anyio
async def test_auth_origin_duplicate_header_and_sanitized_errors(tmp_path,caplog):
    app,store,services,raw,access=setup(tmp_path);grant,token=issue(store,services)
    async with app.lifespan(),client(app,token) as (_,sid,http):
        for extra,status in [({'Authorization':'Bearer wrong'},401),({'Origin':'https://bad.invalid'},403),({'Host':'bad.invalid'},421)]:
            result=await http.post(URL,headers=headers(token,sid())|extra,json=rpc())
            assert result.status_code==status
        duplicate=list(headers(token,sid()).items())+[('Authorization','Bearer other')]
        assert (await http.post(URL,headers=duplicate,json=rpc())).status_code==401
        assert (await http.post(URL+'?company=private',headers=headers(token,sid()),json=rpc())).status_code==404
        malformed=await http.post(URL,headers=headers(token,sid()),json={'jsonrpc':'2.0','id':['private-input'],'method':'tools/list'})
        assert malformed.status_code==400 and 'private-input' not in malformed.text
        assert 'private-input' not in caplog.text
        access[grant.id]=False
        assert (await http.post(URL,headers=headers(token,sid()),json=rpc())).status_code==401
        access[grant.id]=True


@pytest.mark.anyio
async def test_refresh_visible_next_call_and_scope_error_hides_details(tmp_path):
    app,store,services,raw,access=setup(tmp_path);grant,token=issue(store,services);next_rows=[]
    def refresh():
        if next_rows:raw.clips=next_rows.pop();raw.embeddings=np.ones((len(raw.clips),2))
    raw.refresh=refresh
    async with app.lifespan(),client(app,token) as (session,_,_):
        assert len(json.loads((await session.call_tool('list_cameras',{})).content[0].text))==1
        next_rows.append([legacy(),legacy('new')])
        assert len(json.loads((await session.call_tool('list_cameras',{})).content[0].text))==2
        def fail(*args):raise OSError('private-provider-value /private/full/path')
        raw.search=fail
        result=await session.call_tool('search_video',{'query':'x'})
        assert result.isError and 'private-' not in str(result)


def revoke_elsewhere(state,grant):
    MCPGrantStore(state).revoke(grant.id,company_id=grant.company_id,owner_principal_id=grant.owner_principal_id)


@pytest.mark.anyio
async def test_cross_process_revocation_discards_result_and_holds_admission_until_drain(tmp_path,monkeypatch):
    app,store,services,raw,access=setup(tmp_path,recheck_seconds=.05,max_calls=1);grant,token=issue(store,services)
    started=threading.Event();release=threading.Event()
    def slow(*args):started.set();release.wait(2);return [legacy()]
    monkeypatch.setattr('monitoring_tools.read',lambda *args:{'events':slow()})
    async with app.lifespan(),client(app,token) as (session,sid,http):
        task=asyncio.create_task(http.post(URL,headers=headers(token,sid()),json=rpc('tools/call',{'name':'list_tracker_events','arguments':{}})))
        assert await asyncio.to_thread(started.wait,1)
        process=multiprocessing.get_context('fork').Process(target=revoke_elsewhere,args=(tmp_path,grant));process.start();await asyncio.to_thread(process.join,2);assert process.exitcode==0
        await asyncio.sleep(.12);assert app.calls and not task.done()
        release.set()
        # Before-output auth prevents the read result from crossing HTTP at all.
        response=await asyncio.wait_for(task,1)
        assert response.status_code==401 and 'sample' not in response.text
        assert not app.calls


@pytest.mark.anyio
async def test_standard_cancel_and_deadline_drain_threads_and_retain_admission(tmp_path,monkeypatch):
    app,store,services,raw,access=setup(tmp_path,call_timeout=.06,max_calls=1);grant,token=issue(store,services)
    started=threading.Event();release=threading.Event();request_id=[]
    def slow(*args):
        request_id.append(app.server.request_context.request_id)
        started.set();release.wait(2);return [legacy()]
    monkeypatch.setattr('monitoring_tools.read',lambda *args:{'events':slow()})
    async with app.lifespan(),client(app,token) as (session,sid,http):
        pending=asyncio.create_task(session.call_tool('list_tracker_events',{}))
        assert await asyncio.to_thread(started.wait,1)
        assert (await session.call_tool('list_cameras',{})).isError
        await session.send_notification(types.ClientNotification(types.CancelledNotification(
            method='notifications/cancelled',params=types.CancelledNotificationParams(requestId=request_id[0]))))
        with pytest.raises(McpError):await asyncio.wait_for(pending,1)
        assert app.calls
        release.set()
        for _ in range(100):
            if not app.calls:break
            await asyncio.sleep(.005)
        assert not app.calls
        started.clear();release.clear()
        pending=asyncio.create_task(session.call_tool('list_tracker_events',{}))
        assert await asyncio.to_thread(started.wait,1)
        await asyncio.sleep(.12)
        assert app.calls and not pending.done()
        release.set();assert (await pending).isError
        assert not app.calls


@pytest.mark.anyio
async def test_current_membership_revocation_suppresses_running_read(tmp_path,monkeypatch):
    app,store,services,raw,access=setup(tmp_path,recheck_seconds=.05);grant,token=issue(store,services)
    started=threading.Event();release=threading.Event()
    def slow(*args):started.set();release.wait(2);return [legacy()]
    monkeypatch.setattr('monitoring_tools.read',lambda *args:{'events':slow()})
    async with app.lifespan(),client(app,token) as (session,sid,http):
        pending=asyncio.create_task(http.post(URL,headers=headers(token,sid()),json=rpc('tools/call',{'name':'list_tracker_events','arguments':{}})))
        assert await asyncio.to_thread(started.wait,1)
        access[grant.id]=False;await asyncio.sleep(.08);release.set()
        response=await pending;assert response.status_code==401 and 'sample' not in response.text
        assert not app.calls
        access[grant.id]=True


@pytest.mark.anyio
async def test_wrong_host_company_service_mapping_is_denied_before_tools(tmp_path):
    app,store,services,raw,access=setup(tmp_path);grant,token=issue(store,services)
    other=tmp_path/'other';other.mkdir();wrong=HTTPMCPServices(other,ReadyIndex(Index([legacy()]),other))
    app.resolve_scope=lambda _:wrong
    async with app.lifespan(),httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response=await http.post(URL,headers=headers(token),json=rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}}))
        assert response.status_code==401 and not app.adapters


@pytest.mark.anyio
async def test_standard_client_over_actual_loopback_http_socket(tmp_path):
    import socket
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route
    app,store,services,raw,access=setup(tmp_path);grant,token=issue(store,services)
    @asynccontextmanager
    async def lifespan(_):
        async with app.lifespan():yield
    host=Starlette(routes=[Route('/mcp',app,methods=['GET','POST','DELETE'])],lifespan=lifespan)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(host,log_level='critical',access_log=False))
    runner=asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:break
            await asyncio.sleep(.01)
        assert server.started
        async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token,'Host':'mcp.example.invalid'}) as http:
            async with streamable_http_client(f'http://127.0.0.1:{port}/mcp',http_client=http) as (read,write,sid):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    assert sid() and {tool.name for tool in (await session.list_tools()).tools}==READ_TOOLS
                    result=await session.call_tool('list_cameras',{})
                    assert json.loads(result.content[0].text)[0]['video_id']=='sample'
                    assert '/private/' not in str(result)
    finally:
        server.should_exit=True;await asyncio.wait_for(runner,5);listener.close()
    assert not app.calls and not app.manager._server_instances


@pytest.mark.anyio
async def test_key_expiry_and_idle_session_limits_are_enforced(tmp_path):
    app,store,services,raw,access=setup(tmp_path,idle_timeout=.05,max_sessions=1);grant,token=issue(store,services)
    async with app.lifespan(),client(app,token) as (session,sid,http):
        init=rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}})
        assert (await http.post(URL,headers=headers(token),json=init)).status_code==503
        await asyncio.sleep(.12)
        assert (await http.post(URL,headers=headers(token,sid()),json=rpc())).status_code==404
        store.clock=lambda:grant.expires_at
        assert (await http.post(URL,headers=headers(token),json=init)).status_code==401


@pytest.mark.parametrize('kwargs',[{'max_sessions':None},{'max_sessions':10000},{'idle_timeout':None},
    {'idle_timeout':float('inf')},{'call_timeout':float('nan')},{'max_calls_per_key':100}])
def test_lifetime_configuration_stays_bounded(tmp_path,kwargs):
    with pytest.raises(ValueError):setup(tmp_path,**kwargs)

@pytest.mark.anyio
async def test_authorization_admission_retained_after_cancellation_and_deadline(tmp_path):
    app,store,services,_,_=setup(tmp_path/'company')
    _,token=issue(store,services)
    entered=threading.Event();release=threading.Event();calls=[]
    def blocked(grant):
        calls.append(grant.id);entered.set();release.wait(5);return services
    app.resolve_scope=blocked;app.max_auth_checks=1;app.auth_timeout=.05
    async with app.lifespan():
        request=asyncio.create_task(app.verify_token(token))
        assert await asyncio.to_thread(entered.wait,2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):await request
        try:
            assert await app.verify_token(token) is None
            assert len(calls)==1 and len(app.auth_checks)==1
        finally:release.set()
        for _ in range(100):
            if not app.auth_checks:break
            await asyncio.sleep(.01)
        assert not app.auth_checks
        entered.clear();release.clear()
        result=asyncio.create_task(app.verify_token(token))
        assert await asyncio.to_thread(entered.wait,2)
        assert await asyncio.wait_for(result,.5) is None
        try:
            assert len(app.auth_checks)==1
            assert await app.verify_token(token) is None
            assert len(calls)==2
        finally:release.set()

@pytest.mark.anyio
async def test_cancel_arriving_during_timeout_drain_propagates_after_actual_read(tmp_path,monkeypatch):
    app,store,services,raw,_=setup(tmp_path/'company',call_timeout=.05)
    grant,_=issue(store,services);started=threading.Event();release=threading.Event()
    async def selected():return grant,services
    app._request_scope=selected
    def slow(*args):started.set();release.wait(5);return [legacy()]
    monkeypatch.setattr('monitoring_tools.read',lambda *args:{'events':slow()})
    async with app.lifespan():
        task=asyncio.create_task(app._call('list_tracker_events',{}))
        assert await asyncio.to_thread(started.wait,2)
        await asyncio.sleep(.1);task.cancel();await asyncio.sleep(.02)
        assert app.calls and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert not app.calls


@pytest.mark.anyio
async def test_tracker_reads_are_listed_scoped_and_carry_no_write(tmp_path):
    """A customer's own agent reads the trackers, their outputs and their runs; it changes them by
    speaking to the hosted conversation. Each read follows the principal's present location access."""
    import sqlite3
    import monitoring_tools
    import tracker_runner
    from scope_host import filter_view
    from test_tracker_runner import KEYS,build,open_company
    root=tmp_path/'root';location,_=build(root);state=root/'environments'/'shop';company=open_company(root)
    assert [run['status'] for run in tracker_runner.tracker_pass(root,root,pending_since={})]==['completed']
    app,store,services,raw,grants=setup(state,[legacy('sample','retail-demo')])
    # The server gives each company's index this same filter: the view then carries the member's current scope.
    cold={'state':state,'index':services.agent.retrieval,'environment_id':'shop','business_scope':company.scope}
    from retail_scope import RetailScope
    cold['scope_policy'] = RetailScope(cold)
    services.agent.retrieval.scope_filter=lambda view,principal:filter_view(cold,view,principal)
    grant,token=issue(store,services,'peter','retail-demo')
    reads={'list_trackers','get_tracker','list_tracker_runs'};writes=monitoring_tools.TRACKERS.WRITE_NAMES
    async with app.lifespan():
        async with client(app,token) as (session,_,_):
            listing=(await session.list_tools()).tools;names={row.name for row in listing}
            assert reads<=names and names==READ_TOOLS and not writes&names and all(row.annotations.readOnlyHint for row in listing)
            answer=await session.call_tool('list_trackers',{})
            rows=json.loads(answer.content[0].text)['trackers']
            assert [row['key'] for row in rows]==KEYS and all(row['output']['revision']>=1 and row['last_run']['status']=='completed' for row in rows)
            full=await session.call_tool('get_tracker',{'tracker_id':rows[0]['id'],'rows':2})
            value=json.loads(full.content[0].text)
            assert value['output']['tile']['label']=='Visitors' and len(value['output']['canvas']['rows'])==2 and value['runs']
            runs=await session.call_tool('list_tracker_runs',{'location_id':location,'tracker_key':'visitors'})
            assert [run['trigger'] for run in json.loads(runs.content[0].text)['runs']]==['events']
            everything=answer.content[0].text+full.content[0].text+runs.content[0].text
            assert '"owner' not in everything and str(tmp_path) not in everything and 'peter' not in everything
            for name,args in [('save_tracker',{'location_id':location,'key':'spills','label':'Spills','observes':'Liquid.'}),
                ('remove_tracker',{'location_id':location,'key':'spills'}),('run_trackers',{'location_id':location}),
                ('set_tracker_schedule',{'location_id':location,'automatic':False,'expected_revision':1}),
                ('install_core_trackers',{'location_id':location,'business_type':'retail'}),
                ('list_trackers',{'owner_principal_id':'someone'}),('get_tracker',{'tracker_id':'tracker-elsewhere'})]:
                assert (await session.call_tool(name,args)).isError,name
            assert tracker_runner.TrackerRuns(state).state('peter','retail-demo',location)['requested_at'] is None
            # The member keeps the company and loses the location: the same grant now reads an empty list.
            with sqlite3.connect(root/'access.sqlite') as db:
                db.execute("UPDATE principal_environments SET role='operator' WHERE principal_id='peter' AND environment='shop'")
            assert json.loads((await session.call_tool('list_trackers',{})).content[0].text)=={'trackers':[]}
            assert (await session.call_tool('list_tracker_runs',{'location_id':location})).isError
            assert (await session.call_tool('get_tracker',{'tracker_id':rows[0]['id']})).isError
    assert not app.calls and not app.manager._server_instances
