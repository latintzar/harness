"""Host alert delivery of participant phone chat (CHANNELS.md, "Host alert delivery").

One processed alert revision of a Tracking Agent fans out to each participant
whose binding is enabled, under one stable delivery ID per participant and
alert revision. Delivery is host work: it starts no run, ends no run and needs
no model turn or tool call. The records live in the company's messaging store,
so a claim marks its delivery dispatching, writes its outbox row and reserves
its units in one transaction. Each attempt is one outbox row: receipts, reply
continuation and the alerts-sent statistics read it as they read any message.

A phone alert is text and one link into Rocoto. It carries no image, clip,
thumbnail or link preview. An edit-only change, a paused delivery policy, the
compiled-data replay lane and a Core Tracking Module alert write zero
deliveries. A held or cancelled delivery stays unsent for good.

Delivery follows the agent's own phone policy and stays independent of its run
activation. Retained history is bounded: the worker removes a delivery that
reached a provider 366 days after its last change, the window the activity
statistics read from the same outbox, and one that stayed unsent after 24
hours. An uncertain send stays reviewable for 24 hours, then closes.

`python3 phone_delivery.py reconcile <state> <delivery_id> <outcome> --by <name>`
records the host operator's review of an uncertain send.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import quote

from messaging_provider import ProviderError, bounded, digest
from phone_spend import SpendHeld, release, reserve, usage

DELIVERY_SECONDS = 900
MAX_ATTEMPTS = 3      # a send that provably stayed inside Rocoto is retried up to three times
TEXT_LIMIT = 600
SMS_SEGMENTS = 3
MAX_DELIVERIES = 20000
MAX_ALERTS = 60000
RECONCILE_SECONDS = 86400        # an uncertain send stays reviewable this long, then closes as expired
SENT_RETENTION_SECONDS = 366 * 86400  # a delivery that reached a provider: the activity statistics' window
UNSENT_RETENTION_SECONDS = 86400      # held, cancelled or expired: it never left Rocoto
PRUNE_EVERY_SECONDS = 60
RANK = {'sent': 1, 'failed': 2, 'delivered': 3, 'read': 4}
REACHED = ('sent', 'delivered', 'read', 'failed')
UNSENT = ('held', 'cancelled', 'expired')
LANES = ('live', 'replay')

SCHEMA = (
    # One row per alert revision the host read for an agent: the fan-out receipt and its chat entry state.
    '''CREATE TABLE IF NOT EXISTS phone_alerts(agent TEXT NOT NULL,alert TEXT NOT NULL,revision INTEGER NOT NULL,
        chat TEXT NOT NULL,title TEXT NOT NULL,summary TEXT NOT NULL,text TEXT NOT NULL,link TEXT NOT NULL,
        created REAL NOT NULL,deliveries INTEGER NOT NULL,version INTEGER NOT NULL,posted INTEGER NOT NULL,
        PRIMARY KEY(agent,alert,revision))''',
    '''CREATE TABLE IF NOT EXISTS phone_deliveries(id TEXT PRIMARY KEY,agent TEXT NOT NULL,chat TEXT NOT NULL,
        member TEXT NOT NULL,alert TEXT NOT NULL,revision INTEGER NOT NULL,channel TEXT,account TEXT,
        text TEXT NOT NULL,link TEXT NOT NULL,status TEXT NOT NULL,held_reason TEXT,
        binding_generation INTEGER NOT NULL,address_generation INTEGER,policy_generation INTEGER NOT NULL,
        provider_message_id TEXT,units INTEGER NOT NULL,attempts INTEGER NOT NULL,created REAL NOT NULL,
        updated REAL NOT NULL,expires REAL NOT NULL,outbox TEXT,tried TEXT NOT NULL DEFAULT '[]',
        UNIQUE(agent,member,alert,revision))''',
    'CREATE INDEX IF NOT EXISTS phone_delivery_status ON phone_deliveries(status,created)',
    'CREATE INDEX IF NOT EXISTS phone_delivery_outbox ON phone_deliveries(outbox)',
    'CREATE INDEX IF NOT EXISTS phone_delivery_member ON phone_deliveries(agent,member,updated)',
    'CREATE INDEX IF NOT EXISTS phone_alert_posted ON phone_alerts(posted,version)',
    # The lane an observation arrived in, decided once at its first read and kept.
    'CREATE TABLE IF NOT EXISTS phone_lanes(alert TEXT PRIMARY KEY,lane TEXT NOT NULL,decided REAL NOT NULL)',
    # The host operator's recorded review of an uncertain send.
    '''CREATE TABLE IF NOT EXISTS phone_reviews(delivery TEXT NOT NULL,outcome TEXT NOT NULL,reviewed_by TEXT NOT NULL,
        reviewed REAL NOT NULL)''',
)


class Held(Exception):
    """An admission check stopped the send ahead of the provider call."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def delivery_id(company_id, agent_id, member_id, alert_id, alert_revision):
    """The same ID after a retry, a restart, a duplicate outbox claim and a channel fallback."""
    compact = json.dumps([company_id, agent_id, member_id, alert_id, alert_revision], separators=(',', ':'))
    return 'dlv-' + hashlib.sha256(compact.encode()).hexdigest()[:32]


def clean(value, limit):
    """One line of plain text within its limit."""
    text = ' '.join(''.join(c if ord(c) >= 32 else ' ' for c in str(value)).split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + '...'


def alert_lead(title, revision=1):
    """The agent's title; a later revision names the alert it updates."""
    lead = clean(title, 100)
    return lead if revision == 1 else 'Update to an earlier alert from ' + lead


def alert_text(title, summary, revision=1):
    """The agent's title and the alert's summary in at most 600 characters."""
    return clean(alert_lead(title, revision) + ': ' + clean(summary, TEXT_LIMIT), TEXT_LIMIT)


def alert_link(origin, chat_id, alert_id):
    """One link into Rocoto. It opens the alert after the member's ordinary sign-in and carries no token."""
    if not isinstance(origin, str) or not origin.startswith('https://'):
        return ''
    return f"{origin.rstrip('/')}/?chat={quote(chat_id, safe='')}&alert={quote(alert_id, safe='')}"


def mask(address):
    """An E.164 number keeps its first four characters and last two digits; another address its last four."""
    if not isinstance(address, str) or not address:
        return None
    if address.startswith('+') and address[1:].isdigit() and len(address) > 6:
        return address[:4] + '•' * (len(address) - 6) + address[-2:]
    return '••••' + address[-4:]


def public(row):
    return {'id': row['id'], 'agent_id': row['agent'], 'chat_id': row['chat'], 'member_id': row['member'],
        'alert_id': row['alert'], 'alert_revision': row['revision'], 'channel': row['channel'], 'account': row['account'],
        'text': row['text'], 'link': row['link'], 'status': row['status'], 'held_reason': row['held_reason'],
        'binding_generation': row['binding_generation'], 'address_generation': row['address_generation'],
        'policy_generation': row['policy_generation'], 'provider_message_id': row['provider_message_id'],
        'units': row['units'], 'attempts': row['attempts'], 'created_at': row['created'], 'updated_at': row['updated'],
        'expires_at': row['expires']}


# Store rules shared with the messaging store and the participant records. Each runs
# inside its caller's write transaction on the company's messaging store.

def _update(db, row, now, **changes):
    changes['updated'] = now
    db.execute('UPDATE phone_deliveries SET ' + ','.join(key + '=?' for key in changes) + ' WHERE id=?',
        (*changes.values(), row['id']))
    db.execute('UPDATE phone_alerts SET version=version+1 WHERE agent=? AND alert=? AND revision=?',
        (row['agent'], row['alert'], row['revision']))


def settle(db, outbox, status, provider_id, now, returned=0):
    """The provider's answer to one attempt sets the delivery's status.

    `returned` is the units the spend bound took back for a send that provably
    stayed inside Rocoto. Sent, failed and uncertain keep theirs.
    """
    row = db.execute('SELECT * FROM phone_deliveries WHERE outbox=?', (outbox,)).fetchone()
    if row is None or row['status'] != 'dispatching':
        return
    # An alert suspends no turn: it waits for nothing, and a reply to it is an ordinary new message.
    db.execute('UPDATE outbox SET waiting=0 WHERE id=?', (outbox,))
    tried = json.loads(row['tried'])
    if status == 'cancelled':
        if stopped(db, row):
            # Stop reached this delivery at the fence: it stays unsent for good, also after a resume.
            _update(db, row, now, status='cancelled', units=max(0, row['units'] - returned))
            return
        # It waits for its next claim, up to three attempts inside its 900 seconds.
        exhausted = row['attempts'] - len(tried) >= MAX_ATTEMPTS
        _update(db, row, now, status='held' if exhausted else 'queued', units=max(0, row['units'] - returned),
            held_reason='channel_unready' if exhausted else None)
        return
    if status == 'failed':
        choice = db.execute('SELECT channel_choice FROM recipient_bindings WHERE agent=? AND member=?',
            (row['agent'], row['member'])).fetchone()
        if choice is not None and choice[0] == 'automatic' and now < row['expires']:
            # A definitive rejection returns the same delivery to the next eligible channel, one at a time.
            _update(db, row, now, status='queued', tried=json.dumps(tried + [row['channel']]))
            return
    _update(db, row, now, status=status, provider_message_id=provider_id)


def stopped(db, row):
    """The agent's policy is paused, or this participant switched their alerts off or left."""
    policy = db.execute('SELECT delivery FROM phone_policies WHERE agent=?', (row['agent'],)).fetchone()
    binding = db.execute('''SELECT b.enabled FROM phone_participants p JOIN recipient_bindings b
        ON b.agent=p.agent AND b.member=p.member WHERE p.agent=? AND p.member=?
        AND p.removed IS NULL AND b.revoked IS NULL''', (row['agent'], row['member'])).fetchone()
    return policy is None or policy['delivery'] != 'on' or binding is None or not binding['enabled']


def receipt(db, outbox, status, now):
    """A provider receipt moves a delivery forward in the built order sent, failed, delivered, read."""
    row = db.execute('SELECT * FROM phone_deliveries WHERE outbox=?', (outbox,)).fetchone()
    if row is not None and row['status'] in RANK and RANK.get(status, 0) > RANK[row['status']]:
        _update(db, row, now, status=status)


def restart(db, now):
    """A restart moves every dispatching delivery to uncertain; it is reconciled before any retry."""
    rows = db.execute("SELECT * FROM phone_deliveries WHERE status='dispatching'").fetchall()
    for row in rows:
        _update(db, row, now, status='uncertain')
    return len(rows)


def cancel_queued(db, agent, now, member=None):
    """Stop reaches the queued deliveries of one agent, or of one of its participants."""
    clause, values = ('', (agent,)) if member is None else (' AND member=?', (agent, member))
    rows = db.execute("SELECT * FROM phone_deliveries WHERE agent=? AND status='queued'" + clause, values).fetchall()
    for row in rows:
        _update(db, row, now, status='cancelled')
    return len(rows)


def window_lane(ingestion, event):
    """The lane of the stream window that carried a committed tracker event.

    'replay' for a window the compiled-data replay lane admitted (input_kind
    other than 'live'), 'live' for a live window, 'windowless' for an event a
    person or a model turn committed outside any stream window, and 'unknown'
    when the lane cannot be read.
    """
    try:
        source, carried = event['source'], 'windowless'
        with closing(ingestion.connect()) as db:
            rows = db.execute('SELECT body FROM stream_windows WHERE owner=? AND source=? AND start<=? AND end>=?',
                (event['owner_principal_id'], source['clip_id'], source['start_s'], source['end_s'])).fetchall()
        for row in rows:
            window = json.loads(row[0])
            if (window.get('profile_id') == event['profile_id']
                    and any(item.get('event_key') == event['event_key'] for item in window.get('observations', []))):
                if window.get('input_kind') != 'live':
                    return 'replay'
                carried = 'live'
        return carried
    except Exception:
        return 'unknown'


def alert_lane(ingestion, event):
    """The lane a committed tracker event arrived in: 'live', 'replay' or 'unknown'.

    The compiled-data replay lane admits its windows with input_kind
    'dataset_replay'. An event committed outside a stream window is live here;
    the host then gives it the lane of the run that recorded it. A lane that
    cannot be read answers 'unknown', and only a live alert reaches a phone.
    """
    lane = window_lane(ingestion, event)
    return 'live' if lane == 'windowless' else lane


class PhoneDelivery:
    """The host's delivery worker for one company.

    `messaging` is the company's Messaging store, `phone` its PhoneChat and
    `origin` Rocoto's exact HTTPS origin. `post(chat_id, entry)` writes the
    alert entry into the Tracking Agent's chat, replacing the entry of the same
    alert revision, and answers True once it is saved. `fan_out` is synchronous
    and makes no provider call; `tick` claims and dispatches.
    """

    def __init__(self, messaging, phone, *, origin='', post=None, concurrency=4):
        self.messaging, self.phone, self.origin, self.post = messaging, phone, origin, post
        self.clock, self.concurrency, self.tasks = messaging.clock, concurrency, {}
        self._pruned = None

    # Lanes ------------------------------------------------------------

    def lane(self, alert_id, decide):
        """The lane of one observation, decided once and kept: 'live', 'replay' or 'unknown'.

        `decide()` answers at the first read. A later read of the same
        observation, by a retry, the catch-up pass or a run it woke, gets the
        kept answer. An answer that could not be read is asked again next time,
        and only a live alert reaches a phone.
        """
        try:
            with self.messaging._db() as db:
                kept = None if db is None else db.execute('SELECT lane FROM phone_lanes WHERE alert=?', (alert_id,)).fetchone()
        except sqlite3.OperationalError:
            kept = None  # a store from before the first decision holds none
        except (ValueError, OSError, sqlite3.Error):
            return 'unknown'
        if kept is not None:
            return kept[0] if kept[0] in LANES else 'unknown'
        try:
            decided = decide()
        except Exception:
            decided = 'unknown'
        if decided not in LANES or not bounded(alert_id, 160):
            return 'unknown'
        try:
            with self.messaging._db(write=True) as db:
                db.execute('INSERT OR IGNORE INTO phone_lanes VALUES(?,?,?)', (alert_id, decided, self.clock()))
                return db.execute('SELECT lane FROM phone_lanes WHERE alert=?', (alert_id,)).fetchone()[0]
        except (ValueError, OSError, sqlite3.Error):
            return 'unknown'  # a lane the host could not keep reaches zero phones

    # Retained history -------------------------------------------------

    @staticmethod
    def _prune(db, now):
        """Remove what retention has released: deliveries, their outbox rows, then receipts and lanes."""
        gone = db.execute(f'''SELECT id FROM phone_deliveries WHERE
            (status IN ({','.join('?' * len(REACHED))}) AND updated<?) OR (status IN ({','.join('?' * len(UNSENT))}) AND updated<?)''',
            (*REACHED, now - SENT_RETENTION_SECONDS, *UNSENT, now - UNSENT_RETENTION_SECONDS)).fetchall()
        for row in gone:
            db.execute("DELETE FROM outbox WHERE request_id LIKE ? AND status!='dispatching'", (row[0] + ':%',))
            db.execute('DELETE FROM phone_reviews WHERE delivery=?', (row[0],))
            db.execute('DELETE FROM phone_deliveries WHERE id=?', (row[0],))
        # A receipt leaves once its deliveries have, and its chat entry has been written.
        db.execute('''DELETE FROM phone_alerts WHERE created<? AND posted>=version AND NOT EXISTS(
            SELECT 1 FROM phone_deliveries d WHERE d.agent=phone_alerts.agent AND d.alert=phone_alerts.alert
            AND d.revision=phone_alerts.revision)''', (now - UNSENT_RETENTION_SECONDS,))
        db.execute('DELETE FROM phone_lanes WHERE decided<?', (now - SENT_RETENTION_SECONDS,))
        return len(gone)

    def prune(self, *, force=False):
        """One retention pass, at most once a minute. Returns the deliveries removed."""
        now = self.clock()
        if not force and self._pruned is not None and 0 <= now - self._pruned < PRUNE_EVERY_SECONDS:
            return 0
        try:
            with self.messaging._db() as db:
                if db is None:
                    return 0
            with self.messaging._db(write=True) as db:
                removed = self._prune(db, now)
        except sqlite3.OperationalError:
            return 0
        self._pruned = now
        return removed

    def history_full(self):
        """True while retained history is at its bound, so a new alert writes nothing."""
        try:
            with self.messaging._db() as db:
                return db is not None and (db.execute('SELECT count(*) FROM phone_alerts').fetchone()[0] >= MAX_ALERTS
                    or db.execute('SELECT count(*) FROM phone_deliveries').fetchone()[0] >= MAX_DELIVERIES)
        except sqlite3.OperationalError:
            return False  # a store from before the first fan-out holds no history

    def unread(self, agent_id, alert_ids, *, alert_revision=1):
        """The alerts of this list that hold no fan-out receipt and no kept replay lane, for the catch-up pass."""
        wanted = [key for key in alert_ids if bounded(key, 160)][:500]
        try:
            with self.messaging._db() as db:
                if db is None:
                    return set(wanted)
                marks = ','.join('?' * len(wanted))
                read = {row[0] for row in db.execute(f'SELECT alert FROM phone_alerts WHERE agent=? AND revision=? AND alert IN ({marks})',
                    (agent_id, alert_revision, *wanted))}
                replayed = {row[0] for row in db.execute(f"SELECT alert FROM phone_lanes WHERE lane='replay' AND alert IN ({marks})", wanted)}
        except sqlite3.OperationalError:
            return set(wanted)  # a store from before the first fan-out holds no receipt
        return set(wanted) - read - replayed

    # Fan-out ----------------------------------------------------------

    def fan_out(self, agent_id, *, alert_id, summary, alert_revision=1, lane='live', committed_at=None, since=None):
        """Write one delivery per participant whose binding is enabled, and return the ones written.

        Writing is idempotent: a retry, a duplicate claim, a restart or a second
        read of the same alert revision adds nothing. `committed_at` is the
        alert's commit time. The agent listens from the moment its phone
        records exist (`since` can only move that later); an alert from before
        then, or older than a delivery's lifetime, writes nothing, and a
        participant added after the alert gets none for it. The agent's run
        activation plays no part: delivery follows the phone policy alone.
        """
        from phone_chat import PhoneChatError
        if lane != 'live':
            return []  # the compiled-data replay lane reaches zero phones
        if (not bounded(alert_id, 160) or type(alert_revision) is not int or not 1 <= alert_revision <= 10000
                or not isinstance(summary, str) or not summary.strip()):
            raise ValueError('Use the processed alert identity.')
        try:
            record = self.phone._saved(agent_id)
        except PhoneChatError:
            return []  # phone chat belongs to Tracking Agents
        now = self.clock()
        agent, chat, written = record['id'], record['chat_id'], []
        with self.messaging._db(write=True) as db:
            # The agent's phone records start its listening. A Tracking Agent from before
            # this rule gets them with its first alert, and that alert.
            creator = db.execute("SELECT added FROM phone_participants WHERE agent=? AND role='creator'", (agent,)).fetchone()
            listening = max(since or 0, creator['added'] if creator else 0)
            self.phone._ensure(db, record, now)
            self.phone._refresh(db, record, now)
            if db.execute('SELECT 1 FROM phone_alerts WHERE agent=? AND alert=? AND revision=?',
                    (agent, alert_id, alert_revision)).fetchone():
                return []
            def full():
                return (db.execute('SELECT count(*) FROM phone_alerts').fetchone()[0] >= MAX_ALERTS
                    or db.execute('SELECT count(*) FROM phone_deliveries').fetchone()[0] >= MAX_DELIVERIES)
            if full():
                self._prune(db, now)
                if full():
                    raise ValueError('Review retained delivery history.')
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (agent,)).fetchone()
            title, body = clean(record['title'], 100), clean(summary, TEXT_LIMIT)
            text, link = alert_text(title, body, alert_revision), alert_link(self.origin, chat, alert_id)
            earlier = committed_at is not None and (committed_at < listening or now - committed_at >= DELIVERY_SECONDS)
            if policy['delivery'] == 'on' and not earlier:
                for row in db.execute('''SELECT p.member,p.role,p.added,b.channel,b.generation,b.address_generation
                        FROM phone_participants p JOIN recipient_bindings b ON b.agent=p.agent AND b.member=p.member
                        WHERE p.agent=? AND p.removed IS NULL AND b.revoked IS NULL AND b.enabled=1
                        ORDER BY p.role='creator' DESC,p.added,p.member''', (agent,)).fetchall():
                    # A member the creator added after the alert gets none for it. The creator is a
                    # participant from the start, also when this read writes their default records.
                    if committed_at is not None and row['role'] != 'creator' and row['added'] > committed_at:
                        continue
                    identifier = delivery_id(self.messaging.company_id, agent, row['member'], alert_id, alert_revision)
                    inserted = db.execute('''INSERT OR IGNORE INTO phone_deliveries(id,agent,chat,member,alert,revision,
                        channel,account,text,link,status,held_reason,binding_generation,address_generation,
                        policy_generation,provider_message_id,units,attempts,created,updated,expires,outbox)
                        VALUES(?,?,?,?,?,?,?,NULL,?,?,'queued',NULL,?,?,?,NULL,0,0,?,?,?,NULL)''',
                        (identifier, agent, chat, row['member'], alert_id, alert_revision, row['channel'], text, link,
                         row['generation'], row['address_generation'], policy['generation'], now, now,
                         now + DELIVERY_SECONDS)).rowcount
                    if inserted:
                        written.append(public(db.execute('SELECT * FROM phone_deliveries WHERE id=?', (identifier,)).fetchone()))
            # The receipt of this read. Its chat entry exists only when a delivery was written.
            db.execute('INSERT INTO phone_alerts VALUES(?,?,?,?,?,?,?,?,?,?,1,?)',
                (agent, alert_id, alert_revision, chat, title, body, text, link, now, len(written), 0 if written else 1))
        return written

    # The alert's chat entry -------------------------------------------

    def _entry(self, db, alert):
        rows = db.execute('''SELECT d.* FROM phone_deliveries d LEFT JOIN phone_participants p
            ON p.agent=d.agent AND p.member=d.member WHERE d.agent=? AND d.alert=? AND d.revision=?
            ORDER BY p.role='creator' DESC,d.created,d.member''', (alert['agent'], alert['alert'], alert['revision'])).fetchall()
        return {'role': 'assistant', 'content': alert['text'], 'alert': {'id': alert['alert'], 'revision': alert['revision']},
            'channel_deliveries': [{'id': row['id'], 'member_id': row['member'], 'channel': row['channel'],
                'status': row['status'], 'held_reason': row['held_reason'], 'updated_at': row['updated']} for row in rows],
            'created_at': alert['created']}

    def sync_entries(self, limit=64):
        """Append each alert to its chat in arrival order, then keep its delivery states current.

        `post` appends only once an active turn has drained; an entry it leaves
        for later holds every later alert of that chat behind it.
        """
        if self.post is None:
            return 0
        try:
            with self.messaging._db() as db:
                if db is None:
                    return 0
                # Each chat brings its own oldest entries, so a busy conversation holds only its own alerts.
                pending = [(row, self._entry(db, row))
                    for chat in db.execute('SELECT DISTINCT chat FROM phone_alerts WHERE posted<version LIMIT 256').fetchall()
                    for row in db.execute('SELECT * FROM phone_alerts WHERE chat=? AND posted<version ORDER BY created,rowid LIMIT ?',
                        (chat[0], limit)).fetchall()]
        except sqlite3.OperationalError:
            return 0  # a store from before the first fan-out holds no alert
        waiting, done = set(), []
        for row, entry in pending:
            if row['chat'] not in waiting and self.post(row['chat'], entry) is True:
                done.append((row['version'], row['agent'], row['alert'], row['revision'], row['version']))
            else:
                waiting.add(row['chat'])
        if done:
            with self.messaging._db(write=True) as db:
                db.executemany('UPDATE phone_alerts SET posted=? WHERE agent=? AND alert=? AND revision=? AND posted<?', done)
        return len(done)

    # Claim and dispatch -----------------------------------------------

    def _current(self, db, row, now):
        """The agent is a Tracking Agent with its policy on; the member is a current participant with alerts on."""
        from phone_chat import PhoneChatError
        try:
            record = self.phone._saved(row['agent'])
        except PhoneChatError:
            return None
        policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (row['agent'],)).fetchone()
        binding = db.execute('''SELECT b.* FROM phone_participants p JOIN recipient_bindings b
            ON b.agent=p.agent AND b.member=p.member WHERE p.agent=? AND p.member=?
            AND p.removed IS NULL AND b.revoked IS NULL''', (row['agent'], row['member'])).fetchone()
        if (policy is None or policy['delivery'] != 'on' or binding is None or not binding['enabled']
                or policy['chat'] != row['chat'] or record['chat_id'] != row['chat']):
            return None
        return record, policy, binding

    def _prepare(self, provider, contact, alert, row, reference, now):
        """The provider request for one channel, or the reason this channel holds the alert."""
        span, last = provider.reply_window, contact['last_incoming']
        body = row['text'] + ' ' + row['link']
        try:
            if span is None or (last is not None and 0 <= now - last < span):
                text = row['text']
                for _ in range(48):
                    prepared = provider.prepare('text', contact['address'], text=body, reference=reference)
                    if provider.channel != 'sms' or provider.units(prepared) <= SMS_SEGMENTS:
                        return 'text', prepared, provider.preview('text', text=body)
                    if len(text) <= 8:
                        break
                    # An SMS alert is bounded to three segments with its link: shorten the text ahead of the link.
                    text = text[:int(len(text) * .85) - 3].rstrip() + '...'
                    body = text + ' ' + row['link']
                raise Held('channel_unready')
            # WhatsApp only: outside the customer-service window the alert travels in its approved utility template.
            details = {'purpose': 'monitoring', 'language': 'en_US',
                'parameters': [alert_lead(alert['title'], alert['revision']), alert['summary'], row['link']]}
            try:
                return ('utility', provider.prepare('utility', contact['address'], alert=details, reference=reference),
                    provider.preview('utility', alert=details))
            except ProviderError:
                raise Held('window_closed') from None
        except ProviderError:
            raise Held('channel_unready') from None

    def _claim(self, identifier):
        """Repeat the admission checks, reserve the units and mark one queued delivery dispatching."""
        from phone_chat import resolve_member
        now, messaging = self.clock(), self.messaging
        with messaging._db(write=True) as db:
            row = db.execute('SELECT * FROM phone_deliveries WHERE id=?', (identifier,)).fetchone()
            if row is None or row['status'] != 'queued':
                return None
            def close(status, reason=None):
                _update(db, row, now, status=status, held_reason=reason)
            if now >= row['expires']:
                return close('expired')
            current = self._current(db, row, now)
            if current is None:
                return close('cancelled')
            record, policy, binding = current
            actor = messaging.host_actor(row['member'], policy['collection'], row['chat'])
            try:
                messaging._auth(actor, self_only=True)
            except ValueError:
                return close('held', 'access_required')
            if not row['link']:
                return close('held', 'channel_unready')
            resolve_member(db, row['member'], now, messaging.eligible(db, row['member']))
            binding = db.execute('SELECT * FROM recipient_bindings WHERE agent=? AND member=?', (row['agent'], row['member'])).fetchone()
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (row['agent'],)).fetchone()
            held = messaging.member_channels(db, row['member'])
            eligible = [channel for channel, _ in messaging.eligible(db, row['member'])]
            tried = json.loads(row['tried'])
            candidates = [channel for channel in eligible if channel not in tried and (
                binding['channel_choice'] == 'automatic' or channel == binding['channel'])]
            if not candidates:
                if tried:
                    return close('failed')  # every eligible channel gave a definitive rejection
                return close('held', 'number_required' if not held else 'opted_out' if all(
                    value['opted_out_at'] is not None for value in held.values()) else 'channel_unready')
            alert = db.execute('SELECT * FROM phone_alerts WHERE agent=? AND alert=? AND revision=?',
                (row['agent'], row['alert'], row['revision'])).fetchone()
            reason = None
            for channel in candidates:
                provider, contact = messaging.adapters[channel], held[channel]['row']
                outbox_id = 'msg-' + secrets.token_hex(16)
                try:
                    kind, prepared, text = self._prepare(provider, contact, alert, row, outbox_id, now)
                    units = reserve(db, messaging.spend, row['agent'], provider.units(prepared), now, outbox_id)
                except Held as stopped:
                    reason = reason or stopped.reason
                    continue
                except SpendHeld as stopped:
                    return close('held', stopped.reason)
                attempt = row['attempts'] + 1
                outbox = messaging._insert(db, actor, outbox_id, channel=channel, address=contact['address'], kind=kind,
                    text=text, request_id=f"{row['id']}:{attempt}", generation=contact['generation'],
                    intent=digest([row['id'], attempt, channel, contact['address'], text]))
                # The same conversation binding an agent answer writes: a reply to this alert continues this chat.
                db.execute('''INSERT OR REPLACE INTO bindings(member,account,generation,chat,collection,created,channel)
                    VALUES(?,?,?,?,?,?,?)''', (row['member'], provider.account_id, contact['generation'], row['chat'],
                    policy['collection'], now, channel))
                _update(db, row, now, status='dispatching', channel=channel, account=provider.account_id,
                    units=row['units'] + units, attempts=attempt, outbox=outbox_id, held_reason=None,
                    binding_generation=binding['generation'], address_generation=contact['generation'],
                    policy_generation=policy['generation'])
                return actor, outbox, prepared
            return close('held', reason)

    def _still(self, identifier, outbox_id):
        """The admission checks again, immediately before the provider call."""
        from phone_spend import record as spend_record
        try:
            now = self.clock()
            with self.messaging._db() as db:
                row = db.execute('SELECT * FROM phone_deliveries WHERE id=?', (identifier,)).fetchone()
                if (row is None or row['status'] != 'dispatching' or row['outbox'] != outbox_id
                        or now >= row['expires'] or self._current(db, row, now) is None):
                    return False
            return spend_record(self.messaging.spend)['stopped'] is False
        except (ValueError, OSError, sqlite3.Error):
            return False

    async def _deliver(self, identifier):
        claimed = await asyncio.to_thread(self._claim, identifier)
        if claimed is None:
            return None
        actor, outbox, prepared = claimed
        try:
            return await self.messaging.send_prepared(actor, outbox, prepared,
                still=lambda: self._still(identifier, outbox['id']))
        finally:
            try:
                self.sync_entries()  # the provider's answer shows in the alert's chat entry at once
            except (ValueError, KeyError, OSError, sqlite3.Error):
                pass  # the next pass brings the entry up to date

    def _sweep(self, limit):
        """Expire what stayed unsent for 900 seconds and list the queued deliveries, oldest first."""
        now = self.clock()
        try:
            with self.messaging._db() as db:
                if db is None:
                    return []
                overdue = db.execute("""SELECT 1 FROM phone_deliveries WHERE status='queued' AND expires<=?
                    OR status='uncertain' AND updated<=? LIMIT 1""", (now, now - RECONCILE_SECONDS)).fetchone()
            if overdue:
                with self.messaging._db(write=True) as db:
                    for row in db.execute("SELECT * FROM phone_deliveries WHERE status='queued' AND expires<=?", (now,)).fetchall():
                        _update(db, row, now, status='expired')
                    # An uncertain send stays reviewable for 24 hours. It then closes with its units kept
                    # and is never sent again; the alert stays in Rocoto.
                    for row in db.execute("SELECT * FROM phone_deliveries WHERE status='uncertain' AND updated<=?",
                            (now - RECONCILE_SECONDS,)).fetchall():
                        _update(db, row, now, status='expired')
            with self.messaging._db() as db:
                return [row[0] for row in db.execute(
                    "SELECT id FROM phone_deliveries WHERE status='queued' ORDER BY created,rowid LIMIT ?", (limit,))]
        except sqlite3.OperationalError:
            return []  # a store from before the first fan-out holds no delivery

    async def tick(self, limit=8):
        """One pass of the worker: expire, claim and dispatch, then bring the chat entries up to date."""
        self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
        self.sync_entries()
        await asyncio.to_thread(self.prune)
        room = self.concurrency - len(self.tasks)
        if room > 0:
            for identifier in await asyncio.to_thread(self._sweep, limit + len(self.tasks)):
                if identifier in self.tasks or room <= 0:
                    continue
                task = asyncio.create_task(self._deliver(identifier))
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                self.tasks[identifier] = task
                room -= 1
        self.sync_entries()

    async def drain(self):
        """Wait for the sends in flight, then record their states in the chat entries."""
        while self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
        self.sync_entries()

    async def close(self):
        """Stop the sends still in flight. A finished send needs nothing more."""
        pending = [task for task in self.tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks = {}

    # Reconciliation and reads -----------------------------------------

    def reconcile(self, identifier, outcome, *, reviewed_by='host'):
        """Record the host's review of an uncertain send. It is reconciled before any retry.

        `outcome` is 'unsent', 'sent', 'delivered', 'read' or 'failed', and
        `reviewed_by` names the operator who looked. Reconciled as unsent inside
        its 900 seconds the delivery returns to queued and its units come back;
        otherwise it keeps the reconciled status. The host command at the foot
        of this module is its entry point.
        """
        if outcome not in ('unsent', *RANK):
            raise ValueError('Record the reviewed delivery outcome.')
        if not bounded(reviewed_by, 128):
            raise ValueError('Name the operator who reviewed this delivery.')
        now = self.clock()
        with self.messaging._db(write=True) as db:
            row = db.execute('SELECT * FROM phone_deliveries WHERE id=?', (identifier,)).fetchone()
            if row is None or row['status'] != 'uncertain':
                raise ValueError('Choose a delivery that is confirming its send.')
            db.execute('INSERT INTO phone_reviews VALUES(?,?,?,?)', (identifier, outcome, reviewed_by, now))
            if outcome == 'unsent':
                returned = release(db, row['outbox'])
                db.execute("UPDATE outbox SET status='cancelled',updated=?,waiting=0 WHERE id=? AND status='uncertain'", (now, row['outbox']))
                _update(db, row, now, status='queued' if now < row['expires'] else 'expired', units=max(0, row['units'] - returned))
            else:
                db.execute("UPDATE outbox SET status=?,updated=? WHERE id=? AND status='uncertain'", (outcome, now, row['outbox']))
                _update(db, row, now, status=outcome)
            return public(db.execute('SELECT * FROM phone_deliveries WHERE id=?', (identifier,)).fetchone())

    def deliveries(self, agent_id, *, alert_id=None):
        with self.messaging._db() as db:
            if db is None:
                return []
            try:
                clause, values = ('', (agent_id,)) if alert_id is None else (' AND alert=?', (agent_id, alert_id))
                return [public(row) for row in db.execute(
                    'SELECT * FROM phone_deliveries WHERE agent=?' + clause + ' ORDER BY created,rowid', values)]
            except sqlite3.OperationalError:
                return []

    def answered(self, reply):
        """The alert a participant's phone reply answers, as host-supplied context for the turn it starts."""
        try:
            adapter = self.messaging.adapters.get(reply['channel'])
            with self.messaging._db() as db:
                if db is None or adapter is None:
                    return None
                base = '''SELECT o.id,d.alert,d.revision,d.text,d.link FROM outbox o LEFT JOIN phone_deliveries d ON d.outbox=o.id
                    WHERE o.account=? AND o.member=? AND o.chat=?'''
                values = (reply['account'], reply['member_id'], reply['chat_id'])
                if reply.get('reply_context') and adapter.reply_rule != 'recent':
                    column = 'o.id' if adapter.reply_rule == 'tracking' else 'o.provider_message_id'
                    row = db.execute(base + f' AND {column}=?', (*values, reply['reply_context'])).fetchone()
                else:
                    # No reply context, on any channel: the most recent delivery to that member inside 24 hours.
                    row = db.execute(base + ''' AND o.kind!='otp' AND o.status IN ('sent','delivered','read')
                        AND o.created>? AND o.created<=? ORDER BY o.created DESC,o.rowid DESC LIMIT 1''',
                        (*values, reply['received_at'] - 86400, reply['received_at'])).fetchone()
            if row is None or row['alert'] is None:
                return None
            return {'alert_id': row['alert'], 'alert_revision': row['revision'], 'text': row['text'], 'link': row['link']}
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            return None

    def block(self, agent_id, reader, *, company_owner=False, writable=True, members=(), admitted=False):
        """The phone block of CHANNELS.md for one reader: a current participant or the company owner.

        Every reader of the block sees each participant's verified address on
        the channel in use, masked. `members` lists the company's current owners and
        operators as {'id','display_name'}. `admitted` marks a reader the host
        admitted as a participant for the change this block answers, so a
        participant who just removed themself reads its result.
        """
        from phone_chat import NOT_FOUND, PhoneChatError
        state = self.phone.state(agent_id)
        participants = state['participants']
        if not admitted and not company_owner and all(row['member_id'] != reader for row in participants):
            raise PhoneChatError(NOT_FOUND, 404)
        now, manage = self.clock(), bool(writable and reader == state['creator_id'])
        names = {row['id']: row['display_name'] for row in members}
        rows, available = [], []
        with self.messaging._db() as db:
            for row in participants:
                member, binding = row['member_id'], row['binding']
                held = self.messaging.member_channels(db, member)
                last = db.execute('''SELECT * FROM phone_deliveries WHERE agent=? AND member=?
                    ORDER BY updated DESC,created DESC,rowid DESC LIMIT 1''', (state['agent_id'], member)).fetchone()
                verifying = db.execute('''SELECT 1 FROM challenges WHERE member=? AND status IN ('pending','sent','uncertain')
                    AND expires>?''', (member, now)).fetchone()
                request = row['contact_request']
                address = held[binding['channel']]['address'] if binding['channel'] in held else None
                rows.append({'member_id': member, 'name': names.get(member) or 'Member', 'role': row['role'],
                    'is_self': member == reader, 'channel': binding['channel'], 'channel_choice': binding['channel_choice'],
                    'verified_channels': row['verified_channels'],
                    'address_masked': mask(address),
                    'verification': ('opted_out' if row['verified_channels'] and row['verified_channels'] == row['opted_out_channels']
                        else 'verified' if row['verified_channels'] else 'in_progress' if verifying else 'number_required'),
                    'enabled': binding['enabled'],
                    'last_delivery': None if last is None else {'id': last['id'], 'channel': last['channel'],
                        'status': last['status'], 'held_reason': last['held_reason'], 'updated_at': last['updated']},
                    'contact_request': None if request is None else {key: request[key]
                        for key in ('id', 'status', 'created_at', 'expires_at')}})
            if manage:
                current = {row['member_id'] for row in participants}
                available = [{'member_id': row['id'], 'name': row['display_name'],
                    'verified': bool(self.messaging.eligible(db, row['id']))} for row in members if row['id'] not in current]
            spend = usage(db, self.messaging.spend, state['agent_id'], now)
        return {'agent_id': state['agent_id'], 'chat_id': state['chat_id'], 'collection': state['collection'],
            'generation': state['policy']['generation'], 'delivery': state['policy']['participant_phone_delivery'],
            'can_manage': manage, 'read_only': not writable, 'channels': state['channels'], 'participants': rows,
            'available_members': available, 'spend': spend, 'history': 'full' if self.history_full() else 'open'}


def main(argv=None):
    """The host operator's entry point for an uncertain send.

    `reconcile <state> <delivery_id> <outcome> --by <name>` records the review
    in the company's private messaging store and prints the delivery. It makes
    no provider call.
    """
    import argparse
    from messaging import Messaging
    from workspace_identity import workspace_scope
    parser = argparse.ArgumentParser(prog='phone_delivery.py')
    command = parser.add_subparsers(dest='command', required=True).add_parser('reconcile',
        help='record the reviewed outcome of an uncertain phone delivery')
    command.add_argument('state', help='the company state directory that holds messaging.sqlite')
    command.add_argument('delivery_id')
    command.add_argument('outcome', choices=('unsent', *RANK))
    command.add_argument('--by', required=True, help='the operator who reviewed the provider record')
    args = parser.parse_args(argv)
    state = Path(args.state).absolute()
    if not (state / 'messaging.sqlite').is_file():
        parser.exit(2, 'Choose a company state directory that holds a messaging store.\n')
    store = Messaging(state, company_id=workspace_scope(state))
    try:
        row = PhoneDelivery(store, None).reconcile(args.delivery_id, args.outcome, reviewed_by=args.by)
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps({key: row[key] for key in ('id', 'agent_id', 'member_id', 'alert_id', 'status', 'units', 'updated_at')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

