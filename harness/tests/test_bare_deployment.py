"""The bare deployment has its own artifact, identity and dependency closure."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parent
spec = importlib.util.spec_from_file_location('bare_preview_builder', HERE / 'deploy' / 'build_preview.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def tracked_sources(monkeypatch):
    original = builder.subprocess.check_output
    def listing(command, cwd):
        if command[:3] == ['git', 'ls-files', '-z']:
            return '\0'.join(path.relative_to(REPO).as_posix() for path in HERE.rglob('*')
                if path.is_file() and '__pycache__' not in path.parts and 'dist-preview' not in path.parts).encode()
        return original(command, cwd=cwd)
    monkeypatch.setattr(builder.subprocess, 'check_output', listing)


def test_bare_artifact_has_independent_root_and_complete_fresh_import(tmp_path, monkeypatch):
    tracked_sources(monkeypatch)
    selected, missing = builder.artifact_files(REPO, HERE, vertical_id='bare')
    assert not missing
    assert not any('retail' in path.parts or path.is_relative_to(REPO / 'backend') for path in selected)
    assert builder.artifact_destination(REPO, tmp_path, HERE / 'server.py', vertical_id='bare') == tmp_path / 'harness/server.py'
    builder.copy_artifact(REPO, tmp_path, selected, vertical_id='bare')
    assert not (tmp_path / 'app/src/prototypes/video').exists()
    assert not (tmp_path / 'harness/verticals/retail').exists()
    assert not (tmp_path / 'harness/source_service.py').exists()
    script = '''
import os, sys, importlib.abc, tempfile
class LeanOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'transformers', 'numpy', 'cv2', 'huggingface_hub',
                                      'asyncpg', 'sqlalchemy', 'alembic', 'asyncssh'}:
            raise ModuleNotFoundError('Retail dependency absent: ' + fullname)
sys.meta_path.insert(0, LeanOnly())
os.environ['ROCOTO_VERTICAL'] = 'bare'
sys.path.insert(0, sys.argv[1])
import layout
layout.install()
import server, pack
assert pack.active().id == 'bare'
assert 'retail_host' not in sys.modules
assert layout.shared_asset('app/src/rocoto-theme.ts').is_file()
from fastapi.testclient import TestClient
with tempfile.TemporaryDirectory() as state:
    with TestClient(server.create_app(state)) as client:
        assert client.get('/').status_code == 200
print('bare-import-ok')
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(tmp_path / 'harness')],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'bare-import-ok' in result.stdout


def requirements(path):
    result = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith('-r '):
            result.update(requirements((path.parent / line[3:]).resolve()))
        elif line and not line.startswith('#'):
            result.add(line)
    return result


def test_dependency_closure_keeps_heavy_runtime_in_retail():
    shared = requirements(HERE / 'requirements.txt')
    retail = requirements(HERE / 'verticals/retail/requirements.txt')
    names = {line.split('=')[0].split('>')[0].split('<')[0] for line in shared}
    assert not names & {'torch', 'transformers', 'numpy', 'opencv-python-headless',
                        'huggingface-hub', 'asyncpg', 'alembic', 'sqlalchemy', 'asyncssh'}
    assert shared <= retail
    assert {'torch==2.9.1', 'transformers==4.57.3', 'asyncpg==0.31.0', 'asyncssh==2.24.1'} <= retail
    assert {'pydantic==2.13.5', 'mcp==1.30.0', 'pillow>=11,<13'} <= shared


def test_bare_public_source_has_explicit_identity_and_independent_artifact():
    manifest = json.loads((HERE / 'verticals/bare/vertical.json').read_text())
    assert manifest['id'] == 'bare'
    assert manifest['product']['name'] == 'Agent workspace'
    environment = (HERE / '.env.example').read_text()
    assert 'ROCOTO_VERTICAL=bare' in environment
    assert 'ROCOTO_OPENAI_API_KEY=' in environment
    assert builder.artifact_root('bare') == Path('harness')
    assert not (HERE / 'deploy/bare/deploy.preview.json').exists()


def test_output_is_confined_before_any_copy(tmp_path):
    here = tmp_path / 'harness'
    here.mkdir()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (here / 'dist-preview').symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match='dedicated local artifact'):
        builder.build_output(here, 'bare')
    assert not (elsewhere / 'bare').exists()
