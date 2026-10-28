"""One admitted registry preserves tool authority across every transport."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tool_families import ToolContext, ToolRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


def family(*, reads=('read_value',), writes=('save_value',), asynchronous=()):
    def specs(write=True):
        return [{'type': 'function', 'name': name, 'description': name,
                 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
                for name in (*reads, *(writes if write else ())) ]
    return SimpleNamespace(READ_NAMES=frozenset(reads), WRITE_NAMES=frozenset(writes),
        ASYNC_NAMES=frozenset(asynchronous), specs=specs,
        read=lambda context, name, args: context.actor,
        call=lambda context, name, args: context.data_owner)


def context(tmp_path, **changes):
    run = {'cancel': asyncio.Event()}
    record = {'id': 'chat', 'dataset': 'company', 'owner_principal_id': 'company-owner'}
    agent = SimpleNamespace(active={'chat': run}, children={'chat': record})
    values = dict(state=tmp_path, company_id='company-id', actor='member',
        data_owner='company-owner', collection='company', chat_id='chat',
        record=record, run=run, agent=agent, admission=SimpleNamespace(current=lambda: True))
    return ToolContext(**{**values, **changes})


@pytest.mark.parametrize('families', [
    (family(reads=('same',), writes=('same',)),),
    (family(), family()),
    (family(asynchronous=('read_value',)),),
    (family(asynchronous=('save_value',)),),
])
def test_invalid_families_fail_before_dispatch(families):
    with pytest.raises(ValueError):
        ToolRegistry(families)


@pytest.mark.anyio
async def test_shared_context_keeps_actor_separate_from_data_owner(tmp_path):
    registry = ToolRegistry((family(),))
    admitted = context(tmp_path)
    assert await registry.invoke(admitted, 'read_value', {}) == 'member'
    assert await registry.invoke(admitted, 'save_value', {}) == 'company-owner'
    assert admitted.owner == 'member' and admitted.dataset == 'company'
    assert registry.read_names('public') == {'read_value'}
    with pytest.raises(ValueError):
        await registry.invoke(admitted, 'read_value', {'actor': 'other'})


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['child', 'native_child', 'stopped', 'replaced', 'collection', 'revoked'])
async def test_mutation_requires_current_active_parent(tmp_path, change):
    registry = ToolRegistry((family(),))
    admitted = context(tmp_path)
    if change == 'child':
        admitted.record['parent_id'] = 'parent'
    elif change == 'native_child':
        admitted.record['native_root_id'] = 'parent'
    elif change == 'stopped':
        admitted.run['cancel'].set()
    elif change == 'replaced':
        admitted.agent.active['chat'] = {}
    elif change == 'collection':
        admitted.record['dataset'] = 'other'
    else:
        admitted.admission.current = lambda: False
    with pytest.raises(ValueError):
        await registry.invoke(admitted, 'save_value', {})


@pytest.mark.anyio
async def test_revocation_during_await_withholds_result(tmp_path):
    ready, release = asyncio.Event(), asyncio.Event()
    registered = family()
    async def aread(context, name, args):
        ready.set()
        await release.wait()
        return 'private'
    registered.aread = aread
    registry = ToolRegistry((registered,))
    admitted = context(tmp_path)
    task = asyncio.create_task(registry.invoke(admitted, 'read_value', {}))
    await ready.wait()
    admitted.admission.current = lambda: False
    release.set()
    with pytest.raises(ValueError):
        await task


def test_surface_allowlists_are_explicit_and_results_are_copies():
    registered = family(reads=('one', 'two'))
    registered.SURFACE_READS = {'native': frozenset({'one'}), 'public': frozenset(), 'local': frozenset({'two'})}
    registry = ToolRegistry((registered,))
    assert registry.read_names('native') == {'one'}
    assert registry.specs(surface='public') == []
    specs = registry.specs()
    specs[0]['inputSchema']['properties']['injected'] = {}
    assert registry.specs()[0]['inputSchema']['properties'] == {}


def test_bare_tools_run_with_retail_directory_absent(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    source = Path(__file__).parents[1]
    artifact = tmp_path / 'harness'
    artifact.mkdir()
    for name in ('layout.py', 'run.py'):
        shutil.copy2(source / name, artifact / name)
    for directory in ('runtime', 'mechanics', 'sdk'):
        target = artifact / directory
        target.mkdir()
        for module in (source / directory).glob('*.py'):
            shutil.copy2(module, target / module.name)
    shutil.copytree(source / 'verticals' / 'bare', artifact / 'verticals' / 'bare')
    state = tmp_path / 'state'
    state.mkdir()
    script = '''
import asyncio, json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import layout
layout.install()
import agent_bridge, native_events, native_mcp, public_mcp, mcp_host, mcp_server
assert not (layout.VERTICALS / 'retail').exists()
assert not {'monitoring', 'execution_policy', 'source_visibility', 'inspection_previews'} & set(sys.modules)
server = mcp_server.make_server(pathlib.Path(sys.argv[2]))
async def prove():
    listed = await server.list_tools()
    assert {tool.name for tool in listed} == {'get_harness', 'get_business_profile', 'get_recipe', 'get_recipe_run', 'list_recipe_runs'}
    answer = await server.call_tool('get_harness', {'section': 'tools'})
    payload = json.loads(answer[0].text)
    assert payload['product']['vertical']['id'] == 'bare'
    names = {row['name'] for row in payload['tools']['conversation']}
    assert {'get_harness', 'propose_agent', 'send_message', 'create_file'} <= names
    assert 'list_cameras' not in names and 'list_trackers' not in names
asyncio.run(prove())
assert not {'monitoring', 'execution_policy', 'source_visibility', 'inspection_previews'} & set(sys.modules)
'''
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'ROCOTO_PACK', 'PYTHONPATH'}}
    environment.update(ROCOTO_VERTICAL='bare', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-I', '-c', script, str(artifact), str(state)],
        env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_wake_tools_are_common_parent_tools_with_private_read_scope():
    from tool_families import build_registry
    registry = build_registry()
    assert {'list_wakes', 'create_webhook', 'set_wake', 'cancel_wake'} <= registry.reads | registry.writes
    assert 'list_wakes' in registry.reads
    for surface in ('public', 'native', 'local'):
        assert 'list_wakes' not in registry.read_names(surface)
