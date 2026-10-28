"""Original Codex packages, private selections, and verified worker-mount inputs.

This module never authenticates an account or starts a model turn. RuntimePackages
is a host-only mount proposal: a privileged broker must revalidate and copy/freeze
its files before mounting them. An app-owned mode-0400 tree is not a security boundary.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import stat
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from plugin_catalogue import catalogue
from workspace_identity import workspace_scope

MAX_PACKAGE_BYTES = 128 * 1024**2
MAX_FILES = 8192
MAX_FILE_BYTES = 64 * 1024**2
MAX_STORE_BYTES = 1024 * 1024**2
MAX_SELECTED = 64
MAX_PACKAGE_RECORDS = 512
MARKETPLACE = 'rocoto-pinned-api'
_SCHEMA = 1


class PackageError(ValueError):
    pass


async def _read_packages(function, *args, **kwargs):
    """Hash bounded package trees off-loop while retaining the caller's fence.

    Cancellation cannot stop a running filesystem read. Drain the exact read
    before propagating cancellation so runtime_selection keeps its cross-process
    mutation lock until every verification of that selection has finished.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        if task.done() and not task.cancelled(): task.exception()
        raise


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _principal(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise PackageError('Select the signed-in account for this plugin.')
    return _hash(value.encode())


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}', value):
        raise PackageError('Choose a published plugin.')
    return value


def _ordinary_directory(path, *, create=False):
    path = Path(path).absolute()
    for node in [*reversed(path.parents), path]:
        try:
            info = node.lstat()
        except FileNotFoundError:
            if not create:
                raise PackageError('Refresh the plugin package.') from None
            node.mkdir(mode=0o700)
            info = node.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise PackageError('Use an ordinary private plugin directory.')
    return path


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class PackageFile:
    path: str
    size_bytes: int
    sha256: str
    executable: bool


@dataclass(frozen=True)
class PackageSnapshot:
    id: str
    version: str
    tree_sha256: str
    root: Path
    files: tuple[PackageFile, ...]
    directories: tuple[str, ...]
    upstream_revision: str
    source_url: str
    license: str | None


@dataclass(frozen=True)
class RuntimePackages:
    company_scope: str
    owner_binding: str
    generation: int
    selection_sha256: str
    packages: tuple[PackageSnapshot, ...]


def _scan_tree_unchecked(root, *, destination=None):
    """Walk held directory FDs and hash/copy exact regular files without following links."""
    root = _ordinary_directory(root)
    destination = Path(destination) if destination is not None else None
    files = []; directories = []; total = 0; entries = 0
    start = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_id = os.fstat(start)
    def visit(fd, relative='', depth=0):
        nonlocal total, entries
        if depth > 24:
            raise PackageError('The plugin directory exceeds its depth allowance.')
        before = os.fstat(fd)
        names = sorted(os.listdir(fd))
        for name in names:
            entries += 1
            if (entries > MAX_FILES * 2 or not name or name in {'.', '..'} or '\\' in name
                    or any(ord(c) < 32 for c in name) or len(name.encode()) > 240):
                raise PackageError('The plugin contains an unsupported file tree.')
            rel = relative + '/' + name if relative else name
            if len(rel.encode()) > 1024:
                raise PackageError('The plugin contains an unsupported file path.')
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid != root_id.st_uid:
                raise PackageError('The package files must retain their installation owner.')
            if stat.S_ISDIR(info.st_mode):
                directories.append(rel)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise PackageError('The plugin changed while being verified.')
                    if destination is not None: (destination / rel).mkdir(mode=0o700)
                    visit(child, rel, depth + 1)
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if len(files) >= MAX_FILES or info.st_size > MAX_FILE_BYTES or total + info.st_size > MAX_PACKAGE_BYTES:
                    raise PackageError('The plugin exceeds its file allowance.')
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                output = None
                try:
                    if _identity(os.fstat(source)) != _identity(info):
                        raise PackageError('The plugin changed while being verified.')
                    if destination is not None:
                        output = os.open(destination / rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    digest = hashlib.sha256(); count = 0
                    while data := os.read(source, 1024 * 1024):
                        count += len(data)
                        if count > info.st_size:
                            raise PackageError('The plugin changed while being verified.')
                        digest.update(data)
                        if output is not None:
                            view = memoryview(data)
                            while view:
                                written = os.write(output, view); view = view[written:]
                    if count != info.st_size or _identity(os.fstat(source)) != _identity(info):
                        raise PackageError('The plugin changed while being verified.')
                    if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(info):
                        raise PackageError('The plugin changed while being verified.')
                    if output is not None:
                        os.fchmod(output, 0o500 if info.st_mode & 0o111 else 0o400)
                        os.fsync(output)
                    files.append(PackageFile(rel, count, digest.hexdigest(), bool(info.st_mode & 0o111)))
                    total += count
                finally:
                    os.close(source)
                    if output is not None: os.close(output)
            else:
                raise PackageError('Plugin packages require ordinary files and directories.')
        if _identity(os.fstat(fd)) != _identity(before) or sorted(os.listdir(fd)) != names:
            raise PackageError('The plugin changed while being verified.')
        if destination is not None: _fsync_dir(destination / relative)
    try:
        visit(start)
        if _identity(root.lstat()) != _identity(root_id):
            raise PackageError('The plugin changed while being verified.')
    finally: os.close(start)
    files.sort(key=lambda row: row.path)
    directories.sort()
    digest = _hash(_json({'files': [asdict(row) for row in files], 'directories': directories}).encode())
    return tuple(files), digest, total, tuple(directories)


def _scan_tree(root, *, destination=None):
    try:
        return _scan_tree_unchecked(root, destination=destination)
    except (OSError, UnicodeError):
        raise PackageError('The plugin changed while being verified. Retry its installation.') from None


def _remove_private_tree(path):
    if not path.exists(): return
    # This directory is a private failed copy, never an installed snapshot or caller path.
    for directory, _, _ in os.walk(path): os.chmod(directory, 0o700)
    shutil.rmtree(path)


class StablePluginCLI:
    """Credential-free Codex CLI materializer; only configured immutable sources are accepted."""
    def __init__(self, executable=None, *, timeout=120):
        self.executable = executable or shutil.which('codex')
        self.timeout = timeout
        self.last_inventory = None

    def _base(self):
        if not self.executable: raise PackageError('Commission the Codex plugin installer.')
        config = {'features.plugins': True, 'features.remote_plugin': False, 'features.apps': False,
            'features.hooks': False, 'features.shell_tool': False, 'features.code_mode': False,
            'features.skip_host_skill_discovery': True, 'mcp_servers': {},
            'model_provider': 'rocoto_management', 'model_providers.rocoto_management.name': 'Package installation',
            'model_providers.rocoto_management.base_url': 'http://127.0.0.1:1/v1',
            'model_providers.rocoto_management.wire_api': 'responses',
            'model_providers.rocoto_management.requires_openai_auth': False,
            'model_providers.rocoto_management.supports_websockets': False}
        return [str(self.executable), *[part for key, value in config.items() for part in ('-c', key + '=' + _json(value))]]

    @staticmethod
    def _environment(home):
        return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home),
                'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false', 'SSH_ASKPASS': '/bin/false',
                'GIT_SSH_COMMAND': '/bin/false'}

    @staticmethod
    async def _stop(process):
        # The CLI leader can finish while a Git/helper descendant still owns
        # its pipes. Always stop the dedicated group, even after leader exit.
        with suppress(ProcessLookupError): os.killpg(process.pid, signal.SIGTERM)
        try: await asyncio.wait_for(process.wait(), 3)
        except TimeoutError: pass
        finally:
            with suppress(ProcessLookupError): os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    @staticmethod
    async def _drain_cleanup(task):
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
        return task.result()

    async def _command(self, args, *, home, cwd):
        starting = asyncio.create_task(asyncio.create_subprocess_exec(*self._base(), *args, cwd=cwd,
            env=self._environment(home), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True))
        tasks = []
        async def discard(stream):
            while await stream.read(65536): pass
        async def cleanup(process):
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Drain both pipe transports while reaping, including buffered data
            # after a size-limit error. Process exit alone does not close them.
            draining = asyncio.gather(discard(process.stdout), discard(process.stderr))
            try:
                await self._stop(process)
                await draining
            finally:
                if not draining.done(): draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)
        try:
            process = await asyncio.shield(starting)
        except asyncio.CancelledError:
            process = await self._drain_cleanup(starting)
            await self._drain_cleanup(asyncio.create_task(cleanup(process)))
            raise
        except OSError:
            raise PackageError('Commission the native plugin installer executable.') from None
        async def read(stream, limit):
            chunks = []; size = 0
            while data := await stream.read(65536):
                size += len(data)
                if size > limit: raise PackageError('The native installer exceeded its output allowance.')
                chunks.append(data)
            return b''.join(chunks)
        tasks.extend([asyncio.create_task(read(process.stdout, 2 * 1024**2)),
                      asyncio.create_task(read(process.stderr, 128 * 1024))])
        try:
            async with asyncio.timeout(self.timeout):
                output, _ = await asyncio.gather(*tasks)
                if await process.wait(): raise PackageError('Codex could not install this package. Check its published setup.')
                return json.loads(output)
        except (OSError, json.JSONDecodeError, TimeoutError):
            raise PackageError('The native plugin installer needs a fresh attempt.') from None
        finally:
            await self._drain_cleanup(asyncio.create_task(cleanup(process)))

    def _source(self, entry, snapshot, market):
        from native_install_source import source_spec, InstallSourceError
        try:
            spec = source_spec(entry, snapshot)
        except InstallSourceError as error:
            raise PackageError('Pin the published package source before installation.') from error
        if entry['catalogue_entry']['source'].get('source') == 'local':
            return {'source': 'git-subdir', 'url': 'https://github.com/' + spec.repository + '.git',
                    'path': spec.subtree, 'sha': spec.revision}
        return dict(entry.get('resolved_source') or entry['catalogue_entry']['source'])

    @asynccontextmanager
    async def acquire(self, entry, snapshot):
        with tempfile.TemporaryDirectory(prefix='rocoto-native-plugin-') as temporary:
            root = Path(temporary); home = root / 'home'; home.mkdir(mode=0o700)
            market = root / 'market'; (market / '.agents/plugins').mkdir(parents=True)
            row = dict(entry['catalogue_entry']); row.update(entry.get('manifest') or {})
            row['name'] = entry['id']; row['source'] = self._source(entry, snapshot, market)
            manifest = {'name': MARKETPLACE, 'plugins': [row]}
            (market / '.agents/plugins/marketplace.json').write_text(_json(manifest))
            await self._command(['plugin', 'marketplace', 'add', str(market), '--json'], home=home, cwd=market)
            result = await self._command(['plugin', 'add', entry['id'] + '@' + MARKETPLACE, '--json'], home=home, cwd=market)
            path = Path(result.get('installedPath', ''))
            cache = home / 'plugins/cache' / MARKETPLACE / entry['id']
            if (not path.is_absolute() or path.parent != cache
                    or result.get('name') != entry['id'] or result.get('marketplaceName') != MARKETPLACE):
                raise PackageError('The native installer returned a different package.')
            _ordinary_directory(path)
            installed = await self._command(['plugin', 'list', '--json'], home=home, cwd=market)
            rows = [item for item in installed.get('installed', []) if item.get('pluginId') == result.get('pluginId')]
            if len(rows) != 1 or not rows[0].get('installed') or not rows[0].get('enabled'):
                raise PackageError('The native package installation could not be verified.')
            self.last_inventory = {'runtime': 'codex-cli', 'pluginId': result.get('pluginId'),
                                   'version': result.get('version'), 'installed': True, 'enabled': True}
            yield path, self.last_inventory.copy()


class NativePackageStore:
    def __init__(self, state, *, catalogue_snapshot=None, materializer=None):
        self.state = _ordinary_directory(state, create=True)
        self.company_scope = workspace_scope(self.state)
        self.root = _ordinary_directory(self.state / 'native-packages', create=True)
        self.root.chmod(0o700)
        self.trees = _ordinary_directory(self.root / 'trees', create=True)
        self.snapshot_catalogue = json.loads(_json(catalogue() if catalogue_snapshot is None else catalogue_snapshot))
        if not re.fullmatch(r'[0-9a-f]{40}', self.snapshot_catalogue.get('revision', '')):
            raise PackageError('Select an immutable published catalogue revision.')
        self.entries = {row['id']: row for row in self.snapshot_catalogue['plugins']}
        if materializer is None:
            from hosted_install import HostedPluginInstaller
            materializer = HostedPluginInstaller()
        self.materializer = materializer
        self.database = self.root / 'selection.sqlite3'
        fd = os.open(self.database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600); os.close(fd)
        self.database.chmod(0o600)
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS owners(owner TEXT PRIMARY KEY,generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS packages(package_key TEXT PRIMARY KEY,body TEXT NOT NULL,bytes INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS selections(owner TEXT NOT NULL,id TEXT NOT NULL,package_key TEXT NOT NULL,
                    PRIMARY KEY(owner,id),FOREIGN KEY(package_key) REFERENCES packages(package_key));''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA max_page_count=32768')
            yield db
            db.commit()
        finally: db.close()

    @asynccontextmanager
    async def _lock(self):
        fd = os.open(self.root / 'mutation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + 180
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise PackageError('A plugin change is still finishing. Try again.')
                    await asyncio.sleep(.025)
            yield
        finally: os.close(fd)

    def _entry(self, identifier):
        entry = self.entries.get(_name(identifier))
        if not entry or not entry.get('supported_api'):
            raise PackageError('Choose a published API-supported plugin for native installation.')
        return entry

    def _rows(self, owner):
        with self._db() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT generation FROM owners WHERE owner=?', (owner,)).fetchone()
            rows = db.execute('SELECT s.id,p.body FROM selections s JOIN packages p ON p.package_key=s.package_key WHERE s.owner=? ORDER BY s.id', (owner,)).fetchall()
            return row['generation'] if row else 0, [json.loads(row['body']) for row in rows]

    @staticmethod
    def _public(body):
        return {key: body[key] for key in ('id', 'version', 'upstream_revision', 'source_url', 'license')} | {
            'installed': True, 'enabled': True, 'connected': False, 'callable': False,
            'runtime': 'openai_agents', 'package_sha256': body['tree_sha256']}

    def list(self, *, principal_id):
        generation, rows = self._rows(_principal(principal_id))
        return {'generation': generation, 'plugins': [self._public(row) for row in rows]}

    def read(self, identifier, *, principal_id):
        _name(identifier)
        row = next((row for row in self.list(principal_id=principal_id)['plugins'] if row['id'] == identifier), None)
        if row is None: raise PackageError('Install this plugin for the selected account.')
        return row

    def _verified(self, body):
        tree = body['tree_sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', tree): raise PackageError('Refresh the stored plugin package.')
        root = self.trees / tree
        files, actual, total, directories = _scan_tree(root)
        if actual != tree or [asdict(file) for file in files] != body['files'] or total != body['bytes'] or list(directories) != body['directories']:
            raise PackageError('The stored plugin package changed. Reinstall its verified version.')
        return PackageSnapshot(body['id'], body['version'], tree, root, files, directories,
            body['upstream_revision'], body['source_url'], body['license'])

    def _reconcile_trees(self):
        # Under the process-shared mutation lock: a crash before SQLite commit
        # may leave a sealed tree or partial copy without a selected package.
        with self._db() as db:
            known = {json.loads(row['body'])['tree_sha256'] for row in db.execute('SELECT body FROM packages')}
        for path in self.trees.iterdir():
            if path.name in known:
                continue
            if not (re.fullmatch(r'[0-9a-f]{64}', path.name) or re.fullmatch(r'\.copy-[0-9a-f]{32}', path.name)):
                raise PackageError('Review the private plugin storage before another install.')
            _ordinary_directory(path)
            _remove_private_tree(path)
        _fsync_dir(self.trees)

    async def install(self, identifier, *, principal_id):
        owner = _principal(principal_id); entry = self._entry(identifier)
        from native_install_source import source_spec, InstallSourceError
        try:
            spec = source_spec(entry, self.snapshot_catalogue)
        except InstallSourceError as error:
            raise PackageError('Pin the published package source before installation.') from error
        async with self._lock():
            self._reconcile_trees()
            _, rows = self._rows(owner)
            existing = next((row for row in rows if row['id'] == identifier), None)
            resolved_source = entry.get('resolved_source') or entry['catalogue_entry']['source']
            if (existing and existing['upstream_revision'] == spec.revision
                    and existing.get('catalogue_revision', existing['upstream_revision']) == self.snapshot_catalogue['revision']
                    and existing.get('source') == resolved_source):
                self._verified(existing)
                return self._public(existing)
            if len(rows) >= MAX_SELECTED and existing is None:
                raise PackageError('The account has reached its installed plugin allowance.')
            scoped=getattr(self.materializer,'acquire_for_principal',None)
            acquisition=(scoped(entry,self.snapshot_catalogue,principal_id=principal_id) if scoped else
                         self.materializer.acquire(entry,self.snapshot_catalogue))
            async with acquisition as (source, native):
                temporary = self.trees / ('.copy-' + uuid.uuid4().hex); temporary.mkdir(mode=0o700)
                try:
                    files, tree, total, directories = _scan_tree(source, destination=temporary)
                    # Exact copied bytes are verified again; manifest identity is authoritative.
                    manifest_path = temporary / '.codex-plugin/plugin.json'
                    if not any(row.path == '.codex-plugin/plugin.json' for row in files):
                        raise PackageError('The package needs its original Codex plugin manifest.')
                    manifest = json.loads(manifest_path.read_text())
                    version = manifest.get('version')
                    if manifest.get('name') != identifier or not isinstance(version, str) or not version or len(version) > 128:
                        raise PackageError('The package manifest identifies a different plugin.')
                    if native.get('version') != version:
                        raise PackageError('The native install version differs from its manifest.')
                    copied, copied_digest, _, copied_directories = _scan_tree(temporary)
                    if copied != files or copied_digest != tree or copied_directories != directories: raise PackageError('The copied plugin changed before publication.')
                    body = {'schema': _SCHEMA, 'id': identifier, 'version': version, 'tree_sha256': tree,
                        'files': [asdict(row) for row in files], 'directories': list(directories), 'bytes': total,
                        'upstream_revision': spec.revision, 'catalogue_revision': self.snapshot_catalogue['revision'],
                        'source_url': entry.get('source_url') or self.snapshot_catalogue['source_url'],
                        'license': manifest.get('license') if isinstance(manifest.get('license'), str) else None,
                        'source': resolved_source}
                    package_key = _hash(_json(body).encode())
                    destination = self.trees / tree
                    with self._db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        retained = [json.loads(row['body']) for row in db.execute('SELECT body FROM packages')]
                        sizes = {row['tree_sha256']: row['bytes'] for row in retained}
                        used = sum(sizes.values())
                        known = db.execute('SELECT body FROM packages WHERE package_key=?', (package_key,)).fetchone()
                        if not known and len(retained) >= MAX_PACKAGE_RECORDS:
                            raise PackageError('The company package history allowance is full.')
                        if tree not in sizes and used + total > MAX_STORE_BYTES:
                            raise PackageError('The company plugin storage allowance is full.')
                        if destination.exists():
                            self._verified(body)
                        else:
                            for directory, _, _ in os.walk(temporary, topdown=False): os.chmod(directory, 0o500)
                            temporary.rename(destination); _fsync_dir(self.trees)
                        if known and json.loads(known['body']) != body:
                            raise PackageError('The stored package provenance differs. Review its pinned revision.')
                        db.execute('INSERT OR IGNORE INTO packages VALUES(?,?,?)', (package_key, _json(body), total))
                        db.execute('INSERT INTO owners VALUES(?,1) ON CONFLICT(owner) DO UPDATE SET generation=generation+1', (owner,))
                        db.execute('INSERT INTO selections VALUES(?,?,?) ON CONFLICT(owner,id) DO UPDATE SET package_key=excluded.package_key', (owner, identifier, package_key))
                    return self._public(body)
                except (json.JSONDecodeError, UnicodeError):
                    raise PackageError('The package manifest needs valid UTF-8 JSON.') from None
                finally: _remove_private_tree(temporary)

    async def uninstall(self, identifier, *, principal_id):
        owner = _principal(principal_id); _name(identifier)
        async with self._lock():
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                count = db.execute('DELETE FROM selections WHERE owner=? AND id=?', (owner, identifier)).rowcount
                if count:
                    db.execute('UPDATE owners SET generation=generation+1 WHERE owner=?', (owner,))
            return {'id': identifier, 'installed': False, 'enabled': False, 'connected': False, 'callable': False}

    def snapshot(self, *, principal_id, plugin_ids=None):
        owner = _principal(principal_id); generation, rows = self._rows(owner)
        if plugin_ids is not None:
            if not isinstance(plugin_ids, (list, tuple)) or len(plugin_ids) > MAX_SELECTED:
                raise PackageError('Choose the installed plugins for this chat.')
            selected = {_name(identifier) for identifier in plugin_ids}
            if not selected <= {row['id'] for row in rows}: raise PackageError('Install the selected plugins for this account.')
            rows = [row for row in rows if row['id'] in selected]
        packages = tuple(self._verified(row) for row in rows)
        if self._rows(owner)[0] != generation: raise PackageError('Refresh this account’s installed plugins.')
        digest = _hash(_json({'company': self.company_scope, 'owner': owner, 'generation': generation,
            'packages': [(row.id, row.version, row.tree_sha256) for row in packages]}).encode())
        return RuntimePackages(self.company_scope, owner, generation, digest, packages)

    @asynccontextmanager
    async def runtime_selection(self, *, principal_id):
        """Prevent install/removal racing a bounded native worker start."""
        async with self._lock():
            selected = await _read_packages(self.snapshot, principal_id=principal_id)
            yield selected

    async def validate_runtime_snapshot(self, selection, *, principal_id):
        """Recheck the selected package bytes inside the held runtime lock."""
        return await _read_packages(self.validate_snapshot, selection, principal_id=principal_id)

    def validate_snapshot(self, selection, *, principal_id):
        if not isinstance(selection, RuntimePackages) or selection.company_scope != self.company_scope or selection.owner_binding != _principal(principal_id):
            raise PackageError('Use the selected account’s plugin snapshot.')
        current = self.snapshot(principal_id=principal_id, plugin_ids=[row.id for row in selection.packages])
        if current != selection:
            raise PackageError('Refresh the plugin selection before starting the worker.')
        return current
