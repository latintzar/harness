"""Durable, conversation-scoped progress through a vertical's job recipe."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

import pack
from job_recipes import available, launcher_for

READ_NAMES = frozenset({'get_recipe', 'list_recipe_runs', 'get_recipe_run'})
WRITE_NAMES = frozenset({'begin_recipe', 'record_recipe_step'})
SURFACE_READS = {surface: READ_NAMES for surface in ('public', 'native', 'local')}


def specs(write=True):
    key = {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,39}$'}
    run = {'type': 'string', 'pattern': '^job-[a-f0-9]{32}$'}
    definitions = {
        'get_recipe': ('Read the complete job recipe, its declared roles, tools and completion criteria.',
            {'recipe_id': key}, ['recipe_id']),
        'list_recipe_runs': ('Read the durable job progress in this conversation.', {}, []),
        'get_recipe_run': ('Read the saved recipe revision, progress and evidence for one job.', {'run_id': run}, ['run_id']),
        'begin_recipe': ('Start a job record from a recipe this conversation may run, or resume its existing unfinished record.',
            {'recipe_id': key}, ['recipe_id']),
        'record_recipe_step': ('Record this job step’s observed progress and evidence using the saved revision. Prerequisites must be completed first.',
            {'run_id': run, 'expected_revision': {'type': 'integer', 'minimum': 1}, 'step_id': key,
             'status': {'type': 'string', 'enum': ['running', 'completed', 'blocked']},
             'note': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
             'evidence': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 1000}}},
            ['run_id', 'expected_revision', 'step_id', 'status', 'note', 'evidence']),
    }
    return [{'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': fields, 'required': required, 'additionalProperties': False}}
        for name, (description, fields, required) in definitions.items() if write or name in READ_NAMES]


class RecipeRuns:
    def __init__(self, state):
        self.path = Path(state) / 'recipe-runs.sqlite'
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat TEXT NOT NULL,
                    collection TEXT NOT NULL, recipe TEXT NOT NULL, status TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated REAL NOT NULL, record TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS active_recipe ON jobs(owner,chat,collection,recipe)
                    WHERE status IN ('running','blocked');
            ''')
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def scope(context):
        context.require_current()
        if not context.chat_id or context.record is None:
            raise ValueError('Read recipes in their owning conversation.')
        return context.data_owner, context.chat_id, context.collection

    def _get(self, db, context, run_id):
        row = db.execute('SELECT record FROM jobs WHERE id=? AND owner=? AND chat=? AND collection=?',
            (run_id, *self.scope(context))).fetchone()
        if row is None:
            raise ValueError('Choose a job in this conversation.')
        return json.loads(row['record'])

    def get(self, context, run_id):
        with self.connect() as db:
            return self._get(db, context, run_id)

    def list(self, context):
        with self.connect() as db:
            return [json.loads(row['record']) for row in db.execute(
                'SELECT record FROM jobs WHERE owner=? AND chat=? AND collection=? ORDER BY updated DESC LIMIT 50',
                self.scope(context))]

    def begin(self, context, recipe):
        context.require_parent()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            scope = self.scope(context)
            row = db.execute('SELECT record FROM jobs WHERE owner=? AND chat=? AND collection=? AND recipe=? '
                "AND status IN ('running','blocked')", (*scope, recipe['id'])).fetchone()
            if row:
                return json.loads(row['record'])
            if db.execute('SELECT COUNT(*) FROM jobs WHERE owner=? AND status IN (?,?)',
                    (scope[0], 'running', 'blocked')).fetchone()[0] >= 100:
                raise ValueError('Finish an existing job before starting another.')
            now = time.time()
            record = {'id': 'job-' + uuid.uuid4().hex, 'recipe': copy.deepcopy(recipe),
                'status': 'running', 'revision': 1, 'created_at': now, 'updated_at': now,
                'steps': {step['id']: {'status': 'pending', 'note': '', 'evidence': []} for step in recipe['steps']}}
            context.require_parent()
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)', (record['id'], *scope,
                recipe['id'], record['status'], record['revision'], now, json.dumps(record)))
            return record

    def update(self, context, *, run_id, expected_revision, step_id, status, note, evidence):
        context.require_parent()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = self._get(db, context, run_id)
            if record['revision'] != expected_revision:
                raise ValueError('Read the current job revision before recording progress.')
            step = next((step for step in record['recipe']['steps'] if step['id'] == step_id), None)
            if step is None or record['status'] == 'completed' or record['steps'][step_id]['status'] == 'completed':
                raise ValueError('Record progress on an unfinished step in this job.')
            if any(record['steps'][key]['status'] != 'completed' for key in step['after']):
                raise ValueError('Complete this step’s prerequisites first.')
            if status == 'completed' and not evidence:
                raise ValueError('Record the observed evidence for this completed step.')
            record['steps'][step_id] = {'status': status, 'note': note, 'evidence': list(evidence),
                'reported_by': context.actor, 'updated_at': time.time()}
            states = {value['status'] for value in record['steps'].values()}
            record['status'] = 'completed' if states == {'completed'} else 'blocked' if 'blocked' in states else 'running'
            record['revision'] += 1
            record['updated_at'] = time.time()
            context.require_parent()
            db.execute('UPDATE jobs SET status=?,revision=?,updated=?,record=? WHERE id=?',
                (record['status'], record['revision'], record['updated_at'], json.dumps(record), run_id))
            return record


def _store(context):
    context.require_current()
    return RecipeRuns(context.state)


def read(context, name, arguments):
    if name == 'get_recipe':
        context.require_current()
        recipe = pack.active().recipe(arguments['recipe_id'])
        roles = {step['agent'] for step in recipe['steps']} - {'parent'}
        return {'recipe': recipe, 'sub_agents': [pack.active().sub_agent(key) for key in sorted(roles)]}
    if name == 'get_recipe_run':
        return _store(context).get(context, arguments['run_id'])
    return {'runs': _store(context).list(context)}


def call(context, name, arguments):
    context.require_parent()
    if name == 'begin_recipe':
        recipe = pack.active().recipe(arguments['recipe_id'])
        if recipe not in available(pack.active(), launcher_for(context.agent, context.chat_id)):
            raise ValueError('Start this recipe in its declared conversation or agent class.')
        return _store(context).begin(context, recipe)
    return _store(context).update(context, **arguments)


FAMILY = sys.modules[__name__]
