"""Provider access stays bound to the consenting principal and reviewed reads."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys

import pytest
from mcp.shared.auth import OAuthToken
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).parents[1]))
from mcp_connections import MCPConnections, ConnectionError, _Storage, validate_provider_url


class Session:
    def __init__(self): self.calls=[]
    async def initialize(self): pass
    async def list_tools(self, cursor=None):
        return ListToolsResult(tools=[
            Tool(name='search_openai_docs', description='Read docs', inputSchema={
                'type':'object','properties':{'query':{'type':'string'}},'required':['query'],
                'additionalProperties':False}),
            Tool(name='delete_workspace', description='Write', inputSchema={'type':'object'})])
    async def call_tool(self, name, arguments):
        self.calls.append((name,arguments))
        return CallToolResult(content=[TextContent(type='text',text='Actual provider result')])


def fake_transport(broker, monkeypatch, session=None, oauth=False):
    session=session or Session()
    @asynccontextmanager
    async def connect(provider, principal, generation, flow=None):
        if oauth:
            assert flow is not None
            await broker._redirect(flow, 'https://mcp.notion.com/authorize?state=state-'+flow.id+'&code_challenge=x&code_challenge_method=S256')
            code,state=await flow.callback
            assert code=='valid-code'
            await _Storage(broker,principal,provider,generation).set_tokens(
                OAuthToken(access_token='private-access-token',refresh_token='private-refresh-token',expires_in=60))
        yield session
    monkeypatch.setattr(broker,'_session',connect)
    return session


def test_anonymous_real_tool_contract_is_scoped_and_write_tools_stay_hidden(tmp_path,monkeypatch):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://preview.example.com')
        session=fake_transport(broker,monkeypatch)
        result=await broker.start('openai-docs',principal_id='peter',session_binding='session-a')
        assert result['status']=='connected'
        assert [t['name'] for t in (await broker.tools('openai-docs',principal_id='peter'))['tools']]==['search_openai_docs']
        with pytest.raises(ConnectionError):
            await broker.call('openai-docs','delete_workspace',{},principal_id='peter')
        with pytest.raises(ConnectionError):
            await broker.call('openai-docs','search_openai_docs',{'query':9},principal_id='peter')
        with pytest.raises(ConnectionError):
            await broker.tools('openai-docs',principal_id='alice')
        answer=await broker.call('openai-docs','search_openai_docs',{'query':'MCP'},principal_id='peter')
        assert answer['result']['content'][0]['text']=='Actual provider result'
        assert len(session.calls)==1
        assert next(row for row in broker.catalogue('alice')['connections']if row['id']=='openai-docs')['status']=='disconnected'
        await broker.close()
    asyncio.run(scenario())


def test_oauth_callback_is_principal_session_bound_single_use_and_secret_private(tmp_path,monkeypatch):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://preview.example.com')
        # Only a documented Notion read is available after consent.
        session=Session()
        async def notion_tools(cursor=None):
            return ListToolsResult(tools=[Tool(name='notion-fetch',description='Read page',inputSchema={'type':'object'})])
        session.list_tools=notion_tools
        fake_transport(broker,monkeypatch,session,oauth=True)
        flow=await broker.start('notion',principal_id='peter',session_binding='session-a')
        assert flow['status']=='awaiting_authorization'
        assert 'authorization_url' in flow
        state='state-'+flow['flow_id']
        for principal,binding in [('alice','session-a'),('peter','session-b')]:
            with pytest.raises(ConnectionError):
                await broker.oauth_callback(state=state,code='valid-code',principal_id=principal,session_binding=binding)
        done=await broker.oauth_callback(state=state,code='valid-code',principal_id='peter',session_binding='session-a')
        assert done['status']=='connected'
        with pytest.raises(ConnectionError):
            await broker.oauth_callback(state=state,code='valid-code',principal_id='peter',session_binding='session-a')
        assert 'private-' not in str(broker.catalogue('peter'))
        assert not (broker.database.stat().st_mode & 0o077)
        reopened=MCPConnections(tmp_path,public_origin='https://preview.example.com')
        assert next(row for row in reopened.catalogue('peter')['connections']if row['id']=='notion')['status']=='connected'
        await broker.disconnect('notion',principal_id='peter')
        assert broker._record('notion','peter')['tokens'] is None
        await broker.close();await reopened.close()
    asyncio.run(scenario())


def test_disconnect_fences_late_token_refresh_and_running_results(tmp_path,monkeypatch):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://preview.example.com')
        fake_transport(broker,monkeypatch)
        await broker.start('openai-docs',principal_id='peter',session_binding='a')
        generation=broker._record('openai-docs','peter')['generation']
        storage=_Storage(broker,'peter','openai-docs',generation)
        await broker.disconnect('openai-docs',principal_id='peter')
        with pytest.raises(ConnectionError):
            await storage.set_tokens(OAuthToken(access_token='late-token'))
        assert broker._record('openai-docs','peter')['tokens'] is None
        await broker.close()
    asyncio.run(scenario())


def test_expired_and_interrupted_flows_require_new_consent(tmp_path,monkeypatch):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://preview.example.com',flow_timeout=.05)
        fake_transport(broker,monkeypatch,oauth=True)
        flow=await broker.start('notion',principal_id='peter',session_binding='a')
        await asyncio.sleep(.08)
        status=broker.flow_status(flow['flow_id'],principal_id='peter',session_binding='a')
        assert status['status']=='expired'
        with pytest.raises(ConnectionError):
            await broker.oauth_callback(state='state-'+flow['flow_id'],code='valid-code',principal_id='peter',session_binding='a')
        await broker.close()
    asyncio.run(scenario())


def test_storage_expiry_survives_process_restart(tmp_path):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://preview.example.com')
        generation=broker._begin('notion','peter')
        storage=_Storage(broker,'peter','notion',generation)
        await storage.set_tokens(OAuthToken(access_token='token',expires_in=60))
        first=storage.expires_at
        reopened=_Storage(broker,'peter','notion',generation)
        await reopened.get_tokens()
        assert reopened.expires_at==first
        await broker.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('url',[
    'http://mcp.notion.com/mcp','https://user:secret@mcp.notion.com/mcp',
    'https://127.0.0.1/mcp','https://mcp.notion.com.evil.example/mcp',
    'https://mcp.notion.com:8443/mcp','https://mcp.notion.com/mcp#fragment'])
def test_provider_discovery_urls_cannot_escape_catalogue(url):
    with pytest.raises(ConnectionError):validate_provider_url(url,{'mcp.notion.com'})


def test_company_state_does_not_inherit_another_company_connection(tmp_path,monkeypatch):
    async def scenario():
        a=MCPConnections(tmp_path/'a',public_origin='https://preview.example.com')
        b=MCPConnections(tmp_path/'b',public_origin='https://preview.example.com')
        fake_transport(a,monkeypatch)
        await a.start('openai-docs',principal_id='same-person',session_binding='a')
        assert next(row for row in b.catalogue('same-person')['connections']if row['id']=='openai-docs')['status']=='disconnected'
        assert a.company_scope!=b.company_scope
        await a.close();await b.close()
    asyncio.run(scenario())


def test_real_sdk_pkce_exchange_refresh_and_secret_redaction(tmp_path,monkeypatch):
    import base64
    import hashlib
    import httpx
    import json
    import time
    from urllib.parse import parse_qs,urlsplit
    import mcp_connections

    async def scenario():
        issued={};counts={'register':0,'code':0,'refresh':0};seen=[]
        async def provider(request):
            seen.append((request.method,request.url.path))
            path=request.url.path
            if path.startswith('/.well-known/oauth-protected-resource'):
                return httpx.Response(200,json={'resource':'https://mcp.notion.com/mcp',
                    'authorization_servers':['https://mcp.notion.com'],'scopes_supported':['default']})
            if path.startswith('/.well-known/oauth-authorization-server'):
                return httpx.Response(200,json={'issuer':'https://mcp.notion.com',
                    'authorization_endpoint':'https://mcp.notion.com/authorize',
                    'token_endpoint':'https://mcp.notion.com/token','registration_endpoint':'https://mcp.notion.com/register',
                    'response_types_supported':['code'],'grant_types_supported':['authorization_code','refresh_token'],
                    'token_endpoint_auth_methods_supported':['none'],'code_challenge_methods_supported':['S256'],
                    'scopes_supported':['default']})
            if path=='/register':
                counts['register']+=1
                data=json.loads(request.content)
                assert data['redirect_uris']==['https://preview.example.com/api/connections/oauth/callback']
                assert data['token_endpoint_auth_method']=='none'
                return httpx.Response(201,json={**data,'client_id':'client-fixture'})
            if path=='/token':
                body=parse_qs(request.content.decode())
                if body['grant_type']==['authorization_code']:
                    counts['code']+=1
                    assert body['code']==['valid-code']
                    challenge=base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                    assert challenge==issued['challenge']
                else:
                    counts['refresh']+=1
                    assert body['refresh_token']==['saved-refresh-private']
                return httpx.Response(200,json={'access_token':'saved-access-private',
                    'refresh_token':'saved-refresh-private','token_type':'Bearer','expires_in':60,'scope':'default'})
            assert path=='/mcp'
            if request.headers.get('authorization')!='Bearer saved-access-private':
                return httpx.Response(401,headers={'WWW-Authenticate':
                    'Bearer resource_metadata="https://mcp.notion.com/.well-known/oauth-protected-resource"'})
            if request.method in ('GET','DELETE'):return httpx.Response(405)
            body=json.loads(request.content)
            if body['method'].startswith('notifications/'):return httpx.Response(202)
            if body['method']=='initialize':
                result={'protocolVersion':body['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'Fixture','version':'1'}}
            elif body['method']=='tools/list':
                result={'tools':[{'name':'notion-fetch','description':'Read page','inputSchema':{'type':'object'}}]}
            elif body['method']=='tools/call':
                result={'content':[{'type':'text','text':'Page content saved-access-private saved-refresh-private'}]}
            else:raise AssertionError(body)
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})
        monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(provider))
        broker=MCPConnections(tmp_path,public_origin='https://preview.example.com')
        flow=await broker.start('notion',principal_id='peter',session_binding='browser')
        assert flow['status']=='awaiting_authorization'
        params=parse_qs(urlsplit(flow['authorization_url']).query)
        assert params['code_challenge_method']==['S256']
        issued['challenge']=params['code_challenge'][0]
        done=await broker.oauth_callback(state=params['state'][0],code='valid-code',principal_id='peter',session_binding='browser')
        assert done['status']=='connected'
        assert counts=={'register':1,'code':1,'refresh':0}
        # New OAuth instance must refresh the already persisted expired token.
        with broker._db() as db:
            db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-100,))
        result=await broker.call('notion','notion-fetch',{},principal_id='peter')
        assert counts['refresh']==1 and counts['register']==1
        assert 'saved-access-private' not in str(result) and 'saved-refresh-private' not in str(result)
        assert '[private credential]' in str(result)
        assert broker.capabilities('peter')[0]['scopes']==['read:pages']
        assert broker.capabilities('alice')==[]
        await broker.close()
    asyncio.run(scenario())


def test_local_anonymous_connection_does_not_invent_oauth_origin(tmp_path,monkeypatch):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='')
        fake_transport(broker,monkeypatch)
        assert (await broker.start('openai-docs',principal_id='peter',session_binding='a'))['status']=='connected'
        with pytest.raises(ConnectionError):await broker.start('notion',principal_id='peter',session_binding='a')
        await broker.close()
    asyncio.run(scenario())


def test_close_interrupts_pending_consent_and_restart_recovery(tmp_path,monkeypatch):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://preview.example.com')
        fake_transport(broker,monkeypatch,oauth=True)
        flow=await broker.start('notion',principal_id='peter',session_binding='a')
        await broker.close()
        assert broker.flow_status(flow['flow_id'],principal_id='peter',session_binding='a')['status']=='interrupted'
        reopened=MCPConnections(tmp_path,public_origin='https://preview.example.com')
        reopened.recover()
        assert next(row for row in reopened.catalogue('peter')['connections']if row['id']=='notion')['status']=='reconnect_required'
        await reopened.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('reference_keyword',['$ref','$dynamicRef','$recursiveRef'])
def test_dns_private_address_and_remote_schema_ref_are_rejected(tmp_path,monkeypatch,reference_keyword):
    import httpx
    import mcp_connections
    async def scenario():
        monkeypatch.setattr(mcp_connections.socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('127.0.0.1',443))])
        transport=mcp_connections._GuardedTransport({'mcp.notion.com'})
        with pytest.raises(ConnectionError):
            await transport.handle_async_request(httpx.Request('GET','https://mcp.notion.com/mcp'))
        await transport.aclose()
        broker=MCPConnections(tmp_path,public_origin='')
        session=Session()
        async def poisoned(cursor=None):
            return ListToolsResult(tools=[Tool(name='search_openai_docs',inputSchema={reference_keyword:'http://127.0.0.1/private'})])
        session.list_tools=poisoned;fake_transport(broker,monkeypatch,session)
        result=await broker.start('openai-docs',principal_id='peter',session_binding='a')
        assert result['status']=='failed'
        await broker.close()
    asyncio.run(scenario())
