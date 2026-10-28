"""Managed provider sessions inherit only the current host-admitted read lease."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from host import Admission
from managed_mcp import ManagedMCPHost
from public_mcp import HTTPMCPServices
from workspace_identity import workspace_scope

URL='https://managed.example.invalid/runtime/mcp'

@pytest.fixture
def anyio_backend():return 'asyncio'


def setup(tmp_path):
    state=tmp_path/'company';state.mkdir()
    agent=SimpleNamespace(state=state,children={'root':{'id':'root','owner_principal_id':'alice','dataset':'company'}},active={})
    from tool_families import ToolRegistry
    spec={'name':'get_business_profile','description':'Read the workspace profile.',
          'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}
    def read(context,name,arguments):
        return {'actor':context.actor,'data_owner':context.data_owner,'chat_id':context.chat_id}
    family=SimpleNamespace(READ_NAMES=frozenset({'get_business_profile'}),WRITE_NAMES=frozenset(),
        specs=lambda write:[spec],read=read)
    agent.tool_registry=ToolRegistry((family,));agent.vertical=SimpleNamespace()
    host=ManagedMCPHost(tmp_path/'host',public_origin='https://managed.example.invalid')
    lease=host.bind_session('binding-one',company_id=workspace_scope(state),data_owner='alice',
        root_chat='root',collection='company',services=HTTPMCPServices(state,agent))
    return host,lease,agent,family


def begin(lease,agent,*,allowed=None,quiesced=True,actor='alice'):
    allowed=allowed if allowed is not None else [True]
    run={'cancel':asyncio.Event(),'generation':'one'};agent.active['root']=run
    generation=lease.begin(actor=actor,admission=Admission(lambda:allowed[0]),cancel=run['cancel'],
        deadline=time.monotonic()+60,current_run=lambda:agent.active.get('root') is run,
        quiesced=quiesced,run=run,request_id=uuid.uuid4().hex)
    return generation,run,allowed


def token(lease):return lease.tools[0]['transport']['authorization'].removeprefix('Bearer ')


def test_session_token_is_stable_private_and_inactive_after_restart(tmp_path):
    host,lease,agent,_=setup(tmp_path);secret=token(lease)
    assert host.authenticate(secret) is None
    generation,_,_=begin(lease,agent)
    grant=host.authenticate(secret)
    assert grant and grant.id==generation.id and secret not in repr(grant)
    lease.created('sess_provider_one')
    for path in host.root.iterdir():
        assert path.stat().st_mode&0o077==0
        if path.is_file():assert secret.encode() not in path.read_bytes()
    restarted=ManagedMCPHost(tmp_path/'host',public_origin='https://managed.example.invalid')
    assert restarted.authenticate(secret) is None and not generation.current()
    recovered=restarted.bind_session('binding-one',company_id=workspace_scope(agent.state),data_owner='alice',
        root_chat='root',collection='company',services=HTTPMCPServices(agent.state,agent))
    assert token(recovered)==secret and recovered.session_id=='sess_provider_one'
    with pytest.raises(ValueError):begin(recovered,agent,quiesced=False)
    fresh,_,_=begin(recovered,agent)
    assert fresh.current() and restarted.active(grant.id) is None


@pytest.mark.parametrize('field,value',[('company_id','b'*64),('data_owner','bob'),
    ('root_chat','other'),('collection','other')])
def test_session_binding_preserves_exact_identity(tmp_path,field,value):
    host,lease,agent,_=setup(tmp_path)
    identity=dict(company_id=workspace_scope(agent.state),data_owner='alice',root_chat='root',collection='company')
    with pytest.raises(ValueError):
        host.bind_session('binding-one',**(identity|{field:value}),services=HTTPMCPServices(agent.state,agent))
    lease.created('sess_original')
    with pytest.raises(ValueError):lease.created('sess_someone_else')


@pytest.mark.parametrize('change',['member','cancel','run','owner','collection'])
def test_current_turn_checks_remain_live(tmp_path,change):
    host,lease,agent,_=setup(tmp_path);generation,run,allowed=begin(lease,agent)
    if change=='member':allowed[0]=False
    elif change=='cancel':run['cancel'].set()
    elif change=='run':agent.active['root']={}
    elif change=='owner':agent.children['root']['owner_principal_id']='bob'
    elif change=='collection':agent.children['root']['dataset']='other'
    assert not generation.current() and host.authenticate(token(lease)) is None
    with pytest.raises(ValueError):generation.services.admit(generation.grant)


def test_expiry_and_old_generation_cannot_stop_or_admit_successor(tmp_path):
    host,lease,agent,_=setup(tmp_path);old,_,_=begin(lease,agent)
    lease.end(old);fresh,_,_=begin(lease,agent)
    lease.end(old)
    assert fresh.current() and host.active(old.id) is None
    with pytest.raises(ValueError):old.services.admit(old.grant)
    host.monotonic=lambda:fresh.deadline
    assert not fresh.current() and host.authenticate(token(lease)) is None


@asynccontextmanager
async def client(host,secret):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.transport),
            headers={'Authorization':'Bearer '+secret}) as http:
        async with streamable_http_client(URL,http_client=http) as (read,write,sid):
            async with ClientSession(read,write) as session:
                await session.initialize();yield session,sid,http


@pytest.mark.anyio
async def test_real_sdk_inherited_reads_and_stable_session_renewal(tmp_path):
    host,lease,agent,_=setup(tmp_path);old,_,_=begin(lease,agent,actor='bob')
    async with host.lifespan():
        async with client(host,token(lease)) as (session,sid,http):
            first=await session.call_tool('get_business_profile',{})
            assert not first.isError,first
            assert json.loads(first.content[0].text)=={'actor':'bob','data_owner':'alice','chat_id':'root'}
            assert (await session.call_tool('create_agent',{})).isError
            old_session=sid();lease.end(old);await lease.drain()
            begin(lease,agent,actor='alice')
            second=await session.call_tool('get_business_profile',{})
            assert not second.isError,second
            assert json.loads(second.content[0].text)['actor']=='alice' and sid()==old_session
    assert not lease.current() and not host.transport.calls


@pytest.mark.anyio
async def test_revocation_during_read_drains_original_snapshot_before_renewal(tmp_path):
    host,lease,agent,family=setup(tmp_path);entered=threading.Event();release=threading.Event()
    observed=[]
    def delayed(context,name,arguments):
        observed.append((context.actor,context.data_owner,context.admission._resource))
        entered.set();release.wait(5)
        return {'private_marker':'captured-secret'}
    family.read=delayed
    run={'cancel':asyncio.Event()};agent.active['root']=run
    original=object()
    generation=lease.begin(actor='bob',admission=Admission(lambda:True,resource=original),
        cancel=run['cancel'],deadline=time.monotonic()+60,current_run=lambda:agent.active['root'] is run,
        quiesced=True,run=run,request_id='request-delayed')
    async with host.lifespan():
        async with client(host,token(lease)) as (session,sid,http):
            pending=asyncio.create_task(http.post(URL,headers={'MCP-Session-Id':sid(),
                'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'},
                json={'jsonrpc':'2.0','id':29,'method':'tools/call',
                      'params':{'name':'get_business_profile','arguments':{}}}))
            assert await asyncio.to_thread(entered.wait,3)
            lease.end(generation)
            with pytest.raises(ValueError):begin(lease,agent)
            draining=asyncio.create_task(lease.drain())
            await asyncio.sleep(.02);assert not draining.done()
            release.set()
            result=await pending
            assert result.status_code==401 and 'captured-secret' not in result.text
            await draining
            fresh,_,_=begin(lease,agent)
            assert fresh.current() and observed==[('bob','alice',original)]
            assert not generation.current()


def test_credentials_are_separate_from_public_grants_and_private_storage(tmp_path):
    host,lease,agent,_=setup(tmp_path);begin(lease,agent)
    from mcp_access import MCPGrantStore
    public=MCPGrantStore(tmp_path/'public')
    _,public_token=public.issue(company_id=workspace_scope(agent.state),owner_principal_id='alice',dataset='company')
    assert host.authenticate(public_token) is None and public.authenticate(token(lease)) is None
    (host.root/'credential-seed').unlink()
    with pytest.raises(ValueError):ManagedMCPHost(tmp_path/'host',public_origin='https://managed.example.invalid')


def test_provider_session_cannot_be_rebound_to_another_local_binding(tmp_path):
    host,lease,agent,_=setup(tmp_path);lease.created('provider-shared')
    another=host.bind_session('binding-two',company_id=workspace_scope(agent.state),data_owner='alice',
        root_chat='root',collection='company',services=HTTPMCPServices(agent.state,agent))
    with pytest.raises(ValueError):another.created('provider-shared')


def test_same_request_recovery_preserves_actor_and_requires_restart_or_drained_disconnect(tmp_path):
    host,lease,agent,_=setup(tmp_path)
    run={'cancel':asyncio.Event()};agent.active['root']=run
    arguments=dict(actor='alice',admission=Admission(lambda:True),cancel=run['cancel'],
        deadline=time.monotonic()+60,current_run=lambda:agent.active.get('root') is run,
        run=run,request_id='funded-request')
    original=lease.begin(**arguments,quiesced=True)
    lease.end(original)
    with pytest.raises(ValueError):lease.begin(**arguments,quiesced=False,recovering=True)
    restarted=ManagedMCPHost(tmp_path/'host',public_origin='https://managed.example.invalid')
    restored=restarted.bind_session('binding-one',company_id=workspace_scope(agent.state),data_owner='alice',
        root_chat='root',collection='company',services=HTTPMCPServices(agent.state,agent))
    with pytest.raises(ValueError):restored.begin(**(arguments|{'actor':'bob'}),quiesced=False,recovering=True)
    with pytest.raises(ValueError):restored.begin(**(arguments|{'request_id':'new-money'}),quiesced=False,recovering=True)
    recovered=restored.begin(**arguments,quiesced=False,recovering=True)
    assert recovered.current()
    restored.end(recovered,disconnected=True)
    next_attempt=restored.begin(**arguments,quiesced=False,recovering=True)
    assert next_attempt.current()
    restored.revoke(next_attempt)
    newer=ManagedMCPHost(tmp_path/'host',public_origin='https://managed.example.invalid')
    retired=newer.bind_session('binding-one',company_id=workspace_scope(agent.state),data_owner='alice',
        root_chat='root',collection='company',services=HTTPMCPServices(agent.state,agent))
    with pytest.raises(ValueError):retired.begin(**arguments,quiesced=False,recovering=True)


def test_observed_authority_loss_remains_revoked_after_permission_returns(tmp_path):
    host,lease,agent,_=setup(tmp_path);generation,_,allowed=begin(lease,agent)
    allowed[0]=False;assert not generation.current()
    allowed[0]=True
    assert not generation.current() and host.authenticate(token(lease)) is None


def test_host_endpoint_is_bearer_only_and_uses_live_chat_membership(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_server_access import FixtureIndex
    from agent_bridge import CodexVideoAgent
    import server
    from test_workflow_routes import login
    from scope_host import agent_scope
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN','https://testserver')
    app=server.create_app(tmp_path/'state',FixtureIndex('demo'),agent_factory=CodexVideoAgent,
        index_factory=lambda path:FixtureIndex(Path(path).parent.name))
    with TestClient(app) as client:
        login(client,app,'alice')
        selected=app.state.workspaces['demo'];agent=selected['agent'];agent.state=selected['state']
        agent.children['hosted']={'id':'hosted','owner_principal_id':'alice','dataset':'cafe','messages':[]}
        async def capture():
            async with agent_scope(agent,'hosted','cafe',admit=True) as admission:return admission
        admission=asyncio.run(capture())
        lease=agent.managed_mcp_host.bind_session('binding-http',company_id=workspace_scope(agent.state),
            data_owner='alice',root_chat='hosted',collection='cafe',services=HTTPMCPServices(agent.state,agent))
        run={'cancel':asyncio.Event()};agent.active['hosted']=run
        generation=lease.begin(actor='alice',admission=admission,cancel=run['cancel'],deadline=time.monotonic()+60,
            current_run=lambda:agent.active.get('hosted') is run,quiesced=True,run=run,request_id='request-http')
        initialize={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25',
            'capabilities':{},'clientInfo':{'name':'managed-proof','version':'1'}}}
        headers={'Authorization':'Bearer '+token(lease),'Accept':'application/json, text/event-stream'}
        assert client.post('/runtime/mcp',json=initialize).status_code==401
        client.cookies.clear()
        response=client.post('/runtime/mcp',json=initialize,headers=headers)
        assert response.status_code==200,response.text
        assert client.post('/mcp',json=initialize,headers=headers).status_code==401
        headers.update({'MCP-Session-Id':response.headers['mcp-session-id'],'MCP-Protocol-Version':'2025-11-25'})
        assert client.post('/runtime/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'},headers=headers).status_code==202
        result=client.post('/runtime/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/call',
            'params':{'name':'get_business_profile','arguments':{}}},headers=headers)
        assert result.status_code==200 and not result.json()['result'].get('isError'),result.text
        with app.state.access.connect() as db:
            db.execute('DELETE FROM principal_environments WHERE principal_id=?',('alice',))
        assert not generation.current()
        assert client.post('/runtime/mcp',json=initialize,headers=headers).status_code==401


def test_failed_current_run_callback_retires_recovery(tmp_path):
    host,lease,agent,_=setup(tmp_path);generation,_,_=begin(lease,agent)
    def failed():raise ValueError('Authority unavailable')
    generation.current_run=failed
    assert not generation.current() and lease._generation is None
    assert host._row(lease.binding_id)['recoverable']==0


@pytest.mark.anyio
async def test_revoke_binding_after_restart_prevents_same_request_recovery(tmp_path):
    host, lease, agent, _ = setup(tmp_path)
    generation, run, _ = begin(lease, agent)
    request_id = host._row(lease.binding_id)['request_id']
    restarted = ManagedMCPHost(tmp_path / 'host', public_origin='https://managed.example.invalid')
    assert restarted._row(lease.binding_id)['recoverable'] == 1
    await restarted.revoke_binding(lease.binding_id)
    assert restarted._row(lease.binding_id)['recoverable'] == 0
    restored = restarted.bind_session(lease.binding_id, company_id=workspace_scope(agent.state),
        data_owner='alice', root_chat='root', collection='company', services=HTTPMCPServices(agent.state, agent))
    with pytest.raises(ValueError):
        restored.begin(actor='alice', admission=Admission(lambda: True), cancel=run['cancel'],
            deadline=time.monotonic() + 60, current_run=lambda: True, quiesced=False,
            run=run, request_id=request_id, recovering=True)
    assert not generation.current()
