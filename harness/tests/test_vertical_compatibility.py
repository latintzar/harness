"""Canonical names win; one release can still read commissioned legacy manifests."""
import json
from pathlib import Path
import shutil

import pytest
import pack

FIXTURE = Path(__file__).parent / 'verticals/manufacturer'


def variant(tmp_path, *, legacy=False):
    root = tmp_path / 'manufacturer'
    shutil.copytree(FIXTURE, root)
    source = root / ('vertical.json' if (root / 'vertical.json').exists() else 'pack.json')
    value = json.loads(source.read_text())
    source.unlink()
    aliases = {'display_sets': 'tile_sets', 'agent_templates': 'ready_agents',
               'agent_templates_retired': 'ready_agents_retired'}
    for old, new in aliases.items():
        if old in value:
            value[new] = value.pop(old)
    value['schema'] = 'rocoto.vertical.v1'
    if legacy:
        value['schema'] = 'rocoto.pack.v1'
        value['product']['segment'] = value['product'].pop('business')
        for old, new in aliases.items():
            if new in value:
                value[old] = value.pop(new)
    filename = 'pack.json' if legacy else 'vertical.json'
    (root / filename).write_text(json.dumps(value))
    return root, value


def test_canonical_manifest_and_accessors(tmp_path):
    root, _ = variant(tmp_path)
    vertical = pack.load(root)
    assert vertical.manifest['schema'] == 'rocoto.vertical.v1'
    assert vertical.manifest_path == root / 'vertical.json'
    assert vertical.ready_agents()[0]['id'] == 'order-review'
    assert vertical.tile_sets() == []
    assert not {'display_sets', 'agent_templates', 'agent_templates_retired'} & vertical.manifest.keys()


def test_legacy_manifest_normalizes_without_changing_stored_identity(tmp_path):
    root, _ = variant(tmp_path, legacy=True)
    vertical = pack.load(root / 'pack.json')
    assert vertical.manifest_path == root / 'pack.json'
    assert vertical.manifest['schema'] == 'rocoto.vertical.v1'
    assert vertical.ready_agents() == vertical.agent_templates()
    assert vertical.tile_sets() == vertical.display_sets()
    assert vertical.retired_ready_agents() == vertical.retired_agent_templates()
    state = tmp_path / 'state'
    pack.bind(state, vertical)
    before = (state / '.workspace-template.json').read_bytes()
    pack.bind(state, vertical)
    assert (state / '.workspace-template.json').read_bytes() == before
    binding = json.loads(before)
    assert binding == {'schema': 'rocoto.workspace-template.v1', 'template': 'manufacturer'}


def test_canonical_manifest_wins_and_invalid_new_file_never_falls_back(tmp_path):
    root, value = variant(tmp_path)
    (root / 'pack.json').write_text('{"invalid":"old manifest"}')
    assert pack.load(root).id == 'manufacturer'
    (root / 'pack.json').write_text(json.dumps(value))
    (root / 'vertical.json').write_text('{"invalid":"new manifest"}')
    with pytest.raises(pack.PackError):
        pack.load(root)


def test_canonical_keys_win_over_legacy_keys(tmp_path):
    root, value = variant(tmp_path)
    value.update(display_sets='bad old value', agent_templates='bad old value', agent_templates_retired='bad old value')
    value['ready_agents_retired'] = []
    (root / 'vertical.json').write_text(json.dumps(value))
    assert pack.load(root).ready_agents()[0]['id'] == 'order-review'


def test_oldest_recipe_tile_sets_and_vocabulary_remain_readable(tmp_path):
    root = tmp_path / 'jobshop'
    shutil.copytree(FIXTURE.parent / 'jobshop', root)
    path = root / 'vertical.json'
    value = json.loads(path.read_text())
    value['schema'] = 'rocoto.pack.v1'
    value['recipes'] = value.pop('tile_sets')
    value['agent_templates'] = value.pop('ready_agents')
    value['event_vocabulary']['ref'] = 'recipes/jobshop#vocabulary'
    path.unlink()
    (root / 'pack.json').write_text(json.dumps(value))
    loaded = pack.load(root)
    assert loaded.tile_set('jobshop')['trackers']
    assert loaded.vocabulary()['types']['week_full']['group'] == 'office'
    assert loaded.recipes() == []


def test_legacy_display_refs_and_whole_job_recipes_stay_distinct(tmp_path):
    root = tmp_path / 'bare'
    shutil.copytree(FIXTURE.parents[2] / 'verticals/bare', root)
    path = root / 'vertical.json'
    value = json.loads(path.read_text())
    value['schema'] = 'rocoto.pack.v1'
    value['display_sets'] = value.pop('tile_sets')
    value['agent_templates'] = value.pop('ready_agents')
    path.unlink()
    (root / 'pack.json').write_text(json.dumps(value))
    loaded = pack.load(root)
    assert loaded.recipes()[0]['id'] == 'onboarding'
    assert loaded.tile_sets() == []


@pytest.mark.parametrize('schema', [[], {}, 1, None, 'rocoto.vertical.v2'])
def test_invalid_schema_values_raise_the_manifest_error(tmp_path, schema):
    root, value = variant(tmp_path)
    value['schema'] = schema
    (root / 'vertical.json').write_text(json.dumps(value))
    with pytest.raises(pack.PackError, match='declares the schema'):
        pack.load(root)
