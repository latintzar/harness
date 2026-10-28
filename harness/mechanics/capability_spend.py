"""Explicit account-bound email/voice allowances in the existing message ledger.

A host-owned grant supplies an inclusive upper cost per billable unit, including
provider speech charges for voice. Reservations are conservative commitments,
not claims of invoiced cost. Only a provably unstarted request releases one.
The private grant file is reread before every request. Configuration never grants
money by itself: absent, expired, stopped or mismatched authority admits zero.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from pathlib import Path

from messaging_provider import bounded, digest

CHANNELS = ('email', 'voice')
MAX_REQUEST = 1_000_000  # USD microdollars: the host may grant at most $1 per request.
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS capability_spend_holds(outbox TEXT PRIMARY KEY,
       channel TEXT NOT NULL,account TEXT NOT NULL,grant_id TEXT NOT NULL,grant_hash TEXT NOT NULL,
       day TEXT NOT NULL,amount INTEGER NOT NULL,returned INTEGER NOT NULL DEFAULT 0)''',
)
FIELDS = {'grant_id', 'channel', 'account_id', 'expires_at', 'stopped', 'unit_cost_microusd',
          'maximum_request_microusd', 'daily_microusd', 'total_microusd'}


class CapabilitySpendHeld(ValueError):
    def __init__(self):
        super().__init__('Review this channel’s account and spending allowance.')


class CapabilitySpend:
    def __init__(self, state, environment_id, company_id, *, reader=None):
        self.state, self.environment_id, self.company_id = Path(state), environment_id, company_id
        self.reader = reader or self._read

    def _read(self):
        from registered_oauth import read_private_configuration
        path = Path(os.environ.get('ROCOTO_CAPABILITY_SPEND_CONFIG', ''))
        if not path.is_absolute():
            raise CapabilitySpendHeld()
        value = read_private_configuration(path, max_bytes=32768)
        binding = {'schema_version': 1, 'environment_id': self.environment_id, 'company_scope': self.company_id}
        if (not isinstance(value, dict) or set(value) != set(binding) | {'grants'}
                or type(value['schema_version']) is not int
                or any(value.get(key) != expected for key, expected in binding.items())):
            raise CapabilitySpendHeld()
        return value['grants']

    def grant(self, provider, now):
        try:
            grants = self.reader()
            if not isinstance(grants, list) or len(grants) > 16:
                raise ValueError
            selected = []
            for value in grants:
                if (not isinstance(value, dict) or set(value) != FIELDS or value['channel'] not in CHANNELS
                        or not bounded(value['grant_id']) or not bounded(value['account_id'], 256)
                        or type(value['stopped']) is not bool
                        or type(value['expires_at']) not in (int, float) or not math.isfinite(value['expires_at'])
                        or any(type(value[key]) is not int or not 0 <= value[key] <= 10**12 for key in (
                            'unit_cost_microusd', 'maximum_request_microusd', 'daily_microusd', 'total_microusd'))
                        or not 1 <= value['unit_cost_microusd'] <= value['maximum_request_microusd'] <= MAX_REQUEST):
                    raise ValueError
                if value['channel'] == provider.channel and value['account_id'] == provider.account_id:
                    selected.append(value)
            if (provider.company_id != self.company_id or len(selected) != 1
                    or selected[0]['stopped'] or selected[0]['expires_at'] <= now
                    or not selected[0]['daily_microusd'] or not selected[0]['total_microusd']):
                raise ValueError
            return dict(selected[0])
        except Exception:
            raise CapabilitySpendHeld() from None

    def ready(self, provider, now):
        try:
            self.grant(provider, now)
            return True
        except CapabilitySpendHeld:
            return False

    def reserve(self, db, row, provider, prepared, now):
        value = self.grant(provider, now)
        units = provider.units(prepared)
        if (type(units) is not int or not 1 <= units <= 16 or row['channel'] != provider.channel
                or row['account'] != provider.account_id):
            raise CapabilitySpendHeld()
        amount = units * value['unit_cost_microusd']
        if amount > value['maximum_request_microusd']:
            raise CapabilitySpendHeld()
        today = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
        # Immutable pricing/account identity; mutable Stop, expiry and limits may tighten a grant.
        identity = digest({key: value[key] for key in ('grant_id', 'channel', 'account_id', 'unit_cost_microusd')})
        held = db.execute('SELECT * FROM capability_spend_holds WHERE outbox=?', (row['id'],)).fetchone()
        if held is not None and (held['returned'] or held['grant_hash'] != identity or held['amount'] != amount):
            raise CapabilitySpendHeld()
        # Aggregate by account across replacement grants so rotating IDs never resets consumption.
        daily, total = db.execute('''SELECT coalesce(sum(CASE WHEN day=? THEN amount ELSE 0 END),0),
            coalesce(sum(amount),0) FROM capability_spend_holds
            WHERE channel=? AND account=? AND returned=0''', (today, provider.channel, provider.account_id)).fetchone()
        extra = 0 if held is not None else amount
        daily_extra = amount if held is not None and held['day'] != today else extra
        if daily + daily_extra > value['daily_microusd'] or total + extra > value['total_microusd']:
            raise CapabilitySpendHeld()
        if held is None:
            db.execute('INSERT INTO capability_spend_holds VALUES(?,?,?,?,?,?,?,0)',
                       (row['id'], provider.channel, provider.account_id, value['grant_id'], identity, today, amount))
        elif held['day'] != today:
            # The request is still inside the host. Charge the day on which it
            # will actually leave, preserving the original lifetime reservation.
            db.execute('UPDATE capability_spend_holds SET day=? WHERE outbox=?', (today, row['id']))
        return amount


def release(db, outbox):
    db.execute('UPDATE capability_spend_holds SET returned=1 WHERE outbox=?', (outbox,))
