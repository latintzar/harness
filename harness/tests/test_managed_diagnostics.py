"""Private managed failures retain protocol evidence without customer/provider text."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from managed_codex import ManagedCodexError
from managed_diagnostics import record_failure


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def saved(state):
    files = list((state / 'runtime-private').glob('managed-failure-*.json'))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o077 == 0
    assert files[0].parent.stat().st_mode & 0o077 == 0
    assert files[0].stat().st_size < 4096
    return files[0], json.loads(files[0].read_text())


@pytest.mark.anyio
@pytest.mark.parametrize('phase', ['recovery', 'run'])
async def test_bridge_keeps_safe_private_provider_failure_and_generic_public_error(tmp_path, phase):
    from agent_bridge import HarnessAgent
    failure = ManagedCodexError('http_error', status_code=400, request_id='req_fixture-123')
    async def recover(*args):
        if phase == 'recovery':
            raise failure
        if False:
            yield
    async def run(*args, **kwargs):
        raise failure
        yield
    agent = SimpleNamespace(state=tmp_path, children={}, managed_runtime=SimpleNamespace(recover=recover, run=run))
    events = [event async for event in HarnessAgent._turn(agent, 'private input', 'private-chat', 'company')]
    assert events == [{'type': 'error', 'message': 'This managed turn needs review before it can continue.'}]
    path, value = saved(tmp_path)
    assert value['phase'] == phase
    assert value['exception_type'] == 'ManagedCodexError'
    assert value['provider'] == {'code': 'http_error', 'status_code': 400, 'request_id': 'req_fixture-123'}
    assert value['source'] == Path(__file__).name and value['line'] > 0
    assert all(secret not in path.read_text() + path.name for secret in ('private input', 'private-chat'))
    assert agent.children == {}


def test_literal_error_is_retained_but_dynamic_provider_text_is_excluded(tmp_path):
    try:
        raise ValueError('Use the original managed turn.')
    except ValueError as error:
        assert record_failure(tmp_path, 'chat', error, phase='run')
    _, value = saved(tmp_path)
    assert value['message'] == 'Use the original managed turn.'
    secret = 'sk-private-fixture'
    try:
        raise ValueError('Provider echoed ' + secret)
    except ValueError as error:
        assert record_failure(tmp_path, 'chat', error, phase='recovery')
    path, value = saved(tmp_path)
    assert 'message' not in value and secret not in path.read_text()
    assert value['phase'] == 'recovery'


def test_untrusted_code_and_request_id_and_symlink_directory_are_excluded(tmp_path):
    error = ManagedCodexError('private provider content', status_code=400, request_id='Bearer secret')
    assert record_failure(tmp_path, 'chat', error, phase='run')
    _, value = saved(tmp_path)
    assert value['provider'] == {'status_code': 400}
    other = tmp_path / 'other'
    other.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (other / 'runtime-private').symlink_to(outside, target_is_directory=True)
    assert record_failure(other, 'chat', error, phase='run') is False
    assert list(outside.iterdir()) == []
