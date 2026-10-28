"""Sentry's original package contract through real SDK OAuth and native relay.

All HTTP is intercepted by a synthetic provider. No live account or model calls.
"""
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
from provider_policy import CATALOGUE, provider_read_only
from mcp_connections import MCPConnections, ConnectionError
import mcp_connections
import plugin_catalogue
from native_packages import NativePackageStore
from native_provider_relay import NativeProviderRelay, ProviderRelayError
from native_approvals import NativeApprovals, ApprovalScope
from native_mcp import NativeMCP, NativeMCPScope
# Shared in-process official SDK client and synthetic root lineage fixtures.
sys.path.insert(0, str(Path(__file__).parent))
from test_native_mcp import fixture as native_fixture, client

ENDPOINT='https://mcp.sentry.dev/mcp'
SCOPES=('org:read','project:write','team:write','event:write','alerts:write')
TOKEN='synthetic-sentry-private-access'
READ=types.Tool(name='search_issues', title='Search issues', description='Original Sentry issue search contract.',
    inputSchema={'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False},
    outputSchema={'type':'object','properties':{'id':{'type':'string'}},'required':['id']},
    annotations=types.ToolAnnotations(readOnlyHint=True,openWorldHint=True),
    icons=[types.Icon(src='https://mcp.sentry.dev/icon.svg')],_meta={'original':'Sentry fixture'})
WRITE=types.Tool(name='execute_sentry_tool',description='Execute an available Sentry tool.',
    inputSchema={'type':'object','properties':{'name':{'type':'string'},'arguments':{'type':'object'}},
                 'required':['name','arguments'],'additionalProperties':False},
    annotations=types.ToolAnnotations(readOnlyHint=False),_meta={'nested_contract':'original'})
UNKNOWN=types.Tool(name='new_sentry_action',description='Unreviewed despite its hint.',inputSchema={'type':'object'},
                   annotations=types.ToolAnnotations(readOnlyHint=True))
ARGS={'name':'update_issue','arguments':{'issueId':'TEST-1','status':'resolved'}}
META={'callId':'call-0','threadId':'native-root','x-codex-turn-metadata':{'turn_id':'turn-0'}}


class Remote:
    def __init__(self,mode):
        self.mode=mode;self.registrations=[];self.calls=[];self.challenge=None;self.token_scope=None
        self.refresh_scope=None;self.token_requests=[];self.paths=[];self.entered=asyncio.Event();self.release=None
    async def __call__(self,request):
        assert request.url.host=='mcp.sentry.dev', 'Synthetic transport received an unexpected auth/resource host'
        path=request.url.path;self.paths.append((request.method,path))
        if path.startswith('/.well-known/oauth-protected-resource'):
            return httpx.Response(200,json={'resource':ENDPOINT,'authorization_servers':['https://mcp.sentry.dev'],'scopes_supported':SCOPES})
        if path.startswith('/.well-known/oauth-authorization-server'):
            return httpx.Response(200,json={'issuer':'https://mcp.sentry.dev',
                'authorization_endpoint':'https://mcp.sentry.dev/oauth/authorize','token_endpoint':'https://mcp.sentry.dev/oauth/token',
                'registration_endpoint':'https://mcp.sentry.dev/oauth/register','response_types_supported':['code'],
                'grant_types_supported':['authorization_code','refresh_token'],'scopes_supported':SCOPES,
                'token_endpoint_auth_methods_supported':['client_secret_basic','client_secret_post','none'],
                'code_challenge_methods_supported':['S256'],'client_id_metadata_document_supported':True})
        if path=='/oauth/register':
            body=json.loads(request.content);self.registrations.append(body)
            assert body['token_endpoint_auth_method']=='none'
            assert body['scope']==' '.join(CATALOGUE['sentry']['scopes_by_mode'][self.mode])
            assert body['redirect_uris']==['https://rocoto.example/api/connections/oauth/callback']
            return httpx.Response(201,json={**body,'client_id':'synthetic-sentry-public-client'})
        if path=='/oauth/token':
            body=parse_qs(request.content.decode());self.token_requests.append(body)
            assert not request.headers.get('authorization') and 'client_secret' not in body
            if body['grant_type']==['authorization_code']:
                verifier=body['code_verifier'][0]
                challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                assert challenge==self.challenge and body['code']==['synthetic-code']
            override=self.refresh_scope if body['grant_type']==['refresh_token'] else self.token_scope
            return httpx.Response(200,json={'access_token':TOKEN,'refresh_token':'synthetic-sentry-private-refresh',
                'token_type':'Bearer','expires_in':300,'scope':override if override is not None else ' '.join(CATALOGUE['sentry']['scopes_by_mode'][self.mode])})
        assert str(request.url)==ENDPOINT
        if request.headers.get('authorization')!='Bearer '+TOKEN:
            return httpx.Response(401,headers={'WWW-Authenticate':'Bearer resource_metadata="https://mcp.sentry.dev/.well-known/oauth-protected-resource", scope="'+' '.join(SCOPES)+'"'})
        if request.method in ('GET','DELETE'):return httpx.Response(405)
        body=json.loads(request.content);method=body['method']
        if method.startswith('notifications/'):return httpx.Response(202)
        if method=='initialize':result={'protocolVersion':body['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'Synthetic Sentry','version':'1'}}
        elif method=='tools/list':result={'tools':[tool.model_dump(mode='json',by_alias=True,exclude_none=True) for tool in (READ,WRITE,UNKNOWN)]}
        elif method=='tools/call':
            self.calls.append(body);self.entered.set()
            if self.release:await self.release.wait()
            result={'content':[{'type':'text','text':'Original Sentry result'}],'structuredContent':{'id':'TEST-1'},'_meta':{'receipt':'original'},'isError':False}
        else:raise AssertionError(method)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})


async def connect(tmp_path,monkeypatch,mode='read',*,commissioned=False,token_scope=None):
    remote=Remote(mode);remote.token_scope=token_scope
    monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(remote))
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',write_providers={'sentry'} if commissioned else set())
    flow=await broker.start('sentry',principal_id='alice',session_binding='browser',access_mode=mode)
    assert flow['status']=='awaiting_authorization'
    authorization=parse_qs(urlsplit(flow['authorization_url']).query)
    assert authorization['scope']==[' '.join(CATALOGUE['sentry']['scopes_by_mode'][mode])]
    assert authorization['code_challenge_method']==['S256'] and len(authorization['state'][0])>=16
    remote.challenge=authorization['code_challenge'][0]
    result=await broker.oauth_callback(state=authorization['state'][0],code='synthetic-code',principal_id='alice',session_binding='browser')
    return broker,remote,result


def test_sentry_policy_is_the_original_endpoint_and_disconnected_by_default(tmp_path):
    entry=plugin_catalogue.read_plugin('sentry');policy=CATALOGUE['sentry']
    assert policy['endpoint']==entry['mcp_servers'][0]['url']==ENDPOINT
    assert policy['read_scope_authority']is False
    assert policy['policy_revision']=='2026-10-05-sentry-v1'
    assert CATALOGUE['notion']['policy_revision']==CATALOGUE['linear']['policy_revision']=='2026-10-05-v1'
    assert not {'execute_sentry_tool','update_issue','analyze_issue_with_seer'} & policy['tools']
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    row=next(c for c in broker.catalogue('alice')['connections']if c['id']=='sentry')
    assert row['available'] and row['status']=='disconnected' and row['access_mode']=='read'
    assert row['write_available']is False and row['access_modes']==['read','read_write']
    assert not provider_read_only(policy,{'oauth_scopes':'["org:read"]'})


@pytest.mark.parametrize('mode,commissioned',[('read',False),('read_write',False),('read_write',True)])
def test_actual_sdk_dcr_pkce_scope_clamp_and_complete_inventory(tmp_path,monkeypatch,mode,commissioned):
    async def check():
        broker,remote,result=await connect(tmp_path,monkeypatch,mode,commissioned=commissioned)
        try:
            assert result['status']=='connected' and len(remote.registrations)==1
            listed=await broker.tools('sentry',principal_id='alice')
            expected=(READ,)if mode=='read'else(READ,WRITE,UNKNOWN)
            assert listed['tools']==[t.model_dump(mode='json',by_alias=True,exclude_none=True)for t in expected]
            assert listed['tool_permissions'][READ.name]=='read'
            assert (await broker.call('sentry',READ.name,{'query':'is:unresolved'},principal_id='alice'))['result']['structuredContent']=={'id':'TEST-1'}
            for tool in (WRITE,UNKNOWN):
                with pytest.raises(ConnectionError):await broker.call('sentry',tool.name,ARGS if tool is WRITE else {},principal_id='alice')
            assert len(remote.calls)==1
            if mode=='read_write':
                binding=await broker.execution_binding('sentry',WRITE.name,principal_id='alice')
                assert binding['write_enabled']is commissioned and binding['tool_contract']==listed['tools'][1]
                assert listed['tool_permissions'][WRITE.name]==listed['tool_permissions'][UNKNOWN.name]=='approval_required'
            with pytest.raises(ConnectionError):await broker.tools('sentry',principal_id='bob')
            assert all(TOKEN.encode()not in p.read_bytes()for p in broker.directory.iterdir()if p.is_file())
        finally:await broker.close()
    asyncio.run(check())


@pytest.mark.parametrize('when',['initial','refresh'])
def test_scope_escalation_never_turns_read_into_provider_authority(tmp_path,monkeypatch,when):
    async def check():
        broker,remote,result=await connect(tmp_path,monkeypatch,token_scope='org:read event:write'if when=='initial'else None)
        try:
            if when=='initial':assert result['status']=='failed'
            else:
                assert result['status']=='connected';generation=broker.read_generation('sentry',principal_id='alice')
                remote.refresh_scope='org:read event:write'
                with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-10,))
                with pytest.raises(ConnectionError):await broker.tools('sentry',principal_id='alice')
                assert broker._record('sentry','alice')['generation']>generation
            assert broker._record('sentry','alice')['tokens']is None and not remote.calls
        finally:await broker.close()
    asyncio.run(check())


@asynccontextmanager
async def native(tmp_path,monkeypatch,mode='read_write',*,original_package=None):
    entry=plugin_catalogue.read_plugin('sentry')
    root=tmp_path/'package'
    if original_package is not None:
        import shutil
        shutil.copytree(original_package,root)
    else:
        (root/'.codex-plugin').mkdir(parents=True)
        (root/'.codex-plugin/plugin.json').write_text(json.dumps(entry['manifest']))
        (root/'.mcp.json').write_text(json.dumps({'mcpServers':{'sentry':{'type':'http','url':ENDPOINT}}}))
        (root/'skills').mkdir();(root/'skills/SKILL.md').write_text('---\nname: fixture\ndescription: Synthetic instructions.\n---\nUse the connected provider.\n')
    class Materializer:
        @asynccontextmanager
        async def acquire(self,*args):yield root,{'version':entry['version'],'installed':True,'enabled':True}
    packages=NativePackageStore(tmp_path/'state',materializer=Materializer())
    await packages.install('sentry',principal_id='alice');selection=packages.snapshot(principal_id='alice')
    broker,remote,result=await connect(packages.state,monkeypatch,mode,commissioned=True)
    assert result['status']=='connected'
    relay=NativeProviderRelay(packages,broker,selection,company_id=packages.company_scope,principal_id='alice')
    approvals=NativeApprovals(packages.state);scope=ApprovalScope(packages.company_scope,'alice','root','run:1','native-root')
    transport=relay.configure_writes(approvals,scope,enabled=True,timeout=3)
    transport.transport_factory=lambda binding:httpx.MockTransport(remote)
    agent,_,_=native_fixture();mcp_scope=NativeMCPScope(packages.company_scope,'alice','cafe','root','native-root')
    app=NativeMCP(agent,mcp_scope,company_id=packages.company_scope,provider_relay=relay)
    grant=await app.issue_provider('sentry')
    try:
        async with app.lifespan():yield packages,broker,remote,relay,approvals,scope,agent,app,grant
    finally:await transport.close();await broker.close()


@pytest.mark.parametrize('mode',['read','read_write'])
def test_original_server_name_and_nested_executor_remain_permission_separated(tmp_path,monkeypatch,mode):
    async def check():
        async with native(tmp_path,monkeypatch,mode)as(packages,broker,remote,relay,approvals,scope,agent,app,grant):
            assert relay.server_names==('sentry',)
            async with client(app,grant.token)as session:
                tools=(await session.list_tools()).tools
                assert [t.name for t in tools]==([READ.name]if mode=='read'else[READ.name,WRITE.name,UNKNOWN.name])
                assert tools[0].model_dump(by_alias=True)==READ.model_dump(by_alias=True)
                assert not(await session.call_tool(READ.name,{'query':'error'})).isError
                assert(await session.call_tool(WRITE.name,ARGS,meta=META)).isError
            assert [r['params']['name']for r in remote.calls]==[READ.name]
    asyncio.run(check())


@pytest.mark.parametrize('decision',['allow_once','decline'])
def test_native_approved_nested_action_dispatches_once_and_preserves_original_result(tmp_path,monkeypatch,decision):
    async def check():
        async with native(tmp_path,monkeypatch)as(packages,broker,remote,relay,approvals,scope,agent,app,grant):
            binding=await relay.approval_binding('sentry',WRITE.name)
            assert binding.write_enabled and binding.provider==binding.server_name=='sentry'
            row=approvals.open(scope=scope,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=binding,tool_arguments=ARGS)
            async def deliver(identifier,payload,guard):assert guard()
            await approvals.answer(row['id'],scope=scope,binding=binding,action=decision,deliver=deliver,cancelled=asyncio.Event())
            async with client(app,grant.token)as session:
                result=await session.call_tool(WRITE.name,ARGS,meta=META)
                if decision=='allow_once':
                    assert not result.isError and result.structuredContent=={'id':'TEST-1'} and result.meta=={'receipt':'original'}
                    assert(await session.call_tool(WRITE.name,ARGS,meta=META))==result
                    assert len(remote.calls)==1
                else:assert result.isError and not remote.calls
    asyncio.run(check())


@pytest.mark.parametrize('change',['disconnect','uninstall','wrong_owner'])
def test_native_account_and_package_fences_prevent_effects(tmp_path,monkeypatch,change):
    async def check():
        async with native(tmp_path,monkeypatch)as(packages,broker,remote,relay,approvals,scope,agent,app,grant):
            if change=='disconnect':await broker.disconnect('sentry',principal_id='alice')
            elif change=='uninstall':await packages.uninstall('sentry',principal_id='alice')
            else:
                with pytest.raises(ProviderRelayError):NativeProviderRelay(packages,broker,relay.selection,company_id=packages.company_scope,principal_id='bob')
                return
            async with client(app,grant.token)as session:assert(await session.call_tool(READ.name,{'query':'private'})).isError
            assert not remote.calls
    asyncio.run(check())


def test_expired_account_refresh_uses_published_token_endpoint_without_registering_again(tmp_path,monkeypatch):
    async def check():
        broker,remote,result=await connect(tmp_path,monkeypatch)
        try:
            assert result['status']=='connected'
            generation=broker.read_generation('sentry',principal_id='alice')
            with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-10,))
            assert (await broker.tools('sentry',principal_id='alice'))['tools'][0]['name']==READ.name
            assert remote.token_requests[-1]['grant_type']==['refresh_token']
            assert len(remote.registrations)==1 and ('POST','/token')not in remote.paths
            assert broker.read_generation('sentry',principal_id='alice')==generation
        finally:await broker.close()
    asyncio.run(check())


def test_unreviewed_refresh_destination_is_rejected_before_sending_token(tmp_path,monkeypatch):
    async def check():
        broker,remote,result=await connect(tmp_path,monkeypatch)
        try:
            assert result['status']=='connected'
            before=list(remote.paths)
            with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-10,))
            monkeypatch.setitem(broker._policies,'sentry',{**broker.provider_policy('sentry'),'token_endpoint':'https://unreviewed.example/oauth/token'})
            with pytest.raises(ConnectionError):await broker.tools('sentry',principal_id='alice')
            assert remote.paths==before
        finally:await broker.close()
    asyncio.run(check())


def test_refresh_preserves_discovered_metadata_endpoint(monkeypatch):
    async def check():
        from types import SimpleNamespace
        oauth=object.__new__(mcp_connections._OAuth)
        oauth.context=SimpleNamespace(oauth_metadata=object())
        oauth.refresh_token_endpoint='https://mcp.sentry.dev/oauth/token'
        expected=httpx.Request('POST','https://mcp.sentry.dev/discovered-token',content=b'synthetic')
        async def sdk_refresh(self):return expected
        monkeypatch.setattr(mcp_connections.OAuthClientProvider,'_refresh_token',sdk_refresh)
        assert await oauth._refresh_token()is expected
    asyncio.run(check())


def test_native_stop_drains_accepted_sentry_action_and_suppresses_late_output(tmp_path,monkeypatch):
    async def check():
        async with native(tmp_path,monkeypatch)as(packages,broker,remote,relay,approvals,scope,agent,app,grant):
            binding=await relay.approval_binding('sentry',WRITE.name)
            row=approvals.open(scope=scope,actor_chat_id='root',native_request_id=0,turn_id='turn-0',call_id='call-0',binding=binding,tool_arguments=ARGS)
            async def deliver(identifier,payload,guard):assert guard()
            await approvals.answer(row['id'],scope=scope,binding=binding,action='allow_once',deliver=deliver,cancelled=asyncio.Event())
            remote.release=asyncio.Event()
            async with client(app,grant.token)as session:
                task=asyncio.create_task(session.call_tool(WRITE.name,ARGS,meta=META))
                await asyncio.wait_for(remote.entered.wait(),3)
                agent.active['root']['cancel'].set();approvals.invalidate(scope=scope,reason='stopped')
                await asyncio.sleep(.02);assert not task.done() and app._calls
                remote.release.set()
                assert (await task).isError and not app._calls
            assert len(remote.calls)==1 and approvals.get(row['id'],scope=scope)['status']=='succeeded'
    asyncio.run(check())
