"""A tiny second business runs from its own folder and real company database."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import pack

HERE = Path(__file__).resolve().parents[1]
FIXTURE = HERE / 'tests/verticals/manufacturer'


@pytest.fixture
def vertical(tmp_path):
    root = tmp_path / 'manufacturer'
    shutil.copytree(FIXTURE, root)
    return root


def change(root, **fields):
    path = root / 'vertical.json'
    value = json.loads(path.read_text())
    value.update(fields)
    path.write_text(json.dumps(value))


def test_orders_vocabulary_is_independent_of_displays_and_pinned(vertical):
    selected = pack.load(vertical)
    assert selected.tile_sets() == []
    assert selected.vocabulary()['types']['order_submitted']['group'] == 'orders'
    assert selected.ready_agents()[0]['event_types'] == ['order_submitted']
    assert selected.default_kind() == 'order_desk'
    assert selected.purposes() == {'organization': 'Organization', 'office': 'Office'}
    assert selected.files['events.json'] == (vertical / 'events.json').read_bytes()
    observed = selected.vocabulary()
    observed['types']['order_submitted']['group'] = 'changed'
    (vertical / 'events.json').write_text('{}')
    assert selected.vocabulary()['types']['order_submitted']['group'] == 'orders'
    with pytest.raises(pack.PackError, match='order_submitted'):
        pack.load(vertical)


@pytest.mark.parametrize('declaration', [
    {'path': '../outside.json', 'groups': ['orders']},
    {'path': '/outside.json', 'groups': ['orders']},
    {'path': 'prompts/../events.json', 'groups': ['orders']},
    {'path': 'events.txt', 'groups': ['orders']},
    {'path': 'events.json', 'ref': 'tile_sets/orders#vocabulary', 'groups': ['orders']},
    {'path': 'events.json', 'groups': ['orders'], 'extra': True},
    {'path': 'events.json', 'groups': ['orders', 'orders']},
])
def test_vocabulary_declares_one_local_json_source(vertical, declaration):
    change(vertical, event_vocabulary=declaration)
    with pytest.raises(pack.PackError):
        pack.load(vertical)


@pytest.mark.parametrize('body', [[], {'bad event': {'group': 'orders'}},
    {'order_submitted': []}, {'order_submitted': {'group': 'other'}},
    {'order_submitted': {'group': 'orders', 'label': []}},
    {'order_submitted': {'group': 'orders', 'scope': '../company'}},
    {'order_submitted': {'group': 'orders', 'extra': True}}])
def test_event_entries_have_declared_names_groups_and_fields(vertical, body):
    (vertical / 'events.json').write_text(json.dumps(body))
    with pytest.raises(pack.PackError):
        pack.load(vertical)


def test_vocabulary_rejects_duplicate_keys_and_symlink_escape(vertical, tmp_path):
    (vertical / 'events.json').write_text('{"order_submitted":{"group":"orders"},"order_submitted":{"group":"orders"}}')
    with pytest.raises(pack.PackError, match='twice'):
        pack.load(vertical)
    outside = tmp_path / 'outside.json'
    outside.write_text('{"order_submitted":{"group":"orders"}}')
    (vertical / 'events.json').unlink()
    (vertical / 'events.json').symlink_to(outside)
    with pytest.raises(pack.PackError, match='regular file'):
        pack.load(vertical)


@pytest.mark.parametrize('legacy_manifest', [False, True])
def test_manufacturer_build_and_authenticated_database_without_other_verticals(tmp_path, monkeypatch, legacy_manifest):
    spec = importlib.util.spec_from_file_location('manufacturer_preview_builder', HERE / 'deploy/build_preview.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    original = builder.subprocess.check_output

    def tracked(command, cwd):
        if command[:3] == ['git', 'ls-files', '-z']:
            folder = Path(cwd) / command[-1]
            return b'\0'.join(path.relative_to(cwd).as_posix().encode() for path in folder.rglob('*')
                if path.is_file() and '__pycache__' not in path.parts and 'dist-preview' not in path.parts)
        return original(command, cwd=cwd)

    # This disposable source tree represents a new, tracked vertical. Selection and copying
    # still use the release builder; filesystem enumeration substitutes only the git index.
    with monkeypatch.context() as tracking:
        tracking.setattr(builder.subprocess, 'check_output', tracked)
        shared, missing = builder.artifact_files(HERE.parent, HERE, vertical_id='bare')
        assert not missing
        checkout = tmp_path / 'checkout'
        builder.copy_artifact(HERE.parent, checkout, shared, vertical_id='bare')
        shutil.rmtree(checkout / 'harness/verticals/bare')
        shutil.copytree(FIXTURE, checkout / 'harness/verticals/manufacturer')
        if legacy_manifest:
            source = checkout / 'harness/verticals/manufacturer/vertical.json'
            value = json.loads(source.read_text())
            value['schema'] = 'rocoto.pack.v1'
            value['display_sets'] = value.pop('tile_sets')
            value['agent_templates'] = value.pop('ready_agents')
            source.with_name('pack.json').write_text(json.dumps(value))
            source.unlink()
        selected, missing = builder.artifact_files(checkout, checkout / 'harness', vertical_id='manufacturer')
        assert not missing
        assert checkout / 'harness/verticals/manufacturer' / ('pack.json' if legacy_manifest else 'vertical.json') in selected
        assert not any('retail' in path.parts or 'orders' in path.parts for path in selected)
        assert checkout / 'harness/verticals/manufacturer/events.json' in selected
        assert checkout / 'harness/verticals/manufacturer/web/orders.js' in selected
        artifact = tmp_path / 'artifact'
        builder.copy_artifact(checkout, artifact, selected, vertical_id='manufacturer')
    assert {path.name for path in (artifact / 'harness/verticals').iterdir()} == {'manufacturer'}
    script = r'''
import asyncio, importlib.abc, os, sqlite3, sys
from pathlib import Path
class SelectedOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app' or fullname.split('.')[0] in {
            'retail_host', 'core_trackers', 'monitoring', 'monitoring_tools', 'source_visibility',
            'torch', 'transformers', 'numpy', 'cv2', 'asyncpg', 'sqlalchemy', 'asyncssh'}:
            raise AssertionError('Foreign vertical dependency: ' + fullname)
sys.meta_path.insert(0, SelectedOnly())
sys.path.insert(0, sys.argv[1])
import layout
layout.install('manufacturer')
import server, pack
from fastapi.testclient import TestClient
from tool_families import ToolContext
assert pack.active().id == 'manufacturer'
assert pack.active().vocabulary()['types']['order_submitted']['scope'] == 'order'
app = server.create_app(Path(sys.argv[2]))
app.state.access.create_environment('second', 'Second company')
with TestClient(app) as client:
    assert client.get('/api/manufacturer/orders').status_code == 401
    assert '/static/manufacturer/orders.js' in client.get('/').text
    assert client.get('/static/manufacturer/orders.js').status_code == 200
    for environment, person, order_id in [('demo', 'alice', 'A-1'), ('second', 'bob', 'B-1')]:
        code = app.state.access.issue(environment, principal_id=person)
        assert client.post('/api/session', json={'code': code}, headers={'X-Rocoto-Page': '1'}).status_code == 200
        assert client.get('/api/manufacturer/orders').status_code == 200
        profile = client.get('/api/business?collection=company')
        assert profile.status_code == 200
        assert not {'settings', 'connections', 'settings_scope', 'footage_relationship'} & profile.json().keys()
        for route in ('/api/business/settings', '/api/business/connections', '/api/retail/business/settings', '/api/retail/business/connections'):
            assert client.post(route, json={'collection': 'company'}, headers={'X-Rocoto-Page': '1'}).status_code == 404
        selected = app.state.workspaces[environment]
        scope = selected['business_scope'].company_id
        with sqlite3.connect(selected['state'] / 'manufacturer.sqlite') as db:
            db.execute('INSERT INTO orders VALUES(?,?,?)', (scope, order_id, 'quoted'))
            db.execute('INSERT INTO orders VALUES(?,?,?)', ('foreign-company', 'private', 'accepted'))
        response = client.get('/api/manufacturer/orders')
        assert response.status_code == 200, response.text
        assert response.json() == {'orders': [{'id': order_id, 'status': 'quoted'}]}
        extension = selected['vertical']
        context = ToolContext(state=selected['state'], company_id=scope, actor=person,
            data_owner=person, collection='company', extension=extension,
            admission=extension.read_admission(person, 'company'))
        registry = selected['agent'].tool_registry
        try:
            asyncio.run(registry.invoke(context, 'manufacturer_orders', {'company_id': 'foreign-company'}))
        except ValueError:
            pass
        else:
            raise AssertionError('Tool arguments changed the company')
        shell = client.get('/api/harness').json()
        assert shell.get('vertical', shell.get('pack'))['id'] == 'manufacturer', shell.keys()
        assert shell['surfaces'][0]['id'] == 'orders'
        with app.state.access.connect() as db:
            db.execute('DELETE FROM principal_environments WHERE principal_id=? AND environment=?', (person, environment))
        assert not context.admission.current()
        try:
            asyncio.run(registry.invoke(context, 'manufacturer_orders', {}))
        except ValueError:
            pass
        else:
            raise AssertionError('Revoked member read company orders')
        assert client.get('/api/manufacturer/orders').status_code in {401, 403}
assert not any(name.startswith(('retail_', 'app.src.prototypes.orders')) for name in sys.modules)
print('manufacturer-artifact-ok')
'''
    import os
    environment = {**os.environ, 'ROCOTO_VERTICAL': 'manufacturer', 'PYTHONDONTWRITEBYTECODE': '1'}
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-B', '-c', script,
        str(artifact / 'harness'), str(tmp_path / 'state')], cwd=artifact,
        env=environment, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'manufacturer-artifact-ok' in result.stdout


def test_orders_tab_runs_in_the_shared_tab_lifecycle():
    result = subprocess.run(['node', '--test', str(HERE / 'tests/manufacturer_tabs.cjs')],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
