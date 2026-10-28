"""Durable, explicitly activated agents with declared workspace inputs.

Each definition owns a persistent conversation; run snapshots and audit events stay immutable.
Input providers supply business-specific admission and context within the same run grant.
"""
from __future__ import annotations

import asyncio
import copy
from decimal import Decimal
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import time
import uuid

from agent_inputs import AgentDefinition, NoInputProvider


class AgentStore:
    def __init__(self, state, *, input_provider=None, wake_authorizer=None, agent_classes=None, authority=None):
        self.authority = authority
        self.signal_authority = None
        self.wake_authorizer = wake_authorizer
        self.inputs = NoInputProvider()
        self._inputs_installed = False
        self._inputs_sealed = False
        self._classes_installed = False
        self.agent_classes = {'action': {'kind':'action','authority':'granted_effects'}}
        if agent_classes is not None:
            self.install_classes(agent_classes)
        self.state = Path(state)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.state / "background-agents.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL,
                    run_id TEXT, at REAL NOT NULL, event TEXT NOT NULL, body TEXT NOT NULL);
            """)
            db.execute('BEGIN IMMEDIATE')
            # Adopt the newest historical run once. Its saved native thread can
            # resume directly; earlier runs and frozen reports retain their IDs.
            for row in db.execute("SELECT id,body FROM agents").fetchall():
                record = json.loads(row['body'])
                if not record.get('chat_id'):
                    latest = db.execute("SELECT body FROM runs WHERE agent_id=? ORDER BY rowid DESC LIMIT 1", (row['id'],)).fetchone()
                    record['chat_id'] = (json.loads(latest['body'])['chat_id'] if latest else 'agent-chat-' + uuid.uuid4().hex)
                    self._save(db, 'agents', record)
        self.path.chmod(0o600)
        from signal_chain import SignalLedger
        self.signals = SignalLedger(self)
        from agent_wakes import AgentWakeQueue
        self.wakes = AgentWakeQueue(self)
        self.inputs.attach(self)
        if input_provider is not None:
            self.install_inputs(input_provider)

    @property
    def definition_model(self):
        return self.inputs.definition_model

    def install_inputs(self, provider, *, wake_authorizer=None):
        if self._inputs_installed or self._inputs_sealed:
            raise ValueError("Install the input provider once, before scheduler recovery.")
        provider.attach(self)
        self.inputs = provider
        self._inputs_installed = True
        if wake_authorizer is not None:
            self.wake_authorizer = wake_authorizer

    def install_classes(self, declarations):
        if self._classes_installed or self._inputs_sealed:
            raise ValueError("Install declared agent classes once, before scheduler recovery.")
        classes = {}
        if not isinstance(declarations, (list, tuple)) or not declarations:
            raise ValueError("Install this vertical's declared agent classes.")
        for declaration in declarations:
            kind = declaration.get('kind') if isinstance(declaration, dict) else None
            if (not isinstance(kind, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', kind)
                    or kind in classes or declaration.get('authority') not in {'participant_reach','granted_effects'}):
                raise ValueError("Install distinct agent classes with their declared authority.")
            classes[kind] = copy.deepcopy(declaration)
        self.agent_classes = classes
        self._classes_installed = True

    def class_allowed(self, definition):
        return definition.get('agent_kind', 'action') in self.agent_classes

    def validate_class(self, definition):
        if not self.class_allowed(definition):
            raise ValueError("Choose an agent class declared by this vertical.")

    def install_authority(self, authority):
        if self._inputs_sealed or self.authority is not None or not callable(authority):
            raise ValueError('Install current workspace authority before scheduler recovery.')
        self.authority = authority

    @staticmethod
    def standing(record):
        return record.get('grant', {}).get('kind') == 'standing'

    def grant_current(self, record, *, now=None):
        now = time.time() if now is None else now
        if record.get('status') != 'active' or not self.class_allowed(record):
            return False
        grant = record.get('grant', {})
        actor = grant.get('actor_principal_id') or record.get('owner_principal_id')
        if self.standing(record):
            if (self.authority is None or grant.get('revision') != record.get('revision')
                    or not actor or grant.get('revoked_at') is not None):
                return False
        elif (record.get('expires_at') or 0) <= now:
            return False
        if self.authority is not None:
            try:
                return self.authority(record, actor) is True
            except (ValueError, OSError, sqlite3.Error):
                return False
        return True

    def run_current(self, run, *, now=None):
        now = time.time() if now is None else now
        current = self.get(run['agent_id'])
        return (run.get('status') == 'running' and run.get('expires_at', 0) > now
            and run['revision'] == current['revision'] and self.grant_current(current, now=now)
            and run.get('actor_principal_id', current.get('owner_principal_id')) ==
                current.get('grant', {}).get('actor_principal_id', current.get('owner_principal_id')))

    def seal_inputs(self):
        self._inputs_sealed = True

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _read(self, db, table, key):
        row = db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Choose an agent or run in this company.")
        result=json.loads(row["body"])
        self.inputs.normalize(result, table)
        return result

    @staticmethod
    def _save(db, table, data):
        db.execute(f"UPDATE {table} SET body=? WHERE id=?", (json.dumps(data), data["id"]))

    @staticmethod
    def _audit(db, agent_id, event, body=None, run_id=None):
        db.execute("INSERT INTO audit(agent_id,run_id,at,event,body) VALUES(?,?,?,?,?)",
                   (agent_id, run_id, time.time(), event, json.dumps(body or {})))

    def create(self, definition, datasets, *, owner_principal_id=None, input_context=None, conversation_chat_id=None):
        if isinstance(definition, AgentDefinition):
            definition = definition.model_dump(exclude_unset=True)
        if not isinstance(definition, dict):
            raise ValueError('Supply the declared agent definition fields.')
        defaults = {'agent_kind':next((kind for kind, row in self.agent_classes.items() if row.get('default')),
            'action' if 'action' in self.agent_classes else next(iter(self.agent_classes)))}
        declared = self.agent_classes.get(definition.get('agent_kind', defaults['agent_kind']), {})
        if declared.get('input_kinds'):
            defaults['input_kind'] = declared['input_kinds'][0]
        spec = self.definition_model.model_validate({**defaults, **definition}).model_dump()
        self.validate_class(spec)
        self.signals.validate_chain(spec['signal_chain'])
        if not self.inputs.collection_allowed(spec, datasets, owner_principal_id):
            raise ValueError("Choose an available business collection.")
        if owner_principal_id is not None and (not isinstance(owner_principal_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',owner_principal_id)):
            raise ValueError("Use the authenticated creator's principal identity.")
        if conversation_chat_id is not None and (not isinstance(conversation_chat_id, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', conversation_chat_id)):
            raise ValueError("Use a saved conversation identity from this workspace.")
        self.inputs.validate_definition({**spec,'owner_principal_id':owner_principal_id})
        now = time.time()
        record = {**spec, "id": "agent-" + uuid.uuid4().hex, "revision": 1, "status": "draft",
                  "chat_id": conversation_chat_id or "agent-chat-" + uuid.uuid4().hex,
                  "created_at": now, "updated_at": now, "runs_started": 0,
                  "next_run_at": None, "expires_at": None, "watermark": [], "once": False,
                  "resource_ceiling_usd":str(self.inputs.resource_ceiling()),"owner_principal_id":owner_principal_id}
        self.inputs.prepare_record(record, input_context)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM agents").fetchone()[0] >= 100:
                raise ValueError("This preview supports 100 saved agents per company.")
            if any(json.loads(row['body']).get('chat_id') == record['chat_id']
                   for row in db.execute('SELECT body FROM agents')):
                raise ValueError("This conversation already belongs to a saved agent.")
            db.execute("INSERT INTO agents VALUES (?,?)", (record["id"], json.dumps(record)))
            self._audit(db, record["id"], "created", spec)
        return self.get(record["id"])

    def get(self, key):
        with self.connect() as db:
            record = self._read(db, "agents", key)
            runs = [json.loads(r["body"]) for r in db.execute("SELECT body FROM runs WHERE agent_id=? ORDER BY rowid DESC", (key,))]
            self.inputs.describe(record, db)
        record.pop("watermark", None)
        record["runs"] = [{k:v for k,v in run.items() if k not in {"source_keys", "definition", "report_messages"}} for run in runs]
        record["last_run"] = record["runs"][0] if runs else None
        record["credit_ceiling_usd"] = str(Decimal(record["resource_ceiling_usd"]) * self.inputs.resource_allowance(record) * record["max_runs"])
        record["url"] = record["conversation_url"] = f"/?agents=1&agent={key}"
        return record

    def list(self, owner_principal_id=None):
        with self.connect() as db:
            keys = [r["id"] for r in db.execute("SELECT id FROM agents ORDER BY rowid DESC")]
        records=[self.get(key) for key in keys]
        return [record for record in records if owner_principal_id is None or record.get('owner_principal_id')==owner_principal_id]

    def run_report(self, agent_id, run_id):
        with self.connect() as db:
            run = self._read(db, "runs", run_id)
        if run["agent_id"] != agent_id:
            raise ValueError("Choose a run belonging to this agent.")
        return run

    def mark_conversation_run(self, run_id, message_start):
        """Pin the run's report boundary before appending to its shared chat."""
        if type(message_start) is not int or message_start < 0:
            raise ValueError("Use the conversation's current message boundary.")
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._read(db, 'runs', run_id)
            if run['status'] != 'running' or ('message_start' in run and run['message_start'] != message_start):
                raise ValueError("Preserve the admitted run's conversation boundary.")
            run['message_start'] = message_start
            self._save(db, 'runs', run)
        return run

    def pin_skills(self,run_id,snapshots):
        """Pin the host's resolved skill revisions once, before native execution."""
        context=None
        if isinstance(snapshots,dict):
            if snapshots.get('authority')!='instructions_only':
                raise ValueError('Skills provide instructions within the host’s existing grants.')
            context={key:value for key,value in snapshots.items() if key!='skills'}
            snapshots=snapshots.get('skills')
        if not isinstance(snapshots,list) or len(snapshots)>8 or any(not isinstance(item,dict) for item in snapshots):
            raise ValueError('Record the resolved skill snapshots for this run.')
        encoded=json.dumps(snapshots,sort_keys=True,allow_nan=False)
        if len(encoded)+len(json.dumps(context,allow_nan=False))>200_000:raise ValueError('Keep the selected skill context within the run limit.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');run=self._read(db,'runs',run_id)
            if [item.get('id') for item in snapshots]!=run['definition'].get('skill_ids',[]):
                raise ValueError('Resolved skills must match this run’s selected skill IDs.')
            if 'skill_snapshots' in run:
                if json.dumps(run['skill_snapshots'],sort_keys=True)!=encoded:
                    raise ValueError('Preserve the skill revisions originally pinned to this run.')
                if context is not None and run.get('skill_context')!=context:
                    raise ValueError('Preserve the scope and hash originally pinned to this run.')
                return
            if run['status']!='running':raise ValueError('Pin skill revisions before this run completes.')
            run['skill_snapshots']=json.loads(encoded)
            if context is not None:run['skill_context']=json.loads(json.dumps(context))
            self._save(db,'runs',run)
            self._audit(db,run['agent_id'],'skills_pinned',{'skill_ids':run['definition'].get('skill_ids',[])},run_id)

    @staticmethod
    def _snapshot_report(run, messages):
        encoded = json.dumps(messages, sort_keys=True)
        run["report_messages"] = json.loads(encoded)
        run["report_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()

    def freeze_report(self, run_id, messages):
        """Migrate earlier terminal reports once; later chat changes cannot alter them."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._read(db, "runs", run_id)
            if run["status"] not in {"running","recovering"} and "report_messages" not in run:
                self._snapshot_report(run, messages)
                self._save(db, "runs", run)
                self._audit(db, run["agent_id"], "report_preserved", {"sha256":run["report_sha256"]}, run_id)

    def update(self, key, definition, datasets, *, expected_revision=None, input_context=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, "agents", key)
            # Older clients revise cadence without knowing the observation input fields.
            inherited = {name:record[name] for name in self.inputs.inherited_fields}
            spec = self.definition_model.model_validate({**inherited,**definition}).model_dump()
            self.validate_class(record)
            self.validate_class(spec)
            self.signals.validate_chain(spec['signal_chain'])
            if spec['agent_kind'] != record['agent_kind']:
                raise ValueError('Keep this conversation’s original agent class; create a separate agent for another class.')
            if not self.inputs.collection_allowed(spec, datasets, record.get('owner_principal_id')):
                raise ValueError("Choose an available business collection.")
            self.inputs.validate_definition({**spec,'owner_principal_id':record.get('owner_principal_id')})
            if spec["dataset"] != record["dataset"]:
                raise ValueError("Keep this agent conversation in its original business collection.")
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision!=record['revision']):
                raise ValueError("Refresh this agent's latest revision before editing it.")
            if record["status"] == "active" or any(json.loads(r["body"])["status"] in {"running","recovering"} for r in db.execute("SELECT body FROM runs WHERE agent_id=?", (key,))):
                raise ValueError("Pause this agent before editing its instructions.")
            record.update(**spec, revision=record["revision"] + 1, status="draft", updated_at=time.time(),
                          runs_started=0, expires_at=None, next_run_at=None, watermark=[], once=False,
                          resource_ceiling_usd=str(self.inputs.resource_ceiling()), grant={})
            self.inputs.prepare_record(record, input_context)
            self._save(db, "agents", record)
            self.wakes.cancel_pending(db, key, 'definition_revised')
            self._audit(db, key, "revised", spec)
        return self.get(key)

    def activate(self, key, expires_at=None, *, once=False, now=None, expected_revision=None,
                 standing=False, actor_principal_id=None):
        self.validate_class(self.get(key))
        self.inputs.validate_activation(self.get(key))
        now = time.time() if now is None else now
        if not standing and (expires_at is None or expires_at <= now):
            raise ValueError("Sign in with a fresh code to activate this agent.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, "agents", key)
            self.validate_class(record)
            if expected_revision is not None and expected_revision != record["revision"]:
                raise ValueError("Review this agent's latest revision before activating it.")
            if self.inputs.resource_ceiling() > Decimal(record["resource_ceiling_usd"]):
                raise ValueError("Save a new revision to review the updated compute profile.")
            if record["status"] == "active":
                raise ValueError("This agent is already active.")
            if not standing and record["runs_started"] >= record["max_runs"]:
                raise ValueError("Revise the agent to authorize another run.")
            if standing:
                if once or record['mode'] == 'ad_hoc' or actor_principal_id != record.get('owner_principal_id'):
                    raise ValueError('Review recurring work as its owning workspace actor.')
                record['grant'] = {'kind':'standing','actor_principal_id':actor_principal_id,
                    'revision':record['revision'],'created_at':now,'daily_runs':record['max_runs']}
            else:
                record['grant'] = {'kind':'expiring','actor_principal_id':actor_principal_id or record.get('owner_principal_id'),
                    'revision':record['revision'],'created_at':now}
            record.update(status="active", expires_at=None if standing else min(expires_at, now + 86400), next_run_at=now,
                          updated_at=now, once=bool(once))
            if not self.grant_current(record, now=now):
                raise ValueError('Activate with current workspace membership and conversation access.')
            self._save(db, "agents", record)
            self._audit(db, key, "activated", {"revision":record["revision"], "expires_at":record["expires_at"], "once":once})
        return self.get(key)

    def active_since(self, key):
        """When this agent's latest activation began, from its audit trail; None before the first one.

        Run activation and phone delivery are separate controls. Host alert
        delivery listens from the moment the agent's phone records exist
        (phone_delivery.fan_out) and reads no activation.
        """
        with self.connect() as db:
            row = db.execute("SELECT at FROM audit WHERE agent_id=? AND event='activated' ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        return row['at'] if row else None

    def pause(self, key):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, "agents", key)
            record.update(status="paused", next_run_at=None, updated_at=time.time())
            if record.get('grant'):
                record['grant']['revoked_at'] = time.time()
            self._save(db, "agents", record)
            self.wakes.cancel_pending(db, key, 'agent_paused')
            self._audit(db, key, "paused")
        return self.get(key)

    def recover(self, *, now=None):
        """Keep standing grants; reconcile admitted work before another wake."""
        self.seal_inputs()
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.signals.recover(db)
            for row in db.execute("SELECT body FROM agent_wakes WHERE status='claimed'").fetchall():
                signal=json.loads(row['body'])
                if signal.get('target_kind')=='chat' and 'message_start' not in signal:
                    self.signals.release_unstarted(db,signal['id'])
                    signal.update(status='pending',run_id=None)
                    self.wakes._write(db,signal)
            for row in db.execute('SELECT body FROM agents').fetchall():
                record = json.loads(row['body'])
                standing = self.standing(record)
                if not self.class_allowed(record):
                    record.update(status='needs_attention', next_run_at=None, updated_at=now)
                    self._save(db, 'agents', record)
                    self._audit(db, record['id'], 'class_unavailable')
                elif record['status'] == 'active' and (not standing or not self.grant_current(record, now=now)):
                    record.update(status='paused', next_run_at=None, updated_at=now)
                    self._save(db, 'agents', record)
                    self._audit(db, record['id'], 'restart_paused')
                if record['status'] != 'active':
                    self.wakes.cancel_pending(db, record['id'], 'execution_grant_ended', now)
            for row in db.execute('SELECT body FROM runs').fetchall():
                run = json.loads(row['body'])
                if run['status'] not in {'running','recovering'}:
                    continue
                definition = self._read(db, 'agents', run['agent_id'])
                if 'message_start' in run or run.get('resource_uses'):
                    # Withdrawal ends execution authority, not the obligation to
                    # reconcile an admitted provider request and its children.
                    run.update(status='recovering', error='Reconcile the original managed request.')
                    self._save(db, 'runs', run)
                    continue
                if self.standing(definition) and self.grant_current(definition, now=now):
                    run.update(status='deferred',completed_at=now,error=None)
                    definition['runs_started']=max(0,definition['runs_started']-1)
                    self.signals.release_unstarted(db,run['id'])
                    wake=self.wakes.get_for_run(db,run['id'])
                    if wake:
                        wake.update(status='pending',run_id=None)
                        self.wakes._write(db,wake)
                    self._save(db,'agents',definition)
                else:
                    run.update(status='interrupted',completed_at=now,error='Resume this expired execution explicitly.')
                    self.wakes.finish(db,run['id'],'interrupted',now)
                self._save(db,'runs',run)
                self.inputs.recovered(db,run)

    def input_optional_wake(self, definition, wake, *, admit=False):
        """An adapter may admit its own signal without a fresh input batch."""
        if not wake or wake.get('requires_input', True) or self.wake_authorizer is None:
            return False
        now = time.time()
        if (not self.signals.producer_current(wake) or not self.grant_current(definition, now=now)
                or wake.get('expires_at', 0) <= now or wake.get('revision') != definition.get('revision')
                or wake.get('agent_id') != definition.get('id')
                or wake.get('owner_principal_id') != definition.get('owner_principal_id')):
            return False
        try:
            return self.wake_authorizer(definition, wake, admit=admit) is True
        except (ValueError, OSError, sqlite3.Error):
            return False

    def claim(self, key, sources, *, now=None, source_records=None, screening=None, wake_id=None):
        self.validate_class(self.get(key))
        if not self.inputs.runnable(self.get(key)):return None
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, "agents", key)
            self.validate_class(record)
            if record["status"] != "active":
                return None
            if not self.grant_current(record, now=now):
                record.update(status="paused", next_run_at=None, updated_at=now)
                self._save(db, "agents", record)
                self.wakes.cancel_pending(db, key, 'grant_expired', now)
                self._audit(db, key, "grant_expired")
                return None
            wake = self.wakes.for_claim(db, wake_id, record, now) if wake_id else None
            if wake_id and wake is None:
                return None
            input_optional = self.input_optional_wake(record, wake)
            if wake and wake.get('requires_input') is False and not input_optional:
                return None
            if record["next_run_at"] > now and wake is None:
                return None
            # One running investigation per company; the scheduler also bounds global concurrency.
            if (any(json.loads(r["body"])["status"] in {"running", "recovering"} for r in db.execute("SELECT body FROM runs"))
                    or self.wakes.chat_in_flight(db)):
                return None
            if not self.standing(record) and record["runs_started"] >= record["max_runs"]:
                record.update(status="completed", next_run_at=None)
                self._save(db, "agents", record)
                return None
            selection = self.inputs.select(db, record, sources, source_records, screening, now, wake)
            selected = selection.sources
            if not selected and not selection.allow_empty and not input_optional:
                if wake and screening is not None:
                    self.wakes._retire(db, wake, 'screened', 'no_candidates', now)
                if record['mode']=='ongoing' and record['once']:
                    record.update(status='paused',next_run_at=None)
                else:record["next_run_at"] = now + record["interval_seconds"]
                self._save(db, "agents", record)
                return None
            run = {"id": "run-" + uuid.uuid4().hex, "agent_id": key, "chat_id": record["chat_id"],
                   "owner_principal_id":record.get('owner_principal_id'),
                   "actor_principal_id":record.get('grant', {}).get('actor_principal_id',record.get('owner_principal_id')),
                   "status": "running", "created_at": now, "completed_at": None, "error": None,
                   "definition": {k:record[k] for k in self.definition_model.model_fields}, "revision":record["revision"],
                   "source_keys": list(selected), "source_ids": list(selected.values()), "source_count":len(selected),
                   "resource_uses":0, "credit_committed_ceiling_usd":"0", "expires_at":min(record['expires_at'] or now+1800,now+1800)}
            if not self.signals.reserve(db,'turn',run['id'],target=key,now=now,
                    target_limit=record['max_runs'] if self.standing(record) else None):
                record['next_run_at']=(int(now//86400)+1)*86400
                self._save(db,'agents',record)
                return None
            # Providers may annotate selected input, never replace execution authority.
            protected = set(run) - {'source_ids', 'source_count'}
            if protected & selection.fields.keys():
                raise ValueError("Input metadata must preserve the store's execution authority.")
            run.update(selection.fields)
            run["resource_ceiling_usd"] = record['resource_ceiling_usd']
            run["trigger"] = "event" if wake else "manual" if record["once"] or record["mode"] == "ad_hoc" else record["mode"]
            if wake:
                self.wakes.claimed(db, wake, run['id'])
                run['wake'] = copy.deepcopy(wake)
            run["url"] = f"/?chat={run['chat_id']}"
            run["report_url"] = f"/api/agents/{key}/runs/{run['id']}/report"
            db.execute("INSERT INTO runs VALUES(?,?,?)", (run["id"], key, json.dumps(run)))
            self.inputs.claimed(db, record, run)
            record.update(runs_started=record["runs_started"]+1, updated_at=now)
            self._save(db, "agents", record)
            self._audit(db, key, "run_started", {"revision":record["revision"], "source_count":len(selected)}, run["id"])
            return run

    def defer_unstarted_run(self, run_id):
        """A manual turn winning the dispatch race leaves the wake and allowance pending."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._read(db, 'runs', run_id)
            if run['status'] != 'running' or 'message_start' in run or run['resource_uses']:
                raise ValueError("Defer only work that has not entered its conversation or provider.")
            definition = self._read(db, 'agents', run['agent_id'])
            run.update(status='deferred', completed_at=time.time(), error=None)
            self._snapshot_report(run, [])
            definition['runs_started'] = max(0, definition['runs_started'] - 1)
            self.signals.release_unstarted(db, run_id)
            self._save(db, 'runs', run)
            self._save(db, 'agents', definition)
            self.inputs.recovered(db, run)
            if run.get('wake'):
                wake = self.wakes.get_for_run(db, run_id)
                if wake:
                    wake.update(status='pending', run_id=None)
                    if self.wakes._eligible(wake, definition, time.time()):
                        self.wakes._write(db, wake)
                    else:
                        self.wakes._retire(db, wake, 'cancelled', 'execution_grant_ended', time.time())
            self._audit(db, run['agent_id'], 'run_deferred', {'reason':'conversation_busy'}, run_id)

    def schedule_signal(self, definition, *, now=None):
        now=time.time() if now is None else now
        if (definition['mode']=='ad_hoc' or definition.get('once') or not definition.get('owner_principal_id')
                or not self.grant_current(definition,now=now) or (definition.get('next_run_at') or 0)>now):
            return None
        return self.wakes.enqueue(definition['id'],owner=definition['owner_principal_id'],
            expected_revision=definition['revision'],provider='schedule',connection_id=definition['id'],
            event_id=f"{definition['revision']}:{definition['next_run_at']}",kind='schedule',
            text=definition['instruction'],payload={'text':definition['instruction'],'scheduled_at':definition['next_run_at']},now=now)

    def advance_filtered_schedule(self, signal):
        if signal.get('kind')!='schedule':return
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._read(db,'agents',signal['agent_id'])
            if record['revision']==signal['revision'] and self.grant_current(record):
                record['next_run_at']=time.time()+record['interval_seconds']
                self._save(db,'agents',record)

    def hold(self, run_id, *, error='Reconcile the original managed request.'):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run=self._read(db,'runs',run_id)
            if run['status'] in {'running','recovering'}:
                run.update(status='recovering',error=error)
                self._save(db,'runs',run)

    def resume_recovery(self, run_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run=self._read(db,'runs',run_id)
            definition=self._read(db,'agents',run['agent_id'])
            if (run['status']!='recovering' or not self.standing(definition)
                    or not self.grant_current(definition) or run['revision']!=definition['revision']):
                return None
            run.update(status='running',expires_at=time.time()+1800)
            self._save(db,'runs',run)
            return run

    def execution_current(self, run_id):
        with self.connect() as db:
            run=self._read(db,'runs',run_id)
        return self.run_current(run)

    def admit_resource(self, run_id, maximum_usd):
        maximum = Decimal(maximum_usd)
        if not maximum.is_finite() or maximum <= 0 or maximum > self.inputs.resource_ceiling():
            raise ValueError("This resource request exceeds the activated agent's resource profile.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._read(db, "runs", run_id)
            self.validate_class(run["definition"])
            self.inputs.validate_activation(run["definition"])
            if maximum > Decimal(run["resource_ceiling_usd"]):
                raise ValueError("This resource request exceeds the activated run's credit ceiling.")
            definition = self._read(db, "agents", run["agent_id"])
            if (run["status"] != "running" or definition["status"] != "active"
                    or not self.grant_current(definition) or run["expires_at"] <= time.time()):
                raise ValueError("This agent's execution grant has ended.")
            if run["resource_uses"] >= self.inputs.resource_allowance(run["definition"]):
                raise ValueError("This run’s resource allowance is used. Continue with the existing workspace records.")
            run["resource_uses"] += 1
            self.inputs.resource_admitted(run)
            run["credit_committed_ceiling_usd"] = str(Decimal(run["credit_committed_ceiling_usd"]) + maximum)
            self._save(db, "runs", run)
            self._audit(db, run["agent_id"], "resource_admitted", {"maximum_usd":str(maximum)}, run_id)

    def finish(self, run_id, status, error=None, *, now=None, report_messages=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._read(db, "runs", run_id)
            if run["status"] not in {"running","recovering"}:
                return
            run.update(status=status, error=error, completed_at=now)
            self._snapshot_report(run, report_messages or [])
            record = self._read(db, "agents", run["agent_id"])
            if status == "completed":
                record["watermark"] = sorted(set(record["watermark"]) | set(run["source_keys"]))
            self.inputs.finished(db, run, status)
            if record["status"] == "active":
                if status != "completed":
                    record.update(status="needs_attention", next_run_at=None)
                elif record["mode"] == "ad_hoc" or (not self.standing(record) and record["runs_started"] >= record["max_runs"]):
                    record.update(status="completed", next_run_at=None)
                elif record["once"]:
                    record.update(status="paused", next_run_at=None)
                else:
                    record["next_run_at"] = now + record["interval_seconds"]
            record["updated_at"] = now
            self._save(db, "runs", run)
            self._save(db, "agents", record)
            self.wakes.finish(db, run_id, status, now)
            if record['status'] != 'active':
                self.wakes.cancel_pending(db, record['id'], 'execution_grant_ended', now)
            self._audit(db, record["id"], "run_" + status, {"error":error}, run_id)


def ensure_agent_conversation(store, agent, definition, *, briefing=False):
    """Materialize only the host-bound chat; preserve its messages and native ID."""
    record = store.get(definition) if isinstance(definition, str) else definition
    identity = record['chat_id']
    child = agent.children.get(identity)
    if child is not None:
        if (child.get('owner_principal_id') != record.get('owner_principal_id')
                or child.get('dataset') != record['dataset'] or child.get('parent_id')
                or child.get('agent_id') not in {None, record['id']}):
            raise ValueError("Continue the saved agent in its owning conversation and business.")
    else:
        child = {'id':identity, 'parent_id':None, 'messages':[], 'running':False,
                 'owner_principal_id':record.get('owner_principal_id'), 'dataset':record['dataset'],
                 'created_at':record['created_at']}
        if briefing:
            child['messages'].append({'role':'user', 'content':record['instruction'],
                'created_at':datetime.fromtimestamp(record['created_at'], timezone.utc).isoformat(),
                'origin':'agent_briefing'})
        agent.children[identity] = child
    if child.get('active_run_id'):
        active_run = store.run_report(record['id'], child['active_run_id'])
        if active_run['status'] not in {'running','recovering'}:
            child.pop('active_run_id', None)
            child['running'] = False
    child.update(kind='agent_conversation', agent_id=record['id'], agent_kind=record.get('agent_kind','action'), title=record['title'],
        agent_setup={'agent_id':record['id'], 'agent_kind':record.get('agent_kind','action'), 'mode':record['mode']})
    agent._save_child(child)
    return child


class AgentScheduler:
    def __init__(self, store, agent, slots=None):
        self.store, self.agent, self.slots = store, agent, slots or asyncio.Semaphore(1)
        self.store.seal_inputs()
        self.tasks = {}
        self.closing = False
        self.next_recovery_at = 0
        self.input_error = None
        self.next_refresh_at = 0
        self.store.recover()
        for definition in self.store.list():
            for run in definition["runs"]:
                child = self.agent.children.get(run["chat_id"], {})
                messages = child.get("messages", [])
                # Shared conversations contain earlier turns. A run cancelled
                # before dispatch never acquired a report interval.
                if child.get('kind') == 'agent_conversation' and 'message_start' not in run:
                    messages = []
                self.store.freeze_report(run["id"], messages[run.get('message_start', 0):])
            self.ensure_conversation(definition)

    def ensure_conversation(self, definition, *, briefing=False):
        return ensure_agent_conversation(self.store, self.agent, definition, briefing=briefing)

    async def recover_pending(self):
        runtime=getattr(self.agent,'managed_runtime',None)
        if runtime is None or not hasattr(runtime,'recover_request'):
            return
        for definition in self.store.list():
            for run in definition['runs']:
                if run['status']!='recovering':continue
                child=self.agent.children.get(run['chat_id'],{})
                start=run.get('message_start')
                messages=child.get('messages',[])
                original=messages[start] if type(start) is int and start<len(messages) else {}
                logical_id=original.get('managed_request_id') if original.get('role')=='user' else None
                if not logical_id:
                    self.store.hold(run['id'],error='Recover the original managed request identity before resuming.')
                    if child:
                        child['active_run_id']=run['id'];self.agent._save_child(child)
                    continue
                admitted=self.store.resume_recovery(run['id'])
                # A denied grant enters the runtime's cancellation-only recovery path.
                child.update(active_run_id=run['id'],run_id=run['id'])
                self.agent._save_child(child)
                try:
                    result=await runtime.recover_request(self.agent,run['chat_id'],logical_id)
                    status=result.get('status','uncertain')
                    if status not in {'completed','failed','cancelled','missing'}:
                        self.store.hold(run['id'])
                        continue
                    self.store.finish(run['id'],'completed' if status=='completed' else 'interrupted',
                        None if status=='completed' else 'Review the original managed request.',
                        report_messages=child['messages'][start:])
                    child.pop('active_run_id',None)
                    child['running']=False
                    self.agent._save_child(child)
                except BaseException:
                    self.store.hold(run['id'])
                    raise

    async def tick(self):
        self.tasks = {key:task for key,task in self.tasks.items() if not task.done()}
        if time.time() >= self.next_recovery_at and not self.tasks:
            self.next_recovery_at=time.time()+30
            await self.recover_pending()
        if self.closing or self.tasks or self.slots.locked():
            return
        if time.time() < self.next_refresh_at:
            return
        try:
            await self.store.inputs.refresh(self.store.list())
            self.input_error = None
        except (ValueError, OSError) as exc:
            self.input_error = str(exc)[:300]
            self.next_refresh_at = time.time() + 30
            return
        for definition in self.store.list():
            if definition["status"] != "active" or not self.store.class_allowed(definition):
                continue
            conversation = definition['chat_id']
            pending = getattr(self.agent, 'child_tasks', {}).get(conversation)
            if conversation in getattr(self.agent, 'active', {}) or pending is not None and not pending.done():
                continue
            wake = await asyncio.to_thread(self.store.wakes.pending, definition['id'], owner=definition.get('owner_principal_id'))
            if wake is None:
                wake=await asyncio.to_thread(self.store.schedule_signal,definition)
            if wake:
                pending_signal=wake
                wake = await self.store.signals.process(wake)
                if wake is None:
                    latest=self.store.wakes.get(pending_signal['id'],owner=pending_signal['owner_principal_id'])
                    if latest['status']=='screened':self.store.advance_filtered_schedule(latest)
                    continue
            input_optional = self.store.input_optional_wake(definition, wake)
            if wake and wake.get('requires_input') is False and not input_optional:
                continue
            if not self.store.inputs.ready(definition, wake, self.agent, input_optional):
                continue
            await self.slots.acquire()
            try:
                batch = await self.store.inputs.prepare(definition, wake, self.agent)
                if batch is None:
                    self.slots.release()
                    continue
                run = await asyncio.to_thread(self.store.claim, definition['id'], batch.sources,
                    source_records=batch.records, screening=batch.screening, wake_id=wake['id'] if wake else None)
                if run:
                    task = asyncio.create_task(self._execute(run,input_snapshot=batch.snapshot))
                    self.tasks[definition["id"]] = task
                    task.add_done_callback(lambda done, rid=run["id"]: self._finished(rid, done))
                    return
            except BaseException:
                self.slots.release()
                raise
            self.slots.release()

    def _finished(self, run_id, task):
        self.slots.release()
        if task.cancelled():
            if self.closing:return
            self.store.finish(run_id, "interrupted", "The investigation was paused.")
        elif task.exception() is not None:
            self.store.finish(run_id, "failed", str(task.exception())[:500])

    async def _execute(self, run, *, input_snapshot=None):
        if not self.store.execution_current(run['id']):
            self.store.finish(run['id'], 'interrupted', 'Continue with current workspace execution authority.')
            return
        self.store.validate_class(run["definition"])
        self.store.inputs.validate_activation(run["definition"])
        wake = run.get('wake')
        if wake and wake.get('requires_input') is False:
            definition = self.store.get(run['agent_id'])
            if not self.store.input_optional_wake(definition, wake, admit=True):
                self.store.finish(run['id'], 'interrupted', 'The signal requires its current source access.')
                return
        child = self.ensure_conversation(run['agent_id'])
        pending = getattr(self.agent, 'child_tasks', {}).get(run['chat_id'])
        if run['chat_id'] in getattr(self.agent, 'active', {}) or pending is not None and not pending.done():
            self.store.defer_unstarted_run(run['id'])
            return
        message_start = len(child['messages'])
        self.store.mark_conversation_run(run['id'], message_start)
        child.update(run_id=run['id'], active_run_id=run['id'], running=True)
        self.agent._save_child(child)
        spec = run["definition"]
        status, error = "failed", "The investigation ended before completion."
        try:
            with self.store.inputs.context(self.agent, run, input_snapshot) as message:
                async with asyncio.timeout(max(.01, min(1800, run["expires_at"] - time.time()))):
                    options={'skill_ids':spec['skill_ids']} if spec.get('skill_ids') else {}
                    if run.get('actor_principal_id'):
                        options['sender'] = {'principal_id':run['actor_principal_id'],'channel':'web'}
                    if run.get('wake'):
                        wake = run['wake']
                        metadata = {k:wake[k] for k in ('id','provider','connection_id','event_id','kind','sender_id','channel_id')}
                        options['model_context'] = message + '\nAuthenticated host event origin (reference data): ' + json.dumps(metadata)
                        message = (message if wake.get('kind')=='schedule' else
                            wake.get('processed_text',wake['text']) or 'Review the newly available evidence for this saved agent.')
                    async for event in self.agent.turn(message, run["chat_id"], spec["dataset"],**options):
                        if run.get('wake') and len(child['messages']) > message_start:
                            submitted = child['messages'][message_start]
                            if submitted.get('role') == 'user' and 'origin' not in submitted:
                                submitted['origin'] = {k:run['wake'][k] for k in
                                    ('id','provider','connection_id','event_id','kind','sender_id','channel_id')}
                                self.agent._save_child(child)
                        if event["type"] == "error":
                            error = event["message"]
                        elif event["type"] == "done":
                            status = "completed" if event.get("status") in {"completed", "waiting_for_reply"} else "interrupted"
                            error = None if status == "completed" else "The investigation was stopped."
        except asyncio.CancelledError:
            status, error = "interrupted", "The investigation was paused."
            raise
        except Exception as exc:
            error = str(exc)[:500] or "The investigation needs attention."
        finally:
            # Children inherit the same allowance and cannot outlive this execution grant.
            for child_id, record in list(self.agent.children.items()):
                if record.get("parent_id") == run["chat_id"]:
                    task = self.agent.child_tasks.get(child_id)
                    if task and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    record["running"] = False
                    self.agent._save_child(record)
            child["running"] = False
            child.pop("active_run_id", None)
            if status != "completed":
                for message in child["messages"][message_start:]:
                    if message.get("role") == "assistant":
                        for tool in message.get("tools", []):
                            if tool.get("status") == "running":
                                tool["status"] = "interrupted"
            self.agent._save_child(child)
            if getattr(self,'closing',False) and self.store.standing(self.store.get(run['agent_id'])):
                self.store.hold(run['id'])
            else:
                self.store.finish(run["id"], status, error, report_messages=child["messages"][message_start:])
            # Phone alerts that arrived during this run reached their participants as host
            # work. Their chat entries join the conversation now that its turn has drained.
            delivery = getattr(self.agent, 'phone_delivery', None)
            if delivery is not None:
                try:
                    delivery.sync_entries()
                except (ValueError, OSError, sqlite3.Error):
                    pass

    async def pause(self, key):
        self.store.pause(key)
        task = self.tasks.get(key)
        if task and not task.done():
            # Cancellation immediately reaches the active tool/provider cleanup.
            # A stalled native interrupt RPC must never delay that cancellation.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.store.get(key)

    async def close(self):
        self.closing=True
        for task in self.tasks.values():
            if not task.done():task.cancel()
        await asyncio.gather(*self.tasks.values(),return_exceptions=True)
