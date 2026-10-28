"""Script jobs through the real sandbox broker and its real client over a Unix socket. Only the
container is a fixture: a backend that runs the fixed worker, and through it the real entry file,
in a local process. No provider and no model is called."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import custom_worker as worker
import native_custom_broker
import native_executor as native
from custom_execution import RuntimeProfile
from native_custom_broker import CustomExecutor
from native_custom_client import CustomClient
from test_native_custom import Backend
from test_native_executor import broker
from test_tracker_scripts import COUNT, location, periods_of


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class WorkerBackend(Backend):
    """Where the container stands: the fixed worker runs the job's entry with this interpreter."""

    def __init__(self, root):
        super().__init__(root)
        self.running, self.most, self.crash = 0, 0, False

    async def run(self, key, root, sockets):
        self.started += 1
        self.live = True
        self.running += 1
        self.most = max(self.most, self.running)
        self.entered.set()
        try:
            if self.wait:
                await self.wait.wait()
            if self.crash:
                raise native.ExecutorError('custom_worker_failed')
            workspace = self.volume_path(key) / 'workspace'
            workspace.mkdir(parents=True)
            config = json.loads((root / 'input/run.json').read_bytes())
            def execute(argv, **options):
                return subprocess.run([sys.executable, *argv[1:]], **{**options, 'env': {**options['env'], 'HOME': str(workspace), 'TMPDIR': str(workspace)}})
            await asyncio.to_thread(worker.run, root / 'source', config, workspace, execute=execute)
        finally:
            self.running -= 1


class Bench:
    def __init__(self, path):
        import tracker_scripts as scripts
        self.scripts, self.path = scripts, path
        self.host = broker(path / 'broker')
        self.backend = WorkerBackend(path / 'volumes')
        self.executor = CustomExecutor(self.host, worker_path=Path(worker.__file__), backend=self.backend)
        self.server = native.BrokerServer(self.host, path / 'control.sock')
        self.data, self.tracker = location()
        self.document = scripts.build_input(self.data, self.tracker, periods_of())
        self.opened, self.closed = [], []

    async def __aenter__(self):
        await self.server.start()
        self.client = CustomClient(self.server.path)
        self.sandbox = self.scripts.Sandbox(enabled=True, client=self.client, runtime=RuntimeProfile(native.IMAGE, self.executor.worker_sha256),
                                            poll_seconds=.01, busy_waits=(.01, .01), slot_wait=60)
        return self

    async def __aexit__(self, *exc):
        await self.server.close()

    def run(self, source=COUNT, company='company-1', **more):
        return self.sandbox.run(company_id=company, owner='peter', collection='retail-demo', tracker_id=self.tracker['id'], revision=1,
                                source=source, document=self.document, opened=self.opened.append, closed=self.closed.append, **more)

    def rows(self):
        return [row['status'] for row in self.executor.db.execute('SELECT status FROM jobs ORDER BY rowid')]

    def clean(self):
        return (self.host.live_count() == 0 and self.executor.live_count() == 0 and not self.backend.live
                and not list(self.executor.root.glob('jobs/*')) and not list(self.host.run_root.glob('custom-*')))


@pytest.fixture
def bench():
    with tempfile.TemporaryDirectory(prefix='tsr-') as directory:
        yield Bench(Path(directory))


@pytest.mark.anyio
async def test_a_job_runs_through_the_broker_and_leaves_it_clean(bench):
    async with bench:
        started = time.monotonic()
        result = await bench.run()
        took = time.monotonic() - started
        assert result['status'] == 'succeeded' and [entry['ok'] for entry in result['results']] == [True] * 4
        assert [entry['output']['value'] for entry in result['results']] == [5] * 4 and result['log'].startswith('counted 5 in day')
        assert result['broker_run_id'].startswith('custom-') and result['input_sha256'] == bench.scripts.input_digest(bench.document)
        assert result['started'] is True, 'the broker took a job'
        assert result['input_bytes'] == len(bench.scripts.canonical(bench.document)) and 0 < result['seconds'] <= took + .01 < 10
        assert bench.rows() == ['released'] and bench.clean()
        assert bench.opened == bench.closed and set(bench.opened[0]) == {'request_id', 'scope'}
        assert bench.opened[0]['scope'] == {'company_id': 'company-1', 'owner_id': 'peter', 'root_chat_id': 'tracker-run-' + bench.opened[0]['request_id']}
        verdict = bench.scripts.evaluate(result, bench.tracker, bench.document)
        assert verdict['status'] == 'succeeded' and [entry['value'] for entry in verdict['accepted']] == [5] * 4


@pytest.mark.anyio
async def test_a_script_fault_is_a_status_and_a_stopped_container_is_named(bench):
    async with bench:
        raised = await bench.run('def run(input):\n    return input["missing"]\n')
        assert raised['status'] == 'succeeded' and raised['results'][0]['error']['code'] == 'script_error'
        verdict = bench.scripts.evaluate(raised, bench.tracker, bench.document)
        assert verdict['status'] == 'failed' and verdict['error']['code'] == 'script_error' and verdict['error']['line'] == 2
        assert verdict['error']['period'] == 'day' and verdict['accepted'] == []
        gone = await bench.run('import os\n\ndef run(input):\n    os._exit(0)\n')
        assert gone['status'] == 'failed' and gone['error']['code'] == 'script_stopped' and gone['error']['message'] == bench.scripts.FAULTS['script_stopped']
        bench.backend.crash = True
        crashed = await bench.run()
        assert crashed['status'] == 'failed' and crashed['error']['code'] == 'script_stopped' and crashed['started'] is True
        assert bench.rows() == ['released'] * 3 and bench.clean() and bench.opened == bench.closed


@pytest.mark.anyio
async def test_a_busy_or_full_sandbox_is_unavailable_and_the_script_is_blameless(bench, monkeypatch):
    async with bench:
        tries = []
        monkeypatch.setattr(bench.host, 'live_count', lambda: tries.append(1) or native.MAX_WORKERS)
        busy = await bench.run()
        assert busy['status'] == 'unavailable' and busy['error']['code'] == 'sandbox_busy' and len(tries) == 3, 'two waits, three tries'
        assert busy['started'] is False, 'a job the broker refused counts for nothing'
        tries.clear()
        monkeypatch.setattr(bench.host, 'live_count', lambda: tries.append(1) or (native.MAX_WORKERS if len(tries) < 2 else 0))
        assert (await bench.run())['status'] == 'succeeded', 'a worker that frees up in time takes the job'
        monkeypatch.setattr(native_custom_broker, 'MAX_HISTORY', 1)
        full = await bench.run()
        assert full['status'] == 'unavailable' and full['error']['code'] == 'sandbox_maintenance'
        assert bench.scripts.evaluate(full, bench.tracker, bench.document)['status'] == 'unavailable'
        assert bench.clean() and bench.backend.started == 1


@pytest.mark.anyio
async def test_a_sandbox_that_is_off_or_another_version_takes_no_job(bench, tmp_path):
    async with bench:
        off = bench.scripts.Sandbox(enabled=False, client=bench.client, runtime=bench.sandbox.runtime)
        answer = await off.run(company_id='company-1', owner='peter', collection='retail-demo', tracker_id=bench.tracker['id'], revision=1,
                               source=COUNT, document=bench.document)
        assert answer['status'] == 'unavailable' and answer['error']['code'] == 'sandbox_offline' and answer['started'] is False
        other = bench.scripts.Sandbox(enabled=True, client=bench.client, runtime=RuntimeProfile(native.IMAGE, '0' * 64))
        assert not await other.available('company-1', 'peter')
        answer = await other.run(company_id='company-1', owner='peter', collection='retail-demo', tracker_id=bench.tracker['id'], revision=1,
                                 source=COUNT, document=bench.document)
        assert answer['status'] == 'unavailable' and answer['error']['code'] == 'sandbox_offline'
        absent = bench.scripts.Sandbox(enabled=True, client=CustomClient(tmp_path / 'no-broker.sock'), runtime=bench.sandbox.runtime)
        answer = await absent.run(company_id='company-1', owner='peter', collection='retail-demo', tracker_id=bench.tracker['id'], revision=1,
                                  source=COUNT, document=bench.document)
        assert answer['status'] == 'unavailable' and answer['error']['code'] == 'sandbox_offline'
        assert bench.rows() == [] and bench.backend.started == 0


@pytest.mark.anyio
async def test_a_stop_and_a_cancelled_turn_both_clear_the_job(bench):
    async with bench:
        bench.backend.wait = asyncio.Event()
        stop = asyncio.Event()
        running = asyncio.create_task(bench.run(cancelled=stop))
        await bench.backend.entered.wait()
        stop.set()
        stopped = await running
        assert stopped['status'] == 'failed' and stopped['error']['code'] == 'stopped' and bench.clean()
        bench.backend.entered.clear()
        running = asyncio.create_task(bench.run())
        await bench.backend.entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert bench.rows() == ['released', 'released'] and bench.clean() and bench.opened == bench.closed


@pytest.mark.anyio
async def test_two_jobs_run_one_after_the_other(bench):
    async with bench:
        results = await asyncio.gather(bench.run(), bench.run(), bench.run())
        assert [result['status'] for result in results] == ['succeeded'] * 3
        assert bench.backend.most == 1 and bench.backend.started == 3 and bench.clean()


@pytest.mark.anyio
async def test_a_job_left_by_a_stopped_process_is_cleared_at_start(bench):
    async with bench:
        scripts = bench.scripts
        bench.backend.wait = asyncio.Event()
        scope = native.NativeScope('company-1', 'peter', 'tracker-run-' + 'c' * 32)
        snapshot = scripts.snapshot_for(company_id='company-1', owner='peter', collection='retail-demo', tracker_id=bench.tracker['id'], revision=1,
            source=COUNT, parts=scripts.input_parts(bench.document), runtime=bench.sandbox.runtime)
        lease = await bench.client.prepare(scope, 'c' * 32, snapshot, scripts.TOOL, {})
        await bench.client.upload(scope, lease, snapshot, scripts.TOOL, {})
        listener = await scripts.DenySocket(lease.socket_dir).start()
        await bench.client.start(scope, lease, 'fixture_' + 'x' * 40)
        await bench.backend.entered.wait()
        assert bench.rows() == ['running'] and not bench.clean()
        await listener.close()
        cleared = await bench.sandbox.recover([{'request_id': 'c' * 32, 'scope': asdict(scope)}, {'request_id': 'd' * 32, 'scope': {'bad': 1}}],
                                              closed=bench.closed.append)
        assert cleared == ['c' * 32] and bench.rows() == ['released'] and bench.clean() and bench.closed[0]['request_id'] == 'c' * 32


@pytest.mark.anyio
async def test_an_unconfirmed_cleanup_closes_admission_until_it_is_confirmed(bench):
    async with bench:
        bench.backend.fail_stop = True
        first = await bench.run()
        assert first['status'] == 'unavailable' and first['error']['code'] == 'sandbox_cleanup', 'the broker could not confirm the container gone'
        assert bench.sandbox.uncleared and bench.closed == [] and bench.rows() == ['uncertain'], 'the job’s record stays open'
        held = await bench.run()
        assert held['status'] == 'unavailable' and held['error']['code'] == 'sandbox_cleanup' and bench.backend.started == 1
        bench.backend.fail_stop = False
        assert (await bench.run())['status'] == 'succeeded' and not bench.sandbox.uncleared
        assert len(bench.closed) == 2 and bench.rows() == ['released', 'released'] and bench.clean()


@pytest.mark.anyio
async def test_the_job_socket_answers_every_request_with_403(tmp_path):
    import tracker_scripts as scripts
    directory = tmp_path / 'sock'
    directory.mkdir(mode=0o700)
    listener = await scripts.DenySocket(directory).start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(directory / 'custom-http.sock'))
        writer.write(b'POST /request HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}')
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        assert answer.startswith(b'HTTP/1.1 403 Forbidden')
        with pytest.raises(native.ExecutorError):
            await scripts.DenySocket(directory).start()  # one socket per job directory
    finally:
        await listener.close()
    assert not (directory / 'custom-http.sock').exists()
    open_to_all = tmp_path / 'open'
    open_to_all.mkdir(mode=0o755)
    with pytest.raises(native.ExecutorError):
        await scripts.DenySocket(open_to_all).start()


# ── What two reviewers broke, kept as it was fixed ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_each_company_has_its_turn_and_a_wait_for_the_turn_costs_nothing(bench):
    """One job holds the service's sandbox turn. A job that finds it held waits a short, fixed time
    and answers `unavailable`, with no job taken and nothing counted. The company that gave up
    keeps the next turn, and the company that held the last one yields to it."""
    async with bench:
        scripts, counts = bench.scripts, bench.sandbox.counts
        bench.backend.wait = asyncio.Event()
        first = asyncio.create_task(bench.run())
        await bench.backend.entered.wait()
        started = time.monotonic()
        waited = await bench.run(company='company-2', wait_seconds=.05)
        assert waited['status'] == 'unavailable' and waited['error'] == scripts.fault('sandbox_turn') and waited['started'] is False
        assert time.monotonic() - started < 3 and len(bench.opened) == 1, 'it gave up after its short wait and the broker never heard of it'
        assert counts.numbers('company-2') == {'host': 1, 'company': 0, 'total': 1} and bench.backend.started == 1
        # The company that holds the turn asks again: another company keeps a claim, so it yields at once.
        started = time.monotonic()
        yielded = await bench.run()
        assert yielded['status'] == 'unavailable' and yielded['error']['code'] == 'sandbox_turn' and yielded['started'] is False
        assert time.monotonic() - started < 3, 'it yields without joining the line'
        bench.backend.wait.set()
        assert (await first)['status'] == 'succeeded'
        assert (await bench.run())['error']['code'] == 'sandbox_turn', 'the claim outlives the job that was running'
        bench.backend.wait = None
        theirs = await bench.run(company='company-2')
        assert theirs['status'] == 'succeeded' and theirs['started'] is True and bench.sandbox._claims == {}
        assert (await bench.run())['status'] == 'succeeded', 'with the claim used the first company runs again'
        assert counts.numbers('company-1') == {'host': 3, 'company': 2, 'total': 3} and bench.rows() == ['released'] * 3 and bench.clean()
        # A claim that is never used ends by itself: the next turn stays the company's for TURN_SECONDS.
        now = [bench.sandbox.clock()]
        bench.sandbox.clock = lambda: now[0]
        bench.sandbox._claims['company-9'] = now[0] + scripts.TURN_SECONDS
        assert (await bench.run())['error']['code'] == 'sandbox_turn'
        now[0] += scripts.TURN_SECONDS + 1
        assert (await bench.run())['status'] == 'succeeded' and bench.sandbox._claims == {}
        assert (scripts.SLOT_WAIT_SECONDS, scripts.TEST_SLOT_WAIT_SECONDS, scripts.TURN_SECONDS) == (5, 20, 120)
        assert (await bench.run(wait_seconds=0))['status'] == 'succeeded', 'a free turn is taken at once, whatever the wait'


@pytest.mark.anyio
async def test_a_job_counts_once_the_broker_took_it_inside_the_hosts_numbers(bench, monkeypatch):
    async with bench:
        scripts, counts = bench.scripts, bench.sandbox.counts
        assert (await bench.run())['status'] == 'succeeded' and counts.numbers('company-1') == {'host': 1, 'company': 1, 'total': 1}
        # Both workers in use: the broker refuses, and the count goes back.
        monkeypatch.setattr(bench.host, 'live_count', lambda: native.MAX_WORKERS)
        busy = await bench.run()
        assert busy['error']['code'] == 'sandbox_busy' and busy['started'] is False and counts.numbers('company-1')['total'] == 1
        monkeypatch.undo()
        # The company's number for 24 hours, then the host's, then the share of the broker's history.
        monkeypatch.setattr(scripts, 'COMPANY_JOBS_PER_DAY', 1)
        held = await bench.run()
        assert held['status'] == 'unavailable' and held['error'] == scripts.fault('company_daily_limit') and held['started'] is False
        assert (await bench.run(company='company-2'))['status'] == 'succeeded', 'another company keeps its own number'
        monkeypatch.setattr(scripts, 'HOST_JOBS_PER_DAY', 2)
        assert (await bench.run(company='company-3'))['error']['code'] == 'sandbox_daily_limit'
        monkeypatch.setattr(scripts, 'HOST_JOBS_PER_DAY', 48)
        monkeypatch.setattr(scripts, 'HOST_JOBS_BEFORE_RETENTION', 2)
        share = await bench.run(company='company-3')
        assert share['error'] == scripts.fault('sandbox_history_share') and share['started'] is False
        assert bench.rows() == ['released'] * 2 and bench.backend.started == 2, 'a job kept out by a number never reaches the broker'
        # The host states the broker's retention upgrade with its event-runs switch: a day's larger numbers hold from then.
        bench.sandbox.event_runs = True
        assert (await bench.run(company='company-3'))['status'] == 'succeeded' and (await bench.run())['status'] == 'succeeded'
        assert counts.numbers('company-1') == {'host': 4, 'company': 2, 'total': 4} and bench.clean()
        # A count that takes no write admits no job.
        def broken(*args, **kwargs):
            raise sqlite3.OperationalError('disk I/O error')
        monkeypatch.setattr(counts, 'count', broken)
        closed = await bench.run()
        assert closed['status'] == 'unavailable' and closed['error']['code'] == 'sandbox_offline' and bench.backend.started == 4


@pytest.mark.anyio
async def test_the_host_stops_a_job_at_its_time_and_names_the_script(bench):
    async with bench:
        bench.backend.wait = asyncio.Event()
        late = await bench.run(seconds=.2)
        assert late['status'] == 'failed' and late['error'] == bench.scripts.fault('time_limit') and late['started'] is True
        assert bench.rows() == ['released'] and bench.clean() and bench.sandbox.counts.numbers('company-1')['total'] == 1


@pytest.mark.anyio
async def test_the_job_socket_holds_a_few_connections_and_takes_a_fixed_number(tmp_path):
    """A script that opens connections costs the service a fixed few descriptors: four at once, a
    quarter of a second each, sixty-four in all. Then the listener closes and the job is a stopped run."""
    import os
    import tracker_scripts as scripts
    directory = tmp_path / 'sock'
    directory.mkdir(mode=0o700)
    listener = await scripts.DenySocket(directory).start()
    address, held = str(directory / 'custom-http.sock'), []
    descriptors = lambda: len(os.listdir('/proc/self/fd'))
    try:
        before = descriptors()
        for _ in range(40):
            held.append(await asyncio.open_unix_connection(address))
        await asyncio.sleep(.1)
        assert listener.open <= scripts.SOCKET_OPEN == 4 and listener.total == 40 and not listener.flooded
        assert descriptors() - before <= 40 + scripts.SOCKET_OPEN + 1, 'the service holds four of the forty'
        await asyncio.sleep(.5)
        assert listener.open == 0, 'a silent connection is answered and closed within a quarter of a second'
        answers = []
        for reader, writer in held:
            try:
                answers.append(await asyncio.wait_for(reader.read(), 2))
            except ConnectionError:
                answers.append(b'')
            writer.close()
        assert sum(1 for answer in answers if answer.startswith(b'HTTP/1.1 403 Forbidden')) == scripts.SOCKET_OPEN and set(answers) <= {b'', answers[0]}
        # Past the total the listener takes its last connection and says so.
        for _ in range(scripts.SOCKET_TOTAL):
            try:
                reader, writer = await asyncio.open_unix_connection(address)
                writer.close()
            except OSError:
                break
            await asyncio.sleep(0)
        await asyncio.sleep(.1)
        assert listener.flooded and scripts.SOCKET_TOTAL == 64
        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_unix_connection(address), 2)
    finally:
        await listener.close()
    assert not (directory / 'custom-http.sock').exists()


@pytest.mark.anyio
async def test_a_script_that_floods_its_socket_is_a_stopped_run(bench):
    async with bench:
        scripts = bench.scripts
        class Flooded(scripts.DenySocket):
            async def start(self):
                await super().start()
                self.flooded = True
                return self
        bench.sandbox.socket_factory = Flooded
        bench.backend.wait = asyncio.Event()
        flooded = await bench.run()
        assert flooded['status'] == 'failed' and flooded['error'] == scripts.fault('network') and flooded['started'] is True
        assert scripts.evaluate(flooded, bench.tracker, bench.document)['status'] == 'failed', 'it counts toward the pause like every stopped run'
        assert bench.rows() == ['released'] and bench.clean()
