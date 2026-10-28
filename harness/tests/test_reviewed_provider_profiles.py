"""Real MCP SDK + Rocoto authority; all network is intercepted by local fixtures."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import types

SOURCE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from catalogue_providers import RemoteProvider, RemoteProviderCommissioning, remote_entries
from mcp_connections import MCPConnections, ConnectionError
from workspace_identity import workspace_scope

ROOT=SOURCE/'deploy/connections/provider-profiles'
PROVIDERS=('clickup','granola','stripe','cloudflare')
TOKEN='synthetic-descriptor-owner-token'
ACTION='synthetic_unreviewed_action'
ARGS={'title':'Approved local fixture'}
META={'callId':'call-0','threadId':'native-root','x-codex-turn-metadata':{'turn_id':'turn-0'}}
FETCHES={row['name']:row['url']for row in json.loads((ROOT/'public-fetches.json').read_bytes())['requests']}

def load(provider,folder='reviewed'):
    if provider=='clickup':folder='pending'
    return RemoteProvider.from_dict(json.loads((ROOT/folder/(provider+'.json')).read_bytes()))

def configuration(state,*providers):
    return RemoteProviderCommissioning(workspace_scope(state),'https://rocoto.example/api/connections/oauth/callback',tuple(providers))

def metadata(name):return json.loads((ROOT/'evidence'/(name+'.json')).read_bytes())

class Remote:
    """Public real metadata; synthetic registration, account and complete Tool schema."""
    def __init__(self,desc,mode):
        self.desc=desc;self.mode=mode;self.calls=[];self.registrations=[];self.token_requests=[]
        self.challenge=None;self.returned_scope=None;self.entered=asyncio.Event();self.release=None
        self.auth=metadata(desc.plugin_id+('-issuer-authorization'if desc.plugin_id=='stripe'else'-authorization'))
        self.resource=metadata(desc.plugin_id+'-resource')
        self.tools=[types.Tool(name=name,description='Synthetic contract preserving original metadata',
            inputSchema={'type':'object','properties':{'title':{'type':'string'}},'required':['title'],'additionalProperties':False},
            outputSchema={'type':'object','properties':{'id':{'type':'string'}}},
            annotations=types.ToolAnnotations(readOnlyHint=name==ACTION),_meta={'synthetic_fixture':True})
            for name in (*desc.read_tools,ACTION)]

    async def __call__(self,request):
        url=str(request.url);cfg=self.desc.policy();requested=dict(self.desc.scopes_by_mode)[self.mode]
        if '/.well-known/oauth-protected-resource'in request.url.path:
            assert url==FETCHES[self.desc.plugin_id+'-resource']
            return httpx.Response(200,json=self.resource)
        if '/.well-known/oauth-authorization-server'in request.url.path:
            expected=FETCHES[self.desc.plugin_id+('-issuer-authorization'if self.desc.plugin_id=='stripe'else'-authorization')]
            assert url==expected,(url,expected)
            return httpx.Response(200,json=self.auth)
        if url==self.desc.registration_endpoint:
            data=json.loads(request.content);self.registrations.append(data)
            assert data['scope']==' '.join(requested)
            assert data['token_endpoint_auth_method']==self.desc.token_auth_method
            assert data['redirect_uris']==['https://rocoto.example/api/connections/oauth/callback']
            return httpx.Response(201,json={**data,'client_id':'synthetic-client'})
        if url==self.desc.token_endpoint:
            data=parse_qs(request.content.decode());self.token_requests.append(data)
            if data['grant_type']==['authorization_code']:
                challenge=base64.urlsafe_b64encode(hashlib.sha256(data['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                assert challenge==self.challenge
            return httpx.Response(200,json={'access_token':TOKEN,'refresh_token':'synthetic-refresh','token_type':'Bearer',
                'expires_in':300,'scope':self.returned_scope or ' '.join(requested)})
        assert request.url==httpx.URL(cfg['endpoint']),'Unexpected auth/resource destination'
        if request.headers.get('authorization')!='Bearer '+TOKEN:
            return httpx.Response(401,headers={'WWW-Authenticate':'Bearer resource_metadata="'+FETCHES[self.desc.plugin_id+'-resource']+'"'})
        if request.method in ('GET','DELETE'):return httpx.Response(405)
        body=json.loads(request.content);method=body['method']
        if method.startswith('notifications/'):return httpx.Response(202)
        if method=='initialize':result={'protocolVersion':body['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'local-fixture','version':'1'}}
        elif method=='tools/list':result={'tools':[t.model_dump(mode='json',by_alias=True,exclude_none=True)for t in self.tools]}
        elif method=='tools/call':
            self.calls.append(body);self.entered.set()
            if self.release:await self.release.wait()
            result={'content':[{'type':'text','text':'Synthetic result'}],'structuredContent':{'id':'fixture-1'},'_meta':{'synthetic_fixture':True},'isError':False}
        else:raise AssertionError(method)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})

async def connect(state,monkeypatch,provider,mode='read',*,writes=False,expanded=False):
    import mcp_connections
    desc=load(provider);remote=Remote(desc,mode)
    if expanded:remote.returned_scope=' '.join(dict(desc.scopes_by_mode)[mode])+' invented:admin'
    monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(remote))
    broker=MCPConnections(state,public_origin='https://rocoto.example',remote_providers=configuration(state,desc),
                          write_providers={provider}if writes else set())
    result=await broker.start(provider,principal_id='alice',session_binding='browser',access_mode=mode)
    assert result['status']=='awaiting_authorization',result
    params=parse_qs(urlsplit(result['authorization_url']).query)
    assert urlsplit(result['authorization_url'])._replace(query='').geturl()==desc.authorization_endpoint
    assert params['scope']==[' '.join(dict(desc.scopes_by_mode)[mode])]
    remote.challenge=params['code_challenge'][0]
    assert params['code_challenge_method']==['S256']
    result=await broker.oauth_callback(state=params['state'][0],code='fixture-code',principal_id='alice',session_binding='browser')
    return broker,remote,result

@pytest.mark.parametrize('provider',PROVIDERS)
@pytest.mark.parametrize('mode',['read','read_write'])
def test_actual_sdk_dcr_pkce_refresh_and_owner_policy(tmp_path,monkeypatch,provider,mode):
    async def run():
        broker,remote,result=await connect(tmp_path,monkeypatch,provider,mode,writes=mode=='read_write')
        try:
            if provider=='clickup'and mode=='read':
                # The current broker refuses a connection with zero usable tools.
                # ClickUp must be presented as approval-required until exact reads are reviewed.
                assert result['status']=='failed' and not remote.calls
                return
            assert result['status']=='connected',result
            listed=await broker.tools(provider,principal_id='alice')
            expected=remote.tools if mode=='read_write'else remote.tools[:-1]
            assert listed['tools']==[t.model_dump(mode='json',by_alias=True,exclude_none=True)for t in expected]
            assert set(listed['oauth_scopes'])==set(dict(load(provider).scopes_by_mode)[mode])
            with pytest.raises(ConnectionError):await broker.tools(provider,principal_id='bob')
            with pytest.raises(ConnectionError):await broker.call(provider,ACTION,ARGS,principal_id='alice')
            assert not remote.calls
            if load(provider).read_tools:
                response=await broker.call(provider,load(provider).read_tools[0],ARGS,principal_id='alice')
                assert response['result']['structuredContent']=={'id':'fixture-1'}
            with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-1,))
            await broker.tools(provider,principal_id='alice')
            assert remote.token_requests[-1]['grant_type']==['refresh_token']
            assert len(remote.registrations)==1
            for path in broker.directory.iterdir():
                if path.is_file():assert TOKEN.encode()not in path.read_bytes()
        finally:await broker.close()
    asyncio.run(run())

@pytest.mark.parametrize('provider',PROVIDERS)
def test_expanded_real_metadata_scopes_cannot_grant_extra_authority(tmp_path,monkeypatch,provider):
    async def run():
        broker,remote,result=await connect(tmp_path,monkeypatch,provider,expanded=True)
        try:
            assert result['status']!='connected'
            with pytest.raises(ConnectionError):await broker.tools(provider,principal_id='alice')
            assert not remote.calls
        finally:await broker.close()
    asyncio.run(run())

@asynccontextmanager
async def native(state,monkeypatch,provider):
    import plugin_catalogue
    from native_packages import NativePackageStore
    from native_provider_relay import NativeProviderRelay
    from native_approvals import NativeApprovals,ApprovalScope
    from native_mcp import NativeMCP,NativeMCPScope
    from test_native_mcp import fixture as native_fixture
    entry=plugin_catalogue.read_plugin(provider);root=state/'package';server=remote_entries()[provider]['server_name']
    (root/'.codex-plugin').mkdir(parents=True)
    (root/'.codex-plugin/plugin.json').write_text(json.dumps(entry['manifest']))
    (root/'.mcp.json').write_text(json.dumps({'mcpServers':{server:{'type':'http','url':remote_entries()[provider]['endpoint']}}}))
    class Materializer:
        @asynccontextmanager
        async def acquire(self,*args):yield root,{'version':entry['version'],'installed':True,'enabled':True}
    store=NativePackageStore(state/'state',materializer=Materializer())
    await store.install(provider,principal_id='alice');selection=store.snapshot(principal_id='alice')
    broker,remote,result=await connect(store.state,monkeypatch,provider,'read_write',writes=True)
    assert result['status']=='connected'
    relay=NativeProviderRelay(store,broker,selection,company_id=store.company_scope,principal_id='alice')
    approvals=NativeApprovals(store.state);scope=ApprovalScope(store.company_scope,'alice','root','run:1','native-root')
    transport=relay.configure_writes(approvals,scope,enabled=True,timeout=3)
    transport.transport_factory=lambda binding:httpx.MockTransport(remote)
    agent,_,_=native_fixture();mcp_scope=NativeMCPScope(store.company_scope,'alice','cafe','root','native-root')
    app=NativeMCP(agent,mcp_scope,company_id=store.company_scope,provider_relay=relay)
    grant=await app.issue_provider(server)
    try:
        async with app.lifespan():yield remote,relay,approvals,scope,app,grant,server
    finally:await transport.close();await broker.close()

@pytest.mark.parametrize('provider',PROVIDERS)
def test_native_original_package_route_preserves_schema_and_requires_once_approval(tmp_path,monkeypatch,provider):
    from test_native_mcp import client
    async def run():
        async with native(tmp_path,monkeypatch,provider)as(remote,relay,approvals,scope,app,grant,server):
            async with client(app,grant.token)as session:
                tools=(await session.list_tools()).tools
                assert [t.model_dump(by_alias=True)for t in tools]==[t.model_dump(by_alias=True)for t in remote.tools]
                assert (await session.call_tool(ACTION,ARGS,meta=META)).isError and not remote.calls
                binding=await relay.approval_binding(server,ACTION)
                row=approvals.open(scope=scope,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=binding,tool_arguments=ARGS)
                async def deliver(identifier,payload,guard):assert guard()
                await approvals.answer(row['id'],scope=scope,binding=binding,action='allow_once',deliver=deliver,cancelled=asyncio.Event())
                result=await session.call_tool(ACTION,ARGS,meta=META)
                assert not result.isError and result.structuredContent=={'id':'fixture-1'}
                assert result.meta=={'synthetic_fixture':True}
                assert (await session.call_tool(ACTION,ARGS,meta=META))==result
                assert len(remote.calls)==1
                assert approvals.get(row['id'],scope=scope)['status']=='succeeded'
    asyncio.run(run())

def test_all_descriptor_files_match_saved_metadata_and_original_bindings():
    paths=list((ROOT/'reviewed').glob('*.json'))+list((ROOT/'pending').glob('*.json'))
    assert len(paths)==9
    for path in paths:
        desc=RemoteProvider.from_dict(json.loads(path.read_bytes()));name=desc.plugin_id
        auth=metadata(name+('-issuer-authorization'if name=='stripe'else'-authorization'))
        assert all(getattr(desc,k)==auth[k]for k in ('issuer','authorization_endpoint','token_endpoint','registration_endpoint'))
        assert desc.catalogue_binding==remote_entries()[name]['catalogue_binding']
        assert desc.read_scope_authority is False
        assert desc.token_auth_method in auth['token_endpoint_auth_methods_supported']
        assert 'S256'in auth['code_challenge_methods_supported']

def test_private_company_loader(tmp_path):
    path=tmp_path/'configuration.json';company=workspace_scope(tmp_path)
    payload={'schema_version':1,'company_scope':company,'redirect_uri':'https://rocoto.example/api/connections/oauth/callback',
             'providers':[json.loads((ROOT/('pending'if p=='clickup'else'reviewed')/(p+'.json')).read_bytes())for p in PROVIDERS]}
    path.write_text(json.dumps(payload));path.chmod(0o600)
    result=RemoteProviderCommissioning.load(path,company_scope=company,public_origin='https://rocoto.example')
    assert tuple(p.plugin_id for p in result.providers)==PROVIDERS
    with pytest.raises(ValueError):RemoteProviderCommissioning.load(path,company_scope='other-company',public_origin='https://rocoto.example')
