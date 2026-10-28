"""Vertical composition loads only explicitly declared, locally owned code."""
import json
import shutil
import sys
from pathlib import Path

import pytest

import layout
import pack


@pytest.fixture
def vertical(tmp_path):
    root = tmp_path / 'bare'
    shutil.copytree(layout.VERTICALS / 'bare', root)
    return root


def change(root, **fields):
    path = root / 'vertical.json'
    value = json.loads(path.read_text())
    value.update(fields)
    path.write_text(json.dumps(value))


def test_bare_loads_with_one_class_and_without_data_sources(vertical):
    selected = pack.load(vertical)
    assert selected.kinds() == ('action',)
    assert selected.agent_class('action')['input_kinds'] == ['none']
    assert selected.purposes() == {'organization': 'Organization'}
    assert selected.import_host() is None
    assert selected.tile_sets() == [] and [job['id'] for job in selected.recipes()] == ['onboarding']
    assert selected.tool_families() == ()


def test_custom_class_and_input_are_owned_by_the_vertical(vertical):
    value = json.loads((vertical / 'vertical.json').read_text())
    value['agent_classes'][0].update(kind='planner', input_kinds=['orders'])
    change(vertical, agent_classes=value['agent_classes'])
    selected = pack.load(vertical)
    assert selected.default_kind() == 'planner'
    assert selected.authority('planner') == 'granted_effects'
    assert selected.shell()['sections'][1]['input_kinds'] == ['orders']


def test_declared_host_is_loaded_from_its_own_folder(vertical, monkeypatch):
    name = 'fixture_vertical_host'
    source = ('def install_workspace(host, selected): return selected\n'
              'def register_routes(app, host, http): return None\n'
              'def lifespan(app, host): return None\n')
    (vertical / (name + '.py')).write_text(source)
    change(vertical, host_module=name)
    monkeypatch.syspath_prepend(str(vertical))
    try:
        selected = pack.load(vertical)
        module = selected.import_host()
        assert Path(module.__file__).resolve() == vertical / (name + '.py')
        assert selected.files[name + '.py'] == source.encode()
        assert module.install_workspace(None, {'workspace': 'a'}) == {'workspace': 'a'}
    finally:
        sys.modules.pop(name, None)


def test_foreign_host_is_refused_before_import(vertical, tmp_path, monkeypatch):
    name = 'fixture_foreign_host'
    (vertical / (name + '.py')).write_text('raise AssertionError("wrong import path")\n')
    other = tmp_path / 'foreign'
    other.mkdir()
    marker = tmp_path / 'imported'
    (other / (name + '.py')).write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    change(vertical, host_module=name)
    monkeypatch.syspath_prepend(str(other))
    with pytest.raises(pack.PackError, match='vertical folder'):
        pack.load(vertical).import_host()
    assert not marker.exists()


def test_host_requires_every_composition_hook(vertical, monkeypatch):
    name = 'fixture_incomplete_host'
    (vertical / (name + '.py')).write_text('def install_workspace(host, selected): return selected\n')
    change(vertical, host_module=name)
    monkeypatch.syspath_prepend(str(vertical))
    try:
        with pytest.raises(pack.PackError, match='register_routes'):
            pack.load(vertical).import_host()
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize('name', ['../foreign', 'sdk.module', 'module:factory', '__init__'])
def test_host_names_one_module_inside_the_vertical(vertical, name):
    change(vertical, host_module=name)
    with pytest.raises(pack.PackError):
        pack.load(vertical)


def test_tool_family_comes_from_the_selected_vertical(vertical, monkeypatch):
    name = 'fixture_selected_family'
    (vertical / (name + '.py')).write_text('FAMILY = object()\n')
    change(vertical, tool_families=[name + ':FAMILY'])
    monkeypatch.syspath_prepend(str(vertical))
    try:
        selected = pack.load(vertical)
        assert selected.import_tool_families() == [sys.modules[name].FAMILY]
    finally:
        sys.modules.pop(name, None)


def test_foreign_tool_family_is_refused_before_execution(vertical, tmp_path, monkeypatch):
    name = 'fixture_foreign_family'
    (vertical / (name + '.py')).write_text('FAMILY = object()\n')
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    marker = tmp_path / 'family-imported'
    (foreign / (name + '.py')).write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    change(vertical, tool_families=[name])
    monkeypatch.syspath_prepend(str(foreign))
    with pytest.raises(pack.PackError, match='vertical folder'):
        pack.load(vertical).import_tool_families()
    assert not marker.exists()
