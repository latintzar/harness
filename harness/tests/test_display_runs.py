"""The SDK ledger preserves leases and run histories without a selected vertical."""
from pathlib import Path
import subprocess
import sys

import pytest

from display_runs import DisplayRuns, due_reading


def test_bare_display_ledger_uses_only_shared_modules(tmp_path):
    code = '''import sys
import layout
layout.install('bare')
from display_runs import DisplayRuns
ledger = DisplayRuns(sys.argv[1])
run = ledger.claim('maker', 'orders', 'dispatch', trigger='events', stamp='orders:4', day='2026-10-11')
ledger.finish(run['id'], run['lease_token'], status='completed', stamp=run['stamp'], day=run['day'])
assert ledger.runs('maker', 'orders', 'dispatch')[0]['scope_id'] == 'dispatch'
assert all('/verticals/retail/' not in str(getattr(module, '__file__', '')) for module in sys.modules.values())
'''
    subprocess.run([sys.executable, '-c', code, str(tmp_path)], cwd=Path(__file__).parents[1], check=True)


def test_ledger_restart_keeps_current_lease_and_fences_previous_holder(tmp_path):
    first = DisplayRuns(tmp_path, clock=lambda: 100)
    run = first.claim('maker', 'orders', 'dispatch', trigger='events', stamp='v1', day='one', lease_seconds=10)
    second = DisplayRuns(tmp_path, clock=lambda: 105)
    assert second.claim('maker', 'orders', 'dispatch', trigger='events', stamp='v2', day='one') is None
    replacement = second.claim('maker', 'orders', 'dispatch', trigger='events', stamp='v2', day='one', now=111)
    assert first.renew(run['id'], run['lease_token'], lease_seconds=30, now=112) is False
    stale = first.finish(run['id'], run['lease_token'], status='completed', stamp='v1', now=112)
    assert stale['status'] == 'interrupted'
    assert second.state('maker', 'orders', 'dispatch', now=112)['running'] is True
    second.finish(replacement['id'], replacement['lease_token'], status='completed', stamp='v2', day='one', now=113)
    state = first.state('maker', 'orders', 'dispatch', now=114)
    assert state['processed_stamp'] == 'v2' and state['failures'] == 0
    assert first.runs('other', 'orders', 'dispatch') == []
    assert first.runs('maker', 'other', 'dispatch') == []


def test_schedule_revisions_manual_request_and_job_cleanup_survive_restart(tmp_path):
    ledger = DisplayRuns(tmp_path, clock=lambda: 100)
    assert ledger.state('maker', 'orders', 'dispatch')['policy_revision'] == 1
    assert not ledger.path.exists()
    ledger.set_policy('maker', 'orders', 'dispatch', {'automatic': False}, expected_revision=1)
    with pytest.raises(ValueError, match='current revision'):
        ledger.set_policy('maker', 'orders', 'dispatch', {'automatic': True}, expected_revision=1)
    ledger.request('maker', 'orders', 'dispatch', by='agent-maker', only=['orders-ready'])
    job = {'request_id': 'job-one', 'scope': {'company': 'maker', 'turn': 'one'}}
    ledger.job_open(job, owner='maker', collection='orders', scope_id='dispatch', display_id='orders-ready')
    restart = DisplayRuns(tmp_path, clock=lambda: 101)
    state = restart.state('maker', 'orders', 'dispatch')
    assert state['policy']['automatic'] is False and state['policy_revision'] == 2
    assert due_reading(state, 'v1', 'one', now=101) == ('manual', 'v1', 'one')
    run = restart.claim('maker', 'orders', 'dispatch', trigger='manual', stamp='v1', day='one')
    assert run['only'] == ['orders-ready']
    assert restart.jobs_pending() == [job]
    restart.job_close(job)
    assert ledger.jobs_pending() == []


def test_shared_cadence_keeps_failure_wait_and_attention_until_input_changes(tmp_path):
    ledger = DisplayRuns(tmp_path, clock=lambda: 100)
    for number in range(5):
        now = 100 + number * 1000
        run = ledger.claim('maker', 'orders', 'dispatch', trigger='events', stamp='v1', day='one', now=now)
        ledger.finish(run['id'], run['lease_token'], status='failed', stamp='v1', now=now + 1)
    state = ledger.state('maker', 'orders', 'dispatch', now=10000)
    assert state['needs_attention'] is True
    assert due_reading(state, 'v1', 'one', now=10000) is None
    assert due_reading(state, 'v2', 'one', now=10000) == ('events', 'v2', 'one')
    assert due_reading(state, 'v2', 'one', now=10000, inputs_ready=False) is None
    assert due_reading(state, 'v2', 'one', now=state['next_after'] - 1) is None
