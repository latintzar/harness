"""Portable source export and explicit local allowance, without provider work."""
import importlib.util
from decimal import Decimal
import json
from pathlib import Path
import stat

import pytest

ROOT = Path(__file__).resolve().parents[2]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clean_export_packaging_uses_only_confined_reviewed_paths(tmp_path):
    builder = load('public_preview_builder', ROOT / 'harness/deploy/build_preview.py')
    (tmp_path / 'provenance').mkdir()
    inventory = tmp_path / 'provenance/release-files.json'
    inventory.write_text(json.dumps({'files': ['harness/run.py', 'backend/db.py']}))
    assert builder.source_paths(tmp_path, 'harness') == {tmp_path / 'harness/run.py'}
    inventory.write_text(json.dumps({'files': ['../private-state/key']}))
    with pytest.raises(ValueError, match='inside the source tree'):
        builder.source_paths(tmp_path, 'harness')


def test_local_operator_allowance_retains_existing_company_bound_authority(tmp_path):
    tool = load('public_local_allowance', ROOT / 'tools/authorize_local_budget.py')
    state = tmp_path / 'private'
    result = tool.authorize(state, '2.00', 7, acknowledged=True)
    assert Decimal(result['allowance_usd']) == Decimal('2.00')
    assert Decimal(result['available_usd']) == Decimal('2.00')
    assert result['provider_requests'] == 0
    records = list((state / 'operator-authorizations').glob('*.json'))
    assert len(records) == 1
    assert stat.S_IMODE(records[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(records[0].parent.stat().st_mode) == 0o700
    record = json.loads(records[0].read_text())
    assert record['kind'] == 'operator_demo_budget'
    assert record['user_authorized'] is True
    from agent_auth import AgentAuth
    status = AgentAuth(state, mode='openai_api', environ={'ROCOTO_OPENAI_API_KEY': 'local-fixture-key'}).status()
    assert status['inference_enabled'] is True
    assert status['billing_status'] == 'operator_native_request_metering'
    # This is local admission readiness; no upstream access was tested.
    with pytest.raises(ValueError, match='existing demo allowance'):
        tool.authorize(state, '3.00', 7, acknowledged=True)


def test_local_allowance_requires_explicit_choice_and_private_external_state(tmp_path):
    tool = load('public_local_allowance_validation', ROOT / 'tools/authorize_local_budget.py')
    with pytest.raises(ValueError, match='Acknowledge'):
        tool.authorize(tmp_path / 'state', '2', 7, acknowledged=False)
    with pytest.raises(ValueError, match='outside'):
        tool.authorize(ROOT / 'private-state', '2', 7, acknowledged=True)
    with pytest.raises(ValueError, match='whole cents'):
        tool.authorize(tmp_path / 'state', '2.005', 7, acknowledged=True)
    with pytest.raises(ValueError, match='at most'):
        tool.authorize(tmp_path / 'state', '21', 7, acknowledged=True)
