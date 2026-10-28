"""Fresh Modal launch uses private host config and carries no model API credentials."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import cosmos_inspect


def private_config(tmp_path, monkeypatch):
    config = tmp_path / 'modal.toml'
    config.write_text('[fixture]\nactive = true\n')
    config.chmod(0o600)
    monkeypatch.setenv('ROCOTO_MODAL_CONFIG_PATH', str(config))
    return config


def test_fresh_child_uses_host_modal_account_and_excludes_openai_azure_credentials(tmp_path, monkeypatch):
    config = private_config(tmp_path, monkeypatch)
    sensitive = ['OPENAI_API_KEY', 'OPENAI_ORG_ID', 'ROCOTO_OPENAI_API_KEY',
                 'ROCOTO_OPENAI_API_KEY_FILE', 'AZURE_OPENAI_API_KEY', 'AZURE_CLIENT_SECRET',
                 'AZURE_CLIENT_ID', 'ROCOTO_AZURE_TOKEN', 'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET']
    for name in sensitive:
        monkeypatch.setenv(name, 'synthetic-fixture-only')
    monkeypatch.setenv('MODAL_CONFIG_PATH', '/wrong/inherited/config')
    record = {'id': 'clip-a', 'video_id': 'source-a', 'path': 'unused',
              'camera': 'A', 'dataset': 'cafe', 'start': 0, 'end': 6}
    question = 'Describe the visible scene.'
    clips = [{**record, 'source_video_id': 'source-a', 'sha256': 'fixture-source-hash'}]
    requests = [{'clip_ids': ['clip-a'], 'question': question}]
    monkeypatch.setattr(cosmos_inspect, '_reuse_prepared_source', lambda *a, **kw: True)
    monkeypatch.setattr('camera_context.get_context_for_clip', lambda *a: None)
    monkeypatch.setattr(cosmos_inspect, 'load_manifest', lambda *a, **kw: (clips, requests))
    seen = []
    class Process:
        returncode = 0
        async def wait(self):
            return 0
    async def launch(*command, **kwargs):
        seen.append(kwargs['env'])
        output = Path(command[command.index('--output') + 1])
        output.write_text(json.dumps({'requests': [{'parsed': True}]}))
        return Process()
    monkeypatch.setattr(cosmos_inspect.asyncio, 'create_subprocess_exec', launch)
    result = asyncio.run(cosmos_inspect.inspect_live(['clip-a'], question,
        index=SimpleNamespace(get_clip=lambda _: record), state=tmp_path, modal_python=sys.executable))
    assert result['cached'] is False
    assert len(seen) == 1
    assert seen[0]['MODAL_CONFIG_PATH'] == str(config)
    assert all(name not in seen[0] for name in sensitive)
    assert 'PATH' in seen[0]


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o606])
def test_shared_modal_account_permissions_fail_before_launch(tmp_path, monkeypatch, mode):
    config = private_config(tmp_path, monkeypatch)
    config.chmod(mode)
    with pytest.raises(ValueError, match='private Modal account file'):
        cosmos_inspect._modal_runtime(sys.executable)


def test_missing_or_empty_modal_account_requires_private_host_configuration(tmp_path, monkeypatch):
    config = private_config(tmp_path, monkeypatch)
    config.write_text('')
    with pytest.raises(ValueError, match='private Modal account file'):
        cosmos_inspect._modal_runtime(sys.executable)
    config.unlink()
    with pytest.raises(ValueError, match='private Rocoto Modal account file'):
        cosmos_inspect._modal_runtime(sys.executable)


def test_modal_account_must_belong_to_service_user(tmp_path, monkeypatch):
    config = private_config(tmp_path, monkeypatch)
    different_owner = config.stat().st_uid + 1
    monkeypatch.setattr(cosmos_inspect.os, 'geteuid', lambda: different_owner)
    with pytest.raises(ValueError, match='owned by the service user'):
        cosmos_inspect._modal_runtime(sys.executable)
