"""Common admission remains real authority when a workspace has no vertical resources."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


def test_bare_admission_revocation_and_chat_rebinding():
    from host import Admission
    current = {'member': True, 'owner': 'alice'}
    grant = Admission(lambda: current == {'member': True, 'owner': 'alice'})
    assert grant.current()
    with grant.bind():
        current['member'] = False
    assert not grant.current()
    with pytest.raises(ValueError):
        with grant.bind():
            pass


def test_common_collections_require_registered_authority():
    from host import Collections
    choices = Collections()
    assert choices.contains('company', 'alice')
    assert not choices.contains('shop', 'alice')
    choices.register(lambda principal: ['shop'] if principal == 'alice' else [])
    choices.seal()
    assert choices.contains('shop', 'alice')
    assert not choices.contains('shop', 'bob')
    with pytest.raises(RuntimeError):
        choices.register(lambda principal: ['elsewhere'])


def test_bare_host_runs_with_retail_folder_absent(tmp_path):
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    import textwrap
    import layout

    destination=tmp_path/'repo'/'harness'
    destination.mkdir(parents=True)
    (destination.parent/'app').mkdir()
    for folder in ('runtime','mechanics','sdk'):
        shutil.copytree(layout.ROOT/folder,destination/folder,
            ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(layout.ROOT/'verticals'/'bare',destination/'verticals'/'bare')
    for name in ('layout.py','run.py','server.py'):
        shutil.copy2(layout.ROOT/name,destination/name)
    (destination/'web'/'vendor').mkdir(parents=True)
    shutil.copy2(layout.ROOT/'web'/'vendor'/'rich-content-vendors.json',destination/'web'/'vendor')
    script=textwrap.dedent('''
        import asyncio,json,sqlite3,sys
        from pathlib import Path
        from fastapi.testclient import TestClient
        import server
        from scope_host import agent_scope
        state=Path(sys.argv[1])
        app=server.create_app(state)
        selected=app.state.workspaces['demo']
        agent=selected['agent']
        async def runtime(message,chat_id,dataset,*args,**kwargs):
            yield {'type':'delta','text':'A bounded fixture response.'}
            yield {'type':'done','status':'completed'}
        agent._turn=runtime
        headers={'X-Video-Demo':'1'}
        with TestClient(app) as client:
            code=app.state.access.issue('demo',owner=True,principal_id='alice',display_name='Alice')
            assert client.post('/api/session',json={'code':code},headers=headers).status_code==200
            assert client.get('/api/harness').json()['vertical']['id']=='bare'
            capabilities=client.get('/api/workspace/capabilities?dataset=company')
            assert capabilities.status_code==200 and 'footage' not in capabilities.json()
            assert client.get('/api/business?collection=company').status_code==200
            for path in ('/api/business/scope?collection=company','/api/retail/scope?collection=company',
                         '/api/chats/unknown/scope','/api/retail/chats/unknown/scope',
                         '/business-scope.js','/static/harness/business-scope.js','/static/retail/retail-scope.js'):
                assert client.get(path).status_code==404,path
            assert not hasattr(selected['business_scope'],'camera')
            assert not (state/'business-scope.sqlite').exists()
            assert client.get('/api/messaging?collection=company').status_code==200
            created=client.post('/api/organization/chats',json={'collection':'company','purpose':'organization'},headers=headers)
            assert created.status_code==200,created.text
            chat=created.json()
            response=client.post('/api/chat',json={'message':'Keep this working.','chat_id':chat['id'],'dataset':'company'},headers=headers)
            assert response.status_code==200 and 'A bounded fixture response.' in response.text,response.text
            saved=client.get('/api/chats/'+chat['id']).json()
            assert any(row.get('content')=='Keep this working.' for row in saved['messages'])
            file=selected['files'].create({'name':'note.txt','content':'A workspace file.'},
                owner_principal_id=selected['organization_chats'].owner,chat_id=chat['id'],dataset='company')
            assert client.get('/api/files/'+file['id']).status_code==200
            missing=client.get('/api/files/export-unavailable/download')
            assert missing.status_code==404,missing.text
            # The same admission object loses authority after a real membership revocation.
            async def revoke():
                try:
                    async with agent_scope(agent,chat['id'],'company',sender={'principal_id':'alice'},admit=True) as admission:
                        assert admission.current()
                        await asyncio.sleep(0)
                        with sqlite3.connect(app.state.access.path) as db:
                            db.execute('DELETE FROM principal_environments WHERE principal_id=? AND environment=?',('alice','demo'))
                        assert not admission.current()
                except ValueError:
                    return
                raise AssertionError('Admission must reject changed authority on exit.')
            asyncio.run(revoke())
        assert not (Path(server.HERE)/'verticals'/'retail').exists()
        assert not {'retail_host','retail_business_scope','retail_scope_routes','monitoring','source_intake','qdrant_retrieval','tracker_runner'} & set(sys.modules)
        assert not any((state/name).exists() for name in ('cosmos','monitoring.sqlite','source-intake'))
        print('bare lifecycle, Organization chat, files and revocation passed')
    ''')
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path/'state')],cwd=destination.parent,
        env={**os.environ,'ROCOTO_VERTICAL':'bare','PYTHONPATH':str(destination),'PYTHONDONTWRITEBYTECODE':'1'},
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'revocation passed' in result.stdout


def test_lazy_workspace_failure_is_cleaned_up_before_retry(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import retail_host
    from test_server_access import fixture_app
    original=retail_host.install_workspace
    events=[]
    attempts=0

    class Coordinator:
        def __init__(self, number):
            self.number=number
        async def ready(self):
            events.append(('ready',self.number))
        async def close(self):
            events.append(('close',self.number))

    def install(host, selected):
        nonlocal attempts
        extension=original(host,selected)
        if selected['environment_id']=='late':
            attempts+=1
            selected['custom_coordinator']=Coordinator(attempts)
            if attempts==1:
                raise ValueError('Injected workspace setup failure.')
        return extension

    monkeypatch.setattr(retail_host,'install_workspace',install)
    app=fixture_app(tmp_path,monkeypatch)
    app.state.access.create_environment('late','Late workspace')
    with TestClient(app,raise_server_exceptions=False) as client:
        token=app.state.access.issue('late',principal_id='alice',display_name='Alice')
        assert client.post('/api/session',json={'code':token},headers={'X-Video-Demo':'1'}).status_code==200
        assert client.get('/api/workspace/capabilities?dataset=cafe').status_code==500
        assert 'late' not in app.state.workspaces
        result=client.get('/api/workspace/capabilities?dataset=cafe')
        assert result.status_code==200,result.text
        assert ('ready',1) not in events and ('ready',2) in events
    assert events.count(('close',1))==1
    assert events.count(('close',2))==1


def test_vertical_lifespan_drains_before_workspace_resources_close(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi.testclient import TestClient
    import retail_host
    from test_server_access import fixture_app
    events=[]
    original=retail_host.lifespan

    @asynccontextmanager
    async def lifecycle(app,host):
        async with original(app,host):
            yield
        events.append('vertical_drained')

    monkeypatch.setattr(retail_host,'lifespan',lifecycle)
    app=fixture_app(tmp_path,monkeypatch)
    connection=app.state.workspaces['demo']['connections']
    close=connection.close
    async def closed():
        events.append('connections_closed')
        await close()
    monkeypatch.setattr(connection,'close',closed)
    with TestClient(app):
        pass
    assert events==['vertical_drained','connections_closed']


def test_managed_recovery_starts_after_mcp_admission_and_drains_before_store_close(tmp_path,monkeypatch):
    import asyncio
    from pathlib import Path
    from fastapi.testclient import TestClient
    from test_server_access import FixtureAgent,FixtureIndex
    import server
    events=[]
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN','https://testserver')
    class Runtime:
        async def recover_pending(self,agent):
            assert app.state.managed_mcp_host.transport.running
            assert agent.tool_registry and agent.managed_mcp_host is app.state.managed_mcp_host
            events.append('recover')
            try:await asyncio.Event().wait()
            finally:events.append('recovery_drained')
    app=server.create_app(tmp_path/'state',FixtureIndex('demo'),agent_factory=FixtureAgent,
        index_factory=lambda path:FixtureIndex(Path(path).parent.name),managed_runtime_factory=lambda agent:Runtime())
    selected=app.state.workspaces['demo'];close=selected['connections'].close
    async def closing():
        events.append('connections_closed')
        await close()
    monkeypatch.setattr(selected['connections'],'close',closing)
    with TestClient(app) as client:
        token=app.state.access.issue('demo',principal_id='alice')
        client.post('/api/session',json={'code':token},headers={'X-Video-Demo':'1'})
        assert client.get('/api/workspace/capabilities?dataset=cafe').status_code==200
        assert client.get('/api/workspace/capabilities?dataset=cafe').status_code==200
    assert events==['recover','recovery_drained','connections_closed']


def test_disabled_builtins_skip_provider_spend_and_channel_workers(tmp_path):
    import json
    from pathlib import Path
    import shutil
    import pack
    import server
    from fastapi.testclient import TestClient
    source=tmp_path/'bare'
    shutil.copytree(Path(pack.load('bare').root),source)
    manifest=json.loads((source/'vertical.json').read_text())
    manifest['builtin_capabilities']={name:False for name in ('whatsapp','viber','email','voice')}
    (source/'vertical.json').write_text(json.dumps(manifest))
    def unavailable(*args):raise AssertionError('Disabled provider or spend factory was called')
    with pack.use(source):
        app=server.create_app(tmp_path/'state',messaging_provider_factory=unavailable,
            phone_spend_factory=unavailable,builtin_provider_factories={name:unavailable for name in ('viber','email','voice')})
        selected=app.state.workspaces['demo']
        selected['phone_catch_up']=unavailable
        selected['phone'].publish=unavailable
        with TestClient(app) as client:
            assert not selected['phone_enabled']
            assert selected['builtins'].pollers()==()
            assert not any(row['selected'] for row in selected['builtins'].status().values())
            assert not selected['messaging'].path.exists()
