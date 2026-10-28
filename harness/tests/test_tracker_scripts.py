"""A tracker's processing script, as pure functions: the source check, the input document, the
sandbox bundle and the output validator. No sandbox, no store and no model is used here.

The helpers at the top are shared with the other tracker-script tests: a hand-built location, a
script run in a local process, and a sandbox double that runs the real entry file that way.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import layout

sys.path.insert(0, str(Path(__file__).parents[1]))

HERE = Path(__file__).parents[1]
DAY = datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp()
# The copy law's own pattern (tools/copy-gate.py): text a person or a model reads says what is.
NEGATIVE = re.compile(r"(?i)(^|[\s>«(·'\"`])(no|not|nothing|never|nobody|none|don't|doesn't|can't|cannot|won't|isn't|aren't|didn't|wasn't|hasn't|haven't|without|unless)\b(?! *=)")

COUNT = '''def run(input):
    period = input["period"]
    rows = [{"id": event["id"]} for event in input["events"]]
    print("counted", len(rows), "in", period["name"])
    return {"value": len(rows), "unit": "events", "state": "ready", "needs": None,
            "summary": str(len(rows)) + " events, " + period["label"] + ".", "series": [], "events": rows[:40],
            "canvas": {"rows": rows[:500], "total_rows": len(rows), "by_type": [{"type": "spill", "value": len(rows)}]}}
'''


def event(index, kind='spill', t=..., **more):
    return {'id': f'event-{index:04d}', 'type': kind, 'label': kind.capitalize(), 'description': f'Observation {index}',
            'object_description': None, 't': DAY + 3600 + index if t is ... else t, 't_end': DAY + 3601 + index, 'at_s': float(index),
            'end_s': float(index + 1), 'visit_id': f'visit-{index % 3}', 'person_id': index % 3, 'source_id': f'source-{index % 2}',
            'camera_id': 'camera-1', 'thumb': f'/control-room/thumb-{index}.jpg', 'answer_file': '/srv/private/answer.json',
            'data_basis': 'model_output', 'product_type': None, 'product_type_label': None, 'product_id': None, **more}


def location(events=None, **more):
    """A hand-built location: the fields `core_trackers.common.LocationData` carries."""
    from core_trackers.common import LocationData
    tracker = {'id': 'tracker-' + 'a' * 28, 'key': 'spills', 'label': 'Spills', 'core': False, 'location_id': 'location-1',
               'tracker': {'observes': 'Liquid on the floor.', 'stream': 'events', 'event_types': ['spill']},
               'tile': {'family': 'metric', 'unit': 'events'}, 'canvas': {'family': 'occurrences'}}
    visits = [{'id': f'visit-{n}', 't': DAY + 100 * n, 't_end': DAY + 100 * n + 50, 'first_s': 1.0, 'last_s': 51.0, 'source_id': 'source-0',
               'camera_id': 'camera-1', 'person_id': n, 'data_basis': 'model_output'} for n in range(3)]
    values = {'cameras': [{'id': 'camera-1', 'name': 'Door', 'location_id': 'location-1'}], 'visits': visits,
              'events': [event(n) for n in range(5)] if events is None else events, 'items': [], 'baskets': [], 'sales': [],
              'sale_lines': {}, 'shelves': [], 'queues': [], 'catalog': [], 'zones': [], **more}
    company = SimpleNamespace(id='company-1', collection='retail-demo', owner='peter')
    return LocationData(company, 'location-1', {}, [tracker], **values), tracker


def periods_of(day=datetime(2026, 10, 10).date()):
    from core_trackers import common
    import tracker_scripts as scripts
    return [scripts.named('day', common.day_period(day)), scripts.named('week', common.days_period(day, 7)),
            scripts.named('month', common.days_period(day, 30)), scripts.named('year', common.days_period(day, 365))]


def run_local(tmp_path, source, document, *, timeout=20):
    """Run the real entry file over a script in a local process, as the sandbox's worker runs it:
    `python -I rocoto_entry.py` in the source tree. Returns (exit code, envelope)."""
    import tracker_scripts as scripts
    tree = Path(tmp_path) / f'job-{time.monotonic_ns()}'
    (tree / 'input').mkdir(parents=True)
    (tree / scripts.ENTRY).write_text(scripts.ENTRY_SOURCE)
    (tree / scripts.SCRIPT).write_text(source)
    for name, raw in scripts.input_parts(document):
        (tree / name).write_bytes(raw)
    scratch = tree.parent / (tree.name + '-tmp')
    scratch.mkdir()
    done = subprocess.run([sys.executable, '-I', str(tree / scripts.ENTRY)], input=b'{"gateway":{"token":"unused"}}', capture_output=True, cwd=tree,
                          env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(scratch), 'TMPDIR': str(scratch)}, timeout=timeout)
    return done.returncode, json.loads(done.stdout)


class LocalSandbox:
    """A sandbox double for tests: the real entry file and the real script in a local process.

    It stands where `tracker_scripts.Sandbox` stands and answers in the same shape. `answer` replaces
    one job's result with a canned one; `jobs` lists what ran.
    """

    def __init__(self, tmp_path, *, event_runs=False, enabled=True, loop=None):
        self.tmp_path, self.event_runs, self.enabled, self.loop = tmp_path, event_runs, enabled, loop
        self.jobs, self.answer = [], None

    async def run(self, *, company_id, owner, collection, tracker_id, revision, source, document, cancelled=None, opened=None, closed=None,
                  wait_seconds=None, seconds=None):
        import tracker_scripts as scripts
        self.jobs.append({'tracker_id': tracker_id, 'revision': revision, 'periods': [p['name'] for p in document['periods']],
                          'wait_seconds': wait_seconds, 'seconds': seconds})
        started = time.monotonic()
        if self.answer is not None:
            return {'seconds': 0.01, **self.answer}
        code, envelope = await asyncio.to_thread(run_local, self.tmp_path, source, document)
        assert code == 0
        return {'status': 'succeeded', 'results': envelope['results'], 'log': envelope['log'], 'seconds': round(time.monotonic() - started, 3),
                'broker_run_id': 'custom-local', 'input_sha256': scripts.input_digest(document), 'input_bytes': len(scripts.canonical(document))}


@pytest.fixture
def loop_thread():
    """An event loop in its own thread, as the server's loop stands beside a pass's thread."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name='test-loop', daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


# ── The source ──

def test_a_source_is_checked_as_text_and_never_executed(tmp_path):
    import tracker_scripts as scripts
    assert scripts.validate_source(scripts.EXAMPLE_SOURCE) == scripts.EXAMPLE_SOURCE
    assert scripts.validate_source('def run(input, *rest, scale=1):\n    return {}\n')
    flag = tmp_path / 'ran.flag'
    inert = f"open({str(flag)!r}, 'w').write('ran')\nraise SystemExit('module level code')\ndef run(input):\n    return {{}}\n"
    assert scripts.validate_source(inert) == inert and not flag.exists(), 'the source is parsed, never run'
    with pytest.raises(scripts.ScriptError) as fault:
        scripts.validate_source('def run(input):\n    return {\n')
    assert fault.value.code == 'script_syntax' and fault.value.line == 2 and fault.value.public()['line'] == 2
    for source, code in (('', 'script_contract'), ('x = 1\n', 'script_contract'), ('def run(input):\n    return {}\n' + '#' * scripts.SCRIPT_BYTES, 'script_contract'),
                         ('def run(input):\n    return "\x00"\n', 'script_contract'), ('def run(a, b):\n    return {}\n', 'script_contract'),
                         ('def run():\n    return {}\n', 'script_contract'), ('async def run(input):\n    return {}\n', 'script_contract'),
                         ('def run(input):\n    return {}\ndef run(input):\n    return {}\n', 'script_contract'),
                         ('def run(input, *, need):\n    return {}\n', 'script_contract'), (b'def run(input): return {}', 'script_contract'),
                         ('class Holder:\n    def run(self, input):\n        return {}\n', 'script_contract')):
        with pytest.raises(scripts.ScriptError) as fault:
            scripts.validate_source(source)
        assert fault.value.code == code, source[:40]


# ── The input ──

def test_the_input_is_the_trackers_own_events_and_bounded_rows(monkeypatch):
    import tracker_scripts as scripts
    events = [event(n) for n in range(30)] + [event(100 + n, 'pick_up') for n in range(10)] + [event(900, t=None), event(901, t=DAY - 500 * 86400)]
    data, tracker = location(events, sales=[{'id': 'sale-1', 't': DAY + 50, 'total': '4.00', 'data_basis': 'pos'}],
                             sale_lines={'sale-1': [{'id': 'sale-1-0', 'sale_id': 'sale-1', 'label': 'Water', 'data_basis': 'pos'}]})
    periods = periods_of()
    document = scripts.build_input(data, tracker, periods, prior={'revision': 3, 'value': 2, 'unit': 'events', 'state': 'ready', 'period': None})
    assert document['schema'] == 1 and document['period'] == periods[0] and document['periods'] == periods and document['connections'] == {}
    assert document['tracker'] == {'id': tracker['id'], 'key': 'spills', 'label': 'Spills', 'observes': 'Liquid on the floor.',
                                   'event_types': ['spill'], 'unit': 'events', 'tile_family': 'metric', 'canvas_family': 'occurrences'}
    assert document['location'] == {'id': 'location-1', 'company_id': 'company-1', 'collection': 'retail-demo'}
    kinds = {row['type'] for row in document['events']}
    assert kinds == {'spill'} and len(document['events']) == 31, 'the tracker’s event types, inside the window, with the unclocked one'
    assert [row['id'] for row in document['events'][:2]] == ['event-0029', 'event-0028'] and document['events'][-1]['id'] == 'event-0900'
    assert document['events'][0]['at'] == '2026-10-10T01:00:29Z' and document['events'][-1]['at'] is None
    text = json.dumps(document)
    assert 'answer_file' not in text and 'data_basis' not in text and '/srv/private' not in text
    # The host's provenance stays with the host one level down too: an observed item names its answer files there.
    kept = {'id': 'item-1', 'event_ids': ['event-0000'], 'product_type': 'bottle', 'match': {'basis': 'product_type_finder'}}
    inner, _ = location(items=[{**kept, 'data_basis': 'model_output', 'provenance': {'answer_files': ['raw/archive-2026-10-09.jsonl']},
                                'parts': [{'label': 'cap', 'answer_file': 'raw/cap.json', 'provenance': {'kind': 'model'}}]}])
    assert scripts.build_input(inner, tracker, periods)['records']['items'] == [{**kept, 'parts': [{'label': 'cap'}]}]
    assert scripts.INTERNAL == {'answer_file', 'answer_files', 'data_basis', 'provenance'}
    assert document['records']['sale_lines'] == {'sale-1': [{'id': 'sale-1-0', 'sale_id': 'sale-1', 'label': 'Water'}]}
    assert document['records']['cameras'] == [{'id': 'camera-1', 'name': 'Door'}] and len(document['records']['visits']) == 3
    assert document['limits']['rows'] == scripts.ROWS and not any(document['limits']['truncated'].values())
    assert document['limits']['seconds'] == 30 and document['prior']['revision'] == 3
    # An empty list of event types reads every event of the location.
    tracker['tracker']['event_types'] = []
    assert {row['type'] for row in scripts.build_input(data, tracker, periods)['events']} == {'spill', 'pick_up'}
    # The caps hold, and a capped kind is named.
    tracker['tracker']['event_types'] = ['spill']
    monkeypatch.setattr(scripts, 'ROWS', {**scripts.ROWS, 'events': 10})
    capped = scripts.build_input(data, tracker, periods)
    assert len(capped['events']) == 10 and capped['limits']['truncated']['events'] is True and capped['limits']['rows']['events'] == 10
    with pytest.raises(ValueError):
        scripts.build_input(data, tracker, periods + periods)


def test_a_large_location_is_halved_until_it_fits_the_sandbox(monkeypatch):
    import tracker_scripts as scripts
    events = [event(n, description='x' * 900) for n in range(1800)]
    data, tracker = location(events)
    document = scripts.build_input(data, tracker, periods_of())
    raw = scripts.canonical(document)
    assert len(raw) <= scripts.INPUT_BYTES and 0 < len(document['events']) < 1800 and document['limits']['truncated']['events'] is True
    assert document['limits']['rows']['events'] < scripts.ROWS['events']
    parts = scripts.input_parts(document)
    assert [name for name, _ in parts] == [f'input/{n:03d}.json' for n in range(len(parts))] and len(parts) <= 7
    assert all(len(part) <= scripts.PART_BYTES for _, part in parts) and b''.join(part for _, part in parts) == raw
    # The same events and records are the same input, whatever was published before.
    again = scripts.build_input(data, tracker, periods_of(), prior={'revision': 9, 'value': 1, 'unit': None, 'state': 'ready', 'period': None})
    assert scripts.input_digest(again) == scripts.input_digest(document)
    events[-1]['description'] = 'changed'
    assert scripts.input_digest(scripts.build_input(data, tracker, periods_of())) != scripts.input_digest(document)


def test_the_bundle_is_one_the_sandbox_broker_accepts():
    """`snapshot_for` builds what the real worker checks: the request and the bundle, for the job's own scope."""
    import custom_worker
    from custom_execution import IMAGE, RuntimeProfile
    from native_custom_client import request_for
    import tracker_scripts as scripts
    data, tracker = location()
    document = scripts.build_input(data, tracker, periods_of())
    worker_sha = hashlib.sha256(Path(custom_worker.__file__).read_bytes()).hexdigest()
    snapshot = scripts.snapshot_for(company_id='company-1', owner='peter', collection='retail-demo', tracker_id=tracker['id'], revision=4,
        source=COUNT, parts=scripts.input_parts(document), runtime=RuntimeProfile(IMAGE, worker_sha))
    request = request_for(snapshot, scripts.TOOL, {})
    assert custom_worker.request(request)['source_files'] == 3
    value = {'snapshot': snapshot.metadata, 'arguments': {}}
    assert custom_worker.bundle(value, request, company='company-1', owner='peter', image=IMAGE, worker_sha256=worker_sha) == value
    meta = snapshot.metadata
    assert meta['manifest']['entrypoint'] == scripts.ENTRY and meta['target'] is None and meta['secret_fields'] == []
    assert meta['connector_id'] == 'tracker-script-' + 'a' * 28 and meta['revision'] == 4
    assert [item['path'] for item in meta['files']] == ['input/000.json', 'rocoto_entry.py', 'tracker.py']
    assert dict((item.path, item.content) for item in snapshot.files)['tracker.py'] == COUNT.encode()
    # The broker binds a bundle to the scope that asked: another company or owner is refused.
    for other in ({'company': 'company-2', 'owner': 'peter'}, {'company': 'company-1', 'owner': 'mallory'}):
        with pytest.raises(custom_worker.WorkerError):
            custom_worker.bundle(value, request, image=IMAGE, worker_sha256=worker_sha, **other)
    with pytest.raises(custom_worker.WorkerError):
        custom_worker.bundle(value, request, company='company-1', owner='peter', image=IMAGE, worker_sha256='0' * 64)


def test_the_entry_file_is_pinned():
    """The entry runs beside a company's script in every job. A change to it is a reviewed change:
    it moves this hash, and the shim tests run again."""
    import tracker_scripts as scripts
    assert hashlib.sha256(scripts.ENTRY_SOURCE.encode()).hexdigest() == ENTRY_SHA256
    compile(scripts.ENTRY_SOURCE, scripts.ENTRY, 'exec')
    assert '%(' not in scripts.ENTRY_SOURCE and 'import socket' not in scripts.ENTRY_SOURCE


ENTRY_SHA256 = 'aa9d9362756e0220a387b14e9c869bd4365f4701eb8853d39e3c023f8a5b6627'


def test_the_commissioned_sandbox_files_are_the_reviewed_ones():
    """Five files run as root on the staging host. Custom execution stops when `custom_worker.py`
    differs from the broker's copy, so an edit to any of them is a commissioning step: the release
    record names each file's hash, and an edit made by accident turns this red."""
    folder = HERE / 'deploy/native-executor'
    record = json.loads((folder / 'custom-retention-release.json').read_text())
    installed = json.loads((folder / 'TOOLING-PUBLIC-2026-10-05.json').read_text())['host']['broker_sources']
    names = {'native_executor.py', 'native_package_mount.py', 'native_custom_broker.py', 'custom_worker.py', 'broker_service.py'}
    here = {name: hashlib.sha256((folder / name if name == 'broker_service.py' else layout.source(Path(name).stem)).read_bytes()).hexdigest() for name in names}
    assert {name: record['previous_sources'][name] for name in names} == {name: installed[name] for name in names}, 'the record starts from what is installed'
    assert {name: record['sources'][name] for name in names} == here, 'each commissioned file is the one its release record names'
    changed = {name for name in names if record['sources'][name] != record['previous_sources'][name]}
    assert changed == {'native_custom_broker.py'} == set(record['replacements']), 'the retention change is the one edit; the worker is the installed worker'
    assert here['custom_worker.py'] == installed['custom_worker.py']


# ── The output ──

def good(document, **more):
    ids = [row['id'] for row in document['events']]
    return {'value': len(ids), 'unit': 'events', 'state': 'ready', 'needs': None, 'summary': f'{len(ids)} spills on the day.',
            'series': [{'t': '2026-10-10T09:00:00Z', 'value': 3}], 'trend': [{'t': '2026-10-09T00:00:00Z', 'value': 8}],
            'events': [{'id': ids[0], 'label': 'Spill', 'description': 'By the door.'}],
            'canvas': {'rows': [{'id': key} for key in ids] + [{'id': 'visit-1'}], 'total_rows': len(ids) + 1,
                       'by_aisle': [{'aisle': '3', 'value': 5, 'label': 'Aisle 3'}]}, **more}


@pytest.fixture
def case():
    import tracker_scripts as scripts
    data, tracker = location()
    return SimpleNamespace(scripts=scripts, tracker=tracker, data=data, document=scripts.build_input(data, tracker, periods_of()))


def refused(case, output, path=None):
    with pytest.raises(case.scripts.OutputError) as fault:
        case.scripts.validate_output(output, case.tracker, case.document)
    paths = [error['path'] for error in fault.value.errors]
    assert path is None or any(found.startswith(path) for found in paths), paths
    assert all(set(error) == {'path', 'message'} and not NEGATIVE.search(error['message']) for error in fault.value.errors)
    return fault.value.errors


def test_a_valid_output_is_accepted_and_its_rows_are_the_hosts_own(case):
    accepted = case.scripts.validate_output(good(case.document, tile_extra={'aisles': 4}, texts=[{'slot': 'memo', 'facts': {'count': 5}}]), case.tracker, case.document)
    assert set(accepted) == {'value', 'unit', 'state', 'needs', 'summary', 'series', 'trend', 'events', 'canvas', 'tile_extra', 'texts'}
    first = accepted['events'][0]
    host = next(row for row in case.document['events'] if row['id'] == first['id'])
    assert first == {'id': host['id'], 'at': host['at'], 'label': 'Spill', 'description': 'By the door.', 'thumb': host['thumb'],
                     'visitor_id': host['visit_id'], 'source_id': host['source_id'], 'camera_id': 'camera-1', 'person_id': host['person_id'],
                     'at_s': host['at_s'], 'type': 'spill', 'product': None}
    assert accepted['canvas']['rows'][1]['description'] == host['description'] or accepted['canvas']['rows'][1]['description'].startswith('Observation')
    visit = accepted['canvas']['rows'][-1]
    assert visit['id'] == 'visit-1' and visit['visitor_id'] == 'visit-1' and visit['source_id'] == 'source-0' and visit['thumb'] is None
    assert accepted['canvas']['by_aisle'] == [{'aisle': '3', 'value': 5, 'label': 'Aisle 3'}], 'a nested key of the script’s own may be named label or value'
    assert accepted['tile_extra'] == {'aisles': 4} and accepted['texts'] == [{'slot': 'memo', 'facts': {'count': 5}}]
    needs = {**good(case.document), 'state': 'needs', 'needs': 'Needs till data', 'value': None}
    assert case.scripts.validate_output(needs, case.tracker, case.document)['needs'] == 'Needs till data'


def test_a_row_cannot_carry_host_fields_or_an_id_from_elsewhere(case):
    output = good(case.document)
    output['events'] = [{'id': 'event-9999'}]
    assert refused(case, output, 'events[0].id')[0]['message'] == 'Use an id from input.events or input.records.visits.'
    output = good(case.document)
    output['canvas']['rows'][0] = {'id': 'event-0000', 'thumb': 'https://elsewhere.example/x.jpg'}
    refused(case, output, 'canvas.rows[0]')
    output = good(case.document)
    output['events'] = [{'id': 'event-0000', 'source_id': 'source-of-another-company'}]
    refused(case, output, 'events[0]')
    output = good(case.document)
    output['events'] = [{'id': 'event-0000', 'label': 'x' * 201}]
    refused(case, output, 'events[0].label')


def test_every_reserved_name_is_refused_on_the_canvas_and_the_tile(case):
    for name in sorted(case.scripts.RESERVED | case.scripts.LINKS):
        output = good(case.document)
        output['canvas'][name] = 'mine'
        refused(case, output, f'canvas.{name}')
        refused(case, good(case.document, tile_extra={name: 1}), f'tile_extra.{name}')
    output = good(case.document)
    output['canvas']['by_aisle'] = [{'aisle': '3', 'thumb': 'https://elsewhere.example/x.jpg'}]
    refused(case, output, 'canvas.by_aisle[0].thumb')
    output = good(case.document)
    output['canvas']['Aisles'] = 1
    refused(case, output, 'canvas.Aisles')
    refused(case, good(case.document, tile_extra={'deep': {'a': 1}}), 'tile_extra.deep')
    refused(case, good(case.document, tile_extra={str(n): n for n in range(9)}), 'tile_extra')


def test_a_wrong_shape_is_refused(case):
    refused(case, [], '')
    refused(case, {k: v for k, v in good(case.document).items() if k != 'summary'}, 'summary')
    refused(case, good(case.document, label='Mine'), 'label')
    refused(case, good(case.document, key='visitors'), 'key')
    for change, path in (({'state': 'done'}, 'state'), ({'needs': 'Needs a till'}, 'needs'), ({'value': 'many'}, 'value'), ({'value': True}, 'value'),
                         ({'state': 'needs', 'needs': 'Needs a till connection now', 'value': None}, 'needs'),
                         ({'state': 'needs', 'needs': 'Needs till data'}, 'needs'), ({'unit': 'x' * 25}, 'unit'), ({'summary': ''}, 'summary'),
                         ({'summary': 'x' * 601}, 'summary'), ({'series': [{'t': 'soon', 'value': 1}]}, 'series[0]'),
                         ({'series': [{'t': '2026-10-10T09:00:00Z', 'value': 1, 'extra': 1}]}, 'series[0]'),
                         ({'series': [{'t': '2026-10-10T09:00:00Z', 'value': 1}] * 401}, 'series'), ({'trend': 'up'}, 'trend'),
                         ({'canvas': {'rows': []}}, 'canvas'), ({'canvas': {'rows': [], 'total_rows': -1}}, 'canvas.total_rows'),
                         ({'canvas': {'rows': [{'id': 'event-0000'}], 'total_rows': 0}}, 'canvas.total_rows'),
                         ({'texts': [{'slot': 'memo', 'facts': 1}, {'slot': 'memo', 'facts': 2}]}, 'texts[1]'),
                         ({'texts': [{'slot': 'Memo', 'facts': 1}]}, 'texts[0]'), ({'texts': [{'slot': 'memo', 'facts': 'x' * 5000}]}, 'texts[0].facts'),
                         ({'texts': [{'slot': f's{n}', 'facts': n} for n in range(9)]}, 'texts')):
        refused(case, {**good(case.document), **change}, path)
    deep = good(case.document)
    deep['canvas']['nest'] = {'a': {'b': {'c': {'d': {'e': {'f': 1}}}}}}
    refused(case, deep, 'canvas.nest')


def test_a_whole_number_of_any_size_is_refused_and_every_check_answers(case):
    """A whole number past what a page holds exactly is outside the contract, wherever it sits. The
    checks answer with a path for each; a run with such an output is a rejected run like any other."""
    huge = 10 ** 400
    assert case.scripts.NUMBER_LIMIT == 2 ** 53
    for change, path in (({'value': huge}, 'value'), ({'value': -huge}, 'value'), ({'value': 2 ** 53 + 1}, 'value'),
                         ({'series': [{'t': '2026-10-10T09:00:00Z', 'value': huge}]}, 'series[0]'),
                         ({'trend': [{'t': '2026-10-10T09:00:00Z', 'value': huge}]}, 'trend[0]'),
                         ({'tile_extra': {'big': huge}}, 'tile_extra.big'), ({'texts': [{'slot': 'memo', 'facts': {'count': huge}}]}, 'texts[0].facts.count')):
        refused(case, {**good(case.document), **change}, path)
    for name, value, path in (('share', huge, 'canvas.share'), ('total_rows', huge, 'canvas.total_rows'), ('bands', [{'label': 'a', 'value': huge}], 'canvas.bands[0].value'),
                              ('nest', {'deep': [1, huge]}, 'canvas.nest.deep[1]')):
        output = good(case.document)
        output['canvas'][name] = value
        refused(case, output, path)
    assert case.scripts.validate_output({**good(case.document), 'value': 2 ** 53}, case.tracker, case.document)['value'] == 2 ** 53
    # A job whose output holds one is a rejected run: the verdict every stopped run gets, with its path.
    results = [{'ok': True, 'output': {**good(case.document), 'value': huge}}] + [{'ok': True, 'output': good(case.document)}] * 3
    verdict = case.scripts.evaluate({'status': 'succeeded', 'results': results, 'log': ''}, case.tracker, case.document)
    assert verdict['status'] == 'rejected' and verdict['error']['code'] == 'output_contract' and verdict['contract_errors'][0]['path'] == 'day: value'
    # And a fault inside the checks themselves is a rejected run too, never an exception that leaves the run uncounted.
    def breaks(*args):
        raise OverflowError('int too large to convert to float')
    real, case.scripts.validate_output = case.scripts.validate_output, breaks
    try:
        verdict = case.scripts.evaluate({'status': 'succeeded', 'results': [{'ok': True, 'output': good(case.document)}] * 4, 'log': ''}, case.tracker, case.document)
    finally:
        case.scripts.validate_output = real
    assert verdict['status'] == 'rejected' and 'OverflowError' in verdict['contract_errors'][0]['message'] and verdict['accepted'] == []


def test_a_stopped_runs_code_is_one_the_entry_gives(case):
    """The envelope comes out of the sandbox, where a script can write it itself. Its code is one of
    the entry file's own and its line a line of a script, so a run row holds nothing a script wrote."""
    def verdict(error):
        results = [{'ok': False, 'error': error}] + [{'ok': True, 'output': good(case.document)}] * 3
        return case.scripts.evaluate({'status': 'succeeded', 'results': results, 'log': ''}, case.tracker, case.document)['error']
    assert verdict({'code': 'camera-077ea takes a bottle', 'message': 'x', 'line': 3})['code'] == 'script_error'
    assert verdict({'code': 'time_limit', 'message': 'x', 'line': 10 ** 9})['line'] is None
    kept = verdict({'code': 'memory_limit', 'message': 'x', 'line': 14})
    assert (kept['code'], kept['line'], kept['period']) == ('memory_limit', 14, 'day')
    assert {verdict({'code': code, 'message': 'x'})['code'] for code in case.scripts.SCRIPT_CODES} == case.scripts.SCRIPT_CODES
    public = case.scripts._public_error(verdict({'code': 'script_error', 'message': 'LEAK camera-077ea', 'line': 3, 'traceback': 'LEAK'}))
    assert public == {'code': 'script_error', 'line': 3, 'period': 'day', 'message': case.scripts.FAULTS['script_error']}
    assert case.scripts._sentence({'code': 'script_error', 'message': 'LEAK', 'line': 3, 'period': 'day'}) == (
        'The script stopped at line 3 in its day reading with script_error. get_tracker_script shows the message.')


def test_a_key_of_the_scripts_own_never_reads_as_an_evidence_row(case):
    """Evidence rows live in `events` and `canvas.rows`, where the host rebuilds each from its own
    record. Under a key of the script's own, the fields a page reads as a picture, a clip, a link
    or a row's identity are refused at every depth, so a script's own list is words and numbers."""
    scripts = case.scripts
    def own(value):
        output = good(case.document)
        output['canvas']['spots'] = value
        return output
    for name in sorted(scripts.LINKS | {'crop_url', 'anything_url'}):
        refused(case, own([{'description': 'A person takes a bottle', name: 'https://elsewhere.example/login'}]), f'canvas.spots[0].{name}')
        refused(case, own({'inner': {'deeper': [{name: 'x'}]}}), f'canvas.spots.inner.deeper[0].{name}')
    for name in sorted(scripts.ROW_FIELDS):
        refused(case, own([{'description': 'A person takes a bottle', 'at': '2026-10-10T09:00:00Z', name: 'event-fabricated-1'}]), f'canvas.spots[0].{name}')
        output = good(case.document)
        output['canvas'][name] = 'mine'
        refused(case, output, f'canvas.{name}')
        refused(case, good(case.document, tile_extra={name: 'x'}), f'tile_extra.{name}')
    accepted = scripts.validate_output(own([{'description': 'Aisle 3 was busy', 'at': '2026-10-10T09:00:00Z', 'type': 'spill', 'label': 'Aisle 3', 'value': 5,
                                             'product': 'Water', 'band': 'morning', 'shelf': 'A', 'state': 'empty', 'text': 'A note'}]), case.tracker, case.document)
    assert accepted['canvas']['spots'][0]['description'] == 'Aisle 3 was busy', 'words, times and numbers of the script’s own stay'
    # The page's own readers name what they take from a row and from a tile: each of those names is held.
    canvas = (HERE / 'web/display-canvas.js').read_text()
    reader = re.search(r'function occurrence\(raw[^)]*\) \{.*?\n  \}\n', canvas, re.S).group(0)
    def read(field):
        part = re.search(rf'\b{field}: (.*?)(?=, [a-zA-Z]+: |,?\n)', reader, re.S)
        return set(re.findall(r'raw\.([a-z_]+)', part.group(1))) if part else set()
    assert read('thumb') and read('media') and read('href'), 'the page’s row reader names a picture, a clip and a link'
    assert read('thumb') | read('media') | read('href') <= scripts.LINKS, sorted((read('thumb') | read('media') | read('href')) - scripts.LINKS)
    assert read('id') and read('id') <= scripts.ROW_FIELDS, read('id')
    assert set(re.findall(r'raw\.([a-z_]+_s)\b', reader)) <= scripts.ROW_FIELDS and ('raw.source' not in reader or 'source' in scripts.ROW_FIELDS)
    home = (HERE / 'verticals/retail/web/control-room/home-model.js').read_text()
    tile = home[home.index('function suppliedTile('):home.index('function from(')] + home[home.index('const estimatedOf'):home.index('function suppliedTile(')]
    on_a_tile = set(re.findall(r'(?:scoped|row)\??\.([a-z_]+)', tile)) - {'periods', 'events', 'state', 'value', 'unit', 'needs', 'series', 'label', 'key', 'core', 'stage'}
    assert on_a_tile <= scripts.RESERVED, sorted(on_a_tile - scripts.RESERVED)
    from core_trackers import presentation
    assert presentation.ENVELOPE <= scripts.RESERVED | {'state', 'needs'}
    assert {'agent_id', 'kind', 'mark', 'view', 'fraction', 'presentation_revision'} <= scripts.RESERVED, 'the names the page layer stamps on a company tile'
    # The contract an author reads lists the same names.
    for name in sorted(scripts.LINKS | scripts.ROW_FIELDS | (scripts.RESERVED - set(scripts.OUTPUT_KEYS) - set(scripts.OPTIONAL_KEYS))):
        assert re.search(rf'\b{name}\b', scripts.TRACKER_SCRIPT_INSTRUCTIONS), name


def test_a_points_time_is_the_form_the_host_writes(case):
    """A series and a trend are drawn by time. A point's time is a full date and time in UTC, the
    form the Core Trackers write, and it is stored that way whatever offset it arrived with."""
    scripts = case.scripts
    for t in ('2026-W41-6', '20261010T09', '2026-10-10', '2026-10-10 09:00', '2026-10-10T09:00:00', '2026-10-10T09', '0001-01-01T00:00:00Z',
              '2026-13-40T09:00:00Z', '2026-10-10T09:00:00+0530', 'soon', 1760000000, None):
        refused(case, {**good(case.document), 'series': [{'t': t, 'value': 1}]}, 'series[0]')
        refused(case, {**good(case.document), 'trend': [{'t': t, 'value': 1}]}, 'trend[0]')
    points = [{'t': '2026-10-10T09:00:00Z', 'value': 1}, {'t': '2026-10-10T11:30:00+05:30', 'value': 2}, {'t': '2026-10-10T09:00Z', 'value': 3},
              {'t': '2026-10-10T09:00:00.250Z', 'value': 4}]
    accepted = scripts.validate_output({**good(case.document), 'series': points, 'trend': points[:1]}, case.tracker, case.document)
    assert [point['t'] for point in accepted['series']] == ['2026-10-10T09:00:00Z', '2026-10-10T06:00:00Z', '2026-10-10T09:00:00Z', '2026-10-10T09:00:00.250000Z']
    assert accepted['trend'] == [{'t': '2026-10-10T09:00:00Z', 'value': 1}]
    import retail_records as records
    assert all(point['t'] == records.iso(scripts.datetime.fromisoformat(point['t'].replace('Z', '+00:00')).timestamp()) for point in accepted['series'])


def test_script_jobs_are_counted_for_the_host_and_per_company_and_the_count_outlives_a_restart(tmp_path, monkeypatch):
    """The broker's job history is one for the whole host. Until its retention upgrade is installed
    every row stays, so script jobs keep to a few a day and to a share of the history in all; the
    host says the upgrade is installed with its event-runs switch, and the numbers of a day follow."""
    import tracker_scripts as scripts
    assert (scripts.JOBS_PER_DAY, scripts.COMPANY_JOBS_PER_DAY, scripts.HOST_JOBS_PER_DAY) == (8, 24, 48)
    assert scripts.HOST_JOBS_BEFORE_RETENTION == 1024 and scripts.HOST_JOBS_PER_DAY_EVENTS == 1200 < 2048, 'a rolling day of jobs stays under the broker’s kept rows'
    now = [1_800_000_000.0]
    path = tmp_path / 'root' / scripts.COUNTS_FILE
    counts = scripts.JobCounts(path, clock=lambda: now[0])
    assert counts.numbers('a') == {'host': 0, 'company': 0, 'total': 0} and not path.exists(), 'a read makes no file'
    marks = [counts.count('a') for _ in range(24)]
    assert counts.refused('a', retained=False) == 'company_daily_limit' and counts.refused('b', retained=False) is None
    for _ in range(24):
        counts.count('b')
    assert counts.refused('c', retained=False) == 'sandbox_daily_limit', 'the host’s number holds for every company together'
    assert counts.refused('c', retained=True) is None and counts.refused('a', retained=True) is None
    # A job the broker refused goes back out of every number.
    counts.uncount(marks[0])
    counts.uncount(marks[0])
    assert counts.numbers('a') == {'host': 47, 'company': 23, 'total': 47} and counts.refused('a', retained=False) is None
    # Another process of this service reads the same numbers: the count is the host's.
    again = scripts.JobCounts(path, clock=lambda: now[0])
    assert again.numbers('a') == {'host': 47, 'company': 23, 'total': 47} and oct(path.stat().st_mode & 0o777) == '0o600'
    # A day on, the day's numbers are free again and the count in all still stands.
    now[0] += 86401
    assert again.numbers('a') == {'host': 0, 'company': 0, 'total': 47} and again.refused('a', retained=False) is None
    monkeypatch.setattr(scripts, 'HOST_JOBS_BEFORE_RETENTION', 48)
    again.count('a')
    assert again.refused('z', retained=False) == 'sandbox_history_share' and again.refused('z', retained=True) is None
    # Without a path the numbers live in the object, with the same rules.
    held = scripts.JobCounts(clock=lambda: now[0])
    mark = held.count('a')
    assert held.numbers('a') == {'host': 1, 'company': 1, 'total': 1}
    held.uncount(mark)
    assert held.numbers('a') == {'host': 0, 'company': 0, 'total': 0}
    # A store that takes no write admits no job.
    blocked = tmp_path / 'blocked'
    blocked.write_text('a file where the directory should be')
    with pytest.raises((sqlite3.Error, OSError)):
        scripts.JobCounts(blocked / 'x' / scripts.COUNTS_FILE).count('a')


def test_numbers_are_finite_and_sizes_hold(case):
    for bad in (float('nan'), float('inf'), float('-inf')):
        refused(case, {**good(case.document), 'value': bad})
        output = good(case.document)
        output['canvas']['share'] = bad
        refused(case, output)
    output = good(case.document)
    output['canvas']['rows'] = [{'id': 'event-0000'}] * 501
    output['canvas']['total_rows'] = 501
    refused(case, output, 'canvas.rows')
    refused(case, {**good(case.document), 'events': [{'id': 'event-0000'}] * 501}, 'events')
    output = good(case.document)
    output['canvas']['blob'] = ['x' * 1000] * 200
    errors = refused(case, output, '')
    assert str(case.scripts.OUTPUT_BYTES) in errors[0]['message']


def test_what_an_author_reads_says_what_is(case):
    """The contract, each tool's description and every message are written as what is."""
    scripts = case.scripts
    spoken = [scripts.TRACKER_SCRIPT_INSTRUCTIONS, *scripts.FAULTS.values(), *(spec['description'] for spec in scripts.specs(True)),
              *(value for name, value in vars(scripts).items() if name.isupper() and isinstance(value, str) and ' ' in value
                and name not in {'ENTRY_SOURCE', 'EXAMPLE_SOURCE'})]
    for spec in scripts.specs(True):
        spoken.extend(prop.get('description', '') for prop in spec['inputSchema']['properties'].values())
    # `never` is the name of a wake policy; every other word is held to the law.
    assert [text for text in spoken if NEGATIVE.search(text.replace('never,', '').replace('wake: never', ''))] == []
    contract = scripts.TRACKER_SCRIPT_INSTRUCTIONS
    for said in ('def run(input): return output', 'host-fetched input', 'input["connections"]', 'write_tracker_text', 'approve_tracker_output',
                 f'{scripts.RUN_SECONDS} seconds', f'{scripts.MEMORY_MB} MB', f'{scripts.OUTPUT_BYTES // 1024} KB', 'last published output stays in place',
                 f'After {scripts.PAUSE_AFTER} stopped runs in a row the script pauses'):
        assert said in contract, said
    for key in (*scripts.OUTPUT_KEYS, *scripts.OPTIONAL_KEYS, 'tracker', 'location', 'period', 'periods', 'events', 'records', 'connections', 'prior', 'limits'):
        assert re.search(rf'^\s+{key}\s', contract, re.M), key
    # Each sentence about when a script runs, what wakes its agent and what a text is written for is one the code keeps.
    for said in ('once each local day of the location', 'at or after the location\'s daily time', f'up to {scripts.RETRIES_PER_DAY} times a day',
                 'tracker with an active Tracking Agent', 'write_tracker_text(tracker_id, slot, text, output_revision)', 'proposal_id for a waiting proposal',
                 'the run that pauses the script wakes the agent too', 'every_run after each run the sandbox took',
                 'A tracker managed from a conversation has its health and its runs in get_tracker_script', 'ends in Z', 'within 2**53'):
        assert said in contract, said
    assert 'day turns' not in contract and 'in the next pass' not in ' '.join(scripts.FAULTS.values())
