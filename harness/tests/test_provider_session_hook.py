"""Verified package selection -> scoped native relay, with Stop at setup awaits."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_executor import NativeScope,ExecutorError
from native_mcp import NativeMCP,NativeMCPScope
from native_provider_relay import NativeProviderRelay
from deploy.migration.legacy.isolated_native import IsolatedNativeLauncher,IsolatedLaunchError
from test_isolated_native import Broker,MCP,auth,SCOPE
from test_isolated_turn import root_agent,Launcher,NativeProtocol
from test_native_packages import package,snapshot
from test_native_provider_relay import setup

@pytest.fixture
def anyio_backend():return 'asyncio'

class PackageBroker(Broker):
    async def install_packages(self,scope,lease,selection):
        self.selection=selection
        return {'plugins':{'notion@rocoto-pinned-api':{'enabled':True,'mcp_servers':{'notion':{'enabled':False}}}}}

@pytest.mark.anyio
async def test_only_verified_mounted_selection_is_exposed_and_stop_clears_it(tmp_path,package,snapshot,monkeypatch):
    store,_,_,_,_,_=await setup(tmp_path,package,snapshot,monkeypatch)
    broker=PackageBroker(tmp_path);scope=NativeScope(store.company_scope,'alice','root')
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id=store.company_scope,enabled=True,client=broker,package_store=store)
    async with launcher.lease(scope,run_id='run') as session:
        assert session.runtime_packages is broker.selection
        assert session.runtime_packages.packages[0].id=='notion'
        broker.failure='stop'
        with pytest.raises(ExecutorError):await session.stop()
        assert session.runtime_packages is broker.selection and session in launcher._sessions
        broker.failure=None
        await session.stop()
        assert session.runtime_packages is None

@pytest.mark.anyio
async def test_default_binding_replaces_entire_map_with_only_original_rocoto_server(tmp_path):
    class EmptyMCP(MCP):
        async def provider_configs(self):return []
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=Broker(tmp_path))
    async with launcher.lease(SCOPE,run_id='run') as session:
        assert session.runtime_packages is None
        config=await session.bind_mcp(EmptyMCP())
        assert config=={'keyPath':'mcp_servers','mergeStrategy':'replace','value':{'rocoto':{
            'url':'http://127.0.0.1:8766/mcp','http_headers':{'Authorization':'Bearer synthetic-mcp-grant'}}}}

@pytest.mark.anyio
async def test_stop_while_provider_grant_is_issued_revokes_late_grant(tmp_path):
    entered=asyncio.Event();release=asyncio.Event()
    class Pending(MCP):
        async def provider_configs(self):
            entered.set();await release.wait()
            self.revoked=False  # Simulate a grant minted after Stop's initial revoke.
            return [{'keyPath':'mcp_servers.notion','value':{'url':'local'}}]
    app=Pending();launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=Broker(tmp_path))
    async with launcher.lease(SCOPE,run_id='run') as session:
        async def stop():
            await entered.wait();await session.stop();release.set()
        stopping=asyncio.create_task(stop())
        with pytest.raises(IsolatedLaunchError,match='binding_closed'):await session.bind_mcp(app)
        await stopping
        assert app.revoked and session.listener.app is None

@pytest.mark.anyio
async def test_cancel_during_provider_setup_revokes_all_grants_and_stops_worker(tmp_path):
    entered=asyncio.Event();app=None;broker=Broker(tmp_path)
    class Pending(MCP):
        async def provider_configs(self):entered.set();await asyncio.Event().wait()
    app=Pending();launcher=IsolatedNativeLauncher(auth(tmp_path),company_id='demo',enabled=True,client=broker)
    async def task():
        async with launcher.lease(SCOPE,run_id='run') as session:await session.bind_mcp(app)
    consumer=asyncio.create_task(task());await asyncio.wait_for(entered.wait(),2);consumer.cancel()
    with pytest.raises(asyncio.CancelledError):await consumer
    assert app.revoked and app.closed and broker.stopped==1

@pytest.mark.anyio
async def test_full_turn_uses_actual_selection_and_disconnect_removes_old_config(tmp_path,package,snapshot,monkeypatch):
    store,connections,selected,_,_,_=await setup(tmp_path,package,snapshot,monkeypatch)
    agent=root_agent(tmp_path/'agent');agent._connections=lambda:connections
    class SelectedLauncher(Launcher):
        company_id=store.company_scope
        package_store=store
        @asynccontextmanager
        async def lease(self,scope,run_id):
            async with super().lease(scope,run_id) as session:
                session.runtime_packages=selected
                async def bind(app):
                    assert app.provider_relay.selection is selected
                    self.bindings.append(app.provider_relay)
                    providers=await app.provider_configs()
                    return {'keyPath':'mcp_servers','mergeStrategy':'replace','value':{
                        'rocoto':{'url':'local-rocoto'},**{p['keyPath'].split('.',1)[1]:p['value'] for p in providers}}}
                session.bind_mcp=bind
                yield session
    protocol=NativeProtocol();launcher=SelectedLauncher(agent,protocol);agent.isolated_launcher=launcher
    output=[v async for v in agent._turn('Read Notion','chat','cafe')]
    assert output[-1].get('status')=='completed',output
    config=next(p for m,p in protocol.calls if m=='config/value/write')
    assert set(config['value'])=={'rocoto','notion'}
    assert config['value']['notion']['url']=='http://127.0.0.1:8766/mcp'
    assert 'Bearer ' not in json.dumps(output) and 'Bearer ' not in agent.toolsets_path.read_text()
    await connections.disconnect('notion',principal_id='alice');protocol.calls=[]
    output=[v async for v in agent._turn('Continue','chat','cafe')]
    assert output[-1].get('status')=='completed',output
    config=next(p for m,p in protocol.calls if m=='config/value/write')
    assert config['keyPath']=='mcp_servers' and config['mergeStrategy']=='replace' and set(config['value'])=={'rocoto'}

@pytest.mark.anyio
async def test_stop_during_final_mount_verification_prevents_late_selection_or_start(tmp_path,package,snapshot,monkeypatch):
    store,_,_,_,_,_=await setup(tmp_path,package,snapshot,monkeypatch)
    broker=PackageBroker(tmp_path);scope=NativeScope(store.company_scope,'alice','root')
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id=store.company_scope,enabled=True,client=broker,package_store=store)
    actual=store.validate_runtime_snapshot;entered=asyncio.Event();release=asyncio.Event();checks=0;seen=[]
    async def verify(*args,**kwargs):
        nonlocal checks
        checks+=1
        if checks==2:entered.set();await release.wait()
        return await actual(*args,**kwargs)
    monkeypatch.setattr(store,'validate_runtime_snapshot',verify)
    async def launch():
        async with launcher.lease(scope,run_id='run') as session:seen.append(session)
    task=asyncio.create_task(launch());await entered.wait()
    session=next(iter(launcher._sessions));await session.stop();release.set()
    with pytest.raises(IsolatedLaunchError,match='session_closed'):await task
    assert session.runtime_packages is None and not seen and broker.started==0
