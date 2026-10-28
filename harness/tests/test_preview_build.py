"""Release selection keeps the shared harness and exactly its selected vertical."""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parent


def builder():
    spec = importlib.util.spec_from_file_location('preview_builder_contract', HERE / 'deploy/build_preview.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def track_checkout(monkeypatch, build, *, omit=()):
    def listing(command, cwd):
        folder = Path(command[-1])
        folder = folder if folder.is_absolute() else Path(cwd) / folder
        return b'\0'.join(str(path.relative_to(cwd)).encode() for path in folder.rglob('*')
            if path.is_file() and '__pycache__' not in path.parts and path not in omit)
    monkeypatch.setattr(build.subprocess, 'check_output', listing)


def test_harness_files_keep_the_commissioned_artifact_root():
    build = builder()
    output = Path('/artifact')
    for relative in ('server.py', 'layout.py', 'runtime/agent_auth.py',
                     'mechanics/agent_bridge.py', 'sdk/pack.py', 'verticals/retail/vertical.json',
                     'verticals/retail/web/control-room.js', 'web/app.js'):
        assert build.artifact_destination(REPO, output, HERE / relative) == (
            output / 'app/src/prototypes/video' / relative)
    assert build.artifact_destination(REPO, output, REPO / 'backend/db.py') == (
        output / 'app/src/prototypes/video/_maintained_backend/db.py')


def test_selection_contains_shared_layers_and_one_vertical(monkeypatch):
    build = builder()
    track_checkout(monkeypatch, build)
    selected, untracked = build.artifact_files(REPO, HERE, vertical_id='retail')
    required = ['layout.py', 'run.py', 'server.py', 'runtime/agent_auth.py',
        'mechanics/agent_bridge.py', 'runtime/plugin_catalogue.json', 'sdk/pack.py',
        'verticals/retail/source_service.py', 'verticals/retail/vertical.json',
        'verticals/retail/core_trackers/__init__.py', 'verticals/retail/core_trackers/presentation.py',
        'verticals/retail/web/control-room.js', 'web/app.js']
    assert {HERE / path for path in required} <= selected
    assert not untracked
    for source in selected:
        if source.is_relative_to(HERE / 'verticals'):
            assert source.relative_to(HERE / 'verticals').parts[0] == 'retail'
        assert not source.is_relative_to(HERE / 'tests')
    assert HERE / 'README.md' not in selected
    assert build.template_files(HERE, vertical_id='retail') <= selected


def test_untracked_nested_python_is_reported(monkeypatch):
    build = builder()
    missing = HERE / 'verticals/retail/core_trackers/presentation.py'
    track_checkout(monkeypatch, build, omit={missing})
    selected, untracked = build.artifact_files(REPO, HERE, vertical_id='retail')
    assert missing not in selected
    assert 'verticals/retail/core_trackers/presentation.py' in untracked


def test_copy_preserves_helpers_and_generates_migration_launchers(tmp_path):
    build = builder()
    sources = {HERE / 'verticals/retail/source_service.py', HERE / 'verticals/retail/storage_service.py',
               HERE / 'runtime/native_executor.py'}
    build.copy_artifact(REPO, tmp_path, sources)
    release = tmp_path / 'app/src/prototypes/video'
    for source in sources:
        assert build.artifact_destination(REPO, tmp_path, source).read_bytes() == source.read_bytes()
    for module in ('source_service', 'storage_service'):
        launcher = (release / (module + '.py')).read_text()
        assert 'migration' in launcher.lower()
        assert 'from run import main' in launcher
        assert f"main(['{module}', *sys.argv[1:]])" in launcher
        assert "if __name__ == '__main__':" in launcher
    assert not (release / 'native_executor.py').exists()


def test_layered_release_pin_checks_nested_code_and_directory_custody(tmp_path):
    import hashlib
    import json
    import os
    spec = importlib.util.spec_from_file_location('layered_release_verifier',
        HERE / 'deploy/source-intake/demo/verify_release.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    releases = tmp_path / 'releases'
    release = releases / ('b' * 40)
    source = release / 'app/src/prototypes/video'
    migrations = sorted((REPO / 'backend/migrations/versions').glob('[0-9]*.py'))
    assert migrations and any(path.name == '0001_baseline.py' for path in migrations)
    names = ['source_service.py', 'layout.py', 'run.py', 'verticals/retail/source_service.py',
             *[f'mechanics/module_{n}.py' for n in range(12)],
             *[f'_maintained_backend/migrations/versions/{path.name}' for path in migrations]]
    files = {}
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (REPO / 'backend' / name.removeprefix('_maintained_backend/')).read_bytes() if name.startswith('_maintained_backend/') else b'# pinned source\n'
        path.write_bytes(content)
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    current = tmp_path / 'current'
    current.symlink_to(release)
    receipt = b'{"synthetic":true}\n'
    record = {'schema_version': 2, 'source_commit': 'b' * 40, 'release_path': str(release),
        'files': files, 'media_receipt_path': '/opt/rocoto-source-media/readiness-receipt.json',
        'media_receipt_sha256': hashlib.sha256(receipt).hexdigest()}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(record))
    options = {'owner': os.geteuid(), 'current': current, 'releases': releases,
               'receipt_reader': lambda _: receipt}
    assert verifier.verify(manifest, **options)['module_count'] == len(names)
    code = source / 'mechanics/module_0.py'
    code.write_bytes(b'# changed source\n')
    with pytest.raises(ValueError, match='digest'):
        verifier.verify(manifest, **options)
    code.write_bytes(b'# pinned source\n')
    code.parent.chmod(0o777)
    with pytest.raises(ValueError, match='directory'):
        verifier.verify(manifest, **options)


def test_intake_pins_are_built_from_the_commit_not_changed_worktree(tmp_path, monkeypatch):
    import hashlib
    import shutil
    import subprocess
    build = builder()
    with monkeypatch.context() as tracking:
        track_checkout(tracking, build)
        selected, missing = build.artifact_files(REPO, HERE, vertical_id='retail')
    assert not missing
    selected.add(HERE / 'deploy/build_preview.py')
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    for source in selected:
        target = checkout / source.relative_to(REPO)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), 'add', '--all'], check=True)
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Packaging test',
        '-c', 'user.email=packaging@example.invalid', 'commit', '-qm', 'Reviewed source'], check=True)
    commit = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
    module = checkout / 'harness/verticals/retail/source_service.py'
    committed_hash = hashlib.sha256(module.read_bytes()).hexdigest()
    module.write_text(module.read_text() + '\n# Unreviewed worktree change.\n')
    spec = importlib.util.spec_from_file_location('intake_pin_renderer',
        HERE / 'deploy/source-intake/demo/render.py')
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    pins = renderer.layered_modules(checkout, commit)
    assert pins['verticals/retail/source_service.py'] == committed_hash
    assert {'layout.py', 'run.py', 'source_service.py', 'storage_service.py',
            'runtime/native_executor.py', '_maintained_backend/realtime/locations.py'} <= pins.keys()
    assert pins['source_service.py'] != committed_hash
