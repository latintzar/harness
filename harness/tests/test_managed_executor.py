"""Executor lifecycle tests use isolated fakes; they never create provider sessions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from deploy.migration.legacy.managed_executor import (
    ExecutorBinding, ExecutorScope, ExecutorError, ExecutorPool,
    DockerExecutor, DockerExecutorConfig,
)


SCOPE = ExecutorScope('company-a', 'alice', 'chat-a')
BINDING = ExecutorBinding('session-a', 'environment-a', 'https://api.openai.com/v1/agents/environments')


class FakeBackend:
    def __init__(self):
        self.running = set()
        self.starts = []
        self.stops = []
        self.files = {}
        self.fail_start = False

    def inspect(self, spec):
        return spec.container_name in self.running

    def start(self, spec):
        self.starts.append(spec)
        if self.fail_start:
            raise ExecutorError('launch_failed')
        self.running.add(spec.container_name)
        self.files.setdefault(spec.volume_name, {})

    def stop(self, spec):
        self.stops.append(spec)
        self.running.discard(spec.container_name)


def pool(tmp_path, backend=None, clock=None, **kwargs):
    return ExecutorPool(tmp_path, backend or FakeBackend(), clock=clock or (lambda: 1000.0), **kwargs)


def test_one_executor_per_scope_concurrent_input_and_restart_preserve_files(tmp_path):
    backend = FakeBackend()
    first = pool(tmp_path, backend)
    with ThreadPoolExecutor(max_workers=6) as workers:
        states = list(workers.map(lambda n: first.begin_work(SCOPE, BINDING, f'work-{n}'), range(6)))
    assert len(backend.starts) == 1
    spec = backend.starts[0]
    backend.files[spec.volume_name]['report.docx'] = b'document'
    assert len(first.snapshot(SCOPE)['work_ids']) == 6
    restarted = pool(tmp_path, backend)
    restarted.begin_work(SCOPE, BINDING, 'work-0')
    assert len(backend.starts) == 1
    restarted.stop(SCOPE)
    assert backend.running == set()
    assert restarted.snapshot(SCOPE)['work_ids'] == []
    with pytest.raises(ExecutorError, match='resume_required'):
        restarted.begin_work(SCOPE, BINDING, 'new-work')
    restarted.begin_work(SCOPE, BINDING, 'new-work', resume=True)
    assert backend.starts[-1].volume_name == spec.volume_name
    assert backend.files[spec.volume_name]['report.docx'] == b'document'
    assert restarted.snapshot(SCOPE)['generation'] > states[0]['generation']


def test_idle_release_requires_whole_turn_terminal_and_rejects_stale_generation(tmp_path):
    now = [1000.0]
    backend = FakeBackend()
    launcher = pool(tmp_path, backend, lambda: now[0], idle_grace_seconds=30)
    launcher.begin_work(SCOPE, BINDING, 'root-turn')
    with pytest.raises(ExecutorError, match='tree_not_terminal'):
        launcher.complete_work(SCOPE, 'root-turn', all_children_terminal=False)
    state = launcher.complete_work(SCOPE, 'root-turn', all_children_terminal=True)
    assert not launcher.reap_idle(SCOPE, expected_generation=state['generation'])
    now[0] += 31
    launcher.begin_work(SCOPE, BINDING, 'new-input')
    assert not launcher.reap_idle(SCOPE, expected_generation=state['generation'])
    state = launcher.complete_work(SCOPE, 'new-input', all_children_terminal=True)
    now[0] += 31
    assert launcher.reap_idle(SCOPE, expected_generation=state['generation'])
    assert launcher.snapshot(SCOPE)['status'] == 'idle'
    launcher.begin_work(SCOPE, BINDING, 'later-input')
    assert len(backend.starts) == 2


def test_cross_owner_and_environment_rebind_are_distinct_and_capacity_is_bounded(tmp_path):
    backend = FakeBackend()
    launcher = pool(tmp_path, backend, max_active=1)
    launcher.begin_work(SCOPE, BINDING, 'a')
    with pytest.raises(ExecutorError, match='binding_mismatch'):
        launcher.begin_work(SCOPE, ExecutorBinding('other', 'other', BINDING.remote_url), 'b')
    other = ExecutorScope('company-b', 'bob', 'chat-a')
    other_binding = ExecutorBinding('session-b', 'environment-b', BINDING.remote_url)
    assert launcher.snapshot(other) is None
    with pytest.raises(ExecutorError, match='capacity_reached'):
        launcher.begin_work(other, other_binding, 'b')
    launcher.stop(SCOPE)
    launcher.begin_work(other, other_binding, 'b')
    assert backend.starts[0].volume_name != backend.starts[-1].volume_name


def test_launch_failure_is_retryable_and_reconciliation_does_not_drop_work(tmp_path):
    backend = FakeBackend()
    backend.fail_start = True
    launcher = pool(tmp_path, backend)
    with pytest.raises(ExecutorError, match='launch_failed'):
        launcher.begin_work(SCOPE, BINDING, 'work')
    assert launcher.snapshot(SCOPE)['work_ids'] == ['work']
    backend.fail_start = False
    launcher.begin_work(SCOPE, BINDING, 'work')
    backend.running.clear()  # Worker was killed; the same binding/workspace reconnects.
    launcher.begin_work(SCOPE, BINDING, 'work')
    assert len({spec.volume_name for spec in backend.starts}) == 1
    assert launcher.snapshot(SCOPE)['work_ids'] == ['work']


def test_stop_tombstone_survives_failed_container_removal(tmp_path):
    backend = FakeBackend()
    launcher = pool(tmp_path, backend)
    launcher.begin_work(SCOPE, BINDING, 'work')
    original_stop = backend.stop
    backend.stop = lambda spec: (_ for _ in ()).throw(ExecutorError('stop_failed'))
    with pytest.raises(ExecutorError, match='stop_failed'):
        launcher.stop(SCOPE)
    assert launcher.snapshot(SCOPE)['status'] == 'stopping'
    with pytest.raises(ExecutorError, match='stop_incomplete'):
        launcher.begin_work(SCOPE, BINDING, 'new', resume=True)
    backend.stop = original_stop
    launcher.stop(SCOPE)
    assert launcher.snapshot(SCOPE)['status'] == 'stopped'


def test_private_state_and_binding_validation(tmp_path):
    launcher = pool(tmp_path)
    launcher.begin_work(SCOPE, BINDING, 'work')
    assert (tmp_path / 'executors.json').stat().st_mode & 0o077 == 0
    assert 'remote_url' not in launcher.snapshot(SCOPE)
    for remote in ['http://api.openai.com/x', 'https://evil.example/x', 'https://api.openai.com@evil.example/x', 'https://api.openai.com/x\n']:
        with pytest.raises(ValueError):
            ExecutorBinding('session', 'env', remote)
    with pytest.raises(ValueError):
        ExecutorScope('company', 'alice', '')
    with pytest.raises(ValueError):
        launcher.begin_work(SCOPE, BINDING, '')


def test_docker_launch_pins_isolation_and_never_places_key_in_arguments(tmp_path):
    calls = []
    secret = 'restricted-executor-key'
    image = 'registry.example/rocoto-executor@sha256:' + 'a' * 64
    config = DockerExecutorConfig(image=image, network='rocoto-executor-egress')
    container = []
    def command(args, **kwargs):
        calls.append((args, kwargs))
        if args[1:3] == ['container', 'ls']:
            return subprocess.CompletedProcess(args, 0, '{"ID":"container"}' if container else '', '')
        if args[1:3] == ['container', 'inspect']:
            return subprocess.CompletedProcess(args, 0, json.dumps(container), '')
        if args[1] == 'create':
            labels = dict(args[i + 1].split('=', 1) for i, value in enumerate(args) if value == '--label')
            container.append({'Config': {'Labels': labels}, 'State': {'Running': True}})
        if args[1] == 'info':
            value = {'runsc': {}}
        elif args[1:3] == ['network', 'inspect']:
            value = [{'Name': config.network, 'Labels': {'com.rocoto.executor.egress': 'restricted-v1'}}]
        elif args[1:3] == ['volume', 'inspect']:
            value = [{'Name': args[3], 'Labels': {'com.rocoto.executor.scope': SCOPE.key,
                      'com.rocoto.executor.quota': 'enforced-v1',
                      'com.rocoto.executor.quota_bytes': str(config.workspace_bytes)}}]
        elif args[1:3] == ['image', 'inspect']:
            value = [image]
        else:
            value = ''
        return subprocess.CompletedProcess(args, 0, json.dumps(value) if not isinstance(value, str) else value, '')
    backend = DockerExecutor(config, environment_key=lambda: secret, run=command)
    launcher = pool(tmp_path, backend)
    launcher.begin_work(SCOPE, BINDING, 'work')
    args, options = next((a, o) for a, o in calls if a[1] == 'create')
    assert secret not in repr(args)
    assert options['env']['CODEX_API_KEY'] == secret
    assert set(options['env']) <= {'PATH', 'LANG', 'CODEX_API_KEY'}
    assert args[args.index('--runtime') + 1] == 'runsc'
    for flag in ['--read-only', '--cap-drop', '--security-opt', '--pids-limit', '--memory', '--cpus', '--user']:
        assert flag in args
    assert args[args.index('timeout') + 1:args.index('timeout') + 4] == ['--signal=TERM', '--kill-after=10s', '1800']
    assert args[-6:] == ['codex', 'exec-server', '--remote', BINDING.remote_url, '--environment-id', BINDING.environment_id]
    assert '/var/run/docker.sock' not in repr(args)
    launcher.stop(SCOPE)
    stop_args = next(a for a, _ in calls if a[1:3] == ['container', 'rm'])
    assert '--force' in stop_args and '--volumes' not in stop_args


def test_docker_requires_commissioned_runtime_egress_image_and_key():
    image = 'registry.example/rocoto-executor@sha256:' + 'b' * 64
    with pytest.raises(ValueError):
        DockerExecutorConfig(image='registry.example/image:latest', network='host')
    backend = DockerExecutor(DockerExecutorConfig(image=image, network='rocoto-executor-egress'), environment_key=lambda: '')
    with pytest.raises(ExecutorError, match='environment_key_required'):
        backend.preflight()
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, '{}', '')
    backend = DockerExecutor(DockerExecutorConfig(image=image, network='rocoto-executor-egress'), environment_key=lambda: 'key', run=run)
    with pytest.raises(ExecutorError, match='isolated_runtime_required'):
        backend.preflight()


def test_docker_sanitizes_failures_without_leaking_environment_key():
    image = 'registry.example/executor@sha256:' + 'c' * 64
    def run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr='secret-key credential')
    backend = DockerExecutor(DockerExecutorConfig(image=image, network='rocoto-executor-egress'), environment_key=lambda: 'secret-key', run=run)
    with pytest.raises(ExecutorError) as error:
        backend.preflight()
    assert 'secret-key' not in str(error.value)
    assert error.value.__cause__ is None


def test_provider_binding_cannot_be_adopted_by_another_owner(tmp_path):
    launcher = pool(tmp_path)
    launcher.begin_work(SCOPE, BINDING, 'work')
    launcher.stop(SCOPE)
    with pytest.raises(ExecutorError, match='binding_owned_by_other_scope'):
        launcher.begin_work(ExecutorScope('company-a', 'bob', 'chat-a'), BINDING, 'other')


def test_daemon_failure_is_not_treated_as_missing_container():
    image = 'registry.example/executor@sha256:' + 'd' * 64
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, '', 'daemon unavailable')
    backend = DockerExecutor(DockerExecutorConfig(image=image, network='rocoto-executor-egress'), environment_key=lambda: 'key', run=run)
    from deploy.migration.legacy.managed_executor import ExecutorSpec
    with pytest.raises(ExecutorError, match='executor_command_failed'):
        backend.stop(ExecutorSpec(SCOPE.key, BINDING))


def test_docker_requires_durable_quota_provisioned_workspace():
    image = 'registry.example/executor@sha256:' + 'e' * 64
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, '[]', '')
    backend = DockerExecutor(DockerExecutorConfig(image=image, network='rocoto-executor-egress'), environment_key=lambda: 'key', run=run)
    from deploy.migration.legacy.managed_executor import ExecutorSpec
    with pytest.raises(ExecutorError, match='workspace_provisioning_required'):
        backend.check_workspace(ExecutorSpec(SCOPE.key, BINDING))


@pytest.mark.parametrize('broken', ['network', 'image'])
def test_docker_preflight_rejects_uncommissioned_assets(broken):
    image = 'registry.example/executor@sha256:' + 'f' * 64
    def run(args, **kwargs):
        if args[1] == 'info':
            value = {'runsc': {}}
        elif args[1:3] == ['network', 'inspect']:
            value = [{'Name': 'rocoto-executor-egress', 'Labels': {
                'com.rocoto.executor.egress': 'unchecked' if broken == 'network' else 'restricted-v1'}}]
        else:
            value = []
        return subprocess.CompletedProcess(args, 0, json.dumps(value), '')
    backend = DockerExecutor(DockerExecutorConfig(image=image, network='rocoto-executor-egress'),
                             environment_key=lambda: 'key', run=run)
    with pytest.raises(ExecutorError, match='restricted_egress_required' if broken == 'network' else 'pinned_executor_image_required'):
        backend.preflight()


def test_retired_work_stays_terminal_and_idle_clock_regression_keeps_files(tmp_path):
    now = [1000.0]
    backend = FakeBackend()
    launcher = pool(tmp_path, backend, lambda: now[0])
    launcher.begin_work(SCOPE, BINDING, 'finished')
    state = launcher.complete_work(SCOPE, 'finished', all_children_terminal=True)
    with pytest.raises(ExecutorError, match='work_already_terminal'):
        launcher.begin_work(SCOPE, BINDING, 'finished')
    now[0] -= 100
    assert not launcher.reap_idle(SCOPE, expected_generation=state['generation'])
    assert backend.running


def test_executor_refuses_corrupted_state_and_symlinked_private_record(tmp_path):
    launcher = pool(tmp_path)
    record = tmp_path / 'executors.json'
    record.write_text('{broken')
    with pytest.raises(ExecutorError, match='invalid_state'):
        launcher.begin_work(SCOPE, BINDING, 'work')
    record.unlink()
    target = tmp_path / 'other.json'
    target.write_text('{}')
    record.symlink_to(target)
    with pytest.raises(ExecutorError, match='invalid_state'):
        launcher.begin_work(SCOPE, BINDING, 'work')
    assert target.read_text() == '{}'
