"""Pinned remote packages execute through the standard SDK and owner authority."""
import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import types
sys.path.insert(0, str(Path(__file__).parents[1]))
from catalogue_providers import RemoteProvider, RemoteProviderCommissioning, ProviderConfigurationError, remote_entries
from mcp_connections import MCPConnections, ConnectionError
from workspace_identity import workspace_scope


def descriptor(provider='canva', *, revision=1, auth='oauth_dcr', **changes):
    entry=remote_entries()[provider]
    host=urlsplit(entry['endpoint']).hostname
    data={'plugin_id':provider,'revision':revision,'catalogue_binding':entry['catalogue_binding'],
          'auth':auth,'issuer':'https://'+host,'authorization_endpoint':'https://'+host+'/oauth/authorize',
          'token_endpoint':'https://'+host+'/oauth/token','registration_endpoint':'https://'+host+'/oauth/register',
          'token_auth_method':'none','scopes_by_mode':{'read':['read'],'read_write':['read','write']},
          'read_tools':['read_original'],'read_scope_authority':False,
          'reference':'https://'+host+'/documentation/mcp'}
    if auth in ('bearer','anonymous'):
        data.update(issuer=None,authorization_endpoint=None,token_endpoint=None,registration_endpoint=None,
                    scopes_by_mode={'read':[],'read_write':[]} if auth=='bearer' else {'read':[]})
    if auth=='oauth_static':data.update(registration_endpoint=None,token_auth_method='client_secret_post')
    data.update(changes)
    return RemoteProvider.from_dict(data)


def configuration(state, *providers):
    return RemoteProviderCommissioning(workspace_scope(state),'https://rocoto.example/api/connections/oauth/callback',tuple(providers))


def test_all_twenty_remote_originals_have_bound_setup_state(tmp_path):
    entries=remote_entries()
    assert len(entries)==20 and entries['cloudflare']['server_name']=='cloudflare-api'
    assert 'google-drive'not in entries and 'openai-developers'not in entries
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    rows={r['id']:r for r in broker.catalogue('alice')['connections']}
    for name in set(entries)-{'notion','linear','sentry'}:
        assert rows[name]['status']=='setup_required' and not rows[name]['available']
    assert rows['canva']['setup_reason']=='provider_commissioning_required'
    original=json.loads((Path(__file__).parents[1]/'runtime/plugin_catalogue.json').read_bytes())
    assert next(x for x in original['plugins']if x['id']=='google-drive')['supported_api']is False


def test_commissioned_descriptor_changes_real_broker_policy(tmp_path):
    selected=descriptor()
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=configuration(tmp_path,selected))
    row=next(r for r in broker.catalogue('alice')['connections']if r['id']=='canva')
    assert row['available'] and row['status']=='disconnected'
    assert row['catalogue_binding']==remote_entries()['canva']['catalogue_binding']
    assert broker.provider_policy('canva')['endpoint']=='https://mcp.canva.com/mcp'
    assert row['write_available']is False

READ=types.Tool(name='read_original',title='Original read',description='Original provider contract',
    inputSchema={'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False},
    outputSchema={'type':'object','properties':{'id':{'type':'string'}}},
    annotations=types.ToolAnnotations(readOnlyHint=True),_meta={'original':'unchanged'})
WRITE=types.Tool(name='new_provider_action',description='A newly discovered provider action',
    inputSchema={'type':'object','properties':{'title':{'type':'string'}},'required':['title'],'additionalProperties':False},
    annotations=types.ToolAnnotations(readOnlyHint=True),_meta={'provider':'original'})
TOKEN='synthetic-owner-only-provider-token'

class Remote:
    def __init__(self,desc,mode='read'):
        self.config=desc.policy();self.mode=mode;self.challenge=None;self.calls=[];self.requests=[]
        self.registrations=[];self.token_requests=[];self.release=None;self.entered=asyncio.Event()
        self.token_scope=None;self.malicious=None
    async def __call__(self,request):
        cfg=self.config;url=str(request.url);self.requests.append((request.method,url))
        if '/.well-known/oauth-protected-resource'in request.url.path:
            return httpx.Response(200,json={'resource':cfg['endpoint'],'authorization_servers':[cfg['issuer']],
                'scopes_supported':['read','write','admin']})
        if '/.well-known/oauth-authorization-server'in request.url.path:
            data={'issuer':cfg['issuer'],'authorization_endpoint':cfg['authorization_endpoint'],
                'token_endpoint':cfg['token_endpoint'],'response_types_supported':['code'],
                'grant_types_supported':['authorization_code','refresh_token'],'scopes_supported':['read','write','admin'],
                'token_endpoint_auth_methods_supported':['none','client_secret_post','client_secret_basic'],
                'code_challenge_methods_supported':['S256']}
            if cfg['registration_endpoint']:data['registration_endpoint']=cfg['registration_endpoint']
            if self.malicious:data.update(self.malicious)
            return httpx.Response(200,json=data)
        if url==cfg['registration_endpoint']:
            data=json.loads(request.content);self.registrations.append(data)
            assert data['scope']==' '.join(cfg['scopes_by_mode'][self.mode])
            return httpx.Response(201,json={**data,'client_id':'synthetic-public-client'})
        if url==cfg['token_endpoint']:
            data=parse_qs(request.content.decode());self.token_requests.append(data)
            if data['grant_type']==['authorization_code']:
                challenge=base64.urlsafe_b64encode(hashlib.sha256(data['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                assert challenge==self.challenge
            if cfg.get('registration')=='static':
                if cfg['token_auth_method']=='client_secret_post':assert data['client_secret']==['synthetic-private-client-secret']
                if cfg['token_auth_method']=='client_secret_basic':assert request.headers['authorization'].startswith('Basic ')
            return httpx.Response(200,json={'access_token':TOKEN,'refresh_token':'synthetic-private-refresh','token_type':'Bearer',
                'expires_in':300,'scope':self.token_scope if self.token_scope is not None else ' '.join(cfg['scopes_by_mode'][self.mode])})
        assert request.url==httpx.URL(cfg['endpoint']),'Unexpected authentication destination'
        if cfg['auth']!='anonymous' and request.headers.get('authorization')!='Bearer '+TOKEN:
            return httpx.Response(401,headers={'WWW-Authenticate':'Bearer resource_metadata="'+cfg['issuer']+'/.well-known/oauth-protected-resource", scope="read write admin"'})
        if request.method in ('GET','DELETE'):return httpx.Response(405)
        body=json.loads(request.content);method=body['method']
        if method.startswith('notifications/'):return httpx.Response(202)
        if method=='initialize':result={'protocolVersion':body['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'synthetic','version':'1'}}
        elif method=='tools/list':result={'tools':[t.model_dump(mode='json',by_alias=True,exclude_none=True)for t in (READ,WRITE)]}
        elif method=='tools/call':
            self.calls.append(body);self.entered.set()
            if self.release:await self.release.wait()
            result={'content':[{'type':'text','text':'Original result'}],'structuredContent':{'id':'doc-1'},'_meta':{'original':True},'isError':False}
        else:raise AssertionError(method)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})

async def connected(state,monkeypatch,*,provider='canva',auth='oauth_dcr',mode='read',writes=False,desc=None,malicious=None):
    import mcp_connections
    from registered_oauth import RegisteredClient,RegisteredOAuthClients
    from mcp_connections import BearerCredential
    desc=desc or descriptor(provider,auth=auth)
    remote=Remote(desc,mode);remote.malicious=malicious
    monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(remote))
    registration=None
    if desc.auth=='oauth_static':
        registration=RegisteredOAuthClients(workspace_scope(state),'https://rocoto.example/api/connections/oauth/callback',(
            RegisteredClient(provider,1,'https://rocoto.example/api/connections/oauth/callback','synthetic-client',''if desc.token_auth_method=='none'else'synthetic-private-client-secret',desc.issuer,desc.token_auth_method),))
    broker=MCPConnections(state,public_origin='https://rocoto.example',remote_providers=configuration(state,desc),
                          registered_clients=registration,write_providers={provider}if writes else set())
    if desc.auth=='bearer':
        result=await broker.connect_bearer(provider,BearerCredential(TOKEN),principal_id='alice',session_binding='browser',access_mode=mode)
    else:
        result=await broker.start(provider,principal_id='alice',session_binding='browser',access_mode=mode)
        if result['status']=='awaiting_authorization':
            args=parse_qs(urlsplit(result['authorization_url']).query)
            remote.challenge=args['code_challenge'][0]
            result=await broker.oauth_callback(state=args['state'][0],code='fixture-code',principal_id='alice',session_binding='browser')
    return broker,remote,result

@pytest.mark.parametrize('auth',['oauth_dcr','oauth_static','bearer','anonymous'])
def test_actual_sdk_executes_original_contract_for_commissioned_auth(tmp_path,monkeypatch,auth):
    async def run():
        broker,remote,result=await connected(tmp_path,monkeypatch,auth=auth)
        try:
            assert result['status']=='connected'
            inventory=await broker.tools('canva',principal_id='alice')
            assert inventory['tools']==[READ.model_dump(mode='json',by_alias=True,exclude_none=True)]
            response=await broker.call('canva',READ.name,{'query':'hello'},principal_id='alice')
            assert response['result']['structuredContent']=={'id':'doc-1'}
            with pytest.raises(ConnectionError):await broker.call('canva',WRITE.name,{'title':'hi'},principal_id='alice')
            with pytest.raises(ConnectionError):await broker.tools('canva',principal_id='bob')
            assert len(remote.calls)==1
            assert len(remote.registrations)==(1 if auth=='oauth_dcr'else 0)
            for path in broker.directory.iterdir():
                if path.is_file():assert TOKEN.encode()not in path.read_bytes()
            if auth.startswith('oauth'):
                with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-1,))
                await broker.tools('canva',principal_id='alice')
                assert remote.token_requests[-1]['grant_type']==['refresh_token']
        finally:await broker.close()
    asyncio.run(run())

@pytest.mark.parametrize('provider',list(remote_entries()))
def test_each_original_remote_package_accepts_same_standard_sdk_onboarding(tmp_path,monkeypatch,provider):
    async def run():
        broker,remote,result=await connected(tmp_path,monkeypatch,provider=provider)
        assert result['status']=='connected'
        result=await broker.call(provider,READ.name,{'query':'test'},principal_id='alice')
        assert result['result']['_meta']=={'original':True}
        await broker.close()
    asyncio.run(run())

@pytest.mark.parametrize('change',[
    {'catalogue_binding':'0'*64},{'plugin_id':'google-drive'},{'revision':True},
    {'token_endpoint':'http://mcp.canva.com/token'}, {'issuer':'https://127.0.0.1'},
    {'authorization_endpoint':'https://user:secret@mcp.canva.com/authorize'},
    {'token_endpoint':'https://mcp.canva.com/token?secret=x'},
    {'registration_endpoint':'https://mcp.canva.com/oauth/token'},
    {'read_scope_authority':1},{'scopes_by_mode':{'read':['admin'],'read_write':['write']}},
])
def test_descriptor_rejects_changed_provenance_or_unbounded_authority(change):
    with pytest.raises(ProviderConfigurationError):descriptor(**change)


def test_private_loader_and_defensive_policy_copies(tmp_path):
    from dataclasses import asdict
    desc=descriptor();state=tmp_path/'state';state.mkdir(mode=0o700)
    raw=asdict(desc);raw['scopes_by_mode']=dict(raw['scopes_by_mode'])
    document={'schema_version':1,'company_scope':workspace_scope(state),
        'redirect_uri':'https://rocoto.example/api/connections/oauth/callback','providers':[raw]}
    path=state/'providers.json';path.write_text(json.dumps(document));path.chmod(0o600)
    config=RemoteProviderCommissioning.load(path,company_scope=workspace_scope(state),public_origin='https://rocoto.example')
    assert config==configuration(state,desc)
    broker=MCPConnections(state,public_origin='https://rocoto.example',remote_providers=config)
    view=broker.provider_policy('canva');view['endpoint']='https://different.example'
    assert broker.provider_policy('canva')['endpoint']=='https://mcp.canva.com/mcp'
    path.chmod(0o644)
    with pytest.raises(ProviderConfigurationError):RemoteProviderCommissioning.load(path,company_scope=workspace_scope(state),public_origin='https://rocoto.example')
    with pytest.raises(ConnectionError):MCPConnections(tmp_path/'other',public_origin='https://rocoto.example',remote_providers=config)
    with pytest.raises(ConnectionError):MCPConnections(state,public_origin='https://wrong.example',remote_providers=config)


def test_descriptor_rotation_removal_and_builtin_downgrade_fence(tmp_path,monkeypatch):
    async def run():
        old,remote,result=await connected(tmp_path,monkeypatch,provider='notion')
        assert result['status']=='connected';generation=old.read_generation('notion',principal_id='alice')
        builtin=MCPConnections(tmp_path,public_origin='https://rocoto.example')
        assert not builtin.provider_available('notion')
        new=MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=configuration(tmp_path,descriptor('notion',revision=2)))
        assert not old.provider_available('notion') and new.provider_available('notion')
        with pytest.raises(ConnectionError):await old.tools('notion',principal_id='alice')
        assert new._record('notion','alice')['generation']>generation and new._record('notion','alice')['tokens']is None
        removed=MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=configuration(tmp_path))
        assert not new.provider_available('notion') and not removed.provider_available('notion')
        with pytest.raises(ConnectionError):MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=configuration(tmp_path,descriptor('notion',revision=2)))
        with pytest.raises(ConnectionError):MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=configuration(tmp_path,descriptor('notion',revision=1)))
        restored=MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=configuration(tmp_path,descriptor('notion',revision=3)))
        assert restored.provider_available('notion')
        for broker in (old,builtin,new,removed,restored):await broker.close()
    asyncio.run(run())


def test_descriptor_update_is_atomic_with_other_removal(tmp_path):
    one=descriptor();two=descriptor('slack')
    old=MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=configuration(tmp_path,one,two))
    with pytest.raises(ConnectionError):
        MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=configuration(tmp_path,descriptor(reference='https://mcp.canva.com/new-doc')))
    assert old.provider_available('canva')and old.provider_available('slack')


@pytest.mark.parametrize('change',[
    {'token_endpoint':'https://mcp.canva.com/other-token'},
    {'registration_endpoint':'https://mcp.canva.com/other-register'},
    {'authorization_endpoint':'https://mcp.canva.com/other-authorize'},
    {'issuer':'https://another.example'},
])
def test_oauth_metadata_cannot_redirect_credentials_or_consent(tmp_path,monkeypatch,change):
    async def run():
        broker,remote,result=await connected(tmp_path,monkeypatch,malicious=change)
        assert result['status']=='failed' and not remote.token_requests and not remote.registrations
        assert not any('other-'in u for _,u in remote.requests)
        await broker.close()
    asyncio.run(run())


@pytest.mark.parametrize('auth',['oauth_dcr','oauth_static'])
def test_escalating_granted_scopes_revoke_customer_credential(tmp_path,monkeypatch,auth):
    async def run():
        broker,remote,result=await connected(tmp_path,monkeypatch,auth=auth)
        assert result['status']=='connected'
        remote.token_scope='read admin'
        with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-1,))
        with pytest.raises(ConnectionError):await broker.tools('canva',principal_id='alice')
        assert broker._record('canva','alice')['tokens']is None and not remote.calls
        await broker.close()
    asyncio.run(run())


def test_generic_static_requires_matching_registered_client_and_bearer_stays_private(tmp_path):
    from registered_oauth import RegisteredOAuthClients,RegisteredClient
    from mcp_connections import BearerCredential
    desc=descriptor(auth='oauth_static');cfg=configuration(tmp_path,desc)
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=cfg)
    row=next(x for x in broker.catalogue('alice')['connections']if x['id']=='canva')
    assert not row['available']and row['setup_reason']=='registered_client_required'
    with pytest.raises(ConnectionError):
        MCPConnections(tmp_path,public_origin='https://rocoto.example',remote_providers=cfg,
            registered_clients=RegisteredOAuthClients(cfg.company_scope,cfg.redirect_uri,(
                RegisteredClient('canva',1,cfg.redirect_uri,'private-id','secret','https://wrong.example','client_secret_post'),)))
    assert TOKEN not in repr(BearerCredential(TOKEN))
    for bad in [' token','token\n','token:x','x'*16385]:
        with pytest.raises(ConnectionError):BearerCredential(bad)


@asynccontextmanager
async def native(tmp_path,monkeypatch,*,auth='oauth_dcr',writes=True):
    import plugin_catalogue
    from native_packages import NativePackageStore
    from native_provider_relay import NativeProviderRelay
    from native_approvals import NativeApprovals,ApprovalScope
    from native_mcp import NativeMCP,NativeMCPScope
    from test_native_mcp import fixture as native_fixture
    entry=plugin_catalogue.read_plugin('canva');root=tmp_path/'package'
    (root/'.codex-plugin').mkdir(parents=True)
    (root/'.codex-plugin/plugin.json').write_text(json.dumps(entry['manifest']))
    (root/'.mcp.json').write_text(json.dumps({'mcpServers':{'canva':{'type':'http','url':remote_entries()['canva']['endpoint']}}}))
    class Materializer:
        @asynccontextmanager
        async def acquire(self,*args):yield root,{'version':entry['version'],'installed':True,'enabled':True}
    store=NativePackageStore(tmp_path/'state',materializer=Materializer())
    await store.install('canva',principal_id='alice');selection=store.snapshot(principal_id='alice')
    broker,remote,result=await connected(store.state,monkeypatch,auth=auth,mode='read_write',writes=writes)
    assert result['status']=='connected'
    relay=NativeProviderRelay(store,broker,selection,company_id=store.company_scope,principal_id='alice')
    approvals=NativeApprovals(store.state);scope=ApprovalScope(store.company_scope,'alice','root','run:1','native-root')
    transport=relay.configure_writes(approvals,scope,enabled=True,timeout=3)
    transport.transport_factory=lambda binding:httpx.MockTransport(remote)
    agent,_,_=native_fixture();mcp_scope=NativeMCPScope(store.company_scope,'alice','cafe','root','native-root')
    app=NativeMCP(agent,mcp_scope,company_id=store.company_scope,provider_relay=relay)
    grant=await app.issue_provider('canva')
    try:
        async with app.lifespan():yield store,broker,remote,relay,approvals,scope,agent,app,grant
    finally:await transport.close();await broker.close()

ARGS={'title':'Approved fixture'}
META={'callId':'call-0','threadId':'native-root','x-codex-turn-metadata':{'turn_id':'turn-0'}}

async def approve(approvals,scope,relay):
    binding=await relay.approval_binding('canva',WRITE.name)
    row=approvals.open(scope=scope,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=binding,tool_arguments=ARGS)
    async def deliver(identifier,payload,guard):assert guard()
    await approvals.answer(row['id'],scope=scope,binding=binding,action='allow_once',deliver=deliver,cancelled=asyncio.Event())
    return row

@pytest.mark.parametrize('auth',['oauth_dcr','oauth_static','bearer'])
def test_native_original_schema_approval_and_single_effect_for_each_auth(tmp_path,monkeypatch,auth):
    from test_native_mcp import client
    async def run():
        async with native(tmp_path,monkeypatch,auth=auth)as(store,broker,remote,relay,approvals,scope,agent,app,grant):
            async with client(app,grant.token)as session:
                tools=(await session.list_tools()).tools
                assert [t.model_dump(by_alias=True)for t in tools]==[t.model_dump(by_alias=True)for t in (READ,WRITE)]
                assert (await session.call_tool(WRITE.name,ARGS,meta=META)).isError and not remote.calls
                row=await approve(approvals,scope,relay)
                result=await session.call_tool(WRITE.name,ARGS,meta=META)
                assert not result.isError and result.structuredContent=={'id':'doc-1'}and result.meta=={'original':True}
                assert (await session.call_tool(WRITE.name,ARGS,meta=META))==result
                assert len(remote.calls)==1
                assert approvals.get(row['id'],scope=scope)['status']=='succeeded'
    asyncio.run(run())


def test_discovered_actions_stay_denied_without_host_write_commissioning(tmp_path,monkeypatch):
    from test_native_mcp import client
    async def run():
        async with native(tmp_path,monkeypatch,auth='bearer',writes=False)as(store,broker,remote,relay,approvals,scope,agent,app,grant):
            async with client(app,grant.token)as session:
                assert len((await session.list_tools()).tools)==2
                binding=await relay.approval_binding('canva',WRITE.name)
                assert not binding.write_enabled
                assert (await session.call_tool(WRITE.name,ARGS,meta=META)).isError
                assert not remote.calls
    asyncio.run(run())


@pytest.mark.parametrize('action',['stop','disconnect','rotate'])
def test_inflight_effect_drains_receipt_and_withholds_stale_output(tmp_path,monkeypatch,action):
    from test_native_mcp import client
    async def run():
        async with native(tmp_path,monkeypatch,auth='bearer')as(store,broker,remote,relay,approvals,scope,agent,app,grant):
            row=await approve(approvals,scope,relay);remote.release=asyncio.Event()
            async with client(app,grant.token)as session:
                task=asyncio.create_task(session.call_tool(WRITE.name,ARGS,meta=META))
                await asyncio.wait_for(remote.entered.wait(),3)
                pending=None;new=None
                if action=='stop':agent.active['root']['cancel'].set();approvals.invalidate(scope=scope,reason='stopped')
                elif action=='disconnect':pending=asyncio.create_task(broker.disconnect('canva',principal_id='alice'))
                else:new=MCPConnections(store.state,public_origin='https://rocoto.example',remote_providers=configuration(store.state,descriptor(auth='bearer',revision=2)))
                await asyncio.sleep(.02);assert not task.done()
                remote.release.set();result=await task
                assert result.isError and len(remote.calls)==1
                if pending:await pending
                if new:await new.close()
                assert approvals.get(row['id'],scope=scope)['status']=='succeeded'
    asyncio.run(run())


def test_transport_connects_to_validated_ip_with_original_host_and_tls_sni(monkeypatch):
    import mcp_connections
    async def run():
        monkeypatch.setattr(mcp_connections.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))])
        seen=[]
        async def capture(request):
            seen.append(request)
            return httpx.Response(200,json={})
        transport=mcp_connections._GuardedTransport({'mcp.canva.com'})
        await transport.inner.aclose();transport.inner=httpx.MockTransport(capture)
        async with httpx.AsyncClient(transport=transport)as client:
            await client.get('https://mcp.canva.com/mcp',headers={'Authorization':'Bearer synthetic'})
        request=seen[0]
        assert request.url.host=='8.8.8.8'and request.headers['host']=='mcp.canva.com'
        assert request.extensions['sni_hostname']=='mcp.canva.com'
    asyncio.run(run())

@pytest.mark.parametrize('method',['none','client_secret_basic','client_secret_post'])
def test_generic_registered_oauth_client_auth_methods_and_path_issuer(tmp_path,monkeypatch,method):
    async def run():
        desc=descriptor(auth='oauth_static',issuer='https://mcp.canva.com/tenant',token_auth_method=method)
        broker,remote,result=await connected(tmp_path,monkeypatch,desc=desc)
        assert result['status']=='connected'and not remote.registrations
        assert (await broker.call('canva',READ.name,{'query':'verified'},principal_id='alice'))['result']['structuredContent']=={'id':'doc-1'}
        await broker.close()
    asyncio.run(run())
