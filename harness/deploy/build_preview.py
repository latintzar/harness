"""Copy tracked Rocoto runtime and shared UI assets into an explicit ignored artifact.

Publication still requires the clean-source package/receiver workflow and a commissioned
target. This builder carries no state, credentials, model weights or source footage.
"""
import argparse
from pathlib import Path
import shutil
import sys
import subprocess



MAINTAINED_READ_FILES = (
    'db.py', 'config.py', 'schema_migrations.py', 'alembic.ini',
    'realtime/__init__.py', 'realtime/contracts.py', 'realtime/store.py',
    'realtime/subjects.py', 'realtime/replay.py', 'realtime/modules.py',
    'realtime/module_store.py', 'realtime/continuity.py', 'realtime/read_contract.py',
    'realtime/media.py', 'realtime/configuration.py', 'realtime/playbooks/retail-v1.json',
    # The store of record for a location's trackers, records, outputs, schedule and runs.
    'realtime/locations.py',
    # Host commissioning commands and their imports: the identity import (owner role) and the
    # historical tracker-run import (restricted role). The playback seal is a candidate module.
    'realtime/provision.py', 'realtime/provision_host.py',
    'realtime/import_stream.py', 'realtime/import_tracker_run.py',
)


def source_paths(repo, prefix):
    """Use Git in a checkout or the explicit reviewed inventory in a source export."""
    manifest = repo / 'provenance' / 'release-files.json'
    if not (repo / '.git').exists() and manifest.is_file():
        import json
        names = json.loads(manifest.read_text())['files']
        selected = set()
        for name in names:
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Release inventory paths stay inside the source tree.')
            if name == prefix or name.startswith(prefix.rstrip('/') + '/'):
                selected.add(repo / relative)
        return selected
    tracked = subprocess.check_output(['git', 'ls-files', '-z', '--', prefix], cwd=repo)
    return {repo / name for name in tracked.decode().split('\0') if name}


def maintained_read_files(repo):
    """Package the shared read closure, the host commands and the immutable schema chain."""
    selected = {repo / 'backend' / relative for relative in MAINTAINED_READ_FILES}
    selected.update(source_paths(repo, 'backend/migrations'))
    return selected


ARTIFACT_ROOT = Path('app/src/prototypes/video')
# These two commissioned units still execute the old direct entry paths. Generated
# launchers bridge that deployment contract to the single relocated implementation.
MIGRATION_ENTRIES = ('source_service', 'storage_service')
SHARED_ASSETS = (
    'app/src/rocoto-theme.ts', 'app/public/brand/rocoto-mark.svg',
    'app/public/fonts/fraunces/fraunces-latin-500-700.woff2',
    'app/public/fonts/fraunces/OFL.txt',
    'app/public/connection-brands/axis.svg', 'app/public/connection-brands/gmail.png',
    'app/public/connection-brands/square.svg', 'app/public/connection-brands/shopify.svg',
)


def artifact_root(vertical_id=None):
    return ARTIFACT_ROOT if vertical_id in (None, 'retail') else Path('harness')


def artifact_destination(repo, output, source, *, vertical_id=None):
    relative = source.relative_to(repo)
    if relative.parts[0] == 'backend':
        return output / artifact_root(vertical_id) / '_maintained_backend' / relative.relative_to('backend')
    if relative.parts[0] == 'harness':
        return output / artifact_root(vertical_id) / relative.relative_to('harness')
    return output / relative


def source_layout(here, vertical_id=None):
    """Use the runtime's layout rather than maintaining a second folder inventory."""
    sys.path.insert(0, str(here / 'sdk'))
    sys.path.insert(0, str(here))
    import layout
    layout.modules(vertical_id, root=here)
    return layout


def template_files(here, vertical_id=None):
    """Every file the selected vertical declares, including Python and prompts."""
    layout = source_layout(here, vertical_id)
    import pack
    template = pack.load(layout.vertical_root(vertical_id, root=here))
    return {template.manifest_path} | {(template.root / key).resolve() for key in template.files}


def artifact_files(repo, here, *, vertical_id=None):
    """Tracked shared code/assets and the selected vertical, with missing Python reported.

    Retail retains its commissioned artifact root; other verticals use harness/.
    The same selection is used by commissioning rehearsals and packaging tests.
    """
    layout = source_layout(here, vertical_id)
    vertical = layout.vertical_root(vertical_id, root=here)
    directories = [directory for directory in layout.code_directories(vertical_id, root=here) if directory != here]
    runtime = {path for directory in directories for path in directory.rglob('*.py')
               if '__pycache__' not in path.parts and 'tests' not in path.relative_to(directory).parts}
    runtime.update(here / name for name in ('layout.py', 'run.py', 'server.py'))
    assets = {path for directory in (here / 'web', vertical / 'web') for path in directory.rglob('*')
              if path.is_file()}
    # JSON beside shared modules is executable configuration (e.g. plugin catalogue).
    assets.update(path for directory in directories for path in directory.rglob('*.json')
                  if directory != vertical and '__pycache__' not in path.parts)
    assets.update(template_files(here, vertical_id))
    # Retail's processor packages also own their immutable JSON contracts.
    assets.update(path for path in vertical.rglob('*.json') if 'tests' not in path.relative_to(vertical).parts)
    assets.update(here / name for name in ('theme.json', 'requirements.txt', 'deploy/connections/configure_google.py'))
    if (vertical / 'requirements.txt').is_file():
        assets.add(vertical / 'requirements.txt')
    tracked = source_paths(repo, str(here.relative_to(repo)))
    selected = (runtime | assets) & tracked
    untracked = sorted(path.relative_to(here).as_posix() for path in runtime - tracked)
    selected.update(repo / relative for relative in SHARED_ASSETS)
    selected.update(repo / name for name in ('LICENSE', 'THIRD_PARTY.md') if (repo / name).is_file())
    if vertical.name == 'retail':
        selected.update(maintained_read_files(repo))
    return selected, untracked


def copy_artifact(repo, output, selected, *, vertical_id=None):
    """Copy each selected regular file to its place in the artifact layout."""
    for source in sorted(selected):
        if not source.is_file() or source.is_symlink() or source.resolve() != source.absolute():
            named = source.relative_to(repo) if source.is_relative_to(repo) else source.name
            raise ValueError(f'Runtime artifact is missing or symbolic: {named}')
        destination = artifact_destination(repo, output, source, vertical_id=vertical_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    generated = 0
    for module in MIGRATION_ENTRIES if vertical_id in (None, 'retail') else ():
        if any(source.name == module + '.py' for source in selected):
            destination = output / ARTIFACT_ROOT / (module + '.py')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                '# Migration launcher for the commissioned retail worker path.\n'
                'import sys\n'
                "if __name__ == '__main__':\n"
                '    from run import main\n'
                f"    raise SystemExit(main(['{module}', *sys.argv[1:]]))\n")
            destination.chmod(0o644)
            generated += 1
    return len(selected) + generated


def build_output(here, vertical_id):
    output = (here.parent / 'app/dist-video-preview' if vertical_id == 'retail'
              else here / 'dist-preview' / vertical_id)
    if output.is_symlink() or output.resolve() != output.absolute():
        raise ValueError('Build output must remain the dedicated local artifact directory')
    return output


def build(vertical_id=None):
    here = Path(__file__).resolve().parent.parent
    layout = source_layout(here, vertical_id)
    repo = layout.REPO
    vertical_id = layout.selected_vertical(vertical_id)
    output = build_output(here, vertical_id)
    selected, missing_runtime = artifact_files(repo, here, vertical_id=vertical_id)
    if missing_runtime:
        raise ValueError('Track the complete harness runtime before packaging: ' + ', '.join(missing_runtime))
    absent = template_files(here, vertical_id) - selected
    if absent:
        raise ValueError('Track every file of the selected vertical before packaging: '
                         + ', '.join(sorted(path.relative_to(here).as_posix() for path in absent)))
    if output.exists():
        shutil.rmtree(output)
    copy_artifact(repo, output, selected, vertical_id=vertical_id)
    print(f'Prepared {len(selected)} tracked runtime/shared files in {output.relative_to(repo)}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--vertical', default=None)
    build(parser.parse_args().vertical)
