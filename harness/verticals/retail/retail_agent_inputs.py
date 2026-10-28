"""Retail inputs for saved agents: observation admission, source scope and screening."""
from __future__ import annotations
import asyncio
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import re
import time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from agent_inputs import AgentDefinition, AgentInputProvider, InputBatch, InputSelection
from watch_screening import WatchPlan, WatchScreener, author_plan, validate_plan, source_key, digest, coalesce
from retired_tools import is_retired_archive_agent, ARCHIVE_AGENT_REASON, RETIREMENT_MESSAGE

OBSERVATION_NOTICE = 'Configure your agent here. Scheduled and ongoing runs start on committed observations.'


class RetailAgentDefinition(AgentDefinition):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    instruction: str = Field(min_length=1, max_length=8000)
    description: str = Field(default="", max_length=220)
    watch_for: list[str] = Field(default_factory=list, max_length=8)
    dataset: str = Field(min_length=1, max_length=100)
    agent_kind: str = Field(default='action', pattern=r'^[a-z][a-z0-9_]{0,79}$')
    input_kind: Literal['none', 'camera_footage', 'tracker_events'] = 'camera_footage'
    profile_ids: list[str] = Field(default_factory=list, max_length=50)
    event_types: list[str] = Field(default_factory=list, max_length=128)
    mode: Literal["ad_hoc", "scheduled", "ongoing"] = "ad_hoc"
    interval_seconds: int = Field(default=3600, ge=10, le=604800)
    max_runs: int = Field(default=1, ge=1, le=20)
    fresh_inspections_per_run: int = Field(default=1, ge=0, le=1)
    output: Literal["report"] = "report"
    evidence_window: Literal["auto", "archive", "last_interval"] = "auto"
    lookback_seconds: int | None = Field(default=None, ge=60, le=604800)
    skill_ids: list[str] = Field(default_factory=list, max_length=8)
    watch_plan: WatchPlan | None = None

    @model_validator(mode="after")
    def validate_schedule(self):
        if not self.title.strip() or not self.instruction.strip():
            raise ValueError("Give the agent a title and an investigation instruction.")
        if any(not item.strip() or len(item)>200 for item in self.watch_for):
            raise ValueError("Describe each thing to look for in one short line.")
        if self.input_kind in {'none', 'tracker_events'}:
            self.fresh_inspections_per_run = 0
            if self.watch_plan is not None:
                raise ValueError('Event-input agents use committed observations and profile filters.')
        if len(set(self.profile_ids)) != len(self.profile_ids):
            raise ValueError('Use distinct monitoring profile references.')
        if (len(set(self.event_types)) != len(self.event_types)
                or any(not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', kind) for kind in self.event_types)):
            raise ValueError('Choose distinct event type identifiers from the monitoring vocabulary.')
        if self.mode == "scheduled" and self.interval_seconds < 60:
            raise ValueError("Scheduled investigations run at intervals of at least one minute.")
        if self.mode == "ad_hoc":
            self.max_runs = 1
        if (len(set(self.skill_ids))!=len(self.skill_ids)
                or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}',key) for key in self.skill_ids)):
            raise ValueError("Choose distinct registered skill identifiers.")
        return self

def inspection_ceiling():
    from cosmos_inspect import MODEL, execution_profile
    return Decimal(execution_profile(MODEL)["planned_gross_usd"])


def _capture_interval(source):
    """Use only explicit imported wall-clock provenance, never file/index times."""
    def timestamp(value):
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError('Capture timestamps require an explicit timezone.')
        return parsed.timestamp()
    try:
        if source.get('capture_start_utc') and source.get('capture_end_utc'):
            start,end=timestamp(source['capture_start_utc']),timestamp(source['capture_end_utc'])
        elif source.get('source_capture_start_utc'):
            origin=timestamp(source['source_capture_start_utc'])
            start,end=origin+float(source['start']),origin+float(source['end'])
        else:return None
        if not (start<end and math.isfinite(start) and math.isfinite(end)):return None
        return start,end
    except (ValueError,TypeError,AttributeError,KeyError,OverflowError):
        return None


def evidence_scope(definition,sources,source_records,now):
    requested=definition.get('evidence_window','auto')
    use_interval=requested=='last_interval' or (requested=='auto' and definition['mode']=='scheduled' and not definition.get('once'))
    records={source['id']:source for source in (source_records or [])}
    scope={'requested':requested,'basis':'source_relative_archive','utc_window_applied':False,
        'available_source_count':len(sources),'capture_timestamps_missing':0,'capture_filter_excluded_count':0,
        'lookback_seconds':None}
    if not use_interval:
        scope['selected_source_count']=len(sources)
        return dict(sources),scope
    lookback=definition.get('lookback_seconds') or definition['interval_seconds']
    begin=now-lookback
    scope.update(lookback_seconds=lookback,
        requested_window_start_utc=datetime.fromtimestamp(begin,timezone.utc).isoformat(),
        requested_window_end_utc=datetime.fromtimestamp(now,timezone.utc).isoformat())
    selected={};known=0
    for key,source_id in sources.items():
        interval=_capture_interval(records.get(source_id,{}))
        if interval is None:
            scope['capture_timestamps_missing']+=1
            selected[key]=source_id
        else:
            known+=1
            if interval[0]<now and interval[1]>begin:selected[key]=source_id
            else:scope['capture_filter_excluded_count']+=1
    scope.update(selected_source_count=len(selected),utc_window_applied=bool(known))
    if known:
        scope['basis']='mixed_capture_utc_and_source_relative' if scope['capture_timestamps_missing'] else 'capture_utc'
    return selected,scope



class RetailInputProvider(AgentInputProvider):
    definition_model = RetailAgentDefinition
    inherited_fields = ('agent_kind', 'input_kind', 'profile_ids', 'event_types', 'signal_chain')

    def __init__(self, *, monitoring=None, index=None, live_input_ready=None):
        self.monitoring = monitoring
        self.index = index
        self.live_input_ready = live_input_ready or (lambda collection: False)
        self.screener = None

    def attach(self, store):
        super().attach(store)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS watch_windows (agent_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    source_key TEXT NOT NULL, clip_id TEXT NOT NULL, status TEXT NOT NULL,
                    matches TEXT NOT NULL, screening_id TEXT NOT NULL, run_id TEXT,
                    PRIMARY KEY(agent_id,revision,source_key));
                CREATE TABLE IF NOT EXISTS watch_screens (id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,body TEXT NOT NULL);
            """)
        self.bind(self.index)

    def bind(self, index=None, live_input_ready=None):
        if index is not None:
            self.index = index
            self.screener = WatchScreener(self.store.state, index)
        if live_input_ready is not None:
            self.live_input_ready = live_input_ready

    def normalize(self, record, table):
        record.setdefault('resource_ceiling_usd', record.get('inspection_ceiling_usd', '2.50'))
        if table == 'runs':
            record.setdefault('resource_uses', record.get('fresh_inspections', 0))
        if table == 'agents':
            for name in ('input_kind','profile_ids','event_types','watch_for','evidence_window',
                         'lookback_seconds','watch_plan','fresh_inspections_per_run'):
                item = self.definition_model.model_fields[name]
                record.setdefault(name, item.get_default(call_default_factory=True))
        return super().normalize(record, table)

    def collection_allowed(self, spec, datasets, owner):
        return (super().collection_allowed(spec, datasets, owner) or bool(self.monitoring and owner
                and spec['dataset'] in self.monitoring.registered_datasets(owner)))

    def validate_definition(self, spec):
        if spec['input_kind'] == 'none':
            return
        if self.monitoring is not None:
            self.monitoring.validate_definition(spec)

    def definition_context(self, agent, chat_id, arguments):
        if arguments.get('mode') != 'ongoing' or arguments.get('input_kind','camera_footage') != 'camera_footage':
            return None
        run = getattr(agent, 'active', {}).get(chat_id)
        if not chat_id or not arguments.get('watch_plan') or run is None or not run.get('business_context_read'):
            raise ValueError('Read get_business_context and author the observable watch_plan before saving an ongoing agent.')
        from watch_screening import encoder_identity
        return {'chat_id':chat_id, 'encoder':encoder_identity(self.index)}

    def prepare_record(self, record, context=None):
        record['inspection_ceiling_usd'] = record['resource_ceiling_usd']
        record['watch_plan_provenance'] = author_plan(self.store.state, record['watch_plan'],
            record['revision'], record.get('owner_principal_id'), context)
        record.pop('last_screening', None)
        record.pop('screening_error', None)

    def runnable(self, record):
        return not is_retired_archive_agent(record)

    def validate_activation(self, record):
        if not self.runnable(record):
            raise ValueError(RETIREMENT_MESSAGE)
        if record['mode']=='ongoing' and record['input_kind']=='camera_footage':
            try:validate_plan(record,self.store.state)
            except ValueError as exc:raise ValueError('Review an agent-authored screening plan before activating this ongoing watch.') from exc

    def resource_ceiling(self):
        return inspection_ceiling()

    def resource_allowance(self, definition):
        return definition['fresh_inspections_per_run']

    def validate_resource_budget(self, definition, *, runs=1):
        allowance = self.resource_allowance(definition)
        if allowance:
            from query_budget import QueryBudget
            available = QueryBudget(self.store.state/'compute/query-approval.json').status()['available_usd']
            if self.resource_ceiling() * allowance * runs > Decimal(available):
                raise ValueError('Fund this agent’s bounded resource allowance.')

    def resource_admitted(self, run):
        run['fresh_inspections'] = run['resource_uses']

    def describe(self, record, db):
        key=record['id']
        coverage={row['status']:row['n'] for row in db.execute('SELECT status,COUNT(*) n FROM watch_windows WHERE agent_id=? AND revision=? GROUP BY status',(key,record['revision']))}
        if record['mode']=='ongoing' and record['input_kind']=='camera_footage':
            try:validate_plan(record,self.store.state);ready=True
            except ValueError:ready=False
            record['screening_status']='ready' if ready else 'plan_review_required'
            record['screening_coverage']={'screened_window_count':sum(coverage.values()),
                'pending_candidate_count':coverage.get('pending',0),'investigated_window_count':coverage.get('investigated',0),
                'investigating_window_count':coverage.get('investigating',0)}
        if is_retired_archive_agent(record):
            record.update(saved_status=record['status'],status='retired',execution_status='retired',retirement_reason=ARCHIVE_AGENT_REASON)

    def recovered(self, db, run):
        db.execute("UPDATE watch_windows SET status='pending',run_id=NULL WHERE run_id=? AND status='investigating'",(run['id'],))

    def finished(self, db, run, status):
        db.execute("UPDATE watch_windows SET status=?,run_id=NULL WHERE run_id=? AND status='investigating'",('investigated' if status=='completed' else 'pending',run['id']))

    def claimed(self, db, record, run):
        if run.get('screening') is not None:
            db.executemany("UPDATE watch_windows SET status='investigating',run_id=? WHERE agent_id=? AND revision=? AND source_key=?",[(run['id'],record['id'],record['revision'],k) for k in run['source_keys']])

    def prepare_screening(self,key,sources,*,source_records=None,now=None,force=False):
        if is_retired_archive_agent(self.store.get(key)):return None
        """Select unscreened identities; commit coverage only after successful CPU work."""
        now=time.time() if now is None else now
        with self.store.connect() as db:
            record=self.store._read(db,'agents',key)
            if record['mode']!='ongoing' or record['status']!='active' or (record['next_run_at']>now and not force):return None
            if not self.store.grant_current(record,now=now):return None
            validate_plan(record,self.store.state)
            selected,_=evidence_scope(record,sources,source_records,now)
            seen={row['source_key'] for row in db.execute('SELECT source_key FROM watch_windows WHERE agent_id=? AND revision=?',(key,record['revision']))}
            return record,{k:v for k,v in selected.items() if k not in seen}

    def screening_failed(self,key,revision,error):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');record=self.store._read(db,'agents',key)
            if record['revision']==revision and record['status']=='active':
                record.update(status='needs_attention',next_run_at=None,screening_error=str(error)[:500])
                self.store._save(db,'agents',record);self.store._audit(db,key,'screening_needs_attention',{'error':str(error)[:500]})

    def select(self, db, record, sources, source_records, screening, now, wake):
        if record['input_kind'] == 'none':
            return super().select(db, record, sources, source_records, screening, now, wake)
        key=record['id']
        seen = set(record["watermark"])
        channel_reply = bool(wake and wake['kind'] == 'channel_reply')
        event_input=record['input_kind']=='tracker_events'
        if event_input:
            from monitoring import is_reference_event
            original=[]
            for item in source_records or []:
                if self.monitoring is None or is_reference_event(item):continue
                try:saved=self.monitoring.get_event(record['owner_principal_id'],item['id'])
                except (ValueError,KeyError):continue
                if (saved==item and not is_reference_event(saved) and saved['dataset']==record['dataset']
                        and (not record['profile_ids'] or saved['profile_id'] in record['profile_ids'])
                        and (not record['event_types'] or saved['type'] in record['event_types'])):
                    original.append(saved)
            source_records=original
            allowed={e['id']:e['source']['clip_id'] for e in original}
            sources={key:value for key,value in sources.items() if allowed.get(key)==value}
            scope={'basis':'committed_observations','utc_window_applied':False,'requested':record.get('evidence_window','auto'),'lookback_seconds':None,'batch_limit':64,'calendar_filter_applied':False}
        else:
            sources,scope=evidence_scope(record,sources,source_records,now)
        selected = {k:v for k,v in sources.items() if record["mode"] != "ongoing" or record["once"] or channel_reply or k not in seen}
        candidates=[];screen_meta=None
        if record['mode']=='ongoing' and not channel_reply and not event_input:
            plan,provenance=validate_plan(record,self.store.state)
            if screening is None:raise ValueError('Screen committed windows before starting this ongoing investigation.')
            receipt=screening['receipt'];rows=screening['rows']
            if any(receipt.get(k)!=v for k,v in {'agent_id':key,'definition_revision':record['revision'],
                    'plan_sha256':provenance['plan_sha256'],'encoder':provenance['encoder'],
                    'dataset':record['dataset'],'company_scope':provenance['company_scope']}.items()):
                raise ValueError('Preserve this saved revision and scope while screening.')
            prior={r['source_key'] for r in db.execute('SELECT source_key FROM watch_windows WHERE agent_id=? AND revision=?',(key,record['revision']))}
            expected={k:v for k,v in sources.items() if k not in prior}
            if (len(rows)!=len(expected) or {r['source_key']:r['clip_id'] for r in rows}!=expected
                    or receipt['source_keys_sha256']!=digest(sorted(expected))):
                raise ValueError('Retry screening from the current committed source snapshot.')
            if rows:
                db.execute('INSERT INTO watch_screens VALUES(?,?,?)',(receipt['id'],key,json.dumps(receipt)))
                db.executemany('INSERT INTO watch_windows VALUES(?,?,?,?,?,?,?,NULL)',[
                    (key,record['revision'],r['source_key'],r['clip_id'],'pending' if r['matches'] else 'no_hit',json.dumps(r['matches']),receipt['id']) for r in rows])
                record['last_screening']=receipt
                self.store._audit(db,key,'windows_screened',receipt)
            pending=[dict(row) for row in db.execute("SELECT * FROM watch_windows WHERE agent_id=? AND revision=? AND status='pending' ORDER BY rowid",(key,record['revision'])) if row['source_key'] in sources][:64]
            selected={r['source_key']:r['clip_id'] for r in pending}
            records={c['id']:c for c in source_records or []}
            candidates=[records[r['clip_id']] for r in pending]
            screen_meta={'candidate_window_count':len(candidates),'candidate_intervals':coalesce(candidates),
                'screening_ids':sorted({r['screening_id'] for r in pending}),
                'matches':[{'clip_id':r['clip_id'],'families':json.loads(r['matches'])} for r in pending],
                'plan_sha256':provenance['plan_sha256'],'encoder':provenance['encoder'],
                'meaning':'Candidate similarities require source verification before conclusions.'}
        run = {'source_ids': list(selected.values()), 'source_count': len(selected)}
        if event_input:
            event_records={e['id']:e for e in source_records or []}
            run['observations']=[event_records[key] for key in selected if key in event_records]
        run['evidence_scope']={**scope,'selected_source_count':len(selected),
            'incremental_new_windows':record['mode']=='ongoing' and not record['once']}
        if screen_meta is not None:
            # Include only immediate touching neighbors in this collection,
            # bounded separately from the 64 candidate-window claim.
            context=[];candidate_ids=set(run['source_ids'])
            by_video={}
            for source in source_records or []:
                if source['id'] in sources.values():by_video.setdefault(source['video_id'],[]).append(source)
            for sequence in by_video.values():
                sequence.sort(key=lambda c:(c['start'],c['end'],c['id']))
                for i,source in enumerate(sequence):
                    if source['id'] not in candidate_ids:continue
                    for j in (i-1,i+1):
                        if 0<=j<len(sequence):
                            neighbor=sequence[j]
                            gap=max(source['start'],neighbor['start'])-min(source['end'],neighbor['end'])
                            if gap<=1e-6 and neighbor['id'] not in candidate_ids and neighbor['id'] not in context:context.append(neighbor['id'])
            context=context[:32]
            run['source_ids']+=context;run['source_count']=len(run['source_ids'])
            run['screening']={**screen_meta,'context_clip_ids':context}
            run['evidence_scope']['selected_source_count']=run['source_count']
        run.update(fresh_inspections=0, inspection_ceiling_usd=record['resource_ceiling_usd'])
        return InputSelection(selected, run)

    def validate_wake(self,record,definition):
        if record.get('provider')!='monitoring':return True
        monitoring=self.monitoring
        if monitoring is None:return False
        from monitoring import is_reference_event
        try:event=monitoring.get_event(record['owner_principal_id'],record['event_id'])
        except (ValueError,KeyError):return False
        return (not is_reference_event(event) and event['dataset']==definition['dataset']
            and event['profile_id']==record['connection_id']
            and (not definition.get('profile_ids') or event['profile_id'] in definition['profile_ids'])
            and (not definition.get('event_types') or event['type'] in definition['event_types']))

    async def refresh(self, definitions):
        if hasattr(self.index, 'refresh') and any(d['status']=='active' and d.get('input_kind') not in {'tracker_events','none'} for d in definitions):
            await asyncio.to_thread(self.index.refresh)

    def sources(self, dataset):
        return {source_key(c):c['id'] for c in self.index.clips if c['dataset']==dataset}

    def ready(self, definition, wake, agent, input_optional):
        if definition['input_kind']=='none':return True
        if not self.runnable(definition):return False
        if definition['input_kind']=='tracker_events':
            return bool(self.monitoring is not None and (input_optional or self.monitoring.readiness(definition)))
        return definition['mode'] not in {'scheduled','ongoing'} or self.live_input_ready(definition['dataset'])

    async def prepare(self, definition, wake, agent):
        if definition['input_kind']=='none':return InputBatch()
        from investigation_plans import InvestigationPlans
        from source_visibility import ReadyIndex
        snapshot=(await asyncio.to_thread(self.index.snapshot,definition.get('owner_principal_id'))
            if isinstance(self.index,ReadyIndex) else InvestigationPlans._snapshot(self.index))
        event_input=definition['input_kind']=='tracker_events'
        if event_input:
            with self.store.connect() as db:
                private=self.store._read(db,'agents',definition['id'])
            excluded=set(private['watermark']) if definition['mode'] in {'ongoing','scheduled'} and not definition.get('once') else set()
            monitoring=self.monitoring
            host=getattr(agent,'business_scope_host',None)
            if host is not None:
                from monitoring import resolve_monitoring_source
                monitoring=copy.copy(monitoring)
                monitoring.source_lookup=lambda owner,key:resolve_monitoring_source(host,owner,key,chat_id=definition['chat_id'])
            records=monitoring.list_events(definition['owner_principal_id'],definition['dataset'],
                profile_ids=definition.get('profile_ids') or None,event_types=definition.get('event_types') or None,
                limit=64,exclude_ids=excluded,oldest_first=True)
            sources={e['id']:e['source']['clip_id'] for e in records}
        else:
            records=[c for c in snapshot.clips if c['dataset']==definition['dataset']]
            sources={source_key(c):c['id'] for c in records}
        screening=None
        if not event_input and definition['mode']=='ongoing' and not (wake and wake['kind'] == 'channel_reply'):
            try:
                prepared=await asyncio.to_thread(self.prepare_screening,definition['id'],sources,source_records=records,force=bool(wake))
                if prepared:
                    record,new_sources=prepared
                    screening=await asyncio.to_thread(self.screener.screen,record,new_sources,snapshot=snapshot)
            except (ValueError,OSError) as exc:
                self.screening_failed(definition['id'],definition['revision'],exc)
                return None
        return InputBatch(sources, records, screening, snapshot)

    def execution(self, definition):
        if definition.get('input_kind')=='none':return super().execution(definition)
        if definition.get('input_kind')=='tracker_events':
            ready=bool(self.monitoring and self.monitoring.readiness(definition))
            return {'configurable':True,'ready':ready,'required_input':'committed_tracker_events',
                'notice':None if ready else 'Save source-backed observations to start this agent.'}
        recurring=definition.get('mode') in {'scheduled','ongoing'}
        incoming=not recurring or self.live_input_ready(definition.get('dataset'))
        indexed=bool(self.index and self.index.status().get('ready') and any(c.get('dataset')==definition.get('dataset') for c in self.index.clips))
        return {'configurable':True,'ready':incoming and indexed,
            'required_input':'live_camera_feed' if recurring else 'indexed_recordings',
            'notice':('Configure your agent here. Connect a live camera feed to start scheduled runs.' if not incoming else
                'Prepare the selected footage index before starting this agent.' if not indexed else None)}

    def recurring_ready(self, collection, principal=None):
        if self.live_input_ready(collection):return True
        if not principal or self.monitoring is None:return False
        try:
            places=[collection] if collection else sorted({*(self.index.status().get('datasets',()) if self.index else ()),*self.monitoring.registered_datasets(principal)})
            return any(self.monitoring.list_events(principal,place,limit=1) for place in places)
        except ValueError:return False

    @contextmanager
    def context(self, agent, run, snapshot=None):
        if run['definition']['input_kind']=='none':
            with super().context(agent,run,snapshot) as message:yield message
            return
        from execution_policy import FRESH_INSPECTION_ADMISSION, RETRIEVAL_VIEW
        token = FRESH_INSPECTION_ADMISSION.set(lambda amount:self.store.admit_resource(run["id"], amount))
        view_token = None
        try:
            spec = run["definition"]
            from source_visibility import ReadyIndex
            source_index=(snapshot if snapshot is not None else
                self.index.snapshot(run.get('owner_principal_id')) if isinstance(self.index,ReadyIndex) else self.index)
            selected_view=source_index
            if spec.get('input_kind')!='tracker_events' and (run.get('screening') or run["trigger"] == "ongoing" or run.get('evidence_scope',{}).get('utc_window_applied')):
                ids = set(run["source_ids"])
                positions = [i for i,c in enumerate(source_index.clips) if c["id"] in ids]
                view = copy.copy(source_index)
                view.clips = [source_index.clips[i] for i in positions]
                view.embeddings = source_index.embeddings[positions]
                # Share the matching text tower and its lock across all company agents.
                view._encode_text = source_index._encode_text
                selected_view=view
            view_token = RETRIEVAL_VIEW.set(selected_view)
            message = spec["instruction"] + "\n\nThis is an explicitly activated background investigation. "
            message += f"Selected business: {spec['dataset']}. Produce an in-app report with evidence links. "
            message += f"Fresh Cosmos inspection batches allowed for this entire run, including children: {spec['fresh_inspections_per_run']}. "
            message += "Reuse saved observations when suitable and batch related inspections. "
            scope=run.get('evidence_scope',{})
            if scope.get('lookback_seconds'):
                message += f"Requested evidence lookback: {scope['lookback_seconds']} seconds. "
                if scope.get('utc_window_applied'):
                    message += f"Imported capture timestamps filter source windows overlapping {scope['requested_window_start_utc']} to {scope['requested_window_end_utc']}. "
                if scope.get('capture_timestamps_missing'):
                    message += f"{scope['capture_timestamps_missing']} available source windows have source-relative timing only; treat those as archive evidence with unknown calendar time. "
            if run.get('screening'):
                message += f"CPU screening selected {run['screening']['candidate_window_count']} candidate windows, with {len(run['screening']['context_clip_ids'])} immediate neighboring windows for context. "
                message += "Similarity selected candidates only. Investigate and verify the observable criteria before concluding that the objective occurred. "
                message += 'Saved observable plan: '+json.dumps(spec['watch_plan'],ensure_ascii=False)+'. '
                message += 'Candidate provenance: '+json.dumps(run['screening'],ensure_ascii=False)+'. '
            if run["trigger"] == "ongoing":
                message += f"Search, grouping and interval listing are restricted to {run['source_count']} screened candidate/context intervals. "
                message += "Older footage can supply context through neighboring clips. This is indexed archive work; capture-to-index latency is separate. "
            if spec.get('input_kind')=='tracker_events':
                message = spec['instruction'] + '\n\nSummarize these committed observations as reference data; preserve evidence URLs, uncertainty, visit scope and original source timestamps. Their commit time is not their capture time. Use existing annotations first; fresh video inference has no allowance in this run.\n' + json.dumps(run.get('observations',[]),ensure_ascii=False)
            if spec.get('input_kind')=='tracker_events':
                from monitoring_tools import admit_event_history
                admit_event_history(agent,run['chat_id'],run.get('observations',[]))
            yield message
        finally:
            FRESH_INSPECTION_ADMISSION.reset(token)
            if view_token is not None:RETRIEVAL_VIEW.reset(view_token)
