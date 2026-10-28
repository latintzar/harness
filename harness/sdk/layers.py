"""Rocoto source ownership and the remaining imports into the active vertical.

These three source groups describe Rocoto's files: runtime adapters, shared harness
mechanics/SDK/entrypoints, and one vertical including its surfaces. OpenAI's stock core is
external software; this map contains neither a copy nor a replacement for it.

Folders determine ownership. CROSSINGS is the explicit ratchet for imports from shared
code into a vertical: every existing crossing is named, and removed crossings are deleted.
FUSED records the remaining extractions; moving files alone does not finish those splits.
Run `python harness/run.py layers` for the measured source groups and import counts.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import sys

import layout

HERE = layout.ROOT
RUNTIME, HARNESS, TEMPLATE = 1, 2, 3
SURFACES = TEMPLATE  # Existing callers may use this name while vertical naming is migrated.
NAMES = {RUNTIME: 'Rocoto runtime adapters', HARNESS: 'Rocoto harness', TEMPLATE: 'Vertical'}
SEGMENT_FREE = (RUNTIME, HARNESS)
SEGMENT = (TEMPLATE,)
OUTSIDE = {'tests', 'deploy'}


def modules(root=HERE):
    """Active flat modules and package files; tests and frozen deployment closures stay outside."""
    root = Path(root).resolve()
    if any((root / folder).is_dir() for folder in ('runtime', 'mechanics', 'sdk', 'verticals')):
        return layout.modules(root=root)
    # Small flat source trees remain useful for testing the import ratchet itself.
    found = {path.stem: (path,) for path in sorted(root.glob('*.py'))}
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and folder.name not in OUTSIDE and not folder.name.startswith(('.', '__')):
            files = tuple(sorted(path for path in folder.rglob('*.py') if '__pycache__' not in path.parts))
            if files:
                if folder.name in found:
                    raise ValueError(f'Duplicate module {folder.name}.')
                found[folder.name] = files
    return found


def _ownership(root=HERE):
    root = Path(root).resolve()
    groups = {RUNTIME: {}, HARNESS: {}, TEMPLATE: {}}
    for name, files in modules(root).items():
        relative = files[0].relative_to(root)
        folder = relative.parts[0]
        level = RUNTIME if folder == 'runtime' else TEMPLATE if folder == 'verticals' else HARNESS
        label = {'runtime': 'Runtime adapters', 'mechanics': 'Shared mechanics', 'sdk': 'Harness SDK',
                 'verticals': 'Active vertical'}.get(folder, 'Application entrypoints')
        groups[level].setdefault(label, []).append(name)
    return {level: {title: ' '.join(sorted(names)) for title, names in titles.items()}
            for level, titles in groups.items()}


LAYERS = _ownership()
LAYER = {name: level for level, groups in LAYERS.items() for names in groups.values() for name in names.split()}

# Modules that hold work of two layers today, and what a split would move.
FUSED = {
    'company_usage': 'The shared money ledger still contains Modal receipt accounting behind an installed provider adapter.',
    'native_executor': 'The deployed broker hosts both the legacy agent process and sandbox jobs; managed migration separates their retirement.',
}

# Shared and runtime modules import only shared interfaces; verticals install their providers.
CROSSINGS = {}


def _named(node):
    """The module a call imports by a literal name: import_module('a.b') or __import__('a.b')."""
    function = node.func
    name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else None
    if name not in ('import_module', '__import__'):
        return None
    given = node.args[0] if node.args else next((item.value for item in node.keywords if item.arg == 'name'), None)
    return given.value if isinstance(given, ast.Constant) and isinstance(given.value, str) else None


def imports(root=HERE):
    """What each module imports from the other modules of the directory, read with ast.

    Imports inside functions count: most wiring in this directory imports at the point of use.
    A literal name given to importlib.import_module or __import__ counts as well.
    """
    found = modules(root)
    result = {}
    for name, files in found.items():
        seen = set()
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=str(path))):
                if isinstance(node, ast.Import):
                    targets = [alias.name.split('.')[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    targets = [node.module.split('.')[0]]
                elif isinstance(node, ast.Call) and _named(node):
                    targets = [_named(node).split('.')[0]]
                else:
                    continue
                seen.update(target for target in targets if target in found and target != name)
        result[name] = tuple(sorted(seen))
    return result


def crossings(graph, layer=None):
    """The imports from layers 1 and 2 into layer 3 that a graph holds."""
    layer = LAYER if layer is None else layer
    found = {}
    for importer, targets in graph.items():
        if layer.get(importer) not in SEGMENT_FREE:
            continue
        crossed = tuple(target for target in targets if layer.get(target) in SEGMENT)
        if crossed:
            found[importer] = crossed
    return found


def coupling(graph=None, layer=None):
    """Measured numbers: modules per layer and imports per pair of layers ('2>3' reads 2 imports 3)."""
    graph = imports() if graph is None else graph
    layer = LAYER if layer is None else layer
    pairs = Counter()
    for importer, targets in graph.items():
        for target in targets:
            if importer in layer and target in layer:
                pairs[f'{layer[importer]}>{layer[target]}'] += 1
    crossed = crossings(graph, layer)
    return {
        'modules': dict(sorted(Counter(layer[name] for name in graph if name in layer).items())),
        'imports': dict(sorted(pairs.items())),
        'crossings': sum(len(targets) for targets in crossed.values()),
        'crossing_modules': len(crossed),
    }


def check(root=HERE, layer=None, recorded=None, graph=None):
    """Every way the tree and this map disagree, as sentences. An empty list is a green ratchet."""
    layer = LAYER if layer is None else layer
    recorded = CROSSINGS if recorded is None else recorded
    graph = imports(root) if graph is None else graph
    problems = []
    listed = [name for groups in LAYERS.values() for names in groups.values() for name in names.split()]
    if layer is LAYER:
        problems += [f'{name} is listed twice in LAYERS; keep it under one layer.'
                     for name, count in sorted(Counter(listed).items()) if count > 1]
    problems += [f'{name} is a module that needs a layer; add it to LAYERS in layers.py under the layer it belongs to.'
                 for name in sorted(set(graph) - set(layer))]
    problems += [f'{name} is in LAYERS and has left the directory; delete it from layers.py.'
                 for name in sorted(set(layer) - set(graph))]
    found = crossings(graph, layer)
    for importer in sorted(set(found) | set(recorded)):
        now, before = set(found.get(importer, ())), set(recorded.get(importer, ()))
        for target in sorted(now - before):
            problems.append(
                f'{importer} (layer {layer[importer]}) now imports {target} (layer {layer[target]}). '
                f'Runtime adapters and shared harness code stay free of the vertical: reach {target} through the manifest in pack.py, '
                f'or move the code that needs it into the vertical.')
        for target in sorted(before - now):
            problems.append(
                f'{importer} has stopped importing {target}; delete that entry from CROSSINGS in layers.py.')
    return problems


def by_layer():
    """{layer: {what the modules do: (module, ...)}}, for the documents and the self-description."""
    return {layer: {title: tuple(names.split()) for title, names in groups.items()}
            for layer, groups in LAYERS.items()}


def main():
    graph = imports()
    numbers = coupling(graph)
    for layer, count in numbers['modules'].items():
        print(f'layer {layer}  {NAMES[layer]:<16} {count:>3} modules')
    for pair, count in numbers['imports'].items():
        print(f'imports {pair}  {count}')
    print(f"crossings into the segment: {numbers['crossings']} imports from {numbers['crossing_modules']} modules")
    problems = check(graph=graph)
    for problem in problems:
        print('RED · ' + problem)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
