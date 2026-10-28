"""Historical Rocoto adapters remain importable only through explicit migration paths."""
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_preview_build import builder, HERE, REPO

LEGACY = ('isolated_turn', 'isolated_native', 'native_thread_tools', 'native_policy',
          'native_files', 'native_api_smoke', 'managed_executor')


@pytest.mark.parametrize('vertical', ['bare', 'retail'])
def test_published_runtime_excludes_historical_adapters(vertical):
    selected, untracked = builder().artifact_files(REPO, HERE, vertical_id=vertical)
    assert not untracked
    assert {path.stem for path in selected}.isdisjoint(LEGACY)
    assert all(not path.is_relative_to(HERE / 'deploy/migration/legacy') for path in selected)
    assert HERE / 'runtime/managed_runtime.py' in selected
    assert HERE / 'runtime/provider_tool_policy.py' in selected


def test_historical_adapters_import_through_explicit_package():
    for name in LEGACY:
        module = importlib.import_module('deploy.migration.legacy.' + name)
        assert Path(module.__file__).parent == HERE / 'deploy/migration/legacy'
        assert not (HERE / 'runtime' / (name + '.py')).exists()


@pytest.mark.parametrize('vertical', ['bare', 'retail'])
def test_managed_entrypoint_import_keeps_legacy_outside_runtime(vertical):
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'ROCOTO_PACK', 'ROCOTO_VERTICAL', 'PYTHONPATH'}}
    environment.update(ROCOTO_VERTICAL=vertical, PYTHONDONTWRITEBYTECODE='1')
    code = '''import sys, layout
layout.install()
import agent_bridge, managed_runtime, managed_policy, provider_tool_policy
assert not any(name.startswith('deploy.migration.legacy') for name in sys.modules)
assert not set(%r) & set(layout.modules())
''' % (LEGACY,)
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=HERE, env=environment,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
