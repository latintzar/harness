"""On-demand compute for the managed Codex harness; never executes code on the host.

The trusted provisioning service owns this module and its private state. Provider
session control, MCP grants and usage admission remain in the application. Each
work lease covers one root turn AND its children; release only after reconciliation.
See deploy/MANAGED-EXECUTOR.md for the commissioning boundary and upstream contract.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable, Protocol
from urllib.parse import urlsplit


class ExecutorError(RuntimeError):
    """Stable categories only: process output and credentials stay private."""


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError('A bounded nonempty identifier is required')


@dataclass(frozen=True)
class ExecutorScope:
    company_id: str
    owner_id: str
    root_chat_id: str

    def __post_init__(self):
        for value in asdict(self).values():
            _identifier(value)

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:40]


@dataclass(frozen=True)
class ExecutorBinding:
    session_id: str
    environment_id: str
    remote_url: str = field(repr=False)

    def __post_init__(self):
        _identifier(self.session_id)
        _identifier(self.environment_id)
        if not isinstance(self.remote_url, str) or len(self.remote_url) > 4096 or any(ord(c) <= 32 for c in self.remote_url):
            raise ValueError('An API-issued executor URL is required')
        url = urlsplit(self.remote_url)
        if (url.scheme != 'https' or url.hostname not in {'api.openai.com', 'codex-cloud-environments.chatgpt.com'}
                or url.username or url.password or url.port not in (None, 443) or url.fragment):
            raise ValueError('An approved API-issued executor URL is required')


@dataclass(frozen=True)
class ExecutorSpec:
    scope_key: str
    binding: ExecutorBinding

    @property
    def container_name(self) -> str:
        return 'rocoto-executor-' + self.scope_key

    @property
    def volume_name(self) -> str:
        return 'rocoto-workspace-' + self.scope_key

    @property
    def binding_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self.binding), sort_keys=True).encode()).hexdigest()


class ExecutorBackend(Protocol):
    def inspect(self, spec: ExecutorSpec) -> bool: ...
    def start(self, spec: ExecutorSpec) -> None: ...
    def stop(self, spec: ExecutorSpec) -> None: ...


class ExecutorPool:
    """A durable local pool controller. One private directory per worker host.

    Call begin_work BEFORE sending provider input. Reconcile provider root/child
    terminals before complete_work. Provider cancellation and grant revocation must
    precede stop; stopping this executor only guarantees termination of local code.
    The file lock covers admission and idle removal, including across processes.
    """

    def __init__(self, state: Path, backend: ExecutorBackend, *, max_active: int = 2,
                 idle_grace_seconds: float = 60, clock: Callable[[], float] = time.time):
        if max_active < 1 or idle_grace_seconds < 0:
            raise ValueError('Positive capacity and a nonnegative idle grace are required')
        self.state = Path(state)
        if self.state.is_symlink():
            raise ValueError('Executor state must be a private directory')
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state.stat().st_mode & 0o077:
            raise ValueError('Executor state directory must have mode 0700')
        self.backend, self.max_active = backend, max_active
        self.idle_grace_seconds, self.clock = idle_grace_seconds, clock

    @contextmanager
    def _locked(self):
        fd = os.open(self.state / 'executors.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self.state / 'executors.json'
            if path.is_symlink():
                raise ExecutorError('invalid_state')
            try:
                data = json.loads(path.read_text()) if path.exists() else {'version': 1, 'executors': {}}
            except (OSError, ValueError):
                raise ExecutorError('invalid_state') from None
            if data.get('version') != 1 or not isinstance(data.get('executors'), dict):
                raise ExecutorError('invalid_state')
            yield data
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _save(self, data):
        fd, name = tempfile.mkstemp(prefix='.executors-', dir=self.state)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state / 'executors.json')
            directory = os.open(self.state, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _view(entry):
        if entry is None:
            return None
        return {key: entry[key] for key in ('status', 'generation', 'work_ids', 'idle_since', 'started_at')}

    @staticmethod
    def _spec(key, entry):
        return ExecutorSpec(key, ExecutorBinding(**entry['binding']))

    def snapshot(self, scope: ExecutorScope):
        with self._locked() as data:
            return self._view(data['executors'].get(scope.key))

    def begin_work(self, scope: ExecutorScope, binding: ExecutorBinding, work_id: str, *, resume=False):
        _identifier(work_id)
        with self._locked() as data:
            entries = data['executors']
            entry = entries.get(scope.key)
            if any(key != scope.key and (item['binding']['session_id'] == binding.session_id or
                                        item['binding']['environment_id'] == binding.environment_id)
                   for key, item in entries.items()):
                raise ExecutorError('binding_owned_by_other_scope')
            if entry and entry['binding'] != asdict(binding):
                raise ExecutorError('binding_mismatch')
            if entry and entry['status'] == 'stopping':
                raise ExecutorError('stop_incomplete')
            if entry and entry['status'] == 'stopped' and not resume:
                raise ExecutorError('resume_required')
            if entry and work_id in entry['retired_work_ids']:
                raise ExecutorError('work_already_terminal')
            spec = ExecutorSpec(scope.key, binding)
            running = self.backend.inspect(spec)
            if not running:
                active = sum(1 for key, item in entries.items() if key != scope.key and
                             (item['status'] in {'starting', 'stopping'} or self.backend.inspect(self._spec(key, item))))
                if active >= self.max_active:
                    raise ExecutorError('capacity_reached')
            if entry is None:
                entry = {'binding': asdict(binding), 'status': 'idle', 'generation': 0,
                         'work_ids': [], 'retired_work_ids': [], 'idle_since': None, 'started_at': None}
                entries[scope.key] = entry
            if len(entry['work_ids']) >= 128 and work_id not in entry['work_ids']:
                raise ExecutorError('work_limit_reached')
            if work_id not in entry['work_ids']:
                entry['work_ids'].append(work_id)
                entry['generation'] += 1
            entry['idle_since'] = None
            entry['status'] = 'running' if running else 'starting'
            self._save(data)  # Crash recovery can adopt the deterministic container.
            if not running:
                try:
                    self.backend.start(spec)
                except ExecutorError:
                    entry['status'] = 'start_failed'
                    self._save(data)
                    raise
                entry['started_at'] = self.clock()
            entry['status'] = 'running'
            self._save(data)
            return self._view(entry)

    def complete_work(self, scope: ExecutorScope, work_id: str, *, all_children_terminal: bool):
        _identifier(work_id)
        if not all_children_terminal:
            raise ExecutorError('tree_not_terminal')
        with self._locked() as data:
            entry = data['executors'].get(scope.key)
            if entry is None:
                return None
            if work_id in entry['work_ids']:
                entry['work_ids'].remove(work_id)
                entry['retired_work_ids'] = (entry['retired_work_ids'] + [work_id])[-2048:]
                entry['generation'] += 1
                if not entry['work_ids']:
                    entry['idle_since'] = self.clock()
                self._save(data)
            return self._view(entry)

    def stop(self, scope: ExecutorScope):
        with self._locked() as data:
            entry = data['executors'].get(scope.key)
            if entry is None:
                return None
            entry['status'] = 'stopping'
            entry['generation'] += 1
            entry['retired_work_ids'] = (entry['retired_work_ids'] + entry['work_ids'])[-2048:]
            entry['work_ids'] = []
            entry['idle_since'] = None
            self._save(data)  # A failed removal still blocks renewed work admission.
            self.backend.stop(self._spec(scope.key, entry))
            entry['status'] = 'stopped'
            self._save(data)
            return self._view(entry)

    def reap_idle(self, scope: ExecutorScope, *, expected_generation: int) -> bool:
        with self._locked() as data:
            entry = data['executors'].get(scope.key)
            if (not entry or entry['generation'] != expected_generation or entry['work_ids']
                    or entry['status'] not in {'running', 'idle'} or entry['idle_since'] is None
                    or self.clock() - entry['idle_since'] < self.idle_grace_seconds):
                return False
            self.backend.stop(self._spec(scope.key, entry))
            entry['status'] = 'idle'
            entry['generation'] += 1
            self._save(data)
            return True


@dataclass(frozen=True)
class DockerExecutorConfig:
    image: str
    network: str
    executable: str = '/usr/bin/docker'
    memory_bytes: int = 1024 ** 3
    cpus: float = 1.0
    pids: int = 128
    workspace_bytes: int = 2 * 1024 ** 3
    max_lifetime_seconds: int = 1800
    uid: int = 10001
    gid: int = 10001

    def __post_init__(self):
        if not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}', self.image):
            raise ValueError('A digest-pinned executor image is required')
        if not re.fullmatch(r'rocoto-executor-[a-z0-9-]+', self.network):
            raise ValueError('A commissioned executor network is required')
        if (not Path(self.executable).is_absolute() or not 128 * 1024 ** 2 <= self.memory_bytes <= 4 * 1024 ** 3
                or not math.isfinite(self.cpus) or not 0 < self.cpus <= 4 or not 8 <= self.pids <= 512
                or not 128 * 1024 ** 2 <= self.workspace_bytes <= 10 * 1024 ** 3
                or not 30 <= self.max_lifetime_seconds <= 3600 or self.uid <= 0 or self.gid <= 0):
            raise ValueError('Bounded executor resources and an unprivileged user are required')


class DockerExecutor:
    """Trusted broker adapter. Requires gVisor and separately enforced egress policy.

    Network labels are deployment attestations, not a firewall. Commissioning must
    prove the actual packet policy. Docker daemon access belongs only to this broker.
    """

    def __init__(self, config: DockerExecutorConfig, *, environment_key: Callable[[], str],
                 provision_workspace: Callable[[ExecutorSpec], None] | None = None, run=subprocess.run):
        self.config, self.environment_key, self.run = config, environment_key, run
        self.provision_workspace = provision_workspace

    def _command(self, args, *, key=None):
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        if key:
            env['CODEX_API_KEY'] = key
        try:
            result = self.run([self.config.executable, *args], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ExecutorError('executor_command_failed') from None
        if result.returncode:
            raise ExecutorError('executor_command_failed')
        return result.stdout

    def _json(self, args, **kwargs):
        try:
            return json.loads(self._command(args, **kwargs))
        except (TypeError, ValueError):
            raise ExecutorError('invalid_executor_response') from None

    def _key(self):
        key = self.environment_key()
        if not isinstance(key, str) or not key.strip() or any(c.isspace() for c in key) or len(key) > 4096:
            raise ExecutorError('environment_key_required')
        return key

    def preflight(self):
        self._key()
        runtimes = self._json(['info', '--format', '{{json .Runtimes}}'])
        if not isinstance(runtimes, dict) or 'runsc' not in runtimes:
            raise ExecutorError('isolated_runtime_required')
        networks = self._json(['network', 'inspect', self.config.network])
        if (not isinstance(networks, list) or len(networks) != 1
                or networks[0].get('Name') != self.config.network
                or (networks[0].get('Labels') or {}).get('com.rocoto.executor.egress') != 'restricted-v1'):
            raise ExecutorError('restricted_egress_required')
        digests = self._json(['image', 'inspect', '--format', '{{json .RepoDigests}}', self.config.image])
        if not isinstance(digests, list) or self.config.image not in digests:
            raise ExecutorError('pinned_executor_image_required')

    def check_workspace(self, spec):
        rows = self._json(['volume', 'inspect', spec.volume_name])
        if not isinstance(rows, list) or len(rows) != 1:
            raise ExecutorError('workspace_provisioning_required')
        labels = rows[0].get('Labels') or {}
        if (rows[0].get('Name') != spec.volume_name
                or labels.get('com.rocoto.executor.scope') != spec.scope_key
                or labels.get('com.rocoto.executor.quota') != 'enforced-v1'
                or labels.get('com.rocoto.executor.quota_bytes') != str(self.config.workspace_bytes)):
            raise ExecutorError('workspace_provisioning_required')

    def _container(self, spec):
        listed = self._command(['container', 'ls', '--all', '--filter',
                                f'name=^{spec.container_name}$', '--format', '{{json .}}'])
        if not listed.strip():
            return None
        result = self._json(['container', 'inspect', spec.container_name])
        if not isinstance(result, list):
            raise ExecutorError('invalid_executor_response')
        if not result:
            return None
        if len(result) != 1:
            raise ExecutorError('executor_identity_mismatch')
        row = result[0]
        labels = row.get('Config', {}).get('Labels') or {}
        if labels.get('com.rocoto.executor.binding') != spec.binding_hash or labels.get('com.rocoto.executor.scope') != spec.scope_key:
            raise ExecutorError('executor_identity_mismatch')
        return row

    def inspect(self, spec):
        row = self._container(spec)
        return bool(row and row.get('State', {}).get('Running'))

    def start(self, spec):
        self.preflight()
        if self.provision_workspace is not None:
            self.provision_workspace(spec)
        self.check_workspace(spec)
        existing = self._container(spec)
        if existing and existing.get('State', {}).get('Running'):
            return
        if existing:
            self.stop(spec)
        config = self.config
        args = ['create', '--name', spec.container_name, '--runtime', 'runsc', '--network', config.network,
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
                '--user', f'{config.uid}:{config.gid}', '--pids-limit', str(config.pids),
                '--memory', str(config.memory_bytes), '--memory-swap', str(config.memory_bytes),
                '--cpus', str(config.cpus), '--restart', 'no', '--init', '--log-driver', 'none',
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=134217728,mode=1777',
                '--mount', f'type=volume,source={spec.volume_name},target=/workspace',
                '--workdir', '/workspace', '--env', 'CODEX_API_KEY', '--env', 'HOME=/tmp',
                '--env', 'CODEX_HOME=/tmp/.codex',
                '--label', f'com.rocoto.executor.scope={spec.scope_key}',
                '--label', f'com.rocoto.executor.binding={spec.binding_hash}',
                '--entrypoint', '/usr/bin/env', config.image,
                'timeout', '--signal=TERM', '--kill-after=10s', str(config.max_lifetime_seconds),
                'codex', 'exec-server', '--remote', spec.binding.remote_url,
                '--environment-id', spec.binding.environment_id]
        self._command(args, key=self._key())
        self._command(['container', 'start', spec.container_name])

    def stop(self, spec):
        if self._container(spec):
            self._command(['container', 'rm', '--force', spec.container_name])
