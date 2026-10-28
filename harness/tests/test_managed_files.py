"""Real retained bytes from a synthetic stock artifact transport."""
import copy
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from chat_files import ChatFiles
from workspace_identity import workspace_scope
from managed_codex import ManagedCodexClient, ManagedCodexError
from managed_sessions import ManagedSessions


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def fixture(tmp_path):
    ledger = ManagedSessions(tmp_path)
    binding = ledger.binding(company_id=workspace_scope(tmp_path), chat_id='chat', owner='alice',
        collection='company', configuration={'environment': {'type': 'openai_hosted'}})
    ledger.created(binding['id'], 'session-1')
    binding = ledger.get(binding['id'])
    files = ChatFiles(tmp_path)
    agent = SimpleNamespace(state=tmp_path, file_store=files,
        children={'chat': {'id': 'chat', 'owner_principal_id': 'alice', 'dataset': 'company'}})
    raw = b'name,total\nRetail,42\n'
    artifact = dict(id='artifact-1', created_at=1, environment_id='environment-1',
        object='agent.session.artifact', path='/workspace/outputs/report.csv',
        session_id='session-1', size_bytes=len(raw), turn_id='turn-1')
    requests = []
    current = [True]
    async def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith('/content'):
            return httpx.Response(200, content=raw)
        if path.endswith('/artifacts/artifact-1'):
            return httpx.Response(200, json=artifact)
        if path.endswith('/artifacts'):
            return httpx.Response(200, json={'data': [artifact], 'has_more': False})
        if path.endswith('/turns/turn-1'):
            return httpx.Response(200, json={'id': 'turn-1', 'session_id': 'session-1',
                'subagent_id': None, 'status': 'completed'})
        return httpx.Response(200, json={'id': 'session-1', 'agent': {'id': 'root-1'},
            'environment': {'id': 'environment-1', 'type': 'openai_hosted'}})
    return ledger, binding, agent, raw, artifact, requests, current, handler


@pytest.mark.anyio
async def test_artifacts_store_exact_bytes_and_retry_without_duplicate_download(tmp_path):
    from managed_files import ManagedFiles
    ledger, binding, agent, raw, artifact, requests, current, handler = fixture(tmp_path)
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handler)) as client:
        publisher = ManagedFiles(agent, ledger, client, current=lambda: current[0])
        first = await publisher.harvest(binding)
        second = await publisher.harvest(binding)
    assert first == second and len(first) == 1
    record = first[0]['artifact']
    assert record['sha256'] == hashlib.sha256(raw).hexdigest()
    with agent.file_store.connect() as db:
        saved = db.execute('SELECT content FROM files WHERE id=?', (record['id'],)).fetchone()[0]
    assert saved == raw
    assert len([r for r in requests if r.url.path.endswith('/content')]) == 1
    assert agent.file_store.get(record['id'], owner_principal_id='alice', chat_id='chat')['name'] == 'report.csv'
    with pytest.raises(ValueError):
        agent.file_store.get(record['id'], owner_principal_id='bob')


@pytest.mark.anyio
@pytest.mark.parametrize('field,value', [
    ('session_id', 'foreign'), ('environment_id', 'foreign'),
    ('path', '/workspace/outputs/../secret'), ('path', '/workspace/outputs/a\\b.txt'),
    ('size_bytes', 16777217), ('size_bytes', True), ('turn_id', 'unknown'),
])
async def test_unverified_artifact_never_downloads_or_imports(tmp_path, field, value):
    from managed_files import ManagedFiles
    ledger, binding, agent, raw, artifact, requests, current, handler = fixture(tmp_path)
    artifact[field] = value
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((ValueError, ManagedCodexError)):
            await ManagedFiles(agent, ledger, client, current=lambda: current[0]).harvest(binding)
    assert not agent.file_store.list(owner_principal_id='alice')
    assert not any(r.url.path.endswith('/content') for r in requests)


@pytest.mark.anyio
async def test_revoke_during_download_keeps_file_private_and_unpublished(tmp_path):
    from managed_files import ManagedFiles
    ledger, binding, agent, raw, artifact, requests, current, handler = fixture(tmp_path)
    async def revoked(request):
        result = await handler(request)
        if request.url.path.endswith('/content'):
            current[0] = False
        return result
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(revoked)) as client:
        with pytest.raises(ValueError):
            await ManagedFiles(agent, ledger, client, current=lambda: current[0]).harvest(binding)
    assert not agent.file_store.list(owner_principal_id='alice')


@pytest.mark.anyio
async def test_artifact_size_and_immutable_metadata_are_checked(tmp_path):
    from managed_files import ManagedFiles
    ledger, binding, agent, raw, artifact, requests, current, handler = fixture(tmp_path)
    artifact['size_bytes'] -= 1
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((ValueError, ManagedCodexError)):
            await ManagedFiles(agent, ledger, client, current=lambda: current[0]).harvest(binding)
    assert not agent.file_store.list(owner_principal_id='alice')


@pytest.mark.anyio
async def test_original_generation_artifacts_survive_rollover(tmp_path):
    from managed_files import ManagedFiles
    ledger, binding, agent, raw, artifact, requests, current, handler = fixture(tmp_path)
    ledger.retire(binding['id'], 'configuration_changed')
    ledger.rollover(company_id=binding['company_id'], chat_id='chat', owner='alice', collection='company',
        configuration={'environment': {'type': 'openai_hosted'}, 'next': True},
        previous_id=binding['id'], quiesced=True)
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handler)) as client:
        result = await ManagedFiles(agent, ledger, client, current=lambda: current[0]).harvest(binding)
    assert result[0]['artifact']['chat_id'] == 'chat'
    assert len(ledger.generations(binding['company_id'], 'chat')) == 2


@pytest.mark.anyio
async def test_binary_transport_rejects_redirect_and_caps_chunked_content():
    async def handler(request):
        if request.url.path.endswith('/redirect/content'):
            return httpx.Response(302, headers={'location': 'https://foreign.invalid/secret'})
        return httpx.Response(200, content=b'12345')
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ManagedCodexError):
            _ = [part async for part in client.artifact_content('session-1', 'redirect', max_bytes=4)]
        with pytest.raises(ManagedCodexError):
            _ = [part async for part in client.artifact_content('session-1', 'artifact-1', max_bytes=4)]


@pytest.mark.anyio
async def test_runtime_retains_provider_bytes_and_retries_terminal_files_without_new_input(tmp_path):
    from test_managed_runtime import fixture as runtime_fixture, turn_event
    from host import Admission
    from scope_host import bind_admission
    agent, runtime, policy, client = runtime_fixture(tmp_path)
    agent.file_store = ChatFiles(tmp_path)
    from agent_bridge import CodexVideoAgent
    agent._record_event = CodexVideoAgent._record_event
    base = policy.base_configuration
    policy.base_configuration = lambda *args: {**base(*args), 'environment': {'type': 'openai_hosted'}}
    client.session['environment'] = {'type': 'openai_hosted', 'id': 'env-1'}
    raw = b'Actual hosted output\n'
    item = dict(id='artifact-1', created_at=1, environment_id='env-1', object='agent.session.artifact',
        path='/workspace/outputs/report.txt', session_id='session-1', size_bytes=len(raw), turn_id='turn-1')
    attempts = []
    async def listing(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise ManagedCodexError('transport_error')
        return {'data': [item], 'has_more': False}
    async def get(*args):
        return copy.deepcopy(item)
    async def content(*args, **kwargs):
        yield raw
    client.list_artifacts, client.get_artifact, client.artifact_content = listing, get, content
    client.events = [turn_event('completed')]
    with bind_admission(Admission(lambda: True)):
        events = [event async for event in runtime.run(agent, 'Hello', 'chat', 'company', instructions='Fixture')]
        assert events[-1]['status'] == 'completed'
        assert agent.children['chat']['managed_artifact_recovery_required'] is True
        recovered = [event async for event in runtime.recover_files(agent, 'chat')]
    assert recovered[0]['artifact']['sha256'] == hashlib.sha256(raw).hexdigest()
    assert len(client.created) == 1 and not client.sent and not client.cancelled
    assert not agent.children['chat'].get('managed_artifact_recovery_required')
    assert agent.children['chat']['messages'][-1]['artifacts'][0]['id'] == recovered[0]['artifact']['id']


@pytest.mark.anyio
@pytest.mark.parametrize('parent', ['root-1', 'foreign-root'])
async def test_child_file_requires_provider_attested_ancestry(tmp_path, parent):
    from managed_files import ManagedFiles
    ledger, binding, agent, raw, artifact, requests, current, handler = fixture(tmp_path)
    async def children(request):
        if request.url.path.endswith('/turns/turn-1'):
            return httpx.Response(200, json={'id': 'turn-1', 'subagent_id': 'child-1', 'status': 'completed'})
        if request.url.path.endswith('/subagents'):
            return httpx.Response(200, json={'data': [{'id': 'child-1', 'parent_agent_id': parent,
                'session_id': 'session-1'}], 'has_more': False})
        return await handler(request)
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(children)) as client:
        publisher = ManagedFiles(agent, ledger, client, current=lambda: current[0])
        if parent == 'root-1':
            assert len(await publisher.harvest(binding)) == 1
        else:
            with pytest.raises(ValueError):
                await publisher.harvest(binding)
            assert not agent.file_store.list(owner_principal_id='alice')


@pytest.mark.anyio
async def test_same_artifact_id_cannot_change_after_retention(tmp_path):
    from managed_files import ManagedFiles
    ledger, binding, agent, raw, artifact, requests, current, handler = fixture(tmp_path)
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handler)) as client:
        publisher = ManagedFiles(agent, ledger, client, current=lambda: current[0])
        await publisher.harvest(binding)
        artifact['path'] = '/workspace/outputs/replaced.csv'
        with pytest.raises(ValueError):
            await publisher.harvest(binding)
    assert len(agent.file_store.list(owner_principal_id='alice')) == 1


@pytest.mark.anyio
async def test_chat_owner_change_during_metadata_read_prevents_download(tmp_path):
    from managed_files import ManagedFiles
    ledger, binding, agent, raw, artifact, requests, current, handler = fixture(tmp_path)
    async def changed(request):
        result = await handler(request)
        if request.url.path.endswith('/artifacts'):
            agent.children['chat']['owner_principal_id'] = 'bob'
        return result
    async with ManagedCodexClient('fixture', transport=httpx.MockTransport(changed)) as client:
        with pytest.raises(ValueError):
            await ManagedFiles(agent, ledger, client, current=lambda: current[0]).harvest(binding)
    assert not any(r.url.path.endswith('/content') for r in requests)
