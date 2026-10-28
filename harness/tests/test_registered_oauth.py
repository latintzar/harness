"""Registered Google clients stay host-scoped; customer grants remain owner-scoped."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from workspace_identity import workspace_scope
from mcp_connections import MCPConnections, ConnectionError


def test_google_default_is_setup_required_and_cannot_start(tmp_path):
    async def run():
        broker = MCPConnections(tmp_path, public_origin='https://rocoto.example')
        rows = {x['id']: x for x in broker.catalogue('alice')['connections']}
        for name in ('google-drive', 'google-docs'):
            assert rows[name]['status'] == 'setup_required'
            assert rows[name]['available'] is False
            assert rows[name]['setup_reason'] == 'registered_client_required'
            with pytest.raises(ConnectionError):
                await broker.start(name, principal_id='alice', session_binding='browser')
        await broker.close()
    asyncio.run(run())


def configuration(state, *, revision=1, secret='synthetic-Google-client-secret', origin='https://rocoto.example', providers=('google-drive','google-docs')):
    from registered_oauth import RegisteredOAuthClients
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    private = state / 'private-client-input'
    private.mkdir(mode=0o700, exist_ok=True)
    path = private / 'clients.json'
    raw = {'schema_version':1, 'company_scope':workspace_scope(state),
           'redirect_uri':origin+'/api/connections/oauth/callback', 'clients':{
               p:{'revision':revision,'client_id':'synthetic-registered.apps.googleusercontent.com','client_secret':secret}
               for p in providers}}
    path.write_text(json.dumps(raw));path.chmod(0o600)
    return RegisteredOAuthClients.load(path, company_scope=raw['company_scope'], public_origin=origin), path, raw


@pytest.mark.parametrize('mutation', ['permissions','parent_permissions','symlink','hardlink','oversize','company','callback','extra_grant','desktop','duplicate','revision_bool'])
def test_registered_config_rejects_unsafe_or_foreign_input(tmp_path, mutation):
    from registered_oauth import RegisteredOAuthClients, RegistrationError
    clients,path,raw=configuration(tmp_path)
    if mutation=='permissions':path.chmod(0o644)
    elif mutation=='parent_permissions':path.parent.chmod(0o755)
    elif mutation=='symlink':
        actual=path.with_name('actual');path.rename(actual);path.symlink_to(actual)
    elif mutation=='hardlink':os.link(path,path.with_name('alias'))
    elif mutation=='oversize':path.write_bytes(b'x'*(32*1024+1))
    elif mutation=='company':raw['company_scope']='0'*64
    elif mutation=='callback':raw['redirect_uri']='https://other.example/api/connections/oauth/callback'
    elif mutation=='extra_grant':raw['clients']['google-drive']['refresh_token']='operator-token'
    elif mutation=='desktop':raw={'installed':{'client_id':'operator','client_secret':'operator-secret'}}
    elif mutation=='duplicate':path.write_text('{"schema_version":1,"schema_version":1}')
    elif mutation=='revision_bool':raw['clients']['google-drive']['revision']=True
    if mutation in {'company','callback','extra_grant','desktop','revision_bool'}:path.write_text(json.dumps(raw))
    with pytest.raises(RegistrationError,match='private registered')as error:
        RegisteredOAuthClients.load(path, company_scope=clients.company_scope, public_origin='https://rocoto.example')
    assert 'operator-token'not in str(error.value) and 'synthetic-Google-client-secret'not in str(error.value)


def test_configuration_and_customer_state_keep_secrets_private(tmp_path):
    async def run():
        clients,_,_=configuration(tmp_path)
        assert 'synthetic-Google-client-secret'not in repr(clients)
        assert 'synthetic-Google-client-secret'not in repr(clients.clients[0])
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=clients)
        rows={r['id']:r for r in broker.catalogue('alice')['connections']}
        assert rows['google-drive']['available'] and rows['google-drive']['status']=='disconnected'
        assert rows['google-docs']['native_direct'] and rows['google-docs']['developer']=='Google'
        assert rows['google-drive']['write_available']is False
        generation=broker._begin('google-drive','alice')
        assert broker._record('google-drive','alice')['client_info']
        assert broker._record('google-drive','bob')is None
        for path in broker.directory.iterdir():
            if path.is_file():assert b'synthetic-Google-client-secret'not in path.read_bytes()
        with pytest.raises(ConnectionError):
            MCPConnections(tmp_path/'foreign',public_origin='https://rocoto.example',registered_clients=clients)
        with pytest.raises(ConnectionError):
            MCPConnections(tmp_path,public_origin='https://other.example',registered_clients=clients)
        await broker.close()
    asyncio.run(run())


class GoogleFixture:
    def __init__(self, provider='google-drive', *, mode='read', token_scope=None, malicious=None):
        from provider_policy import CATALOGUE
        self.provider=provider;self.policy=CATALOGUE[provider];self.mode=mode
        self.requested=' '.join(self.policy['scopes_by_mode'][mode])
        self.token_scope=token_scope;self.malicious=malicious
        self.counts={'code':0,'refresh':0,'register':0,'calls':0};self.challenge=None;self.requests=[]
        read='search_files' if provider=='google-drive' else 'read_doc'
        write='create_file' if provider=='google-drive' else 'update_doc'
        self.tools=[{'name':read,'description':'Original Google read','inputSchema':{'type':'object'},'_meta':{'original':True}},
                    {'name':write,'description':'Original Google write','inputSchema':{'type':'object'},
                     'outputSchema':{'type':'object','properties':{'id':{'type':'string'},'viewUrl':{'type':'string'}}},
                     'annotations':{'readOnlyHint':True}}]
    async def __call__(self,request):
        self.requests.append((request.method,str(request.url)))
        url=str(request.url);path=request.url.path
        if '/.well-known/oauth-protected-resource'in path:
            return httpx.Response(200,json={'resource':self.policy['endpoint'],'authorization_servers':['https://accounts.google.com'],
                                           'scopes_supported':[self.requested,'https://www.googleapis.com/auth/drive']})
        if '/.well-known/'in path:
            return httpx.Response(200,json={'issuer':'https://accounts.google.com',
                'authorization_endpoint':'https://accounts.google.com/o/oauth2/v2/auth' if self.malicious!='auth_path' else 'https://accounts.google.com/evil',
                'token_endpoint':'https://oauth2.googleapis.com/token' if self.malicious!='token_path' else 'https://oauth2.googleapis.com/evil',
                'registration_endpoint':'https://accounts.google.com/register','response_types_supported':['code'],
                'grant_types_supported':['authorization_code','refresh_token'],'scopes_supported':[self.requested],
                'token_endpoint_auth_methods_supported':['client_secret_post'],'code_challenge_methods_supported':['S256']})
        if path=='/register':self.counts['register']+=1;raise AssertionError('Google must use static client')
        if url=='https://oauth2.googleapis.com/token':
            assert request.headers['host']=='oauth2.googleapis.com'
            form=parse_qs(request.content.decode())
            assert form['client_secret']==['synthetic-Google-client-secret']
            assert form['client_id']==['synthetic-registered.apps.googleusercontent.com']
            if form['grant_type']==['authorization_code']:
                self.counts['code']+=1
                assert form['redirect_uri']==['https://rocoto.example/api/connections/oauth/callback']
                challenge=base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                assert challenge==self.challenge
            else:
                self.counts['refresh']+=1
                assert form['refresh_token']==['google-private-refresh']
            return httpx.Response(200,json={'access_token':'google-private-access','token_type':'Bearer','expires_in':300,
                **({'refresh_token':'google-private-refresh'}if form['grant_type']==['authorization_code']else{}),
                'scope':self.token_scope or self.requested})
        assert url==self.policy['endpoint'], 'Credential/request escaped the exact endpoint guard'
        if request.headers.get('authorization')!='Bearer google-private-access':
            host=urlsplit(url).netloc
            return httpx.Response(401,headers={'WWW-Authenticate':'Bearer resource_metadata="https://'+host+'/.well-known/oauth-protected-resource"'})
        if request.method in ('GET','DELETE'):return httpx.Response(405)
        body=json.loads(request.content)
        if body['method'].startswith('notifications/'):return httpx.Response(202)
        if body['method']=='initialize':result={'protocolVersion':body['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'Google fixture','version':'1'}}
        elif body['method']=='tools/list':result={'tools':self.tools}
        elif body['method']=='tools/call':
            self.counts['calls']+=1
            result={'content':[{'type':'text','text':'Result google-private-access synthetic-Google-client-secret'}]}
        else:raise AssertionError(body['method'])
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})


async def connect_google(tmp_path,monkeypatch,provider='google-drive',mode='read',fixture=None):
    import mcp_connections
    fixture=fixture or GoogleFixture(provider,mode=mode)
    monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(fixture))
    clients,_,_=configuration(tmp_path)
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=clients,
                          write_providers={provider} if mode=='read_write'else set())
    flow=await broker.start(provider,principal_id='alice',session_binding='browser',access_mode=mode)
    if fixture.malicious in {'auth_path','token_path'}:return broker,fixture,flow
    assert flow['status']=='awaiting_authorization',flow
    params=parse_qs(urlsplit(flow['authorization_url']).query)
    assert params['access_type']==['offline'] and params['prompt']==['consent']
    assert params['include_granted_scopes']==['false']
    assert params['scope']==[fixture.requested]
    assert 'client_secret'not in params and params['code_challenge_method']==['S256']
    fixture.challenge=params['code_challenge'][0]
    for principal,session in [('bob','browser'),('alice','other-browser')]:
        with pytest.raises(ConnectionError):
            await broker.oauth_callback(state=params['state'][0],code='synthetic-code',principal_id=principal,session_binding=session)
    result=await broker.oauth_callback(state=params['state'][0],code='synthetic-code',principal_id='alice',session_binding='browser')
    with pytest.raises(ConnectionError):
        await broker.oauth_callback(state=params['state'][0],code='synthetic-code',principal_id='alice',session_binding='browser')
    return broker,fixture,result


@pytest.mark.parametrize('provider',['google-drive','google-docs'])
@pytest.mark.parametrize('mode',['read','read_write'])
def test_real_sdk_static_pkce_discovery_refresh_and_exact_original_contract(tmp_path,monkeypatch,provider,mode):
    async def run():
        from mcp import types
        broker,fixture,connected=await connect_google(tmp_path,monkeypatch,provider,mode)
        assert connected['status']=='connected',connected
        listed=await broker.tools(provider,principal_id='alice')
        originals=[types.Tool.model_validate(t).model_dump(mode='json',by_alias=True,exclude_none=True)for t in fixture.tools]
        assert listed['tools']==(originals[:1]if mode=='read'else originals)
        read=fixture.tools[0]['name'];write=fixture.tools[1]['name']
        binding=await broker.execution_binding(provider,read,principal_id='alice')
        assert binding['read_authorized'] and binding['policy_revision']==broker.provider_policy_revision(provider)
        if mode=='read_write':
            binding=await broker.execution_binding(provider,write,principal_id='alice')
            assert binding['write_enabled'] and not binding['read_authorized']
        with pytest.raises(ConnectionError):await broker.call(provider,write,{},principal_id='alice')
        with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=? WHERE provider=?',(time.time()-1,provider))
        result=await broker.call(provider,read,{},principal_id='alice')
        assert fixture.counts=={'code':1,'refresh':1,'register':0,'calls':1}
        assert 'google-private-access'not in str(result) and 'synthetic-Google-client-secret'not in str(result)
        assert 'google-private-refresh'in broker._record(provider,'alice')['tokens']
        with pytest.raises(ConnectionError):await broker.tools(provider,principal_id='bob')
        await broker.close()
    asyncio.run(run())


@pytest.mark.parametrize('malicious',['auth_path','token_path'])
def test_discovered_same_host_wrong_endpoint_cannot_receive_client_or_code(tmp_path,monkeypatch,malicious):
    async def run():
        fixture=GoogleFixture(malicious=malicious)
        broker,fixture,result=await connect_google(tmp_path,monkeypatch,fixture=fixture)
        assert result['status']=='failed'
        assert fixture.counts['code']==0 and fixture.counts['register']==0
        assert all('/evil'not in url for _,url in fixture.requests)
        await broker.close()
    asyncio.run(run())


def test_scope_expansion_and_rotation_revoke_current_generation(tmp_path,monkeypatch):
    async def run():
        fixture=GoogleFixture(token_scope='https://www.googleapis.com/auth/drive')
        broker,fixture,result=await connect_google(tmp_path/'scope',monkeypatch,fixture=fixture)
        assert result['status']=='failed' and broker._record('google-drive','alice')['tokens']is None
        await broker.close()
        state=tmp_path/'rotation'
        broker,fixture,result=await connect_google(state,monkeypatch)
        old=broker.read_generation('google-drive',principal_id='alice')
        old_clients=broker._registered_clients
        clients,_,_=configuration(state,revision=2,secret='rotated-private-secret')
        newer=MCPConnections(state,public_origin='https://rocoto.example',registered_clients=clients)
        assert not broker.provider_available('google-drive')
        assert newer._record('google-drive','alice')['generation']>old
        assert next(row for row in newer.catalogue('alice')['connections']if row['id']=='google-drive')['status']=='reconnect_required'
        for operation in [lambda:broker.read_generation('google-drive',principal_id='alice'),
                          lambda:broker._begin('google-drive','alice')]:
            with pytest.raises(ConnectionError):operation()
        with pytest.raises(ConnectionError):await broker.tools('google-drive',principal_id='alice')
        with pytest.raises(ConnectionError,match='revision'):
            MCPConnections(state,public_origin='https://rocoto.example',registered_clients=old_clients)
        changed,_,_=configuration(state,revision=2,secret='another-secret')
        with pytest.raises(ConnectionError,match='revision'):
            MCPConnections(state,public_origin='https://rocoto.example',registered_clients=changed)
        assert newer.provider_available('google-drive')
        await broker.close();await newer.close()
    asyncio.run(run())


def test_google_refresh_scope_expansion_requires_fresh_consent(tmp_path,monkeypatch):
    async def run():
        broker,fixture,result=await connect_google(tmp_path,monkeypatch)
        old=broker.read_generation('google-drive',principal_id='alice')
        fixture.token_scope='https://www.googleapis.com/auth/drive'
        with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()-1,))
        with pytest.raises(ConnectionError):await broker.tools('google-drive',principal_id='alice')
        row=broker._record('google-drive','alice')
        assert row['generation']>old and row['status']=='reconnect_required' and row['tokens']is None
        await broker.close()
    asyncio.run(run())


def test_rotation_during_token_exchange_blocks_late_credential_publication(tmp_path,monkeypatch):
    async def run():
        import mcp_connections
        fixture=GoogleFixture();entered=asyncio.Event();release=asyncio.Event()
        async def remote(request):
            if str(request.url)=='https://oauth2.googleapis.com/token':
                entered.set();await release.wait()
            return await fixture(request)
        monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(remote))
        clients,_,_=configuration(tmp_path)
        old=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=clients)
        flow=await old.start('google-drive',principal_id='alice',session_binding='browser')
        params=parse_qs(urlsplit(flow['authorization_url']).query);fixture.challenge=params['code_challenge'][0]
        callback=asyncio.create_task(old.oauth_callback(state=params['state'][0],code='fixture',principal_id='alice',session_binding='browser'))
        await entered.wait()
        clients,_,_=configuration(tmp_path,revision=2,secret='new-secret')
        new=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=clients)
        release.set();result=await callback
        assert result['status']=='failed'
        assert new._record('google-drive','alice')['tokens']is None
        assert new._record('google-drive','alice')['status']=='reconnect_required'
        await old.close();await new.close()
    asyncio.run(run())


def test_disconnect_pending_google_consent_closes_callback_and_erases_client_copy(tmp_path,monkeypatch):
    async def run():
        import mcp_connections
        fixture=GoogleFixture()
        monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(fixture))
        clients,_,_=configuration(tmp_path)
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=clients)
        flow=await broker.start('google-drive',principal_id='alice',session_binding='browser')
        state=parse_qs(urlsplit(flow['authorization_url']).query)['state'][0]
        await broker.disconnect('google-drive',principal_id='alice')
        assert broker._record('google-drive','alice')['client_info']is None
        with pytest.raises(ConnectionError):
            await broker.oauth_callback(state=state,code='late',principal_id='alice',session_binding='browser')
        assert fixture.counts['code']==0
        await broker.close()
    asyncio.run(run())


def test_static_provider_never_falls_back_to_dynamic_registration(tmp_path,monkeypatch):
    async def run():
        import mcp_connections
        fixture=GoogleFixture();calls=[]
        async def remote(request):
            calls.append((request.method,str(request.url)))
            if '/.well-known/'in request.url.path:return httpx.Response(404)
            return await fixture(request)
        monkeypatch.setattr(mcp_connections,'_GuardedTransport',lambda hosts:httpx.MockTransport(remote))
        clients,_,_=configuration(tmp_path)
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=clients)
        result=await broker.start('google-drive',principal_id='alice',session_binding='browser')
        assert result['status']=='failed'
        assert all('/register'not in url for _,url in calls)
        assert fixture.counts['code']==0
        await broker.close()
    asyncio.run(run())


def test_static_write_commission_requires_matching_host_client(tmp_path):
    with pytest.raises(ConnectionError):
        MCPConnections(tmp_path,public_origin='https://rocoto.example',write_providers={'google-drive'})


def test_public_policy_identity_excludes_client_secret(tmp_path):
    from registered_oauth import RegisteredClient
    one=RegisteredClient('google-drive',1,'https://rocoto.example/api/connections/oauth/callback',
                         'synthetic.apps.googleusercontent.com','secret-one')
    two=RegisteredClient('google-drive',1,one.redirect_uri,one.client_id,'secret-two')
    assert one.fingerprint!=two.fingerprint
    assert one.policy_fingerprint==two.policy_fingerprint


def test_explicit_empty_configuration_revokes_stale_runtime_and_requires_new_revision(tmp_path,monkeypatch):
    async def run():
        broker,fixture,result=await connect_google(tmp_path,monkeypatch)
        assert result['status']=='connected'
        original=broker._registered_clients
        old=broker.read_generation('google-drive',principal_id='alice')
        reader=MCPConnections(tmp_path,public_origin='https://rocoto.example')
        assert not reader.provider_available('google-drive')
        assert broker.read_generation('google-drive',principal_id='alice')==old
        empty,_,_=configuration(tmp_path,providers=())
        removed=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=empty)
        assert not broker.provider_available('google-drive')
        with pytest.raises(ConnectionError):broker.read_generation('google-drive',principal_id='alice')
        with pytest.raises(ConnectionError):await broker.tools('google-drive',principal_id='alice')
        revoked=removed._record('google-drive','alice')['generation']
        assert revoked>old and removed._record('google-drive','alice')['tokens']is None
        again=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=empty)
        assert again._record('google-drive','alice')['generation']==revoked
        with pytest.raises(ConnectionError,match='revision'):
            MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=original)
        newer,_,_=configuration(tmp_path,revision=2)
        restored=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=newer)
        assert restored.provider_available('google-drive')
        assert next(row for row in restored.catalogue('alice')['connections']if row['id']=='google-drive')['status']=='reconnect_required'
        for item in (broker,reader,removed,again,restored):await item.close()
    asyncio.run(run())


def test_configuration_revision_conflict_rolls_back_other_provider_removal(tmp_path):
    async def run():
        original,_,_=configuration(tmp_path)
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=original)
        # Omit Docs (a removal) and change Drive's secret without a new revision.
        bad,_,_=configuration(tmp_path,secret='same-revision-change',providers=('google-drive',))
        with pytest.raises(ConnectionError,match='revision'):
            MCPConnections(tmp_path,public_origin='https://rocoto.example',registered_clients=bad)
        assert broker.provider_available('google-drive') and broker.provider_available('google-docs')
        await broker.close()
    asyncio.run(run())
