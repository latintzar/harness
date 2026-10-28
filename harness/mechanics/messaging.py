"""Scoped phone-channel enrollment, one-attempt delivery and verified reply admission.

Every contact, binding, outbox row, inbox row and delivery event names its
channel and its sender account. A channel is one ChannelAdapter; WhatsApp is the
built one. All identities originate at the host. Public projections exclude
OTPs, provider credentials and inbox text. Call synchronous methods off the HTTP
event loop. Incoming human replies require the application's ordinary
authenticated chat admission, with the returned stable admission_key as its
durable idempotency key.

Host alert deliveries of participant phone chat (phone_delivery.py) send
through this same outbox, one row per attempt, so their receipts and replies
follow the built paths. Every message of a Tracking Agent's phone chat, alert
or answer, reserves its units against the spend bound (phone_spend.py) in the
transaction that marks it dispatching. The host's Stop and a spend record the
host cannot read hold every other provider-bound send of the company too:
security codes, the greeting and answers in any conversation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time

from messaging_provider import (ALL_CHANNELS, CHANNELS, CHANNEL_LABELS, ChannelAdapter, ProviderError, SendNotStarted,
    SendRejected, SendUncertain, bounded, digest, e164)
from phone_chat import SCHEMA as PHONE_SCHEMA, chat_admits, resolve_member
from phone_delivery import (SCHEMA as DELIVERY_SCHEMA, receipt as delivery_receipt, restart as delivery_restart,
    settle as delivery_settle)
from phone_spend import SCHEMA as SPEND_SCHEMA, SpendHeld, admit as spend_admit, release, reserve

from capability_spend import (SCHEMA as CAPABILITY_SPEND_SCHEMA, CapabilitySpendHeld,
    release as capability_release)

MAX_PENDING_SCAN = 256
SCHEMA_VERSION = 1  # 0: one WhatsApp phone per member. 1: verified channel addresses.
RECENT_REPLY_SECONDS = 24 * 3600


class MessagingError(ValueError):
    pass


PARTICIPANTS_ONLY = 'A Tracking Agent’s phone messages reach its own participants. Use an Action agent to message other people.'
OPTED_OUT = 'This member stopped messages on this channel.'


def require(value, message):
    if not value:
        raise MessagingError(message)


@dataclass(frozen=True)
class MessagingActor:
    company_id: str
    member_id: str
    collection: str
    chat_id: str
    actor_id: str

    def __post_init__(self):
        require(all(bounded(value) for value in vars(self).values()), 'Use the current member conversation.')


CONTACTS = '''CREATE TABLE IF NOT EXISTS contacts(member TEXT NOT NULL,channel TEXT NOT NULL,address TEXT NOT NULL,
        account TEXT NOT NULL,generation INTEGER NOT NULL,setup_chat TEXT NOT NULL,collection TEXT NOT NULL,
        verified_at REAL NOT NULL,verified_challenge TEXT NOT NULL,last_incoming REAL,opted_out REAL,
        PRIMARY KEY(member,channel),UNIQUE(account,address))'''
CONTACT_GENERATIONS = '''CREATE TABLE IF NOT EXISTS contact_generations(member TEXT NOT NULL,channel TEXT NOT NULL,
        generation INTEGER NOT NULL,PRIMARY KEY(member,channel))'''
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS identity(company TEXT NOT NULL)',
    CONTACTS,
    CONTACT_GENERATIONS,
    '''CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY,member TEXT NOT NULL,requester TEXT NOT NULL,
        address TEXT NOT NULL,account TEXT NOT NULL,chat TEXT NOT NULL,collection TEXT NOT NULL,
        code_hash TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,attempts INTEGER NOT NULL,
        status TEXT NOT NULL,outbox_id TEXT NOT NULL,channel TEXT NOT NULL DEFAULT 'whatsapp')''',
    '''CREATE TABLE IF NOT EXISTS bindings(member TEXT NOT NULL,account TEXT NOT NULL,generation INTEGER NOT NULL,
        chat TEXT NOT NULL,collection TEXT NOT NULL,created REAL NOT NULL,channel TEXT NOT NULL DEFAULT 'whatsapp',
        PRIMARY KEY(member,account,chat))''',
    '''CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,member TEXT NOT NULL,account TEXT NOT NULL,
        generation INTEGER,address TEXT NOT NULL,chat TEXT NOT NULL,collection TEXT NOT NULL,
        kind TEXT NOT NULL,text TEXT,request_id TEXT NOT NULL,intent_sha256 TEXT NOT NULL,
        status TEXT NOT NULL,provider_message_id TEXT,created REAL NOT NULL,updated REAL NOT NULL,
        waiting INTEGER NOT NULL DEFAULT 0,channel TEXT NOT NULL DEFAULT 'whatsapp',
        UNIQUE(member,request_id),UNIQUE(account,provider_message_id))''',
    '''CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,account TEXT NOT NULL,provider_event_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,member TEXT,address TEXT NOT NULL,chat TEXT,collection TEXT,
        contact_generation INTEGER,text TEXT,reply_context TEXT,sent_at REAL NOT NULL,received_at REAL NOT NULL,
        status TEXT NOT NULL,claim_id TEXT,admission_key TEXT NOT NULL,channel TEXT NOT NULL DEFAULT 'whatsapp',
        UNIQUE(account,provider_event_id))''',
    '''CREATE TABLE IF NOT EXISTS delivery_events(account TEXT NOT NULL,provider_message_id TEXT NOT NULL,
        status TEXT NOT NULL,sent_at REAL NOT NULL,address TEXT NOT NULL,channel TEXT NOT NULL DEFAULT 'whatsapp',
        PRIMARY KEY(account,provider_message_id,status))''',
    '''CREATE TABLE IF NOT EXISTS message_contexts(account TEXT NOT NULL,kind TEXT NOT NULL,
        value TEXT NOT NULL,outbox TEXT NOT NULL,PRIMARY KEY(account,kind,value))''',
    '''CREATE TABLE IF NOT EXISTS email_reply_ids(account TEXT NOT NULL,event TEXT NOT NULL,
        message_id TEXT NOT NULL,PRIMARY KEY(account,event))''',
    'CREATE INDEX IF NOT EXISTS challenge_member_created ON challenges(member,created)',
    'CREATE INDEX IF NOT EXISTS inbox_status ON inbox(status,received_at)',
) + PHONE_SCHEMA + SPEND_SCHEMA + DELIVERY_SCHEMA + CAPABILITY_SPEND_SCHEMA


def upgrade(db):
    """Bring an existing store to SCHEMA_VERSION inside the caller's transaction.

    Version 0 held one WhatsApp phone per member. Every row stays: a contact
    becomes that member's verified address on the whatsapp channel, and each
    outbox, inbox, challenge, binding and delivery row gains the channel it was
    sent or received on. Running it again changes nothing.
    """
    version = db.execute('PRAGMA user_version').fetchone()[0]
    require(version <= SCHEMA_VERSION, 'Use the current company messaging store.')
    if version == SCHEMA_VERSION:
        return
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def columns(table):
        return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
    if 'contacts' in tables and 'channel' not in columns('contacts'):
        db.execute('ALTER TABLE contacts RENAME TO contacts_before_channels')
        db.execute(CONTACTS)
        db.execute('''INSERT INTO contacts(member,channel,address,account,generation,setup_chat,collection,
            verified_at,verified_challenge,last_incoming,opted_out)
            SELECT member,'whatsapp',phone,account,generation,setup_chat,collection,verified_at,verified_challenge,
            last_incoming,NULL FROM contacts_before_channels''')
        db.execute('DROP TABLE contacts_before_channels')
    if 'contact_generations' in tables and 'channel' not in columns('contact_generations'):
        db.execute('ALTER TABLE contact_generations RENAME TO contact_generations_before_channels')
        db.execute(CONTACT_GENERATIONS)
        db.execute('''INSERT INTO contact_generations(member,channel,generation)
            SELECT member,'whatsapp',generation FROM contact_generations_before_channels''')
        db.execute('DROP TABLE contact_generations_before_channels')
    for table in ('challenges', 'outbox', 'inbox', 'delivery_events', 'bindings'):
        if table not in tables:
            continue
        if 'phone' in columns(table):
            db.execute(f'ALTER TABLE {table} RENAME COLUMN phone TO address')
        if 'channel' not in columns(table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN channel TEXT NOT NULL DEFAULT 'whatsapp'")
    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')


class Messaging:
    def __init__(self, state, *, company_id, provider=None, adapters=(), authorized=lambda actor: False, clock=time.time,
            spend=None, send_admission=None, channel_names=None, capability_spend=None):
        require(bounded(company_id) and callable(authorized), 'Use the host company authority.')
        self.state = Path(state).absolute()
        self.path = self.state / 'messaging.sqlite'
        self.company_id, self.authorized, self.clock = company_id, authorized, clock
        # The host's spend record, read afresh ahead of every send. The application
        # host always installs its reader (server.py), so there a missing record
        # holds every phone send. A store built with no reader has no host bound:
        # a Tracking Agent's phone messages stay unsent and its other sends follow
        # their own built limits.
        self.spend = spend
        self.capability_spend = capability_spend
        self.channel_names = tuple(CHANNELS if channel_names is None else channel_names)
        require(len(self.channel_names) == len(set(self.channel_names))
                and all(name in ALL_CHANNELS for name in self.channel_names), 'Install the selected message channels.')
        self.send_admission = send_admission
        self.adapters = {}
        for adapter in ([provider] if provider is not None else []) + list(adapters or ()):
            self._install(adapter)
        self._jobs = {}
        self._closed = False
        self._pending_after = None
        self._unsettled_after = None
        self._unsettled_scan_lock = threading.Lock()
        self._pending_scan_lock = threading.Lock()

    # Channel adapters -------------------------------------------------

    def _install(self, adapter, *, replace=False):
        """One adapter per channel, for this company, each with its own sender account."""
        try:
            valid = (isinstance(adapter, ChannelAdapter) and adapter.channel in self.channel_names
                and adapter.company_id == self.company_id and bounded(adapter.account_id, 256))
        except Exception:
            valid = False
        require(valid, 'Use this company’s messaging account.')
        others = {channel: other for channel, other in self.adapters.items() if channel != adapter.channel}
        require((replace or adapter.channel not in self.adapters)
            and all(other.account_id != adapter.account_id for other in others.values()),
            'Use one sender account for each channel.')
        self.adapters[adapter.channel] = adapter

    @property
    def provider(self):
        """The WhatsApp adapter, the first built transport."""
        return self.adapters.get('whatsapp')

    @provider.setter
    def provider(self, value):
        if value is None:
            self.adapters.pop('whatsapp', None)
            return
        require(getattr(value, 'channel', None) == 'whatsapp', 'Use this company’s messaging account.')
        self._install(value, replace=True)

    def adapter(self, channel):
        require(channel in self.adapters, 'provider_setup_required')
        return self.adapters[channel]

    def channels(self):
        """Selected channels, each with its adapter and spending readiness."""
        result = []
        for channel in self.channel_names:
            adapter = self.adapters.get(channel)
            try:
                ready = adapter.readiness() if adapter is not None else None
            except Exception:
                ready = None
            if not isinstance(ready, dict):
                ready = {'ready': False, 'reason': 'provider_setup_required'}
            if channel in {'email', 'voice'} and ready.get('ready') is True:
                if self.capability_spend is None or not self.capability_spend.ready(adapter, self.clock()):
                    ready = dict(ready, ready=False, reason='spending_required')
            result.append({'channel': channel, 'label': CHANNEL_LABELS[channel], 'ready': ready.get('ready') is True,
                'reason': ready.get('reason') if ready.get('ready') is not True else None, 'sender': ready.get('sender')})
        return result

    def member_channels(self, db, member):
        """This member's verified addresses on the current sender accounts, by channel."""
        held = {}
        if db is None:
            return held
        for row in db.execute('SELECT * FROM contacts WHERE member=?', (member,)).fetchall():
            adapter = self.adapters.get(row['channel'])
            if row['channel'] in self.channel_names and (adapter is None or adapter.account_id == row['account']):
                held[row['channel']] = {'address': row['address'], 'generation': row['generation'],
                    'verified_at': row['verified_at'], 'opted_out_at': row['opted_out'], 'row': row}
        return {channel: held[channel] for channel in self.channel_names if channel in held}

    def eligible(self, db, member):
        """(channel, address generation) in channel order: verified, opted in, on a commissioned channel."""
        ready = {row['channel'] for row in self.channels() if row['ready']}
        held = self.member_channels(db, member)
        return [(channel, held[channel]['generation']) for channel in held
            if channel in ready and held[channel]['opted_out_at'] is None]

    def _default_channel(self, member):
        """The channel a send uses when its caller names none: channel order, then the built transport."""
        with self._db() as db:
            eligible = self.eligible(db, member)
            if eligible:
                return eligible[0][0]
            return next((channel for channel in self.member_channels(db, member) if channel in self.adapters), 'whatsapp')

    def return_replies(self, db, member, chat):
        """Unadmitted replies of a member who left a Tracking Agent's participants continue their setup chat."""
        for row in db.execute("SELECT id,account FROM inbox WHERE member=? AND chat=? AND status='pending'", (member, chat)).fetchall():
            contact = db.execute('SELECT setup_chat,collection FROM contacts WHERE member=? AND account=?', (member, row['account'])).fetchone()
            if contact is None:
                db.execute("UPDATE inbox SET status='blocked',text=NULL WHERE id=?", (row['id'],))
            else:
                db.execute('UPDATE inbox SET chat=?,collection=? WHERE id=?', (contact['setup_chat'], contact['collection'], row['id']))
        db.execute('UPDATE outbox SET waiting=0 WHERE member=? AND chat=?', (member, chat))

    # Authority and storage --------------------------------------------

    def _spend_open(self, channel=None):
        """The host's Stop and an unreadable spend record hold every provider-bound send of the company."""
        if self.spend is None or channel in {'email', 'voice'}:
            return
        try:
            spend_admit(self.spend)
        except SpendHeld as held:
            raise MessagingError(str(held)) from None

    def host_actor(self, member, collection, chat):
        """The member themself in one conversation, for host work addressed to them."""
        return MessagingActor(self.company_id, member, collection, chat, member)

    def _auth(self, actor, *, self_only=False):
        require(isinstance(actor, MessagingActor) and actor.company_id == self.company_id,
                'Use this company’s member conversation.')
        require(not self_only or actor.actor_id == actor.member_id, 'Verify your own messaging number.')
        try:
            current = self.authorized(actor)
        except Exception:
            current = False
        require(current is True, 'Open an authorized member conversation.')

    @contextmanager
    def _db(self, *, write=False, before_commit=None):
        if not self.path.exists() and not write:
            yield None
            return
        require(not self.path.is_symlink(), 'Use the private company messaging store.')
        if write:
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            except FileExistsError:
                pass
        info = self.path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'Use the private company messaging store.')
        def connect():
            value = sqlite3.connect(str(self.path) if write else 'file:' + str(self.path) + '?mode=ro',
                                    uri=not write, timeout=5)
            value.row_factory = sqlite3.Row
            return value
        db = connect()
        try:
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA max_page_count=16384')
                db.execute('BEGIN IMMEDIATE')
                upgrade(db)
                for statement in SCHEMA:
                    db.execute(statement)
                identity = db.execute('SELECT company FROM identity').fetchall()
                if not identity:
                    db.execute('INSERT INTO identity VALUES(?)', (self.company_id,))
                else:
                    require(len(identity) == 1 and identity[0][0] == self.company_id, 'Use this company’s messaging store.')
            else:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                require(version <= SCHEMA_VERSION, 'Use the current company messaging store.')
                if version != SCHEMA_VERSION:
                    # An existing store upgrades in place, in one write transaction, ahead of its first read.
                    db.close()
                    with self._db(write=True):
                        pass
                    db = connect()
                identity = db.execute('SELECT company FROM identity').fetchall()
                require(len(identity) == 1 and identity[0][0] == self.company_id, 'Use this company’s messaging store.')
            yield db
            if write:
                if before_commit is not None:
                    before_commit()
                db.commit()
        finally:
            db.close()

    def _ready(self, kind, channel):
        provider = self.adapter(channel)
        ready = provider.readiness()
        require(ready['ready'], ready['reason'])
        if kind == 'hello':
            require(ready['hello_ready'], ready['hello_reason'])
        return provider

    def _account(self, channel):
        return self.adapter(channel).account_id

    def _contact(self, db, member, channel):
        row = db.execute('SELECT * FROM contacts WHERE member=? AND channel=? AND account=?',
                         (member, channel, self._account(channel))).fetchone()
        require(row is not None, 'Verify this member’s messaging number.')
        return row

    # Public projections -----------------------------------------------

    @staticmethod
    def _public_contact(row):
        return None if row is None else {'member_id': row['member'], 'phone': row['address'], 'channel': row['channel'],
            'verified': True, 'verified_at': row['verified_at'], 'generation': row['generation'],
            'setup_chat_id': row['setup_chat'], 'collection': row['collection']}

    @staticmethod
    def _public_challenge(row):
        return None if row is None else {'id': row['id'], 'member_id': row['member'], 'channel': row['channel'],
            'phone': row['address'], 'status': row['status'], 'expires_at': row['expires'],
            'resend_after': row['created'] + 60, 'attempts_remaining': max(0, 5-row['attempts'])}

    @staticmethod
    def _public_outbox(row):
        return {'id': row['id'], 'kind': row['kind'], 'channel': row['channel'], 'status': row['status'], 'text': row['text'],
            'chat_id': row['chat'], 'collection': row['collection'], 'provider_message_id': row['provider_message_id'],
            'created_at': row['created'], 'updated_at': row['updated'],
            'waiting_for_reply': bool(row['waiting']), 'finish_turn': row['kind'] != 'otp'}

    def describe(self, actor):
        self._auth(actor)
        primary = 'whatsapp' if self.channel_names == CHANNELS else next(iter(self.channel_names), None)
        adapter = self.adapters.get(primary)
        readiness = adapter.readiness() if adapter else {
            'provider': primary, 'configured': False, 'ready': False, 'reason': 'provider_setup_required',
            'otp_ready': False, 'hello_ready': False, 'hello_reason': 'provider_setup_required', 'webhook_ready': False}
        if primary in {'email', 'voice'} and readiness.get('ready') is True:
            if self.capability_spend is None or not self.capability_spend.ready(adapter, self.clock()):
                readiness = dict(readiness, ready=False, reason='spending_required', otp_ready=False, hello_ready=False)
        addresses, challenge, outbox = [], None, []
        with self._db() as db:
            if db:
                addresses = [self._public_contact(value['row']) for value in self.member_channels(db, actor.member_id).values()]
                challenge = db.execute('SELECT * FROM challenges WHERE member=? ORDER BY created DESC,id DESC LIMIT 1', (actor.member_id,)).fetchone()
                outbox = db.execute('SELECT * FROM outbox WHERE member=? ORDER BY created DESC,rowid DESC LIMIT 10', (actor.member_id,)).fetchall()
        # `contact` is the address on the first channel in channel order; `addresses` lists one per channel.
        return {'provider': primary, 'readiness': readiness, 'contact': addresses[0] if addresses else None,
            'addresses': addresses, 'channels': self.channels(),
            'challenge': self._public_challenge(challenge), 'outbox': [self._public_outbox(row) for row in outbox],
            'waiting': any(row['waiting'] for row in outbox)}

    # One-attempt dispatch ---------------------------------------------

    async def _run(self, operation, cancelled=None):
        require(not self._closed, 'Open messaging in the current application session.')
        stop = asyncio.Event()
        async def watch():
            await cancelled.wait()
            stop.set()
        watcher = asyncio.create_task(watch()) if cancelled is not None else None
        if cancelled is not None and cancelled.is_set():
            stop.set()
        task = asyncio.create_task(operation(stop))
        self._jobs[task] = stop
        interrupted = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    interrupted = True
                    stop.set()
                except Exception:
                    break
            if interrupted or stop.is_set():
                if not task.cancelled():
                    task.exception()
                raise asyncio.CancelledError
            return task.result()
        finally:
            self._jobs.pop(task, None)
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    def _current(self, actor, outbox_id, stop):
        try:
            if stop.is_set() or self._closed:
                return False
            self._auth(actor)
            with self._db() as db:
                row = db.execute('SELECT * FROM outbox WHERE id=? AND member=?', (outbox_id, actor.member_id)).fetchone()
                if row is not None:
                    self._spend_open(row['channel'])
                if row is None or row['status'] != 'dispatching' or row['account'] != self._account(row['channel']):
                    return False
                opted_out = db.execute('SELECT 1 FROM contacts WHERE account=? AND address=? AND opted_out IS NOT NULL',
                    (row['account'], row['address'])).fetchone()
                if opted_out:
                    return False
                if row['kind'] == 'otp':
                    challenge = db.execute('SELECT status,expires FROM challenges WHERE outbox_id=?', (outbox_id,)).fetchone()
                    return bool(challenge and challenge['status'] == 'pending' and challenge['expires'] > self.clock())
                contact = self._contact(db, actor.member_id, row['channel'])
                binding = db.execute('SELECT 1 FROM bindings WHERE member=? AND account=? AND chat=? AND collection=? AND generation=?',
                    (actor.member_id, row['account'], actor.chat_id, actor.collection, row['generation'])).fetchone()
                # Only a channel with a service window, WhatsApp, bounds free text by the member's last message.
                span = self.adapters[row['channel']].reply_window
                window = row['kind'] != 'text' or span is None or (contact['last_incoming'] is not None
                    and 0 <= self.clock()-contact['last_incoming'] < span)
                return bool(binding and window and contact['generation'] == row['generation'] and contact['address'] == row['address']
                    and chat_admits(db, actor.chat_id, actor.member_id))
        except (MessagingError, sqlite3.Error, OSError):
            return False

    def _settle(self, identifier, status, provider_id=None):
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM outbox WHERE id=?', (identifier,)).fetchone()
            require(row is not None, 'Read the admitted messaging action.')
            if row['status'] != 'dispatching':
                return self._public_outbox(row)
            if status == 'sent':
                context = getattr(provider_id, 'message_id', None)
                if context is not None:
                    require(bounded(context, 256), 'Use the provider message receipt.')
                    db.execute('INSERT INTO message_contexts(account,kind,value,outbox) VALUES(?,?,?,?)',
                               (row['account'], 'message_id', context, identifier))
                rank = {'sent': 1, 'failed': 2, 'delivered': 3, 'read': 4}
                delivery = db.execute('SELECT status FROM delivery_events WHERE account=? AND provider_message_id=? AND address=?',
                    (row['account'], provider_id, row['address'])).fetchall()
                if delivery:
                    status = max([status, *(item['status'] for item in delivery)], key=lambda value: rank[value])
            contact = db.execute('SELECT 1 FROM contacts WHERE member=? AND account=? AND generation=? AND address=?',
                (row['member'], row['account'], row['generation'], row['address'])).fetchone()
            db.execute('UPDATE outbox SET status=?,provider_message_id=?,updated=?,waiting=? WHERE id=?',
                (status, provider_id, self.clock(), int(status in {'sent', 'delivered', 'read'} and row['kind'] != 'otp' and bool(contact)), identifier))
            # A send that provably stayed inside Rocoto returns its units; sent, failed and uncertain keep them.
            returned = release(db, identifier) if status == 'cancelled' else 0
            if status == 'cancelled':
                capability_release(db, identifier)
            delivery_settle(db, identifier, status, provider_id, self.clock(), returned)
            if row['kind'] == 'otp':
                db.execute("UPDATE challenges SET status=? WHERE outbox_id=? AND status='pending'",
                    ('sent' if status in {'delivered', 'read'} else status, identifier))
            return self._public_outbox(db.execute('SELECT * FROM outbox WHERE id=?', (identifier,)).fetchone())

    def _capability_admission(self, actor, row, prepared, stop):
        if row['channel'] not in {'email', 'voice'}:
            return True
        try:
            if self.capability_spend is None:
                return False
            def fence():
                require(self._current(actor, row['id'], stop), 'Use the current message authority.')
                self.capability_spend.grant(self.adapter(row['channel']), self.clock())
            with self._db(write=True, before_commit=fence) as db:
                fence()
                latest = db.execute('SELECT * FROM outbox WHERE id=?', (row['id'],)).fetchone()
                self.capability_spend.reserve(db, latest, self.adapter(row['channel']), prepared, self.clock())
            return True
        except (CapabilitySpendHeld, MessagingError, OSError, sqlite3.Error):
            return False

    async def _dispatch(self, actor, row, prepared, stop, still=None):
        status, provider_id = 'uncertain', None
        async def current():
            # Receipt reads may wait for a short SQLite commit. Keep them off the
            # shared loop, then repeat the cheap live actor/Stop fence after await.
            allowed = await asyncio.to_thread(self._current, actor, row['id'], stop)
            # A host delivery repeats its own admission checks immediately before the request leaves.
            if allowed and still is not None: allowed = await asyncio.to_thread(still) is True
            if allowed and self.send_admission is not None:
                allowed = await asyncio.to_thread(self.send_admission, actor, row) is True
                # Quota admission can wait; contact, account and host delivery
                # authority must still be current after that wait completes.
                if allowed: allowed = await asyncio.to_thread(self._current, actor, row['id'], stop)
                if allowed and still is not None: allowed = await asyncio.to_thread(still) is True
            if allowed:
                allowed = await asyncio.to_thread(self._capability_admission, actor, row, prepared, stop)
                if allowed: allowed = await asyncio.to_thread(self._current, actor, row['id'], stop)
                if allowed and still is not None: allowed = await asyncio.to_thread(still) is True
            if not allowed or stop.is_set() or self._closed: return False
            try: self._auth(actor)
            except MessagingError: return False
            # This last bounded transaction has no subsequent await before the
            # adapter's request hook returns: tightened grants and Stop take effect here.
            return self._capability_admission(actor, row, prepared, stop)
        try:
            async with asyncio.timeout(25):
                provider_id = await self.adapter(row['channel']).send(prepared, current=current)
            status = 'sent'
        except SendNotStarted:
            status = 'cancelled'
        except SendRejected:
            status = 'failed'
        except (SendUncertain, TimeoutError):
            pass
        except Exception:
            pass  # Claim already exists: retain a conservative uncertain effect.
        return await asyncio.to_thread(self._settle, row['id'], status, provider_id)

    async def send_prepared(self, actor, row, prepared, *, still=None, cancelled=None):
        """One provider request for a host delivery whose outbox row its claim already wrote.

        The fence inside the dispatch reads the member's authority again. A
        store that is closing settles the row as a send that stayed inside Rocoto.
        """
        if self._closed:
            return await asyncio.to_thread(self._settle, row['id'], 'cancelled', None)
        async def operation(stop):
            return await self._dispatch(actor, row, prepared, stop, still)
        return await self._run(operation, cancelled)

    def _replay(self, db, actor, request_id, intent):
        require(bounded(request_id), 'Use a stable message request identity.')
        prior = db.execute('SELECT * FROM outbox WHERE member=? AND request_id=?', (actor.member_id, request_id)).fetchone()
        if prior:
            require(prior['intent_sha256'] == intent, 'Use the original message for this request identity.')
        return prior

    def _insert(self, db, actor, identifier, *, channel, address, kind, text, request_id, intent, generation=None):
        require(db.execute('SELECT count(*) FROM outbox').fetchone()[0] < 20000, 'Review retained messaging history.')
        now = self.clock()
        db.execute('''INSERT INTO outbox(id,member,account,generation,address,chat,collection,kind,text,request_id,
            intent_sha256,status,created,updated,channel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (identifier, actor.member_id, self._account(channel), generation, address, actor.chat_id, actor.collection,
             kind, text, request_id, intent, 'dispatching', now, now, channel))
        return db.execute('SELECT * FROM outbox WHERE id=?', (identifier,)).fetchone()

    # Enrollment -------------------------------------------------------

    async def request_code(self, actor, phone, request_id=None, *, channel='whatsapp', cancelled=None):
        """Send a security code over the channel it proves. `phone` is that channel's address."""
        self._auth(actor)
        require(channel in self.channel_names, 'Choose an available message channel.')
        try:
            # An address is validated ahead of any provider work, also while its channel is being prepared.
            phone = self.adapters[channel].address(phone) if channel in self.adapters else e164(phone)
        except ProviderError as exc:
            raise MessagingError(str(exc)) from None
        require(bounded(phone, 254), 'Enter the address for this channel.')
        request_id = request_id or 'verify-' + secrets.token_hex(16)
        async def operation(stop):
            def prepare():
                provider = self._ready('otp', channel)
                account = provider.account_id
                with self._db(write=True, before_commit=lambda: self._auth(actor)) as db:
                    self._auth(actor)
                    require(not stop.is_set(), 'The messaging action has finished.')
                    intent = digest(['otp', actor.member_id, actor.chat_id, actor.collection, account, phone])
                    prior = self._replay(db, actor, request_id, intent)
                    if prior:
                        return db.execute('SELECT * FROM challenges WHERE outbox_id=?', (prior['id'],)).fetchone(), None, None
                    self._spend_open(channel)  # a new security code is a paid send: Stop and an unreadable record hold it
                    now = self.clock()
                    recent = db.execute('SELECT created FROM challenges WHERE (member=? OR address=?) AND created>?',
                        (actor.member_id, phone, now-3600)).fetchall()
                    require(len(recent) < 3 and all(row['created'] <= now-60 for row in recent), 'Wait before requesting another security code.')
                    require(db.execute('SELECT count(*) FROM challenges WHERE created>?', (now-3600,)).fetchone()[0] < 100,
                        'Finish the current company verification requests.')
                    require(db.execute('SELECT 1 FROM contacts WHERE account=? AND address=? AND opted_out IS NOT NULL',
                        (account, phone)).fetchone() is None, OPTED_OUT)
                    code, identifier = f'{secrets.randbelow(1_000_000):06d}', 'verify-' + secrets.token_hex(16)
                    outbox_id = 'msg-' + secrets.token_hex(16)
                    prepared = provider.prepare('otp', phone, code=code, reference=outbox_id)
                    row = self._insert(db, actor, outbox_id, channel=channel, address=phone, kind='otp', text=None,
                        request_id=request_id, intent=intent)
                    db.execute("UPDATE challenges SET status='superseded' WHERE member=? AND channel=? AND status IN ('pending','sent','uncertain')",
                        (actor.member_id, channel))
                    db.execute('''INSERT INTO challenges(id,member,requester,address,account,chat,collection,code_hash,
                        created,expires,attempts,status,outbox_id,channel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identifier, actor.member_id, actor.actor_id, phone, account, actor.chat_id, actor.collection,
                         provider.otp_hash(identifier, code), now, now+300, 0, 'pending', row['id'], channel))
                    challenge = db.execute('SELECT * FROM challenges WHERE id=?', (identifier,)).fetchone()
                    require(not stop.is_set(), 'The messaging action has finished.')
                    return challenge, row, prepared
            challenge, row, prepared = await asyncio.to_thread(prepare)
            if row is not None:
                await self._dispatch(actor, row, prepared, stop)
            def read():
                with self._db() as db:
                    return self._public_challenge(db.execute('SELECT * FROM challenges WHERE id=?', (challenge['id'],)).fetchone())
            return await asyncio.to_thread(read)
        try:
            return await self._run(operation, cancelled)
        except ProviderError as exc:
            raise MessagingError(str(exc)) from None

    def verify_code(self, actor, challenge_id, code):
        """Confirm a security code. The challenge carries its channel."""
        self._auth(actor, self_only=True)
        require(bounded(challenge_id) and isinstance(code, str) and re.fullmatch(r'[0-9]{6}', code), 'Enter the six-digit security code.')
        error, contact = None, None
        with self._db(write=True, before_commit=lambda: self._auth(actor, self_only=True)) as db:
            self._auth(actor, self_only=True)
            row = db.execute('SELECT * FROM challenges WHERE id=? AND member=? AND chat=? AND collection=?',
                (challenge_id, actor.member_id, actor.chat_id, actor.collection)).fetchone()
            require(self.adapters, 'provider_setup_required')
            require(row is not None and row['channel'] in self.adapters
                and row['account'] == self.adapters[row['channel']].account_id, 'Use this member’s current verification challenge.')
            channel, provider = row['channel'], self.adapters[row['channel']]
            matches = hmac.compare_digest(row['code_hash'], provider.otp_hash(challenge_id, code))
            if row['status'] == 'verified' and matches:
                contact = self._contact(db, actor.member_id, channel)
                require(contact['verified_challenge'] == challenge_id, 'Use the current verified contact.')
            elif row['expires'] <= self.clock() or row['attempts'] >= 5 or row['status'] not in {'sent', 'uncertain'}:
                error = 'Request a fresh security code.'
            elif not matches:
                db.execute('UPDATE challenges SET attempts=attempts+1 WHERE id=?', (challenge_id,))
                error = 'Enter the current security code.'
            else:
                # A number stays with the member who verified it, on every channel.
                duplicate = db.execute('SELECT member FROM contacts WHERE address=? AND member!=?', (row['address'], actor.member_id)).fetchone()
                require(duplicate is None, 'Use a number available for this member.')
                previous = db.execute('SELECT generation FROM contact_generations WHERE member=? AND channel=?', (actor.member_id, channel)).fetchone()
                generation = previous['generation']+1 if previous else 1
                db.execute('INSERT OR REPLACE INTO contact_generations(member,channel,generation) VALUES(?,?,?)', (actor.member_id, channel, generation))
                db.execute('''INSERT OR REPLACE INTO contacts(member,channel,address,account,generation,setup_chat,collection,
                    verified_at,verified_challenge,last_incoming,opted_out) VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)''',
                    (actor.member_id, channel, row['address'], provider.account_id, generation, actor.chat_id, actor.collection,
                     self.clock(), challenge_id))
                db.execute('DELETE FROM bindings WHERE member=? AND channel=?', (actor.member_id, channel))
                db.execute('INSERT INTO bindings(member,account,generation,chat,collection,created,channel) VALUES(?,?,?,?,?,?,?)',
                    (actor.member_id, provider.account_id, generation, actor.chat_id, actor.collection, self.clock(), channel))
                db.execute("UPDATE challenges SET status='verified' WHERE id=?", (challenge_id,))
                contact = self._contact(db, actor.member_id, channel)
                # Completing a contact card resolves this member's waiting bindings in the same commit.
                resolve_member(db, actor.member_id, self.clock(), self.eligible(db, actor.member_id))
        if error:
            raise MessagingError(error)
        return self._public_contact(contact)

    def revoke(self, actor, *, channel=None):
        """Explicit authenticated contact removal, for one channel or all; accepted sends still drain."""
        self._auth(actor)
        require(channel is None or channel in self.channel_names, 'Choose an available message channel.')
        scope, values = ('', (actor.member_id,)) if channel is None else (' AND channel=?', (actor.member_id, channel))
        with self._db(write=True, before_commit=lambda: self._auth(actor)) as db:
            self._auth(actor)
            db.execute('DELETE FROM contacts WHERE member=?' + scope, values)
            db.execute('DELETE FROM bindings WHERE member=?' + scope, values)
            db.execute("UPDATE challenges SET status='revoked' WHERE member=? AND status IN ('pending','sent','uncertain')" + scope, values)
            db.execute("UPDATE inbox SET status='blocked' WHERE member=? AND status='pending'" + scope, values)
            db.execute('UPDATE outbox SET waiting=0 WHERE member=?' + scope, values)
            resolve_member(db, actor.member_id, self.clock(), self.eligible(db, actor.member_id))
        return {'member_id': actor.member_id, 'verified': False}

    # Conversations and sends ------------------------------------------

    def bind_conversation(self, actor, *, channel=None):
        self._auth(actor, self_only=True)
        channel = channel or self._default_channel(actor.member_id)
        require(channel in self.channel_names, 'Choose an available message channel.')
        with self._db(write=True, before_commit=lambda: self._auth(actor, self_only=True)) as db:
            self._auth(actor, self_only=True)
            contact = self._contact(db, actor.member_id, channel)
            db.execute('INSERT OR REPLACE INTO bindings(member,account,generation,chat,collection,created,channel) VALUES(?,?,?,?,?,?,?)',
                (actor.member_id, contact['account'], contact['generation'], actor.chat_id, actor.collection, self.clock(), channel))
        return {'chat_id': actor.chat_id, 'member_id': actor.member_id, 'collection': actor.collection, 'channel': channel}

    async def send_hello(self, actor, *, request_id, channel=None, cancelled=None):
        return await self._send(actor, 'hello', None, request_id, cancelled, channel)

    async def send_message(self, actor, text, *, request_id, channel=None, cancelled=None):
        require(bounded(text, 4096), 'Write a message up to 4096 characters.')
        return await self._send(actor, 'text', text, request_id, cancelled, channel)

    async def send_alert(self, actor, alert, *, request_id, channel=None, cancelled=None):
        require(self.adapters, 'provider_setup_required')
        # With one possible channel the alert is checked against its template ahead of any store work.
        only = self.adapters.get(channel) if channel else self.provider if set(self.adapters) == {'whatsapp'} else None
        if only is not None:
            only.preview('utility', alert=alert)
        return await self._send(actor, 'utility', alert, request_id, cancelled, channel)

    async def _send(self, actor, kind, text, request_id, cancelled, channel=None):
        self._auth(actor, self_only=True)
        require(channel is None or channel in self.channel_names, 'Choose an available message channel.')
        async def operation(stop):
            def prepare():
                selected = channel or self._default_channel(actor.member_id)
                provider = self._ready(kind, selected)
                account = provider.account_id
                with self._db(write=True, before_commit=lambda: self._auth(actor, self_only=True)) as db:
                    self._auth(actor, self_only=True)
                    require(not stop.is_set(), 'The messaging action has finished.')
                    contact = self._contact(db, actor.member_id, selected)
                    intent = digest([kind, actor.member_id, actor.chat_id, actor.collection, account, contact['address'], text])
                    prior = self._replay(db, actor, request_id, intent)
                    if prior:
                        return prior, None
                    self._spend_open(selected)  # the greeting and an answer in any conversation follow the host's Stop
                    # Host alert deliveries keep their own stable IDs and reconciliation; this fence covers answers.
                    require(db.execute("""SELECT 1 FROM outbox WHERE member=? AND account=? AND kind!='otp'
                        AND status IN ('dispatching','uncertain')
                        AND id NOT IN (SELECT outbox FROM phone_deliveries WHERE outbox IS NOT NULL)""",
                        (actor.member_id, account)).fetchone() is None, 'Confirm the previous message delivery.')
                    binding = db.execute('SELECT * FROM bindings WHERE member=? AND account=? AND chat=? AND collection=? AND generation=?',
                        (actor.member_id, account, actor.chat_id, actor.collection, contact['generation'])).fetchone()
                    require(binding is not None, 'Bind this member’s conversation before sending.')
                    require(chat_admits(db, actor.chat_id, actor.member_id), PARTICIPANTS_ONLY)
                    require(contact['opted_out'] is None, OPTED_OUT)
                    if kind == 'hello':
                        require(actor.chat_id == contact['setup_chat'] and actor.collection == contact['collection'], 'Send the greeting from messaging setup.')
                    elif kind == 'text' and provider.reply_window is not None:
                        # WhatsApp only: free text follows a message from that person inside the service window.
                        require(contact['last_incoming'] is not None and 0 <= self.clock()-contact['last_incoming'] < provider.reply_window,
                            'Use an approved template or wait for this member’s reply.')
                    identifier = 'msg-' + secrets.token_hex(16)
                    prepared = provider.prepare(kind, contact['address'], text=text if kind == 'text' else None,
                        alert=text if kind == 'utility' else None, reference=identifier)
                    incoming = db.execute("""SELECT provider_event_id,
                        (SELECT message_id FROM email_reply_ids e WHERE e.account=inbox.account
                         AND e.event=inbox.provider_event_id) AS email_message_id FROM inbox WHERE account=? AND member=?
                        AND contact_generation=? AND chat=? AND collection=? AND status IN ('pending','admitted')
                        ORDER BY received_at DESC,rowid DESC LIMIT 1""",
                        (account, actor.member_id, contact['generation'], actor.chat_id, actor.collection)).fetchone()
                    if incoming is not None and kind == 'text':
                        prepared = provider.prepare_reply(prepared, incoming['provider_event_id'], message_id=incoming['email_message_id'])
                    public_text = provider.preview(kind, text=text if kind == 'text' else None, alert=text if kind == 'utility' else None)
                    tracking = db.execute('SELECT agent FROM phone_policies WHERE chat=?', (actor.chat_id,)).fetchone()
                    if tracking is not None and selected not in {'email', 'voice'}:
                        # An answer in a Tracking Agent's phone chat counts against the spend bound, like its alerts.
                        try:
                            reserve(db, self.spend, tracking['agent'], provider.units(prepared), self.clock(), identifier)
                        except SpendHeld as held:
                            raise MessagingError(str(held)) from None
                    row = self._insert(db, actor, identifier, channel=selected, address=contact['address'], kind=kind, text=public_text,
                        request_id=request_id, intent=intent, generation=contact['generation'])
                    require(not stop.is_set(), 'The messaging action has finished.')
                    return row, prepared
            row, prepared = await asyncio.to_thread(prepare)
            if prepared is None:
                return self._public_outbox(row)
            return await self._dispatch(actor, row, prepared, stop)
        try:
            return await self._run(operation, cancelled)
        except ProviderError as exc:
            raise MessagingError(str(exc)) from None

    # Signed provider events -------------------------------------------

    def _reply_target(self, db, provider, contact, reply_to, now, reply_kind='provider'):
        """Which conversation a reply continues, by the channel's own rule (CHANNELS.md).

        Only an outgoing message to this same member, under a current binding,
        chooses an agent conversation. An exact provider reply context wins when
        the message carries one. A message with no reply context, on any channel,
        continues the most recent delivery to that verified address inside 24
        hours. Everything else uses setup.
        """
        base = '''SELECT o.chat,o.collection FROM outbox o JOIN bindings b
            ON b.member=o.member AND b.account=o.account AND b.generation=o.generation
            AND b.chat=o.chat AND b.collection=o.collection
            WHERE o.account=? AND o.member=? AND o.address=? AND o.generation=?'''
        values = (contact['account'], contact['member'], contact['address'], contact['generation'])
        if reply_to and reply_kind == 'message_id' and provider.channel == 'email':
            return db.execute(base + """ AND o.kind!='otp' AND o.status IN ('sent','delivered','read')
                AND EXISTS(SELECT 1 FROM message_contexts c WHERE c.account=o.account AND c.outbox=o.id
                           AND c.kind='message_id' AND c.value=?)""", (*values, reply_to)).fetchone()
        if reply_to and provider.reply_rule != 'recent':
            # The returned value is a lookup key among that sender's own outgoing messages, never a chat ID.
            # A context naming none of them chooses no agent conversation.
            column = 'o.id' if provider.reply_rule == 'tracking' else 'o.provider_message_id'
            strict = " AND o.kind!='otp' AND o.status IN ('sent','delivered','read')" if provider.require_reply_context else ''
            return db.execute(base + f' AND {column}=?' + strict, (*values, reply_to)).fetchone()
        if provider.require_reply_context:
            return None
        # No reply context: the most recent delivery to that address inside 24 hours.
        return db.execute(base + ''' AND o.kind!='otp' AND o.status IN ('sent','delivered','read')
            AND o.created>? AND o.created<=? ORDER BY o.created DESC,o.rowid DESC LIMIT 1''',
            (*values, now-RECENT_REPLY_SECONDS, now)).fetchone()

    def handle_webhook(self, raw_body, signature=None, *, channel='whatsapp'):
        """Admit one signed provider request. `signature` is the header value or the request headers."""
        provider = self.adapters.get(channel)
        require(provider is not None and provider.commissioned is True, 'provider_setup_required')
        headers = {provider.signature_header: signature} if isinstance(signature, str) or signature is None else signature
        try:
            provider.verify(raw_body, headers)
            events = provider.events(raw_body)
        except ProviderError as exc:
            raise MessagingError(str(exc)) from None
        result = {'accepted': 0, 'duplicates': 0, 'blocked': 0, 'ignored': int(events.get('ignored', 0)),
            'delivery_updates': 0, 'opt_outs': 0}
        result['ignored'] += len(events.get('subscriptions', []))  # A subscription binds nobody until its adapter's card verifies it.
        now, account = self.clock(), provider.account_id
        with self._db(write=True) as db:
            messages = events.get('messages', [])
            require(db.execute('SELECT count(*) FROM inbox').fetchone()[0] + len(messages) < 50000, 'Review retained messaging events.')
            for value in events.get('receipts', []):
                try:
                    identifier, status, address = value['provider_message_id'], value['status'], value['address']
                    timestamp = float(value['at'])
                except (KeyError, ValueError, TypeError):
                    continue
                if (status not in {'sent', 'delivered', 'read', 'failed'} or not bounded(identifier, 256)
                        or not bounded(address, 254) or not now-7*86400 <= timestamp <= now+300):
                    continue
                row = db.execute('SELECT * FROM outbox WHERE account=? AND provider_message_id=?', (account, identifier)).fetchone()
                if row is not None and address != row['address']:
                    continue
                if row is None and not db.execute("SELECT 1 FROM outbox WHERE account=? AND address=? AND status='dispatching'", (account, address)).fetchone():
                    continue
                require(db.execute('SELECT count(*) FROM delivery_events').fetchone()[0] < 80000, 'Review retained delivery history.')
                inserted = db.execute('INSERT OR IGNORE INTO delivery_events(account,provider_message_id,status,sent_at,address,channel) VALUES(?,?,?,?,?,?)',
                    (account, identifier, status, timestamp, address, channel)).rowcount
                if inserted:
                    rank = {'sent': 1, 'failed': 2, 'delivered': 3, 'read': 4}
                    if row is not None and rank.get(status, 0) > rank.get(row['status'], 0):
                        db.execute('UPDATE outbox SET status=?,updated=?,waiting=CASE WHEN ?=\'failed\' THEN 0 ELSE waiting END WHERE id=?',
                            (status, now, status, row['id']))
                        delivery_receipt(db, row['id'], status, now)
                    result['delivery_updates'] += 1
            # A stop keyword or an unsubscribe event is handled ahead of routing and never becomes a chat message.
            for value in events.get('opt_outs', []):
                try:
                    address, timestamp, opted_out = value['address'], float(value['at']), value['opted_out']
                except (KeyError, ValueError, TypeError):
                    continue
                contact = db.execute('SELECT member FROM contacts WHERE account=? AND address=?', (account, address)).fetchone() if bounded(address, 254) else None
                if contact is None or type(opted_out) is not bool or not now-7*86400 <= timestamp <= now+300:
                    continue
                db.execute('UPDATE contacts SET opted_out=? WHERE account=? AND address=?', (timestamp if opted_out else None, account, address))
                if opted_out:
                    db.execute('UPDATE outbox SET waiting=0 WHERE member=? AND account=?', (contact['member'], account))
                resolve_member(db, contact['member'], now, self.eligible(db, contact['member']))
                result['opt_outs'] += 1
            for value in messages:
                try:
                    identifier, address, text = value['id'], value['address'], value['text']
                    sent_at, context, body_hash = float(value['sent_at']), value.get('reply_to'), value['payload_sha256']
                    require(bounded(identifier, 256) and bounded(text, 4096) and bounded(address, 254)
                        and (context is None or bounded(context, 256))
                        and isinstance(body_hash, str) and re.fullmatch(r'[0-9a-f]{64}', body_hash), 'Use a bounded text reply.')
                except (KeyError, TypeError, ValueError):
                    result['ignored'] += 1
                    continue
                if not now-7*86400 <= sent_at <= now+300:
                    result['ignored'] += 1
                    continue
                prior = db.execute('SELECT payload_sha256 FROM inbox WHERE account=? AND provider_event_id=?', (account, identifier)).fetchone()
                if prior:
                    require(prior[0] == body_hash, 'Review the repeated provider event identity.')
                    result['duplicates'] += 1
                    continue
                contact = db.execute('SELECT * FROM contacts WHERE account=? AND address=?', (account, address)).fetchone()
                member, chat, collection, generation = None, None, None, None
                status = 'unverified'
                if contact:
                    member, generation = contact['member'], contact['generation']
                    chat, collection = contact['setup_chat'], contact['collection']
                    matched = self._reply_target(db, provider, contact, context, now, value.get('reply_kind', 'provider'))
                    # A Tracking Agent's chat takes the reply from a current participant.
                    # Every other reply continues the member's private setup chat.
                    if matched and chat_admits(db, matched['chat'], member):
                        chat, collection = matched['chat'], matched['collection']
                    actor = MessagingActor(self.company_id, member, collection, chat, member)
                    try:
                        require(not provider.require_reply_context or matched is not None
                                and chat_admits(db, matched['chat'], member), 'Use the original message conversation.')
                        self._auth(actor, self_only=True)
                        status = 'pending'
                    except MessagingError:
                        status = 'blocked'
                    if status == 'pending':
                        if channel == 'email' and value.get('message_id') is not None:
                            require(bounded(value['message_id'], 256), 'Use the verified email identifier.')
                            db.execute('INSERT OR IGNORE INTO email_reply_ids VALUES(?,?,?)',
                                       (account, identifier, value['message_id']))
                        db.execute('UPDATE contacts SET last_incoming=max(coalesce(last_incoming,0),?) WHERE member=? AND channel=?', (sent_at, member, channel))
                        db.execute('UPDATE outbox SET waiting=0 WHERE member=? AND account=? AND chat=? AND collection=?', (member, account, chat, collection))
                event_id = 'in-' + secrets.token_hex(16)
                admission_key = channel + ':' + digest([self.company_id, account, identifier])
                db.execute('''INSERT INTO inbox(id,account,provider_event_id,payload_sha256,member,address,chat,collection,
                    contact_generation,text,reply_context,sent_at,received_at,status,claim_id,admission_key,channel)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (event_id, account, identifier, body_hash, member, address, chat, collection, generation,
                     text if status == 'pending' else None, context, sent_at, now, status, None, admission_key, channel))
                result['accepted' if status == 'pending' else 'blocked'] += 1
        return result

    def _reply(self, row):
        return {'id': row['id'], 'company_id': self.company_id, 'member_id': row['member'],
            'chat_id': row['chat'], 'collection': row['collection'], 'text': row['text'],
            'provider': row['channel'], 'channel': row['channel'], 'account': row['account'],
            'provider_event_id': row['provider_event_id'],
            'reply_context': row['reply_context'], 'sent_at': row['sent_at'], 'received_at': row['received_at'],
            'admission_key': row['admission_key'], 'claim_id': row['claim_id']}

    def _admissible(self, db, row):
        """A reply is admitted for a current member, on their current address, into a chat that takes them."""
        actor = MessagingActor(self.company_id, row['member'], row['collection'], row['chat'], row['member'])
        self._auth(actor, self_only=True)
        contact = self._contact(db, row['member'], row['channel'])
        require(contact['generation'] == row['contact_generation'], 'Use the current verified contact.')
        require(chat_admits(db, row['chat'], row['member']), PARTICIPANTS_ONLY)

    def pending_replies(self, limit=32):
        require(type(limit) is int and 1 <= limit <= 100, 'Use a bounded reply batch.')
        with self._pending_scan_lock, self._db() as db:
            if db is None:
                return []
            # Retain temporarily unavailable authority for a later tick, while
            # advancing past it so another member's reply can reach admission.
            # The in-memory keyset is only a scan hint; claims remain durable.
            after = self._pending_after
            clause = ' AND (received_at,id) > (?,?)' if after is not None else ''
            rows = db.execute("SELECT * FROM inbox WHERE status='pending'" + clause
                + ' ORDER BY received_at,id LIMIT ?', (*after, MAX_PENDING_SCAN) if after is not None
                else (MAX_PENDING_SCAN,)).fetchall()
            if after is not None and len(rows) < MAX_PENDING_SCAN:
                rows += db.execute("SELECT * FROM inbox WHERE status='pending' AND (received_at,id) <= (?,?)"
                    ' ORDER BY received_at,id LIMIT ?', (*after, MAX_PENDING_SCAN-len(rows))).fetchall()
            result = []
            for row in rows:
                try:
                    self._admissible(db, row)
                except MessagingError:
                    if not result:
                        self._pending_after = (row['received_at'], row['id'])
                    continue
                result.append(self._reply(row))
                if len(result) == limit:
                    break
            # Keep the next scan before the first eligible row until its host
            # caller claims it. A full unavailable cycle restarts at the oldest.
            if not result and len(rows) < MAX_PENDING_SCAN:
                self._pending_after = None
            return result

    def signal_reply_current(self, event_id, signal):
        """Recheck the exact verified provider event at each signal admission."""
        try:
            with self._db() as db:
                if db is None:
                    return False
                row = db.execute('SELECT * FROM inbox WHERE id=?', (event_id,)).fetchone()
                if row is None or not (row['status'] == 'pending' or signal.get('status') == 'claimed'
                        and row['status'] in {'admitting', 'admitted', 'admission_uncertain'}):
                    return False
                self._admissible(db, row)
                sender = signal.get('sender', {})
                connection = 'message-' + hashlib.sha256(
                    json.dumps([row['channel'], row['account']]).encode()).hexdigest()
                return (signal.get('chat_id') == row['chat'] and signal.get('collection') == row['collection']
                    and signal.get('actor_principal_id') == row['member'] and signal.get('text') == row['text']
                    and signal.get('provider') == row['channel'] and signal.get('connection_id') == connection
                    and signal.get('event_id') == row['provider_event_id']
                    and sender.get('principal_id') == row['member'] and sender.get('channel') == row['channel']
                    and sender.get('admission_key') == row['admission_key']
                    and sender.get('provider_event_id') == row['provider_event_id'])
        except MessagingError:
            return False

    def unsettled_replies(self, limit=8):
        """Receipts needing reconciliation only; they never authorize new work."""
        require(type(limit) is int and 1 <= limit <= 100, 'Use a bounded reply batch.')
        with self._unsettled_scan_lock, self._db() as db:
            if db is None:
                return []
            # The cursor is a fairness hint only. Durable claims and ordinary
            # messages remain the authority for completing each receipt.
            after = self._unsettled_after
            query = ("SELECT * FROM inbox WHERE status IN ('admitting','admission_uncertain') "
                "AND claim_id IS NOT NULL")
            clause = ' AND (received_at,id) > (?,?)' if after is not None else ''
            rows = db.execute(query + clause + ' ORDER BY received_at,id LIMIT ?',
                (*after, limit) if after is not None else (limit,)).fetchall()
            if after is not None and len(rows) < limit:
                rows += db.execute(query + ' AND (received_at,id) <= (?,?) ORDER BY received_at,id LIMIT ?',
                    (*after, limit-len(rows))).fetchall()
            self._unsettled_after = (rows[-1]['received_at'], rows[-1]['id']) if rows else None
            return [self._reply(row) for row in rows]

    def claim_reply(self, event_id):
        require(bounded(event_id), 'Read an admitted provider reply.')
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM inbox WHERE id=?', (event_id,)).fetchone()
            require(row is not None, 'Read an admitted provider reply.')
            if row['status'] != 'pending':
                return None
            self._admissible(db, row)
            db.execute("UPDATE inbox SET status='admitting',claim_id=? WHERE id=?", (secrets.token_hex(16), event_id))
            return self._reply(db.execute('SELECT * FROM inbox WHERE id=?', (event_id,)).fetchone())

    def finish_reply(self, event_id, claim_id, *, admitted):
        require(type(admitted) is bool and bounded(claim_id), 'Record the actual chat admission outcome.')
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM inbox WHERE id=? AND claim_id=?', (event_id, claim_id)).fetchone()
            require(row is not None and row['status'] in {'admitting', 'admitted', 'admission_uncertain'}, 'Finish the owned reply claim.')
            status = 'admitted' if admitted else 'admission_uncertain'
            if row['status'] == 'admitted':
                require(admitted, 'Preserve the admitted reply receipt.')
            db.execute('UPDATE inbox SET status=? WHERE id=?', (status, event_id))
        return {'id': event_id, 'status': status}

    def recover(self):
        """Call after prior workers drain. Uncertain work is never replayed."""
        if not self.path.exists():
            return {'uncertain_sends': 0, 'uncertain_replies': 0}
        with self._db(write=True) as db:
            sent = db.execute("UPDATE outbox SET status='uncertain',updated=? WHERE status='dispatching'", (self.clock(),)).rowcount
            delivery_restart(db, self.clock())
            db.execute("UPDATE challenges SET status='uncertain' WHERE status='pending'")
            replies = db.execute("UPDATE inbox SET status='admission_uncertain' WHERE status='admitting'").rowcount
        return {'uncertain_sends': sent, 'uncertain_replies': replies}

    async def close(self):
        self._closed = True
        for stop in self._jobs.values():
            stop.set()
        interrupted = False
        for task in tuple(self._jobs):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    interrupted = True
                except Exception:
                    break
        if interrupted:
            raise asyncio.CancelledError


SEND_SPEC = {'type':'function','name':'send_message',
    'description':'Send a requested phone message to the current authenticated member’s verified number on their channel: Viber, SMS or WhatsApp. The host selects the member and the channel; supply the text alone. In a Tracking Agent conversation the member is one of that agent’s own participants; messages to other people belong to an Action Agent. WhatsApp requires a current customer-service window. The saved delivery receipt ends this turn; a verified incoming reply resumes this conversation.',
    'inputSchema':{'type':'object','properties':{'text':{'type':'string','minLength':1,'maxLength':4096}},
        'required':['text'],'additionalProperties':False}}


ALERT_SPEC = {'type':'function','name':'send_alert',
    'description':'Send a requested operational phone alert to the current verified member on their channel, using the approved Utility template for its purpose. The host selects the member and the channel. In a Tracking Agent conversation the member is one of that agent’s own participants. Use monitoring, urgent (only an enabled urgent rule), reminder, or a subscribed weekly report. Supply exactly the three factual details in template order. The receipt ends this turn and a verified reply resumes the chat.',
    'inputSchema':{'type':'object','properties':{
        'purpose':{'type':'string','enum':['monitoring','urgent','reminder','weekly']},
        'language':{'type':'string','enum':['en_US','es']},
        'parameters':{'type':'array','minItems':3,'maxItems':3,'items':{'type':'string','minLength':1,'maxLength':800}}},
        'required':['purpose','language','parameters'],'additionalProperties':False}}
