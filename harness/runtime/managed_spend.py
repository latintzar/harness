"""Opt-in token estimates and a persisted wall deadline for managed execution.

These estimates never settle the money ledger. OpenAI reports best-effort session
usage, including session work, separately from final billing and hosted/tool fees.
Rates: https://developers.openai.com/api/docs/models/gpt-6-astra (2026-10-13).
"""
from __future__ import annotations

import asyncio
import inspect
import math
import os
import time


MAX_ACTIVE_SECONDS = 300


def spending_mode(value=None):
    selected = os.environ.get('ROCOTO_MANAGED_SPENDING_MODE', 'enforced') if value is None else value
    if selected not in {'enforced', 'monitored'}:
        raise ValueError('Select enforced or explicitly authorized monitored managed spending.')
    return selected


def estimate_cents(snapshot, model='gpt-6-astra'):
    """Conservative token estimate; missing optional usage stays unknown.

    Input is charged at the cache-write rate because this usage schema omits
    cache-write counts. Aggregate input above 272K uses the long-context rates.
    Reasoning is already part of output_tokens. Hosted/tool fees remain unknown.
    """
    if model != 'gpt-6-astra':
        raise ValueError('Configure verified token rates for this managed model.')
    usage = snapshot.get('usage')
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError('Read valid managed token usage.')
    inputs, outputs = usage.get('input_tokens'), usage.get('output_tokens')
    details = usage.get('input_tokens_details') or {}
    if not isinstance(details, dict):
        raise ValueError('Read valid managed token details.')
    cached = details.get('cached_tokens', 0)
    if (any(type(value) is not int or value < 0 for value in (inputs, outputs, cached))
            or cached > inputs):
        raise ValueError('Read valid managed token counts.')
    # USD per million => cents per token. Cache-write premium is conservative.
    input_rate, cached_rate, output_rate = (25, 2, 75) if inputs > 272_000 else (12.5, 1, 50)
    return ((inputs - cached) * input_rate + cached * cached_rate + outputs * output_rate) / 10_000


class SpendMonitor:
    def __init__(self, target_cents, deadline_at, model='gpt-6-astra', baseline=None, observed_cents=0):
        if type(target_cents) is not int or not 0 < target_cents <= 100:
            raise ValueError('Use the approved monitored spending target.')
        values = (deadline_at, observed_cents) + (() if baseline is None else (baseline,))
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise ValueError('Use a finite saved monitored deadline and estimate.')
        if observed_cents < 0 or baseline is not None and baseline < 0:
            raise ValueError('Use nonnegative monitored estimates.')
        estimate_cents({}, model)
        self.target_cents, self.deadline_at, self.model = target_cents, deadline_at, model
        self.baseline, self.observed_cents = baseline, observed_cents
        self.usage_status = 'unknown'
        self.stop_reason = None

    @classmethod
    def from_receipt(cls, receipt):
        monitor = cls(receipt['target_cents'], receipt['deadline_at'], receipt['model'],
                      receipt.get('baseline'), receipt.get('observed_cents', 0))
        monitor.usage_status = receipt.get('usage_status', 'unknown')
        monitor.stop_reason = receipt.get('stop_reason')
        return monitor

    def receipt(self):
        return {'mode': 'monitored', 'target_cents': self.target_cents, 'deadline_at': self.deadline_at,
                'model': self.model, 'baseline': self.baseline, 'observed_cents': self.observed_cents,
                'usage_status': self.usage_status, 'basis': 'token_estimate', 'billed_cents': None,
                'other_provider_costs': 'unknown', 'stop_reason': self.stop_reason}

    def observe(self, snapshot):
        estimated = estimate_cents(snapshot, self.model)
        self.usage_status = 'unknown' if estimated is None else 'estimated'
        if estimated is not None:
            self.observed_cents = max(self.observed_cents, estimated - (self.baseline or 0))
        return self.receipt()

    async def watch(self, client, session_id, cancel, persist=None, *, poll_interval=1):
        """Poll until cancellation is requested; caller owns root/child Stop proof.

        The caller persists receipt() before submitting input and every update.
        Unknown optional usage continues under the original deadline. Transport,
        malformed usage and persistence failures withdraw execution immediately.
        """
        if not 0 < poll_interval <= 5:
            raise ValueError('Poll managed spending at least every five seconds.')
        while True:
            try:
                remaining = self.deadline_at - time.time()
                if remaining <= 0:
                    self.stop_reason = 'deadline'
                elif self.observed_cents >= self.target_cents:
                    self.stop_reason = 'target_reached'
                if self.stop_reason is None:
                    async with asyncio.timeout(min(10, remaining)):
                        snapshot = await client.get_session(session_id)
                    if not isinstance(snapshot, dict) or snapshot.get('id') != session_id:
                        raise ValueError('Monitor the original managed session.')
                    self.observe(snapshot)
                    if self.observed_cents >= self.target_cents:
                        self.stop_reason = 'target_reached'
                if persist is not None:
                    result = persist(self.receipt())
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stop_reason = 'monitor_failed'
            if self.stop_reason is not None:
                try:
                    await cancel(self.stop_reason)
                finally:
                    if persist is not None:
                        result = persist(self.receipt())
                        if inspect.isawaitable(result):
                            await result
                return self.stop_reason
            await asyncio.sleep(min(poll_interval, max(0, self.deadline_at - time.time())))
