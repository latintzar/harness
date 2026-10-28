"""Verify the installed CLI's effective capabilities without any model request."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from deploy.migration.legacy.native_policy import DISABLED_FEATURES, cli_args, thread_params, policy_digest


def test_installed_codex_disables_local_readers_hooks_and_personal_connectors():
    binary = shutil.which('codex')
    if not binary:
        pytest.skip('Installed native CLI supplies the effective capability proof')
    result = subprocess.run([binary, *cli_args(), 'features', 'list'],
                            capture_output=True, text=True, timeout=15, check=True)
    values = {parts[0]: parts[-1] for line in result.stdout.splitlines()
              if (parts := line.split())}
    for feature in DISABLED_FEATURES:
        assert values[feature] == 'false', feature
    assert values['skip_host_skill_discovery'] == 'true'


def test_permission_or_tool_changes_invalidate_native_configuration_fingerprint():
    original = thread_params('/company/private', 'Instructions', [])
    digest = policy_digest(original)
    for key, value in [('sandbox', 'danger-full-access'), ('approvalPolicy', 'on-request'),
                       ('cwd', '/other-company'), ('config', {}),
                       ('dynamicTools', [{'name': 'read_any_file'}])]:
        changed = json.loads(json.dumps(original))
        changed[key] = value
        assert policy_digest(changed) != digest
