"""Retail display calculations and optional agent delivery on the shared signal ledger.

Large immutable inputs and completed publication receipts stay in the existing private
location store. The signal contains their digest/reference and the original native job
identity. Recovery consults that job and the saved publication; it never replays a script.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

PRODUCER = 'tracker_script'
HANDLER = 'retail_script'
SNAPSHOT = 'tracker_script_signal'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ScriptSignals:
    def __init__(self, store, company, *, sandbox=None, clock=time.time):
        self.store, self.company, self.sandbox = store, company, sandbox
        self.clock = clock
        self.invocations = {}
        self.tasks = {}
        from tracker_scripts import SCRIPT_PASS_SECONDS, SLOT_WAIT_SECONDS, HOST_GRACE_SECONDS
        self.timeout = SCRIPT_PASS_SECONDS + SLOT_WAIT_SECONDS + HOST_GRACE_SECONDS
        self.cleanup_seconds = HOST_GRACE_SECONDS * 2
        store.signals.register_producer(PRODUCER, self.authorized)
        store.signals.register(HANDLER, self.run, kind='script', timeout_seconds=self.timeout,
            cleanup_seconds=self.cleanup_seconds, cancel=self.cancel, reconcile=self.reconcile)

    def _snapshot(self, record):
        from tracker_scripts import _saved
        saved = _saved(self.store.path.parent, record['owner_principal_id'], SNAPSHOT,
            record.get('payload', {}).get('snapshot_id'))
        if saved is None or saved.get('input_sha256') != record.get('payload', {}).get('input_sha256'):
            raise ValueError('Read the original private script input.')
        return saved

    def _save(self, company, tracker, value):
        from tracker_scripts import TrackerScripts
        with self.store.connect() as db:
            terminal = {json.loads(row['body']).get('payload', {}).get('snapshot_id') for row in db.execute(
                "SELECT body FROM agent_wakes WHERE provider=? AND status IN ('completed','cancelled','screened','failed')",
                (PRODUCER,))}
        def save(head, put):
            prior = put.get(SNAPSHOT, value['id'])
            if prior is not None:
                if any(prior.get(key) != value.get(key) for key in
                        ('input_sha256', 'document_sha256', 'source_sha256', 'source_scope')):
                    raise ValueError('Preserve the original script input snapshot.')
                return False
            put(SNAPSHOT, value)
            # Retain bounded completed input history; unresolved native jobs keep their snapshot.
            rows = put.db.execute("SELECT id,body FROM records WHERE owner=? AND kind=? "
                "AND location=? ORDER BY rowid DESC", (company.owner, SNAPSHOT, tracker['location_id'])).fetchall()
            for row in rows[50:]:
                if row['id'] in terminal and json.loads(row['body']).get('publication_completed'):
                    put.drop(SNAPSHOT, row['id'])
            return True
        return TrackerScripts(company)._change(tracker, save)

    def _update(self, record, **changes):
        # Cleanup receipts remain writable after revocation; they only settle the original job.
        with sqlite3.connect(self.store.path.parent / 'location-intelligence.sqlite') as db:
            db.execute('BEGIN IMMEDIATE')
            key = (record['owner_principal_id'], SNAPSHOT, record['payload']['snapshot_id'])
            row = db.execute("SELECT body FROM records WHERE owner=? AND kind=? AND id=?", key).fetchone()
            if row is None:
                raise ValueError('Preserve the original script receipt.')
            saved = json.loads(row[0])
            saved.update(changes)
            db.execute("UPDATE records SET body=? WHERE owner=? AND kind=? AND id=?",
                (json.dumps(saved, sort_keys=True, allow_nan=False), *key))
        return saved

    def authorized(self, record):
        from core_trackers import tracker_records
        from tracker_scripts import TrackerScripts, script_camera_ids
        from tracker_runner import TrackerRuns, pass_access
        owner, collection = record['owner_principal_id'], record['collection']
        if record.get('actor_principal_id') != owner or record.get('producer') != PRODUCER:
            return False
        company = self.company(owner, collection)
        pass_access(company, record['source_scope']['location_id'])
        tracker = next((row for row in tracker_records(company.state, owner, collection)
            if row['id'] == record['producer_id']), None)
        scope = record['source_scope']
        if (tracker is None or tracker.get('generation_id') != scope.get('tracker_generation')
                or tracker['location_id'] != scope.get('location_id') or digest(tracker) != scope.get('tracker_sha256')):
            return False
        scripts = TrackerScripts(company)
        head = scripts.head(tracker)
        if (not head or head.get('published_revision') != scope.get('script_revision')
                or digest(head['policy']) != scope.get('policy_sha256')
                or hashlib.sha256(scripts.revision(tracker, head['published_revision'])['source'].encode()).hexdigest()
                    != scope.get('source_sha256')):
            return False
        cameras = set(scope['camera_ids']) | set(script_camera_ids(company.state, owner, tracker))
        access = company.scope.effective(company.actor, collection)
        if not access['can_query'] or not cameras <= set(access['camera_ids']):
            return False
        if record.get('target_kind') == 'job' and not self._snapshot(record).get('publication_completed'):
            runs = TrackerRuns(company.state)
            with runs.connect() as db:
                row = runs.execute(db, 'SELECT lease_token,lease_run_id,lease_until FROM display_schedule '
                    'WHERE owner=? AND collection=? AND scope_key=?', (owner, collection, tracker['location_id'])).fetchone() if db else None
            if (row is None or row['lease_run_id'] != scope.get('pass_id') or row['lease_token'] != scope.get('pass_lease')
                    or row['lease_until'] <= self.clock()):
                return False
        return True

    def processor(self, tracker, company, location_id, period, data):
        from tracker_scripts import box, run_script_tracker
        return run_script_tracker(company, location_id, tracker, period, data,
            sandbox=self.sandbox or box(), signals=self)

    def execute(self, company, tracker, document, source, context, perform, *, current):
        from tracker_scripts import TrackerScripts, box, input_digest, script_camera_ids
        sandbox = self.sandbox or box()
        head = TrackerScripts(company).head(tracker)
        source_scope = {'location_id':tracker['location_id'], 'tracker_generation':tracker.get('generation_id'),
            'camera_ids':sorted({row['id'] for row in document['records']['cameras']} |
                set((current or {}).get('camera_ids', [])) | set(script_camera_ids(company.state, company.owner, tracker))),
            'tracker_sha256':digest(tracker), 'script_revision':head['published_revision'],
            'policy_sha256':digest(head['policy']), 'source_sha256':hashlib.sha256(source.encode()).hexdigest(), 'pass_id':context.get('run_id'), 'pass_lease':context.get('lease_token')}
        mark = input_digest(document)
        identity = digest({'pass':context.get('run_id'), 'tracker':tracker['id'],
            'generation':tracker.get('generation_id'), 'revision':head['published_revision'], 'input':mark})
        snapshot = {'id':'script-signal-' + identity, 'input_sha256':mark, 'document_sha256':digest(document),
            'document':copy.deepcopy(document), 'source_sha256':hashlib.sha256(source.encode()).hexdigest(),
            'source_scope':source_scope, 'created_at':time.time()}
        created = self._save(company, tracker, snapshot)
        destination = None
        if head['policy']['wake'] != 'never' and tracker.get('agent_id'):
            try:
                definition = self.store.get(tracker['agent_id'])
                destination = {'agent_id':definition['id'], 'revision':definition['revision']}
            except ValueError:
                pass
        try:
            signal = self.store.wakes.enqueue_job(producer=PRODUCER, producer_id=tracker['id'],
                owner=company.owner, actor=company.owner, collection=company.collection, event_id=identity,
                payload={'snapshot_id':snapshot['id'], 'input_sha256':mark}, source_scope=source_scope,
                kind='event', chain=[{'kind':'script', 'handler':HANDLER}, {'kind':'deliver'}], destination=destination)
        except BaseException:
            if created:
                # A durable ingress receipt keeps its input even if the commit acknowledgement failed.
                with self.store.connect() as db:
                    linked = db.execute("SELECT 1 FROM agent_wakes WHERE owner=? AND "
                        "json_extract(body,'$.payload.snapshot_id')=? LIMIT 1",
                        (company.owner, snapshot['id'])).fetchone()
                if linked is None:
                    with sqlite3.connect(company.state / 'location-intelligence.sqlite') as db:
                        db.execute("DELETE FROM records WHERE owner=? AND kind=? AND id=?",
                            (company.owner, SNAPSHOT, snapshot['id']))
            raise
        control = Control(self, signal)
        self.invocations[signal['id']] = (perform, control)
        future = asyncio.run_coroutine_threadsafe(self.store.signals.process(signal), sandbox.loop)
        try:
            future.result(timeout=self.timeout + self.cleanup_seconds + 5)
        except FutureTimeout:
            control.halt.set()
            future.cancel()
        finally:
            self.invocations.pop(signal['id'], None)
        saved = self._snapshot(signal)
        if 'result' not in saved:
            raise ValueError('Review the original script signal and its cleanup receipt.')
        result = saved['result']
        final = self.store.wakes.get(signal['id'], owner=company.owner)
        if final['target_kind'] == 'agent' and final['status'] == 'pending':
            result['script_run']['wake'] = {'queued':True, 'id':signal['id']}
        return result

    async def run(self, payload, config, context):
        from signal_chain import StepHeld
        saved = self._snapshot(context['signal'])
        if digest(saved['document']) != saved['document_sha256']:
            raise ValueError('Preserve the original script input bytes.')
        entry = self.invocations.get(context['signal_id'])
        if entry is None:
            raise StepHeld('The original location pass owns this calculation.')
        perform, control = entry
        control.document = copy.deepcopy(saved['document'])
        control.context = context
        task = asyncio.create_task(asyncio.to_thread(perform, control))
        self.tasks[context['signal_id']] = task
        try:
            result = await asyncio.shield(task)
            control.before_publish()
            self._update(context['signal'], result=result, step_output=control.output,
                publication_completed=True)
            return control.output
        finally:
            if task.done():self.tasks.pop(context['signal_id'], None)

    async def cancel(self, context):
        entry = self.invocations.get(context['signal_id'])
        if entry is not None:entry[1].halt.set()
        task = self.tasks.get(context['signal_id'])
        if task is not None:
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            self.tasks.pop(context['signal_id'], None)
        return await self._cleanup(context)

    async def _cleanup(self, context):
        from tracker_runner import TrackerRuns
        from tracker_scripts import box
        saved = self._snapshot(context['signal'])
        job = context.get('external_identity') or saved.get('job')
        if saved.get('job') and job != saved['job']:
            raise ValueError('Reconcile the original admitted native job.')
        if not job:return {'quiescent':True, 'admitted':False}
        ledger = TrackerRuns(self.store.path.parent)
        pending = [row for row in ledger.jobs_pending() if row['request_id'] == job['request_id']]
        if pending:
            await (self.sandbox or box()).recover(pending, closed=ledger.job_close)
        quiescent = all(row['request_id'] != job['request_id'] for row in ledger.jobs_pending())
        return {'quiescent':quiescent, 'request_id':job['request_id']}

    async def recover_pending(self):
        """Settle interrupted original jobs before another location pass is admitted."""
        with self.store.connect() as db:
            pending = [json.loads(row['body']) for row in db.execute(
                "SELECT body FROM agent_wakes WHERE provider=? AND status='pending' ORDER BY created_at LIMIT 1000",
                (PRODUCER,))]
        for signal in pending:
            if (signal['id'] in self.invocations or signal['id'] in self.tasks
                    or signal.get('chain_status') not in {'uncertain', 'succeeded'}):
                continue
            if signal.get('chain_status') == 'uncertain':
                await self.store.signals.reconcile(signal['id'], owner=signal['owner_principal_id'])
            await self.store.signals.process(signal)

    async def reconcile(self, context):
        evidence = await self._cleanup(context)
        if evidence['quiescent'] is not True:return {'status':'uncertain', 'evidence':evidence}
        saved = self._snapshot(context['signal'])
        if saved.get('publication_completed'):
            return {'status':'succeeded', 'output':saved['step_output'], 'evidence':evidence}
        return {'status':'failed', 'evidence':evidence}


class Control:
    def __init__(self, adapter, signal):
        self.adapter, self.signal = adapter, signal
        self.halt = threading.Event()
        self.context = None
        self.output = {'payload':signal['payload'], 'deliver':not bool(signal.get('destination'))}
        self.job = None

    def opened(self, job):
        self.before_publish()
        self.adapter._update(self.signal, job=job, cleanup_confirmed=False)
        self.context['external'](job)
        self.job = job

    def closed(self, job):
        self.adapter._update(self.signal, cleanup_confirmed=True)

    def before_publish(self):
        if self.halt.is_set() or not self.context['current']():
            self.halt.set()
            raise ValueError('Current script authority ended.')
        if self.job is not None:
            from tracker_runner import TrackerRuns
            if any(row['request_id'] == self.job['request_id'] for row in TrackerRuns(self.adapter.store.path.parent).jobs_pending()):
                raise ValueError('Confirm cleanup of the original script job.')

    def delivery(self, wanted, text):
        self.output = {'payload':self.signal['payload'], 'text':text,
            'deliver':bool(wanted) if self.signal.get('destination') else True}


def install(selected):
    """Register against the actual host store so later delivery retains producer authority."""
    if 'script_signals' not in selected:
        from retail_records import Company
        def company(owner, collection):
            return Company(selected['state'], owner, collection, selected['business_scope'],
                selected['monitoring'], selected['location_intelligence'])
        selected['script_signals'] = ScriptSignals(selected['agents'], company)
    return selected['script_signals']


def processor(app_state, sandbox):
    def run(tracker, company, location_id, period, data):
        selected = next((value for value in app_state.workspaces.values()
            if Path(value['state']) == Path(company.state)), None)
        if selected is None:
            raise ValueError('Open the selected workspace before running its scripts.')
        adapter = install(selected)
        adapter.sandbox = sandbox
        return adapter.processor(tracker, company, location_id, period, data)
    return run
