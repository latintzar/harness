"""Management-only Codex RPCs preserve tenant, account and installation boundaries."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from native_plugins import NativePlugins, NativePluginError, _Session
from plugin_catalogue import catalogue


class NativeFixture:
    def __init__(self):
        self.calls, self.homes, self.installed, self.enabled = [], [], {}, {}

    @asynccontextmanager
    async def session(self, home, workspace):
        self.homes.append(home)
        owner = self
        class RPC:
            async def call(self, method, params):
                owner.calls.append((home, method, deepcopy(params)))
                if method == 'plugin/list':
                    return {'marketplaces': [{'name': 'rocoto-openai-api', 'plugins': [
                        {'name': 'notion', 'id': 'notion@rocoto-openai-api', 'installed': owner.installed.get(home, False), 'enabled': owner.enabled.get(home, False), 'localVersion': '1.0'}]}]}
                if method == 'plugin/install':
                    owner.installed[home] = owner.enabled[home] = True
                    return {'authPolicy': 'ON_INSTALL', 'appsNeedingAuth': [{'name': 'Notion', 'id': 'connector_notion', 'installUrl': 'https://example.org/private-token'}]}
                if method == 'plugin/uninstall':
                    owner.installed[home] = owner.enabled[home] = False
                    return {}
                if method == 'config/value/write':
                    owner.enabled[home] = params['value']
                    return {}
                if method == 'plugin/read':
                    return {'plugin': {'summary': {'name': 'notion', 'id': 'notion@rocoto-openai-api', 'installed': owner.installed.get(home, False)}, 'skills': [{'name': 'skill', 'description': 'Description', 'enabled': True, 'path': '/private/host'}], 'mcpServers': ['notion']}}
                raise AssertionError(method)
        yield RPC()


def test_install_uses_native_pinned_package_and_keeps_accounts_isolated(tmp_path):
    async def run():
        fixture = NativeFixture()
        manager = NativePlugins(tmp_path, api_key='fixture', session_factory=fixture.session)
        added = await manager.install('notion', principal_id='person-a')
        assert added['installed'] and added['enabled'] and not added['connected'] and not added['callable']
        assert added['apps_needing_auth'] == [{'name': 'Notion', 'id': 'connector_notion'}]
        assert not (await manager.list(principal_id='person-b'))['plugins'][0]['installed']
        home = fixture.homes[0]
        assert home != fixture.homes[-1] and home.stat().st_mode & 0o777 == 0o700
        manifest = json.loads((home.parent / 'workspace/.agents/plugins/marketplace.json').read_text())
        notion = next(row for row in manifest['plugins'] if row['name'] == 'notion')
        assert notion['source'] == {'source': 'git-subdir', 'url': 'https://github.com/openai/plugins.git', 'path': 'plugins/notion', 'sha': catalogue()['revision']}
        assert len(manifest['plugins']) == 50
        assert {method for _, method, _ in fixture.calls} <= {'plugin/install', 'plugin/list'}
    asyncio.run(run())


def test_read_and_controls_use_only_owned_plugin_ids(tmp_path):
    async def run():
        fixture = NativeFixture()
        manager = NativePlugins(tmp_path, api_key='fixture', session_factory=fixture.session)
        await manager.install('notion', principal_id='person')
        details = await manager.read('notion', principal_id='person')
        assert '/private/host' not in json.dumps(details)
        disabled = await manager.set_enabled('notion', False, principal_id='person')
        assert not disabled['enabled']
        call = next(params for _, method, params in fixture.calls if method == 'config/value/write')
        assert call == {'keyPath': 'plugins."notion@rocoto-openai-api".enabled', 'value': False, 'mergeStrategy': 'replace'}
        removed = await manager.uninstall('notion', principal_id='person')
        assert not removed['installed']
        with pytest.raises(NativePluginError):
            await manager.set_enabled('notion', True, principal_id='person')
    asyncio.run(run())


@pytest.mark.parametrize('identifier', ['gmail', '../outside', 'unknown', 'notion@another-account'])
def test_unsupported_catalogue_id_never_starts_native_process(tmp_path, identifier):
    async def run():
        fixture = NativeFixture()
        manager = NativePlugins(tmp_path, api_key='fixture', session_factory=fixture.session)
        with pytest.raises(NativePluginError):
            await manager.install(identifier, principal_id='person')
        assert fixture.calls == [] and fixture.homes == []
    asyncio.run(run())


def test_rpc_transport_rejects_model_work_before_writing():
    async def run():
        session = _Session(None)
        for method in ['thread/start', 'turn/start', 'command/exec', 'mcpServer/tool/call']:
            with pytest.raises(NativePluginError):
                await session.call(method, {})
    asyncio.run(run())


def test_symlinked_private_workspace_is_rejected(tmp_path):
    manager = NativePlugins(tmp_path, api_key='fixture')
    target = tmp_path / 'outside'; target.mkdir()
    (tmp_path / 'native-plugins').symlink_to(target, target_is_directory=True)
    with pytest.raises(NativePluginError):
        manager._paths('person')
