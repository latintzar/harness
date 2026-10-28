"""Durable display cadence, leases and run history shared by every vertical.

The default SQLite ledger is self-contained. A vertical may supply physical table and
column names to retain its installed store; the transactions and lease semantics stay
here. Input stamps, source projection and output publication belong to that vertical.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

LEASE_SECONDS = 300
ATTENTION_AFTER = 5
FAILURE_CEILING_SECONDS = 3600
MANUAL_FLOOR_SECONDS = 5
KEEP_TEST_RUNS = 10
RUN_PAGE = 200
PASS_TRIGGERS = ('events', 'day', 'manual', 'host')
INTERRUPTED = 'The pass stopped before it finished. The next pass runs the same work again.'
DEFAULT_POLICY = {'automatic': True, 'min_interval_seconds': 60, 'budget_seconds': 60, 'daily_at': None, 'paused': []}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

def iso(epoch):
    return None if epoch is None else datetime.fromtimestamp(round(epoch, 3), timezone.utc).isoformat().replace('+00:00', 'Z')

def clean_policy(value):
    """Validate the shared cadence and bounded runtime policy."""
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError('Use the schedule fields automatic, min_interval_seconds, budget_seconds, daily_at and paused.')
    policy = {**DEFAULT_POLICY, **value}
    whole = lambda number: type(number) is int
    if type(policy['automatic']) is not bool:
        raise ValueError('Set automatic to true or false.')
    if not whole(policy['min_interval_seconds']) or not 30 <= policy['min_interval_seconds'] <= 86400:
        raise ValueError('Choose a minimum interval between 30 seconds and one day.')
    if not whole(policy['budget_seconds']) or not 5 <= policy['budget_seconds'] <= 180:
        raise ValueError('Choose a time budget between 5 and 180 seconds.')
    daily = policy['daily_at']
    if daily is not None and (not isinstance(daily, str) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', daily)):
        raise ValueError('Give the daily time as HH:MM in the scope’s declared time.')
    paused = policy['paused']
    if (not isinstance(paused, list) or len(paused) > 100 or len(set(paused)) != len(paused)
            or any(not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', key) for key in paused)):
        raise ValueError('List the paused displays by their keys.')
    policy['paused'] = sorted(paused)
    return policy

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS display_schedule(
        owner TEXT NOT NULL, collection TEXT NOT NULL, scope_key TEXT NOT NULL,
        processed_stamp TEXT, processed_day TEXT, attempt_stamp TEXT,
        last_run_id TEXT, last_status TEXT, failures INTEGER NOT NULL DEFAULT 0,
        next_after REAL NOT NULL DEFAULT 0, manual_after REAL NOT NULL DEFAULT 0,
        requested_at REAL, requested_by TEXT, requested_only TEXT,
        lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0, lease_run_id TEXT,
        policy TEXT NOT NULL DEFAULT '{}', policy_revision INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(owner, collection, scope_key))""",
    """CREATE TABLE IF NOT EXISTS display_runs(
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, collection TEXT NOT NULL, scope_key TEXT NOT NULL,
        trigger TEXT NOT NULL, status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
        input_stamp TEXT NOT NULL, day TEXT, period TEXT, wall_ms INTEGER, derived TEXT,
        outputs TEXT, errors TEXT, skipped TEXT, error TEXT, published INTEGER NOT NULL DEFAULT 0,
        requested_by TEXT, requested_only TEXT)""",
    """CREATE INDEX IF NOT EXISTS display_runs_scope ON display_runs(owner, collection, scope_key, started_at)""",
    """CREATE TABLE IF NOT EXISTS display_script_jobs(
        request_id TEXT PRIMARY KEY, owner TEXT NOT NULL, collection TEXT NOT NULL, scope_key TEXT NOT NULL,
        display_id TEXT NOT NULL, scope TEXT NOT NULL, opened_at REAL NOT NULL)""",
)

class DisplayRuns:
    def __init__(self, state, *, clock=time.time, filename='display-runs.sqlite', names=None,
                 policy=clean_policy, keep_runs=lambda: 200, scope_field='scope_id', run_prefix='drun-'):
        self.path = Path(state) / filename
        self.clock, self.clean_policy, self.keep_runs = clock, policy, keep_runs
        self.scope_field, self.run_prefix = scope_field, run_prefix
        self.names = dict(names or {})
        allowed = {'display_schedule', 'display_runs', 'display_runs_scope', 'display_script_jobs', 'scope_key', 'display_id'}
        if set(self.names) - allowed or any(not re.fullmatch(r'[a-z][a-z0-9_]*', value) for value in self.names.values()):
            raise ValueError('Use registered storage identifiers for this display ledger.')
        self.scope_column = self.names.get('scope_key', 'scope_key')
        self._tokens = re.compile(r'\b(' + '|'.join(sorted(allowed, key=len, reverse=True)) + r')\b')
        self._ready = False

    def execute(self, db, statement, values=()):
        return db.execute(self._tokens.sub(lambda match: self.names.get(match[0], match[0]), statement), values)

    def initialize(self, db):
        for statement in SCHEMA:
            self.execute(db, statement)
        if 'scripts' not in {row['name'] for row in self.execute(db, 'PRAGMA table_info(display_runs)')}:
            self.execute(db, 'ALTER TABLE display_runs ADD COLUMN scripts TEXT')

    @contextmanager
    def connect(self, write=False):
        if not write and not self.path.is_file():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
                if not self._ready:
                    self.path.chmod(0o600)
                    self.initialize(db)
            yield db
            if write:
                db.execute('COMMIT')
                self._ready = True
        except BaseException:
            if write and db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def _schedule(self, db, key):
        return self.execute(db, 'SELECT * FROM display_schedule WHERE owner=? AND collection=? AND scope_key=?', key).fetchone() if db else None

    def _held(self, db, key):
        row = self._schedule(db, key)
        if row is None:
            self.execute(db, 'INSERT INTO display_schedule(owner,collection,scope_key) VALUES(?,?,?)', key)
            row = self._schedule(db, key)
        return row

    def state(self, owner, collection, scope_id, *, now=None):
        """The scope’s schedule as the probe and the tools read it."""
        now = self.clock() if now is None else now
        with self.connect() as db:
            row = self._schedule(db, (owner, collection, scope_id))
        value = dict(row) if row else {}
        policy = self.clean_policy(json.loads(value.get('policy') or '{}'))
        failures = value.get('failures', 0)
        return {'processed_stamp': value.get('processed_stamp'), 'processed_day': value.get('processed_day'),
            'attempt_stamp': value.get('attempt_stamp'), 'failures': failures, 'needs_attention': failures >= ATTENTION_AFTER,
            'next_after': value.get('next_after', 0), 'manual_after': value.get('manual_after', 0),
            'requested_at': value.get('requested_at'), 'requested_only': json.loads(value['requested_only']) if value.get('requested_only') else None,
            'running': bool(value.get('lease_token')) and value.get('lease_until', 0) > now,
            'last_run_id': value.get('last_run_id'), 'last_status': value.get('last_status'),
            'policy': policy, 'policy_revision': value.get('policy_revision', 1)}

    def policy(self, owner, collection, scope_id):
        state = self.state(owner, collection, scope_id)
        return {**state['policy'], 'revision': state['policy_revision']}

    def set_policy(self, owner, collection, scope_id, changes, *, expected_revision):
        """Change the scope’s run policy. `expected_revision` is the revision the caller read."""
        key = (owner, collection, scope_id)
        with self.connect(True) as db:
            row = self._held(db, key)
            if type(expected_revision) is not int or expected_revision != row['policy_revision']:
                raise ValueError('Read the schedule again, then change it with its current revision.')
            policy = self.clean_policy({**json.loads(row['policy'] or '{}'), **changes})
            self.execute(db, 'UPDATE display_schedule SET policy=?,policy_revision=policy_revision+1 WHERE owner=? AND collection=? AND scope_key=?',
                       (canonical(policy), *key))
        return self.policy(*key)

    def request(self, owner, collection, scope_id, *, by, only=None):
        """Ask for a pass now. Requests made before the pass starts become that one pass."""
        key = (owner, collection, scope_id)
        with self.connect(True) as db:
            self._held(db, key)
            self.execute(db, 'UPDATE display_schedule SET requested_at=?,requested_by=?,requested_only=? WHERE owner=? AND collection=? AND scope_key=?',
                       (self.clock(), by, canonical(sorted(set(only))) if only else None, *key))
        return self.state(*key)

    def claim(self, owner, collection, scope_id, *, trigger, stamp, day, now=None, lease_seconds=LEASE_SECONDS):
        """Take the scope’s lease and open a run row. None while another pass holds the lease."""
        if trigger not in PASS_TRIGGERS:
            raise ValueError('Use a known run trigger.')
        now = self.clock() if now is None else now
        key = (owner, collection, scope_id)
        with self.connect(True) as db:
            row = self._held(db, key)
            if row['lease_token'] and row['lease_until'] > now:
                return None
            if row['lease_token']:
                self._expire(db, key, row, now)  # A pass stopped mid-way; this pass does the work again.
                row = self._schedule(db, key)
            manual = trigger == 'manual'
            run_id, token = self.run_prefix + uuid.uuid4().hex, secrets.token_hex(16)
            self.execute(db, 'INSERT INTO display_runs(id,owner,collection,scope_key,trigger,status,started_at,input_stamp,day,requested_by,requested_only) '
                       "VALUES(?,?,?,?,?,'running',?,?,?,?,?)",
                       (run_id, *key, trigger, now, stamp, day, row['requested_by'] if manual else None, row['requested_only'] if manual else None))
            self.execute(db, 'UPDATE display_schedule SET lease_token=?,lease_until=?,lease_run_id=?,last_run_id=?,last_status=? '
                       + (',requested_at=NULL,requested_by=NULL,requested_only=NULL ' if manual else '')
                       + 'WHERE owner=? AND collection=? AND scope_key=?', (token, now + lease_seconds, run_id, run_id, 'running', *key))
            only = json.loads(row['requested_only']) if manual and row['requested_only'] else None
        return {'id': run_id, 'lease_token': token, 'trigger': trigger, 'started_at': now, 'stamp': stamp, 'day': day, 'only': only}

    def _expire(self, db, key, row, now):
        """Close the run of a lease that ran out and free the scope.

        The service's own pass that stopped mid-way counts like a failed one: one more failure, the
        doubling wait, attention after ATTENTION_AFTER in a row. A host command that stopped is its
        operator's to run again, so it counts for nothing.
        """
        run = self.execute(db, "SELECT trigger,input_stamp FROM display_runs WHERE id=? AND status='running'", (row['lease_run_id'],)).fetchone()
        self.execute(db, "UPDATE display_runs SET status='interrupted',finished_at=?,error=? WHERE id=? AND status='running'",
                   (now, INTERRUPTED, row['lease_run_id']))
        changes = {'lease_token': None, 'lease_until': 0, 'lease_run_id': None, 'last_status': 'interrupted'}
        if run is not None and run['trigger'] != 'host':
            failures = row['failures'] + 1
            changes.update(failures=failures, attempt_stamp=run['input_stamp'],
                           next_after=now + min(FAILURE_CEILING_SECONDS, 60 * 2 ** min(failures, 10)))
        self.execute(db, f"UPDATE display_schedule SET {','.join(name + '=?' for name in changes)} WHERE owner=? AND collection=? AND scope_key=?",
                   (*changes.values(), *key))

    def reap(self, owner, collection, scope_id, *, now=None):
        """Close a pass whose lease ran out, so the probe reads the scope as it now stands. True when it closed one."""
        now = self.clock() if now is None else now
        key = (owner, collection, scope_id)
        expired = lambda row: bool(row is not None and row['lease_token'] and row['lease_until'] <= now)
        with self.connect() as db:
            if not expired(self._schedule(db, key)):
                return False
        with self.connect(True) as db:
            row = self._schedule(db, key)
            if not expired(row):
                return False
            self._expire(db, key, row, now)
        return True

    def renew(self, run_id, lease_token, *, lease_seconds, now=None):
        """Extend a lease its holder still has. False once the lease has moved on."""
        now = self.clock() if now is None else now
        with self.connect(True) as db:
            return self.execute(db, 'UPDATE display_schedule SET lease_until=? WHERE lease_token=? AND lease_run_id=?',
                              (now + lease_seconds, lease_token, run_id)).rowcount == 1

    def finish(self, run_id, lease_token, *, status, stamp=None, day=None, period=None, derived=None, outputs=(), errors=(),
               skipped=(), scripts=(), wall_ms=0, error=None, published=False, reread=False, now=None):
        """Close a run row and free its lease. A completed pass that names its `stamp` records that
        stamp and `day` as processed. A failed pass backs off and leaves what was processed as it is.
        `reread` says the run stored outputs of another reading than the scope’s own (a chosen
        day from a host command): the next probe then runs the scope’s whole pass."""
        if status not in {'completed', 'failed', 'budget_exceeded'}:
            raise ValueError('Use a known run status.')
        now = self.clock() if now is None else now
        with self.connect(True) as db:
            run = self.execute(db, 'SELECT * FROM display_runs WHERE id=?', (run_id,)).fetchone()
            if run is None:
                raise ValueError('Choose a run of this display scope.')
            key = (run['owner'], run['collection'], run[self.scope_column])
            row = self._held(db, key)
            if run['status'] != 'running' or row['lease_token'] != lease_token or row['lease_run_id'] != run_id:
                return self._public(run)  # The lease moved on; the pass that holds it owns the scope’s state.
            self.execute(db, 'UPDATE display_runs SET status=?,finished_at=?,period=?,wall_ms=?,derived=?,outputs=?,errors=?,skipped=?,scripts=?,error=?,published=? WHERE id=?',
                (status, now, canonical(period) if period else None, int(wall_ms), canonical(derived) if derived else None,
                 canonical(list(outputs)), canonical(list(errors)), canonical(list(skipped)), canonical(list(scripts)) if scripts else None,
                 error, int(bool(published)), run_id))
            policy = self.clean_policy(json.loads(row['policy'] or '{}'))
            wall = max(0.0, wall_ms / 1000)
            changes = {'lease_token': None, 'lease_until': 0, 'lease_run_id': None, 'last_status': status,
                       'manual_after': now + max(MANUAL_FLOOR_SECONDS, 10 * wall)}
            if status == 'completed' and stamp is not None:
                changes.update(processed_stamp=stamp, processed_day=day or row['processed_day'], attempt_stamp=None, failures=0,
                               next_after=now + max(policy['min_interval_seconds'], 10 * wall))
            elif status != 'completed' and run['trigger'] != 'host':
                failures = row['failures'] + 1
                changes.update(failures=failures, attempt_stamp=stamp or run['input_stamp'],
                               next_after=now + min(FAILURE_CEILING_SECONDS, 60 * 2 ** min(failures, 10)))
            if reread and 'processed_stamp' not in changes:
                changes['processed_stamp'] = None
            self.execute(db, f"UPDATE display_schedule SET {','.join(name + '=?' for name in changes)} WHERE owner=? AND collection=? AND scope_key=?",
                       (*changes.values(), *key))
            # The scope’s passes keep their own number of rows; a script's tests keep theirs (`record`).
            self.execute(db, "DELETE FROM display_runs WHERE owner=? AND collection=? AND scope_key=? AND trigger!='test' AND id NOT IN "
                       "(SELECT id FROM display_runs WHERE owner=? AND collection=? AND scope_key=? AND trigger!='test' "
                       'ORDER BY started_at DESC,rowid DESC LIMIT ?)', (*key, *key, self.keep_runs()))
            run = self.execute(db, 'SELECT * FROM display_runs WHERE id=?', (run_id,)).fetchone()
        return self._public(run)

    def _public(self, row):
        """A run as the tools and the page read it: no principal, no stamp, no file path.

        `scripts` holds one entry per script run of the pass, or the one test of a `test` row:
        the display's key, the script revision, the status, the seconds, the error, whether an
        output was published or proposed, and the wake."""
        read = lambda name, default: json.loads(row[name]) if name in row.keys() and row[name] else default
        return {'id': row['id'], self.scope_field: row[self.scope_column], 'trigger': row['trigger'], 'status': row['status'],
            'started_at': iso(row['started_at']), 'finished_at': iso(row['finished_at']), 'day': row['day'],
            'period': read('period', None), 'wall_ms': row['wall_ms'], 'derived': read('derived', None),
            'outputs': read('outputs', []), 'errors': read('errors', []), 'skipped': read('skipped', []),
            'scripts': read('scripts', []),
            'error': row['error'], 'page_published': bool(row['published']), 'only': read('requested_only', None)}

    def record(self, owner, collection, scope_id, *, trigger, status, started_at, wall_ms=0, period=None, scripts=(), error=None):
        """Write one closed run row beside the passes: a script's test. It holds no lease and
        leaves the scope’s schedule, its failures and its waits as they are. A display keeps
        its newest KEEP_TEST_RUNS test rows, so tests leave the scope’s pass rows in place."""
        if trigger != 'test' or status not in {'completed', 'failed'}:
            raise ValueError('Use a known run trigger and status.')
        key = (owner, collection, scope_id)
        run_id = self.run_prefix + uuid.uuid4().hex
        display = next((entry.get('key') for entry in scripts if isinstance(entry, dict)), None)
        with self.connect(True) as db:
            self.execute(db, 'INSERT INTO display_runs(id,owner,collection,scope_key,trigger,status,started_at,finished_at,input_stamp,period,wall_ms,'
                       "outputs,errors,skipped,scripts,error) VALUES(?,?,?,?,?,?,?,?,'',?,?,'[]','[]','[]',?,?)",
                       (run_id, *key, trigger, status, started_at, self.clock(), canonical(period) if period else None, int(wall_ms),
                        canonical(list(scripts)), error))
            mine = "trigger='test' AND json_extract(scripts,'$[0].key') IS ?"
            self.execute(db, f'DELETE FROM display_runs WHERE owner=? AND collection=? AND scope_key=? AND {mine} AND id NOT IN '
                       f'(SELECT id FROM display_runs WHERE owner=? AND collection=? AND scope_key=? AND {mine} ORDER BY started_at DESC,rowid DESC LIMIT ?)',
                       (*key, display, *key, display, KEEP_TEST_RUNS))
            row = self.execute(db, 'SELECT * FROM display_runs WHERE id=?', (run_id,)).fetchone()
        return self._public(row)

    def forget_display(self, owner, collection, scope_id, key):
        """Take a removed display out of the scope’s run history: its tests go, and each pass row
        loses the display's entries. A display added later under the same key starts with an empty history."""
        where = (owner, collection, scope_id)
        with self.connect() as db:
            if db is None:
                return 0
        changed = 0
        with self.connect(True) as db:
            changed += self.execute(db, "DELETE FROM display_runs WHERE owner=? AND collection=? AND scope_key=? AND trigger='test' "
                                  "AND json_extract(scripts,'$[0].key') IS ?", (*where, key)).rowcount
            for row in self.execute(db, 'SELECT id,outputs,errors,skipped,scripts,requested_only FROM display_runs WHERE owner=? AND collection=? AND scope_key=?', where).fetchall():
                kept = {}
                for name in ('outputs', 'errors', 'skipped', 'scripts'):
                    entries = json.loads(row[name]) if row[name] else []
                    left = [entry for entry in entries if not (isinstance(entry, dict) and entry.get('key') == key)]
                    if len(left) != len(entries):
                        kept[name] = canonical(left) if left or name != 'scripts' else None
                if kept:
                    self.execute(db, f"UPDATE display_runs SET {','.join(name + '=?' for name in kept)} WHERE id=?", (*kept.values(), row['id']))
                    changed += 1
        return changed

    def job_open(self, job, *, owner, collection, scope_id, display_id):
        """Note a sandbox job before the broker hears of it: {'request_id', 'scope'}."""
        with self.connect(True) as db:
            self.execute(db, 'INSERT OR REPLACE INTO display_script_jobs VALUES(?,?,?,?,?,?,?)',
                       (job['request_id'], owner, collection, scope_id, display_id, canonical(job['scope']), self.clock()))

    def job_close(self, job):
        """The broker confirmed the job released."""
        with self.connect(True) as db:
            self.execute(db, 'DELETE FROM display_script_jobs WHERE request_id=?', (job['request_id'],))

    def jobs_pending(self):
        """Sandbox jobs a stopped process left open: [{'request_id', 'scope'}]."""
        with self.connect() as db:
            if db is None:
                return []
            try:
                return [{'request_id': row['request_id'], 'scope': json.loads(row['scope'])}
                        for row in self.execute(db, 'SELECT request_id,scope FROM display_script_jobs ORDER BY opened_at')]
            except sqlite3.OperationalError:
                return []

    def runs(self, owner, collection, scope_id, *, limit=20):
        """The scope’s runs, newest first."""
        if type(limit) is not int or not 1 <= limit <= RUN_PAGE:
            raise ValueError('Choose a bounded number of runs.')
        with self.connect() as db:
            rows = self.execute(db, 'SELECT * FROM display_runs WHERE owner=? AND collection=? AND scope_key=? ORDER BY started_at DESC,rowid DESC LIMIT ?',
                              (owner, collection, scope_id, limit)).fetchall() if db else []
        return [self._public(row) for row in rows]

    def last_run(self, owner, scope_id=None):
        """The owner's newest pass, of one scope when it is named. A script's test is a row of
        the history and stays out of this: it is one display's test, and the scope’s pass stands."""
        query, values = "SELECT * FROM display_runs WHERE owner=? AND trigger!='test'", [owner]
        if scope_id is not None:
            query += ' AND scope_key=?'
            values.append(scope_id)
        with self.connect() as db:
            row = self.execute(db, query + ' ORDER BY started_at DESC,rowid DESC LIMIT 1', values).fetchone() if db else None
        return self._public(row) if row else None


def due_reading(row, stamp, day, *, now, inputs_ready=True, daily_ready=True):
    """One declared input reading through manual, changed-input and daily cadence gates."""
    if row['running']:
        return None
    if row['requested_at'] is not None:
        return ('manual', stamp, day) if now >= row['manual_after'] else None
    policy = row['policy']
    if not policy['automatic'] or now < row['next_after']:
        return None
    if row['needs_attention'] and stamp == row['attempt_stamp']:
        return None
    if stamp != row['processed_stamp']:
        return ('events', stamp, day) if inputs_ready else None
    if day != row['processed_day'] and daily_ready:
        return ('day', stamp, day)
    return None
