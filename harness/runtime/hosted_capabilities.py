"""Exact installed capabilities packaged for OpenAI's managed environment.

Original plugin files are immutable inputs. Account connections remain host tools;
only explicitly reviewed credential-free public MCP endpoints load in a sandbox.
Private content-addressed ZIPs preserve the original environment across recovery.
"""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import hashlib
import fcntl
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile

from managed_sessions import digest
from workspace_identity import workspace_scope

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_CACHE_FILES = 4096
PUBLIC_READ_SERVERS = {'openai_docs': 'https://developers.openai.com/mcp'}


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise ValueError('Use a declared hosted capability name.')
    return value


def _zip(name, files, directories=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for directory in sorted(directories):
            entry = zipfile.ZipInfo(name + '/' + directory + '/', date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFDIR | 0o755) << 16
            archive.writestr(entry, b'')
        for path, data, executable in sorted(files):
            entry = zipfile.ZipInfo(name + '/' + path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            archive.writestr(entry, data)
    result = buffer.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise ValueError('Choose capabilities within the hosted archive allowance.')
    return result


def _read_file(package, entry):
    """Read verified files through held directory FDs, including every ancestor."""
    parts = PurePosixPath(entry.path).parts
    if (not parts or PurePosixPath(entry.path).is_absolute() or '..' in parts
            or '\\' in entry.path or entry.size_bytes > MAX_ARCHIVE_BYTES):
        raise ValueError('Choose an ordinary bounded installed package file.')
    directory = os.open(package.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.geteuid() or before.st_size != entry.size_bytes):
                raise ValueError('Refresh the verified installed package files.')
            chunks, total = [], 0
            while data := os.read(descriptor, 1024 * 1024):
                total += len(data)
                if total > entry.size_bytes:
                    raise ValueError('Refresh the verified installed package files.')
                chunks.append(data)
            data = b''.join(chunks)
            after = os.fstat(descriptor)
            if (len(data) != entry.size_bytes or _hash(data) != entry.sha256
                    or (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) !=
                       (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
                    or bool(after.st_mode & 0o111) != entry.executable):
                raise ValueError('Refresh the verified installed package files.')
            return data
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _manifest(files, public_servers):
    by_path = {path: data for path, data, _ in files}
    try:
        manifest = json.loads(by_path['.codex-plugin/plugin.json'])
        name = _name(manifest['name'])
        description = manifest['description']
        if not isinstance(description, str) or not 0 < len(description) <= 4096:
            raise ValueError('Use the original plugin description.')
        # These mechanisms may execute code or introduce account authority outside
        # the audited relay. Preserve the package; report readiness to its caller.
        if any(manifest.get(field) for field in ('apps', 'hooks', 'lspServers')):
            raise ValueError('This installed package needs its commissioned host relay.')
        configs = manifest.get('mcpServers', [])
        if isinstance(configs, str):
            configs = [configs]
        elif isinstance(configs, dict):
            configs = [configs]
        if not isinstance(configs, list):
            raise ValueError('This installed package needs its commissioned host relay.')
        # Codex discovers the conventional file even when it is not declared.
        if '.mcp.json' in by_path and './.mcp.json' not in configs:
            configs.append('./.mcp.json')
        for config in configs:
            if isinstance(config, str):
                path = PurePosixPath(config)
                if not config.startswith('./') or '..' in path.parts or path.is_absolute():
                    raise ValueError('Use a plugin-local MCP configuration.')
                config = json.loads(by_path[path.as_posix()])
            servers = config.get('mcpServers', config)
            if not isinstance(servers, dict):
                raise ValueError('This installed package needs its commissioned host relay.')
            for server_name, server in servers.items():
                if (not isinstance(server, dict) or set(server) != {'type', 'url'}
                        or server['type'] != 'http' or server['url'] != public_servers.get(server_name)
                        or not server['url'].startswith('https://')):
                    raise ValueError('This installed package needs its commissioned host relay.')
        return name, description
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError):
        raise ValueError('Use an installed package with its complete original manifest.') from None


@dataclass(frozen=True)
class HostedSelection:
    identity: dict
    environment: dict
    guard: object

    def current(self):
        try:
            return bool(self.guard())
        except (OSError, ValueError):
            return False

    def commands(self, resolved):
        selected = {item['id']: item for item in self.identity['skills']}
        commands = []
        for item in (resolved or {}).get('skills', []) + (resolved or {}).get('native_skills', []):
            saved = selected.get(item.get('id'))
            if saved is None or saved['source_hash'] != digest(item):
                raise ValueError('Start a new turn to select this hosted skill revision.')
            commands.append(saved['command'])
        return commands


class HostedCapabilities:
    def __init__(self, state, store=None, *, public_read_servers=None):
        self.state = Path(state)
        self.store = store
        if store is not None:
            from plugin_routes import PendingRemovals
            self.removals = PendingRemovals(store)
        else:
            self.removals = None
        self.public_servers = dict(PUBLIC_READ_SERVERS if public_read_servers is None else public_read_servers)
        self.cache = self.state / 'hosted-capabilities'

    def _cache_directory(self):
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = self.cache.lstat()
        if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o077):
            raise ValueError('Use private hosted archive storage.')

    def _archive(self, sha, data=None):
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('Use a saved hosted archive identity.')
        self._cache_directory()
        path = self.cache / (sha + '.zip')
        if data is not None:
            if len(data) > MAX_ARCHIVE_BYTES or _hash(data) != sha:
                raise ValueError('Use a bounded verified hosted archive.')
            lock = os.open(self.cache / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            temporary = None
            try:
                info = os.fstat(lock)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or info.st_mode & 0o077):
                    raise ValueError('Use private hosted archive publication locks.')
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not path.exists() and not path.is_symlink():
                    existing = list(self.cache.glob('*.zip'))
                    if len(existing) >= MAX_CACHE_FILES or sum(item.lstat().st_size for item in existing) + len(data) > MAX_CACHE_BYTES:
                        raise ValueError('Review retained hosted archives before adding another capability.')
                    with tempfile.NamedTemporaryFile(dir=self.cache, prefix='.pending-', delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(path)
                    directory = os.open(self.cache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                os.close(lock)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_ARCHIVE_BYTES):
                raise ValueError('Use private bounded hosted archive storage.')
            result = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(result) > MAX_ARCHIVE_BYTES or _hash(result) != sha:
            raise ValueError('Recover the original verified hosted archive.')
        return result

    def freeze(self, configuration):
        """Save provider configuration with private archive references, not blobs."""
        result = copy.deepcopy(configuration)
        for field in ('plugins', 'skills'):
            for item in result.get('environment', {}).get(field, []):
                source = item.get('source', {})
                if source.get('type') != 'base64' or source.get('media_type') != 'application/zip':
                    raise ValueError('Use an inline hosted capability archive.')
                data = base64.b64decode(source['data'], validate=True)
                sha = _hash(data)
                self._archive(sha, data)
                item['source'] = {'type': 'rocoto_archive', 'sha256': sha}
        return result

    def configuration(self, frozen):
        result = copy.deepcopy(frozen)
        total = 0
        for field in ('plugins', 'skills'):
            for item in result.get('environment', {}).get(field, []):
                source = item.get('source', {})
                if set(source) != {'type', 'sha256'} or source['type'] != 'rocoto_archive':
                    raise ValueError('Recover the original hosted archive references.')
                data = self._archive(source['sha256'])
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise ValueError('Choose capabilities within the hosted archive allowance.')
                item['source'] = {'type': 'base64', 'media_type': 'application/zip',
                                  'data': base64.b64encode(data).decode()}
        return result

    @staticmethod
    def _packages(selection):
        return {'company': selection.company_scope, 'owner': selection.owner_binding,
                'generation': selection.generation, 'selection_sha256': selection.selection_sha256,
                'packages': [{'id': package.id, 'version': package.version, 'tree_sha256': package.tree_sha256,
                              'upstream_revision': package.upstream_revision, 'source_url': package.source_url}
                             for package in selection.packages]}

    def _guard(self, principal, identity):
        if self.removals is not None and self.removals.list(principal):
            return False
        if (identity['company'] != workspace_scope(self.state)
                or identity['owner'] != _hash(principal.encode())):
            return False
        expected = identity.get('selection')
        if expected is None:
            return self.store is None
        if self.store is None or self.store.company_scope != expected['company']:
            return False
        # Generation binds the complete selection. Install/remove advances it
        # atomically; the hot authority fence does not hash package trees.
        generation, rows = self.store._rows(expected['owner'])
        actual = sorted((row['id'], row['version'], row['tree_sha256'], row['upstream_revision']) for row in rows)
        original = sorted((row['id'], row['version'], row['tree_sha256'], row['upstream_revision'])
                          for row in expected['packages'])
        return generation == expected['generation'] and actual == original

    async def prepare(self, principal_id, resolved_skills):
        if self.removals is not None and self.removals.list(principal_id):
            raise ValueError('Finish the pending package removal before another managed turn.')
        identity = {'company': workspace_scope(self.state), 'owner': _hash(principal_id.encode()),
                    'selection': None, 'skills': [], 'readiness': []}
        environment = {}
        selected_native = (resolved_skills or {}).get('native_skills', [])
        if self.store is None and selected_native:
            raise ValueError('Install the selected original package in this workspace.')
        if self.store is not None:
            async with self.store.runtime_selection(principal_id=principal_id) as selection:
                identity['selection'] = self._packages(selection)
                plugins, total = [], 0
                for package in selection.packages:
                    total += sum(entry.size_bytes for entry in package.files)
                    if total > MAX_TOTAL_BYTES:
                        raise ValueError('Choose capabilities within the hosted archive allowance.')
                    files = [(entry.path, _read_file(package, entry), entry.executable) for entry in package.files]
                    try:
                        name, description = _manifest(files, self.public_servers)
                    except ValueError as error:
                        identity['readiness'].append({'id': package.id, 'ready': False, 'reason': str(error)})
                        if any(skill.get('plugin_id') == package.id for skill in selected_native):
                            raise
                        continue
                    identity['readiness'].append({'id': package.id, 'ready': True})
                    if name != package.id:
                        raise ValueError('Use the original installed plugin identity.')
                    plugins.append(self._upload(name, description, _zip(name, files, package.directories)))
                if plugins:
                    environment['plugins'] = plugins
                await self.store.validate_runtime_snapshot(selection, principal_id=principal_id)
                by_id = {package.id: package for package in selection.packages}
                if selected_native:
                    from native_skills import NativePluginSkills
                    verified = NativePluginSkills(self.store).resolve([skill['id'] for skill in selected_native], principal_id)
                    if verified != selected_native:
                        raise ValueError('Refresh the installed skill selection.')
                for skill in selected_native:
                    package = by_id.get(skill.get('plugin_id'))
                    if (package is None or skill.get('revision') != selection.generation
                            or skill.get('package_version') != package.version):
                        raise ValueError('Refresh the installed skill selection.')
                    name = _name(skill.get('name'))
                    command = package.id + ':' + name
                    if skill.get('command') != command:
                        raise ValueError('Use the installed skill command.')
                    identity['skills'].append({'id': skill['id'], 'source_hash': digest(skill), 'command': command})
        skills = []
        for skill in (resolved_skills or {}).get('skills', []):
            name = 'rocoto-' + digest({'id': skill['id']})[:24]
            description, body = skill['description'], skill['instructions']
            if not isinstance(description, str) or not isinstance(body, str) or len(body.encode()) > 64_000:
                raise ValueError('Use a bounded saved company skill.')
            text = ('---\nname: ' + json.dumps(name, ensure_ascii=False) + '\ndescription: '
                    + json.dumps(description, ensure_ascii=False) + '\n---\n\n' + body)
            skills.append(self._upload(name, description, _zip(name, [('SKILL.md', text.encode(), False)])))
            identity['skills'].append({'id': skill['id'], 'source_hash': digest(skill), 'command': name})
        if skills:
            environment['skills'] = skills
        return HostedSelection(identity, environment, lambda: self._guard(principal_id, identity))

    @staticmethod
    def _upload(name, description, data):
        return {'type': 'inline', 'name': name, 'description': description,
                'source': {'type': 'base64', 'media_type': 'application/zip', 'data': base64.b64encode(data).decode()}}

    async def restore(self, principal_id, identity, frozen):
        if not self._guard(principal_id, identity):
            raise ValueError('Recover only the original current package selection.')
        if self.store is not None:
            async with self.store.runtime_selection(principal_id=principal_id) as selection:
                if self._packages(selection) != identity['selection']:
                    raise ValueError('Recover only the original current package selection.')
        environment = self.configuration(frozen).get('environment', {})
        return HostedSelection(copy.deepcopy(identity),
            {key: value for key, value in environment.items() if key in {'plugins', 'skills'}},
            lambda: self._guard(principal_id, identity))
