"""The layer map names every module once, and the segment stays out of layers 1 and 2."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import layers

# Imports from runtime adapters and the harness into the vertical. This number only goes down: a change that
# removes a crossing lowers it here and deletes the entry from layers.CROSSINGS.
CEILING = 0


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture(scope='module')
def graph():
    """The directory's import graph, parsed once for this file."""
    return layers.imports()


def test_every_module_has_a_layer_and_every_crossing_is_recorded(graph):
    assert layers.check(graph=graph) == []


def test_each_module_sits_in_exactly_one_group_and_the_map_covers_the_directory(graph):
    listed = [name for groups in layers.by_layer().values() for names in groups.values() for name in names]
    assert len(listed) == len(set(listed)) == len(layers.LAYER)
    assert set(layers.LAYER) == set(layers.modules()) == set(graph)
    assert set(layers.LAYER.values()) == {1, 2, 3} == set(layers.NAMES)
    assert set(layers.FUSED) <= set(layers.LAYER)


def test_the_number_of_crossings_only_goes_down(graph):
    numbers = layers.coupling(graph)
    recorded = sum(len(targets) for targets in layers.CROSSINGS.values())
    assert numbers['crossings'] == recorded <= CEILING
    assert sum(numbers['modules'].values()) == len(layers.LAYER)
    # Every recorded crossing starts in runtime adapters or the harness and ends in the vertical.
    for importer, targets in layers.CROSSINGS.items():
        assert layers.LAYER[importer] in layers.SEGMENT_FREE, importer
        assert all(layers.LAYER[target] in layers.SEGMENT for target in targets), importer


def test_the_pack_loader_and_this_map_import_harness_modules_only(graph):
    for name in ('layers', 'pack'):
        if name in graph:
            assert all(layers.LAYER[target] in layers.SEGMENT_FREE for target in graph[name]), name


def test_a_new_import_from_the_harness_into_the_template_turns_the_ratchet_red(tmp_path):
    write(tmp_path, {
        'chat_core.py': 'import json\n',
        'shelf_rules.py': 'VALUE = 1\n',
        'tests/test_shelf_rules.py': 'import shelf_rules\n',
        'deploy/build.py': 'import shelf_rules\n',
    })
    layer = {'chat_core': 2, 'shelf_rules': 3}
    assert layers.check(tmp_path, layer, {}) == []
    for statement in ('import shelf_rules\n', 'from shelf_rules import VALUE\n', 'import shelf_rules.detail as d\n',
                      'def later():\n    from shelf_rules import VALUE\n    return VALUE\n'):
        write(tmp_path, {'chat_core.py': statement})
        problems = layers.check(tmp_path, layer, {})
        assert len(problems) == 1 and 'chat_core (layer 2) now imports shelf_rules (layer 3)' in problems[0], statement
        # The same import, once recorded, is a known crossing.
        assert layers.check(tmp_path, layer, {'chat_core': ('shelf_rules',)}) == []
    # A template module may import the harness, and a runtime module is held to the same rule.
    write(tmp_path, {'chat_core.py': 'import json\n', 'shelf_rules.py': 'import chat_core\n'})
    assert layers.check(tmp_path, layer, {}) == []
    write(tmp_path, {'codex_link.py': 'import shelf_rules\n'})
    problems = layers.check(tmp_path, {**layer, 'codex_link': 1}, {})
    assert problems == [problems[0]] and 'codex_link (layer 1) now imports shelf_rules (layer 3)' in problems[0]


def test_a_module_missing_from_the_map_and_a_stale_entry_turn_the_ratchet_red(tmp_path):
    write(tmp_path, {'chat_core.py': '', 'shelf_rules.py': '', 'tiles/__init__.py': '', 'tiles/draw.py': 'import shelf_rules\n'})
    layer = {'chat_core': 2, 'shelf_rules': 3}
    problems = layers.check(tmp_path, layer, {})
    assert problems == ['tiles is a module that needs a layer; add it to LAYERS in layers.py under the layer it belongs to.']
    # A package folder is one module, and its files' imports are its imports.
    assert layers.imports(tmp_path)['tiles'] == ('shelf_rules',)
    assert layers.check(tmp_path, {**layer, 'tiles': 3}, {}) == []
    assert any('tiles (layer 2) now imports shelf_rules' in line for line in layers.check(tmp_path, {**layer, 'tiles': 2}, {}))
    gone = layers.check(tmp_path, {**layer, 'tiles': 3, 'old_bridge': 1}, {})
    assert gone == ['old_bridge is in LAYERS and has left the directory; delete it from layers.py.']
    stale = layers.check(tmp_path, {**layer, 'tiles': 3}, {'chat_core': ('shelf_rules',)})
    assert stale == ['chat_core has stopped importing shelf_rules; delete that entry from CROSSINGS in layers.py.']


def test_a_folder_that_holds_python_is_a_module_with_or_without_an_init_file(tmp_path):
    # Python imports a bare folder as a namespace package.
    write(tmp_path, {'chat_core.py': 'from retail_extras.shelves import RULE\n', 'shelf_rules.py': '',
                     'retail_extras/shelves.py': 'RULE = 1\n', 'retail_extras/deep/more.py': 'import shelf_rules\n',
                     'deploy/serve.py': 'import shelf_rules\n', 'web/page.js': '', 'packs/retail/vertical.json': '{}',
                     'notes/read.txt': '', '.cache/kept.py': ''})
    layer = {'chat_core': 2, 'shelf_rules': 3}
    assert set(layers.modules(tmp_path)) == {'chat_core', 'shelf_rules', 'retail_extras'}
    assert layers.check(tmp_path, layer, {}) == [
        'retail_extras is a module that needs a layer; add it to LAYERS in layers.py under the layer it belongs to.']
    assert layers.imports(tmp_path)['retail_extras'] == ('shelf_rules',)
    problems = layers.check(tmp_path, {**layer, 'retail_extras': 3}, {})
    assert len(problems) == 1 and 'chat_core (layer 2) now imports retail_extras (layer 3)' in problems[0]
    # Python placed in a template's folder is a module too, and the harness importing it is a crossing.
    write(tmp_path, {'chat_core.py': 'from packs.retail.shelves import RULE\n', 'packs/retail/shelves.py': 'RULE = 1\n'})
    assert 'packs is a module that needs a layer' in layers.check(tmp_path, {**layer, 'retail_extras': 3}, {})[0]
    problems = layers.check(tmp_path, {**layer, 'retail_extras': 3, 'packs': 3}, {})
    assert len(problems) == 1 and 'chat_core (layer 2) now imports packs (layer 3)' in problems[0]
    # Today the folders beside the modules hold data, pages and documents: the map lists core_trackers alone.
    assert [name for name, files in layers.modules().items() if len(files) > 1 or files[0].parent.name == 'core_trackers'] == ['core_trackers']


def test_a_literal_name_given_to_import_module_or_to_dunder_import_is_an_import(tmp_path):
    layer = {'chat_core': 2, 'shelf_rules': 3}
    write(tmp_path, {'shelf_rules.py': ''})
    for statement in ('import importlib\nimportlib.import_module("shelf_rules")\n',
                      'from importlib import import_module\nimport_module("shelf_rules.detail")\n',
                      'import importlib\nimportlib.import_module(name="shelf_rules")\n',
                      '__import__("shelf_rules")\n',
                      'def later():\n    return __import__("shelf_rules", fromlist=["RULE"])\n'):
        write(tmp_path, {'chat_core.py': statement})
        problems = layers.check(tmp_path, layer, {})
        assert len(problems) == 1 and 'chat_core (layer 2) now imports shelf_rules (layer 3)' in problems[0], statement
        assert layers.check(tmp_path, layer, {'chat_core': ('shelf_rules',)}) == []
    # The map reads source text. A name the service computes while it runs is outside that reading:
    # pack.py imports the tool families a manifest names that way, and the manifest is their record.
    write(tmp_path, {'chat_core.py': 'import importlib\nNAME = "shelf" + "_rules"\nimportlib.import_module(NAME)\n'})
    assert layers.check(tmp_path, layer, {}) == []


def test_the_command_prints_the_measured_numbers(capsys, graph):
    assert layers.main() == 0
    printed = capsys.readouterr().out
    assert 'layer 1  Rocoto runtime adapters' in printed
    for pair, count in layers.coupling(graph)['imports'].items():
        assert f'imports {pair}  {count}' in printed
    assert f'crossings into the segment: {layers.coupling(graph)["crossings"]} imports' in printed


def test_source_folders_define_ownership_and_only_the_selected_vertical_is_scanned(tmp_path):
    write(tmp_path, {
        'runtime/connection.py': '', 'mechanics/chat.py': 'import rules\n',
        'sdk/contract.py': '', 'server.py': '', 'verticals/retail/rules.py': '',
        'verticals/other/chat.py': 'raise RuntimeError("inactive")\n',
        'deploy/closure/chat.py': '', 'tests/test_chat.py': '',
    })
    groups = layers._ownership(tmp_path)
    owners = {name: level for level, entries in groups.items() for names in entries.values() for name in names.split()}
    assert owners == {'connection': 1, 'chat': 2, 'contract': 2, 'server': 2, 'rules': 3}
    assert layers.imports(tmp_path)['chat'] == ('rules',)
    assert 'chat (layer 2) now imports rules (layer 3)' in layers.check(tmp_path, owners, {})[0]
