"""Participant phone chat of a participant-reach agent: who it reaches and on which channel.

Holds the participants, one recipient binding per participant, the agent's
delivery policy and the contact-card requests described in CHANNELS.md. The
records live in the company's private messaging store, beside the verified
channel addresses they resolve against, so a verification and the bindings it
completes commit together. Every identity originates at the host: tool
arguments, alert payloads and model text name neither a member nor an address.
A participant-reach agent addresses its participants and nobody else.
"""
from __future__ import annotations

import secrets
import sqlite3

from messaging_provider import CHANNELS
from phone_delivery import cancel_queued
from agent_kind_policy import declared_class_authority

PURPOSE = 'tracking_alerts'
MAX_PARTICIPANTS = 20
CONTACT_REQUEST_SECONDS = 604800

NOT_FOUND = 'Choose an agent with participant messaging from your current workspace.'
FIELDS = 'Send the phone alert fields.'
MEMBER = 'Choose a member of this company.'
PARTICIPANT_FIRST = 'Add this member as a participant first.'
CREATOR_MANAGES = 'This agent’s creator manages its participants.'
OWN_CHOICE = 'Each participant chooses their own channel and switches their own alerts on.'
REFRESH = 'Refresh the phone alerts and repeat your change.'
FULL = 'This agent holds its full 20 participants.'
CREATOR_STAYS = 'The creator stays a participant of this agent.'
VERIFY_CHANNEL = 'Verify a number on this channel to choose it.'
CHANNEL_PREPARING = 'This channel is being prepared.'
NUMBER_VERIFIED = 'This participant’s number is verified.'
PARTICIPANTS_ONLY = 'This agent’s messages reach its own participants.'
SWITCHED_OFF = 'This participant’s phone alerts are switched off.'
PAUSED = 'This agent’s phone alerts are paused.'
VIA = {'viber': 'on Viber', 'sms': 'by SMS', 'whatsapp': 'on WhatsApp'}


def card_text(status, title, name, channel=None):
    """The chat entry of a contact card says what its state is."""
    if status == 'completed':
        return f'{name} gets alerts from {title} {VIA.get(channel, "on their phone")}.'
    if status == 'expired':
        return f'The phone number request for {name} ended after seven days. Ask again from “Alerts on your phone”.'
    if status == 'cancelled':
        return f'The phone number request for {name} ended.'
    return f'Alerts from {title}: add a verified phone number for {name}.'

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS phone_policies(agent TEXT PRIMARY KEY,chat TEXT NOT NULL UNIQUE,
        collection TEXT NOT NULL,creator TEXT NOT NULL,delivery TEXT NOT NULL,generation INTEGER NOT NULL,
        updated_by TEXT NOT NULL,updated REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS phone_participants(agent TEXT NOT NULL,chat TEXT NOT NULL,member TEXT NOT NULL,
        role TEXT NOT NULL,added_by TEXT NOT NULL,added REAL NOT NULL,removed REAL,PRIMARY KEY(agent,member))''',
    '''CREATE TABLE IF NOT EXISTS recipient_bindings(agent TEXT NOT NULL,chat TEXT NOT NULL,member TEXT NOT NULL,
        channel TEXT,channel_choice TEXT NOT NULL,purpose TEXT NOT NULL,enabled INTEGER NOT NULL,
        generation INTEGER NOT NULL,address_generation INTEGER,created REAL NOT NULL,updated REAL NOT NULL,
        revoked REAL,PRIMARY KEY(agent,member))''',
    '''CREATE TABLE IF NOT EXISTS contact_requests(id TEXT PRIMARY KEY,agent TEXT NOT NULL,chat TEXT NOT NULL,
        member TEXT NOT NULL,requested_by TEXT NOT NULL,purpose TEXT NOT NULL,status TEXT NOT NULL,channel TEXT,
        created REAL NOT NULL,expires REAL NOT NULL,completed REAL,posted TEXT)''',
    "CREATE UNIQUE INDEX IF NOT EXISTS contact_request_open ON contact_requests(agent,member) WHERE status='open'",
    'CREATE INDEX IF NOT EXISTS phone_participant_member ON phone_participants(member,removed)',
    'CREATE INDEX IF NOT EXISTS recipient_binding_member ON recipient_bindings(member,revoked)',
)


class PhoneChatError(ValueError):
    """A refusal with the HTTP status its route answers; the sentence is customer copy."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _identity(value, maximum=128):
    return isinstance(value, str) and 1 <= len(value) <= maximum and all(ord(c) >= 32 for c in value)


def chat_admits(db, chat, member):
    """A participant-reach agent's chat takes messages from its current participants.

    Any other conversation keeps its own owner rule, so this answers True for
    it. Runs inside the messaging store's transaction for reply routing, claim
    and the send fence.
    """
    policy = db.execute('SELECT agent FROM phone_policies WHERE chat=?', (chat,)).fetchone()
    if policy is None:
        return True
    return db.execute('''SELECT 1 FROM phone_participants p JOIN recipient_bindings b
        ON b.agent=p.agent AND b.member=p.member WHERE p.agent=? AND p.member=?
        AND p.removed IS NULL AND b.revoked IS NULL''', (policy['agent'], member)).fetchone() is not None


def resolve_member(db, member, now, eligible):
    """Resolve one member's bindings against their current verified addresses.

    `eligible` lists (channel, address generation) in channel order: a verified
    address with no opt-out on a commissioned channel. The messaging store calls
    this in the same transaction that verifies or removes an address, so a
    completed contact card and the bindings it resolves commit together.
    """
    held = dict(eligible)
    first = eligible[0][0] if eligible else None
    for row in db.execute('SELECT * FROM recipient_bindings WHERE member=? AND revoked IS NULL', (member,)).fetchall():
        choice = row['channel_choice']
        if choice == 'picked' and row['channel'] not in held:
            choice = 'automatic'  # The picked channel's address left; channel order applies again.
        channel = row['channel'] if choice == 'picked' else first
        address_generation = held.get(channel)
        moved = channel != row['channel'] or choice != row['channel_choice']
        if moved or address_generation != row['address_generation']:
            db.execute('''UPDATE recipient_bindings SET channel=?,channel_choice=?,address_generation=?,
                generation=generation+?,updated=? WHERE agent=? AND member=?''',
                (channel, choice, address_generation, int(moved), now, row['agent'], member))
            db.execute("UPDATE phone_policies SET generation=generation+1,updated_by='host',updated=? WHERE agent=?",
                (now, row['agent']))
        if first is not None:
            db.execute("UPDATE contact_requests SET status='completed',channel=?,completed=? WHERE agent=? AND member=? AND status='open'",
                (channel or first, now, row['agent'], member))


class PhoneChat:
    """Host operations on one company's participant phone chat.

    `messaging` is the company's Messaging store, `agents` reads saved agent
    definitions, `member` answers a current owner or operator of the company as
    {'id','display_name','role'}, and `post(chat_id, entry)` writes a contact
    card into its chat, replacing the entry that carries the same request ID.
    Methods are synchronous and brief; `by` is always a host-authenticated member.
    """

    def __init__(self, messaging, *, agents, member, post=None, clock=None):
        if not callable(member) or not callable(getattr(agents, 'get', None)):
            raise ValueError('Use the host company authority.')
        self.messaging, self.agents, self.member, self.post = messaging, agents, member, post
        self.clock = clock or messaging.clock

    # Host lookups -----------------------------------------------------

    def _saved(self, agent_id):
        # A passed record names the agent; the saved definition is read afresh.
        key = agent_id.get('id') if isinstance(agent_id, dict) else agent_id
        try:
            record = self.agents.get(key) if _identity(key) else None
        except (ValueError, KeyError, TypeError):
            record = None
        if (not isinstance(record, dict) or declared_class_authority(self.agents, record) != 'participant_reach'
                or not all(_identity(record.get(key)) for key in ('id', 'chat_id', 'owner_principal_id', 'dataset'))):
            raise PhoneChatError(NOT_FOUND, 404)
        return record

    def _member(self, member_id):
        try:
            value = self.member(member_id) if _identity(member_id) else None
        except Exception:
            value = None
        return value if isinstance(value, dict) and value.get('role') in {'owner', 'operator'} else None

    def _caller(self, by):
        if self._member(by) is None:
            raise PhoneChatError(NOT_FOUND, 404)

    def _manager(self, policy, by, company_owner):
        owner = company_owner is True and (self._member(by) or {}).get('role') == 'owner'
        if by != policy['creator'] and not owner:
            raise PhoneChatError(CREATOR_MANAGES, 403)

    @staticmethod
    def _expected(policy, expected_generation):
        if expected_generation is not None and (type(expected_generation) is not int
                or expected_generation != policy['generation']):
            raise PhoneChatError(REFRESH, 409)

    # Records ----------------------------------------------------------

    def _ensure(self, db, record, now):
        """The default at promotion: policy on, the creator, their enabled binding, their card."""
        agent, creator = record['id'], record['owner_principal_id']
        policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (agent,)).fetchone()
        if policy is None:
            if db.execute('SELECT 1 FROM phone_policies WHERE chat=?', (record['chat_id'],)).fetchone():
                raise PhoneChatError(NOT_FOUND, 404)
            db.execute('INSERT INTO phone_policies VALUES(?,?,?,?,?,?,?,?)',
                (agent, record['chat_id'], record['dataset'], creator, 'on', 1, creator, now))
            self._admit(db, record, creator, 'creator', creator, now, requested_by='host')
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (agent,)).fetchone()
        elif policy['chat'] != record['chat_id'] or policy['creator'] != creator:
            raise PhoneChatError(NOT_FOUND, 404)
        return policy

    def _admit(self, db, record, member, role, by, now, *, requested_by):
        agent, chat = record['id'], record['chat_id']
        db.execute('''INSERT INTO phone_participants VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(agent,member)
            DO UPDATE SET removed=NULL,added_by=excluded.added_by,added=excluded.added''',
            (agent, chat, member, role, by, now))
        eligible = self.messaging.eligible(db, member)
        channel, address_generation = eligible[0] if eligible else (None, None)
        db.execute('''INSERT INTO recipient_bindings VALUES(?,?,?,?,'automatic',?,1,1,?,?,?,NULL)
            ON CONFLICT(agent,member) DO UPDATE SET channel=excluded.channel,channel_choice='automatic',enabled=1,
            generation=generation+1,address_generation=excluded.address_generation,updated=excluded.updated,revoked=NULL''',
            (agent, chat, member, channel, PURPOSE, address_generation, now, now))
        if channel is None:
            self._request(db, record, member, requested_by, now)

    def _request(self, db, record, member, requested_by, now):
        self._expire(db, now)
        prior = db.execute("SELECT * FROM contact_requests WHERE agent=? AND member=? AND status='open'",
            (record['id'], member)).fetchone()
        if prior is not None:
            return prior
        identifier = 'contact-' + secrets.token_hex(16)
        db.execute('INSERT INTO contact_requests VALUES(?,?,?,?,?,?,?,NULL,?,?,NULL,NULL)',
            (identifier, record['id'], record['chat_id'], member, requested_by, PURPOSE, 'open', now,
             now + CONTACT_REQUEST_SECONDS))
        return db.execute('SELECT * FROM contact_requests WHERE id=?', (identifier,)).fetchone()

    @staticmethod
    def _expire(db, now):
        db.execute("UPDATE contact_requests SET status='expired' WHERE status='open' AND expires<=?", (now,))

    def _revoke(self, db, agent, member, now):
        """End one participant's phone reach. Their unadmitted replies return to their own setup chat."""
        db.execute('UPDATE phone_participants SET removed=? WHERE agent=? AND member=? AND removed IS NULL',
            (now, agent, member))
        db.execute('UPDATE recipient_bindings SET revoked=?,updated=? WHERE agent=? AND member=? AND revoked IS NULL',
            (now, now, agent, member))
        db.execute("UPDATE contact_requests SET status='cancelled' WHERE agent=? AND member=? AND status='open'",
            (agent, member))
        cancel_queued(db, agent, now, member)
        chat = db.execute('SELECT chat FROM phone_policies WHERE agent=?', (agent,)).fetchone()['chat']
        self.messaging.return_replies(db, member, chat)

    def _refresh(self, db, record, now):
        """Apply what changed outside this store: verified addresses, opt-outs and expiry.

        Membership is read live wherever a message is admitted or sent, and a
        missing answer refuses there. It removes nobody here: the host's lookup
        answers None for a busy store as well as for a member who left.
        """
        self._expire(db, now)
        for row in db.execute('SELECT member FROM recipient_bindings WHERE agent=? AND revoked IS NULL', (record['id'],)).fetchall():
            if self._member(row['member']) is not None:
                resolve_member(db, row['member'], now, self.messaging.eligible(db, row['member']))

    @staticmethod
    def _bump(db, agent, by, now):
        db.execute('UPDATE phone_policies SET generation=generation+1,updated_by=?,updated=? WHERE agent=?', (by, now, agent))

    @staticmethod
    def _participant(db, agent, member):
        row = db.execute('''SELECT p.*,b.revoked FROM phone_participants p JOIN recipient_bindings b
            ON b.agent=p.agent AND b.member=p.member WHERE p.agent=? AND p.member=? AND p.removed IS NULL''',
            (agent, member)).fetchone()
        if row is None:
            raise PhoneChatError(PARTICIPANT_FIRST, 400)
        return row

    @staticmethod
    def public_request(row):
        return None if row is None else {'id': row['id'], 'agent_id': row['agent'], 'chat_id': row['chat'],
            'member_id': row['member'], 'requested_by': row['requested_by'], 'purpose': row['purpose'],
            'status': row['status'], 'channel': row['channel'], 'created_at': row['created'],
            'expires_at': row['expires'], 'completed_at': row['completed']}

    def _snapshot(self, db, record):
        agent = record['id']
        policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (agent,)).fetchone()
        participants = []
        for row in db.execute('''SELECT * FROM phone_participants WHERE agent=? AND removed IS NULL
                ORDER BY role='creator' DESC,added,member''', (agent,)).fetchall():
            binding = db.execute('SELECT * FROM recipient_bindings WHERE agent=? AND member=?', (agent, row['member'])).fetchone()
            request = db.execute("SELECT * FROM contact_requests WHERE agent=? AND member=? AND status='open'",
                (agent, row['member'])).fetchone()
            held = self.messaging.member_channels(db, row['member'])
            participants.append({'agent_id': agent, 'chat_id': row['chat'], 'member_id': row['member'],
                'role': row['role'], 'added_by': row['added_by'], 'added_at': row['added'], 'removed_at': row['removed'],
                'member_current': self._member(row['member']) is not None,
                'binding': {'agent_id': agent, 'chat_id': binding['chat'], 'member_id': binding['member'],
                    'channel': binding['channel'], 'channel_choice': binding['channel_choice'],
                    'purpose': binding['purpose'], 'enabled': bool(binding['enabled']),
                    'generation': binding['generation'], 'address_generation': binding['address_generation'],
                    'created_at': binding['created'], 'updated_at': binding['updated'], 'revoked_at': binding['revoked']},
                'verified_channels': [channel for channel in CHANNELS if channel in held],
                'opted_out_channels': [channel for channel in CHANNELS if channel in held and held[channel]['opted_out_at'] is not None],
                'contact_request': self.public_request(request)})
        return {'agent_id': agent, 'chat_id': policy['chat'], 'collection': policy['collection'],
            'creator_id': policy['creator'],
            'policy': {'agent_id': agent, 'participant_phone_delivery': policy['delivery'],
                'generation': policy['generation'], 'updated_by': policy['updated_by'], 'updated_at': policy['updated']},
            'participants': participants, 'channels': self.messaging.channels()}

    def _result(self, record):
        with self.messaging._db() as db:
            snapshot = self._snapshot(db, record)
        self.publish(agent_id=record['id'])
        return snapshot

    # Operations -------------------------------------------------------

    def state(self, agent_id):
        """Read one participant-reach agent's phone records, creating the default ones on first read."""
        record = self._saved(agent_id)
        now = self.clock()
        with self.messaging._db(write=True) as db:
            self._ensure(db, record, now)
            self._refresh(db, record, now)
        return self._result(record)

    promote = state

    def add_participant(self, agent_id, member_id, *, by, company_owner=False, expected_generation=None):
        record = self._saved(agent_id)
        self._caller(by)
        now = self.clock()
        with self.messaging._db(write=True) as db:
            policy = self._ensure(db, record, now)
            self._refresh(db, record, now)
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (record['id'],)).fetchone()
            self._manager(policy, by, company_owner)
            if self._member(member_id) is None:
                raise PhoneChatError(MEMBER, 400)
            current = db.execute('SELECT 1 FROM phone_participants WHERE agent=? AND member=? AND removed IS NULL',
                (record['id'], member_id)).fetchone()
            if current is None:
                self._expected(policy, expected_generation)
                count = db.execute('SELECT count(*) FROM phone_participants WHERE agent=? AND removed IS NULL',
                    (record['id'],)).fetchone()[0]
                if count >= MAX_PARTICIPANTS:
                    raise PhoneChatError(FULL, 409)
                self._admit(db, record, member_id, 'added', by, now, requested_by=by)
                self._bump(db, record['id'], by, now)
        return self._result(record)

    def remove_participant(self, agent_id, member_id, *, by, company_owner=False, expected_generation=None):
        record = self._saved(agent_id)
        self._caller(by)
        now = self.clock()
        with self.messaging._db(write=True) as db:
            self._ensure(db, record, now)
            self._refresh(db, record, now)
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (record['id'],)).fetchone()
            if by != member_id:
                self._manager(policy, by, company_owner)
            row = self._participant(db, record['id'], member_id)
            if row['role'] == 'creator':
                raise PhoneChatError(CREATOR_STAYS, 409)
            self._expected(policy, expected_generation)
            self._revoke(db, record['id'], member_id, now)
            self._bump(db, record['id'], by, now)
        return self._result(record)

    def update_binding(self, agent_id, member_id, *, by, enabled=None, channel=None, expected_generation=None, company_owner=False):
        record = self._saved(agent_id)
        self._caller(by)
        if ((enabled is None and channel is None) or enabled is not None and type(enabled) is not bool
                or channel is not None and channel not in ('automatic', *CHANNELS)):
            raise PhoneChatError(FIELDS, 400)
        now = self.clock()
        with self.messaging._db(write=True) as db:
            self._ensure(db, record, now)
            self._refresh(db, record, now)
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (record['id'],)).fetchone()
            self._participant(db, record['id'], member_id)
            if by != member_id:
                if enabled is True or channel is not None:
                    raise PhoneChatError(OWN_CHOICE, 403)
                self._manager(policy, by, company_owner)
            self._expected(policy, expected_generation)
            binding = db.execute('SELECT * FROM recipient_bindings WHERE agent=? AND member=?', (record['id'], member_id)).fetchone()
            target = {'channel': binding['channel'], 'channel_choice': binding['channel_choice'],
                'address_generation': binding['address_generation'], 'enabled': binding['enabled']}
            if enabled is not None:
                target['enabled'] = int(enabled)
            if channel is not None:
                held = self.messaging.member_channels(db, member_id)
                if channel == 'automatic':
                    eligible = self.messaging.eligible(db, member_id)
                    target.update(channel_choice='automatic', channel=eligible[0][0] if eligible else None,
                        address_generation=eligible[0][1] if eligible else None)
                else:
                    if not next(row for row in self.messaging.channels() if row['channel'] == channel)['ready']:
                        raise PhoneChatError(CHANNEL_PREPARING, 409)
                    if channel not in held:
                        raise PhoneChatError(VERIFY_CHANNEL, 409)
                    target.update(channel_choice='picked', channel=channel, address_generation=held[channel]['generation'])
            if any(target[key] != binding[key] for key in target):
                db.execute('''UPDATE recipient_bindings SET channel=?,channel_choice=?,address_generation=?,enabled=?,
                    generation=generation+1,updated=? WHERE agent=? AND member=?''',
                    (target['channel'], target['channel_choice'], target['address_generation'], target['enabled'],
                     now, record['id'], member_id))
                if not target['enabled']:
                    cancel_queued(db, record['id'], now, member_id)  # a participant's own Stop
                self._bump(db, record['id'], by, now)
        return self._result(record)

    def set_delivery(self, agent_id, state, *, by, company_owner=False, expected_generation=None):
        record = self._saved(agent_id)
        self._caller(by)
        if state not in ('on', 'paused'):
            raise PhoneChatError(FIELDS, 400)
        now = self.clock()
        with self.messaging._db(write=True) as db:
            self._ensure(db, record, now)
            self._refresh(db, record, now)
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (record['id'],)).fetchone()
            self._manager(policy, by, company_owner)
            self._expected(policy, expected_generation)
            if policy['delivery'] != state:
                db.execute('UPDATE phone_policies SET delivery=? WHERE agent=?', (state, record['id']))
                if state == 'paused':
                    cancel_queued(db, record['id'], now)  # Stop for this agent
                self._bump(db, record['id'], by, now)
        return self._result(record)

    def request_contact(self, agent_id, member_id, *, by, company_owner=False):
        """Raise the contact card in the agent's chat, or return the open one."""
        record = self._saved(agent_id)
        self._caller(by)
        now = self.clock()
        with self.messaging._db(write=True) as db:
            self._ensure(db, record, now)
            self._refresh(db, record, now)
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (record['id'],)).fetchone()
            self._participant(db, record['id'], member_id)
            if by != member_id:
                self._manager(policy, by, company_owner)
            if self.messaging.eligible(db, member_id):
                raise PhoneChatError(NUMBER_VERIFIED, 409)
            request = self.public_request(self._request(db, record, member_id, by, now))
        self.publish(agent_id=record['id'])
        return request

    def recipient(self, agent_id, chat_id, member_id, *, channel=None, host=False):
        """The gate's answer: the one member this participant-reach agent's turn may message.

        The member comes from the admitted turn sender. A turn with no human
        sender, started by a schedule or an event, uses the creator's binding
        and requires it enabled under a policy that is on.
        """
        record = self._saved(agent_id)
        now = self.clock()
        with self.messaging._db(write=True) as db:
            self._ensure(db, record, now)
            self._refresh(db, record, now)
            policy = db.execute('SELECT * FROM phone_policies WHERE agent=?', (record['id'],)).fetchone()
            row = db.execute('''SELECT p.role,b.* FROM phone_participants p JOIN recipient_bindings b
                ON b.agent=p.agent AND b.member=p.member WHERE p.agent=? AND p.member=?
                AND p.removed IS NULL AND b.revoked IS NULL''', (record['id'], member_id)).fetchone()
            if (row is None or chat_id != policy['chat'] or self._member(member_id) is None
                    or host and row['role'] != 'creator'):
                raise PhoneChatError(PARTICIPANTS_ONLY, 403)
            if host and policy['delivery'] != 'on':
                raise PhoneChatError(PAUSED, 409)
            if host and not row['enabled']:
                raise PhoneChatError(SWITCHED_OFF, 409)
            eligible = dict(self.messaging.eligible(db, member_id))
            selected = channel or row['channel']
            if selected not in eligible:
                raise PhoneChatError(VERIFY_CHANNEL, 409)
            return {'agent_id': record['id'], 'chat_id': policy['chat'], 'member_id': member_id, 'channel': selected,
                'generation': row['generation'], 'address_generation': eligible[selected],
                'policy_generation': policy['generation']}

    def participant_chat(self, chat_id, member_id):
        """True when this chat is a participant-reach agent's and the member is one of its current participants."""
        if not _identity(chat_id) or self._member(member_id) is None:
            return False
        try:
            with self.messaging._db() as db:
                if db is None or db.execute('SELECT 1 FROM phone_policies WHERE chat=?', (chat_id,)).fetchone() is None:
                    return False
                return chat_admits(db, chat_id, member_id)
        except (ValueError, OSError, sqlite3.Error):
            return False  # An unreadable store admits nobody.

    def publish(self, *, agent_id=None, member_id=None):
        """Mirror each contact card's state into its chat entry, outside the store's transaction."""
        if self.post is None:
            return 0
        clause, values = '', []
        for column, value in (('agent', agent_id), ('member', member_id)):
            if value is not None:
                clause += f' AND {column}=?'
                values.append(value)
        with self.messaging._db() as db:
            rows = [] if db is None else db.execute(
                'SELECT * FROM contact_requests WHERE posted IS NOT status' + clause + ' ORDER BY created,id', values).fetchall()
        written = []
        for row in rows:
            try:
                title = self.agents.get(row['agent'])['title']
            except (ValueError, KeyError, TypeError):
                continue
            name = (self._member(row['member']) or {}).get('display_name') or 'this participant'
            entry = {'role': 'assistant', 'content': card_text(row['status'], title, name, row['channel']),
                'contact_request': self.public_request(row), 'created_at': row['created']}
            if self.post(row['chat'], entry) is True:
                written.append((row['status'], row['id'], row['status']))
        if written:
            with self.messaging._db(write=True) as db:
                db.executemany('UPDATE contact_requests SET posted=? WHERE id=? AND status=?', written)
        return len(written)
