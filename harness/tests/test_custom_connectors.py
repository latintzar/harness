"""Custom adapter source, identity and private credentials have separate lifecycles."""
import json
from pathlib import Path
import sys
from io import BytesIO
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from custom_connectors import CustomConnectors


def package():
    return {'summary': 'Read delivery records from the company database.',
            'instructions': 'Set DATABASE_URL through Credentials, then test with the local runner.',
            'secret_fields': [{'name': 'DATABASE_URL', 'label': 'Database connection',
                               'description': 'A database account with access to delivery records.'}],
            'files': [{'path': 'connector.py', 'content': 'import os\nurl = os.environ["DATABASE_URL"]\n'},
                      {'path': 'README.md', 'content': 'Run in the company runner.'}]}


def create(store, owner='alice'):
    return store.create(name='Warehouse database', description='Find deliveries by order number.',
                        kind='sql', collection='warehouse', owner=owner)


def test_package_and_encrypted_secret_survive_restart_without_exposing_values(tmp_path):
    store=CustomConnectors(tmp_path)
    record=create(store)
    prepared=store.save_package(record['id'], package(), expected_revision=1, owner='alice')
    assert prepared['status']=='prepared' and prepared['revision']==2
    private='postgresql://warehouse:private-password@db.internal/orders'
    saved=store.set_secret(record['id'],'DATABASE_URL',private,owner='alice')
    assert saved['configured']
    assert private not in json.dumps(saved)
    reopened=CustomConnectors(tmp_path)
    metadata=reopened.get(record['id'],owner='alice')
    assert metadata['secret_fields'][0]['configured']
    assert metadata['status']=='prepared'  # Code and credentials alone prove no connection.
    assert private not in json.dumps(metadata)
    assert private.encode() not in store.database.read_bytes()
    assert store.database.stat().st_mode & 0o077 == 0
    assert store.key_path.stat().st_mode & 0o077 == 0
    archive=reopened.package(record['id'],owner='alice')
    assert private.encode() not in archive
    with ZipFile(BytesIO(archive)) as output:
        assert output.read('connector.py').startswith(b'import os')
        manifest=json.loads(output.read('rocoto-connector.json'))
        assert manifest['credential_names']==['DATABASE_URL']
        assert manifest['execution']=='customer_runner_or_isolated_executor'
    reopened.remove_secret(record['id'],'DATABASE_URL',owner='alice')
    assert not reopened.get(record['id'],owner='alice')['secret_fields'][0]['configured']


def test_scopes_revisions_paths_and_credential_echo_are_enforced(tmp_path):
    store=CustomConnectors(tmp_path)
    record=create(store)
    assert store.list(owner='bob')==[]
    for call in [lambda:store.get(record['id'],owner='bob'),
                 lambda:store.save_package(record['id'],package(),expected_revision=1,owner='bob'),
                 lambda:store.package(record['id'],owner='bob')]:
        with pytest.raises(ValueError):call()
    assert CustomConnectors(tmp_path/'other-company').list(owner='alice')==[]
    for path in ['../token','/etc/passwd','.env','src/../../bad.py','a\\b.py','rocoto-connector.json']:
        bad=package();bad['files'][0]['path']=path
        with pytest.raises(ValueError):store.save_package(record['id'],bad,expected_revision=1,owner='alice')
    store.save_package(record['id'],package(),expected_revision=1,owner='alice')
    with pytest.raises(ValueError):store.save_package(record['id'],package(),expected_revision=1,owner='alice')
    with pytest.raises(ValueError):store.set_secret(record['id'],'UNDECLARED','abc',owner='alice')
    store.set_secret(record['id'],'DATABASE_URL','super-private-value',owner='alice')
    bad=package();bad['files'][0]['content']='print("super-private-value")'
    with pytest.raises(ValueError):store.save_package(record['id'],bad,expected_revision=2,owner='alice')
    assert store.get(record['id'],owner='alice')['revision']==2


def test_package_validation_cannot_mark_connected_or_execute_source(tmp_path):
    store=CustomConnectors(tmp_path);record=create(store)
    malicious=package();malicious['status']='connected'
    with pytest.raises(ValueError):store.save_package(record['id'],malicious,expected_revision=1,owner='alice')
    source=package();source['files'][0]['content']='raise RuntimeError("Generated source is data")'
    assert store.save_package(record['id'],source,expected_revision=1,owner='alice')['status']=='prepared'
    assert 'runtime_ready' not in store.get(record['id'],owner='alice')


def test_unchanged_package_downloads_keep_the_same_digest(tmp_path, monkeypatch):
    import zipfile
    store = CustomConnectors(tmp_path)
    record = create(store)
    store.save_package(record['id'], package(), expected_revision=1, owner='alice')
    monkeypatch.setattr(zipfile.time, 'localtime', lambda *args: (2026, 10, 4, 10, 0, 0, 0, 0, 0))
    first = store.package(record['id'], owner='alice')
    monkeypatch.setattr(zipfile.time, 'localtime', lambda *args: (2026, 10, 4, 10, 0, 10, 0, 0, 0))
    assert store.package(record['id'], owner='alice') == first


def test_http_builder_chat_private_secret_and_cross_user_boundaries(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import HEADERS,make_app
    app=make_app(tmp_path,monkeypatch)
    calls=[]
    async def native(message,chat_id,dataset,*args,**kwargs):
        calls.append({'message':message,'chat_id':chat_id,'dataset':dataset,**kwargs})
        yield {'type':'delta','text':'Which database do you use?'}
        yield {'type':'done','status':'completed'}
    monkeypatch.setattr(app.state.agent,'_turn',native)
    with TestClient(app) as client:
        session=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()
        owner=session['principal']['id']
        response=client.post('/api/connections/custom',json={'name':'Deliveries','kind':'sql',
            'description':'Read deliveries by order number.','collection':'cafe'},headers=HEADERS)
        assert response.status_code==200,response.text
        saved=response.json();key=saved['id']
        chat=client.get('/api/chats/'+saved['chat_id']).json()
        assert chat['owner_principal_id']==owner
        assert chat['messages'][0]['content']=='Help me connect Deliveries.\n\nRead deliveries by order number.'
        assert calls[0]['dataset']=='cafe' and key in calls[0]['model_context']
        store=app.state.agent.custom_connector_store
        store.save_package(key,package(),expected_revision=1,owner=owner)
        secret='unique-database-secret-value'
        for body in [{'name':'DATABASE_URL','value':secret}, {'name':'DATABASE_URL','value':[secret]},
                     {'name':'DATABASE_URL','value':secret,'extra':secret}]:
            reply=client.post(f'/api/connections/custom/{key}/secrets',json=body,headers=HEADERS)
            assert secret not in reply.text
        detail=client.get(f'/api/connections/custom/{key}').json()
        assert detail['secret_fields'][0]['configured']
        assert secret not in json.dumps(detail)
        assert secret not in client.get('/api/chats/'+saved['chat_id']).text
        download=client.get(detail['package_url'])
        assert download.status_code==200 and download.headers['cache-control']=='private, no-store'
        assert secret.encode() not in download.content
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        assert client.get('/api/connections/custom').json()['connections']==[]
        assert client.get(f'/api/connections/custom/{key}').status_code==404
        assert client.get(detail['package_url']).status_code==404
        assert client.post(f'/api/connections/custom/{key}/secrets',json={'name':'DATABASE_URL','value':secret},headers=HEADERS).status_code==400


def test_custom_connector_agent_tools_keep_source_scope_and_no_secret_read_tool(tmp_path):
    import asyncio
    from agent_bridge import CodexVideoAgent,tool_specs
    agent=CodexVideoAgent(tmp_path,object(),lambda:{})
    store=CustomConnectors(tmp_path);agent.custom_connector_store=store
    saved=create(store)
    agent.children['chat']={'id':'chat','owner_principal_id':'alice','dataset':'warehouse'}
    agent.active['chat']={'cancel':asyncio.Event()}
    async def scenario():
        output,_,ok=await agent._tool('save_custom_connector',{'connector_id':saved['id'],
            'expected_revision':1,'package':package()},scope='warehouse',chat_id='chat')
        assert ok and json.loads(output[0]['text'])['status']=='prepared'
        store.set_secret(saved['id'],'DATABASE_URL','private-field-value',owner='alice')
        output,_,ok=await agent._tool('get_custom_connector',{'connector_id':saved['id']},scope='warehouse',chat_id='chat')
        assert ok and 'private-field-value' not in json.dumps(output)
        assert json.loads(output[0]['text'])['secret_fields'][0]['configured']
        _,_,ok=await agent._tool('get_custom_connector',{'connector_id':saved['id']},scope='cafe',chat_id='chat')
        assert not ok
        agent.children['chat']['parent_id']='parent'
        output,_,ok=await agent._tool('get_custom_connector',{'connector_id':saved['id']},scope='warehouse',chat_id='chat')
        assert ok and 'private-field-value' not in json.dumps(output)
        _,_,ok=await agent._tool('save_custom_connector',{'connector_id':saved['id'],
            'expected_revision':2,'package':package()},scope='warehouse',chat_id='chat')
        assert not ok
    asyncio.run(scenario())
    assert not any('secret' in row['name'] for row in tool_specs())


def test_automatic_builder_completes_without_an_http_stream_subscriber(tmp_path,monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from test_skill_runtime import HEADERS,make_app
    app=make_app(tmp_path,monkeypatch)
    async def native(*args,**kwargs):
        for _ in range(180):yield {'type':'delta','text':'x'}
        yield {'type':'done','status':'completed'}
    monkeypatch.setattr(app.state.agent,'_turn',native)
    with TestClient(app) as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        saved=client.post('/api/connections/custom',json={'name':'Long setup','kind':'api',
            'description':'Describe this API.','collection':'cafe'},headers=HEADERS).json()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            chat=client.get('/api/chats/'+saved['chat_id']).json()
            if not chat['running']:break
            time.sleep(.01)
        assert not chat['running']
        assert chat['messages'][-1]['content']=='x'*180


@pytest.mark.parametrize('private',['pass"word\\literal','line-one\nline-two','contraseña-秘密'])
def test_credential_echo_guards_compare_decoded_source_and_archived_versions(tmp_path,private):
    store=CustomConnectors(tmp_path);saved=create(store)
    store.save_package(saved['id'],package(),expected_revision=1,owner='alice')
    store.set_secret(saved['id'],'DATABASE_URL',private,owner='alice')
    bad=package();bad['files'][0]['content']=private
    with pytest.raises(ValueError):store.save_package(saved['id'],bad,expected_revision=2,owner='alice')
    other=create(store);store.save_package(other['id'],bad,expected_revision=1,owner='alice')
    store.save_package(other['id'],package(),expected_revision=2,owner='alice')
    with pytest.raises(ValueError):store.set_secret(other['id'],'DATABASE_URL',private,owner='alice')
