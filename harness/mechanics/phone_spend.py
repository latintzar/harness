"""The spend bound of participant phone chat: daily caps, Stop and fail-closed reads.

Every provider-bound message of a Tracking Agent's phone chat reserves its
billable units here ahead of the provider call: alert deliveries and agent
answers. The host holds one spend record per environment and company
(CHANNELS.md, "Spend bound"); `reader` returns it afresh on every call, so the
host's Stop and a changed cap apply to the next claim. A missing, unreadable or
malformed record, an unreadable counter or an absent cap admits zero sends.
The same record's Stop and readability hold every other phone send of the
company as well (`admit`): security codes, the greeting and answers elsewhere.
The counters live in the company's messaging store, so a reservation commits
in the same transaction that marks its message dispatching.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

FIELDS = ('company_daily_messages', 'agent_daily_messages', 'stopped')
MAX_CAP = 1_000_000
MAX_UNITS = 16

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS phone_spend(day TEXT NOT NULL,scope TEXT NOT NULL,units INTEGER NOT NULL,
        PRIMARY KEY(day,scope))''',
    '''CREATE TABLE IF NOT EXISTS phone_spend_holds(outbox TEXT PRIMARY KEY,agent TEXT NOT NULL,day TEXT NOT NULL,
        units INTEGER NOT NULL,returned INTEGER NOT NULL DEFAULT 0)''',
)

# What a refused agent answer reads. Deliveries record the reason code instead.
SENTENCES = {
    'spend_cap': 'Today’s phone alert limit is reached. Phone messages resume at the next UTC midnight.',
    'stopped': 'Phone messages for this company are paused.',
    'spend_unreadable': 'Phone messages wait for this company’s daily message limit.',
}


class SpendHeld(ValueError):
    """The bound admits zero units for this message; `reason` is the delivery's held_reason."""

    def __init__(self, reason):
        super().__init__(SENTENCES[reason])
        self.reason = reason


def record(reader):
    """The host's spend record, validated. Anything else is unreadable."""
    try:
        value = reader() if callable(reader) else None
    except Exception:
        value = None
    if (not isinstance(value, dict) or set(value) != set(FIELDS) or type(value['stopped']) is not bool
            or any(type(value[key]) is not int or not 0 <= value[key] <= MAX_CAP for key in FIELDS[:2])):
        raise SpendHeld('spend_unreadable')
    return value


def admit(reader):
    """The host's answer ahead of any provider-bound phone send of the company.

    The host's Stop holds every phone send: security codes, the greeting, an
    agent's answer in any conversation and alert deliveries. A spend record the
    host cannot read holds them too. Units are counted for a Tracking Agent's
    alerts and answers alone (`reserve`); a security code keeps its own built
    member, number and company limits.
    """
    if record(reader)['stopped']:
        raise SpendHeld('stopped')


def day(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')


def resets_at(now):
    """The next UTC midnight."""
    today = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today + timedelta(days=1)).timestamp()


def _used(db, today, scope):
    try:
        row = db.execute('SELECT units FROM phone_spend WHERE day=? AND scope=?', (today, scope)).fetchone()
    except sqlite3.Error:
        raise SpendHeld('spend_unreadable') from None
    units = 0 if row is None else row[0]
    if type(units) is not int or units < 0:
        raise SpendHeld('spend_unreadable')
    return units


def reserve(db, reader, agent, units, now, outbox):
    """Add one message's units to the agent's and the company's counters for the current UTC day.

    Runs inside the caller's write transaction, the one that marks the message
    dispatching. Raises SpendHeld with the reason when the bound admits nothing.
    """
    value = record(reader)
    if value['stopped']:
        raise SpendHeld('stopped')
    if type(units) is not int or not 1 <= units <= MAX_UNITS or not isinstance(agent, str) or not agent:
        raise SpendHeld('spend_unreadable')
    today = day(now)
    company, own = _used(db, today, 'company'), _used(db, today, 'agent:' + agent)
    if company + units > value['company_daily_messages'] or own + units > value['agent_daily_messages']:
        raise SpendHeld('spend_cap')
    for scope, used in (('company', company), ('agent:' + agent, own)):
        db.execute('INSERT OR REPLACE INTO phone_spend(day,scope,units) VALUES(?,?,?)', (today, scope, used + units))
    db.execute('INSERT INTO phone_spend_holds(outbox,agent,day,units) VALUES(?,?,?,?)', (outbox, agent, today, units))
    return units


def release(db, outbox):
    """Return the units of a send that provably stayed inside Rocoto. Sent, failed and uncertain keep theirs."""
    hold = db.execute('SELECT agent,day,units FROM phone_spend_holds WHERE outbox=? AND returned=0', (outbox,)).fetchone()
    if hold is None:
        return 0
    for scope in ('company', 'agent:' + hold[0]):
        db.execute('UPDATE phone_spend SET units=max(0,units-?) WHERE day=? AND scope=?', (hold[2], hold[1], scope))
    db.execute('UPDATE phone_spend_holds SET returned=1 WHERE outbox=?', (outbox,))
    return hold[2]


def usage(db, reader, agent, now):
    """The phone block's spend object: units used today against each cap, and the state they give."""
    result = {'state': 'unavailable', 'agent': None, 'company': None, 'resets_at': resets_at(now)}
    try:
        value = record(reader)
        today = day(now)
        company = 0 if db is None else _used(db, today, 'company')
        own = 0 if db is None else _used(db, today, 'agent:' + agent)
    except SpendHeld:
        return result
    result.update(agent={'used': own, 'cap': value['agent_daily_messages']},
        company={'used': company, 'cap': value['company_daily_messages']},
        state='stopped' if value['stopped'] else 'cap_reached' if (
            own >= value['agent_daily_messages'] or company >= value['company_daily_messages']) else 'open')
    return result
