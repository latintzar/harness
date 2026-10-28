"""HTTP plugin installation and removal use the commissioned managed host."""
from contextlib import asynccontextmanager
import json

from fastapi.testclient import TestClient
from test_server_access import fixture_app
from test_workflow_routes import login, HEADERS


def test_default_closed_installer_and_scoped_managed_removal(tmp_path,monkeypatch):
    from native_packages import StablePluginCLI
    async def forbidden(*args,**kwargs):raise AssertionError('HTTP invoked a local agent core.')
    monkeypatch.setattr(StablePluginCLI,'_command',forbidden)
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/plugins/installed').status_code==401
        login(client,app,'alice')
        result=client.get('/api/plugins/installed').json()
        assert result['plugins']==[] and result['available'] is False
        assert client.post('/api/plugins/notion/install',json={},headers=HEADERS).status_code==503
        calls=[]
        async def revoke(agent,*,principal_id,plugin_id):
            calls.append((agent,principal_id,plugin_id));return True
        monkeypatch.setattr(app.state.agent.managed_runtime,'revoke_plugin',revoke)
        response=client.delete('/api/plugins/notion/installation',headers=HEADERS)
        assert response.status_code==200 and response.json()['cleanup_confirmed']
        assert calls==[(app.state.agent,'alice','notion')]


def test_unconfirmed_managed_cleanup_retains_removal_journal(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);confirmed=[False]
    async def revoke(*args,**kwargs):return confirmed[0]
    monkeypatch.setattr(app.state.agent.managed_runtime,'revoke_plugin',revoke)
    with TestClient(app) as client:
        login(client,app,'alice')
        result=client.delete('/api/plugins/notion/installation',headers=HEADERS)
        assert result.status_code==503 and result.json()['removed']
        assert client.get('/api/plugins/installed').json()['cleanup_pending_ids']==['notion']
        confirmed[0]=True
        assert client.delete('/api/plugins/notion/installation',headers=HEADERS).status_code==200
        assert client.get('/api/plugins/installed').json()['cleanup_pending_ids']==[]


def test_managed_host_installs_original_files_for_actual_owner_without_cli(tmp_path,monkeypatch):
    from hosted_install import HostedPluginInstaller
    from native_packages import StablePluginCLI
    monkeypatch.setenv('ROCOTO_PUBLIC_ORIGIN','https://harness.example.test')
    original=tmp_path/'package';(original/'.codex-plugin').mkdir(parents=True)
    (original/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'notion','version':'0.1.7','license':'MIT'}))
    installs=[]
    @asynccontextmanager
    async def acquire(self,entry,snapshot):
        installs.append(entry['id']);yield original,{'version':'0.1.7'}
    async def forbidden(*args,**kwargs):raise AssertionError('HTTP invoked a local agent core.')
    monkeypatch.setattr(HostedPluginInstaller,'acquire',acquire)
    monkeypatch.setattr(StablePluginCLI,'_command',forbidden)
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as alice:
        bob=TestClient(app)
        login(alice,app,'alice');login(bob,app,'bob')
        listing=alice.get('/api/plugins/installed').json()
        assert listing['available'] and 'notion' in listing['installable_ids']
        assert alice.post('/api/plugins/notion/install',json={},headers=HEADERS).status_code==200
        assert bob.get('/api/plugins/installed').json()['plugins']==[]
        assert len(alice.get('/api/plugins/installed').json()['plugins'])==1
        assert isinstance(app.state.agent.plugin_package_store.materializer,HostedPluginInstaller)
        assert alice.delete('/api/plugins/notion/installation',headers=HEADERS).json()['cleanup_confirmed']
        assert alice.delete('/api/session',headers=HEADERS).status_code==200
        assert alice.post('/api/plugins/notion/install',json={},headers=HEADERS).status_code==401
        assert installs==['notion']
        bob.close()
