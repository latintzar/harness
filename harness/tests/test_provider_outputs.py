"""Provider results remain exact, inert, owner-bound and independently revocable."""
import asyncio
import copy
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from test_native_packages import package,snapshot
from chat_files import ChatFiles
from provider_outputs import ProviderOutputs,resources,external_url,MAX_TEXT_BYTES,MAX_RESOURCES

@pytest.fixture
def anyio_backend():return 'asyncio'

LINK={'type':'resource_link','uri':'https://www.notion.so/workspace/report','name':'Service report','mimeType':'text/markdown'}
TEXT={'type':'resource','resource':{'uri':'https://www.notion.so/workspace/report','name':'Service report','mimeType':'text/markdown','text':'# Original\n![tracking](https://tracking.example/image)\n<script>bad()</script>'}}
PROVENANCE={'provider':'notion','plugin_id':'notion','server_name':'notion','package_sha256':'a'*64,'package_generation':1,'connection_generation':1}
SCOPE={'owner_principal_id':'alice','chat_id':'chat-a','dataset':'cafe'}

def saved(store,resource=None,**kwargs):
    return store.import_provider_resource(resource or resources({'content':[TEXT]})[0],provider='notion',provider_name='Notion',publication_key='b'*64,provenance=PROVENANCE,guard=lambda:True,**{**SCOPE,**kwargs})

class Relay:
    def __init__(self):self.value=copy.deepcopy(PROVENANCE);self.store=SimpleNamespace(entries={'notion':{'name':'Notion'}});self.before=None
    def display_name(self,server):return self.store.entries[server]['name']
    @asynccontextmanager
    async def artifact_admission(self,server,expected):
        def current():return self.value==expected
        if not current():raise ValueError("revoked")
        yield current
        if not current():raise ValueError("revoked")
    async def provenance(self,server):
        assert server=='notion'
        if self.before:await self.before()
        if self.value is None:raise ValueError('disconnected')
        return copy.deepcopy(self.value)

def params(status='completed',*,identity='item-1',thread='native-root',content=None):
    return {'threadId':thread,'item':{'type':'mcpToolCall','id':identity,'server':'notion','tool':'fetch','status':status,'result':{'content':content or [TEXT]}}}

def projector(tmp_path,active=None):
    store=ChatFiles(tmp_path);relay=Relay();active=active if active is not None else [True]
    return store,relay,ProviderOutputs(store,relay,owner='alice',root_chat_id='chat-a',dataset='cafe',company_id='company',current=lambda:active[0])


def test_standard_resources_preserve_original_text_and_ignore_untyped_urls():
    content=[{'type':'text','text':json.dumps({'url':LINK['uri']})},LINK,TEXT,TEXT,{'type':'image','data':'AA==','mimeType':'image/png'}]
    output=resources({'content':content,'structuredContent':{'url':'https://docs.google.com/document/d/123'}})
    assert len(output)==2 and output[0]['content'] is None
    assert output[1]['content']==TEXT['resource']['text'] and output[1]['media_type']=='text/markdown'
    assert output[0]['title']=='Service report' and output[0]['source_url']==LINK['uri']
    assert resources({'content':[LINK],'isError':True})==[]
    assert resources({'content':[{'type':'resource','resource':{'uri':'file:///tmp/report','text':'exact local snapshot'}}]})[0]['source_url'] is None
    assert resources({'content':[{'type':'resource','resource':{'uri':'file:///tmp/image','blob':'AA=='}}]})==[]

@pytest.mark.parametrize('uri',['http://www.notion.so/a','https://user:secret@www.notion.so/a','javascript:alert(1)','data:text/html,x','file:///etc/passwd','https://127.0.0.1/a','https://0x7f.0.0.1/a','https://127.1/a','https://[::1]/a','https://localhost/a','https://service.internal/a','https://thing.local/a','https://bad.example:8443/a','https://foo.example\\evil/a',' https://foo.example/a','https://foo.example/\nsecret'])
def test_unsafe_link_only_resources_are_not_published(uri):
    assert external_url(uri) is None
    assert resources({'content':[{**LINK,'uri':uri}]})==[]


def test_resource_limits_keep_links_useful_and_text_bounded():
    big={**TEXT['resource'],'text':'x'*(MAX_TEXT_BYTES+1)}
    assert resources({'content':[{'type':'resource','resource':big}]})[0]['content'] is None
    assert len(resources({'content':[{**LINK,'uri':LINK['uri']+str(i)} for i in range(100)]}))==MAX_RESOURCES
    assert resources({'content':[LINK],'structuredContent':{'oversized':'x'*(4*1024*1024)}})==[]
    for text in ['\x00invalid','\ud800']:
        output=resources({'content':[{'type':'resource','resource':{**big,'text':text}}]})
        assert output==[] or output[0]['content'] is None


# Field names from Google's published create_file File output schema. Synthetic
# IDs/content only; an actual provider write is a separate commissioning proof.
# https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/create_file
GOOGLE_FILE={'id':'synthetic_document_1','title':'Service report','parentId':'folder_1',
    'mimeType':'application/vnd.google-apps.document','description':'Service summary',
    'contentSnippet':'A partial excerpt, not the complete document.',
    'viewUrl':'https://docs.google.com/document/d/synthetic_document_1/edit','owner':'synthetic@example.com'}


@pytest.mark.parametrize('envelope',['structured','text','resource'])
def test_google_create_file_original_file_reference_is_a_link_only_card(envelope):
    result=({'content':[],'structuredContent':GOOGLE_FILE} if envelope=='structured' else
        {'content':[{'type':'text','text':json.dumps(GOOGLE_FILE)}]} if envelope=='text' else
        {'content':[{'type':'resource_link','uri':GOOGLE_FILE['viewUrl'],'name':GOOGLE_FILE['title'],
            'mimeType':GOOGLE_FILE['mimeType']}],'structuredContent':GOOGLE_FILE})
    before=copy.deepcopy(result)
    selected=resources(result,provider='google-drive',tool='create_file')
    assert len(selected)==1 and selected[0]['source_url']==GOOGLE_FILE['viewUrl']
    assert selected[0]['title']=='Service report' and selected[0]['content'] is None
    assert selected[0]['media_type']=='application/vnd.google-apps.document'
    assert result==before


@pytest.mark.parametrize('change',[{'id':'different_id'}, {'viewUrl':'https://example.com/document/d/synthetic_document_1/edit'},
    {'viewUrl':'https://docs.google.com.evil.example/document/d/synthetic_document_1/edit'},
    {'viewUrl':'http://docs.google.com/document/d/synthetic_document_1/edit'},
    {'viewUrl':'https://docs.google.com/document/d/other_id/edit'},
    {'viewUrl':'https://docs.google.com/document/d/synthetic_document_1/../../secret'},
    {'viewUrl':None}, {'id':'../secret'}])
def test_google_file_reference_requires_bound_identity_and_exact_provider_host(change):
    result={'content':[],'structuredContent':{**GOOGLE_FILE,**change}}
    assert resources(result,provider='google-drive',tool='create_file')==[]


def test_typed_google_output_cannot_supply_its_own_provider_or_tool_authority():
    result={'content':[],'structuredContent':{**GOOGLE_FILE,'provider':'google-drive','tool':'create_file'}}
    assert resources(result)==[]
    assert resources(result,provider='notion',tool='create_file')==[]
    assert resources(result,provider='google-drive',tool='read_file_content')==[]
    assert resources({**result,'isError':True},provider='google-drive',tool='create_file')==[]
    assert resources({**result,'structuredContent':{**GOOGLE_FILE,'oversized':'x'*(4*1024*1024)}},
        provider='google-drive',tool='create_file')==[]


@pytest.mark.anyio
async def test_direct_google_creation_persists_real_link_with_host_provenance_and_owner(tmp_path):
    # The projector seam must accept direct provenance without any package store.
    class DirectRelay:
        value={'provider':'google-drive','server_name':'rocoto_google_drive','authority_kind':'direct_provider',
            'policy_revision':'google-fixture-v1','policy_sha256':'a'*64,'connection_generation':1}
        async def provenance(self,server):assert server=='rocoto_google_drive';return dict(self.value)
        def display_name(self,server):return 'Google Drive'
        @asynccontextmanager
        async def artifact_admission(self,server,expected):
            assert expected==self.value
            yield lambda:True
    files=ChatFiles(tmp_path);relay=DirectRelay()
    output=ProviderOutputs(files,relay,owner='alice',root_chat_id='chat-a',dataset='cafe',company_id='company',current=lambda:True)
    call={'threadId':'verified-root','item':{'type':'mcpToolCall','id':'native-file-call',
        'server':'rocoto_google_drive','tool':'create_file','status':'completed',
        'result':{'content':[],'structuredContent':dict(GOOGLE_FILE)}}}
    await output.event('item/started',call,actor_chat_id='chat-a')
    events=await output.event('item/completed',call,actor_chat_id='chat-a')
    assert len(events)==1
    artifact=events[0]['artifact']
    assert artifact['provider_name']=='Google Drive' and artifact['source_url']==GOOGLE_FILE['viewUrl']
    assert artifact['download_url'] is None and artifact['kind']=='provider_resource'
    assert files.content(artifact['id'],**SCOPE)['content'] is None
    assert files.list(owner_principal_id='bob')==[]
    with files.connect() as db:
        provenance=json.loads(db.execute('SELECT provenance FROM native_publications').fetchone()[0])
        assert provenance['policy_sha256']=='a'*64 and 'package_sha256' not in provenance


def test_scoped_store_replay_provenance_original_preview_and_restart(tmp_path):
    store=ChatFiles(tmp_path);resource=resources({'content':[TEXT]})[0];resource.update(name='report.csv',title='report.csv')
    record=saved(store,resource)
    assert record['kind']=='provider_resource' and record['download_url'] is None and record['size_bytes'] is None
    assert record['snapshot_sha256']==hashlib.sha256(resource['content'].encode()).hexdigest()
    assert saved(store,resource)==record
    reopened=ChatFiles(tmp_path)
    assert reopened.get(record['id'],**SCOPE)==record
    assert reopened.content(record['id'],**SCOPE)['content']==resource['content']
    with reopened.connect() as db:
        assert db.execute('SELECT rendered FROM files').fetchone()==(None,)
        assert json.loads(db.execute('SELECT provenance FROM native_publications').fetchone()[0])==PROVENANCE
    assert 'package_generation' not in record and 'owner' not in record
    for scope in [{'owner_principal_id':'bob'},{'chat_id':'other'},{'dataset':'warehouse'}]:
        with pytest.raises(ValueError):reopened.content(record['id'],**{**SCOPE,**scope})
    with pytest.raises(ValueError):saved(store,resource,owner_principal_id='bob')
    with pytest.raises(ValueError):saved(store,{**resource,'content':'replacement'})
    with pytest.raises(ValueError,match='provider'):store.download(record['id'],**SCOPE)
    assert reopened.list(owner_principal_id='bob')==[]


def test_link_only_content_and_company_quota_are_truthful(tmp_path,monkeypatch):
    import chat_files
    store=ChatFiles(tmp_path);record=saved(store,resources({'content':[LINK]})[0])
    assert store.content(record['id'],**SCOPE)['content'] is None
    assert record['copyable'] is False and record['snapshot_sha256'] is None
    monkeypatch.setattr(chat_files,'MAX_COMPANY_FILES',1)
    with pytest.raises(ValueError,match='allowance'):
        store.import_provider_resource(resources({'content':[LINK]})[0],provider='notion',provider_name='Notion',publication_key='c'*64,provenance=PROVENANCE,guard=lambda:True,**SCOPE)


@pytest.mark.anyio
async def test_projector_requires_start_and_matches_host_provenance(tmp_path):
    store,relay,projection=projector(tmp_path)
    assert await projection.event('item/completed',params(),actor_chat_id='chat-a')==[]
    assert await projection.event('item/started',params('inProgress'),actor_chat_id='chat-a')==[]
    events=await projection.event('item/completed',params(),actor_chat_id='chat-a')
    assert len(events)==1 and events[0]['type']=='artifact'
    record=events[0]['artifact'];assert record['provider_name']=='Notion'
    assert await projection.event('item/completed',params(),actor_chat_id='chat-a')==[]
    # Exact replay across projection restart deduplicates its durable source identity.
    again=ProviderOutputs(store,relay,owner='alice',root_chat_id='chat-a',dataset='cafe',company_id='company',current=lambda:True)
    await again.event('item/started',params('inProgress'),actor_chat_id='chat-a')
    assert (await again.event('item/completed',params(),actor_chat_id='chat-a'))[0]['artifact']==record
    assert len(store.list(owner_principal_id='alice'))==1


@pytest.mark.anyio
@pytest.mark.parametrize('change',['disconnect','connection','package','actor','tool','server','status','error','cancel'])
async def test_late_results_from_revoked_or_mismatched_calls_stay_out(tmp_path,change):
    active=[True];store,relay,projection=projector(tmp_path,active)
    await projection.event('item/started',params('inProgress'),actor_chat_id='chat-a')
    end=params();actor='chat-a'
    if change=='disconnect':relay.value=None
    elif change=='connection':relay.value['connection_generation']+=1
    elif change=='package':relay.value['package_generation']+=1
    elif change=='actor':actor='other'
    elif change in {'tool','server'}:end['item'][change]='other'
    elif change=='status':end['item']['status']='failed'
    elif change=='error':end['item']['error']={'message':'failed'}
    elif change=='cancel':active[0]=False
    assert await projection.event('item/completed',end,actor_chat_id=actor)==[]
    assert store.list(owner_principal_id='alice')==[]


@pytest.mark.anyio
async def test_stop_during_provenance_and_storage_guard_suppresses_result(tmp_path,monkeypatch):
    active=[True];store,relay,projection=projector(tmp_path,active)
    await projection.event('item/started',params('inProgress'),actor_chat_id='chat-a')
    async def stop():active[0]=False
    relay.before=stop
    assert await projection.event('item/completed',params(),actor_chat_id='chat-a')==[]
    assert store.list(owner_principal_id='alice')==[]
    active[0]=True;relay.before=None
    await projection.event('item/started',params('inProgress'),actor_chat_id='chat-a')
    real=store.import_provider_resource
    def race(*args,**kwargs):
        active[0]=False
        return real(*args,**kwargs)
    monkeypatch.setattr(store,'import_provider_resource',race)
    assert await projection.event('item/completed',params(),actor_chat_id='chat-a')==[]
    assert store.list(owner_principal_id='alice')==[]


@pytest.mark.anyio
async def test_actual_native_tree_routes_resources_to_admitted_root_or_child(tmp_path):
    from test_isolated_turn import tree_fixture,event
    agent,run,connection,tree=tree_fixture(tmp_path)
    relay=Relay();tree.provider_outputs=ProviderOutputs(agent._files(),relay,owner='alice',root_chat_id='chat',dataset='cafe',company_id='company',current=lambda:not run['cancel'].is_set())
    connection.child('child')
    for thread in ['native-root','child','foreign']:
        start=params('inProgress',identity=thread,thread=thread);end=params(identity=thread,thread=thread)
        await tree.event({'method':'item/started','params':start})
        output=await tree.event({'method':'item/completed','params':end})
        if thread=='native-root':
            artifact=next(e['artifact'] for e in output if e['type']=='artifact')
            assert artifact['chat_id']=='chat'
        elif thread=='child':
            child=await tree.admit('child');assert not any(e['type']=='artifact' for e in output)
            assert agent.children[child]['messages'][-1]['artifacts'][0]['chat_id']==child
        else:assert output==[]
    assert len(agent._files().list(owner_principal_id='alice'))==2


def test_resource_http_uses_existing_owner_observer_and_private_cache(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_server_access import fixture_app
    from test_workflow_routes import login,HEADERS
    app=fixture_app(tmp_path,monkeypatch);store=ChatFiles(tmp_path/'state');record=saved(store)
    app.state.agent.children['chat-a']={'id':'chat-a','owner_principal_id':'alice','dataset':'cafe','messages':[]}
    with TestClient(app) as alice,TestClient(app) as bob,TestClient(app) as observer:
        login(alice,app,'alice');login(bob,app,'bob');login(observer,app,'owner',owner=True)
        assert alice.get('/api/files?chat_id=chat-a').json()['files']==[record]
        response=alice.get(record['preview_url'])
        assert response.status_code==200 and response.json()['content']==TEXT['resource']['text']
        assert 'no-store' in response.headers['cache-control']
        assert bob.get(record['preview_url']).status_code==404
        assert bob.get('/api/files').json()['files']==[]
        assert alice.get('/api/files/'+record['id']+'/download').status_code==404
        observer.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS)
        assert observer.get(record['preview_url']).json()['content']==TEXT['resource']['text']

@pytest.mark.anyio
async def test_disconnect_after_insert_removes_only_new_rows_and_keeps_historical_outputs(tmp_path,monkeypatch):
    store,relay,projection=projector(tmp_path)
    await projection.event('item/started',params('inProgress'),actor_chat_id='chat-a')
    historical=(await projection.event('item/completed',params(),actor_chat_id='chat-a'))[0]['artifact']
    real=store.import_provider_resource
    def disconnect_after_insert(*args,**kwargs):
        value=real(*args,**kwargs)
        relay.value=None
        return value
    monkeypatch.setattr(store,'import_provider_resource',disconnect_after_insert)
    # A distinct new result is rolled back if disconnect wins during commit.
    await projection.event('item/started',params('inProgress',identity='new-item'),actor_chat_id='chat-a')
    assert await projection.event('item/completed',params(identity='new-item'),actor_chat_id='chat-a')==[]
    assert store.list(owner_principal_id='alice')==[historical]
    assert store.content(historical['id'],**SCOPE)['content']==TEXT['resource']['text']
    # Replayed historical output gets no new-row receipt and survives the same race.
    relay.value=copy.deepcopy(PROVENANCE)
    await projection.event('item/started',params('inProgress'),actor_chat_id='chat-a')
    assert await projection.event('item/completed',params(),actor_chat_id='chat-a')==[]
    assert store.list(owner_principal_id='alice')==[historical]


@pytest.mark.anyio
async def test_cancel_drains_storage_before_releasing_provider_admission(tmp_path,monkeypatch):
    import threading
    store,relay,projection=projector(tmp_path)
    entered=threading.Event();release=threading.Event();held=[];exited=[]
    @asynccontextmanager
    async def admission(server,expected):
        held.append(True)
        try:yield lambda:True
        finally:held.clear();exited.append(True)
    relay.artifact_admission=admission
    real=store.import_provider_resource
    def delayed(*args,**kwargs):
        entered.set();assert release.wait(5)
        assert held and not exited
        return real(*args,**kwargs)
    monkeypatch.setattr(store,'import_provider_resource',delayed)
    await projection.event('item/started',params('inProgress'),actor_chat_id='chat-a')
    task=asyncio.create_task(projection.event('item/completed',params(),actor_chat_id='chat-a'))
    assert await asyncio.to_thread(entered.wait,5)
    task.cancel();await asyncio.sleep(.02)
    assert not task.done() and held
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert exited and store.list(owner_principal_id='alice')==[]


def test_rollback_receipt_cannot_delete_another_owner_or_regular_file(tmp_path):
    store=ChatFiles(tmp_path);created=[]
    record=store.import_provider_resource(resources({'content':[LINK]})[0],provider='notion',provider_name='Notion',publication_key='b'*64,provenance=PROVENANCE,guard=lambda:True,created=created,**SCOPE)
    assert created==[('b'*64,record['id'])]
    with pytest.raises(ValueError):store.discard_provider_publications(created,**{**SCOPE,'owner_principal_id':'bob'})
    assert store.get(record['id'],**SCOPE)==record
    store.discard_provider_publications(created,**SCOPE)
    assert store.list(owner_principal_id='alice')==[]

@pytest.mark.anyio
async def test_real_relay_admission_fences_disconnect_and_preserves_saved_resources(tmp_path,package,snapshot,monkeypatch):
    import threading
    from test_native_provider_relay import setup
    packages,connections,selection,remote,relay,app=await setup(tmp_path,package,snapshot,monkeypatch)
    store=ChatFiles(tmp_path/'outputs')
    projection=ProviderOutputs(store,relay,owner='alice',root_chat_id='chat-a',dataset='cafe',company_id=packages.company_scope,current=lambda:True)
    try:
        await projection.event('item/started',params('inProgress'),actor_chat_id='chat-a')
        historical=(await projection.event('item/completed',params(),actor_chat_id='chat-a'))[0]['artifact']
        entered=threading.Event();release=threading.Event();real=store.import_provider_resource
        def pause_after_commit(*args,**kwargs):
            record=real(*args,**kwargs);entered.set();assert release.wait(5);return record
        monkeypatch.setattr(store,'import_provider_resource',pause_after_commit)
        await projection.event('item/started',params('inProgress',identity='late'),actor_chat_id='chat-a')
        pending=asyncio.create_task(projection.event('item/completed',params(identity='late'),actor_chat_id='chat-a'))
        assert await asyncio.to_thread(entered.wait,5)
        disconnect=asyncio.create_task(connections.disconnect('notion',principal_id='alice'))
        for _ in range(100):
            if connections._record('notion','alice')['status']!='connected':break
            await asyncio.sleep(.005)
        assert not disconnect.done()
        release.set()
        assert await pending==[]
        await disconnect
        assert store.list(owner_principal_id='alice')==[historical]
        assert store.content(historical['id'],**SCOPE)['content']==TEXT['resource']['text']
        assert remote.calls==[]
    finally:await connections.close()


@pytest.mark.anyio
async def test_locked_file_catalogue_preserves_successful_native_tool_result(tmp_path):
    from test_isolated_turn import tree_fixture
    agent,run,connection,tree=tree_fixture(tmp_path)
    store=agent._files();relay=Relay()
    tree.provider_outputs=ProviderOutputs(store,relay,owner='alice',root_chat_id='chat',dataset='cafe',company_id='company',current=lambda:True)
    await tree.event({'method':'item/started','params':params('inProgress')})
    with store.connect() as writer:
        writer.execute('BEGIN IMMEDIATE')
        output=await tree.event({'method':'item/completed','params':params(content=[{'type':'text','text':'Original provider response.'},TEXT])})
    assert any(event['type']=='tool_end' and event['success'] and 'Original provider response.' in event['output'] for event in output)
    assert not any(event['type']=='artifact' for event in output)
    assert store.list(owner_principal_id='alice')==[]
