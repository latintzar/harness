"""Monitored estimates, durable deadlines and cancellation without provider calls."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from managed_spend import SpendMonitor, spending_mode


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def snapshot(inputs=0, outputs=0, cached=0):
    return {'id': 'session-1', 'usage': {'input_tokens': inputs, 'output_tokens': outputs,
            'input_tokens_details': {'cached_tokens': cached}}}


def test_estimate_counts_reasoning_once_and_keeps_high_water_mark():
    monitor = SpendMonitor(100, time.time() + 100)
    value = snapshot(10_000, 10_000, 5_000)
    value['usage']['output_tokens_details'] = {'reasoning_tokens': 8_000}
    receipt = monitor.observe(value)
    assert receipt['observed_cents'] == pytest.approx(56.75)
    assert receipt['basis'] == 'token_estimate'
    assert receipt['billed_cents'] is None
    assert monitor.observe(snapshot())['observed_cents'] == receipt['observed_cents']


def test_unknown_usage_is_unknown_and_original_baseline_survives_recovery():
    original = SpendMonitor(100, time.time() + 100, baseline=50)
    assert original.observe({'id': 'session-1', 'usage': None})['usage_status'] == 'unknown'
    receipt = original.observe(snapshot(outputs=15_000))
    recovered = SpendMonitor.from_receipt(receipt)
    assert recovered.deadline_at == original.deadline_at
    assert recovered.observe(snapshot(outputs=20_000))['observed_cents'] == 50
    assert recovered.receipt()['baseline'] == 50


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['target', 'deadline', 'malformed', 'transport', 'wrong_session'])
async def test_watch_cancels_for_target_deadline_or_monitor_failure(failure):
    deadline = time.time() - 1 if failure == 'deadline' else time.time() + 100
    monitor = SpendMonitor(100, deadline)
    cancelled, receipts = [], []
    async def read(session_id):
        if failure == 'transport':
            raise OSError('private provider response')
        if failure == 'malformed':
            return {'id': session_id, 'usage': {'input_tokens': -1, 'output_tokens': 0}}
        if failure == 'wrong_session':
            return {'id': 'another-session', 'usage': None}
        return snapshot(outputs=20_000)
    async def cancel(reason):
        cancelled.append(reason)
    reason = await monitor.watch(SimpleNamespace(get_session=read), 'session-1', cancel,
                                 persist=receipts.append, poll_interval=0.01)
    assert reason == ('target_reached' if failure == 'target' else 'deadline' if failure == 'deadline' else 'monitor_failed')
    assert cancelled == [reason]
    assert receipts[-1]['stop_reason'] == reason
    assert 'private provider' not in str(receipts)


def test_explicit_mode_and_conservative_long_context_pricing(monkeypatch):
    monkeypatch.delenv('ROCOTO_MANAGED_SPENDING_MODE', raising=False)
    assert spending_mode() == 'enforced'
    monkeypatch.setenv('ROCOTO_MANAGED_SPENDING_MODE', 'monitored')
    assert spending_mode() == 'monitored'
    with pytest.raises(ValueError):
        spending_mode('unlimited')
    assert SpendMonitor(100, time.time() + 100).observe(snapshot(300_000, 1_000))['observed_cents'] == 757.5
