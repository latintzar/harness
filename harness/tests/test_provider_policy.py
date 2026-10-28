"""Commissioned provider inventory and encrypted authority, synthetic IO only."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.auth import OAuthToken
sys.path.insert(0,str(Path(__file__).parents[1]))
from mcp_connections import MCPConnections, ConnectionError, _Storage


def test_policy_lists_linear_and_pending_figma(tmp_path):
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    rows={r['id']:r for r in broker.catalogue('alice')['connections']}
    assert rows['linear']['available'] is True
    assert rows['figma']['available'] is False
    assert rows['figma']['status']=='setup_required'


@pytest.mark.parametrize('commissioned',[False,True])
def test_read_write_discovers_original_contract_but_cannot_dispatch_write(tmp_path,monkeypatch,commissioned):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',write_providers={'notion'}if commissioned else set())
        tool=types.Tool(name='notion-create-pages',description='Original writing tool',inputSchema={'type':'object'},
            outputSchema={'type':'object'},annotations=types.ToolAnnotations(readOnlyHint=False),
            icons=[types.Icon(src='https://mcp.notion.com/icon.svg')],_meta={'provider_field':'retained'})
        calls=[]
        class Session:
            async def initialize(self):pass
            async def list_tools(self,cursor=None):return types.ListToolsResult(tools=[tool])
            async def call_tool(self,*args):calls.append(args);raise AssertionError('Write dispatched')
        @asynccontextmanager
        async def session(*args,**kwargs):yield Session()
        monkeypatch.setattr(broker,'_session',session)
        flow=await broker.start('notion',principal_id='alice',session_binding='browser',access_mode='read_write')
        assert flow['status']=='connected'
        result=await broker.tools('notion',principal_id='alice')
        assert result['tools']==[tool.model_dump(mode='json',by_alias=True,exclude_none=True)]
        assert result['tool_permissions']=={'notion-create-pages':'approval_required'}
        with pytest.raises(ConnectionError,match='approval'):
            await broker.call('notion',tool.name,{},principal_id='alice')
        assert not calls
        await broker.close()
    asyncio.run(scenario())


def test_oauth_tokens_are_ciphertext_at_rest(tmp_path):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
        generation=broker._begin('notion','alice')
        storage=_Storage(broker,'alice','notion',generation)
        await storage.set_tokens(OAuthToken(access_token='private-access-fixture',refresh_token='private-refresh-fixture',scope='default'))
        assert b'private-access-fixture' not in broker.database.read_bytes()
        assert (await storage.get_tokens()).access_token=='private-access-fixture'
        await broker.close()
    asyncio.run(scenario())


def test_real_sdk_scope_clamp_and_linear_read_authority(tmp_path,monkeypatch):
    asyncio.run(_oauth_scenario(tmp_path,monkeypatch,'linear','read'))


def test_real_sdk_full_read_write_inventory_and_binding(tmp_path,monkeypatch):
    asyncio.run(_oauth_scenario(tmp_path,monkeypatch,'linear','read_write'))


def test_figma_confidential_dcr_fixture_remains_setup_pending_by_default(tmp_path,monkeypatch):
    import mcp_connections
    pending=mcp_connections.CATALOGUE['figma']
    assert pending['available']is False
    monkeypatch.setitem(mcp_connections.CATALOGUE,'figma',{**pending,'available':True})
    asyncio.run(_oauth_scenario(tmp_path,monkeypatch,'figma','read_write'))


async def _oauth_scenario(tmp_path,monkeypatch,provider,mode,*,token_scope=None,refresh_scope=None):
    import httpx,time
    from urllib.parse import parse_qs,urlsplit
    import mcp_connections
    config=mcp_connections.CATALOGUE[provider]
    expected=' '.join(config['scopes_by_mode'][mode]);issuer='https://api.figma.com'if provider=='figma'else'https://mcp.linear.app'
    endpoint=config['endpoint'];calls=[];registrations=[];tokens=[]
    tool={'name':'provider-new-tool','title':'Exact original title','description':'Original provider description',
        'inputSchema':{'type':'object','properties':{'query':{'type':'string'}},'additionalProperties':False},
        'outputSchema':{'type':'object'},'annotations':{'readOnlyHint':True},'_meta':{'verbatim':'original'},
        'icons':[{'src':'https://mcp.linear.app/icon.svg'}]}
    async def remote(request):
        path=request.url.path
        if path.startswith('/.well-known/oauth-protected-resource'):
            return httpx.Response(200,json={'resource':endpoint,'authorization_servers':[issuer],
                'scopes_supported':['mcp:connect']if provider=='figma'else['read','write','openid','email']})
        if path.startswith('/.well-known/oauth-authorization-server'):
            return httpx.Response(200,json={'issuer':issuer,'authorization_endpoint':issuer+'/authorize',
                'token_endpoint':issuer+'/token','registration_endpoint':issuer+'/register',
                'response_types_supported':['code'],'grant_types_supported':['authorization_code','refresh_token'],
                'token_endpoint_auth_methods_supported':[config['token_auth_method']],
                'scopes_supported':['mcp:connect']if provider=='figma'else['read','write','openid','email'],
                'code_challenge_methods_supported':['S256']})
        if path=='/register':
            body=json.loads(request.content);registrations.append(body)
            assert body['scope']==expected and body['token_endpoint_auth_method']==config['token_auth_method']
            return httpx.Response(201,json={**body,'client_id':'fixture-public-client',
                **({'client_secret':'fixture-confidential-secret'}if provider=='figma'else{})})
        if path=='/token':
            body=parse_qs(request.content.decode());tokens.append(body)
            if provider=='figma':
                assert request.headers.get('authorization','').startswith('Basic ')
                assert 'client_secret'not in body
            actual=refresh_scope if body['grant_type']==['refresh_token']and refresh_scope is not None else token_scope
            return httpx.Response(200,json={'access_token':'synthetic-private-token','refresh_token':'synthetic-private-refresh',
                'token_type':'Bearer','expires_in':300,'scope':actual if actual is not None else expected})
        assert str(request.url)==endpoint
        if request.headers.get('authorization')!='Bearer synthetic-private-token':
            return httpx.Response(401,headers={'WWW-Authenticate':'Bearer resource_metadata="'+endpoint.rsplit('/mcp',1)[0]+'/.well-known/oauth-protected-resource", scope="read write"'})
        if request.method in('GET','DELETE'):return httpx.Response(405)
        body=json.loads(request.content)
        if body['method'].startswith('notifications/'):return httpx.Response(202)
        if body['method']=='initialize':result={'protocolVersion':body['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'Original provider','version':'1'}}
        elif body['method']=='tools/list':result={'tools':[tool]}
        elif body['method']=='tools/call':calls.append(body);result={'content':[{'type':'text','text':'Original result'}],'structuredContent':{}}
        else:raise AssertionError(body['method'])
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})
    monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(remote))
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    flow=await broker.start(provider,principal_id='alice',session_binding='browser',access_mode=mode)
    assert flow['status']=='awaiting_authorization'
    authorization=parse_qs(urlsplit(flow['authorization_url']).query)
    assert authorization['scope']==[expected]
    result=await broker.oauth_callback(state=authorization['state'][0],code='synthetic-code',principal_id='alice',session_binding='browser')
    if token_scope is not None and not set(token_scope.split())<=set(expected.split()):
        assert result['status']=='failed'
        assert broker._record(provider,'alice')['tokens']is None
    else:
        assert result['status']=='connected'
        listed=await broker.tools(provider,principal_id='alice')
        actual=types.Tool.model_validate(tool).model_dump(mode='json',by_alias=True,exclude_none=True)
        assert listed['tools']==[actual]
        binding=await broker.execution_binding(provider,tool['name'],principal_id='alice')
        assert binding['tool_contract']==actual and binding['write_enabled']is False
        from provider_policy import digest
        assert binding['schema_sha256']==digest(actual)
        if mode=='read':
            assert listed['tool_permissions'][tool['name']]=='read'
            assert (await broker.call(provider,tool['name'],{'query':'known'},principal_id='alice'))['result']['content'][0]['text']=='Original result'
        else:
            assert listed['tool_permissions'][tool['name']]=='approval_required'
            with pytest.raises(ConnectionError,match='approval'):await broker.call(provider,tool['name'],{},principal_id='alice')
        if refresh_scope is not None:
            generation=broker.read_generation(provider,principal_id='alice')
            with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-10,))
            with pytest.raises(ConnectionError):await broker.tools(provider,principal_id='alice')
            assert broker._record(provider,'alice')['generation']>generation
            assert broker._record(provider,'alice')['status']=='reconnect_required'
    for path in broker.directory.iterdir():
        if path.is_file():assert b'synthetic-private-'not in path.read_bytes()and b'fixture-confidential-secret'not in path.read_bytes()
    assert len(registrations)==1
    await broker.close()


def test_initial_token_scope_expansion_is_rejected(tmp_path,monkeypatch):
    asyncio.run(_oauth_scenario(tmp_path,monkeypatch,'linear','read',token_scope='read write'))


def test_refresh_scope_expansion_revokes_generation(tmp_path,monkeypatch):
    asyncio.run(_oauth_scenario(tmp_path,monkeypatch,'linear','read',refresh_scope='read write'))


def test_read_mode_never_trusts_tool_hint_for_write_authority(tmp_path,monkeypatch):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
        generation=broker._begin('notion','alice');broker._update('notion','alice',generation,status='connected')
        class Session:
            async def initialize(self):pass
            async def list_tools(self,cursor=None):return types.ListToolsResult(tools=[types.Tool(name='delete_everything',inputSchema={'type':'object'},annotations=types.ToolAnnotations(readOnlyHint=True))])
            async def call_tool(self,*args):raise AssertionError('Hint became authority')
        @asynccontextmanager
        async def session(*a,**kw):yield Session()
        monkeypatch.setattr(broker,'_session',session)
        assert (await broker.tools('notion',principal_id='alice'))['tools']==[]
        with pytest.raises(ConnectionError):await broker.call('notion','delete_everything',{},principal_id='alice')
        await broker.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind',['duplicate','count','bytes','external_ref'])
def test_full_inventory_remains_bounded_even_for_undispatchable_tools(tmp_path,monkeypatch,kind):
    async def scenario():
        import mcp_connections
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
        generation=broker._begin('notion','alice','read_write');broker._update('notion','alice',generation,status='connected')
        data=[types.Tool(name='new-tool',inputSchema={'type':'object'})]
        if kind=='duplicate':data*=2
        if kind=='count':data=[types.Tool(name='tool-'+str(n),inputSchema={'type':'object'})for n in range(513)]
        if kind=='bytes':
            monkeypatch.setattr(mcp_connections,'MAX_INVENTORY_BYTES',1000)
            data=[types.Tool(name='tool-'+str(n),description='x'*400,inputSchema={'type':'object'})for n in range(3)]
        if kind=='external_ref':data=[types.Tool(name='new-tool',inputSchema={'$ref':'https://evil.example/schema'})]
        class Session:
            async def initialize(self):pass
            async def list_tools(self,cursor=None):return types.ListToolsResult(tools=data)
        @asynccontextmanager
        async def session(*a,**kw):yield Session()
        monkeypatch.setattr(broker,'_session',session)
        with pytest.raises(ConnectionError):await broker.tools('notion',principal_id='alice')
        await broker.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('configured',[{'figma'},{'openai-docs'},{'unknown'},'notion',None,{'notion',1}])
def test_write_commissioning_rejects_unreviewed_or_unavailable_policy(tmp_path,configured):
    with pytest.raises(ConnectionError,match='commissioned'):
        MCPConnections(tmp_path,public_origin='https://rocoto.example',write_providers=configured)
    assert not (tmp_path/'mcp-connections').exists()


def test_write_commissioning_is_immutable_and_default_disabled(tmp_path):
    default=MCPConnections(tmp_path/'default',public_origin='https://rocoto.example')
    assert all(not r['write_available']for r in default.catalogue('alice')['connections'])
    chosen={'notion'}
    broker=MCPConnections(tmp_path/'configured',public_origin='https://rocoto.example',write_providers=chosen)
    chosen.clear();chosen.add('linear')
    catalogue={r['provider']:r for r in broker.catalogue('alice')['connections']}
    assert catalogue['notion']['write_available']is True and catalogue['linear']['write_available']is False


@pytest.mark.parametrize('provider,mode,granted,commissioned,expected',[
    ('notion','read_write','default',True,True),('notion','read_write','default',False,False),
    ('notion','read','default',True,False),('notion','read_write','',True,False),
    ('linear','read_write','read write',True,True),('linear','read_write','read',True,False),
])
def test_write_binding_requires_host_commission_mode_and_actual_scopes(tmp_path,monkeypatch,provider,mode,granted,commissioned,expected):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',write_providers={provider}if commissioned else set())
        name='notion-fetch'if provider=='notion'else'provider-new-tool'
        tool=types.Tool(name=name,inputSchema={'type':'object'})
        class Session:
            async def initialize(self):pass
            async def list_tools(self,cursor=None):return types.ListToolsResult(tools=[tool])
        @asynccontextmanager
        async def session(*a,**kw):yield Session()
        monkeypatch.setattr(broker,'_session',session)
        generation=broker._begin(provider,'alice',mode)
        broker._update(provider,'alice',generation,status='connected',oauth_scopes=json.dumps(granted.split()))
        binding=await broker.execution_binding(provider,name,principal_id='alice')
        assert binding['write_enabled']is expected
        # Owner connection state is still required even when host commission exists.
        with pytest.raises(ConnectionError):await broker.execution_binding(provider,name,principal_id='bob')
        await broker.close()
    asyncio.run(scenario())
