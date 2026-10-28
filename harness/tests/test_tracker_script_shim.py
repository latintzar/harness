"""The entry file a sandbox job runs beside a company's script, run here in a local process the way
the sandbox's worker runs it: `python -I rocoto_entry.py`, input as read-only files, one JSON
envelope on stdout and exit code 0 whatever the script does."""
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_tracker_scripts import COUNT, location, periods_of, run_local


@pytest.fixture
def job(tmp_path):
    import tracker_scripts as scripts
    data, tracker = location()
    def run(source, *, periods=4, seconds=None, change=None):
        document = scripts.build_input(data, tracker, periods_of()[:periods], seconds=seconds or scripts.RUN_SECONDS)
        if change:
            change(document)
        code, envelope = run_local(tmp_path, source, document)
        assert code == 0 and envelope['schema'] == 1 and len(envelope['results']) == periods
        return envelope
    run.tracker, run.data, run.scripts, run.tmp_path = tracker, data, scripts, tmp_path
    return run


def test_one_job_computes_each_period_and_prints_stay_in_the_log(job):
    started = time.monotonic()
    envelope = job(COUNT)
    took = time.monotonic() - started
    assert [result['ok'] for result in envelope['results']] == [True] * 4 and took < 10
    assert [result['output']['summary'] for result in envelope['results']] == [
        '5 events, Sat 10 Oct 2026.', '5 events, 4 Oct to 10 Oct 2026.', '5 events, 11 Sep to 10 Oct 2026.', '5 events, 11 Oct 2025 to 10 Oct 2026.']
    assert all(result['output']['value'] == 5 for result in envelope['results'])
    assert envelope['log'].splitlines() == ['counted 5 in day', 'counted 5 in week', 'counted 5 in month', 'counted 5 in year']
    assert len(job(COUNT, periods=1)['results']) == 1
    # The example every author reads is a script that passes.
    example = job(job.scripts.EXAMPLE_SOURCE)
    document = job.scripts.build_input(job.data, job.tracker, periods_of())
    for result in example['results']:
        assert result['ok'] and job.scripts.validate_output(result['output'], job.tracker, document)['value'] == 5


def test_each_period_gets_a_fresh_copy_of_the_input(job):
    source = COUNT.replace('    period = input["period"]\n', '    period = input["period"]\n    seen = len(input["events"])\n    input["events"].clear()\n    input["events"].extend([{"id": "event-0000"}] * seen)\n')
    assert [result['output']['value'] for result in job(source)['results']] == [5, 5, 5, 5]


def test_a_script_that_raises_names_its_line(job):
    source = 'def helper(rows):\n    return rows["aisle"]\n\n\ndef run(input):\n    if input["period"]["name"] == "week":\n        return helper({})\n    return {"value": 1}\n'
    envelope = job(source)
    assert [result['ok'] for result in envelope['results']] == [True, False, True, True], 'one period’s fault stays that period’s'
    error = envelope['results'][1]['error']
    assert error['code'] == 'script_error' and error['message'] == "KeyError: 'aisle'" and error['line'] == 2
    assert 'tracker.py", line 7, in run' in error['traceback'] and 'line 2, in helper' in error['traceback']
    assert str(job.tmp_path) not in error['traceback'] and 'rocoto_entry' not in error['traceback']
    for stop in ('raise SystemExit(3)', 'import sys\n    sys.exit(0)', 'raise KeyboardInterrupt()'):
        stopped = job(f'def run(input):\n    {stop}\n')
        assert all(result['error']['code'] == 'script_error' for result in stopped['results']), stop


def test_a_script_that_fails_to_load_fails_every_period(job):
    cases = (('def run(input):\n    return {\n', 'script_syntax', 2), ('x = 1\n', 'script_contract', None), ('run = 5\n', 'script_contract', None),
             ('import missing_module_of_the_company\n\ndef run(input):\n    return {}\n', 'script_error', 1), ('raise ValueError("at load")\n', 'script_error', 1))
    for source, code, line in cases:
        envelope = job(source)
        assert [result['error']['code'] for result in envelope['results']] == [code] * 4, source
        assert envelope['results'][0]['error']['line'] == line, source


def test_another_shape_and_another_encoding_are_named(job):
    assert [r['error']['code'] for r in job('def run(input):\n    return [1, 2]\n')['results']] == ['script_contract'] * 4
    assert [r['error']['code'] for r in job('def run(input):\n    return None\n')['results']] == ['script_contract'] * 4
    assert [r['error']['code'] for r in job('def run(input):\n    return {"value": float("nan")}\n')['results']] == ['output_encoding'] * 4
    assert [r['error']['code'] for r in job('def run(input):\n    return {"value": {1, 2}}\n')['results']] == ['output_encoding'] * 4
    assert [r['error']['code'] for r in job('def run(input):\n    return {"value": "\\ud800"}\n')['results']] == ['output_encoding'] * 4


def test_a_huge_output_is_refused_inside_the_sandbox(job):
    big = job('def run(input):\n    return {"value": "x" * 200000}\n')
    assert [r['error']['code'] for r in big['results']] == ['output_too_large'] * 4 and str(job.scripts.OUTPUT_BYTES) in big['results'][0]['error']['message']
    # A print that floods the log keeps the envelope whole: the log's tail comes back.
    loud = job('def run(input):\n    print("y" * 300000)\n    return {"value": 1}\n', periods=1)
    assert loud['results'][0]['ok'] and len(loud['log']) <= 4000 and set(loud['log'].strip()) == {'y'}


def test_a_slow_script_meets_its_time_limit(job):
    started = time.monotonic()
    slow = job('import time\n\ndef run(input):\n    if input["period"]["name"] != "day":\n        time.sleep(30)\n    return {"value": 1}\n', seconds=1)
    assert time.monotonic() - started < 8
    assert slow['results'][0]['ok'] and [r['error']['code'] for r in slow['results'][1:]] == ['time_limit'] * 3
    assert slow['results'][1]['error']['message'] == 'The script ran past its 1 seconds.'
    # A handler of Exception leaves the limit standing.
    stubborn = job('import time\n\ndef run(input):\n    try:\n        time.sleep(30)\n    except Exception:\n        return {"value": 2}\n', seconds=1)
    assert [r['error']['code'] for r in stubborn['results']] == ['time_limit'] * 4
    # The limit is the host's: a document that asks for more gets the entry's own ceiling.
    assert f'SECONDS, MEMORY, OUTPUT, ENVELOPE, PERIODS, TAIL = {job.scripts.RUN_SECONDS},' in job.scripts.ENTRY_SOURCE


def test_memory_is_bounded(job):
    greedy = job('def run(input):\n    held = bytearray(1 << 30)\n    return {"value": len(held)}\n', periods=1)
    assert greedy['results'][0]['error']['code'] == 'memory_limit'


def test_the_script_reads_its_input_and_holds_no_token(job):
    source = ('import os, sys\n\ndef run(input):\n    left = sys.stdin.read()\n    names = sorted(os.listdir(os.path.dirname(__file__)))\n'
              '    return {"value": len(left), "names": names, "keys": sorted(input), "cwd_files": sorted(os.listdir("."))[:3]}\n')
    seen = job(source, periods=1)['results'][0]['output']
    assert seen['value'] == 0, 'the worker’s document with the gateway token is read and dropped before the script runs'
    assert seen['names'] == ['input', 'rocoto_entry.py', 'tracker.py']
    assert seen['keys'] == ['connections', 'events', 'limits', 'location', 'period', 'periods', 'prior', 'records', 'schema', 'tracker']


def test_a_script_that_ends_the_process_leaves_no_envelope(job, tmp_path):
    """The one way past the entry is to end the process; the host then reads an empty result as a stopped script."""
    import json
    import subprocess
    scripts = job.scripts
    tree = tmp_path / 'direct'
    (tree / 'input').mkdir(parents=True)
    (tree / scripts.ENTRY).write_text(scripts.ENTRY_SOURCE)
    (tree / scripts.SCRIPT).write_text('import os\n\ndef run(input):\n    os._exit(0)\n')
    for name, raw in scripts.input_parts(scripts.build_input(job.data, job.tracker, periods_of())):
        (tree / name).write_bytes(raw)
    done = subprocess.run([sys.executable, '-I', str(tree / scripts.ENTRY)], input=b'{}', capture_output=True, cwd=tree,
                          env={'PATH': '/usr/bin:/bin', 'TMPDIR': str(tmp_path)}, timeout=20)
    assert done.stdout == b''
    with pytest.raises(ValueError):
        json.loads(done.stdout)
