"""Fresh Cosmos children retain the relocated harness imports and credential boundary."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import camera_context
import cosmos_inspect
import execution_policy
import layout


class Process:
    returncode = 0

    async def wait(self):
        return 0


def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(cosmos_inspect.os, 'environ', {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
    config = tmp_path / 'synthetic-modal.toml'
    config.write_text('synthetic account configuration; never sent\n')
    config.chmod(0o600)
    monkeypatch.setenv('ROCOTO_MODAL_CONFIG_PATH', str(config))
    monkeypatch.setenv('ROCOTO_MODAL_PYTHON', sys.executable)
    for name in ('OPENAI_API_KEY', 'ROCOTO_OPENAI_API_KEY', 'AZURE_OPENAI_API_KEY',
                 'ROCOTO_AZURE_API_KEY', 'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET'):
        monkeypatch.setenv(name, 'synthetic-never-sent')
    return config


def assert_worker(argv, environment, config):
    assert argv[:3] == (sys.executable, str(layout.ROOT / 'run.py'), 'cosmos_inspect')
    assert environment['MODAL_CONFIG_PATH'] == str(config)
    assert not any(key.upper().startswith(('OPENAI_', 'ROCOTO_OPENAI_', 'AZURE_', 'ROCOTO_AZURE_'))
                   or key in {'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET'} for key in environment)


def test_fresh_inspection_launch_uses_bootstrap_and_keeps_credential_scrub(monkeypatch, tmp_path):
    config = runtime(monkeypatch, tmp_path)
    calls = []
    record = {'id': 'clip', 'camera': 'Camera', 'dataset': 'shop', 'video_id': 'source',
              'start': 0, 'end': 2, 'sha256': 'synthetic', 'path': 'synthetic'}
    monkeypatch.setattr(cosmos_inspect, '_reuse_prepared_source', lambda *args, **kwargs: True)
    monkeypatch.setattr(cosmos_inspect, 'load_manifest', lambda *args, **kwargs: ([record], []))
    monkeypatch.setattr(camera_context, 'get_context_for_clip', lambda *args: None)
    monkeypatch.setattr(execution_policy, 'admit_fresh_inspection', lambda *args: None)

    async def launch(*argv, **kwargs):
        calls.append((argv, kwargs['env']))
        Path(argv[argv.index('--output') + 1]).write_text(json.dumps({'requests': []}))
        return Process()

    monkeypatch.setattr(cosmos_inspect.asyncio, 'create_subprocess_exec', launch)
    result = asyncio.run(cosmos_inspect.inspect_live(['clip'], 'Describe visible movement.',
        index=SimpleNamespace(get_clip=lambda clip: record), state=tmp_path))
    assert result['cached'] is False and len(calls) == 1
    assert_worker(*calls[0], config)
    assert '--query-approval' in calls[0][0]


def test_stop_receipt_launch_uses_same_bootstrap_and_scrub(monkeypatch, tmp_path):
    config = runtime(monkeypatch, tmp_path)
    interpreter, environment = cosmos_inspect._modal_runtime()
    receipt = tmp_path / 'result.app.json'
    receipt.write_text('{"app_id":"ap-synthetic"}\n')
    calls = []

    async def launch(*argv, **kwargs):
        calls.append((argv, kwargs['env']))
        return Process()

    monkeypatch.setattr(cosmos_inspect.asyncio, 'create_subprocess_exec', launch)
    asyncio.run(cosmos_inspect._stop_remote_receipt(tmp_path, interpreter, environment))
    assert len(calls) == 1
    assert_worker(*calls[0], config)
    assert calls[0][0][3:] == ('--stop-receipt', str(receipt))


def test_fresh_process_dispatch_resolves_live_query_and_derivative_dependencies():
    # Enter the actual run.main path in a clean child, stopping at module dispatch before
    # Cosmos can read an authority, construct an SDK client or contact a provider.
    script = f'''import sys,runpy
sys.path.insert(0, {str(layout.ROOT)!r})
import run
def inspect_dispatch(name, **kwargs):
    assert name == 'cosmos_inspect'
    import company_usage, workspace_identity, source_derivatives
    print('live-query and derivative dependencies resolved')
runpy.run_module = inspect_dispatch
run.main(['cosmos_inspect', '--help'])
'''
    environment = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'ROCOTO_VERTICAL': 'retail'}
    result = subprocess.run([sys.executable, '-c', script], env=environment, cwd='/',
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'live-query and derivative dependencies resolved'
