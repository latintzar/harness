"""Trackers run by themselves.

A location's trackers recompute when its inputs change, when its local day turns and when someone
asks. This module is that job: the durable record of what was processed, the lease that keeps one
pass per location, the pass itself and the tick the server calls every second.

    probe    every PROBE_SECONDS, for every company environment and every location with installed
             trackers: bring committed observations into the location store, then compare the
             location's input stamp and local day with what the last completed pass recorded
    pass     claim the location's lease, run the trackers (`core_trackers.run.run_trackers`), publish
             the page data, close the run row
    bounds   one pass per location at a time (lease), one sweep at a time in the process, a minimum
             interval between passes, a time budget per pass with its page publish, backoff after
             a failure or a pass that stopped mid-way, a kept number of output revisions, and the
             off switch ROCOTO_TILE_RUNNER=off. A pass is deterministic: it calls no
             model and no provider, so it spends nothing
    faults   each company is swept by itself: a store the service cannot read is that company's
             fault, logged once (`rocoto.video.trackers`) and kept in its service note, while
             every other company gets its pass
    scripts  a company's tracker with a published processing script runs inside the same pass as
             one sandbox job (`tracker_scripts.py`), after the Core Trackers and after the page
             data is published, in a time of its own (`script_phase`). A script job costs the
             pass's time budget, its status and the location's failure count nothing: the Core
             outputs and the page publish as they should, and each script's own row of the page
             follows its run. The pass's run row carries what each script run did; a script's
             test is a row of its own with the trigger `test`

An unattended pass acts as the owner who installed the location's trackers, with the membership
the server itself reads from the access registry (`pass_company`). When that owner's management
of the location or access to its cameras ends, the location keeps its last outputs and its run
row says why.

Storage. The functions under "Storage touchpoints" are the only readers and writers the pass
logic uses: the input stamp, the projection queue, the commit cursor, the installed locations,
the current outputs and their kept revisions, the page revision and the `TrackerRuns` store
(`tracker-runs.sqlite`: schedule, lease, run rows, commit cursor and service note). A second
storage authority replaces those functions and leaves `due`, `run_location` and `tracker_pass`
as they are.
"""
from __future__ import annotations
from retail_settings import value as setting

import asyncio
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from display_runs import DisplayRuns, due_reading

PROBE_SECONDS = 5            # how often the server looks for due locations
LEASE_SECONDS = 300          # a pass that stopped mid-way frees its location after this long
HOST_LEASE_SECONDS = 120     # a host command's lease; the command renews it while it works
HOST_BEAT_SECONDS = 30       # how often a host command renews its lease
HOST_HOLD_SECONDS = 1800     # the longest a host command keeps its location: a whole seed
HOST_WAIT_SECONDS = 90       # a host command waits this long for a running pass
PROJECTION_WAIT_SECONDS = 120
PROJECT_LIMIT = 100          # queued projections one sweep drains per owner
PROJECTION_KINDS = ('project_event', 'project_basket')
COMMIT_PAGE = 500            # committed observations one sweep reads per owner and collection
UNPLACED_KEEP = 5000         # observations per owner and collection that wait for their recording's camera
MANUAL_FLOOR_SECONDS = 5
FAILURE_CEILING_SECONDS = 3600
ATTENTION_AFTER = 5          # failures in a row before a location waits for a change or a request
KEEP_RUNS = 200              # pass rows kept per location
KEEP_TEST_RUNS = 10          # test rows kept per tracker, beside the location's pass rows
SCRIPT_LEASE_MARGIN = 150    # what a pass keeps of its lease past the script time, for the jobs' own waits
SERVICE = 'tracker-service'  # who asks for the pass that runs a script whose job had to wait
KEEP_REVISIONS = 20          # output revisions kept per tracker, beside its current output
RUN_PAGE = 200               # run rows one read returns at most
PASSES_PER_SWEEP = 4
DEFAULT_POLICY = {'automatic': True, 'min_interval_seconds': 60, 'budget_seconds': 60, 'daily_at': None, 'paused': []}
TRIGGERS = ('events', 'day', 'manual', 'host', 'test')
PASS_TRIGGERS = ('events', 'day', 'manual', 'host')   # what starts a pass; `test` is one script's test run
INTERRUPTED = 'The pass stopped before it finished. The next pass runs the same work again.'
OWNER_ACCESS = 'These trackers run as the member who installed them. That member needs management access to this location.'
CAMERA_ACCESS = 'These trackers run as the member who installed them. That member needs access to every camera of this location.'
STORE_FAULT = 'The tracker service met {kind} in this company’s stores. Its trackers run again once the stores read cleanly.'
BUSY = 'A tracker pass of this location is running. Run this command again in a minute.'

# Rows a pass reads and never writes. A row of the counted kinds is only ever added or rewritten,
# and SQLite gives a rewritten row a new rowid, so (count, highest rowid) moves with every change.
# The small kinds a person edits and removes are read whole, and so is the catalog: a pass carries
# a confirmed product's price onto its own row, which rewrites that row with the content it had.
COUNTED_KINDS = ('event', 'tracked_visit', 'item_grounding', 'basket_evidence', 'event_receipt', 'recording_receipt')
READ_KINDS = ('tracker', 'zone', 'location')
# Beside the recipe and its processors, the modules that decide what a pass derives and publishes.
ENGINE_MODULES = ('retail_records.py', 'visit_basket.py', 'catalog_finder.py', 'import_control_room_bundle.py', 'tracker_scripts.py', 'retail_script_signals.py')

log = logging.getLogger('rocoto.video.trackers')

_PASS = threading.Lock()     # one sweep at a time in this process
_PENDING_SINCE = {}          # (state, owner) -> when its projection queue was first seen waiting
_PAGE_DIGESTS = {}           # data.json path -> (file signature, sha256)
_FAULTS = {}                 # (root, environment) -> the fault its last sweep met, logged when it changes


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def iso(epoch):
    return None if epoch is None else datetime.fromtimestamp(round(epoch, 3), timezone.utc).isoformat().replace('+00:00', 'Z')


# ── Storage touchpoints ────────────────────────────────────────────────────────────────────────

@contextmanager
def read_only(path):
    """A read-only connection to a company store, or None while the store has no file yet."""
    path = Path(path)
    if not path.is_file():
        yield None
        return
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


_ENGINE = None


def engine_sources():
    """Every file that decides what a pass computes: the recipe, its processors, the accumulation
    step, the basket reducer, the product types and the page publisher."""
    here = Path(__file__).parent
    core = [path for path in sorted((here / 'core_trackers').glob('*')) if path.suffix in {'.py', '.json'}]
    return core + [here / name for name in ENGINE_MODULES]


def engine_revision(sources=None):
    """The revision of the maintained code this process runs a pass with. A release that changes
    any of it moves every location's stamp, so each location recomputes once."""
    global _ENGINE
    if sources is None and _ENGINE is not None:
        return _ENGINE
    mark = hashlib.sha256()
    for path in engine_sources() if sources is None else sources:
        mark.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    if sources is not None:
        return mark.hexdigest()[:16]
    _ENGINE = mark.hexdigest()[:16]
    return _ENGINE


def installed_locations(state):
    """Every (owner, collection, location) of this company that has installed trackers."""
    with read_only(Path(state) / 'location-intelligence.sqlite') as db:
        if db is None:
            return []
        try:
            # An installation row is keyed by its location, so the location index answers this alone.
            rows = db.execute("SELECT owner,collection,location FROM records WHERE kind='tracker_installation' "
                              'ORDER BY owner,collection,location').fetchall()
        except sqlite3.OperationalError:
            return []
    return [(row['owner'], row['collection'], row['location']) for row in rows]


def input_stamp(state, owner, collection, location_id):
    """A stamp of everything a pass reads for one location. It moves when an input row is added,
    rewritten or removed, when the location's place in the company changes, and with a new revision
    of the maintained processors. What a pass writes itself (derived rows, outputs) stays out."""
    parts = {'engine': engine_revision(), 'counted': [], 'read': None, 'catalog': None, 'scope': 0}
    where = (owner, collection, location_id)
    with read_only(Path(state) / 'location-intelligence.sqlite') as db:
        if db is not None:
            marks = ','.join('?' * len(COUNTED_KINDS))
            parts['counted'] = [list(row) for row in db.execute(
                f'SELECT kind,COUNT(*),MAX(rowid) FROM records WHERE owner=? AND collection=? AND location=? AND kind IN ({marks}) GROUP BY kind ORDER BY kind',
                (*where, *COUNTED_KINDS))]
            for kind in ('sale', 'sale_line'):  # the till's rows; the estimated ones are a pass's own
                parts['counted'].append([kind + ':pos', *db.execute(
                    "SELECT COUNT(*),MAX(rowid) FROM records WHERE owner=? AND collection=? AND location=? AND kind=? AND json_extract(body,'$.basis')='pos'",
                    (*where, kind)).fetchone()])
            catalog = hashlib.sha256()
            for row in db.execute("SELECT id,body FROM records WHERE owner=? AND collection=? AND location=? AND kind='product' "
                                  "AND json_extract(body,'$.provenance.kind') IS NOT 'camera_observation' ORDER BY id", where):
                catalog.update(f"{row['id']}\0{row['body']}\0".encode())
            parts['catalog'] = catalog.hexdigest()
            read = hashlib.sha256()
            marks = ','.join('?' * len(READ_KINDS))
            for row in db.execute(f'SELECT kind,id,body FROM records WHERE owner=? AND collection=? AND location=? AND kind IN ({marks}) ORDER BY kind,id',
                                  (*where, *READ_KINDS)):
                read.update(f"{row['kind']}\0{row['id']}\0{row['body']}\0".encode())
            parts['read'] = read.hexdigest()
    parts['scope'] = scope_revision(state, collection)
    return digest(parts)


def scope_revision(state, collection):
    """The revision of the collection's place graph: its locations, rooms, cameras and members."""
    with read_only(Path(state) / 'business-scope.sqlite') as db:
        row = db.execute('SELECT revision FROM graphs WHERE collection=?', (collection,)).fetchone() if db is not None else None
    return row['revision'] if row else 0


def script_wake_authorizer(selected):
    """Bind the retail script signal's authority to this workspace's current stores."""
    def authorize(definition, wake, *, admit=False):
        from core_trackers import agent_tracker
        from tracker_scripts import script_camera_ids
        from business_scope import ScopeActor
        if (wake.get('provider') != 'tracker_script' or wake.get('kind') != 'event'
                or definition.get('agent_kind') != 'tracker' or definition.get('input_kind') != 'tracker_events'):
            return False
        owner = definition.get('owner_principal_id')
        monitoring = selected.get('monitoring')
        scope_store = selected.get('business_scope')
        if monitoring is None or scope_store is None:
            return False
        tracker = agent_tracker(selected['state'], owner, definition['dataset'], definition['id'])
        if tracker is None or tracker['id'] != wake.get('connection_id'):
            return False
        monitoring.validate_definition(definition)
        source_scope = wake.get('source_scope') or {}
        captured_cameras = source_scope.get('camera_ids')
        if (source_scope.get('location_id') != tracker['location_id']
                or source_scope.get('tracker_generation') != tracker.get('generation_id')
                or not isinstance(captured_cameras, list)
                or any(not isinstance(key, str) for key in captured_cameras)):
            return False
        cameras = sorted(set(script_camera_ids(selected['state'], owner, tracker)) | set(captured_cameras))
        if not monitoring.profile_authorized(owner, {'dataset':definition['dataset'],
                'location_id':tracker['location_id'], 'camera_ids':cameras}):
            return False
        scope = scope_store.chat_scope(ScopeActor(scope_store.company_id, owner), definition['chat_id'], definition['dataset'])
        allowed = (scope['can_query'] and tracker['location_id'] in scope['location_ids']
            and set(cameras) <= set(scope['camera_ids']))
        if allowed and admit:
            # Script words are evidence too: bind their conversation before the model receives them.
            scope_store.admit_history(ScopeActor(scope_store.company_id, owner), definition['chat_id'], [scope],
                {definition['dataset']:scope['source_ids']}, legacy=not scope['configured'])
        return allowed
    return authorize


def location_cameras(state, collection, location_id):
    """Every camera the place graph has at a location: the cameras whose rows a pass reads."""
    with read_only(Path(state) / 'business-scope.sqlite') as db:
        row = db.execute('SELECT body FROM graphs WHERE collection=?', (collection,)).fetchone() if db is not None else None
    return sorted(camera['id'] for camera in (json.loads(row['body']).get('cameras', []) if row else []) if camera.get('location_id') == location_id)


def projection_pending(state, owner):
    """How many of the owner's queued stream projections wait to reach the location store."""
    with read_only(Path(state) / 'stream-ingestion.sqlite') as db:
        if db is None:
            return 0
        return db.execute("SELECT COUNT(*) FROM stream_work work JOIN stream_windows window ON window.id=work.window_id "
            "WHERE work.owner=? AND work.status='pending' AND window.status='committed' AND work.kind IN (?,?)",
            (owner, *PROJECTION_KINDS)).fetchone()[0]


def drain_projections(company, limit=PROJECT_LIMIT):
    """Bring the owner's queued projections into the location store, oldest first; returns how many.

    The stream queue holds three kinds of work per observation. Two are projections, safe to
    repeat, and this reads them by name. The third is the paid image inspection of an item, the
    server's own work: however many of those wait ahead, the projections stay reachable. Each job
    is leased and closed the way `StreamIngestion.drain` leases and closes it.
    """
    from stream_ingestion import StreamIngestion, canonical as stream_canonical
    from visit_basket import VisitBasket
    owner = company.owner
    pipeline, baskets = StreamIngestion(company.state, company.monitoring), VisitBasket(company.locations)
    handlers = {'project_event': lambda event, key: company.locations.ingest_event(owner, event, job_id=key),
                'project_basket': lambda event, key: baskets.admit_model(owner, event['id'], pipeline)}
    done = 0
    for _ in range(limit):
        with closing(pipeline.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT work.id,work.event_id,work.kind FROM stream_work work JOIN stream_windows window ON window.id=work.window_id "
                "WHERE work.owner=? AND work.status='pending' AND window.status='committed' AND work.kind IN (?,?) ORDER BY work.rowid LIMIT 1",
                (owner, *PROJECTION_KINDS)).fetchone()
            if row is None:
                break
            db.execute("UPDATE stream_work SET status='running',lease_until=?,attempts=attempts+1 WHERE id=?", (pipeline.clock() + 120, row['id']))
        try:
            result = stream_canonical(handlers[row['kind']](company.monitoring.get_event(owner, row['event_id']), row['id']))
            status = 'done'
        except Exception:  # The job keeps a payload-free reason, as every job of this queue does.
            result, status = stream_canonical({'reason': 'Processing needs review.'}), 'failed'
        with closing(pipeline.connect()) as db, db:
            db.execute('UPDATE stream_work SET status=?,result=?,lease_until=0 WHERE id=?', (status, result, row['id']))
        done += 1
    return done


def commit_count(state, owner):
    """How many observations the owner has committed. The commit store only ever grows."""
    with read_only(Path(state) / 'monitoring.sqlite') as db:
        if db is None:
            return 0
        return db.execute('SELECT COUNT(*) FROM monitoring_events WHERE owner=?', (owner,)).fetchone()[0]


def committed_after(state, owner, collection, cursor, limit=COMMIT_PAGE):
    """The owner's committed observations of a collection past a cursor, in the order they were
    committed: [(position, observation)]. The position is the commit store's own row number, which
    only grows, so a commit is read once whatever the host clock said when it was made."""
    with read_only(Path(state) / 'monitoring.sqlite') as db:
        if db is None:
            return []
        return [(row['rowid'], json.loads(row['body'])) for row in db.execute(
            'SELECT rowid,body FROM monitoring_events WHERE owner=? AND dataset=? AND rowid>? ORDER BY rowid LIMIT ?',
            (owner, collection, cursor, limit))]


def committed_events(state, owner, event_ids):
    """The owner's committed observations with these ids, in commit order."""
    if not event_ids:
        return []
    with read_only(Path(state) / 'monitoring.sqlite') as db:
        if db is None:
            return []
        marks = ','.join('?' * len(event_ids))
        return [json.loads(row['body']) for row in db.execute(
            f'SELECT body FROM monitoring_events WHERE owner=? AND id IN ({marks}) ORDER BY rowid', (owner, *event_ids))]


def saved_zone(state, owner, location_id):
    """The timezone of the location's saved coordinates, when it has them."""
    with read_only(Path(state) / 'location-intelligence.sqlite') as db:
        row = db.execute("SELECT body FROM records WHERE owner=? AND kind='location' AND id=?", (owner, location_id)).fetchone() if db else None
    return (json.loads(row['body']).get('coordinates') or {}).get('timezone') if row else None


def current_outputs(state, owner, collection=None, location_id=None):
    """The current output of each tracker the owner has: its tile, canvas and revision."""
    query, values = "SELECT body FROM records WHERE owner=? AND kind='tracker_output'", [owner]
    for column, value in (('collection', collection), ('location', location_id)):
        if value is not None:
            query += f' AND {column}=?'
            values.append(value)
    with read_only(Path(state) / 'location-intelligence.sqlite') as db:
        if db is None:
            return []
        return [json.loads(row['body']) for row in db.execute(query + ' ORDER BY id', values)]


def output_cameras(state, owner, collection, location_id):
    """Every camera the location's current outputs read, without their tiles and canvases."""
    with read_only(Path(state) / 'location-intelligence.sqlite') as db:
        if db is None:
            return []
        rows = db.execute("SELECT json_extract(body,'$.camera_ids') FROM records WHERE owner=? AND collection=? AND location=? "
                          "AND kind='tracker_output'", (owner, collection, location_id)).fetchall()
    return sorted({camera for row in rows for camera in json.loads(row[0] or '[]')})


def output_revisions(state, owner, location_id=None):
    """Each current output's tracker key, location and revision, read without its tile and canvas."""
    query, values = ("SELECT json_extract(body,'$.tracker_key') AS key,collection,location,json_extract(body,'$.revision') AS revision "
                     "FROM records WHERE owner=? AND kind='tracker_output'"), [owner]
    if location_id is not None:
        query += ' AND location=?'
        values.append(location_id)
    with read_only(Path(state) / 'location-intelligence.sqlite') as db:
        if db is None:
            return []
        return [dict(row) for row in db.execute(query + ' ORDER BY id', values)]


def prune_output_revisions(company, location_id, keep=None):
    """Keep each tracker's newest output revisions at a location; returns how many rows went.
    The current output is a row of its own and stays whole."""
    keep = KEEP_REVISIONS if keep is None else keep
    owner, where = company.owner, (company.owner, company.collection, location_id)
    with company.locations.connect(True) as db:
        rows = db.execute("SELECT id,json_extract(body,'$.tracker_id') AS tracker,json_extract(body,'$.revision') AS revision FROM records "
                          "WHERE owner=? AND collection=? AND location=? AND kind='tracker_output_revision'", where).fetchall()
        newest = {}
        for row in rows:
            newest[row['tracker']] = max(newest.get(row['tracker'], 0), row['revision'] or 0)
        old = [row['id'] for row in rows if (row['revision'] or 0) <= newest[row['tracker']] - keep]
        db.executemany("DELETE FROM records WHERE owner=? AND kind='tracker_output_revision' AND id=?", [(owner, key) for key in old])
    return len(old)


def page_directory(state, owner):
    from control_room_routes import owner_directory
    return owner_directory(state, owner)


def page_location(state, owner):
    """The location the owner's published page shows, once a host import has published one."""
    try:
        manifest = json.loads((page_directory(state, owner) / 'manifest.json').read_text())
    except (OSError, ValueError):
        return None
    place = manifest.get('location') if isinstance(manifest, dict) else None
    return place.get('id') if isinstance(place, dict) else None


def page_digest(state, owner):
    """The sha256 of the owner's page data as it is published now, read once per file version."""
    path = page_directory(state, owner) / 'data.json'
    try:
        stat = path.stat()
    except OSError:
        return None
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    held = _PAGE_DIGESTS.get(str(path))
    if held is None or held[0] != signature:
        try:
            held = (signature, hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            return None
        if len(_PAGE_DIGESTS) >= 256:
            _PAGE_DIGESTS.clear()
        _PAGE_DIGESTS[str(path)] = held
    return held[1]


def ledger_mark(state):
    """A mark that moves with every write to the company's run ledger: one file stat."""
    try:
        stat = (Path(state) / 'tracker-runs.sqlite').stat()
    except OSError:
        return None
    return f'{stat.st_mtime_ns:x}{stat.st_size:x}'


def clean_policy(value):
    """A location's run policy, checked: the fields `set_tracker_schedule` writes."""
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
        raise ValueError('Give the daily time as HH:MM in the location’s own time.')
    paused = policy['paused']
    if (not isinstance(paused, list) or len(paused) > 100 or len(set(paused)) != len(paused)
            or any(not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', key) for key in paused)):
        raise ValueError('List the paused trackers by their keys.')
    policy['paused'] = sorted(paused)
    return policy


class TrackerRuns(DisplayRuns):
    """What was processed, who holds the lease and what each run did, per location.

    `tracker-runs.sqlite` in the company's state directory. A read creates no file; the first
    write does. Every state change is one `BEGIN IMMEDIATE` transaction, so two processes agree.
    """

    def __init__(self, state, *, clock=time.time):
        super().__init__(state, clock=clock, filename='tracker-runs.sqlite', policy=clean_policy,
            keep_runs=lambda: KEEP_RUNS, scope_field='location_id', run_prefix='trun-', names={
                'display_schedule': 'tracker_schedule', 'display_runs': 'tracker_runs',
                'display_runs_scope': 'tracker_runs_location', 'display_script_jobs': 'tracker_script_jobs',
                'scope_key': 'location', 'display_id': 'tracker_id'})

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
                    db.execute('''CREATE TABLE IF NOT EXISTS tracker_commits(
                        owner TEXT NOT NULL, collection TEXT NOT NULL, commits INTEGER NOT NULL, cursor INTEGER NOT NULL,
                        scope INTEGER NOT NULL, PRIMARY KEY(owner, collection))''')
                    db.execute('''CREATE TABLE IF NOT EXISTS tracker_unplaced(
                        owner TEXT NOT NULL, collection TEXT NOT NULL, event TEXT NOT NULL, recording TEXT NOT NULL,
                        PRIMARY KEY(owner, collection, event))''')
                    db.execute('''CREATE TABLE IF NOT EXISTS tracker_service(
                        id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL, fault TEXT, noted_at REAL NOT NULL)''')
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

    # ── schedule ──


    # ── lease and run rows ──


    # ── sandbox jobs of script runs ──


  # A ledger from before script runs.


    def state(self, owner, collection, location_id, **options):
        return super().state(owner, collection, location_id, **options)

    def policy(self, owner, collection, location_id, **options):
        return super().policy(owner, collection, location_id, **options)

    def set_policy(self, owner, collection, location_id, changes, **options):
        return super().set_policy(owner, collection, location_id, changes, **options)

    def request(self, owner, collection, location_id, **options):
        return super().request(owner, collection, location_id, **options)

    def pending_location_jobs(self, owner, collection, location_id):
        """An admitted signal job retains its location until original cleanup confirms."""
        with self.connect() as db:
            if db is None:
                return False
            try:
                pending = {row['request_id'] for row in self.execute(db,
                    'SELECT request_id FROM display_script_jobs WHERE owner=? AND collection=? AND scope_key=?',
                    (owner, collection, location_id))}
            except sqlite3.OperationalError:
                return False  # A ledger created before script jobs.
        if not pending:
            return False
        with read_only(self.path.parent / 'background-agents.sqlite') as db:
            if db is None:
                return False
            try:
                return any(json.loads(row['body']).get('external_identity', {}).get('request_id') in pending
                    for row in db.execute("SELECT body FROM signal_steps WHERE status IN ('started','uncertain')"))
            except sqlite3.OperationalError:
                return False  # An earlier workspace before shared signal receipts.

    def claim(self, owner, collection, location_id, **options):
        if self.pending_location_jobs(owner, collection, location_id):
            return None
        return super().claim(owner, collection, location_id, **options)

    def finish(self, run_id, lease_token, **options):
        # A timeout cannot release publication custody while the original native job is uncertain.
        with self.connect() as db:
            row = self.execute(db, 'SELECT * FROM display_runs WHERE id=?', (run_id,)).fetchone() if db else None
        if row is not None and self.pending_location_jobs(row['owner'], row['collection'], row[self.scope_column]):
            self.renew(run_id, lease_token, lease_seconds=SCRIPT_LEASE_MARGIN)
            return self._public(row)
        return super().finish(run_id, lease_token, **options)

    def reap(self, owner, collection, location_id, **options):
        return super().reap(owner, collection, location_id, **options)

    def record(self, owner, collection, location_id, **options):
        return super().record(owner, collection, location_id, **options)

    def runs(self, owner, collection, location_id, **options):
        return super().runs(owner, collection, location_id, **options)

    def forget_tracker(self, owner, collection, location_id, key):
        return super().forget_display(owner, collection, location_id, key)

    def job_open(self, job, *, owner, collection, location_id, tracker_id):
        return super().job_open(job, owner=owner, collection=collection, scope_id=location_id, display_id=tracker_id)

    def last_run(self, owner, location_id=None):
        return super().last_run(owner, location_id)

    # ── cursor over the owner's committed observations ──
    def projection(self, owner, collection):
        """Where the reading of the owner's commits stands: how many commits were counted, the
        position of the last one read, the place graph's revision at that reading and how many
        observations wait for their recording's camera."""
        row, waiting = None, 0
        with self.connect() as db:
            if db is not None:
                try:
                    row = db.execute('SELECT commits,cursor,scope FROM tracker_commits WHERE owner=? AND collection=?', (owner, collection)).fetchone()
                    waiting = db.execute('SELECT COUNT(*) FROM tracker_unplaced WHERE owner=? AND collection=?', (owner, collection)).fetchone()[0]
                except sqlite3.OperationalError:
                    pass  # A ledger from before these tables: the first write makes them.
        return {'commits': row['commits'] if row else 0, 'cursor': row['cursor'] if row else 0, 'scope': row['scope'] if row else 0,
                'unplaced': waiting}

    def unplaced(self, owner, collection):
        """The observations that wait for their recording's camera: {observation id: recording id}."""
        with self.connect() as db:
            if db is None:
                return {}
            try:
                return {row['event']: row['recording'] for row in db.execute(
                    'SELECT event,recording FROM tracker_unplaced WHERE owner=? AND collection=? ORDER BY rowid', (owner, collection))}
            except sqlite3.OperationalError:
                return {}

    def set_projection(self, owner, collection, *, commits, cursor, scope, placed=(), unplaced=None):
        """Record one reading of the commits: the cursor, which waiting observations found their
        place and which new ones wait. The newest UNPLACED_KEEP waiting observations are kept."""
        key = (owner, collection)
        with self.connect(True) as db:
            db.execute('INSERT OR REPLACE INTO tracker_commits VALUES(?,?,?,?,?)', (*key, commits, cursor, scope))
            db.executemany('DELETE FROM tracker_unplaced WHERE owner=? AND collection=? AND event=?', [(*key, event) for event in placed])
            db.executemany('INSERT OR IGNORE INTO tracker_unplaced VALUES(?,?,?,?)',
                           [(*key, event, recording) for event, recording in (unplaced or {}).items()])
            db.execute('DELETE FROM tracker_unplaced WHERE owner=? AND collection=? AND rowid NOT IN '
                       '(SELECT rowid FROM tracker_unplaced WHERE owner=? AND collection=? ORDER BY rowid DESC LIMIT ?)', (*key, *key, UNPLACED_KEEP))

    # ── what the service last found at this company ──
    def service(self):
        """The service's note for this company: `on` once a sweep has read it, `off` when the host
        started the service switched off, `waiting` until either; and the fault of the last sweep."""
        row = None
        with self.connect() as db:
            if db is not None:
                try:
                    row = db.execute('SELECT state,fault,noted_at FROM tracker_service WHERE id=1').fetchone()
                except sqlite3.OperationalError:
                    pass
        return {'state': row['state'], 'fault': row['fault'], 'noted_at': iso(row['noted_at'])} if row else {'state': 'waiting', 'fault': None, 'noted_at': None}

    def note_service(self, state, fault=None):
        """Write the service's note when it changes."""
        if state not in {'on', 'off'}:
            raise ValueError('Use a known service state.')
        current = self.service()
        if (current['state'], current['fault']) != (state, fault):
            with self.connect(True) as db:
                db.execute('INSERT OR REPLACE INTO tracker_service VALUES(1,?,?,?)', (state, fault, self.clock()))
        return self.service()


def home_revision(state, owner, location_id=None):
    """What a page polls: the revision of the owner's published page data, the trackers' output
    revisions, the location's schedule as the tools set it and the newest run. The revision moves
    once the new page data is in place."""
    runs = TrackerRuns(state)
    schedule = None
    outputs = output_revisions(state, owner, location_id)
    if location_id is not None and outputs:
        schedule = runs.state(owner, outputs[0]['collection'], location_id)
    run = runs.last_run(owner, location_id)
    return {'schema': 'rocoto.home-revision.v1', 'revision': page_digest(state, owner),
        'day': schedule['processed_day'] if schedule else None,
        'schedule': {'automatic': schedule['policy']['automatic'], 'needs_attention': schedule['needs_attention'],
                     'running': schedule['running'], 'requested': schedule['requested_at'] is not None,
                     'service': runs.service()['state']} if schedule else None,
        'outputs': [{'key': output['key'], 'location_id': output['location'], 'revision': output['revision']} for output in outputs],
        'run': {'status': run['status'], 'trigger': run['trigger'], 'started_at': run['started_at'],
                'finished_at': run['finished_at'], 'error': run['error']} if run else None}


# ── The pass ───────────────────────────────────────────────────────────────────────────────────

def location_name(company, location_id):
    """The location's own name in the company's records."""
    view = company.locations.authorized(company.owner, company.collection, location_id)
    return next((place['name'] for place in view['locations'] if place['id'] == location_id and place.get('name')), 'Location')


def projection_due(runs, state, owner, collection, *, drain=True):
    """Whether a sweep has observations to bring into the location store for this owner and collection."""
    seen = runs.projection(owner, collection)
    return bool((drain and projection_pending(state, owner)) or commit_count(state, owner) != seen['commits']
                or (seen['unplaced'] and scope_revision(state, collection) != seen['scope']))


def project_observations(company, runs, *, drain=True, limit=PROJECT_LIMIT):
    """Bring the owner's committed observations of this collection into the location store.

    Two kinds of commit wait to arrive there. A stream window queues its projections: the server's
    stream tick drains them for a company with an open session, and with `drain` this drains the
    same queue for a company nobody has opened since the restart. An observation that a tool or a
    route committed has no queue entry, so it is read from the commit store itself, in commit
    order. One whose recording has no camera of a location yet waits by name, and is placed once
    the place graph changes and its recording has a camera. All of it is safe to repeat. The paid
    image inspection of an item stays in the server's queue.
    """
    from monitoring import is_reference_event
    owner, state, collection = company.owner, company.state, company.collection
    done = {'queued': 0, 'committed': 0}
    if drain and projection_pending(state, owner):
        done['queued'] = drain_projections(company, limit)
    seen, count, scope = runs.projection(owner, collection), commit_count(state, owner), scope_revision(state, collection)
    again = []
    if seen['unplaced'] and scope != seen['scope']:
        try:
            mapped = set(company.scope.effective(company.actor, collection)['source_ids'])
        except ValueError:
            mapped = set()
        again = [event for event, recording in runs.unplaced(owner, collection).items() if recording in mapped]
    if count == seen['commits'] and not again:
        if scope != seen['scope']:
            runs.set_projection(owner, collection, commits=seen['commits'], cursor=seen['cursor'], scope=scope)
        return done
    rows = committed_after(state, owner, collection, seen['cursor'])
    events = committed_events(state, owner, again[:COMMIT_PAGE]) + [event for _, event in rows if not is_reference_event(event)]
    queued, present = set(), set()
    if events:
        from stream_ingestion import canonical as stream_canonical
        work = {hashlib.sha256(stream_canonical([owner, event['id'], 'project_event']).encode()).hexdigest(): event['id'] for event in events}
        marks = ','.join('?' * len(events))
        with read_only(Path(state) / 'stream-ingestion.sqlite') as db:
            if db is not None:
                queued = {work[row[0]] for row in db.execute(f'SELECT id FROM stream_work WHERE id IN ({marks})', list(work))}
        with read_only(Path(state) / 'location-intelligence.sqlite') as db:
            if db is not None:
                present = {row[0] for row in db.execute(f"SELECT id FROM records WHERE owner=? AND kind='event' AND id IN ({marks})",
                                                        (owner, *[event['id'] for event in events]))}
    placed, waiting = [], {}
    for event in events:
        if event['id'] not in queued and event['id'] not in present:
            try:
                company.locations.ingest_event(owner, event)
                done['committed'] += 1
            except ValueError:
                # A recording with no camera of a location stays an observation of its collection for now.
                waiting[event['id']] = event['recording_id']
                continue
        placed.append(event['id'])
    more = len(rows) >= COMMIT_PAGE or len(again) > COMMIT_PAGE
    runs.set_projection(owner, collection, commits=-1 if more else count, cursor=rows[-1][0] if rows else seen['cursor'],
                        scope=seen['scope'] if len(again) > COMMIT_PAGE else scope, placed=placed, unplaced=waiting)
    return done


def at_daily_time(zone, daily_at, now):
    """Whether the location's daily time has come on its own clock at `now`. A location with no
    daily time has its day from the first moment of it."""
    if not daily_at:
        return True
    from zoneinfo import ZoneInfo
    moment = datetime.fromtimestamp(now, timezone.utc)
    try:
        moment = moment.astimezone(ZoneInfo(zone)) if zone else moment
    except (KeyError, ValueError, OSError):
        pass
    return moment.strftime('%H:%M') >= daily_at


def due(runs, state, owner, collection, location_id, *, now, pending_since=None):
    """Why this location's trackers should run now: (trigger, stamp, day), else None.

    In order: a request (`manual`); new inputs once the projection queue is quiet and the minimum
    interval has passed (`events`); a new local day (`day`). A location with `automatic` off runs on
    request only. After ATTENTION_AFTER failures in a row it waits for new inputs or a request.
    A pass whose lease ran out is closed first and counts among those failures.
    """
    from core_trackers.run import zone_day
    runs.reap(owner, collection, location_id, now=now)
    row = runs.state(owner, collection, location_id, now=now)
    if row['running']:
        return None
    stamp = input_stamp(state, owner, collection, location_id)
    zone = saved_zone(state, owner, location_id)
    day = zone_day(zone, now).isoformat()
    candidate = due_reading(row, stamp, day, now=now, daily_ready=at_daily_time(zone, row['policy']['daily_at'], now))
    if candidate is None or candidate[0] != 'events':
        return candidate
    waiting = _PENDING_SINCE if pending_since is None else pending_since
    mark = (str(state), owner)
    if projection_pending(state, owner):
        if now - waiting.setdefault(mark, now) < PROJECTION_WAIT_SECONDS:
            return None
    else:
        waiting.pop(mark, None)
    return candidate


def pass_access(company, location_id):
    """Confirm that the member a pass acts as manages the location and reaches every camera of it.

    A pass reads the rows of every camera of the location and publishes what they add up to, so it
    acts as a member whose present access covers them all. Raises with the sentence a run row keeps.
    """
    try:
        access = company.locations.authorized(company.owner, company.collection, location_id, write=True)['effective']
    except ValueError:
        raise ValueError(OWNER_ACCESS) from None
    if not access['includes_unassigned'] and not set(location_cameras(company.state, company.collection, location_id)) <= set(access['camera_ids']):
        raise ValueError(CAMERA_ACCESS)


def run_location(company, location_id, runs, *, trigger, stamp, day, budget_seconds=None, custom=None, clock=time.monotonic):
    """One pass of one location, under its lease. Returns the closed run, or None while another pass
    holds the lease.

    The pass confirms that the installing owner still manages the location and reaches every
    camera whose rows it reads, follows the recipe to its current revision, runs the trackers and
    publishes the page data inside the time budget, and keeps the newest output revisions.
    The company's script trackers run after that, in a time of their own (`script_phase`): a
    script job leaves the pass's status, its time budget and the location's failure count alone.

    A location with a daily time has its day's pass at that time. An `events` pass before it
    recomputes the trackers and leaves the day for the `day` pass, which is also where a script
    has its run for the day.
    """
    from core_trackers import install_core_trackers, load_recipe
    from core_trackers.run import BUDGET, TODAY, BudgetReached, brief, run_trackers
    owner, collection, state = company.owner, company.collection, company.state
    claimed = runs.claim(owner, collection, location_id, trigger=trigger, stamp=stamp, day=day)
    if claimed is None:
        return None
    started = clock()
    policy = runs.state(owner, collection, location_id)['policy']
    deadline = started + (budget_seconds if budget_seconds is not None else policy['budget_seconds'])
    # Whether this pass stands for the location's local day: the `day` pass, a requested pass, or
    # a pass on new events once the daily time has come.
    daily = trigger != 'events' or at_daily_time(saved_zone(state, owner, location_id), policy['daily_at'], runs.clock())
    before = {output['tracker_id']: output['revision'] for output in current_outputs(state, owner, collection, location_id)}
    result, status, error, published, passed = None, 'failed', None, False, None
    token = TODAY.set(date.fromisoformat(day))
    try:
        pass_access(company, location_id)
        # Core coverage follows the maintained recipe: a newer revision is installed before the run.
        installed = next((row for row in company.locations._rows(owner, 'tracker_installation')
                          if row['id'] == location_id and row['collection'] == collection), None)
        if installed and installed.get('recipe_revision') != load_recipe(installed['business_type'])['revision']:
            install_core_trackers(company, location_id, installed['business_type'])
            stamp = input_stamp(state, owner, collection, location_id)
        only = claimed['only']
        if only is None and trigger != 'manual' and policy['paused']:
            from core_trackers import installed_trackers
            only = [t['key'] for t in installed_trackers(company, location_id) if t['core'] or t['key'] not in policy['paused']]
        result = run_trackers(company, location_id, only=only, deadline=deadline, custom=custom, clock=clock, defer=True)
        status = result['status']
        if status != 'budget_exceeded' and clock() >= deadline:
            status = 'budget_exceeded'
        if status != 'budget_exceeded' and page_location(state, owner) == location_id:
            # The page data refreshes where a host import has published this location's page. Its week,
            # month and year readings count against the same budget and stop before anything is written.
            from import_control_room_bundle import publish_core_trackers
            budget = BUDGET.set((clock, deadline))
            try:
                publish_core_trackers(company, location_id, location_name=location_name(company, location_id))
                published = True
            except BudgetReached:
                status = 'budget_exceeded'
            finally:
                BUDGET.reset(budget)
        if status == 'failed':
            error = 'A Core Tracker stopped. Its last output stays in place.'
        elif status == 'budget_exceeded':
            error = 'The pass reached its time budget. The page keeps its last data until the next pass.'
        passed = clock() - started  # The pass's own work ends here; the script jobs keep their own time.
        if result.get('deferred') and status != 'budget_exceeded':
            try:
                script_phase(company, location_id, runs, claimed, result, custom, trigger=trigger, day=day, daily=daily,
                             published=published, clock=clock)
            except Exception as exc:  # The pass's own work stands; each script that has no entry yet carries the reason.
                ran = {entry.get('key') for entry in result['scripts']}
                result['errors'] = [*result['errors'], *[{'key': tracker['key'], 'core': False, 'error': brief(exc)}
                                                         for tracker in result['deferred'] if tracker['key'] not in ran]]
    except Exception as exc:  # The run row carries the reason; the location keeps its last outputs.
        status, error = 'failed', brief(exc, 'pass')
    finally:
        TODAY.reset(token)
    if result and result.get('outputs'):
        try:
            prune_output_revisions(company, location_id)
        except Exception:
            pass  # The next pass keeps the same number.
    # A pass over a chosen few trackers leaves the location's whole pass to the next probe.
    whole = claimed['only'] is None
    rows = []
    for output in (result or {}).get('outputs', []):
        tile = output.get('tile') or {}
        rows.append({'key': output.get('tracker_key') or tile.get('key'), 'tracker_id': output.get('tracker_id'), 'core': output.get('core'),
            'revision': output.get('revision'), 'changed': before.get(output.get('tracker_id')) != output.get('revision'),
            'state': tile.get('state')})
    return runs.finish(claimed['id'], claimed['lease_token'], status=status, stamp=stamp if whole or status != 'completed' else None,
        day=day if daily else None, period=(result or {}).get('period'), derived=(result or {}).get('derived'), outputs=rows,
        errors=[{'key': e['key'], 'error': e['error']} for e in (result or {}).get('errors', [])],
        skipped=(result or {}).get('skipped', []), scripts=(result or {}).get('scripts', []),
        wall_ms=round((clock() - started if passed is None else passed) * 1000), error=error, published=published)


def script_phase(company, location_id, runs, claimed, result, custom, *, trigger, day, daily, published, clock=time.monotonic):
    """The script trackers of a pass, after its own work: its Core Trackers have run and its page
    data is published.

    Each script tracker is one sandbox job. The jobs of a pass share `SCRIPT_PASS_SECONDS`, a time
    beside the pass's budget, and the lease is renewed to cover it. What they did joins the pass's
    result: outputs, errors and one `scripts` entry each. A script that published or refreshed its
    readings has its own row of the page assembled again, so the page follows without a second
    whole publish. A script whose job had to wait (`retry`) gets one more pass, which the tracker
    service asks for here, for those trackers alone.
    """
    from core_trackers.run import PASS, run_script_trackers
    import tracker_scripts
    owner, collection = company.owner, company.collection
    if not runs.renew(claimed['id'], claimed['lease_token'], lease_seconds=tracker_scripts.SCRIPT_PASS_SECONDS + SCRIPT_LEASE_MARGIN):
        return  # The lease moved on; the pass that holds it runs the scripts.
    within = PASS.set({'trigger': trigger, 'run_id': claimed['id'], 'lease_token': claimed['lease_token'], 'clock': clock, 'day': day, 'daily': daily,
                       'deadline': clock() + tracker_scripts.SCRIPT_PASS_SECONDS})
    try:
        done = run_script_trackers(company, location_id, result['period'], result['data'], result['deferred'], custom)
    finally:
        PASS.reset(within)
    for name in ('outputs', 'errors', 'scripts'):
        result[name] = [*result[name], *done[name]]
    if published:
        by_key = {tracker['key']: tracker for tracker in result['deferred']}
        for entry in done['scripts']:
            if entry.get('status') == 'succeeded' and entry.get('key') in by_key:
                tracker_scripts.show_on_page(company, by_key[entry['key']])
    owed = sorted(entry['key'] for entry in done['scripts'] if entry.get('retry'))
    if owed and runs.state(owner, collection, location_id)['requested_at'] is None:
        runs.request(owner, collection, location_id, by=SERVICE, only=owed)


class HostPass:
    """What a host command tells the lease it holds: which inputs its run covers and how it ended."""

    def __init__(self, company, location_id, stamp):
        self.company, self.location_id, self.stamp = company, location_id, stamp
        self.result, self.published, self.covers = None, False, False

    def mark(self):
        """Call right before the run: the inputs present now are the ones this run reads."""
        self.stamp = input_stamp(self.company.state, self.company.owner, self.company.collection, self.location_id)

    def done(self, result, page=None, *, covers_inputs=True):
        """The run's result. `covers_inputs` says the run was the location's whole pass with its page."""
        self.result, self.published = result, page is not None
        self.covers = bool(covers_inputs and page is not None and result.get('status', 'completed') == 'completed')


@contextmanager
def host_pass(company, location_id, *, runs=None, wait_seconds=HOST_WAIT_SECONDS, lease_seconds=HOST_LEASE_SECONDS,
              beat_seconds=HOST_BEAT_SECONDS, hold_seconds=HOST_HOLD_SECONDS, clock=time.time, sleep=time.sleep):
    """The lease a host command holds while it derives, runs or publishes a location.

    The service's own pass and every host command share it, so one of them at a time rebuilds the
    location's derived rows. The command waits for a running pass, then for `wait_seconds` at most.
    Its lease is short and the command renews it every `beat_seconds` while it works, for
    `hold_seconds` at most: a command that is killed stops renewing, and the location is free again
    within `lease_seconds`. `company` is any handle with `state`, `owner` and `collection`.
    """
    from core_trackers.run import TODAY, brief, zone_day
    runs = runs or TrackerRuns(company.state, clock=clock)
    key = (company.owner, company.collection, location_id)
    waited = 0
    while True:
        stamp = input_stamp(company.state, *key)
        day = zone_day(saved_zone(company.state, company.owner, location_id), clock()).isoformat()
        claimed = runs.claim(*key, trigger='host', stamp=stamp, day=day, lease_seconds=lease_seconds)
        if claimed is not None:
            break
        if waited >= wait_seconds:
            raise ValueError(BUSY)
        sleep(1)
        waited += 1
    held, started, working = HostPass(company, location_id, stamp), time.monotonic(), threading.Event()

    def renew():
        until = time.monotonic() + hold_seconds
        while not working.wait(beat_seconds) and time.monotonic() < until:
            try:
                if not runs.renew(claimed['id'], claimed['lease_token'], lease_seconds=lease_seconds):
                    return  # The lease moved on; the pass that holds it owns the location's state.
            except sqlite3.Error:
                continue  # The next beat renews.

    beat = threading.Thread(target=renew, name='tracker-host-lease', daemon=True)
    beat.start()
    token = TODAY.set(date.fromisoformat(day))
    wall = lambda: round((time.monotonic() - started) * 1000)
    try:
        try:
            yield held
        finally:
            working.set()
            beat.join(5)
            if held.result and held.result.get('outputs'):
                try:
                    prune_output_revisions(company, location_id)
                except Exception:
                    pass  # The next pass keeps the same number.
    except BaseException as exc:
        runs.finish(claimed['id'], claimed['lease_token'], status='failed', wall_ms=wall(),
                    error=brief(exc, 'command') if isinstance(exc, Exception) else INTERRUPTED)
        raise
    else:
        result = held.result or {}
        # Outputs stored by a run that was another reading than the location's own (a chosen day, a
        # chosen few) leave the location's whole pass to the service: its next probe runs it.
        runs.finish(claimed['id'], claimed['lease_token'], status=result.get('status', 'completed'),
            stamp=held.stamp if held.covers else None, day=day if held.covers else None, period=result.get('period'),
            derived=result.get('derived'), errors=[{'key': e['key'], 'error': e['error']} for e in result.get('errors', [])],
            skipped=result.get('skipped', []), wall_ms=wall(), published=held.published,
            reread=not held.covers and bool(result.get('outputs')),
            outputs=[{'key': o.get('tracker_key'), 'tracker_id': o.get('tracker_id'), 'core': o.get('core'),
                      'revision': o.get('revision'), 'state': (o.get('tile') or {}).get('state')} for o in result.get('outputs', [])])
    finally:
        TODAY.reset(token)


def pass_company(state, access_state, environment, owner, collection):
    """The handle an unattended pass acts through: the installing owner, with the membership the
    server itself reads. A member is a person of the access registry who is an owner or operator
    of this company; anyone else is no member, whatever one leftover row says."""
    import retail_records as records
    access = Path(access_state) / 'access.sqlite'
    if not access.is_file():
        raise ValueError('Use the company access registry.')

    def member(principal):
        with closing(sqlite3.connect(access.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            row = db.execute('SELECT e.role FROM principals p JOIN principal_environments e ON e.principal_id=p.id '
                             'WHERE p.id=? AND e.environment=?', (principal, environment)).fetchone()
        return {'id': principal, 'role': row[0]} if row and row[0] in {'owner', 'operator'} else None

    return records.open_company(state, owner, collection, member=member)


def environments(access_state):
    """Every company environment in the access registry."""
    with read_only(Path(access_state) / 'access.sqlite') as db:
        if db is None:
            return []
        try:
            return [row[0] for row in db.execute('SELECT id FROM environments ORDER BY id')]
        except sqlite3.OperationalError:
            return []


def environment_state(root, environment):
    """The state directory of one company environment."""
    return Path(root) if environment == 'demo' else Path(root) / 'environments' / environment


def note_fault(root, environment, runs, error):
    """One company's stores stopped its sweep. The fault is logged once, where an operator reads
    it, and kept in that company's service note, where its tools read it."""
    text = STORE_FAULT.format(kind=type(error).__name__)
    if _FAULTS.get((str(root), environment)) != text:
        _FAULTS[(str(root), environment)] = text
        log.warning('tracker runner: company %s is skipped: %s: %s', environment, type(error).__name__, ' '.join(str(error).split())[:200])
    try:
        runs.note_service('on', text)
    except (sqlite3.Error, OSError):
        pass  # The log line stands; the note is written once this company's directory takes a write.


def note_swept(root, environment, runs):
    """One company was read whole: its service note says `on`, and an earlier fault is over."""
    runs.note_service('on')
    if _FAULTS.pop((str(root), environment), None) is not None:
        log.warning('tracker runner: company %s reads cleanly again', environment)


def tracker_pass(root, access_state, *, clock=time.time, custom=None, limit=PASSES_PER_SWEEP, stop=None, pending_since=None,
                 served=frozenset()):
    """One sweep over every company environment: project, probe, and run what is due.

    Synchronous; the server calls it in a thread. It needs no open session and no loaded workspace:
    environments come from the access registry and each company is opened as the pass acts in it
    (`pass_company`). `served` names the environments whose open workspace drains its own stream
    queue. Each company is swept by itself: a store that cannot be read is noted for that company
    (`note_fault`) and the sweep goes on to the next. Returns the runs this sweep closed.
    """
    from core_trackers.run import brief
    if not _PASS.acquire(blocking=False):
        return []
    try:
        candidates = []
        for environment in environments(access_state):
            state = environment_state(root, environment)
            runs, opened, projected, found = TrackerRuns(state, clock=clock), {}, set(), []
            def company(owner, collection, environment=environment, state=state, opened=opened):
                if (owner, collection) not in opened:
                    opened[(owner, collection)] = pass_company(state, access_state, environment, owner, collection)
                return opened[(owner, collection)]
            try:
                places = installed_locations(state)
                for owner, collection, location_id in places:
                    if stop is not None and stop.is_set():
                        return []
                    if (owner, collection) not in projected:
                        projected.add((owner, collection))
                        drain = environment not in served
                        try:
                            if projection_due(runs, state, owner, collection, drain=drain):
                                project_observations(company(owner, collection), runs, drain=drain)
                        except (sqlite3.Error, OSError):
                            raise
                        except Exception:
                            pass  # The next sweep reads the same queue again.
                    try:
                        reason = due(runs, state, owner, collection, location_id, now=clock(), pending_since=pending_since)
                    except (sqlite3.Error, OSError):
                        raise
                    except Exception:
                        continue
                    if reason:
                        waited = runs.state(owner, collection, location_id)
                        found.append((reason[0] != 'manual', waited['next_after'], owner, collection, location_id, reason, runs, company))
                if places or (str(root), environment) in _FAULTS:
                    note_swept(root, environment, runs)
            except (sqlite3.Error, OSError) as exc:
                note_fault(root, environment, runs, exc)
                continue
            candidates.extend(found)
        results = []
        for _, _, owner, collection, location_id, (trigger, stamp, day), runs, company in sorted(candidates, key=lambda c: c[:5])[:limit]:
            if stop is not None and stop.is_set():
                break
            try:
                handle = company(owner, collection)
            except Exception as exc:
                claimed = runs.claim(owner, collection, location_id, trigger=trigger, stamp=stamp, day=day)
                if claimed:
                    results.append(runs.finish(claimed['id'], claimed['lease_token'], status='failed', stamp=stamp, error=brief(exc, 'pass')))
                continue
            try:
                run = run_location(handle, location_id, runs, trigger=trigger, stamp=stamp, day=day, custom=custom)
            except Exception:
                continue  # A lease left behind frees itself; the other locations still run.
            if run:
                results.append(run)
        return results
    finally:
        _PASS.release()


# ── The server's hooks ─────────────────────────────────────────────────────────────────────────

def runner_state(root, access_state, environ=None):
    """The server's handle on this job, made once at startup. ROCOTO_TILE_RUNNER=off stops it,
    and each company with trackers then reads `off` in its service note, so a tool that takes a
    request says the request waits."""
    value = setting('ROCOTO_TILE_RUNNER', 'on', environ=environ).strip().lower()
    if value not in {'on', 'off'}:
        raise ValueError('Set ROCOTO_TILE_RUNNER to on or off.')
    if value == 'off':
        try:
            names = environments(access_state)
        except (sqlite3.Error, OSError):
            names = []
        for environment in names:
            state = environment_state(root, environment)
            try:
                if (state / 'tracker-runs.sqlite').is_file() or installed_locations(state):
                    TrackerRuns(state).note_service('off')
            except (sqlite3.Error, OSError):
                continue  # A company whose stores need repair reads its own fault once the service runs.
    from tracker_scripts import switches
    return {'root': Path(root), 'access_state': Path(access_state), 'enabled': value == 'on', 'task': None,
            'next_probe': 0.0, 'stop': threading.Event(), 'custom': None, 'fault': None,
            # The host's two switches of script runs, read once: the sandbox, and runs on new events.
            'scripts': switches(environ), 'recovery': None}


async def tracker_start(app_state, *, sandbox=None):
    """Called once on the server's event loop as it starts: script trackers join the passes.

    A pass runs in a thread and a sandbox job runs on this loop, so the runner's `custom` callback
    is made here, with the loop. Sandbox jobs a stopped process left open are cleared in the
    background: each company's ledger names them, and the broker confirms each release.
    """
    runner = getattr(app_state, 'tracker_runner', None)
    if not runner:
        return None
    import tracker_scripts
    chosen = runner.get('scripts') or {'sandbox': False, 'event_runs': False}
    # The count of script jobs is kept in the service's state root, so a restart keeps the host's numbers.
    box = tracker_scripts.configure(loop=asyncio.get_running_loop(), sandbox=sandbox or tracker_scripts.Sandbox(
        enabled=chosen['sandbox'], event_runs=chosen['event_runs'],
        counts=tracker_scripts.JobCounts(Path(runner['root']) / tracker_scripts.COUNTS_FILE)))
    if runner['enabled']:
        from retail_script_signals import processor
        runner['custom'] = processor(app_state, box)
    if box.enabled:
        runner['recovery'] = asyncio.create_task(recover_script_jobs(runner['root'], runner['access_state'], box))
        runner['recovery'].add_done_callback(lambda task: task.cancelled() or task.exception())
    return box


async def recover_script_jobs(root, access_state, box):
    """Clear the sandbox jobs each company's ledger still lists as open. Returns how many were cleared."""
    cleared = 0
    try:
        names = await asyncio.to_thread(environments, access_state)
    except (sqlite3.Error, OSError):
        return 0
    for environment in names:
        ledger = TrackerRuns(environment_state(root, environment))
        try:
            pending = await asyncio.to_thread(ledger.jobs_pending)
        except (sqlite3.Error, OSError):
            continue
        if pending:
            cleared += len(await box.recover(pending, closed=ledger.job_close))
    return cleared


async def tracker_tick(app_state):
    """Called every second by the server loop: one sweep at a time, a new one every PROBE_SECONDS."""
    runner = getattr(app_state, 'tracker_runner', None)
    if not runner or not runner['enabled']:
        return
    task = runner['task']
    if task is not None:
        if not task.done():
            return
        runner['task'] = None
        if not task.cancelled():
            # Each pass's reason is in its durable run row. A sweep that stopped as a whole is logged, once per fault.
            error = task.exception()
            fault = None if error is None else f'{type(error).__name__}: {" ".join(str(error).split())[:200]}'
            if fault and fault != runner.get('fault'):
                log.error('tracker runner: the sweep stopped: %s', fault)
            runner['fault'] = fault
    if time.monotonic() < runner['next_probe']:
        return
    runner['next_probe'] = time.monotonic() + PROBE_SECONDS
    for selected in getattr(app_state, 'workspaces', {}).values():
        adapter = selected.get('script_signals')
        if adapter is not None:
            await adapter.recover_pending()
    runner['task'] = asyncio.create_task(asyncio.to_thread(
        tracker_pass, runner['root'], runner['access_state'], custom=runner['custom'], stop=runner['stop'],
        served=frozenset(getattr(app_state, 'workspaces', None) or ())))


async def tracker_stop(app_state):
    """At shutdown: ask the sweep to stop between locations and wait for the pass in hand to close its run row."""
    runner = getattr(app_state, 'tracker_runner', None)
    if not runner:
        return
    runner['enabled'] = False
    runner['stop'].set()
    task, runner['task'] = runner['task'], None
    if task is not None:
        try:
            await task
        except (Exception, asyncio.CancelledError):
            pass
    recovery, runner['recovery'] = runner.get('recovery'), None
    if recovery is not None and not recovery.done():
        recovery.cancel()
        try:
            await recovery
        except (Exception, asyncio.CancelledError):
            pass
