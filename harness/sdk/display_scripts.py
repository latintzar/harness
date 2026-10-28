"""Bounded script execution supplied by Rocoto's SDK.

A vertical supplies input packing, a commissioned source bundle and output validation.
The SDK owns source syntax checks, socket denial, fair turns, durable quotas, broker
cleanup and cancellation. It executes scripts through the host's commissioned sandbox.
"""
from __future__ import annotations

import ast
import asyncio
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
import threading
import time
from typing import Callable

SOCKET_OPEN, SOCKET_TOTAL = 4, 64
BUSY_WAITS = (2, 5, 10)
JOB_SECONDS, SLOT_WAIT_SECONDS, TURN_SECONDS = 75, 5, 120
REFUSALS = frozenset({'capacity_reached', 'custom_output_capacity', 'custom_history_capacity'})


@dataclass(frozen=True)
class JobLimits:
    """Host-commissioned job bounds before and after broker history retention."""
    company: int = 24
    host: int = 48
    history: int = 1024
    company_retained: int = 600
    host_retained: int = 1200


@dataclass(frozen=True)
class ScriptContract:
    input_parts: Callable
    snapshot: Callable
    input_digest: Callable
    fault: Callable
    tool: str = 'run_display'
    scope_prefix: str = 'display'
    run_seconds: int = 30


class ScriptError(ValueError):
    """A source the contract refuses: `code`, a sentence and, where it has one, the line."""

    def __init__(self, code, message, line=None):
        super().__init__(message)
        self.code, self.message, self.line = code, message, line

    def public(self):
        return {'code': self.code, 'message': self.message, 'line': self.line}

def validate_source(text, *, filename='display.py', max_bytes=64 * 1024):
    """A script's source, checked as text: size, encoding, syntax and one top-level `def run(input)`.

    The source is parsed and never executed here. Raises ScriptError.
    """
    if not isinstance(text, str) or not text.strip():
        raise ScriptError('script_contract', 'Send the script’s Python source as text.')
    try:
        raw = text.encode('utf-8')
    except UnicodeEncodeError:
        raise ScriptError('script_contract', 'Send the script as UTF-8 text.') from None
    if len(raw) > max_bytes or '\x00' in text:
        raise ScriptError('script_contract', f'A script is up to {max_bytes // 1024} KB of UTF-8 text.')
    try:
        tree = ast.parse(text, filename=filename)
    except SyntaxError as exc:
        raise ScriptError('script_syntax', f'Syntax error: {exc.msg}.', exc.lineno) from None
    except (ValueError, RecursionError, MemoryError):
        raise ScriptError('script_syntax', 'The source is too deeply nested to parse.') from None
    runs = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'run']
    if len(runs) != 1 or isinstance(runs[0], ast.AsyncFunctionDef):
        raise ScriptError('script_contract', 'Define one function at the top of the script: def run(input).',
                          runs[1].lineno if len(runs) > 1 else runs[0].lineno if runs else None)
    arguments = runs[0].args
    positional = arguments.posonlyargs + arguments.args
    required = len(positional) - len(arguments.defaults)
    keyword_only = sum(1 for default in arguments.kw_defaults if default is None)
    if required > 1 or keyword_only or not (positional or arguments.vararg):
        raise ScriptError('script_contract', 'run takes one argument: def run(input).', runs[0].lineno)
    return text

class DenySocket:
    """The one socket a job mounts. A display script holds no network grant: every request gets 403.

    The listener is bounded, so a script that opens connections costs this service a fixed few
    descriptors: SOCKET_OPEN connections at once, each for a quarter of a second at most, and
    SOCKET_TOTAL in all. Past that total it stops accepting and `flooded` is set; the job then
    counts as a stopped run.
    """

    def __init__(self, directory):
        self.directory, self.sock, self.server, self.path, self.inode = Path(directory), None, None, None, None
        self.open, self.total, self.flooded = 0, 0, False

    async def start(self):
        from native_executor import ExecutorError
        info = self.directory.lstat()
        path = self.directory / 'custom-http.sock'
        if (not self.directory.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or len(os.fsencode(path)) >= 108 or path.exists() or path.is_symlink()):
            raise ExecutorError('custom_socket_required')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.bind(str(path))
            self.path = path
            os.chmod(path, 0o600)
            self.inode = path.lstat().st_ino
            self.sock.listen(SOCKET_OPEN)
            self.sock.setblocking(False)
            self.server = await asyncio.start_unix_server(self._refuse, sock=self.sock)
        except BaseException:
            await self.close()
            raise
        return self

    async def _refuse(self, reader, writer):
        self.total += 1
        if self.total > SOCKET_TOTAL and not self.flooded:
            self.flooded = True
            if self.server is not None:
                self.server.close()  # The listener takes its last connection here.
        if self.flooded or self.open >= SOCKET_OPEN:
            writer.transport.abort()
            return
        self.open += 1
        try:
            try:
                await asyncio.wait_for(reader.read(65536), .25)  # Take the request off the wire; its content changes nothing.
            except asyncio.TimeoutError:
                pass
            writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            await asyncio.wait_for(writer.drain(), 1)
        except Exception:
            pass
        finally:
            self.open -= 1
            writer.close()

    async def close(self):
        if self.server is not None:
            self.server.close()
            self.server = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.path is not None:
            try:
                info = self.path.lstat()
                if stat.S_ISSOCK(info.st_mode) and info.st_ino == self.inode:
                    self.path.unlink()
            except (FileNotFoundError, PermissionError):
                pass  # The broker owns the frozen path once the job has started.
            self.path = None

async def _drain(task):
    """Cleanup keeps its owner through a caller's cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return None if task.cancelled() else task.result()

class JobCounts:
    """The script jobs the broker took from this service: for the host and per company in any 24
    hours, and for the host in all.

    With a path the counts are durable (`display-script-jobs.sqlite` in the service's state root):
    a restart keeps them, so the numbers bound the host and never one process's lifetime. Without
    a path they live in this object, for a service that has yet to be configured and for tests.
    A job is counted before the broker hears of it and taken back when the broker refuses it, so a
    store that takes no write admits no job.
    """

    def __init__(self, path=None, *, clock=time.time, limits=None):
        self.limits = limits or (lambda: JobLimits())
        self.path, self.clock = (Path(path) if path else None), clock
        self._rows, self._total, self._next, self._lock = {}, 0, 0, threading.Lock()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS script_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, company TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS script_job_total(id INTEGER PRIMARY KEY CHECK(id=1), jobs INTEGER NOT NULL)')
        db.execute('INSERT OR IGNORE INTO script_job_total VALUES(1,0)')
        return db

    def numbers(self, company_id):
        """{'host', 'company'}: jobs in the last 24 hours; 'total': every job this host's service took."""
        since = self.clock() - 86400
        if self.path is None:
            with self._lock:
                recent = [company for at, company in self._rows.values() if at > since]
                return {'host': len(recent), 'company': recent.count(company_id), 'total': self._total}
        if not self.path.is_file():
            return {'host': 0, 'company': 0, 'total': 0}
        with closing(self._connect()) as db:
            host = db.execute('SELECT COUNT(*) FROM script_jobs WHERE at>?', (since,)).fetchone()[0]
            company = db.execute('SELECT COUNT(*) FROM script_jobs WHERE at>? AND company=?', (since, company_id)).fetchone()[0]
            total = db.execute('SELECT jobs FROM script_job_total WHERE id=1').fetchone()[0]
            db.execute('COMMIT')
        return {'host': host, 'company': company, 'total': total}

    def refused(self, company_id, *, retained):
        """The fault code that keeps one more job of this company out now, else None. `retained`
        says the broker's retention upgrade is installed, which the host states with its event-runs switch."""
        held = self.numbers(company_id)
        limits = self.limits()
        if not retained and held['total'] >= limits.history:
            return 'sandbox_history_share'
        if held['host'] >= (limits.host_retained if retained else limits.host):
            return 'sandbox_daily_limit'
        if held['company'] >= (limits.company_retained if retained else limits.company):
            return 'company_daily_limit'
        return None

    def count(self, company_id):
        """Count one job. Returns the mark `uncount` takes."""
        now = self.clock()
        if self.path is None:
            with self._lock:
                self._next += 1
                self._rows = {mark: row for mark, row in self._rows.items() if row[0] > now - 86400}
                self._rows[self._next], self._total = (now, company_id), self._total + 1
                return self._next
        with closing(self._connect()) as db:
            self.path.chmod(0o600)
            db.execute('DELETE FROM script_jobs WHERE at<=?', (now - 86400,))
            mark = db.execute('INSERT INTO script_jobs(at,company) VALUES(?,?)', (now, company_id)).lastrowid
            db.execute('UPDATE script_job_total SET jobs=jobs+1 WHERE id=1')
            db.execute('COMMIT')
        return mark

    def uncount(self, mark):
        """Take back the count of a job the broker refused."""
        if self.path is None:
            with self._lock:
                if self._rows.pop(mark, None) is not None:
                    self._total -= 1
            return
        with closing(self._connect()) as db:
            if db.execute('DELETE FROM script_jobs WHERE id=?', (mark,)).rowcount:
                db.execute('UPDATE script_job_total SET jobs=jobs-1 WHERE id=1 AND jobs>0')
            db.execute('COMMIT')

class ScriptSandbox:
    """Script jobs through the commissioned custom broker: one at a time in this service, each
    company in its turn.

    `run` drives the broker's own operations in the order the custom coordinator uses them:
    prepare, upload, bind the job socket, start, wait, read the result, stop, release. It returns
    a status for every script fault and raises `asyncio.CancelledError` alone.

    The turn. One job holds the service's sandbox turn. A job waits for it for a short, fixed time
    and then answers `unavailable` (`sandbox_turn`), having cost nothing. The company whose job
    gave up keeps the next turn for TURN_SECONDS, and the company that held the last turn yields
    while another company waits or keeps such a claim: one company's stream of jobs leaves every
    other company its turn.
    """

    def __init__(self, contract, *, enabled=False, event_runs=False, client=None, runtime=None, socket_factory=DenySocket, poll_seconds=.1,
                 busy_waits=BUSY_WAITS, job_seconds=JOB_SECONDS, slot_wait=SLOT_WAIT_SECONDS, counts=None, clock=time.monotonic, loop=None):
        self.contract = contract
        self.enabled, self.event_runs, self.loop = enabled, event_runs, loop
        self._client, self._runtime = client, runtime
        self.socket_factory, self.poll_seconds, self.busy_waits, self.job_seconds, self.clock = socket_factory, poll_seconds, busy_waits, job_seconds, clock
        self.slot_wait, self.counts = slot_wait, counts if counts is not None else JobCounts()
        self._slot, self._ready_until, self._ready, self.uncleared = None, 0.0, False, {}
        self._last, self._claims, self._line = None, {}, {}

    @property
    def client(self):
        if self._client is None:
            from native_custom_client import CustomClient
            self._client = CustomClient()
        return self._client

    @property
    def runtime(self):
        if self._runtime is None:
            import custom_worker
            from custom_execution import IMAGE, RuntimeProfile
            self._runtime = RuntimeProfile(IMAGE, hashlib.sha256(Path(custom_worker.__file__).read_bytes()).hexdigest())
        return self._runtime

    async def available(self, company_id, owner):
        """Whether the broker of this host is the commissioned one: same profile, image and worker."""
        from native_executor import ExecutorError, NativeScope
        if self._ready and self.clock() < self._ready_until:
            return True
        try:
            async with asyncio.timeout(10):
                value = await self.client.capabilities(NativeScope(company_id, owner, self.contract.scope_prefix + '-readiness'))
        except (ExecutorError, OSError, asyncio.TimeoutError, ValueError):
            self._ready = False
            return False
        self._ready = (value.get('schema_version') == 1 and value.get('available') is True
                       and all(value.get(key) == expected for key, expected in self.runtime.document().items()))
        self._ready_until = self.clock() + 60
        return self._ready

    async def _turn(self, company_id, wait):
        """Take the service's sandbox turn for a company. False when the turn is another company's
        or stays held for the whole wait."""
        if self._slot is None:
            self._slot = asyncio.Semaphore(1)
        now = self.clock()
        self._claims = {company: until for company, until in self._claims.items() if until > now}
        others = any(company != company_id for company in (*self._claims, *self._line))
        if others and self._last == company_id and company_id not in self._claims:
            return False
        self._line[company_id] = self._line.get(company_id, 0) + 1
        try:
            if self._slot.locked():
                await asyncio.wait_for(self._slot.acquire(), wait)
            else:
                await self._slot.acquire()  # A free turn is taken at once, whatever the wait.
        except asyncio.TimeoutError:
            self._claims[company_id] = self.clock() + TURN_SECONDS
            return False
        finally:
            if self._line[company_id] > 1:
                self._line[company_id] -= 1
            else:
                del self._line[company_id]
        self._claims.pop(company_id, None)
        self._last = company_id
        return True

    async def run(self, *, company_id, owner, collection, display_id, revision, source, document, cancelled=None, opened=None, closed=None,
                  wait_seconds=None, seconds=None):
        """One job. Returns {status: 'succeeded', results, log, seconds, broker_run_id, input_sha256, input_bytes},
        or {status: 'failed' | 'unavailable', error: {code, message, …}, seconds}. `started` says whether
        the broker holds a row for it: such a job counts against the day's numbers, and a job the
        broker refused or never heard of counts for nothing.

        `opened(job)` is called with {'request_id', 'scope'} before the broker hears of the job and
        `closed(job)` once its cleanup is confirmed, so a restart can clear what a stopped process left.
        `wait_seconds` is how long the job waits for the service's sandbox turn, and `seconds` how
        long the host waits for the job itself, inside JOB_SECONDS.
        """
        from native_executor import ExecutorError, NativeScope
        started = self.clock()
        done = lambda status, **more: {'status': status, 'seconds': round(self.clock() - started, 3), 'started': False, **more}
        if not self.enabled:
            return done('unavailable', error=self.contract.fault('sandbox_offline'))
        try:
            parts = self.contract.input_parts(document)
            snapshot = self.contract.snapshot(company_id=company_id, owner=owner, collection=collection, display_id=display_id,
                                    revision=revision, source=source, parts=parts, runtime=self.runtime)
        except ValueError:
            return done('failed', error=self.contract.fault('input_too_large'))
        if not await self._turn(company_id, self.slot_wait if wait_seconds is None else wait_seconds):
            return done('unavailable', error=self.contract.fault('sandbox_turn'))
        try:
            if not await self.available(company_id, owner):
                return done('unavailable', error=self.contract.fault('sandbox_offline'))
            if self.uncleared:
                await self.recover([])
            if self.uncleared:
                return done('unavailable', error=self.contract.fault('sandbox_cleanup'))
            try:
                kept_out = await asyncio.to_thread(self.counts.refused, company_id, retained=self.event_runs)
                mark = None if kept_out else await asyncio.to_thread(self.counts.count, company_id)
            except (sqlite3.Error, OSError):
                return done('unavailable', error=self.contract.fault('sandbox_offline'))  # The count takes no write: no job.
            if kept_out:
                return done('unavailable', error=self.contract.fault(kept_out))
            request_id = secrets.token_hex(16)
            scope = NativeScope(company_id, owner, self.contract.scope_prefix + '-run-' + request_id)
            job = {'request_id': request_id, 'scope': asdict(scope)}
            state = {'lease': None, 'listener': None, 'prepared': False}
            outcome = None
            try:
                if opened is not None:
                    await asyncio.to_thread(opened, job)
                outcome = await self._job(scope, request_id, snapshot, cancelled, state, min(self.job_seconds, seconds or self.job_seconds))
            except ExecutorError as exc:
                code = str(exc)
                outcome = ('unavailable', self.contract.fault('sandbox_busy' if code in {'capacity_reached', 'custom_output_capacity'} else
                           'sandbox_maintenance' if code == 'custom_history_capacity' else 'sandbox_offline'))
            except (OSError, asyncio.TimeoutError, ValueError, KeyError, TypeError, sqlite3.Error):
                outcome = ('unavailable', self.contract.fault('sandbox_offline'))
            finally:
                # A job counts once the broker holds a row for it: a lease, or the tombstone that
                # fences a prepare whose reply was lost. A request the broker refused counts for nothing.
                taken = state['lease'] is not None or state['prepared']
                if not taken:
                    await _drain(asyncio.ensure_future(asyncio.to_thread(self._uncount, mark)))
                cleared = await _drain(asyncio.ensure_future(self._clear(scope, request_id, state)))
                if cleared:
                    if closed is not None:
                        await _drain(asyncio.ensure_future(asyncio.to_thread(closed, job)))
                else:
                    self.uncleared[request_id] = (job, closed)
            if isinstance(outcome, dict):
                return done('succeeded', **outcome, started=taken, input_sha256=self.contract.input_digest(document), input_bytes=sum(len(raw) for _, raw in parts))
            return done(outcome[0], error=outcome[1], started=taken)
        finally:
            self._slot.release()

    def _uncount(self, mark):
        try:
            self.counts.uncount(mark)
        except (sqlite3.Error, OSError):
            pass

    async def _job(self, scope, request_id, snapshot, cancelled, state, job_seconds):
        from native_executor import ExecutorError
        client = self.client
        for wait in (*self.busy_waits, None):
            try:
                state['prepared'] = True
                state['lease'] = await client.prepare(scope, request_id, snapshot, self.contract.tool, {})
                break
            except ExecutorError as exc:
                if str(exc) in REFUSALS:
                    state['prepared'] = False  # The broker answered before it made a row: this request left nothing with it.
                if str(exc) not in {'capacity_reached', 'custom_output_capacity'} or wait is None:
                    raise
                await asyncio.sleep(wait)
        lease = state['lease']
        await client.upload(scope, lease, snapshot, self.contract.tool, {})
        state['listener'] = await self.socket_factory(lease.socket_dir).start()
        await client.start(scope, lease, secrets.token_urlsafe(32))
        running = self.clock()
        while True:
            status = (await client.status(scope, lease))['status']
            if status in {'succeeded', 'failed', 'stopped', 'released'}:
                break
            if status == 'uncertain':
                # The broker could not confirm the container gone. The job is the broker's to clear.
                return 'unavailable', self.contract.fault('sandbox_cleanup')
            if getattr(state['listener'], 'flooded', False):
                return 'failed', self.contract.fault('network')
            if cancelled is not None and cancelled.is_set():
                return 'failed', self.contract.fault('stopped')
            if self.clock() - running > job_seconds:
                return 'failed', self.contract.fault('time_limit')
            await asyncio.sleep(self.poll_seconds)
        if getattr(state['listener'], 'flooded', False):
            return 'failed', self.contract.fault('network')
        if status != 'succeeded':
            # The broker reports a stopped job by its status alone. A job the sandbox ended at its
            # time limit ran for the whole of it.
            return 'failed', self.contract.fault('time_limit' if self.clock() - running >= self.contract.run_seconds + 3 else 'script_stopped')
        result = await client.result(scope, lease, snapshot, self.contract.tool, {})
        envelope = result['result']
        if envelope.get('schema') != 1 or not isinstance(envelope.get('results'), list) or not envelope['results']:
            return 'failed', self.contract.fault('script_stopped')
        log = envelope.get('log') if isinstance(envelope.get('log'), str) else ''
        return {'results': envelope['results'], 'log': log[-4000:], 'broker_run_id': result['broker_run_id']}

    async def _clear(self, scope, request_id, state):
        """Stop and release the job, whatever happened. True once the broker confirmed it."""
        from native_executor import ExecutorError
        try:
            if state['listener'] is not None:
                await state['listener'].close()
            lease = state['lease']
            if lease is None and state['prepared']:
                # The reply to prepare may be lost: the tombstone keeps a delayed prepare from starting.
                lease = await self.client.recover(scope, request_id, retire=True)
            if lease is not None:
                await self.client.release(scope, lease)
            return True
        except (ExecutorError, OSError, asyncio.TimeoutError, ValueError):
            return False

    async def recover(self, pending, closed=None):
        """Clear jobs a stopped process left: [{'request_id', 'scope'}]. Returns the request ids cleared."""
        from native_executor import NativeScope
        cleared = []
        for job in [*pending, *[entry[0] for entry in self.uncleared.values()]]:
            try:
                scope = NativeScope(**job['scope'])
            except (TypeError, KeyError, RuntimeError):
                continue
            own = self.uncleared.get(job['request_id'], (None, closed))[1]
            if await self._clear(scope, job['request_id'], {'lease': None, 'listener': None, 'prepared': True}):
                self.uncleared.pop(job['request_id'], None)
                if own is not None:
                    await asyncio.to_thread(own, job)
                cleared.append(job['request_id'])
        return cleared
