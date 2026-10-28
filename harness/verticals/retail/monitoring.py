"""Versioned observation profiles and immutable, source-backed visit events.

A store belongs to one host-selected company state directory. Every operation also
requires the host-selected owner; model arguments never supply authority.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

def is_reference_event(value):
    return (value.get('origin')=='dataset_reference' or value.get('observation_origin')=='dataset_reference'
        or value.get('review_state')=='publisher_annotation')


class VocabularyEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: str = Field(pattern=r'^[a-z][a-z0-9_]{0,79}$')
    description: str = Field(min_length=1,max_length=600)
    group: Literal['shopper_journey','service_maintenance','other'] = 'shopper_journey'

class ProfileDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=120)
    dataset: str = Field(min_length=1,max_length=100)
    criteria: str = Field(default='',max_length=8000)
    vocabulary: list[VocabularyEntry] = Field(min_length=1,max_length=150)
    camera_ids: list[str] = Field(default_factory=list,max_length=100)
    location_id: str | None = Field(default=None,max_length=100)
    room_id: str | None = Field(default=None,max_length=100)
    @model_validator(mode='after')
    def unique(self):
        if len({v.type for v in self.vocabulary}) != len(self.vocabulary):
            raise ValueError('Use a distinct name for each observation type.')
        if not self.name.strip() or len(set(self.camera_ids))!=len(self.camera_ids):
            raise ValueError('Use a name and distinct camera references.')
        return self

class SourceReference(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)
    clip_id: str = Field(min_length=1,max_length=160)
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)

class EventDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)
    profile_id: str
    profile_revision: int = Field(ge=1)
    event_key: str = Field(min_length=1,max_length=160)
    type: str = Field(pattern=r'^[a-z][a-z0-9_]{0,79}$')
    description: str = Field(min_length=1,max_length=4000)
    source: SourceReference
    track_id: str | None = Field(default=None,max_length=100)
    object_ref: str | None = Field(default=None,max_length=100)
    object_description: str | None = Field(default=None,max_length=2000)
    bbox: list[float] | None = Field(default=None,min_length=4,max_length=4)
    @model_validator(mode='after')
    def valid_geometry(self):
        if self.source.end_s < self.source.start_s:
            raise ValueError('Use the observed interval in source order.')
        if self.bbox is not None:
            x1,y1,x2,y2=self.bbox
            if not all(math.isfinite(v) and 0<=v<=1 for v in self.bbox) or x1>=x2 or y1>=y2:
                raise ValueError('Use a normalized xyxy box inside the source frame.')
        return self

class RecordingDefinition(BaseModel):
    """Trusted host import receipt. This schema has no browser or model write route."""
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    id:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$')
    video_id:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$')
    dataset:str=Field(min_length=1,max_length=100)
    start:float=Field(default=0,ge=0)
    end:float=Field(gt=0)
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    media_url:str=Field(pattern=r'^/[A-Za-z0-9?][^\x00-\x20\\]*$')
    thumbnail_url:str|None=Field(default=None,pattern=r'^/[A-Za-z0-9?][^\x00-\x20\\]*$')
    evidence_url:str|None=Field(default=None,pattern=r'^/[A-Za-z0-9?][^\x00-\x20\\]*$')
    camera_id:str|None=Field(default=None,max_length=100)
    capture_start_utc:str|None=None
    capture_end_utc:str|None=None
    source_capture_start_utc:str|None=None
    @model_validator(mode='after')
    def bounds(self):
        if self.start>=self.end:raise ValueError('Use the verified recording interval.')
        return self

class MonitoringStore:
    def __init__(self,state,*,source_lookup=None):
        self.state=Path(state);self.state.mkdir(parents=True,exist_ok=True)
        self.path=self.state/'monitoring.sqlite'
        self.source_lookup=source_lookup or self.registered_source
        self.event_sink=None
        self.profile_authorized=lambda owner,record:True
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS monitoring_sources(id TEXT NOT NULL,owner TEXT NOT NULL,dataset TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(id,owner));
            CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, owner TEXT NOT NULL, dataset TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profile_revisions(id TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(id,revision));
            CREATE TABLE IF NOT EXISTS monitoring_events(id TEXT PRIMARY KEY,owner TEXT NOT NULL,dataset TEXT NOT NULL,profile_id TEXT NOT NULL,source_id TEXT NOT NULL,event_key TEXT NOT NULL,created_at REAL NOT NULL,fingerprint TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(owner,profile_id,source_id,event_key));
            ''')
        self.path.chmod(0o600)
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row;return db
    @staticmethod
    def owner(owner):
        if not isinstance(owner,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,149}',owner):
            raise ValueError('Use the authenticated workspace owner.')
        return owner
    def register_source(self,owner,descriptor):
        """Called by the host only after it verifies the imported bytes and routes."""
        owner=self.owner(owner);value=RecordingDefinition.model_validate(descriptor).model_dump(exclude_none=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT body FROM monitoring_sources WHERE id=? AND owner=?',(value['id'],owner)).fetchone()
            if row:
                prior=json.loads(row['body'])
                if prior!=value:raise ValueError('Keep the registered recording’s original bytes, timing and identity.')
                return prior
            db.execute('INSERT INTO monitoring_sources VALUES(?,?,?,?)',(value['id'],owner,value['dataset'],json.dumps(value)))
        return value
    def registered_source(self,owner,key):
        owner=self.owner(owner)
        with self.connect() as db:row=db.execute('SELECT body FROM monitoring_sources WHERE id=? AND owner=?',(key,owner)).fetchone()
        return json.loads(row['body']) if row else None
    def registered_sources(self,owner,dataset=None):
        owner=self.owner(owner)
        with self.connect() as db:
            return [json.loads(row['body']) for row in db.execute('SELECT body FROM monitoring_sources WHERE owner=?',(owner,)) if dataset is None or json.loads(row['body'])['dataset']==dataset]
    def registered_datasets(self,owner):
        owner=self.owner(owner)
        with self.connect() as db:return [row[0] for row in db.execute('SELECT DISTINCT dataset FROM monitoring_sources WHERE owner=?',(owner,))]
    def save_profile(self,owner,definition,*,datasets,profile_id=None,expected_revision=None):
        owner=self.owner(owner);spec=ProfileDefinition.model_validate(definition).model_dump()
        if spec['dataset'] not in datasets:raise ValueError('Choose an available business collection.')
        if not self.profile_authorized(owner,spec):raise ValueError('Choose a profile within your current location access.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if profile_id:
                prior=db.execute('SELECT * FROM profiles WHERE id=? AND owner=?',(profile_id,owner)).fetchone()
                if not prior:raise ValueError('Choose a profile from your workspace.')
                if type(expected_revision) is not int or expected_revision!=prior['revision']:
                    raise ValueError('Refresh the profile revision before editing.')
                if prior['dataset']!=spec['dataset']:raise ValueError('Keep the profile in its original business collection.')
                revision=prior['revision']+1
            else:profile_id='profile-'+uuid.uuid4().hex;revision=1
            record={**spec,'id':profile_id,'revision':revision,'owner_principal_id':owner,'updated_at':time.time(),
                'camera_onboarding':{'preferred_protocol':'rtsp','credential_entry':'secure_request','profile_id':profile_id}}
            encoded=json.dumps(record,allow_nan=False)
            db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?)',(profile_id,owner,spec['dataset'],revision,encoded))
            db.execute('INSERT INTO profile_revisions VALUES(?,?,?)',(profile_id,revision,encoded))
        return record
    def get_profile(self,owner,key,*,revision=None):
        owner=self.owner(owner)
        with self.connect() as db:
            current=db.execute('SELECT * FROM profiles WHERE id=? AND owner=?',(key,owner)).fetchone()
            if not current:raise ValueError('Choose a profile from your workspace.')
            row=current if revision is None else db.execute('SELECT body FROM profile_revisions WHERE id=? AND revision=?',(key,revision)).fetchone()
            if not row:raise ValueError('Choose an existing profile revision.')
            record=json.loads(row['body'])
            if not self.profile_authorized(owner,record):raise ValueError('Choose a profile within your current location access.')
            return record
    def list_profiles(self,owner,dataset=None):
        owner=self.owner(owner)
        with self.connect() as db:
            return [json.loads(r['body']) for r in db.execute('SELECT body FROM profiles WHERE owner=? ORDER BY rowid DESC',(owner,))
                if (dataset is None or json.loads(r['body'])['dataset']==dataset) and self.profile_authorized(owner,json.loads(r['body']))]
    def source(self,owner,key,dataset):
        source=self.source_lookup(owner,key)
        if not source or source.get('dataset')!=dataset:raise ValueError('Choose an accessible source in this business collection.')
        return source
    def commit_event(self,owner,definition,*,origin='model_observation'):
        owner=self.owner(owner);spec=EventDefinition.model_validate(definition).model_dump()
        profile=self.get_profile(owner,spec['profile_id'],revision=spec['profile_revision'])
        if spec['type'] not in {entry['type'] for entry in profile['vocabulary']}:raise ValueError('Use a type from this profile revision.')
        source=self.source(owner,spec['source']['clip_id'],profile['dataset'])
        start,end=spec['source']['start_s'],spec['source']['end_s']
        if start<float(source.get('start',0)) or end>float(source.get('end',source.get('duration_s',0))):
            raise ValueError('Use timestamps inside the referenced source interval.')
        if profile['camera_ids'] and source.get('camera_id',source.get('camera')) not in profile['camera_ids']:
            raise ValueError('Use a source from this profile’s selected cameras.')
        if origin not in {'model_observation','pipeline_observation','human_review','dataset_reference'}:raise ValueError('Use a registered observation origin.')
        recording=source.get('video_id') or source['id']
        scoped=lambda value,prefix:prefix+'-'+hashlib.sha256(json.dumps([owner,profile['dataset'],recording,value]).encode()).hexdigest()[:28] if value else None
        fingerprint=hashlib.sha256(json.dumps([spec,origin],sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT body,fingerprint FROM monitoring_events WHERE owner=? AND profile_id=? AND source_id=? AND event_key=?',
                (owner,profile['id'],source['id'],spec['event_key'])).fetchone()
            if prior:
                if prior['fingerprint']!=fingerprint:raise ValueError('Preserve the original observation for this event identity.')
                saved=json.loads(prior['body'])
                if self.event_sink is not None and not is_reference_event(saved):self.event_sink(saved)
                return saved
            record={**spec,'id':'event-'+uuid.uuid4().hex,'owner_principal_id':owner,'dataset':profile['dataset'],
                'visit_id':scoped(spec['track_id'],'visit'),'item_id':scoped(spec['object_ref'],'item'),
                'recording_id':recording,'camera_id':source.get('camera_id',source.get('camera')),
                'captured_at':None,'committed_at':time.time(),'origin':origin,'review_state':'reviewed' if origin=='human_review' else 'publisher_annotation' if origin=='dataset_reference' else 'observation',
                'evidence_url':source.get('evidence_url') or source.get('media_url') or '/?clip='+source['id'],
                'thumbnail_url':source.get('thumbnail_url') or ('/thumbnails/'+source['id']+'.jpg' if source.get('thumbnail_path') else None),
                'media_url':source.get('media_url'), 'source_sha256':source.get('sha256')}
            # Calendar capture timestamps are copied only from explicit source provenance.
            if source.get('capture_start_utc') or source.get('source_capture_start_utc'):
                from retail_agent_inputs import _capture_interval
                interval=_capture_interval(source)
                if interval:
                    from datetime import datetime,timezone
                    record['captured_at']=datetime.fromtimestamp(interval[0]+start-float(source.get('start',0)),timezone.utc).isoformat()
            db.execute('INSERT INTO monitoring_events VALUES(?,?,?,?,?,?,?,?,?)',(record['id'],owner,profile['dataset'],profile['id'],source['id'],spec['event_key'],record['committed_at'],fingerprint,json.dumps(record)))
        if self.event_sink is not None and not is_reference_event(record):self.event_sink(record)
        return record
    def list_events(self,owner,dataset,*,profile_ids=None,event_types=None,limit=200,after=0,exclude_ids=None,oldest_first=False,include_reference=False):
        owner=self.owner(owner)
        if type(include_reference)is not bool or not 1<=limit<=500 or not math.isfinite(after) or after<0:raise ValueError('Use bounded event pagination.')
        with self.connect() as db:
            order='ASC' if oldest_first else 'DESC'
            rows=db.execute(f'SELECT body FROM monitoring_events WHERE owner=? AND dataset=? AND created_at>=? ORDER BY created_at {order},rowid {order}',(owner,dataset,after))
            result=[]
            for row in rows:
                value=json.loads(row['body'])
                if not include_reference and is_reference_event(value):continue
                if exclude_ids and value['id'] in exclude_ids:continue
                if profile_ids is not None and value['profile_id'] not in profile_ids:continue
                if event_types is not None and value['type'] not in event_types:continue
                try:self.source(owner,value['source']['clip_id'],dataset)
                except ValueError:continue
                result.append(value)
                if len(result)>=limit:break
        return result
    def event_sources(self,owner):
        """Each of the owner's events as (source, dataset, origin, review state), in one read."""
        owner=self.owner(owner)
        with self.connect() as db:rows=db.execute('SELECT id,body FROM monitoring_events WHERE owner=?',(owner,)).fetchall()
        found={}
        for row in rows:
            value=json.loads(row['body'])
            found[row['id']]=(value['source']['clip_id'],value['dataset'],value.get('origin'),value.get('review_state'))
        return found
    def get_event(self,owner,key):
        owner=self.owner(owner)
        with self.connect() as db:row=db.execute('SELECT body FROM monitoring_events WHERE id=? AND owner=?',(key,owner)).fetchone()
        if not row:raise ValueError('Choose an observation from your workspace.')
        value=json.loads(row['body']);self.source(owner,value['source']['clip_id'],value['dataset']);return value
    def validate_definition(self,definition):
        owner=definition.get('owner_principal_id');dataset=definition['dataset']
        for key in definition.get('profile_ids',[]):
            if self.get_profile(owner,key)['dataset']!=dataset:raise ValueError('Use observation profiles in this agent’s business.')
    def events_for(self,definition):
        self.validate_definition(definition)
        return self.list_events(definition.get('owner_principal_id'),definition['dataset'],profile_ids=definition.get('profile_ids') or None,
            event_types=definition.get('event_types') or None,limit=500)
    def readiness(self,definition):
        try:events=self.events_for(definition)
        except ValueError:return False
        return bool(events)


def resolve_monitoring_source(selected,owner,key,chat_id=None):
    """Resolve current source and optional conversation grants before returning evidence."""
    index=selected['index'];source=None
    try:
        view=index.snapshot(owner) if hasattr(index,'snapshot') else index
        source=view.get_clip(key)
    except (ValueError,OSError):pass
    if source is None:source=selected['monitoring'].registered_source(owner,key)
    if source is None:return None
    scope=selected.get('business_scope')
    if scope is not None:
        from business_scope import ScopeActor
        try:
            actor=ScopeActor(scope.company_id,owner)
            effective=scope.chat_scope(actor,chat_id,source['dataset']) if chat_id else scope.effective(actor,source['dataset'])
            if effective['configured'] and (not effective['can_query'] or not (
                    effective['includes_unassigned'] or source['video_id'] in effective['source_ids'])):return None
        except (ValueError,OSError,sqlite3.Error):return None
    return source


def available_monitoring_datasets(selected,owner):
    return sorted(set(selected['index'].status().get('datasets',[])) | set(selected['monitoring'].registered_datasets(owner)))


def monitoring_profile_authorized(selected,owner,record):
    store=selected.get('business_scope')
    if store is None:return True
    from business_scope import ScopeActor
    try:
        effective=store.effective(ScopeActor(store.company_id,owner),record['dataset'])
        if not effective['configured']:return True
        if not effective['can_query']:return False
        return (effective['includes_unassigned'] or (
            (not record.get('location_id') or record['location_id'] in effective['location_ids']) and
            (not record.get('room_id') or record['room_id'] in effective['room_ids']) and
            set(record.get('camera_ids',[]))<=set(effective['camera_ids'])))
    except (ValueError,OSError,sqlite3.Error):return False
