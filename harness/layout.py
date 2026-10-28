"""Source and asset paths for one Rocoto harness process, using only the standard library.

Runtime, mechanics and SDK are Rocoto code. OpenAI's installed core stays outside this tree.
The selected vertical is the sole business-specific directory added to the import path.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'runtime'
MECHANICS = ROOT / 'mechanics'
SDK = ROOT / 'sdk'
VERTICALS = ROOT / 'verticals'
# The current retail receiver keeps its commissioned repo-shaped artifact location.
REPO = ROOT.parents[3] if ROOT.parts[-4:] == ('app', 'src', 'prototypes', 'video') else ROOT.parent
ENVIRONMENT = 'ROCOTO_VERTICAL'
LEGACY_ENVIRONMENT = 'ROCOTO_PACK'
DEFAULT = 'retail'
COMPATIBILITY_ENTRIES = frozenset({'source_service', 'storage_service'})
_VERTICAL = re.compile(r'[a-z][a-z0-9-]{0,39}\Z')
_MODULE = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*\Z')
_INSTALLED_VERTICAL = None


def selected_vertical(vertical_id=None):
    """Validate the explicit selection or the primary and legacy environment settings."""
    if vertical_id is None and _INSTALLED_VERTICAL is not None:
        return _INSTALLED_VERTICAL
    if vertical_id is None:
        current = os.environ.get(ENVIRONMENT, '').strip()
        legacy = os.environ.get(LEGACY_ENVIRONMENT, '').strip()
        if current and legacy and current != legacy:
            raise ValueError(f'{ENVIRONMENT} and {LEGACY_ENVIRONMENT} name different verticals.')
        vertical_id = current or legacy or DEFAULT
    if not isinstance(vertical_id, str) or not _VERTICAL.fullmatch(vertical_id):
        raise ValueError(f'{ENVIRONMENT} names one vertical folder under verticals/, written like {DEFAULT}.')
    return vertical_id


def vertical_root(vertical_id=None, *, root=None):
    base = ROOT if root is None else Path(root).resolve()
    path = base / 'verticals' / selected_vertical(vertical_id)
    if path.resolve() != path:
        raise ValueError('A vertical directory stays inside its harness source tree.')
    return path


def code_directories(vertical_id=None, *, root=None):
    """Import priority; root contains entry commands and optional deployment launchers."""
    base = ROOT if root is None else Path(root).resolve()
    paths = (base / 'runtime', base / 'mechanics', base / 'sdk', vertical_root(vertical_id, root=base), base)
    if any(path.resolve() != path for path in paths):
        raise ValueError('Harness code directories stay inside their source tree.')
    return paths



def _root_module(base, name):
    # The retail receiver's generated entry scripts dispatch to the relocated implementations.
    legacy_artifact = base.parts[-4:] == ('app', 'src', 'prototypes', 'video')
    return not (legacy_artifact and name in COMPATIBILITY_ENTRIES)


def modules(vertical_id=None, *, root=None):
    """Unique flat modules and package files in this selected source tree, without imports."""
    base = ROOT if root is None else Path(root).resolve()
    found = {}
    for folder in code_directories(vertical_id, root=base):
        if not folder.is_dir():
            continue
        candidates = [(path.stem, (path,)) for path in sorted(folder.glob('*.py'))
                      if folder != base or _root_module(base, path.stem)]
        if folder != base:
            for package in sorted(folder.iterdir()):
                if package.is_dir() and not package.name.startswith(('.', '__')):
                    files = tuple(sorted(path for path in package.rglob('*.py') if '__pycache__' not in path.parts))
                    if files:
                        candidates.append((package.name, files))
        for name, files in candidates:
            if name in found:
                raise ValueError(f'Duplicate module {name}: {found[name][0]} and {files[0]}.')
            found[name] = files
    return found


def source(module_name, vertical_id=None, *, root=None):
    """Resolve a flat or package-qualified source file without loading customer code."""
    if not isinstance(module_name, str) or not _MODULE.fullmatch(module_name):
        raise ValueError('A source module uses a Python module name.')
    top = module_name.split('.')[0]
    if top not in modules(vertical_id, root=root):
        raise FileNotFoundError(f'The selected harness source has no module {module_name}.')
    base = ROOT if root is None else Path(root).resolve()
    relative = Path(*module_name.split('.'))
    for folder in code_directories(vertical_id, root=base):
        if folder == base and not _root_module(base, top):
            continue
        for candidate in (folder / relative.with_suffix('.py'), folder / relative / '__init__.py'):
            if candidate.is_file():
                if candidate.resolve() != candidate:
                    raise ValueError('A source module stays inside its harness source tree.')
                return candidate
    raise FileNotFoundError(f'The selected harness source has no module {module_name}.')


def shared_asset(relative_path):
    """A repository-relative shared asset in either checkout or repo-shaped artifact."""
    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError('A shared asset uses a repository-relative path.')
    path = (REPO / relative).resolve()
    if not path.is_relative_to(REPO):
        raise ValueError('A shared asset stays inside the repository or artifact.')
    return path


def install(vertical_id=None):
    """Install one vertical's paths before application imports; repeat calls are idempotent."""
    global _INSTALLED_VERTICAL
    chosen = selected_vertical(vertical_id)
    if _INSTALLED_VERTICAL not in (None, chosen):
        raise ValueError('A harness process serves one vertical; start a fresh process to select another.')
    modules(chosen)  # Check ambiguous names before any domain module can import the wrong implementation.
    paths = code_directories(chosen)
    owned = {ROOT, RUNTIME, MECHANICS, SDK}
    sys.path[:] = [value for value in sys.path
                   if (Path(value or '.').resolve() not in owned
                       and Path(value or '.').resolve().parent != VERTICALS)]
    sys.path[:0] = [str(path) for path in paths]
    _INSTALLED_VERTICAL = chosen
    return paths
