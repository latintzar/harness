"""A tracker's processing script: the harness writes it, the sandbox runs it, the runner publishes it.

A company's tracker computes its own tile and canvas from one Python file its agent writes.

    save      `save_tracker_script` stores the source as a numbered draft revision
    test      `test_tracker_script` runs that revision in the sandbox over the location's present
              data and returns the tile and canvas it would publish, with every error
    publish   `publish_tracker_script` makes a revision with a passing test the one that runs
    run       `retail_script_signals.py` runs each published script as a shared signal step inside
              the location pass: one sandbox job computes the day, week, month and year
    output    a validated output is published through `core_trackers.common.publish`, the one
              writer of every tracker's output, or kept as a proposal when the agent chose review
    wake      the same signal delivers to the linked agent when the evaluated wake policy selects it

Bounds. The script's code is text until it reaches the sandbox: this process parses it (`ast`) and
never imports or executes it. The job has no network grant and no credential; its one mounted
socket answers 403. Input is JSON this host prepared. Output is validated before anything is
stored: reserved names, ids outside the input, sizes and shapes are refused, and every evidence
row is rebuilt from the host's own record. A run that stops leaves the last published output in
place; three in a row pause the script. One script job runs at a time in this service, each
company in its turn, inside a number of jobs per tracker, per company and for the host.

Words. A script writes the words of its exception, its summary and its key names. Those words stay
where an output is read with its cameras: the test's own answer, the script's health in
`get_tracker_script` and the wake to the tracker's agent. A run row carries the code, the line and
a sentence of this host.

The sandbox is the commissioned custom executor, unchanged: this module drives its broker
operations with a bundle of its own (`snapshot_for`). CUSTOM-ADAPTERS.md holds the contract an
author reads; `TRACKER_SCRIPT_INSTRUCTIONS` is the same contract as `get_tracker_script` returns it.
"""
from __future__ import annotations
from retail_settings import value as setting

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

KIND = 'tracker-python-v1'
ENTRY, SCRIPT, TOOL = 'rocoto_entry.py', 'tracker.py', 'run_tracker'
SCRIPT_BYTES = 64 * 1024
PART_BYTES = 120 * 1024           # one input file, under the sandbox's 128 KiB per file
INPUT_BYTES = 840 * 1024          # seven input files; with the script and the entry, under 1 MiB of source
OUTPUT_BYTES = 112 * 1024         # one period's output
ENVELOPE_BYTES = 480 * 1024       # the four outputs with the log, under the sandbox's 512 KiB result
RUN_SECONDS = 30                  # the entry's alarm; the sandbox ends the process at 35 s and the job at 45 s
MEMORY_MB = 384
MAX_PERIODS = 4
JOB_SECONDS = 75                  # how long this host waits for one job before it stops it
BUSY_WAITS = (2, 5, 10)           # waits between tries while both sandbox workers are in use
SLOT_WAIT_SECONDS = 5             # a pass waits this long for this service's one sandbox turn
TEST_SLOT_WAIT_SECONDS = 20       # a test, which a person's turn waits on
TURN_SECONDS = 120                # a company that gave up waiting keeps the next turn this long
SCRIPT_PASS_SECONDS = 90          # the script jobs of one pass together, beside the pass's own time budget
MIN_JOB_SECONDS = RUN_SECONDS + 20  # a job of a pass starts while this much of that time is left
RETRIES_PER_DAY = 3               # passes the tracker service asks for by itself, per script and local day
HOST_GRACE_SECONDS = 30           # what a pass gives the sandbox past a job's own time, and again to stop it
PAUSE_AFTER = 3                   # stopped runs in a row before a script pauses
KEEP_REVISIONS = 50
# Sandbox jobs, runs and tests together: per tracker and UTC day, and per company and for this host
# in any 24 hours. The first number of each pair holds while the installed broker keeps every job
# row for good (2,048 in all, connector reads included). The second holds once the host has event
# runs switched on, which follows the broker's retention upgrade: a released row then leaves after
# one day, so 1,200 in 24 hours stays under the 2,048 rows the broker keeps.
JOBS_PER_DAY = 8
JOBS_PER_DAY_EVENTS = 480
COMPANY_JOBS_PER_DAY = 24
COMPANY_JOBS_PER_DAY_EVENTS = 600
HOST_JOBS_PER_DAY = 48
HOST_JOBS_PER_DAY_EVENTS = 1200
HOST_JOBS_BEFORE_RETENTION = 1024  # script jobs this host takes in all before the upgrade: half the broker's rows
NUMBER_LIMIT = 2 ** 53            # a number an output carries stays inside what a page holds exactly
SOCKET_OPEN, SOCKET_TOTAL = 4, 64  # connections a job's socket holds at once, and takes in all
TEXT_SLOTS = 8
TEXT_CHARS = 4000
FACTS_BYTES = 4 * 1024
ROWS = {'events': 2000, 'visits': 2000, 'items': 2000, 'baskets': 1000, 'sales': 1000, 'sale_lines': 4000,
        'shelf_states': 500, 'queue_intervals': 1000, 'catalog': 1000, 'cameras': 100, 'zones': 200}
# Names the host stamps on a tile or a canvas, and names the page reads on a company's tile: its
# agent, its share, its mark and number format, its currency and the basis of its value. A key of
# the script's own takes another name.
RESERVED = frozenset({'id', 'display_id', 'owner_agent_id', 'owner_principal_id', 'key', 'label', 'family', 'tile_family', 'canvas_family', 'value', 'unit', 'state', 'needs', 'period',
    'periods', 'summary', 'series', 'history', 'trend', 'events', 'revision', 'script_revision', 'company_id', 'location_id',
    'camera_ids', 'tracker_id', 'core', 'chat', 'locked', 'texts',
    'agent_id', 'fraction', 'kind', 'mark', 'view', 'text', 'currency', 'basis', 'value_basis', 'price_basis', 'estimated',
    'presentation', 'presentation_revision', 'problems'})
# Fields a page reads as a picture, a clip or a link. They hold host values at every depth, and so
# does every name that ends in `_url`.
LINKS = frozenset({'thumb', 'thumbnail', 'thumbnail_url', 'href', 'media', 'media_url', 'evidence_url', 'url', 'src', 'link',
    'image', 'image_url', 'poster', 'video', 'object_crop_url'})
# Fields that make an object an evidence row: its identity, its recording and its place in it.
# Rows live in `events` and `canvas.rows`, where the host rebuilds each from its own record.
ROW_FIELDS = frozenset({'id', 'event_key', 'source_id', 'source', 'camera_id', 'visitor_id', 'visit_id', 'person_id', 'at_s', 'end_s'})
# Provenance the host keeps for itself, at every depth of an input row.
INTERNAL = frozenset({'answer_file', 'answer_files', 'data_basis', 'provenance'})
# The codes the entry file gives a script that stopped. A run row names one of these.
SCRIPT_CODES = frozenset({'script_syntax', 'script_contract', 'script_error', 'time_limit', 'memory_limit', 'output_too_large', 'output_encoding'})
OUTPUT_KEYS = ('value', 'unit', 'state', 'needs', 'summary', 'series', 'events', 'canvas')
OPTIONAL_KEYS = ('trend', 'tile_extra', 'texts')
PUBLISH_MODES = ('automatic', 'review')
WAKE_MODES = ('never', 'on_failure', 'on_change', 'every_run')
DEFAULT_POLICY = {'publish': 'automatic', 'wake': 'on_failure'}
NAME = re.compile(r'[a-z][a-z0-9_]{0,39}')
SANDBOX_SWITCH = 'ROCOTO_CUSTOM_EXECUTOR_ENABLED'
EVENT_RUNS_SWITCH = 'ROCOTO_TILE_SCRIPT_EVENT_RUNS'

READ_NAMES = frozenset({'get_tracker_script'})
WRITE_NAMES = frozenset({'save_tracker_script', 'test_tracker_script', 'publish_tracker_script', 'set_tracker_script_policy',
                         'approve_tracker_output', 'write_tracker_text'})
# Writes this family answers on the event loop: the dispatcher awaits `acall` for these.
ASYNC_NAMES = frozenset({'test_tracker_script'})

UNKNOWN = 'Choose a tracker from list_trackers.'
MANAGEMENT = 'Use location management access to change this tracker’s script.'
LINK_MANAGEMENT = 'Use location management access to add a tracker at this location.'
INSTALL_FIRST = 'Trackers run at a location once its Core Trackers are installed. Install them from the Organization conversation.'
SAVE_FIRST = 'Save a script for this tracker first with save_tracker_script.'
READ_AGAIN = 'Read the script again with get_tracker_script, then use its current revision.'
TEST_FIRST = 'Test this revision with test_tracker_script. A revision with a passing test is the one publish_tracker_script takes.'
LOCATION_NEEDED = 'Name the location this Tracking Agent tracks with location_id; list_trackers shows each location’s id.'
PROPOSAL_MOVED = 'Read get_tracker_script again: the proposal to approve is the one it shows now.'
TEXT_MOVED = 'This text belongs to an earlier output. Read get_tracker_script for the slots of the current one.'
TEXT_FOR = ('Name what this text is written for: output_revision for a published output, proposal_id for a waiting proposal. '
            'get_tracker_script and the wake show both.')
PAGE_SHOWS = 'The page shows this tracker’s row as it now stands.'
PAGE_AFTER_PASS = 'The page shows it after the next pass of this location.'
FULL = 'This location holds its full set of own trackers. Remove one to add another.'
EVENT_RUNS_NOTE = 'Runs on new events are switched off on this host.'
EVENT_RUNS_WAIT = ('The script ran for this local day. Runs on new events are switched off on this host: its next run is '
                   'the next local day’s, or one on request.')
DAILY_TIME_WAIT = 'The script’s run for this local day starts with the location’s daily pass, at its daily time.'
BUDGET_WAIT = 'The script jobs of this pass used their time together. This script’s job waits for another pass.'
RETRY_SOON = 'The tracker service asks for that pass within minutes.'
RETRY_NEXT = 'It runs again in the location’s next pass.'
DAILY_LIMIT = 'This tracker reached its sandbox jobs for today. Its next run is tomorrow, UTC.'
PAUSED = ('The script is paused after {count} stopped runs in a row. The last one: {message} '
          'A passing test of the published revision, or publishing a corrected revision, starts it again.')
HOST_RUN = 'A host command runs the Core Trackers. The script runs in the service’s own passes.'
NO_AGENT = 'A wake goes to the tracker’s Tracking Agent. This tracker is managed from a conversation.'
CAMERA_SCOPE = 'This script’s output, proposal and messages read cameras beyond your current access.'
SEE_MESSAGE = 'get_tracker_script shows the message.'
UNLINKED = ('This Tracking Agent gets its tracker record with its first script: save it with save_tracker_script and '
            'expected_revision 0, and name the location with location_id where list_trackers shows several.')
FAULTS = {
    'sandbox_offline': 'The sandbox of this host is being commissioned. Script runs start once it is ready.',
    'sandbox_busy': 'Both sandbox workers of this host are in use.',
    'sandbox_turn': 'Another script job holds this service’s sandbox turn.',
    'sandbox_maintenance': 'The sandbox job history of this host is full. Script runs resume once the operator has cleared it.',
    'sandbox_daily_limit': 'This host reached its script jobs for 24 hours. Script runs resume as the earliest of them turn a day old.',
    'company_daily_limit': 'This company reached its script jobs for 24 hours. Script runs resume as the earliest of them turn a day old.',
    'sandbox_history_share': ('Script jobs have used their share of this host’s sandbox job history. '
                              'They resume once the operator has installed the broker’s retention upgrade.'),
    'sandbox_cleanup': 'An earlier sandbox job is still being cleared. Script runs resume once it is.',
    'daily_limit': DAILY_LIMIT,
    'script_stopped': 'The script’s process ended before it returned a result.',
    'time_limit': f'The script ran past its {RUN_SECONDS} seconds.',
    'network': 'The script opened more connections than a job’s socket takes. A script computes from its input; the host holds the network.',
    'stopped': 'The run was stopped.',
    'input_too_large': 'The input of this run exceeds the sandbox’s input size.',
    'output_contract': 'The output differs from the contract.',
    'script_error': 'The script raised an error.',
    'script_syntax': 'The script has a syntax error.',
    'script_contract': 'The script differs from the contract: def run(input) returns a dict.',
    'memory_limit': f'The script reached its {MEMORY_MB} MB of memory.',
    'output_too_large': 'An output exceeds its size.',
    'output_encoding': 'An output holds a value outside JSON.',
}
# What the broker answers a prepare with before it makes a row for the request.
REFUSALS = frozenset({'capacity_reached', 'custom_output_capacity', 'custom_history_capacity'})
# Faults that pass by themselves within minutes: the tracker service asks for another pass.
TRANSIENT = frozenset({'sandbox_busy', 'sandbox_turn', 'sandbox_cleanup'})

def _listed(names):
    return ', '.join(sorted(names))


TRACKER_SCRIPT_INSTRUCTIONS = f"""\
A tracker's processing script is one Python file that turns the tracker's events into its tile and canvas.

Contract
  def run(input): return output
  Python 3.12, standard library. The host calls run once per period in one sandbox job: the day first, then the week, the month and the year. Each call receives a fresh copy of input with input["period"] set. A run passes when every period returns a valid output.

Where it runs
  A sandbox job holds the script, the host's entry file and the input as read-only files. It has {RUN_SECONDS} seconds for the four periods together, one CPU, {MEMORY_MB} MB of memory and 16 MB of scratch space under /tmp. Network access and credentials stay with the host. Data from a connection (a till, an ERP) arrives as host-fetched input in input["connections"]; version 1 passes it empty, and till sales the host has recorded are in input["records"]["sales"].

Input (schema 1)
  tracker      id, key, label, observes, event_types, unit, tile_family, canvas_family
  location     id, company_id, collection
  period       name (day, week, month or year), start and end as ISO times in UTC, label, step (hour or day)
  periods      the four periods of this job
  events       the tracker's committed events, newest first: those whose type is in the tracker's event_types (every event of the location while that list is empty), from 30 days before the earliest period to the end of the latest. Fields: id, type, label, description, object_description, t and t_end (epoch seconds; null for a recording with an unknown clock), at (ISO), at_s, end_s, visit_id, person_id, source_id, camera_id, thumb, product_type, product_type_label, product_id
  records      read-only rows of the location: visits, items, baskets, sales, sale_lines (by sale id), shelf_states, queue_intervals, catalog, cameras, zones
  connections  an empty dict in version 1
  prior        the published output before this run: revision, value, unit, state, period; null before the first
  limits       seconds, output_bytes, memory_mb, rows (the most rows of each kind) and truncated (true for a kind that reached its cap)

Output (one dict per period, with exactly these keys and the optional ones)
  value        a number, or null
  unit         a string of up to 24 characters, or null
  state        "ready", or "needs" with value null
  needs        null when ready; with "needs", one to three words such as "Needs till data"
  summary      1 to 600 characters a person reads
  series       up to 400 points {{"t": time, "value": number}}; t is a full date and time in UTC that ends in Z, such as "2026-10-10T09:00:00Z" (an offset such as +02:00 is read and stored as UTC)
  events       up to 500 rows {{"id": an id from input.events or input.records.visits}}, each with an optional "label" (200 characters) and "description" (600); the host fills time, picture, camera and source from its own record
  canvas       {{"rows": up to 500 rows of that shape, "total_rows": a whole number of at least len(rows)}}, plus keys of your own. A key of your own is lower_snake_case and holds numbers, words and lists and objects of them, up to six levels deep. Evidence rows live in events and canvas.rows.
  trend        optional: up to 400 points of the series shape
  tile_extra   optional: up to 8 scalar values under keys of your own
  texts        optional, on the day: up to {TEXT_SLOTS} slots {{"slot": name, "facts": JSON the text is written from}}
  A number is finite, and a whole number stays within 2**53 either side of zero.

Reserved names
  The host stamps these, and the page reads them on a tile: {_listed(RESERVED - set(OUTPUT_KEYS) - set(OPTIONAL_KEYS))}. A canvas key or a tile_extra key of your own takes another name; value, unit, state, needs, summary, series, trend, events and texts live at the top of the output. Picture, clip and link fields hold host values at every depth: {_listed(LINKS)}, and every name that ends in _url. The fields of an evidence row stay in events and canvas.rows: under a key of your own, give an object other names than {_listed(ROW_FIELDS)}.

Limits
  Source {SCRIPT_BYTES // 1024} KB. Each output {OUTPUT_BYTES // 1024} KB of JSON. print() goes to a log; its last 4,000 characters come back with a test.

Written text (a model step)
  For a memo or a comment, return texts on the day's output and set the wake policy to on_change. For a tracker with an active Tracking Agent, the host wakes that agent after a run that published or proposed a new output, and names the output revision or the proposal. The agent writes each slot with write_tracker_text(tracker_id, slot, text, output_revision) for a published output, or with proposal_id for a waiting proposal. A written text gives the output its next revision and stays while the slot's facts stay the same.

Lifecycle
  save_tracker_script stores a draft revision. test_tracker_script runs it in the sandbox over the location's present data and returns the tile and canvas it would publish, with every error, its line and the log. publish_tracker_script makes a revision with a passing test the one that runs. From then on the tracker runner runs it once each local day of the location, in the day's first pass at or after the location's daily time; on request; and in the passes new events start once this host has event runs switched on. A job that found the sandbox full is tried again in the location's next pass, and the tracker service asks for that pass by itself up to {RETRIES_PER_DAY} times a day. A run with the same events and records as the last good run reuses that result.

Policies (set_tracker_script_policy)
  publish      automatic: a valid output is published by the run. review: a valid output waits as a proposal, approve_tracker_output publishes it, and the last published output stays on the surface meanwhile.
  wake         never, on_failure, on_change or every_run: when the host wakes the tracker's active Tracking Agent after a run, with one short message. on_failure wakes after a run that stopped; on_change after a run that published or proposed a new output; every_run after each run the sandbox took, whatever its result. Under those three, the run that pauses the script wakes the agent too. A tracker managed from a conversation has its health and its runs in get_tracker_script. The defaults are automatic and on_failure.

A run that stops
  A script that raises, runs past its time, returns another shape, names an id outside its input or exceeds a size is recorded as a stopped run with its code and line; get_tracker_script shows its message to a reader whose cameras cover the location. The last published output stays in place and the Core Trackers keep their own. After {PAUSE_AFTER} stopped runs in a row the script pauses and its health says why. A passing test of the published revision, or publishing a corrected revision, starts it again.
"""

EXAMPLE_SOURCE = '''from datetime import datetime, timezone


def run(input):
    period = input["period"]
    start = datetime.fromisoformat(period["start"].replace("Z", "+00:00")).timestamp()
    end = datetime.fromisoformat(period["end"].replace("Z", "+00:00")).timestamp()
    step = 3600 if period["step"] == "hour" else 86400
    inside = [event for event in input["events"] if event["t"] is not None and start <= event["t"] < end]
    series, moment = [], start
    while moment < end:
        count = sum(1 for event in inside if moment <= event["t"] < moment + step)
        at = datetime.fromtimestamp(moment, timezone.utc).isoformat().replace("+00:00", "Z")
        series.append({"t": at, "value": count})
        moment += step
    rows = [{"id": event["id"]} for event in inside[:500]]
    return {"value": len(inside), "unit": input["tracker"]["unit"], "state": "ready", "needs": None,
            "summary": f"{len(inside)} events, {period['label']}.", "series": series, "events": rows[:40],
            "canvas": {"rows": rows, "total_rows": len(inside)}}
'''


class OutputError(ValueError):
    """An output the contract refuses: `errors` is [{path, message}]."""

    def __init__(self, errors):
        super().__init__(errors[0]['message'] if errors else FAULTS['output_contract'])
        self.errors = errors


def canonical(value):
    """The sandbox's own canonical JSON (`custom_worker.canonical`)."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def iso(epoch):
    return None if epoch is None else datetime.fromtimestamp(round(epoch, 3), timezone.utc).isoformat().replace('+00:00', 'Z')


def fault(code, message=None, **more):
    return {'code': code, 'message': message or FAULTS.get(code, FAULTS['script_stopped']), 'line': None, 'traceback': None,
            'log': None, **more}


# ── The input ──────────────────────────────────────────────────────────────────────────────────

def named(name, period):
    return {'name': name, 'start': period['start'], 'end': period['end'], 'label': period['label'], 'step': period.get('step', 'day')}


def job_periods(company, location_id, data, day=None):
    """The periods of one job: the day, then the week, the month and the year the page reads."""
    from core_trackers import common
    from core_trackers.run import latest_day, reading_periods
    day = day or common.day_period(latest_day(data))
    return [named('day', day)] + [named(name, period) for name, period in reading_periods(company, location_id, data).items()]


def prior_of(output):
    """What a script reads of the output published before its run."""
    if not output:
        return None
    tile = output.get('tile') or {}
    return {'revision': output.get('revision'), 'value': tile.get('value'), 'unit': tile.get('unit'), 'state': tile.get('state'),
            'period': output.get('period')}


def without_internal(value):
    """A host row as a script reads it: the host's own provenance fields left out at every depth."""
    if isinstance(value, dict):
        return {key: without_internal(child) for key, child in value.items() if key not in INTERNAL}
    if isinstance(value, (list, tuple)):
        return [without_internal(child) for child in value]
    return value


def build_input(data, tracker, periods, *, connections=None, prior=None, seconds=RUN_SECONDS):
    """The JSON document one job reads: the periods, the tracker's own events and bounded location rows.

    Events are the ones whose type the tracker record lists (`tracker.event_types`; every event of
    the location while the list is empty), newest first, from TREND_DAYS before the earliest
    period to the end of the latest. Internal provenance fields (`INTERNAL`) stay with the host at
    every depth of a row. Row caps halve until the document fits the sandbox's input size;
    `limits.truncated` names each capped kind.
    """
    from core_trackers import common
    if not periods or len(periods) > MAX_PERIODS:
        raise ValueError('Run a script over one to four periods.')
    kinds = set(tracker['tracker'].get('event_types') or [])
    floor = min(common.epoch(period['start']) for period in periods) - common.TREND_DAYS * 86400
    ceiling = max(common.epoch(period['end']) for period in periods)
    clean = without_internal
    inside = lambda row: row.get('t') is None or floor <= row['t'] < ceiling
    newest = lambda rows_: sorted(rows_, key=lambda row: (row.get('t') is None, -(row.get('t') or 0), str(row.get('id'))))
    events = newest({**clean(event), 'at': iso(event['t'])} for event in data.events
                    if (not kinds or event['type'] in kinds) and inside(event))
    sales = newest(clean(row) for row in data.sales if inside(row))
    available = {'events': events, 'visits': newest(clean(row) for row in data.visits if inside(row)),
        'items': [clean(row) for row in data.items], 'baskets': newest(clean(row) for row in data.baskets if inside(row)),
        'sales': sales, 'shelf_states': [clean(row) for row in data.shelves],
        'queue_intervals': newest(clean(row) for row in data.queues if inside(row)),
        'catalog': [clean(row) for row in data.catalog],
        'cameras': [{'id': camera['id'], 'name': camera.get('name')} for camera in data.cameras],
        'zones': [clean(row) for row in data.zones]}
    described = {'id': tracker['id'], 'key': tracker['key'], 'label': tracker['label'], 'observes': tracker['tracker'].get('observes'),
        'event_types': sorted(kinds), 'unit': (tracker.get('tile') or {}).get('unit'),
        'tile_family': (tracker.get('tile') or {}).get('family'), 'canvas_family': (tracker.get('canvas') or {}).get('family')}
    caps = dict(ROWS)
    for _ in range(16):
        kept = {kind: rows_[:caps[kind]] for kind, rows_ in available.items()}
        lines, count = {}, 0
        for sale in kept['sales']:
            for line in data.sale_lines.get(sale['id'], []):
                if count >= caps['sale_lines']:
                    break
                lines.setdefault(sale['id'], []).append(clean(line))
                count += 1
        every = sum(len(data.sale_lines.get(sale['id'], [])) for sale in sales)
        truncated = {kind: len(rows_) > caps[kind] for kind, rows_ in available.items()} | {'sale_lines': every > count}
        document = {'schema': 1, 'tracker': described,
            'location': {'id': data.location_id, 'company_id': data.company.id, 'collection': data.company.collection},
            'period': periods[0], 'periods': list(periods), 'events': kept.pop('events'),
            'records': {**kept, 'sale_lines': lines}, 'connections': connections or {}, 'prior': prior,
            'limits': {'seconds': seconds, 'output_bytes': OUTPUT_BYTES, 'memory_mb': MEMORY_MB, 'rows': dict(caps), 'truncated': truncated}}
        if len(canonical(document)) <= INPUT_BYTES:
            return document
        caps = {kind: max(1, cap // 2) for kind, cap in caps.items()}
    raise ScriptError('input_too_large', FAULTS['input_too_large'])


def input_digest(document):
    """The identity of a job's input: everything a script computes from. The prior output stays
    out, so a script that reads it still settles on one result for the same events and records."""
    return hashlib.sha256(canonical({k: v for k, v in document.items() if k != 'prior'})).hexdigest()


def input_parts(document):
    """The input as the files a job mounts: [('input/000.json', bytes), …], each within one file's size."""
    raw = canonical(document)
    if len(raw) > INPUT_BYTES:
        raise ScriptError('input_too_large', FAULTS['input_too_large'])
    return [(f'input/{index:03d}.json', raw[offset:offset + PART_BYTES]) for index, offset in enumerate(range(0, len(raw), PART_BYTES))]


def snapshot_for(*, company_id, owner, collection, tracker_id, revision, source, parts, runtime):
    """The sandbox bundle of one job: the entry, the script and the input, as the broker checks it.

    The broker verifies a bundle's own consistency: its scope, its runtime identity and every file
    hash. This bundle carries no target and no credential generation: a tracker script holds no
    network grant. It never enters the connector store.
    """
    from custom_execution import ExecutionSnapshot, SourceFile, digest, encoded
    import custom_worker
    files = sorted([(ENTRY, ENTRY_SOURCE.encode()), (SCRIPT, source.encode('utf-8')), *parts])
    manifest = {'profile': custom_worker.PROFILE, 'entrypoint': ENTRY, 'tools': [{'name': TOOL}], 'tests': []}
    listing = [{'path': path, 'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()} for path, content in files]
    document = {'schema': 1, 'company': company_id, 'owner': owner, 'collection': collection,
        'connector_id': 'tracker-script-' + tracker_id.removeprefix('tracker-'), 'revision': revision,
        'source_sha256': digest(listing), 'files': listing, 'secret_fields': [], 'manifest': manifest,
        'manifest_sha256': digest(manifest), 'target': None, 'target_sha256': '', 'credential_generation': 0,
        'target_generation': 0, 'revocation_generation': 0, 'runtime': runtime.document()}
    return ExecutionSnapshot(encoded(document), tuple(SourceFile(path, content, hashlib.sha256(content).hexdigest()) for path, content in files))


# ── The output ─────────────────────────────────────────────────────────────────────────────────

def _number(value):
    """A number an output may carry: a finite float, or a whole number inside NUMBER_LIMIT."""
    if type(value) is int:
        return -NUMBER_LIMIT <= value <= NUMBER_LIMIT
    return type(value) is float and math.isfinite(value)


def _text(value, most, least=0):
    return isinstance(value, str) and least <= len(value) <= most


def host_field(name):
    """Whether a page reads this name as a picture, a clip or a link."""
    return name in LINKS or name.endswith('_url')


def _free(value, path, errors, depth=1):
    """A value under a key of the script's own: numbers, words and lists and objects of them, up
    to six levels. An object here is the script's own data and never an evidence row: the fields
    a page reads as a picture, a clip or a link, and the fields of a row's identity, are refused
    at every depth."""
    if len(errors) >= 20:
        return
    if depth > 6:
        errors.append({'path': path, 'message': 'Keep a value of your own within six levels of nesting.'})
    elif isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not NAME.fullmatch(key):
                errors.append({'path': f'{path}.{_shown(key)}', 'message': 'Name a key in lower_snake_case, up to 40 characters.'})
            elif host_field(key):
                errors.append({'path': f'{path}.{key}', 'message': 'Picture, clip and link fields hold host values. Give this key another name.'})
            elif key in ROW_FIELDS:
                errors.append({'path': f'{path}.{key}', 'message': 'This is a field of an evidence row. Rows live in events and canvas.rows; give this key another name.'})
            else:
                _free(child, f'{path}.{key}', errors, depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value[:2000]):
            _free(child, f'{path}[{index}]', errors, depth + 1)
        if len(value) > 2000:
            errors.append({'path': path, 'message': 'Keep a list of your own within 2,000 entries.'})
    elif type(value) in (int, float):
        if not _number(value):
            errors.append({'path': path, 'message': 'Use finite numbers, whole ones within 2**53 either side of zero.'})
    elif not (value is None or isinstance(value, (str, bool))):
        errors.append({'path': path, 'message': 'Use JSON values.'})


def _shown(key):
    """A key of the script's own as an error path names it: its first 40 characters."""
    return str(key)[:40]


POINT_TIME = re.compile(r'(20\d{2}|2100)-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})')


def point_time(value):
    """A point's time as the host stores it: UTC, ending in Z, the form the Core Trackers write.
    None for a text that is another form of time, or none."""
    if not isinstance(value, str) or not POINT_TIME.fullmatch(value):
        return None
    try:
        return iso(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
    except (ValueError, OverflowError):
        return None


def _points(value, path, errors):
    if not isinstance(value, list) or len(value) > 400:
        errors.append({'path': path, 'message': 'Give up to 400 points {"t": time, "value": number}.'})
        return []
    points = []
    for index, point in enumerate(value):
        at = point_time(point.get('t')) if isinstance(point, dict) and set(point) == {'t', 'value'} else None
        if at is None or not _number(point['value']):
            errors.append({'path': f'{path}[{index}]', 'message': 'A point is exactly {"t": a full date and time in UTC ending in Z, '
                           'such as "2026-10-10T09:00:00Z", "value": a finite number}.'})
            return []
        points.append({'t': at, 'value': point['value']})
    return points


def _rows(value, path, most, events, visits, errors):
    """Evidence rows, rebuilt from the host's own records: the script chooses ids and words."""
    from core_trackers import common
    if not isinstance(value, list) or len(value) > most:
        errors.append({'path': path, 'message': f'Give up to {most} rows.'})
        return []
    rows_ = []
    for index, entry in enumerate(value):
        at = f'{path}[{index}]'
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or set(entry) - {'id', 'label', 'description'}:
            errors.append({'path': at, 'message': 'A row carries "id" and, for words of your own, "label" and "description".'})
            continue
        event, visit = events.get(entry['id']), visits.get(entry['id'])
        if event is not None:
            row = common.event_row(event)
        elif visit is not None:
            row = {'id': visit['id'], 'at': iso(visit.get('t')), 'label': 'Visitor', 'description': '', 'thumb': None,
                   'visitor_id': visit['id'], 'source_id': visit.get('source_id'), 'camera_id': visit.get('camera_id'),
                   'person_id': visit.get('person_id'), 'at_s': visit.get('first_s'), 'type': 'visit', 'product': None}
        else:
            errors.append({'path': at + '.id', 'message': 'Use an id from input.events or input.records.visits.'})
            continue
        for name, most_ in (('label', 200), ('description', 600)):
            if name in entry:
                if not _text(entry[name], most_):
                    errors.append({'path': f'{at}.{name}', 'message': f'Write {name} as text of up to {most_} characters.'})
                else:
                    row[name] = entry[name]
        rows_.append(row)
        if len(errors) >= 20:
            break
    return rows_


def validate_output(value, tracker, document):
    """One period's output, checked against the contract. Returns what `publish_output` stores.

    Raises OutputError with one {path, message} per fault, 20 at most. Nothing here trusts the
    script: reserved names are refused, every row id has to be in the input the host built, and
    each row is rebuilt from the host's record with the script's label and description alone.
    """
    from core_trackers import common
    errors = []
    if not isinstance(value, dict):
        raise OutputError([{'path': '', 'message': 'run(input) returns a dict.'}])
    missing = [key for key in OUTPUT_KEYS if key not in value]
    extra = sorted(_shown(key) for key in set(value) - set(OUTPUT_KEYS) - set(OPTIONAL_KEYS))
    if missing or extra:
        raise OutputError([{'path': name, 'message': 'An output has exactly value, unit, state, needs, summary, series, events and canvas, '
                            'with trend, tile_extra and texts as options.'} for name in (missing + extra)[:20]])
    try:
        size = len(canonical(value))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise OutputError([{'path': '', 'message': 'Return finite JSON values.'}]) from None
    if size > OUTPUT_BYTES:
        raise OutputError([{'path': '', 'message': f'One output is up to {OUTPUT_BYTES} bytes of JSON; this one has {size}.'}])
    state, needs, number = value['state'], value['needs'], value['value']
    if state not in ('ready', 'needs'):
        errors.append({'path': 'state', 'message': 'state is "ready" or "needs".'})
    elif state == 'ready' and needs is not None:
        errors.append({'path': 'needs', 'message': 'needs is null while state is "ready".'})
    elif state == 'needs' and (not _text(needs, 40, 1) or not 1 <= len(needs.split()) <= 3 or number is not None):
        errors.append({'path': 'needs', 'message': 'With state "needs", needs is one to three words and value is null.'})
    if number is not None and not _number(number):
        errors.append({'path': 'value', 'message': 'value is a finite number or null.'})
    if value['unit'] is not None and not _text(value['unit'], 24, 1):
        errors.append({'path': 'unit', 'message': 'unit is null or a string of up to 24 characters.'})
    if not _text(value['summary'], 600, 1) or not value['summary'].strip():
        errors.append({'path': 'summary', 'message': 'summary is 1 to 600 characters.'})
    series = _points(value['series'], 'series', errors)
    trend = _points(value.get('trend', []), 'trend', errors)
    events = {event['id']: event for event in document['events']}
    visits = {visit['id']: visit for visit in document['records'].get('visits', [])}
    listed = _rows(value['events'], 'events', 500, events, visits, errors)
    canvas, shown = value['canvas'], {}
    if not isinstance(canvas, dict) or 'rows' not in canvas or 'total_rows' not in canvas:
        errors.append({'path': 'canvas', 'message': 'canvas is a dict with "rows" and "total_rows".'})
    else:
        rows_ = _rows(canvas['rows'], 'canvas.rows', common.CANVAS_ROWS, events, visits, errors)
        total = canvas['total_rows']
        if type(total) is not int or not _number(total) or total < (len(canvas['rows']) if isinstance(canvas['rows'], list) else 0):
            errors.append({'path': 'canvas.total_rows', 'message': 'total_rows is a whole number of at least len(rows).'})
        shown = {'rows': rows_, 'total_rows': total}
        for key, child in canvas.items():
            if key in ('rows', 'total_rows'):
                continue
            if not isinstance(key, str) or not NAME.fullmatch(key):
                errors.append({'path': f'canvas.{_shown(key)}', 'message': 'Name a canvas key in lower_snake_case, up to 40 characters.'})
            elif key in RESERVED or host_field(key) or key in ROW_FIELDS:
                errors.append({'path': f'canvas.{key}', 'message': 'The host stamps this name. Give your canvas key another name.'})
            else:
                _free(child, f'canvas.{key}', errors)
                shown[key] = child
    extras = value.get('tile_extra', {})
    if not isinstance(extras, dict) or len(extras) > 8:
        errors.append({'path': 'tile_extra', 'message': 'tile_extra is a dict of up to 8 scalar values.'})
        extras = {}
    for key, child in extras.items():
        if not isinstance(key, str) or not NAME.fullmatch(key) or key in RESERVED or host_field(key) or key in ROW_FIELDS:
            errors.append({'path': f'tile_extra.{_shown(key)}', 'message': 'The host stamps this name. Give your tile_extra key another name in lower_snake_case.'})
        elif not (child is None or type(child) is bool or _number(child) or _text(child, 200)):
            errors.append({'path': f'tile_extra.{key}', 'message': 'A tile_extra value is a string of up to 200 characters, a finite number, true, false or null.'})
    slots = value.get('texts', [])
    if not isinstance(slots, list) or len(slots) > TEXT_SLOTS:
        errors.append({'path': 'texts', 'message': f'texts is a list of up to {TEXT_SLOTS} slots.'})
        slots = []
    seen = set()
    for index, slot in enumerate(slots):
        at = f'texts[{index}]'
        if (not isinstance(slot, dict) or set(slot) != {'slot', 'facts'} or not isinstance(slot['slot'], str)
                or not NAME.fullmatch(slot['slot']) or slot['slot'] in seen):
            errors.append({'path': at, 'message': 'A text slot is exactly {"slot": a distinct lower_snake_case name, "facts": JSON}.'})
            continue
        seen.add(slot['slot'])
        if _size(slot['facts']) > FACTS_BYTES:
            errors.append({'path': at + '.facts', 'message': f'The facts of a slot are up to {FACTS_BYTES} bytes of JSON.'})
        _free(slot['facts'], at + '.facts', errors)
    if errors:
        raise OutputError(errors[:20])
    return {'value': number, 'unit': value['unit'], 'state': state, 'needs': needs, 'summary': value['summary'], 'series': series,
            'trend': trend, 'events': listed, 'canvas': shown, 'tile_extra': dict(extras),
            'texts': [{'slot': slot['slot'], 'facts': slot['facts']} for slot in slots]}


def _size(value):
    """The bytes of a value's canonical JSON; past every size for a value JSON cannot hold."""
    try:
        return len(canonical(value))
    except (ValueError, TypeError, RecursionError, OverflowError):
        return OUTPUT_BYTES + 1


def slot_texts(slots, *written):
    """Each marked slot with the text written for the same facts, where one was written."""
    known = {}
    for source in written:
        for entry in source or []:
            if entry.get('text'):
                known.setdefault((entry['slot'], canonical(entry.get('facts'))), entry)
    texts = []
    for slot in slots:
        prior = known.get((slot['slot'], canonical(slot['facts'])), {})
        texts.append({'slot': slot['slot'], 'facts': slot['facts'], 'text': prior.get('text'), 'written_at': prior.get('written_at')})
    return texts


def publish_output(data, tracker, period, accepted, *, script_revision, texts=None):
    """Write a validated output through the one writer of every tracker's output.

    Under `common.READING` it returns {tile, canvas} and stores nothing. The host stamps scope,
    period, families, the script revision and the text slots after the script's own keys.
    """
    from core_trackers import common
    canvas = {**accepted['canvas'], 'script_revision': script_revision}
    if texts:
        canvas['texts'] = texts
    return common.publish(data, tracker['key'], period, value=accepted['value'], unit=accepted['unit'], series=accepted['series'],
        events=accepted['events'], canvas=canvas, summary=accepted['summary'], trend=accepted['trend'], state=accepted['state'],
        needs=accepted['needs'], tile_extra={**accepted['tile_extra'], 'script_revision': script_revision})


def content_of(tile, canvas):
    """The content hash `common.publish` gives a tile and canvas: their revision stays out of it."""
    import retail_records as records
    bare = lambda value: {k: v for k, v in value.items() if k != 'revision'}
    return records.digest({'tile': bare(tile), 'canvas': bare(canvas)}, 64)


# ── The entry the sandbox runs ─────────────────────────────────────────────────────────────────

ENTRY_SOURCE = '''"""Entry of a tracker processing script. The host wrote this file; the script is tracker.py."""
import json
import os
import signal
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'tracker.py')
SECONDS, MEMORY, OUTPUT, ENVELOPE, PERIODS, TAIL = %(seconds)d, %(memory)d, %(output)d, %(envelope)d, %(periods)d, 4000


class Limit(BaseException):
    """The time limit. It passes handlers of Exception."""


class Contract(Exception):
    pass


def plain(text):
    return str(text).encode('utf-8', 'replace').decode('utf-8')


def stopped(code, message, line=None, trace=None):
    return {'ok': False, 'error': {'code': code, 'message': plain(message)[:500], 'line': line, 'traceback': trace}}


def where(exc):
    """The deepest line of tracker.py in the fault, and the fault as the script's own frames."""
    line, frames = None, []
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename == SCRIPT:
            line = frame.lineno
            frames.append(frame)
    if isinstance(exc, SyntaxError) and exc.filename == SCRIPT:
        line = exc.lineno
    text = ''.join(traceback.format_list(frames) + traceback.format_exception_only(type(exc), exc))
    return line, plain(text.replace(HERE + os.sep, ''))[-TAIL:]


def named(exc):
    return type(exc).__name__ + (': ' + str(exc) if str(exc) else '')


def packed(value):
    if not isinstance(value, dict):
        return stopped('script_contract', 'run(input) returns a dict.')
    try:
        raw = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, RecursionError) as exc:
        return stopped('output_encoding', 'Return finite JSON values: ' + named(exc))
    if len(raw) > OUTPUT:
        return stopped('output_too_large', 'One output is up to %%d bytes of JSON; this one has %%d.' %% (OUTPUT, len(raw)))
    return {'ok': True, 'output': json.loads(raw)}


def compute(raw, periods, seconds):
    def expired(*_):
        raise Limit()
    late = stopped('time_limit', 'The script ran past its %%d seconds.' %% seconds)
    results, halted = [], None
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        try:
            import runpy
            run = runpy.run_path(SCRIPT).get('run')
            if not callable(run):
                raise Contract('Define one function at the top of the script: def run(input).')
        except Limit:
            halted = late
        except MemoryError:
            halted = stopped('memory_limit', 'The script reached its %%d MB of memory.' %% (MEMORY // 1048576))
        except SyntaxError as exc:
            halted = stopped('script_syntax', 'Syntax error: ' + str(exc.msg), *where(exc))
        except Contract as exc:
            halted = stopped('script_contract', exc)
        except BaseException as exc:
            halted = stopped('script_error', named(exc), *where(exc))
        for period in periods:
            if halted is not None:
                results.append(halted)
                continue
            try:
                document = json.loads(raw)
                document['period'] = period
                result = packed(run(document))
            except Limit:
                result = halted = late
            except MemoryError:
                result = halted = stopped('memory_limit', 'The script reached its %%d MB of memory.' %% (MEMORY // 1048576))
            except BaseException as exc:
                result = stopped('script_error', named(exc), *where(exc))
            results.append(result)
    except Limit:
        results = [late for _ in periods]
    finally:
        signal.alarm(0)
    return (results + [late for _ in periods])[:len(periods)]


def main(out):
    sys.stdin.buffer.read()
    log = os.path.join(os.environ.get('TMPDIR') or '/tmp', 'rocoto-script-%%d.log' %% os.getpid())
    sink = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.dup2(sink, 1)
    os.dup2(sink, 2)
    os.close(sink)
    folder = os.path.join(HERE, 'input')
    raw = b''
    for name in sorted(os.listdir(folder)):
        if name.endswith('.json'):
            with open(os.path.join(folder, name), 'rb') as part:
                raw += part.read()
    document = json.loads(raw)
    periods = list(document['periods'])[:PERIODS]
    seconds = max(1, min(SECONDS, int((document.get('limits') or {}).get('seconds') or SECONDS)))
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY, MEMORY))
    except (ImportError, ValueError, OSError):
        pass
    results = compute(raw, periods, seconds)
    tail = ''
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        with open(log, 'rb') as written:
            written.seek(max(0, os.path.getsize(log) - 4 * TAIL))
            tail = plain(written.read().decode('utf-8', 'replace'))[-TAIL:]
        os.unlink(log)
    except BaseException:
        pass
    envelope = json.dumps({'schema': 1, 'results': results, 'log': tail}, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(envelope) > ENVELOPE:
        large = stopped('output_too_large', 'The outputs of one job are up to %%d bytes of JSON together.' %% ENVELOPE)
        envelope = json.dumps({'schema': 1, 'results': [large for _ in periods], 'log': tail[-1000:]}, separators=(',', ':')).encode('utf-8')
    return envelope


if __name__ == '__main__':
    out = os.dup(1)
    try:
        answer = main(out)
    except BaseException as exc:
        answer = json.dumps({'schema': 1, 'results': [], 'log': '', 'entry': type(exc).__name__}).encode('utf-8')
    try:
        while answer:
            answer = answer[os.write(out, answer):]
    finally:
        os._exit(0)
''' % {'seconds': RUN_SECONDS, 'memory': MEMORY_MB * 1024 * 1024, 'output': OUTPUT_BYTES, 'envelope': ENVELOPE_BYTES, 'periods': MAX_PERIODS}


def evaluate(result, tracker, document):
    """What a sandbox job amounts to: its status and, for a job whose every period is valid, the
    accepted outputs in period order.

    `result` is what `Sandbox.run` returned. Returns {status, error, contract_errors, accepted, log}:
    `succeeded`, `failed` (the script stopped), `rejected` (an output differs from the contract)
    or `unavailable` (the sandbox took no job).
    """
    periods = document['periods']
    verdict = {'status': result['status'], 'error': result.get('error'), 'contract_errors': [], 'accepted': [], 'log': result.get('log')}
    if result['status'] != 'succeeded':
        return verdict
    results = result.get('results')
    if not isinstance(results, list) or len(results) != len(periods) or any(not isinstance(entry, dict) for entry in results):
        return {**verdict, 'status': 'failed', 'error': fault('script_stopped', log=result.get('log'))}
    for period, entry in zip(periods, results):
        if entry.get('ok') is not True:
            error = entry.get('error') if isinstance(entry.get('error'), dict) else {}
            # The envelope comes out of the sandbox: its code is one the entry file gives, its line a line of the script.
            code = error.get('code') if error.get('code') in SCRIPT_CODES else 'script_error'
            line = error.get('line') if type(error.get('line')) is int and 0 < error['line'] <= 100000 else None
            trace = error.get('traceback') if isinstance(error.get('traceback'), str) else None
            return {**verdict, 'status': 'failed', 'error': fault(code, str(error.get('message') or FAULTS['script_stopped'])[:500],
                line=line, traceback=trace[-4000:] if trace else None, log=result.get('log'), period=period['name'])}
    for period, entry in zip(periods, results):
        try:
            verdict['accepted'].append(validate_output(entry.get('output'), tracker, {**document, 'period': period}))
        except OutputError as exc:
            errors = exc.errors
        except Exception as exc:  # A value the checks themselves stop on is an output outside the contract.
            errors = [{'path': '', 'message': f'The output holds a value the contract leaves out ({type(exc).__name__}). Return finite JSON values.'}]
        else:
            continue
        mark = lambda path: f"{period['name']}: {path}" if path else period['name']
        listed = [{'path': mark(error['path']), 'message': error['message']} for error in errors]
        return {**verdict, 'status': 'rejected', 'accepted': [], 'contract_errors': listed,
                'error': fault('output_contract', f"{listed[0]['path']}: {listed[0]['message']}"[:500], log=result.get('log'), period=period['name'])}
    return verdict


# ── The sandbox ────────────────────────────────────────────────────────────────────────────────


from display_scripts import (
    ScriptError, ScriptContract, ScriptSandbox, JobCounts as DisplayJobCounts, JobLimits,
    DenySocket, validate_source as validate_script_source,
)


def validate_source(text):
    return validate_script_source(text, filename=SCRIPT, max_bytes=SCRIPT_BYTES)


class JobCounts(DisplayJobCounts):
    def __init__(self, path=None, *, clock=time.time):
        super().__init__(path, clock=clock, limits=lambda: JobLimits(
            company=COMPANY_JOBS_PER_DAY, host=HOST_JOBS_PER_DAY, history=HOST_JOBS_BEFORE_RETENTION,
            company_retained=COMPANY_JOBS_PER_DAY_EVENTS, host_retained=HOST_JOBS_PER_DAY_EVENTS))


class Sandbox(ScriptSandbox):
    """Retail input bundle and commissioned broker names over the shared script executor."""
    def __init__(self, **options):
        contract = ScriptContract(input_parts=input_parts,
            snapshot=lambda display_id, **args: snapshot_for(tracker_id=display_id, **args),
            input_digest=input_digest, fault=fault, tool=TOOL, scope_prefix='tracker', run_seconds=RUN_SECONDS)
        if options.get('counts') is None:
            options['counts'] = JobCounts()
        super().__init__(contract, **options)

    async def run(self, *, tracker_id, **options):
        return await super().run(display_id=tracker_id, **options)


_BOX = None


def switches(environ=None):
    """The two host switches of script runs: the sandbox, and runs on the `events` trigger."""
    environ = os.environ if environ is None else environ
    events = setting(EVENT_RUNS_SWITCH, 'off', environ=environ).strip().lower()
    if events not in {'on', 'off'}:
        raise ValueError(f'Set {EVENT_RUNS_SWITCH} to on or off.')
    return {'sandbox': environ.get(SANDBOX_SWITCH) == '1', 'event_runs': events == 'on'}


def box():
    """This process's sandbox handle. It stays switched off until `configure` has read the host's switches."""
    global _BOX
    if _BOX is None:
        _BOX = Sandbox()
    return _BOX


COUNTS_FILE = 'tracker-script-jobs.sqlite'


def configure(*, loop=None, environ=None, sandbox=None, root=None):
    """Called once as the service starts: the event loop script jobs run on, the host's switches
    and, with `root`, the service's state root, where the count of script jobs is kept."""
    global _BOX
    if sandbox is None:
        chosen = switches(environ)
        sandbox = Sandbox(enabled=chosen['sandbox'], event_runs=chosen['event_runs'],
                          counts=JobCounts(Path(root) / COUNTS_FILE) if root is not None else None)
    sandbox.loop = loop
    _BOX = sandbox
    return sandbox


# ── The store ──────────────────────────────────────────────────────────────────────────────────

def script_id(tracker):
    return 'script-' + tracker['id'].removeprefix('tracker-')


def _suffix(tracker):
    return tracker['id'].removeprefix('tracker-')


def _saved(state, owner, kind, key):
    """One record of the location store, read without opening the company for writing."""
    path = Path(state) / 'location-intelligence.sqlite'
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            row = db.execute('SELECT body FROM records WHERE owner=? AND kind=? AND id=?', (owner, kind, key)).fetchone()
        finally:
            db.close()
    except sqlite3.OperationalError:
        return None
    return json.loads(row[0]) if row else None


class TrackerScripts:
    """A tracker's script records in the location store, beside its tracker and its outputs.

        tracker_script             the head: current source and revision, the published revision,
                                   the two policies, the last test, health and the day's job count
        tracker_script_revision    every saved revision, immutable; the newest KEEP_REVISIONS are kept
        tracker_output_proposal    a validated output that waits for approval (publish policy `review`)
        tracker_script_readings    the week, month and year readings of the published output

    Each change of the head is one transaction on the store, so a tool and a pass agree.
    """

    def __init__(self, company, *, clock=time.time):
        self.company, self.clock = company, clock

    def _get(self, kind, key):
        try:
            return self.company.locations._get(self.company.owner, kind, key)
        except ValueError:
            return None

    def head(self, tracker):
        return self._get('tracker_script', script_id(tracker))

    def revision(self, tracker, revision):
        saved = self._get('tracker_script_revision', f'{script_id(tracker)}-r{revision}')
        if saved is None:
            raise ValueError('Choose a saved revision of this script.')
        return saved

    def output(self, tracker):
        return self._get('tracker_output', 'output-' + _suffix(tracker))

    def proposal(self, tracker):
        return self._get('tracker_output_proposal', 'proposal-' + _suffix(tracker))

    def _change(self, tracker, change):
        """Apply `change(head, put)` to the head inside one transaction and return what it returns."""
        import retail_records as records
        from location_intelligence import encoded
        company, location_id = self.company, tracker['location_id']
        company.locations.authorized(company.owner, company.collection, location_id, write=True)
        with company.locations.connect(True) as db:
            row = db.execute("SELECT body FROM records WHERE owner=? AND kind='tracker_script' AND id=?",
                             (company.owner, script_id(tracker))).fetchone()
            def put(kind, value):
                db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)',
                           (company.owner, kind, value['id'], company.collection, location_id, encoded(records.stamp(company, location_id, value))))
            def drop(kind, key):
                db.execute('DELETE FROM records WHERE owner=? AND kind=? AND id=?', (company.owner, kind, key))
            def get(kind, key):
                found = db.execute('SELECT body FROM records WHERE owner=? AND kind=? AND id=?', (company.owner, kind, key)).fetchone()
                return json.loads(found['body']) if found else None
            put.drop, put.db, put.get = drop, db, get
            return change(json.loads(row['body']) if row else None, put)

    def save(self, tracker, source, *, expected_revision, policy=None, chat_id=None):
        """Store the source as the next draft revision. `expected_revision` is the revision read, 0 for the first."""
        source = validate_source(source)
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        now = iso(self.clock())

        def change(head, put):
            current = head['revision'] if head else 0
            if type(expected_revision) is not int or expected_revision != current:
                raise ValueError(READ_AGAIN)
            chosen = clean_policy({**(head['policy'] if head else DEFAULT_POLICY), **(policy or {})})
            if head and head['source_sha256'] == digest and chosen == head['policy']:
                return head
            identity = script_id(tracker)
            value = {**(head or {'published_revision': None, 'health': {'failures': 0, 'paused': None, 'last_run': None, 'last_good': None},
                                 'jobs': {'day': None, 'count': 0}, 'day': None, 'reads': []}),
                'id': identity, 'tracker_id': tracker['id'], 'tracker_key': tracker['key'], 'kind': KIND, 'revision': current + 1,
                'source': source, 'source_sha256': digest, 'bytes': len(source.encode('utf-8')), 'policy': chosen, 'tested': None,
                'updated_at': now, 'author': {'chat_id': chat_id}}
            put('tracker_script', value)
            put('tracker_script_revision', {'id': f'{identity}-r{current + 1}', 'script_id': identity, 'tracker_id': tracker['id'],
                'tracker_key': tracker['key'], 'kind': KIND, 'revision': current + 1, 'source': source, 'source_sha256': digest,
                'bytes': value['bytes'], 'policy': chosen, 'saved_at': now, 'author': {'chat_id': chat_id}})
            kept = [number for number in range(1, current + 2) if number != value['published_revision']]
            for number in kept[:max(0, len(kept) - KEEP_REVISIONS)]:
                put.drop('tracker_script_revision', f'{identity}-r{number}')
            return value
        return self._change(tracker, change)

    def set_policy(self, tracker, changes, *, expected_revision):
        def change(head, put):
            if head is None:
                raise ValueError(SAVE_FIRST)
            if type(expected_revision) is not int or expected_revision != head['revision']:
                raise ValueError(READ_AGAIN)
            value = {**head, 'policy': clean_policy({**head['policy'], **changes}), 'updated_at': iso(self.clock())}
            put('tracker_script', value)
            saved = self._get('tracker_script_revision', f"{head['id']}-r{head['revision']}")
            if saved:
                put('tracker_script_revision', {**saved, 'policy': value['policy']})
            return value
        return self._change(tracker, change)

    def admit_job(self, tracker, *, most):
        """Count one sandbox job against the tracker's day. False once the day's number is reached."""
        day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()

        def change(head, put):
            if head is None:
                raise ValueError(SAVE_FIRST)
            jobs = head.get('jobs') or {}
            count = jobs.get('count', 0) if jobs.get('day') == day else 0
            if count >= most:
                return False
            put('tracker_script', {**head, 'jobs': {'day': day, 'count': count + 1}})
            return True
        return self._change(tracker, change)

    def refund_job(self, tracker):
        """Give the day its count back for a job the sandbox never took."""
        def change(head, put):
            jobs = (head or {}).get('jobs') or {}
            if head is not None and jobs.get('count', 0) > 0:
                put('tracker_script', {**head, 'jobs': {**jobs, 'count': jobs['count'] - 1}})
        return self._change(tracker, change)

    def record_test(self, tracker, revision, status, run_id):
        """Keep the last test of the head revision. A passing test of the paused revision starts the script again."""
        def change(head, put):
            if head is None or head['revision'] != revision:
                return head
            health = dict(head['health'])
            if status == 'passed' and (health.get('paused') or {}).get('revision') == revision:
                health.update(paused=None, failures=0)
            value = {**head, 'health': health, 'tested': {'revision': revision, 'status': status, 'run_id': run_id, 'at': iso(self.clock())}}
            put('tracker_script', value)
            return value
        return self._change(tracker, change)

    def publish(self, tracker, *, expected_revision):
        """Make the head revision the one that runs. It needs the passing test of that very revision."""
        def change(head, put):
            if head is None:
                raise ValueError(SAVE_FIRST)
            if type(expected_revision) is not int or expected_revision != head['revision']:
                raise ValueError(READ_AGAIN)
            tested = head.get('tested') or {}
            if tested.get('revision') != head['revision'] or tested.get('status') != 'passed':
                raise ValueError(TEST_FIRST)
            value = {**head, 'published_revision': head['revision'], 'published_at': iso(self.clock()),
                     'health': {**head['health'], 'failures': 0, 'paused': None}}
            put('tracker_script', value)
            return value
        return self._change(tracker, change)

    def after_run(self, tracker, revision, detail, *, error=None, input_sha256=None):
        """Carry a run into the script's health: its last run, its last good run, and the pause
        after PAUSE_AFTER stopped runs in a row. `error` is the run's own fault with the script's
        words; the health keeps it for the readers `get_tracker_script` shows it to."""
        now = iso(self.clock())
        error = error or {}

        def change(head, put):
            if head is None:
                return None
            health = dict(head['health'])
            health['last_run'] = {'status': detail['status'], 'at': now, 'trigger': detail.get('trigger'), 'script_revision': revision,
                                  'error_code': error.get('code'), 'line': error.get('line'), 'period': error.get('period'),
                                  'message': error.get('message')}
            if detail['status'] == 'succeeded':
                health.update(failures=0, last_good={'at': now, 'script_revision': revision, 'input_sha256': input_sha256,
                    'output_revision': detail.get('output_revision'), 'proposal_id': detail.get('proposal_id')})
            elif detail['status'] in {'failed', 'rejected'} and head.get('published_revision') == revision:
                health['failures'] = health.get('failures', 0) + 1
                if health['failures'] >= PAUSE_AFTER and not health.get('paused'):
                    health['paused'] = {'at': now, 'revision': revision, 'error_code': error.get('code'), 'line': error.get('line'),
                                        'count': health['failures'], 'message': error.get('message')}
            value = {**head, 'health': health}
            put('tracker_script', value)
            return value
        return self._change(tracker, change)

    def note_day(self, tracker, day, *, done, retry=False):
        """Keep how the script stands for a local day of its location: whether its run for that
        day is done, and how many passes the tracker service has asked for by itself.

        `done` is a run the sandbox completed, whatever its result, or a result that was reused.
        A run that had to wait leaves the day owed: the next pass of the location runs it. With
        `retry` the caller asks for that pass by itself; True is returned while the day's number
        of such requests (RETRIES_PER_DAY) has room, and the request is then counted.
        """
        if day is None:
            return False

        def change(head, put):
            if head is None:
                return False
            mine = head.get('day') or {}
            same = mine.get('day') == day
            retries = mine.get('retries', 0) if same else 0
            granted = bool(retry and not done and retries < RETRIES_PER_DAY)
            value = {'day': day, 'done': bool(done or same and mine.get('done')), 'retries': retries + (1 if granted else 0)}
            if value != mine:
                put('tracker_script', {**head, 'day': value})
            return granted
        return self._change(tracker, change)

    def propose(self, tracker, proposal):
        def change(head, put):
            put('tracker_output_proposal', proposal)
            return proposal
        return self._change(tracker, change)

    def drop_proposal(self, tracker, proposal_id=None):
        def change(head, put):
            row = put.db.execute("SELECT body FROM records WHERE owner=? AND kind='tracker_output_proposal' AND id=?",
                                 (self.company.owner, 'proposal-' + _suffix(tracker))).fetchone()
            if row and (proposal_id is None or json.loads(row['body'])['proposal_id'] == proposal_id):
                put.drop('tracker_output_proposal', 'proposal-' + _suffix(tracker))
                return True
            return False
        return self._change(tracker, change)

    def put_readings(self, tracker, output, readings, script_revision):
        """Store the week, month and year readings of the published output. They follow the output
        that stands now: a text written meanwhile gave it a newer revision."""
        def change(head, put):
            current = put.get('tracker_output', output['id']) or output
            put('tracker_script_readings', {'id': 'readings-' + _suffix(tracker), 'tracker_id': tracker['id'], 'tracker_key': tracker['key'],
                'output_revision': current['revision'], 'script_revision': script_revision, 'readings': readings})
        return self._change(tracker, change)


def clean_policy(value):
    """A script's two policies, checked: when a valid output is published and when the agent is woken."""
    if not isinstance(value, dict) or set(value) != {'publish', 'wake'} or value['publish'] not in PUBLISH_MODES or value['wake'] not in WAKE_MODES:
        raise ValueError('Set publish to automatic or review, and wake to never, on_failure, on_change or every_run.')
    return {'publish': value['publish'], 'wake': value['wake']}


def saved_readings(company, location_id, trackers):
    """The week, month and year readings of each script tracker's published output: {key: {name: {tile, canvas}}}."""
    import retail_records as records
    outputs = {output['tracker_id']: output['revision'] for output in records.rows(company, 'tracker_output', location_id)}
    wanted = {tracker['id'] for tracker in trackers if not tracker.get('core')}
    return {row['tracker_key']: row['readings'] for row in records.rows(company, 'tracker_script_readings', location_id)
            if row['tracker_id'] in wanted and outputs.get(row['tracker_id']) == row['output_revision']}


def pause_reason(paused, *, words=False):
    """Why a script is paused. With `words` the sentence carries the script's own message; a
    reader outside the location's cameras gets the code and the line."""
    if not paused:
        return None
    if paused.get('reason'):  # A record from before the message was kept apart.
        return paused['reason'] if words else PAUSED.format(count=PAUSE_AFTER, message=_plain_stop(paused))
    message = paused.get('message') or FAULTS['script_stopped']
    said = (message if message.endswith('.') else message + '.') if words else _plain_stop(paused)
    return PAUSED.format(count=paused.get('count', PAUSE_AFTER), message=said)


def _plain_stop(error):
    """A stopped run in this host's own words: its code and its line."""
    error = error or {}
    code = error.get('error_code') or error.get('code')
    line = f" at line {error['line']}" if error.get('line') else ''
    return f"it stopped{line} with {code}." if code else 'it stopped.'


def health_of(head, *, words=False):
    """A script's health as the tools return it.

    A script writes the words of its own exception. With `words` the last run's message and the
    pause's sentence carry them: that is for a reader whose cameras cover the location. Every
    other read gets the status, the code and the line.
    """
    if not head:
        return None
    health = head.get('health') or {}
    paused, last = health.get('paused'), health.get('last_run')
    status = ('draft' if head.get('published_revision') is None else 'paused' if paused else
              'failing' if health.get('failures') else 'ready' if health.get('last_good') else 'waiting')
    if last:
        last = {k: v for k, v in last.items() if words or k != 'message'}
    if paused:
        paused = {'at': paused.get('at'), 'revision': paused.get('revision'), 'error_code': paused.get('error_code'),
                  'line': paused.get('line'), 'reason': pause_reason(paused, words=words)}
    day = head.get('day') or {}
    return {'status': status, 'consecutive_failures': health.get('failures', 0), 'paused': paused, 'last_run': last,
            'last_good': {k: v for k, v in (health.get('last_good') or {}).items() if k != 'input_sha256'} or None,
            'day': {'day': day.get('day'), 'done': bool(day.get('done'))} if day.get('day') else None}


def describe(head, *, words=False):
    """What `get_tracker` returns of a tracker's script: its revision, policy, health and last test."""
    if not head:
        return {}
    return {'revision': head['revision'], 'published_revision': head.get('published_revision'), 'kind': head['kind'],
            'policy': head['policy'], 'health': health_of(head, words=words), 'last_test': head.get('tested'), 'updated_at': head.get('updated_at')}


def describe_saved(state, owner, tracker):
    """`describe` for a reader without a company handle: the head is read from the store's file."""
    return describe(_saved(state, owner, 'tracker_script', script_id(tracker)))


# ── A run ──────────────────────────────────────────────────────────────────────────────────────

def _bundle(periods, accepted):
    return {'day': {'period': periods[0], 'accepted': accepted[0]},
            'readings': {period['name']: {'period': period, 'accepted': entry} for period, entry in zip(periods[1:], accepted[1:])}}


def readings_of(data, tracker, bundle, *, script_revision):
    """The week, month and year readings of a job, as the page reads them. Nothing is stored."""
    from core_trackers import common
    token = common.READING.set(True)
    try:
        return {name: publish_output(data, tracker, entry['period'], entry['accepted'], script_revision=script_revision)
                for name, entry in bundle['readings'].items()}
    finally:
        common.READING.reset(token)


_WRITERS, _WRITERS_GUARD = {}, threading.Lock()


def writing(tracker):
    """The lock of one tracker's output in this service. A run's publish reads the output that
    stands and writes its next revision, and so does a written text: each holds this lock for the
    whole of that, so one never writes over the other."""
    with _WRITERS_GUARD:
        if len(_WRITERS) > 4096:
            _WRITERS.clear()
        return _WRITERS.setdefault(tracker['id'], threading.Lock())


def publish_bundle(scripts, data, tracker, bundle, *, script_revision, written=()):
    """Publish a job's day output and store its week, month and year readings beside it."""
    readings = readings_of(data, tracker, bundle, script_revision=script_revision)
    with writing(tracker):
        current = scripts.output(tracker)
        texts = slot_texts(bundle['day']['accepted']['texts'], *written, ((current or {}).get('canvas') or {}).get('texts'))
        output = publish_output(data, tracker, bundle['day']['period'], bundle['day']['accepted'], script_revision=script_revision, texts=texts)
        scripts.put_readings(tracker, output, readings, script_revision)
    return output


def preview_bundle(scripts, data, tracker, bundle, *, script_revision, written=()):
    """The tile and canvas a bundle would publish for its day, their content hash, and the hash of
    the same content before any text is written: (shown, content, unwritten). Nothing is stored."""
    from core_trackers import common
    current = scripts.output(tracker)
    slots = bundle['day']['accepted']['texts']
    token = common.READING.set(True)
    try:
        shown = publish_output(data, tracker, bundle['day']['period'], bundle['day']['accepted'], script_revision=script_revision,
                               texts=slot_texts(slots, *written, ((current or {}).get('canvas') or {}).get('texts')))
        bare = publish_output(data, tracker, bundle['day']['period'], bundle['day']['accepted'], script_revision=script_revision,
                              texts=slot_texts(slots))
    finally:
        common.READING.reset(token)
    return shown, content_of(shown['tile'], shown['canvas']), content_of(bare['tile'], bare['canvas'])


def _sentence(error):
    """A run row's words for a script that stopped: this host's own. The script's message stays
    with the script's health, where `get_tracker_script` reads it with the location's cameras."""
    error = error or {}
    line = f" at line {error['line']}" if error.get('line') else ''
    during = f" in its {error['period']} reading" if error.get('period') else ''
    return f"The script stopped{line}{during} with {error.get('code') or 'script_stopped'}. {SEE_MESSAGE}"


def _public_error(error):
    """A run's fault as a run row keeps it: the code, the line, the period and this host's sentence for the code."""
    if not error:
        return None
    public = {k: error.get(k) for k in ('code', 'line', 'period') if error.get(k) is not None}
    return {**public, 'message': FAULTS.get(error.get('code'), FAULTS['script_stopped'])}


def show_on_page(company, tracker):
    """Carry one tracker's stored output and readings into the owner's published page, without a
    pass: the page publisher assembles that one row again. Returns whether the page data changed."""
    try:
        from import_control_room_bundle import show_presentation
        return bool(show_presentation(company, tracker['location_id'], tracker['id']))
    except Exception:
        return False  # The row follows at the location's next pass.


def wake_agent(company, tracker, *, event_id, text, clock=time.time, camera_ids=()):
    """Enqueue one wake for the tracker's Tracking Agent through the durable wake queue, inside its bounds.

    Returns {queued: True, id} or {queued: False, reason}: the queue's own rule when it keeps the wake out.
    """
    agent_id = tracker.get('agent_id')
    if not agent_id:
        return {'queued': False, 'reason': NO_AGENT}
    try:
        from background_agents import AgentStore
        from retail_agent_inputs import RetailInputProvider
        import pack
        from tracker_runner import pass_access
        def current_producer_authority(definition, actor):
            if (actor != company.owner or definition.get('owner_principal_id') != company.owner
                    or definition.get('dataset') != company.collection):
                return False
            company.scope.authority(company.actor)
            pass_access(company, tracker['location_id'])
            return True
        store = AgentStore(company.state, input_provider=RetailInputProvider(monitoring=company.monitoring),
            agent_classes=pack.active().agent_classes(), authority=current_producer_authority)
        with store.connect() as db:
            definition = store._read(db, 'agents', agent_id)
        wake = store.wakes.enqueue(agent_id, owner=company.owner, expected_revision=definition['revision'], provider='tracker_script',
                                   connection_id=tracker['id'], event_id=event_id, text=text[:8000], now=clock(), requires_input=False,
                                   source_scope={'location_id':tracker['location_id'], 'tracker_generation':tracker.get('generation_id'),
                                       'camera_ids':sorted(set(camera_ids) | set(script_camera_ids(company.state, company.owner, tracker)))})
    except ValueError as exc:
        return {'queued': False, 'reason': ' '.join(str(exc).split())[:300]}
    except (sqlite3.Error, OSError) as exc:
        return {'queued': False, 'reason': f'The wake queue met {type(exc).__name__}.'}
    return {'queued': True, 'id': wake['id']}


def wake_text(tracker, detail, head, *, error=None, output=None, proposal=None):
    """One short factual message for the agent a run wakes. The agent is the tracker's own: the
    message carries the script's words for a run that stopped."""
    label, revision, trigger = tracker['label'], detail['script_revision'], detail.get('trigger')
    if detail['status'] in {'failed', 'rejected'}:
        error = error or detail.get('error') or {}
        message = ' '.join(str(error.get('message') or FAULTS['script_stopped']).split())[:300]
        text = (f"The processing script of {label} (revision {revision}) stopped in its {trigger} run"
                f"{' at line ' + str(error['line']) if error.get('line') else ''}: {message}{'' if message.endswith('.') else '.'} "
                'The last published output stays on the surface. Read get_tracker_script for the run, then save, test and publish a corrected script.')
        if (head.get('health') or {}).get('paused'):
            text += f' The script is paused after {PAUSE_AFTER} stopped runs in a row.'
        return text
    if detail.get('output') == 'proposed' and proposal:
        slots = [entry['slot'] for entry in proposal.get('texts') or [] if not entry.get('text')]
        text = f"The script of {label} proposes a new output ({proposal['proposal_id']}): {proposal['summary']} "
        if slots:
            text += (f"It marks {len(slots)} text slot{'s' if len(slots) != 1 else ''} to write with write_tracker_text and "
                     f"proposal_id {proposal['proposal_id']}: {', '.join(slots)}. ")
        return text + 'Read get_tracker_script, then publish it with approve_tracker_output.'
    slots = [entry['slot'] for entry in ((output or {}).get('canvas') or {}).get('texts') or [] if not entry.get('text')]
    if detail.get('output') == 'published' and output:
        text = f"{label} has a new output, revision {output['revision']}: {(output.get('tile') or {}).get('summary')}"
        if slots:
            text += (f" It marks {len(slots)} text slot{'s' if len(slots) != 1 else ''} to write with write_tracker_text and "
                     f"output_revision {output['revision']}: {', '.join(slots)}.")
        return text
    return f"The script of {label} ran ({trigger}). Its output stays at revision {(output or {}).get('revision')}."


def run_script_tracker(company, location_id, tracker, period, data, *, sandbox=None, clock=time.time, signals=None):
    """One script tracker inside a pass. Synchronous: the pass calls it in its thread.

    Returns {'script_run': what the run did, 'output': the stored output when one stands,
    'error': the sentence of a run that stopped}. A tracker with a record and no published script
    returns {'script_run': None}. The job itself runs on the service's event loop.

    When it runs. On request (`manual`), always. With the host's event runs on, in every pass.
    Otherwise once for each local day of the location: in a pass that stands for the day (the
    `day` pass, or an `events` pass at or after the location's daily time) while the script's run
    for that day is still owed. The run is owed until the sandbox has completed one for the day;
    a job that had to wait is run by the location's next pass, and the entry's `retry` says the
    tracker service asks for that pass by itself.
    """
    from concurrent.futures import TimeoutError as Late
    from core_trackers.run import PASS
    import tracker_runner as runner
    sandbox = sandbox or box()
    scripts = TrackerScripts(company)
    head = scripts.head(tracker)
    if not head or head.get('published_revision') is None or not (tracker.get('processor') or {}).get('sandbox'):
        return {'script_run': None}
    revision, policy = head['published_revision'], head['policy']
    context = PASS.get() or {}
    trigger, today = context.get('trigger', 'manual'), context.get('day')
    detail = {'key': tracker['key'], 'tracker_id': tracker['id'], 'script_revision': revision, 'trigger': trigger, 'status': None,
              'seconds': 0, 'error': None, 'output': None, 'output_revision': None, 'proposal_id': None, 'wake': None, 'reason': None,
              'retry': False}
    current = scripts.output(tracker)

    def closed(status, **more):
        detail.update(status=status, **more)
        return {'script_run': detail, 'output': current, 'error': None}

    def later(message, *, retry=False):
        """A run that waits: its day stays owed, and with `retry` the service asks for the next pass."""
        granted = scripts.note_day(tracker, today, done=False, retry=retry)
        return {'reason': f'{message} {RETRY_SOON if granted else RETRY_NEXT}', 'retry': granted}

    was_paused = (head.get('health') or {}).get('paused')
    if was_paused:
        return closed('paused', reason=pause_reason(was_paused))
    if trigger not in {'events', 'day', 'manual'}:
        return closed('waiting', reason=HOST_RUN)
    if trigger != 'manual' and not sandbox.event_runs:
        mine = head.get('day') or {}
        if today is not None and mine.get('day') == today and mine.get('done'):
            return closed('waiting', reason=EVENT_RUNS_WAIT)
        if not context.get('daily', trigger == 'day'):
            return closed('waiting', reason=DAILY_TIME_WAIT)
    source = scripts.revision(tracker, revision)['source']
    periods = job_periods(company, location_id, data, period)
    document = build_input(data, tracker, periods, prior=prior_of(current))
    input_cameras = sorted({row['id'] for row in data.cameras} | set((current or {}).get('camera_ids') or ()))
    marked = input_digest(document)
    good = (head.get('health') or {}).get('last_good') or {}
    if good.get('script_revision') == revision and good.get('input_sha256') == marked:
        scripts.note_day(tracker, today, done=True)
        return closed('reused', output_revision=(current or {}).get('revision'), output='unchanged',
                      reason='The events and records match the last good run. Its result stands.')
    if sandbox.loop is None:
        return closed('waiting', **later(FAULTS['sandbox_offline']))
    remaining = context['deadline'] - context['clock']() if context.get('deadline') is not None else SCRIPT_PASS_SECONDS
    if remaining < MIN_JOB_SECONDS:
        return closed('waiting', **later(BUDGET_WAIT, retry=True))
    def perform(control=None):
        nonlocal head, document
        if control is not None:
            control.before_publish()
            document = control.document
        admitted = False
        if not sandbox.enabled:
            result = {'status': 'unavailable', 'error': fault('sandbox_offline'), 'seconds': 0, 'started': False}
        elif not scripts.admit_job(tracker, most=JOBS_PER_DAY_EVENTS if sandbox.event_runs else JOBS_PER_DAY):
            return closed('waiting', **later(DAILY_LIMIT))
        else:
            admitted = True
            ledger = runner.TrackerRuns(company.state)
            def opened(job):
                ledger.job_open(job, owner=company.owner, collection=company.collection, location_id=location_id, tracker_id=tracker['id'])
                if control is not None:
                    control.opened(job)
            def job_closed(job):
                ledger.job_close(job)
                if control is not None:
                    control.closed(job)
            started, halt = clock(), control.halt if control is not None else threading.Event()
            # The host waits for the job inside what is left of the pass's script time; the job keeps a full run's time.
            seconds = max(MIN_JOB_SECONDS - 10, min(JOB_SECONDS, remaining - 10))
            future = asyncio.run_coroutine_threadsafe(sandbox.run(company_id=company.id, owner=company.owner, collection=company.collection,
                tracker_id=tracker['id'], revision=revision, source=source, document=document, cancelled=halt, opened=opened,
                closed=job_closed, wait_seconds=SLOT_WAIT_SECONDS, seconds=seconds), sandbox.loop)
            slow = {'status': 'unavailable', 'error': fault('sandbox_busy'), 'seconds': 0, 'started': True}
            try:
                result = future.result(timeout=SLOT_WAIT_SECONDS + seconds + HOST_GRACE_SECONDS)
            except Late:
                # The sandbox itself is slow: the job is asked to stop and its own answer says whether the broker took it.
                halt.set()
                try:
                    result = {**slow, 'started': bool(future.result(timeout=HOST_GRACE_SECONDS).get('started'))}
                except Exception:
                    future.cancel()
                    result = slow
                result['seconds'] = round(clock() - started, 3)
            except Exception:
                result = {'status': 'unavailable', 'error': fault('sandbox_offline'), 'seconds': round(clock() - started, 3), 'started': False}
        if admitted and result['status'] == 'unavailable' and not result.get('started'):
            scripts.refund_job(tracker)  # The day's number counts the jobs the broker took.
        if control is not None:
            control.before_publish()
        verdict = evaluate(result, tracker, document)
        detail.update(status=verdict['status'], seconds=result.get('seconds', 0), error=_public_error(verdict['error']))
        output, proposal, sentence = current, None, None
        if verdict['status'] == 'succeeded':
            bundle = _bundle(periods, verdict['accepted'])
            if policy['publish'] == 'automatic':
                output = publish_bundle(scripts, data, tracker, bundle, script_revision=revision)
                scripts.drop_proposal(tracker)
                changed = current is None or output['revision'] != current['revision']
                detail.update(output='published' if changed else 'unchanged', output_revision=output['revision'])
            else:
                pending = scripts.proposal(tracker)
                shown, content, unwritten = preview_bundle(scripts, data, tracker, bundle, script_revision=revision, written=((pending or {}).get('texts'),))
                if current is not None and current['content_sha256'] == content:
                    # The day stands as published; the week, the month and the year still move with the calendar.
                    scripts.put_readings(tracker, current, readings_of(data, tracker, bundle, script_revision=revision), revision)
                    scripts.drop_proposal(tracker)
                    detail.update(output='unchanged', output_revision=current['revision'])
                else:
                    # A proposal is named by what the script computed. A text written into it leaves its name as it is.
                    identity = 'proposal-' + unwritten[:16]
                    if pending and pending['proposal_id'] == identity:
                        proposal = pending
                        detail.update(output='unchanged', proposal_id=identity)
                    else:
                        proposal = scripts.propose(tracker, {'id': 'proposal-' + _suffix(tracker), 'proposal_id': identity,
                            'tracker_id': tracker['id'], 'tracker_key': tracker['key'], 'script_revision': revision, 'run_id': context.get('run_id'),
                            'at': iso(clock()), 'period': shown['tile']['period'], 'summary': shown['tile']['summary'],
                            'value': shown['tile']['value'], 'unit': shown['tile']['unit'], 'state': shown['tile']['state'],
                            # A proposal is read with every camera of its location, like the output it becomes.
                            'camera_ids': runner.location_cameras(company.state, company.collection, location_id),
                            'texts': shown['canvas'].get('texts') or [], 'bundle': bundle})
                        detail.update(output='proposed', proposal_id=identity)
        elif verdict['status'] in {'failed', 'rejected'}:
            sentence = _sentence(verdict['error'])
        else:
            code = (verdict['error'] or {}).get('code')
            detail.update(**later((verdict['error'] or {}).get('message') or FAULTS['sandbox_offline'], retry=code in TRANSIENT))
        if verdict['status'] in {'succeeded', 'failed', 'rejected'}:
            scripts.note_day(tracker, today, done=True)
        head = scripts.after_run(tracker, revision, detail, error=verdict['error'], input_sha256=marked) or head
        changed = detail['output'] in {'published', 'proposed'}
        stopped = detail['status'] in {'failed', 'rejected'}
        paused_now = bool((head.get('health') or {}).get('paused'))
        wanted = {'never': False, 'on_failure': stopped, 'on_change': changed or paused_now,
                  'every_run': detail['status'] in {'succeeded', 'failed', 'rejected'}}[policy['wake']]
        if control is not None:
            control.delivery(wanted, wake_text(tracker, detail, head, error=verdict['error'], output=output, proposal=proposal))
        elif wanted:
            detail['wake'] = wake_agent(company, tracker, event_id=f"{context.get('run_id') or 'run-' + secrets.token_hex(8)}:{tracker['key']}",
                                        text=wake_text(tracker, detail, head, error=verdict['error'], output=output, proposal=proposal),
                                        camera_ids=input_cameras)
        return {'script_run': detail, 'output': output, 'error': sentence}

    if signals is not None:
        return signals.execute(company, tracker, document, source, context, perform, current=current)
    return perform()


def sandbox_processor(sandbox=None):
    """The `custom` callback of `run_trackers`: a company's script tracker, run in the sandbox."""
    def custom(tracker, company, location_id, period, data):
        return run_script_tracker(company, location_id, tracker, period, data, sandbox=sandbox)
    return custom


async def test_script(company, tracker, *, expected_revision, cancelled=None, sandbox=None, clock=time.time):
    """Run the head revision in the sandbox over the location's present data. Nothing is published.

    Returns status `passed`, `failed`, `rejected` or `unavailable`, the tile and canvas the day
    would publish, each period's reading in brief, every error with its line, and the log. A test
    that ran to its end is a row of the location's run history with the trigger `test`; one the
    sandbox had to turn away, or the turn stopped, answers `unavailable` and leaves the history
    and the revision's last test as they are.

    A test reads the rows of every camera of the location and answers with what they hold, so the
    member who tests is one whose present access covers them all (`tracker_runner.pass_access`).
    """
    from core_trackers import common
    import tracker_runner as runner
    sandbox = sandbox or box()
    scripts = TrackerScripts(company)
    location_id = tracker['location_id']
    limits = {'seconds': RUN_SECONDS, 'output_bytes': OUTPUT_BYTES, 'memory_mb': MEMORY_MB}

    def prepare():
        head = scripts.head(tracker)
        if head is None:
            raise ValueError(SAVE_FIRST)
        if type(expected_revision) is not int or expected_revision != head['revision']:
            raise ValueError(READ_AGAIN)
        runner.pass_access(company, location_id)
        if sandbox.enabled and not scripts.admit_job(tracker, most=JOBS_PER_DAY_EVENTS if sandbox.event_runs else JOBS_PER_DAY):
            return head, None, None, None
        data = common.read_location(company, location_id)
        periods = job_periods(company, location_id, data)
        return head, data, periods, build_input(data, tracker, periods, prior=prior_of(scripts.output(tracker)))

    started_at = clock()
    head, data, periods, document = await asyncio.to_thread(prepare)
    ledger = runner.TrackerRuns(company.state)
    if document is None:
        result = {'status': 'unavailable', 'error': fault('daily_limit'), 'seconds': 0}
    else:
        opened = lambda job: ledger.job_open(job, owner=company.owner, collection=company.collection, location_id=location_id, tracker_id=tracker['id'])
        result = await sandbox.run(company_id=company.id, owner=company.owner, collection=company.collection, tracker_id=tracker['id'],
            revision=head['revision'], source=head['source'], document=document, cancelled=cancelled, opened=opened, closed=ledger.job_close,
            wait_seconds=TEST_SLOT_WAIT_SECONDS)

    def conclude():
        taken = bool(result.get('started', result['status'] != 'unavailable'))
        if document is not None and sandbox.enabled and not taken:
            scripts.refund_job(tracker)
        verdict = evaluate(result, tracker, document) if document is not None else {'status': 'unavailable', 'error': result['error'],
                                                                                     'contract_errors': [], 'accepted': [], 'log': None}
        status = {'succeeded': 'passed'}.get(verdict['status'], verdict['status'])
        # A test the turn stopped says nothing about the script: it is answered and the last test stands.
        halted = status == 'failed' and (verdict['error'] or {}).get('code') == 'stopped'
        if halted:
            status = 'unavailable'
        answer = {'status': status, 'revision': head['revision'], 'run_id': None, 'seconds': result.get('seconds', 0),
                  'period': periods[0] if periods else None, 'preview': None, 'readings': [], 'error': None, 'contract_errors': verdict['contract_errors'],
                  'log': verdict.get('log') or (verdict['error'] or {}).get('log') or '', 'limits': limits}
        if document is not None:
            answer['input'] = {'bytes': len(canonical(document)), 'counts': {'events': len(document['events']),
                **{kind: len(rows_) for kind, rows_ in document['records'].items()}}, 'truncated': document['limits']['truncated']}
        if verdict['error']:
            answer['error'] = {k: verdict['error'].get(k) for k in ('code', 'message', 'line', 'traceback', 'period')}
        if status == 'passed':
            bundle = _bundle(periods, verdict['accepted'])
            shown, _, _ = preview_bundle(scripts, data, tracker, bundle, script_revision=head['revision'])
            canvas = {**shown['canvas'], 'rows': shown['canvas'].get('rows', [])[:20]}
            answer['preview'] = {'tile': {**shown['tile'], 'events': shown['tile'].get('events', [])[:20]}, 'canvas': canvas}
            answer['readings'] = [{'period': period['name'], 'label': period['label'], 'value': entry['value'], 'unit': entry['unit'],
                                   'state': entry['state'], 'summary': entry['summary']} for period, entry in zip(periods, verdict['accepted'])]
            answer['next_step'] = 'Publish this revision with publish_tracker_script.'
        elif status in {'failed', 'rejected'}:
            answer['next_step'] = 'Correct the script, save it as the next revision and test again.'
        elif halted:
            answer['next_step'] = 'Test this revision again with test_tracker_script.'
        if status not in {'passed', 'failed', 'rejected'}:
            return answer  # No test ran to its end: the history keeps its rows for the runs and tests that did.
        entry = {'key': tracker['key'], 'tracker_id': tracker['id'], 'script_revision': head['revision'], 'trigger': 'test', 'status': status,
                 'seconds': result.get('seconds', 0), 'error': _public_error(verdict['error']), 'output': None, 'output_revision': None,
                 'proposal_id': None, 'wake': None, 'reason': None}
        row = ledger.record(company.owner, company.collection, location_id, trigger='test', status='completed' if status == 'passed' else 'failed',
            started_at=started_at, wall_ms=round((clock() - started_at) * 1000), period=periods[0] if periods else None, scripts=[entry],
            error=None if status == 'passed' else _sentence(verdict['error']))
        answer['run_id'] = row['id']
        scripts.record_test(tracker, head['revision'], status, row['id'])
        return answer

    return await asyncio.to_thread(conclude)


def publish_script(company, tracker, *, expected_revision):
    """Make the head revision the one that runs: the script's record, then the tracker's processor layer.

    The tracker record points at the script through `processor.sandbox`. Its `processor.script`, the
    name this service imports for a Core Tracker, stays as the record has it.
    """
    from core_trackers import update_tracker
    head = TrackerScripts(company).publish(tracker, expected_revision=expected_revision)
    update_tracker(company, tracker['location_id'], tracker['key'], {'processor': {'reads': ['events'], 'summary': 'script',
        'sandbox': {'kind': KIND, 'script_id': head['id'], 'revision': head['published_revision']}}})
    return head


def approve_output(company, tracker, proposal_id):
    """Publish the proposal a review-mode script is waiting with. Returns the stored output.

    The output is the record of every camera of its location, so the member who approves is one
    whose present access covers them all, as a pass's is.
    """
    from core_trackers import common
    import tracker_runner as runner
    runner.pass_access(company, tracker['location_id'])
    scripts = TrackerScripts(company)
    proposal = scripts.proposal(tracker)
    if proposal is None or proposal['proposal_id'] != proposal_id:
        raise ValueError(PROPOSAL_MOVED)
    data = common.read_location(company, tracker['location_id'])
    output = publish_bundle(scripts, data, tracker, proposal['bundle'], script_revision=proposal['script_revision'],
                            written=(proposal.get('texts'),))
    scripts.drop_proposal(tracker, proposal_id)
    return output


def write_text(company, tracker, slot, text, *, output_revision=None, proposal_id=None, clock=time.time):
    """Store a written text in one slot of the published output, or of the waiting proposal.

    The caller names what the text was written for: the output revision whose facts it read, or
    the proposal. A text for an output is stored while the slot's facts in the output that stands
    now are the facts of the revision named, and it gives the output its next revision, so every
    stored revision stays as it was written. The read, the comparison and the write are one
    transaction on the store.
    """
    import tracker_runner as runner
    runner.pass_access(company, tracker['location_id'])
    scripts = TrackerScripts(company)
    text = text.strip()
    if not 1 <= len(text) <= TEXT_CHARS:
        raise ValueError(f'Write 1 to {TEXT_CHARS} characters.')
    if (output_revision is None) == (proposal_id is None):
        raise ValueError(TEXT_FOR)
    suffix, now = _suffix(tracker), iso(clock())
    stamp_ = lambda entries: [{**entry, 'text': text, 'written_at': now} if entry['slot'] == slot else entry for entry in entries]
    facts_of = lambda entries: next((canonical(entry.get('facts')) for entry in entries or [] if entry.get('slot') == slot), None)

    def named(entries):
        if facts_of(entries) is None:
            slots = ', '.join(entry['slot'] for entry in entries or [])
            raise ValueError(f'Choose one of the text slots this output marks: {slots}.' if slots else
                             'This output marks its text slots in the script: return texts from run for the day.')

    def change(head, put):
        if proposal_id is not None:
            proposal = put.get('tracker_output_proposal', 'proposal-' + suffix)
            if proposal is None or proposal['proposal_id'] != proposal_id:
                raise ValueError(PROPOSAL_MOVED)
            named(proposal.get('texts'))
            put('tracker_output_proposal', {**proposal, 'texts': stamp_(proposal['texts'])})
            return {'stored_with': 'proposal', 'proposal_id': proposal_id, 'slot': slot}
        output = put.get('tracker_output', 'output-' + suffix)
        if output is None or type(output_revision) is not int or output_revision > output['revision']:
            raise ValueError(TEXT_MOVED)
        texts = (output.get('canvas') or {}).get('texts') or []
        read = output if output_revision == output['revision'] else put.get('tracker_output_revision', f"{output['id']}-r{output_revision}")
        if read is None:
            raise ValueError(TEXT_MOVED)
        named((read.get('canvas') or {}).get('texts'))
        if facts_of(texts) != facts_of((read.get('canvas') or {}).get('texts')):
            raise ValueError(TEXT_MOVED)  # The slot's facts moved since the revision this text was written for.
        if any(entry['slot'] == slot and entry.get('text') == text for entry in texts):
            return {'stored_with': 'output', 'output_revision': output['revision'], 'slot': slot}
        revision = output['revision'] + 1
        tile, canvas = {**output['tile'], 'revision': revision}, {**output['canvas'], 'texts': stamp_(texts), 'revision': revision}
        value = {**output, 'revision': revision, 'tile': tile, 'canvas': canvas, 'content_sha256': content_of(tile, canvas)}
        put('tracker_output', value)
        put('tracker_output_revision', {**value, 'id': f"{output['id']}-r{revision}"})
        readings = put.get('tracker_script_readings', 'readings-' + suffix)
        if readings and readings.get('output_revision') == output['revision']:
            put('tracker_script_readings', {**readings, 'output_revision': revision})  # The readings follow their output.
        return {'stored_with': 'output', 'output_revision': revision, 'slot': slot}
    with writing(tracker):
        return scripts._change(tracker, change)


# ── The tool family ────────────────────────────────────────────────────────────────────────────

def _tool(name, description, properties, required=()):
    return {'type': 'function', 'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}


def specs(write=True):
    tracker = {'tracker_id': {'type': 'string', 'minLength': 1, 'maxLength': 80, 'description': 'The id of one of the company’s own trackers, from list_trackers.'}}
    revision = {'expected_revision': {'type': 'integer', 'minimum': 0, 'description': 'The script revision get_tracker_script returned; 0 before the first save.'}}
    publish = {'publish': {'type': 'string', 'enum': list(PUBLISH_MODES)}}
    wake = {'wake': {'type': 'string', 'enum': list(WAKE_MODES)}}
    values = [_tool('get_tracker_script', 'Read the processing script of one of the company’s own trackers: its source and revision, the published revision, '
        'its publish and wake policies, its health and last test, the text slots of its output, a waiting proposal and its recent runs. '
        'The result carries the script contract: def run(input) -> output, the input and output fields, the limits and what a stopped run does. '
        'The output, the proposal and the message of a stopped run are read with the cameras of the location. '
        'A Tracking Agent that has yet to save a script gets the contract and its next step by its own id.',
        {**tracker, 'revision': {'type': 'integer', 'minimum': 1, 'description': 'A saved revision to read the source of. The newest when left out.'}}, ['tracker_id'])]
    if write:
        values.extend([
            _tool('save_tracker_script', 'Save Python source as the next draft revision of a tracker’s processing script. Read get_tracker_script first for the contract '
                'and the current revision; the first save takes expected_revision 0. Saving stores text: the script runs in the sandbox, first with test_tracker_script. '
                'For a Tracking Agent with a location to choose, name it with location_id.',
                {**tracker, 'source': {'type': 'string', 'minLength': 1, 'maxLength': SCRIPT_BYTES}, **revision, **publish, **wake,
                 'location_id': {'type': 'string', 'minLength': 1, 'maxLength': 120}}, ['tracker_id', 'source', 'expected_revision']),
            _tool('test_tracker_script', 'Run a script revision in the sandbox over the location’s present data, for the day, the week, the month and the year. '
                'Returns passed, failed, rejected or unavailable, the tile and canvas the day would publish, each error with its line, and the log. '
                'A test publishes zero outputs and is free of model calls. It reads every camera of the location, so it runs for a member whose access covers them.',
                {**tracker, **revision}, ['tracker_id', 'expected_revision']),
            _tool('publish_tracker_script', 'Make a script revision with a passing test the one the tracker runner runs. '
                'From then on it runs once each local day of the location, on request, and on new events once this host has event runs switched on.',
                {**tracker, **revision}, ['tracker_id', 'expected_revision']),
            _tool('set_tracker_script_policy', 'Choose how a script’s runs reach the surface and the agent. publish: automatic (a valid output is published by the run) '
                'or review (it waits as a proposal for approve_tracker_output). wake: never, on_failure, on_change or every_run; '
                'a wake reaches a tracker’s active Tracking Agent.',
                {**tracker, **revision, **publish, **wake}, ['tracker_id', 'expected_revision']),
            _tool('approve_tracker_output', 'Publish the output a review-mode script proposed. Pass the proposal_id get_tracker_script shows. '
                'The page shows the tracker’s row at once.',
                {**tracker, 'proposal_id': {'type': 'string', 'pattern': r'^proposal-[a-f0-9]{16}$'}}, ['tracker_id', 'proposal_id']),
            _tool('write_tracker_text', 'Write the text of one slot a script’s output marks, from the slot’s facts. Name what the text is written for: '
                'output_revision, the revision of the published output whose facts you read, or proposal_id for a waiting proposal. '
                'A text for an output is stored while the slot’s facts still stand, and gives the output its next revision.',
                {**tracker, 'slot': {'type': 'string', 'pattern': r'^[a-z][a-z0-9_]{0,39}$'}, 'text': {'type': 'string', 'minLength': 1, 'maxLength': TEXT_CHARS},
                 'output_revision': {'type': 'integer', 'minimum': 1, 'description': 'The output revision get_tracker_script or the wake showed with the slot’s facts.'},
                 'proposal_id': {'type': 'string', 'pattern': r'^proposal-[a-f0-9]{16}$', 'description': 'The waiting proposal the text is written for.'}},
                ['tracker_id', 'slot', 'text'])])
    return values


def _record(context, tracker_id):
    """The saved tracker record a tool names: by its own id, or by the id of its Tracking Agent."""
    from core_trackers import tracker_records
    return next((record for record in tracker_records(context.state, context.owner, context.dataset)
                 if tracker_id in (record['id'], record.get('agent_id'))), None)


def _script_runs(context, record, limit=10):
    """The tracker's newest script runs, each as one entry of the location's run history."""
    import tracker_runner as runner
    rows_ = []
    for run in runner.TrackerRuns(context.state).runs(context.owner, context.dataset, record['location_id'], limit=100):
        for entry in run.get('scripts') or []:
            if entry.get('key') == record['key']:
                rows_.append({'run_id': run['id'], 'trigger': run['trigger'], 'started_at': run['started_at'], **{k: entry.get(k) for k in
                    ('status', 'seconds', 'script_revision', 'error', 'output', 'output_revision', 'proposal_id', 'wake', 'reason')}})
        if len(rows_) >= limit:
            break
    return rows_[:limit]


def _unlinked(context, tracker_id):
    """A Tracking Agent of the reader that has yet to get its tracker record, by the id `list_trackers` gives it."""
    if context.agents is None:
        return None
    return next((agent for agent in context.agents.list(owner_principal_id=context.owner)
                 if agent['id'] == tracker_id and agent.get('agent_kind') == 'tracker' and agent.get('dataset') == context.dataset), None)


def script_camera_ids(state, owner, record):
    """The cameras whose data a script's current output, proposal or run message may contain."""
    import tracker_runner as runner
    cameras = set(runner.location_cameras(state, record['collection'], record['location_id']))
    for kind, prefix in (('tracker_output', 'output-'), ('tracker_output_proposal', 'proposal-')):
        saved = _saved(state, owner, kind, prefix + _suffix(record))
        cameras.update((saved or {}).get('camera_ids') or ())
    return sorted(cameras)


def reads_cameras(context, record, *records_):
    """Whether the reader's present access covers every camera a script's words come from: the
    cameras of the location, and the ones a stored output or proposal names. A script copies what
    it reads into its summary, its facts and the message of its own exception, so each of those is
    read with the cameras, like a Core Tracker's output."""
    cameras = set(script_camera_ids(context.state, context.owner, record))
    for value in records_:
        cameras |= set((value or {}).get('camera_ids') or ())
    return context.allowed(record['location_id'], sorted(cameras))


def read(context, name, args):
    from core_trackers import LOCKED
    record = _record(context, args['tracker_id'])
    limits = {'source_bytes': SCRIPT_BYTES, 'seconds': RUN_SECONDS, 'memory_mb': MEMORY_MB, 'output_bytes': OUTPUT_BYTES,
              'input_bytes': INPUT_BYTES, 'rows': ROWS, 'periods': ['day', 'week', 'month', 'year']}
    if record is None:
        agent = _unlinked(context, args['tracker_id'])
        if agent is None:
            raise ValueError(UNKNOWN)
        # The agent reads the contract before its first save, which gives it its tracker record.
        return {'tracker_id': None, 'agent_id': agent['id'], 'key': None, 'label': agent.get('title'), 'location_id': None, 'core': False,
                'event_types': [], 'contract': TRACKER_SCRIPT_INSTRUCTIONS, 'example': EXAMPLE_SOURCE, 'limits': limits, 'script': None,
                'output': None, 'proposal': None, 'runs': [], 'next_step': UNLINKED}
    # A tracker of a location outside the reader's present access reads like one that is absent.
    if not context.allowed(record['location_id']):
        raise ValueError(UNKNOWN)
    answer = {'tracker_id': record['id'], 'key': record['key'], 'label': record['label'], 'location_id': record['location_id'],
              'core': record['core'], 'event_types': record['tracker'].get('event_types', []), 'contract': TRACKER_SCRIPT_INSTRUCTIONS,
              'example': EXAMPLE_SOURCE, 'limits': limits}
    if record['core']:
        return {**answer, 'script': None, 'locked': LOCKED}
    head = _saved(context.state, context.owner, 'tracker_script', script_id(record))
    shown = head
    if head and args.get('revision') not in (None, head['revision']):
        shown = _saved(context.state, context.owner, 'tracker_script_revision', f"{head['id']}-r{args['revision']}")
        if shown is None:
            raise ValueError('Choose a saved revision of this script.')
    output = _saved(context.state, context.owner, 'tracker_output', 'output-' + _suffix(record))
    proposal = _saved(context.state, context.owner, 'tracker_output_proposal', 'proposal-' + _suffix(record))
    covered = reads_cameras(context, record, output, proposal)
    held = bool(output or proposal or ((head or {}).get('health') or {}).get('last_run'))
    if not covered:
        output = proposal = None  # An output, a proposal and a script's message are read with the cameras they cover.
    tile = (output or {}).get('tile') or {}
    return {**answer,
        'script': {**describe(head, words=covered), 'bytes': shown['bytes'], 'source_sha256': shown['source_sha256'], 'source_revision': shown['revision'],
                   'source': shown['source']} if head else None,
        'output': {'revision': output['revision'], 'period': output.get('period'), 'state': tile.get('state'), 'value': tile.get('value'),
                   'unit': tile.get('unit'), 'summary': tile.get('summary'), 'script_revision': tile.get('script_revision'),
                   'texts': (output.get('canvas') or {}).get('texts') or []} if output else None,
        'proposal': {k: proposal.get(k) for k in ('proposal_id', 'script_revision', 'at', 'period', 'state', 'value', 'unit', 'summary', 'texts')} if proposal else None,
        **({'output_access': CAMERA_SCOPE} if held and not covered else {}),
        'runs': _script_runs(context, record),
        'next_step': None if head else 'Save the first revision with save_tracker_script and expected_revision 0.'}


def _installed(context):
    """The locations whose Core Trackers the writer installed: where that member's trackers run."""
    import tracker_runner as runner
    return {place for member, collection, place in runner.installed_locations(context.state)
            if member == context.owner and collection == context.dataset}


def _writable(context, company, args, *, link=False):
    """The company's own tracker a write names, with the writer's authority over it checked."""
    from core_trackers import LOCKED
    record = _record(context, args['tracker_id'])
    installed = _installed(context)
    if record is None:
        agent = _unlinked(context, args['tracker_id'])
        if agent is None:
            raise ValueError(UNKNOWN)
        if not link:
            raise ValueError(SAVE_FIRST)
        location_id = args.get('location_id') or (next(iter(installed)) if len(installed) == 1 else None)
        if location_id is None:
            raise ValueError(LOCATION_NEEDED if installed else INSTALL_FIRST)
        record = link_agent(context, company, location_id, agent, installed=installed)
    if not context.allowed(record['location_id']):
        raise ValueError(UNKNOWN)
    if record['core']:
        raise ValueError(LOCKED)
    try:
        company.locations.authorized(context.owner, context.dataset, record['location_id'], write=True)
    except ValueError:
        raise ValueError(MANAGEMENT) from None
    if record['location_id'] not in installed:
        raise ValueError(INSTALL_FIRST)
    return record


def link_check(context, company, location_id, *, installed=None):
    """Whether the writer may give a Tracking Agent its tracker record at a location: a location in
    their access that they manage and whose Core Trackers they installed, with room for one more.
    Raises with the sentence that says what is needed."""
    from core_trackers import installed_trackers
    installed = _installed(context) if installed is None else installed
    context.require(location_id)
    company.locations.authorized(context.owner, context.dataset, location_id)  # a location of this business, in the writer's access
    try:
        company.locations.authorized(context.owner, context.dataset, location_id, write=True)
    except ValueError:
        raise ValueError(LINK_MANAGEMENT) from None
    if location_id not in installed:
        raise ValueError(INSTALL_FIRST)
    if sum(1 for tracker in installed_trackers(company, location_id) if not tracker['core']) >= 50:
        raise ValueError(FULL)


def link_agent(context, company, location_id, agent, *, installed=None):
    """The tracker record of a Tracking Agent, written at a location through `add_tracker` the first time.

    Its events are the types of the agent's observation profiles; `save_tracker` changes them later.
    """
    from core_trackers import agent_tracker, link_tracking_agent
    linked = agent_tracker(context.state, context.owner, context.dataset, agent['id'])
    if linked is not None:
        return linked
    link_check(context, company, location_id, installed=installed)
    kinds = []
    for profile_id in agent.get('profile_ids') or []:
        try:
            vocabulary = company.monitoring.get_profile(context.owner, profile_id).get('vocabulary') or []
        except (ValueError, KeyError):
            continue
        kinds.extend(entry.get('type') for entry in vocabulary if isinstance(entry, dict))
    kinds = sorted({kind for kind in kinds if isinstance(kind, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,59}', kind)})[:24]
    return link_tracking_agent(company, location_id, agent, event_types=kinds)


def promote_link(context, company, agent, location_id=None):
    """After a chat became a Tracking Agent: its tracker record, where a location is named or the
    writer has one installed location. Returns {'tracker': record or None, 'next_step': sentence or None}."""
    installed = _installed(context)
    if location_id is None:
        if len(installed) != 1:
            return {'tracker': None, 'next_step': LOCATION_NEEDED if installed else INSTALL_FIRST}
        location_id = next(iter(installed))
    record = link_agent(context, company, location_id, agent, installed=installed)
    return {'tracker': {k: record[k] for k in ('id', 'key', 'label', 'location_id', 'revision')} | {'event_types': record['tracker']['event_types']},
            'next_step': None}


def _public_head(head):
    return {**describe(head), 'bytes': head['bytes'], 'source_sha256': head['source_sha256']}


def _page(company, record):
    """What a tool says of the page after it changed an output: the tracker's own row is assembled
    again from the stored output, with no pass and no script run."""
    shown = show_on_page(company, record)
    return {'refreshed': shown, 'note': PAGE_SHOWS if shown else PAGE_AFTER_PASS}


def call(context, name, args):
    """The family's writes, in the hosted parent conversation. A Core Tracker answers each with LOCKED."""
    company = context.company()
    record = _writable(context, company, args, link=name == 'save_tracker_script')
    scripts = TrackerScripts(company)
    base = {'tracker_id': record['id'], 'key': record['key'], 'location_id': record['location_id']}
    if name == 'save_tracker_script':
        policy = {k: args[k] for k in ('publish', 'wake') if k in args}
        try:
            head = scripts.save(record, args['source'], expected_revision=args['expected_revision'], policy=policy, chat_id=context.chat_id)
        except ScriptError as exc:
            return {**base, 'status': 'rejected', 'error': exc.public(), 'next_step': 'Correct the source and save it again with the same expected_revision.'}
        return {**base, 'status': 'saved', 'script': _public_head(head), 'next_step': 'Test this revision with test_tracker_script.'}
    if name == 'set_tracker_script_policy':
        changes = {k: args[k] for k in ('publish', 'wake') if k in args}
        if not changes:
            raise ValueError('Name the policy to change: publish, wake or both.')
        return {**base, 'script': _public_head(scripts.set_policy(record, changes, expected_revision=args['expected_revision']))}
    if name == 'publish_tracker_script':
        head = publish_script(company, record, expected_revision=args['expected_revision'])
        import tracker_runner as runner
        ledger = runner.TrackerRuns(context.state)
        ledger.request(context.owner, context.dataset, record['location_id'], by=context.owner, only=[record['key']])
        service = ledger.service()['state']
        return {**base, 'status': 'published', 'script': _public_head(head),
                'runs': {'requested': True, 'service': service, 'event_runs': 'on' if box().event_runs else 'off',
                         'note': 'The tracker runner runs the script in its next sweep, then once each local day of the location and on request.'
                                 + (' New events start a run too.' if box().event_runs else ' ' + EVENT_RUNS_NOTE)}}
    if name == 'approve_tracker_output':
        output = approve_output(company, record, args['proposal_id'])
        return {**base, 'status': 'published', 'output': {'revision': output['revision'], 'period': output['period'],
                'summary': output['tile']['summary'], 'value': output['tile']['value'], 'unit': output['tile']['unit']},
                'page': _page(company, record)}
    if name == 'write_tracker_text':
        stored = write_text(company, record, args['slot'], args['text'], output_revision=args.get('output_revision'),
                            proposal_id=args.get('proposal_id'))
        return {**base, **stored, 'page': _page(company, record) if stored['stored_with'] == 'output' else None}
    raise ValueError('Choose a tracker script tool.')


async def acall(context, name, args):
    """The family's awaited write: a test runs its sandbox job on the event loop of the turn."""
    company = context.company()
    record = await asyncio.to_thread(_writable, context, company, args)
    cancelled = (context.run or {}).get('cancel')
    answer = await test_script(company, record, expected_revision=args['expected_revision'], cancelled=cancelled)
    return {'tracker_id': record['id'], 'key': record['key'], 'location_id': record['location_id'], **answer}
