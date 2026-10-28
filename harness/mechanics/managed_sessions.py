"""Durable managed-session identity, submissions and local effect receipts.

Conversation messages remain in the chat store and OpenAI's session history. This
ledger holds identity, input hashes and receipts needed to recover local effects.
A claimed mutation with an unknown outcome is reviewed rather than repeated.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

TERMINAL = frozenset({'completed', 'failed', 'cancelled'})
# The documented waiting state awaits external input, including function results.
ACTIVE_TURNS = frozenset({'in_progress', 'waiting'})
REVIEW_RECEIPT = {'success': False, 'output':
                  'This operation needs review of its saved outcome before another attempt.'}


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:@/-]{1,512}', value):
        raise ValueError('Use a bounded identity.')
    return value


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def new_key():
    return 'rocoto-' + uuid.uuid4().hex


class ManagedSessions:
    """Each operation commits before a caller sends input or performs an effect."""

    def __init__(self, state):
        self.path = Path(state) / 'managed-sessions.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._private_file(create=True)
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, company_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    owner TEXT NOT NULL, collection TEXT NOT NULL, configuration TEXT NOT NULL,
                    session_id TEXT UNIQUE, creation_key TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1, configuration_body TEXT, capabilities TEXT,
                    UNIQUE(company_id, chat_id, generation));
                CREATE TABLE IF NOT EXISTS session_retirements (
                    binding_id TEXT PRIMARY KEY REFERENCES sessions(id), reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS submissions (
                    binding_id TEXT NOT NULL REFERENCES sessions(id), logical_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload_hash TEXT NOT NULL, submission_key TEXT NOT NULL,
                    PRIMARY KEY(binding_id, logical_id));
                CREATE TABLE IF NOT EXISTS calls (
                    binding_id TEXT NOT NULL REFERENCES sessions(id), call_id TEXT NOT NULL,
                    action_hash TEXT NOT NULL, generation TEXT NOT NULL, state TEXT NOT NULL,
                    receipt TEXT, submission_key TEXT NOT NULL,
                    PRIMARY KEY(binding_id, call_id));
                CREATE TABLE IF NOT EXISTS delivery_boundaries (
                    request_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, call_id TEXT NOT NULL,
                    receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES sessions(id),
                    logical_id TEXT NOT NULL, input TEXT, input_hash TEXT NOT NULL,
                    status TEXT NOT NULL, baseline TEXT NOT NULL, turn_id TEXT,
                    UNIQUE(binding_id, logical_id));
                CREATE TABLE IF NOT EXISTS artifacts (
                    binding_id TEXT NOT NULL REFERENCES sessions(id), artifact_id TEXT NOT NULL,
                    metadata_hash TEXT NOT NULL, file_id TEXT NOT NULL,
                    PRIMARY KEY(binding_id, artifact_id));
                CREATE TABLE IF NOT EXISTS turns (
                    binding_id TEXT NOT NULL REFERENCES sessions(id), turn_id TEXT NOT NULL,
                    subagent_id TEXT, actor_chat TEXT NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY(binding_id, turn_id));
            ''')
        self._migrate()

    def _private_file(self, *, create=False):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        descriptor = os.open(self.path, flags, 0o600)
        try:
            value = os.fstat(descriptor)
            if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid()
                    or value.st_nlink != 1 or value.st_mode & 0o077):
                raise ValueError('Use private managed session receipt storage.')
        finally:
            os.close(descriptor)

    @contextmanager
    def transaction(self):
        self._private_file()
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _migrate(self):
        # A separate reader backs up the last committed state while this reserved
        # write lock prevents changes. The schema conversion is one transaction.
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            fields = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
            if 'generation' not in fields:
                backup_path = self.path.with_name('managed-sessions.before-generations-' + uuid.uuid4().hex + '.sqlite3')
                descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
                reader = sqlite3.connect(self.path, timeout=30)
                backup = sqlite3.connect(backup_path)
                try:
                    reader.backup(backup)
                    if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Verify the original managed ledger backup before migration.')
                finally:
                    reader.close()
                    backup.close()
                descriptor = os.open(backup_path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                # Preserve the original table name in all existing foreign keys.
                db.execute('CREATE TABLE sessions_next ('
                    'id TEXT PRIMARY KEY, company_id TEXT NOT NULL, chat_id TEXT NOT NULL, '
                    'owner TEXT NOT NULL, collection TEXT NOT NULL, configuration TEXT NOT NULL, '
                    'session_id TEXT UNIQUE, creation_key TEXT NOT NULL, '
                    'generation INTEGER NOT NULL, configuration_body TEXT, capabilities TEXT, '
                    'UNIQUE(company_id, chat_id, generation))')
                db.execute('INSERT INTO sessions_next SELECT *, 1, NULL, NULL FROM sessions')
                db.execute('DROP TABLE sessions')
                db.execute('ALTER TABLE sessions_next RENAME TO sessions')
            if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('Preserve every managed request and effect receipt during migration.')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def fingerprint(configuration, capabilities=None):
        return digest(configuration if capabilities is None else
                      {'configuration': configuration, 'capabilities': capabilities})

    @staticmethod
    def _binding_record(row):
        if row is None:
            return None
        result = dict(row)
        for field in ('configuration_body', 'capabilities'):
            result[field] = json.loads(result[field]) if result[field] is not None else None
        return result

    def _insert(self, db, values, configuration, capabilities, generation):
        body = encoded(configuration)
        if len(body.encode()) > 4_000_000:
            raise ValueError('Save bounded managed configuration with private archive references.')
        binding_id = new_key()
        db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
                   (binding_id, *values, self.fingerprint(configuration, capabilities), new_key(),
                    generation, body, encoded(capabilities) if capabilities is not None else None))
        return self._binding_record(db.execute('SELECT * FROM sessions WHERE id=?', (binding_id,)).fetchone())

    def binding(self, *, company_id, chat_id, owner, collection, configuration, capabilities=None):
        values = tuple(identity(value) for value in (company_id, chat_id, owner, collection))
        with self.transaction() as db:
            row = db.execute('SELECT * FROM sessions WHERE company_id=? AND chat_id=? '
                             'ORDER BY generation DESC LIMIT 1', values[:2]).fetchone()
            if row is None:
                return self._insert(db, values, configuration, capabilities, 1)
            if db.execute('SELECT 1 FROM session_retirements WHERE binding_id=?', (row['id'],)).fetchone():
                raise ValueError('Create a new managed generation after session retirement.')
            if (row['owner'], row['collection'], row['configuration']) != (
                    owner, collection, self.fingerprint(configuration, capabilities)):
                raise ValueError('Continue with the saved session owner, collection and configuration.')
            return self._binding_record(row)

    def rollover(self, *, company_id, chat_id, owner, collection, configuration,
                 previous_id, quiesced, capabilities=None):
        values = tuple(identity(value) for value in (company_id, chat_id, owner, collection))
        if quiesced is not True:
            raise ValueError('Confirm the previous root and children are quiescent before rollover.')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM sessions WHERE company_id=? AND chat_id=? '
                             'ORDER BY generation DESC LIMIT 1', values[:2]).fetchone()
            if row is None or row['id'] != previous_id or (row['owner'], row['collection']) != values[2:]:
                raise ValueError('Roll over the current original workspace binding.')
            pending = db.execute("SELECT requests.id FROM requests JOIN sessions ON sessions.id=requests.binding_id "
                "WHERE sessions.company_id=? AND sessions.chat_id=? AND requests.status NOT IN "
                "('completed','failed','cancelled') LIMIT 1", values[:2]).fetchone()
            if pending:
                raise ValueError('Recover the original pending request before session rollover.')
            return self._insert(db, values, configuration, capabilities, row['generation'] + 1)

    def retire(self, binding_id, reason):
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO session_retirements VALUES (?, ?)',
                       (identity(binding_id), identity(reason)))

    def retired(self, binding_id):
        with self.transaction() as db:
            return db.execute('SELECT 1 FROM session_retirements WHERE binding_id=?',
                              (identity(binding_id),)).fetchone() is not None

    def for_owner(self, company_id, owner):
        with self.transaction() as db:
            return [self._binding_record(row) for row in db.execute(
                'SELECT * FROM sessions WHERE company_id=? AND owner=? ORDER BY generation',
                (identity(company_id), identity(owner)))]

    def generations(self, company_id, chat_id):
        with self.transaction() as db:
            return [self._binding_record(row) for row in db.execute(
                'SELECT * FROM sessions WHERE company_id=? AND chat_id=? ORDER BY generation',
                (identity(company_id), identity(chat_id)))]

    def artifact(self, binding_id, artifact_id, metadata, *, file_id=None):
        values = (identity(binding_id), identity(artifact_id))
        metadata_hash = digest(metadata)
        with self.transaction() as db:
            row = db.execute('SELECT metadata_hash,file_id FROM artifacts WHERE binding_id=? AND artifact_id=?', values).fetchone()
            if row is not None:
                if row['metadata_hash'] != metadata_hash or (file_id is not None and row['file_id'] != file_id):
                    raise ValueError('Keep the immutable artifact publication identity.')
                return row['file_id']
            if file_id is not None:
                db.execute('INSERT INTO artifacts VALUES (?,?,?,?)', (*values, metadata_hash, identity(file_id)))
            return file_id

    def request_binding(self, company_id, chat_id, logical_id):
        with self.transaction() as db:
            row = db.execute('SELECT sessions.* FROM sessions JOIN requests ON requests.binding_id=sessions.id '
                'WHERE sessions.company_id=? AND sessions.chat_id=? AND requests.logical_id=?',
                (identity(company_id), identity(chat_id), identity(logical_id))).fetchone()
            return self._binding_record(row)

    def pending_binding(self, company_id, chat_id):
        with self.transaction() as db:
            row = db.execute("SELECT sessions.* FROM sessions JOIN requests ON requests.binding_id=sessions.id "
                "WHERE sessions.company_id=? AND sessions.chat_id=? AND requests.status NOT IN "
                "('completed','failed','cancelled') ORDER BY sessions.generation LIMIT 1",
                (identity(company_id), identity(chat_id))).fetchone()
            return self._binding_record(row)

    def get(self, binding_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (identity(binding_id),)).fetchone()
            if row is None:
                raise ValueError('Use a saved managed session binding.')
            return self._binding_record(row)

    def created(self, binding_id, session_id):
        with self.transaction() as db:
            row = db.execute('SELECT session_id FROM sessions WHERE id=?', (identity(binding_id),)).fetchone()
            if row is None or row['session_id'] not in (None, session_id):
                raise ValueError('Keep the provider session bound to its original workspace.')
            db.execute('UPDATE sessions SET session_id=? WHERE id=?', (identity(session_id), binding_id))

    def submission(self, binding_id, logical_id, kind, payload):
        values = (identity(binding_id), identity(logical_id))
        fingerprint = digest(payload)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM submissions WHERE binding_id=? AND logical_id=?', values).fetchone()
            if row is not None:
                if (row['kind'], row['payload_hash']) != (kind, fingerprint):
                    raise ValueError('A submission identity keeps its exact original input.')
                return row['submission_key']
            key = new_key()
            db.execute('INSERT INTO submissions VALUES (?, ?, ?, ?, ?)',
                       (*values, identity(kind), fingerprint, key))
            return key

    def claim(self, binding_id, action, *, generation, actor_chat, write, recoverable=False):
        if not isinstance(action, dict) or action.get('type') != 'function_call':
            raise ValueError('Execute a function from the current required actions.')
        for field in ('turn_id', 'call_id', 'name'):
            identity(action.get(field))
        if not isinstance(action.get('arguments'), dict) or type(write) is not bool:
            raise ValueError('Use a declared function call and effect classification.')
        identity(generation)
        fingerprint = digest({'action': action, 'actor_chat': identity(actor_chat), 'write': write})
        values = (identity(binding_id), action['call_id'])
        with self.transaction() as db:
            row = db.execute('SELECT * FROM calls WHERE binding_id=? AND call_id=?', values).fetchone()
            if row is None:
                key = new_key()
                db.execute('INSERT INTO calls VALUES (?, ?, ?, ?, ?, NULL, ?)',
                           (*values, fingerprint, generation, 'claimed', key))
                return {'decision': 'execute', 'submission_key': key}
            if row['action_hash'] != fingerprint:
                raise ValueError('A tool call identity keeps its original operation and authority.')
            result = {'submission_key': row['submission_key']}
            if row['state'] in {'completed', 'review'}:
                return result | {'decision': 'replay', 'receipt': json.loads(row['receipt'])}
            if row['generation'] == generation:
                return result | {'decision': 'pending'}
            if write and not (recoverable and action['name'] == 'delegate_job'):
                db.execute('UPDATE calls SET state=?, generation=?, receipt=? WHERE binding_id=? AND call_id=?',
                           ('review', generation, encoded(REVIEW_RECEIPT), *values))
                return result | {'decision': 'review', 'receipt': dict(REVIEW_RECEIPT)}
            db.execute('UPDATE calls SET generation=? WHERE binding_id=? AND call_id=?', (generation, *values))
            return result | {'decision': 'execute'}

    def completed(self, binding_id, call_id, generation, receipt):
        if (not isinstance(receipt, dict) or type(receipt.get('success')) is not bool
                or not isinstance(receipt.get('output'), str)):
            raise ValueError('Save the exact tool success and output receipt.')
        value = encoded(receipt)
        if len(value.encode()) > 4_000_000:
            raise ValueError('Save a bounded tool receipt.')
        with self.transaction() as db:
            changed = db.execute('UPDATE calls SET state=?, receipt=? WHERE binding_id=? AND call_id=? '
                                 'AND generation=? AND state=?',
                                 ('completed', value, binding_id, call_id, generation, 'claimed')).rowcount
            if changed != 1:
                raise ValueError('Only the current claimed execution may save its result.')

    def save_delivery_boundary(self, binding_id, request_id, call_id, delivery):
        """Stop recovery at the original durable send, even after a lost result reply."""
        value = encoded(delivery)
        with self.transaction() as db:
            request = db.execute('SELECT binding_id FROM requests WHERE id=?', (request_id,)).fetchone()
            call = db.execute('SELECT state, receipt FROM calls WHERE binding_id=? AND call_id=?',
                              (binding_id, call_id)).fetchone()
            if not request or request['binding_id'] != binding_id or not call or call['state'] != 'completed':
                raise ValueError('Bind the delivery to its original completed managed call.')
            receipt = json.loads(call['receipt'])
            if not receipt['success'] or json.loads(receipt['output']) != delivery:
                raise ValueError('Use the saved provider delivery receipt.')
            existing = db.execute('SELECT * FROM delivery_boundaries WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if (existing['binding_id'], existing['call_id'], existing['receipt']) != (binding_id, call_id, value):
                    raise ValueError('Keep the original delivery boundary for this request.')
                return
            db.execute('INSERT INTO delivery_boundaries VALUES (?, ?, ?, ?)',
                       (request_id, binding_id, call_id, value))

    def delivery_boundary(self, binding_id, request_id):
        with self.transaction() as db:
            row = db.execute('SELECT receipt FROM delivery_boundaries WHERE binding_id=? AND request_id=?',
                             (binding_id, request_id)).fetchone()
            return json.loads(row['receipt']) if row else None

    def turn(self, binding_id, turn, actor_chat):
        if not isinstance(turn, dict) or 'subagent_id' not in turn:
            raise ValueError('Use a provider turn with explicit agent attribution.')
        turn_id = identity(turn.get('id'))
        subagent = turn['subagent_id']
        if subagent is not None:
            identity(subagent)
        status = turn.get('status')
        if status not in TERMINAL | ACTIVE_TURNS | {'queued'}:
            raise ValueError('Use a documented turn state.')
        values = (identity(binding_id), turn_id)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM turns WHERE binding_id=? AND turn_id=?', values).fetchone()
            if row is None:
                db.execute('INSERT INTO turns VALUES (?, ?, ?, ?, ?)',
                           (*values, subagent, identity(actor_chat), status))
            elif (row['subagent_id'], row['actor_chat']) != (subagent, actor_chat):
                raise ValueError('Keep the original turn agent and conversation attribution.')
            elif row['status'] not in TERMINAL:
                db.execute('UPDATE turns SET status=? WHERE binding_id=? AND turn_id=?', (status, *values))

    def turns(self, binding_id):
        with self.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM turns WHERE binding_id=? ORDER BY rowid',
                                                    (identity(binding_id),))]

    @staticmethod
    def _request_record(row):
        if row is None:
            return None
        value = dict(row)
        value['input'] = json.loads(value['input']) if value['input'] is not None else None
        value['baseline'] = json.loads(value['baseline'])
        return value

    def request(self, binding_id, logical_id, input_value):
        """A pending outbox keeps exact input until its provider turn is terminal."""
        payload = encoded(input_value)
        if len(payload.encode()) > 16 * 1024 * 1024:
            raise ValueError('Keep pending managed input within the request allowance.')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM requests WHERE binding_id=? AND logical_id=?',
                             (binding_id, identity(logical_id))).fetchone()
            if row is not None:
                if row['input_hash'] != digest(input_value):
                    raise ValueError('Resume the saved request with its original input.')
                return self._request_record(row)
            selected = db.execute('SELECT * FROM sessions WHERE id=?', (binding_id,)).fetchone()
            latest = db.execute('SELECT id FROM sessions WHERE company_id=? AND chat_id=? '
                                'ORDER BY generation DESC LIMIT 1',
                                (selected['company_id'], selected['chat_id'])).fetchone()
            if latest['id'] != binding_id or db.execute('SELECT 1 FROM session_retirements WHERE binding_id=?', (binding_id,)).fetchone():
                raise ValueError('Send new input through the current managed session generation.')
            duplicate = db.execute('SELECT requests.id FROM requests JOIN sessions ON sessions.id=requests.binding_id '
                'WHERE sessions.company_id=? AND sessions.chat_id=? AND requests.logical_id=?',
                (selected['company_id'], selected['chat_id'], logical_id)).fetchone()
            if duplicate:
                raise ValueError('Recover this request through its original managed session.')
            pending = db.execute("SELECT id FROM requests WHERE binding_id=? AND status NOT IN "
                                 "('completed','failed','cancelled')", (binding_id,)).fetchone()
            if pending:
                raise ValueError('Recover the pending managed request before sending another input.')
            request_id = new_key()
            db.execute('INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
                       (request_id, binding_id, logical_id, payload, digest(input_value), 'prepared', '[]'))
            return self._request_record(db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone())

    def request_record(self, binding_id, logical_id):
        """Read one original input without admitting or changing it."""
        with self.transaction() as db:
            row = db.execute('SELECT * FROM requests WHERE binding_id=? AND logical_id=?',
                             (identity(binding_id), identity(logical_id))).fetchone()
            return self._request_record(row)

    def pending(self, binding_id):
        with self.transaction() as db:
            return self._request_record(db.execute("SELECT * FROM requests WHERE binding_id=? AND status NOT IN "
                "('completed','failed','cancelled') ORDER BY rowid LIMIT 1", (binding_id,)).fetchone())

    def for_chat(self, company_id, chat_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM sessions WHERE company_id=? AND chat_id=? ORDER BY generation DESC LIMIT 1',
                             (company_id, chat_id)).fetchone()
            return self._binding_record(row)

    def request_state(self, request_id, status, *, baseline=None, turn_id=None):
        if status not in TERMINAL | {'prepared', 'submitted'}:
            raise ValueError('Use a documented pending request state.')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ValueError('Use a saved pending request.')
            if row['status'] in TERMINAL:
                if row['status'] != status:
                    raise ValueError('Keep the final request outcome.')
                return
            if turn_id is not None and row['turn_id'] not in (None, turn_id):
                raise ValueError('Keep the original provider turn attribution.')
            db.execute('UPDATE requests SET status=?, baseline=?, turn_id=?, input=? WHERE id=?',
                       (status, encoded(baseline) if baseline is not None else row['baseline'],
                        turn_id or row['turn_id'], None if status in TERMINAL else row['input'], request_id))
