"""Bootstrap isolation and command dispatch in checkout-shaped and deployed trees."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1]


def tree(tmp_path, *, artifact=False):
    repo = tmp_path / 'release'
    root = repo / ('app/src/prototypes/video' if artifact else 'harness')
    for folder in ('runtime', 'mechanics', 'sdk', 'verticals/bare'):
        (root / folder).mkdir(parents=True)
    (repo / 'app/public').mkdir(parents=True, exist_ok=True)
    for name in ('layout.py', 'run.py'):
        shutil.copy2(SOURCE / name, root / name)
    return repo, root


def execute(root, code, **environment):
    env = {key: value for key, value in os.environ.items() if key not in ('ROCOTO_PACK', 'ROCOTO_VERTICAL', 'PYTHONPATH')}
    env.update(PYTHONDONTWRITEBYTECODE='1', **environment)
    return subprocess.run([sys.executable, '-c', code], cwd=root, env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize('artifact', [False, True])
def test_bare_bootstrap_and_assets_work_with_retail_physically_absent(tmp_path, artifact):
    repo, root = tree(tmp_path, artifact=artifact)
    (root / 'verticals/bare/role.py').write_text('VALUE = "bare"\n')
    (root / 'mechanics/common.py').write_text('VALUE = "common"\n')
    result = execute(root, '''import json, sys, layout
layout.install()
import role, common
print(json.dumps({'root': str(layout.ROOT), 'repo': str(layout.REPO),
 'asset': str(layout.shared_asset('app/public/logo.svg')), 'role': role.VALUE,
 'common': common.VALUE, 'source': str(layout.source('role')),
 'paths': [p for p in sys.path if '/verticals/' in p]}))
''', ROCOTO_VERTICAL='bare')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'root': str(root), 'repo': str(repo), 'asset': str(repo / 'app/public/logo.svg'),
        'role': 'bare', 'common': 'common', 'source': str(root / 'verticals/bare/role.py'),
        'paths': [str(root / 'verticals/bare')]}
    assert not (root / 'verticals/retail').exists()


def test_bootstrap_rejects_ambiguous_active_modules_but_ignores_inactive_verticals(tmp_path):
    _, root = tree(tmp_path)
    (root / 'mechanics/common.py').write_text('')
    (root / 'verticals/retail').mkdir()
    (root / 'verticals/retail/common.py').write_text('raise RuntimeError("inactive")\n')
    good = execute(root, 'import layout; layout.install(); import common', ROCOTO_VERTICAL='bare')
    assert good.returncode == 0, good.stderr
    (root / 'verticals/bare/common.py').write_text('')
    bad = execute(root, 'import layout; layout.install()', ROCOTO_VERTICAL='bare')
    assert bad.returncode != 0 and 'Duplicate module common' in bad.stderr
    (root / 'verticals/bare/common.py').unlink()
    (root / 'common.py').write_text('')
    root_collision = execute(root, 'import layout; layout.install()', ROCOTO_VERTICAL='bare')
    assert root_collision.returncode != 0 and 'Duplicate module common' in root_collision.stderr


@pytest.mark.parametrize('environment, expected', [
    ({'ROCOTO_PACK': 'bare'}, 'bare'),
    ({'ROCOTO_VERTICAL': 'bare', 'ROCOTO_PACK': 'bare'}, 'bare'),
    ({'ROCOTO_VERTICAL': 'bare', 'ROCOTO_PACK': 'retail'}, 'different verticals'),
    ({'ROCOTO_VERTICAL': '../bare'}, 'names one vertical folder'),
])
def test_vertical_selection_validates_both_environment_names(tmp_path, environment, expected):
    _, root = tree(tmp_path)
    result = execute(root, 'import layout; print(layout.selected_vertical())', **environment)
    if expected == 'bare':
        assert result.returncode == 0 and result.stdout.strip() == expected, result.stderr
    else:
        assert result.returncode != 0 and expected in result.stderr


def test_run_dispatches_relocated_command_and_preserves_arguments_around_legacy_launcher(tmp_path):
    _, root = tree(tmp_path, artifact=True)
    (root / 'verticals/bare/source_service.py').write_text(
        'import json,sys; print(json.dumps({"name": __name__, "args": sys.argv[1:]}))\n')
    (root / 'source_service.py').write_text('''import sys
from run import main
if __name__ == '__main__':
    raise SystemExit(main(['source_service', *sys.argv[1:]]))
''')
    result = execute(root, "import run; run.main(['source_service', '--help', 'two words'])", ROCOTO_VERTICAL='bare')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'name': '__main__', 'args': ['--help', 'two words']}


def test_package_child_source_and_repository_asset_traversal(tmp_path):
    _, root = tree(tmp_path)
    package = root / 'verticals/bare/recipes'
    package.mkdir()
    (package / '__init__.py').write_text('raise RuntimeError("source lookup must not import")\n')
    (package / 'daily.py').write_text('')
    result = execute(root, '''import layout
layout.install()
assert layout.source('recipes.daily') == layout.VERTICALS / 'bare/recipes/daily.py'
try:
    layout.shared_asset('../outside')
except ValueError:
    print('bounded')
''', ROCOTO_VERTICAL='bare')
    assert result.returncode == 0 and result.stdout.strip() == 'bounded', result.stderr


def test_explicit_install_sets_the_process_vertical_for_following_default_lookups(tmp_path):
    _, root = tree(tmp_path)
    (root / 'verticals/bare/role.py').write_text('')
    result = execute(root, '''import layout
layout.install('bare')
assert layout.selected_vertical() == 'bare'
assert layout.source('role') == layout.VERTICALS / 'bare/role.py'
try:
    layout.install('retail')
except ValueError:
    print('one process, one vertical')
''')
    assert result.returncode == 0 and result.stdout.strip() == 'one process, one vertical', result.stderr


def test_run_supports_package_commands_with_relative_imports(tmp_path):
    _, root = tree(tmp_path)
    package = root / 'verticals/bare/jobs'
    package.mkdir()
    (package / '__init__.py').write_text('VALUE = "package command"\n')
    (package / 'check.py').write_text('from . import VALUE\nprint(VALUE)\n')
    result = execute(root, "import run; run.main(['jobs.check'])", ROCOTO_VERTICAL='bare')
    assert result.returncode == 0 and result.stdout.strip() == 'package command', result.stderr
