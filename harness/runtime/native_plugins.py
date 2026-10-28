"""Host-only native Codex plugin management, isolated per company and person.

The manager exposes installation RPCs, never a model thread or turn. Plugin packages
are materialized by Codex itself. Account connection and tool admission are separate.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from copy import deepcopy
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil

from plugin_catalogue import catalogue

_METHODS = frozenset({'initialize', 'account/login/start', 'account/read', 'plugin/list',
                     'plugin/read', 'plugin/install', 'plugin/uninstall', 'config/value/write'})
_MARKETPLACE = 'rocoto-openai-api'


class NativePluginError(ValueError):
    pass


class _Session:
    def __init__(self, process):
        self.process, self.sequence = process, 0

    async def call(self, method, params):
        if method not in _METHODS:
            raise NativePluginError('Choose a supported plugin management action.')
        self.sequence += 1
        identity = self.sequence
        message = {'id': identity, 'method': method, 'params': params}
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()
        async with asyncio.timeout(60):
            for _ in range(1000):
                line = await self.process.stdout.readline()
                if not line:
                    raise NativePluginError('The plugin manager needs a fresh connection.')
                result = json.loads(line)
                if result.get('id') == identity and 'method' not in result:
                    if 'error' in result:
                        raise NativePluginError('Codex could not complete this plugin action. Try again.')
                    return result.get('result', {})
                if 'method' in result and 'id' in result:
                    # This management transport never approves executable work or user delegation.
                    self.process.stdin.write((json.dumps({'id': result['id'], 'error': {
                        'code': -32601, 'message': 'Plugin management request only'}}) + '\n').encode())
                    await self.process.stdin.drain()
        raise NativePluginError('The plugin manager reached its response limit.')


class NativePlugins:
    def __init__(self, state, *, api_key, executable=None, snapshot=None, session_factory=None):
        self.state = Path(state).resolve()
        self._api_key = api_key
        self.executable = executable or shutil.which('codex')
        self.snapshot = deepcopy(catalogue() if snapshot is None else snapshot)
        self._entries = {row['id']: row for row in self.snapshot['plugins']}
        self._session_factory = session_factory
        self._slot = asyncio.Semaphore(1)

    def _paths(self, principal_id):
        if not isinstance(principal_id, str) or not principal_id.strip() or len(principal_id) > 512:
            raise NativePluginError('Choose the signed-in account for this plugin.')
        digest = hashlib.sha256(principal_id.encode()).hexdigest()
        root = self.state / 'native-plugins' / digest
        home, workspace = root / 'home', root / 'workspace'
        for path in (self.state / 'native-plugins', root, home, workspace, workspace / '.agents', workspace / '.agents/plugins'):
            if path.is_symlink():
                raise NativePluginError('The plugin workspace needs an ordinary private directory.')
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        return home, workspace, workspace / '.agents/plugins/marketplace.json'

    def _manifest(self):
        rows = []
        for entry in self.snapshot['plugins']:
            if not entry['supported_api']:
                continue
            row = deepcopy(entry['catalogue_entry'])
            row.update(deepcopy(entry.get('manifest') or {}))
            row['name'] = entry['id']
            source = row.get('source', {})
            if source.get('source') == 'local':
                row['source'] = {'source': 'git-subdir', 'url': 'https://github.com/openai/plugins.git',
                                 'path': source['path'].removeprefix('./'), 'sha': self.snapshot['revision']}
            rows.append(row)
        return {'name': _MARKETPLACE, 'interface': {'displayName': 'OpenAI plugins'}, 'plugins': rows}

    @asynccontextmanager
    async def _session(self, principal_id):
        async with self._slot:
            home, workspace, marketplace = self._paths(principal_id)
            manifest = self._manifest()
            if marketplace.is_symlink():
                raise NativePluginError('The plugin directory needs a private catalogue file.')
            marketplace.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
            marketplace.chmod(0o600)
            if self._session_factory:
                async with self._session_factory(home, workspace) as session:
                    yield session, marketplace
                return
            if not self.executable:
                raise NativePluginError('Install the commissioned Codex runtime to manage plugins.')
            key = self._api_key() if callable(self._api_key) else self._api_key
            if not isinstance(key, str) or not key or len(key) > 16384 or any(c.isspace() for c in key):
                raise NativePluginError('Commission the Rocoto API credential to manage plugins.')
            # The key is delivered by RPC, never inherited by Git or plugin subprocesses.
            env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}}
            env['CODEX_HOME'] = str(home)
            # Isolate native configuration; this is the supported Codex home configuration.
            config = {'cli_auth_credentials_store': 'ephemeral', 'features.plugins': True,
                      'features.apps': False, 'features.remote_plugin': False, 'features.hooks': False,
                      'features.shell_tool': False, 'features.code_mode': False, 'features.code_mode_only': False,
                      'features.skip_host_skill_discovery': True, 'mcp_servers': {},
                      'model_provider': 'rocoto_management',
                      'model_providers.rocoto_management.name': 'Plugin management',
                      'model_providers.rocoto_management.base_url': 'http://127.0.0.1:1/v1',
                      'model_providers.rocoto_management.wire_api': 'responses',
                      'model_providers.rocoto_management.requires_openai_auth': True,
                      'model_providers.rocoto_management.supports_websockets': False,
                      f'projects.{json.dumps(str(workspace))}.trust_level': 'trusted'}
            args = [part for name, value in config.items() for part in ('-c', name + '=' + json.dumps(value))]
            process = await asyncio.create_subprocess_exec(self.executable, 'app-server', '--listen', 'stdio://', *args,
                env=env, cwd=workspace, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=8_000_000, start_new_session=True)
            session = _Session(process)
            try:
                async with asyncio.timeout(150):
                    await session.call('initialize', {'clientInfo': {'name': 'rocoto_plugin_manager', 'title': 'Rocoto', 'version': '1.0'}, 'capabilities': {'experimentalApi': True}})
                    process.stdin.write(b'{"method":"initialized"}\n')
                    await process.stdin.drain()
                    await session.call('account/login/start', {'type': 'apiKey', 'apiKey': key})
                    account = await session.call('account/read', {'refreshToken': False})
                    if (account.get('account') or {}).get('type') != 'apiKey':
                        raise NativePluginError('Select the commissioned API account for plugin management.')
                    yield session, marketplace
            except (TimeoutError, OSError, json.JSONDecodeError) as error:
                raise NativePluginError('The plugin manager needs a fresh connection. Try again.') from error
            finally:
                key = None
                if process.returncode is None:
                    # Git children belong to this dedicated process group too.
                    import signal
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()

    def _entry(self, identifier):
        entry = self._entries.get(identifier)
        if not entry:
            raise NativePluginError('Choose a plugin from the published catalogue.')
        if not entry['supported_api']:
            raise NativePluginError('This plugin uses its publisher’s account setup. Open its details to continue.')
        return entry

    @staticmethod
    def _status(row):
        return {'id': row['name'], 'native_id': row['id'], 'installed': bool(row.get('installed')),
                'enabled': bool(row.get('enabled')), 'version': row.get('localVersion') or row.get('version'),
                'runtime': 'codex', 'connected': False, 'callable': False}

    async def _list(self, session, marketplace):
        result = await session.call('plugin/list', {'cwds': [str(marketplace.parents[2])], 'marketplaceKinds': ['local'], 'forceRefetch': False})
        selected = [item for item in result.get('marketplaces', []) if item.get('name') == _MARKETPLACE]
        if len(selected) != 1:
            raise NativePluginError('Codex is preparing the published plugin directory. Try again.')
        return [row for row in selected[0].get('plugins', []) if row.get('name') in self._entries and self._entries[row['name']]['supported_api']]

    async def list(self, *, principal_id):
        async with self._session(principal_id) as (session, marketplace):
            return {'plugins': [self._status(row) for row in await self._list(session, marketplace)], 'runtime': 'codex', 'catalogue_revision': self.snapshot['revision']}

    async def read(self, identifier, *, principal_id):
        self._entry(identifier)
        async with self._session(principal_id) as (session, marketplace):
            result = await session.call('plugin/read', {'pluginName': identifier, 'marketplacePath': str(marketplace)})
            plugin = result['plugin']
            return {'plugin': self._status(plugin['summary']), 'skills': [
                {key: row.get(key) for key in ('name', 'description', 'enabled')} for row in plugin.get('skills', [])],
                'mcp_servers': plugin.get('mcpServers', []), 'runtime': 'codex'}

    async def install(self, identifier, *, principal_id):
        self._entry(identifier)
        async with self._session(principal_id) as (session, marketplace):
            result = await session.call('plugin/install', {'pluginName': identifier, 'marketplacePath': str(marketplace)})
            rows = await self._list(session, marketplace)
            row = next((row for row in rows if row['name'] == identifier), None)
            if not row or not row.get('installed'):
                raise NativePluginError('Codex is preparing this plugin. Refresh its details to check progress.')
            return {**self._status(row), 'auth_policy': result.get('authPolicy'),
                    'apps_needing_auth': [{'id': app.get('id'), 'name': app.get('name')} for app in result.get('appsNeedingAuth', [])]}

    async def uninstall(self, identifier, *, principal_id):
        self._entry(identifier)
        async with self._session(principal_id) as (session, marketplace):
            row = next((row for row in await self._list(session, marketplace) if row['name'] == identifier), None)
            if row and row.get('installed'):
                await session.call('plugin/uninstall', {'pluginId': row['id']})
            rows = await self._list(session, marketplace)
            current = next((row for row in rows if row['name'] == identifier), None)
            if current and current.get('installed'):
                raise NativePluginError('Refresh the plugin directory to check the removal.')
            return {'id': identifier, 'installed': False, 'enabled': False, 'connected': False, 'callable': False, 'runtime': 'codex'}

    async def set_enabled(self, identifier, enabled, *, principal_id):
        self._entry(identifier)
        if type(enabled) is not bool:
            raise NativePluginError('Choose the enabled state for this plugin.')
        async with self._session(principal_id) as (session, marketplace):
            row = next((row for row in await self._list(session, marketplace) if row['name'] == identifier), None)
            if not row or not row.get('installed'):
                raise NativePluginError('Add this plugin before changing its enabled state.')
            await session.call('config/value/write', {'keyPath': f'plugins.{json.dumps(row["id"])}.enabled', 'value': enabled, 'mergeStrategy': 'replace'})
            return {**self._status(row), 'enabled': enabled}
