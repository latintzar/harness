"""Named, funded read jobs that run in separate OpenAI-managed sessions."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import json
import sys
import time

import pack
from job_recipes import launcher_for
from managed_sessions import ManagedSessions, TERMINAL, digest
from managed_turn import current_managed_call

READ_NAMES = frozenset()
WRITE_NAMES = ASYNC_NAMES = frozenset({'delegate_job'})
SURFACE_READS = {surface: frozenset() for surface in ('public', 'native', 'local')}


def specs(write=True):
    return [] if not write else [{'name': 'delegate_job', 'description':
        'Run one declared read job in its own bounded managed session and return its saved findings.',
        'inputSchema': {'type': 'object', 'properties': {
            'role_id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,39}$'},
            'task': {'type': 'string', 'minLength': 1, 'maxLength': 8000}},
            'required': ['role_id', 'task'], 'additionalProperties': False}}]


def role_plan(agent, chat_id):
    if agent.children.get(chat_id, {}).get('parent_id'):
        return []
    launcher = launcher_for(agent, chat_id)
    return [copy.deepcopy(role) for role in pack.active().sub_agents() if launcher in role['launched_by']]


@dataclass(frozen=True)
class DelegatedGrant:
    agent: object
    parent_id: str
    parent_run: dict
    admission: object
    actor: str
    job: dict

    @property
    def role(self):
        return self.job['role']

    def current(self):
        try:
            engine = self.parent_run['managed_engine']
            engine.require_current()
            record = self.agent.children.get(self.job['chat_id'], {})
            parent = self.agent.children.get(self.parent_id, {})
            return bool(self.agent.active.get(self.parent_id) is self.parent_run
                and time.time() < self.job.get('expires_at', 0)
                and self.admission.current() and self.agent._authority_principal(self.parent_id) == self.actor
                and engine.binding['id'] == self.job['parent_binding']
                and engine.request['id'] == self.job['parent_request']
                and not parent.get('parent_id')
                and (parent.get('owner_principal_id'), parent.get('dataset')) == (self.job['owner'], self.job['collection'])
                and record.get('kind') == 'managed_job_child' and record.get('parent_id') == self.parent_id
                and record.get('managed_root_id') == self.parent_id
                and (record.get('owner_principal_id'), record.get('dataset')) == (self.job['owner'], self.job['collection'])
                and self.role in role_plan(self.agent, self.parent_id))
        except (KeyError, ValueError, RuntimeError):
            return False

    def configuration(self, tools):
        allowed = set(self.role['tools'])
        selected = [copy.deepcopy(tool) for tool in tools if tool['name'] in allowed]
        if {tool['name'] for tool in selected} != allowed:
            raise ValueError('Install every declared read tool before running this role.')
        return {'agent': {'model': self.role['model'], 'instructions': self.role['instructions'],
            'reasoning': {'effort': self.role['effort'], 'summary': 'concise'}, 'service_tier': 'default',
            'multi_agent': {'enabled': False}, 'tools': selected}, 'environment': {'type': 'none'}}


class DelegatedJobs:
    def __init__(self, state):
        self.ledger = ManagedSessions(state)
        with self.ledger.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS delegated_jobs (
                id TEXT PRIMARY KEY, parent_binding TEXT NOT NULL REFERENCES sessions(id),
                parent_request TEXT NOT NULL, parent_turn TEXT NOT NULL, call_id TEXT NOT NULL,
                role_id TEXT NOT NULL, ordinal INTEGER NOT NULL, arguments_hash TEXT NOT NULL,
                record TEXT NOT NULL, UNIQUE(parent_binding,call_id),
                UNIQUE(parent_binding,parent_request,role_id,ordinal))''')

    def claim(self, context, role, task, call):
        context.require_parent()
        engine = context.run.get('managed_engine')
        if (engine is None or call is None or call['binding_id'] != engine.binding['id']
                or call['request_id'] != engine.request['id'] or not context.run.get('managed_lease')):
            raise ValueError('Launch this job through its admitted managed function call.')
        if role not in context.run.get('role_plan', []):
            raise ValueError('Use a role preallocated for this parent turn.')
        if not set(role['tools']) <= context.agent.tool_registry.reads:
            raise ValueError('Declare available read tools for this job.')
        fingerprint = digest({'role': role, 'task': task, 'actor': context.actor,
                              'owner': context.data_owner, 'collection': context.collection})
        with self.ledger.transaction() as db:
            row = db.execute('SELECT * FROM delegated_jobs WHERE parent_binding=? AND call_id=?',
                             (call['binding_id'], call['call_id'])).fetchone()
            if row:
                if row['arguments_hash'] != fingerprint or row['parent_request'] != call['request_id'] or row['parent_turn'] != call['turn_id']:
                    raise ValueError('Keep this managed call bound to its original job.')
                return json.loads(row['record'])
            ordinal = db.execute('SELECT COUNT(*) FROM delegated_jobs WHERE parent_binding=? AND parent_request=? AND role_id=?',
                                 (call['binding_id'], call['request_id'], role['id'])).fetchone()[0]
            if ordinal >= role['per_turn']:
                raise ValueError('Use this role within its per-turn job allowance.')
            key = digest({'binding': call['binding_id'], 'call': call['call_id']})[:32]
            job = {'id': 'delegated-' + key, 'chat_id': 'managed-job-' + key, 'role': copy.deepcopy(role),
                'parent_id': context.chat_id, 'parent_binding': call['binding_id'], 'parent_request': call['request_id'],
                'parent_turn': call['turn_id'], 'call_id': call['call_id'], 'ordinal': ordinal,
                'funded_parent_request': context.run['managed_lease'].request_id,
                'actor': context.actor, 'owner': context.data_owner, 'collection': context.collection,
                'task': task, 'status': 'pending', 'created_at': time.time(),
                'expires_at': time.time() + role['timeout_seconds']}
            context.require_parent()
            db.execute('INSERT INTO delegated_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                (job['id'], call['binding_id'], call['request_id'], call['turn_id'], call['call_id'],
                 role['id'], ordinal, fingerprint, json.dumps(job)))
            return job

    def save(self, job):
        with self.ledger.transaction() as db:
            db.execute('UPDATE delegated_jobs SET record=? WHERE id=?', (json.dumps(job), job['id']))

    def children(self, binding_id):
        with self.ledger.transaction() as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT record FROM delegated_jobs WHERE parent_binding=?', (binding_id,))]

    async def execute(self, context, role, task, call):
        job = self.claim(context, role, task, call)
        agent, root, run = context.agent, context.chat_id, context.run
        child = agent.children.get(job['chat_id'])
        if child is None:
            child = {'id': job['chat_id'], 'parent_id': root, 'managed_root_id': root,
                'kind': 'managed_job_child', 'managed_job_id': job['id'], 'title': role['title'],
                'owner_principal_id': job['owner'], 'dataset': job['collection'], 'status': 'pending',
                'messages': [{'role': 'user', 'content': task, 'managed_request_id': job['id']},
                             {'role': 'assistant', 'content': '', 'tools': [], 'results': []}]}
            agent.children[child['id']] = child
            agent._save_child(child)
        grant = DelegatedGrant(agent, root, run, context.admission, context.actor, job)
        if not grant.current():
            raise ValueError('Run the job through its current parent authority and declared role.')
        run['child_ids'].add(child['id'])
        public = {'type': 'subchat', 'chat_id': child['id'], 'parent_id': root, 'title': child['title'],
                  'url': '/?chat=' + child['id'], 'status': child['status']}
        run['tool_events'].put_nowait(public)
        if job['status'] in TERMINAL:
            return self.result(job, child)
        tasks = run.setdefault('delegated_tasks', {})
        running = tasks.get(job['id'])
        if running is None:
            running = asyncio.create_task(self._run(context, job, child, grant))
            tasks[job['id']] = running
        try:
            await asyncio.shield(running)
        except asyncio.CancelledError:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            raise
        context.require_current()
        return self.result(job, child)

    @staticmethod
    def result(job, child):
        answer = next((message for message in reversed(child['messages']) if message.get('role') == 'assistant'), {})
        return {'chat_id': child['id'], 'url': '/?chat=' + child['id'], 'role_id': job['role']['id'],
                'status': job['status'], 'answer': str(answer.get('content', ''))[:12000]}

    async def _run(self, context, job, child, grant):
        agent, runtime = context.agent, context.agent.managed_runtime
        answer = child['messages'][-1]
        binding = self.ledger.request_binding(context.company_id, child['id'], job['id'])
        request = self.ledger.request_record(binding['id'], job['id']) if binding else None
        try:
            if request is not None and request['status'] in TERMINAL:
                job['status'] = request['status']
            else:
                child['running'] = True
                child['status'] = 'running'
                agent._save_child(child)
                async for event in runtime.run(agent, job['task'], child['id'], job['collection'],
                    delegated_grant=grant, recovering=request is not None, recovery_answer=answer):
                    agent._record_event(answer, event)
                    agent._save_child(child)
                binding = self.ledger.request_binding(context.company_id, child['id'], job['id'])
                request = self.ledger.request_record(binding['id'], job['id']) if binding else None
                job['status'] = request['status'] if request is not None else 'failed'
        finally:
            binding = self.ledger.request_binding(context.company_id, child['id'], job['id'])
            request = self.ledger.request_record(binding['id'], job['id']) if binding else None
            if request is not None:
                job['status'] = request['status']
            child['running'] = False
            child['status'] = job['status']
            self.save(job)
            agent._save_child(child)
            if grant.current():
                context.run['tool_events'].put_nowait({'type': 'subchat', 'chat_id': child['id'],
                    'parent_id': context.chat_id, 'title': child['title'], 'url': '/?chat=' + child['id'],
                    'status': child['status']})


async def acall(context, name, arguments):
    context.require_parent()
    role = next((role for role in role_plan(context.agent, context.chat_id) if role['id'] == arguments['role_id']), None)
    if role is None:
        raise ValueError('Choose a role declared for this conversation.')
    return await DelegatedJobs(context.state).execute(context, role, arguments['task'], current_managed_call())


FAMILY = sys.modules[__name__]


async def withdraw_children(runtime, agent, parent_binding):
    """Cancellation uses original saved IDs and needs no renewed tool authority."""
    from managed_runtime import quiescent
    store = DelegatedJobs(agent.state)
    complete = True
    for job in store.children(parent_binding['id']):
        child_binding = store.ledger.request_binding(parent_binding['company_id'], job['chat_id'], job['id'])
        request = store.ledger.request_record(child_binding['id'], job['id']) if child_binding else None
        if request is None or request['status'] in TERMINAL:
            continue
        if child_binding['session_id'] is None:
            if request['status'] == 'submitted':
                complete = False
                continue
            store.ledger.request_state(request['id'], 'cancelled')
        else:
            try:
                async with runtime.client_factory() as client:
                    key = store.ledger.submission(child_binding['id'], 'parent-stop-' + request['id'], 'cancel', {})
                    await client.interrupt(child_binding['session_id'], submission_key=key)
                    async with asyncio.timeout(30):
                        while True:
                            stopped, _ = await quiescent(client, child_binding['session_id'])
                            if stopped:
                                store.ledger.request_state(request['id'], 'cancelled')
                                break
                            await asyncio.sleep(0.1)
            except (ValueError, RuntimeError, OSError, TimeoutError):
                complete = False
                continue
        job['status'] = 'cancelled'
        store.save(job)
        child = agent.children.get(job['chat_id'])
        if child:
            child.update(status='cancelled', running=False)
            agent._save_child(child)
    return complete
