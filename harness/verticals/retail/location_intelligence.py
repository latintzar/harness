"""Durable location/catalog projection of authorized committed observations.

BusinessScope owns geography and grants. This store owns private catalog records,
visit projections and daily fact receipts. Source and receipt clocks stay separate.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from business_scope import ScopeActor, identifier
from stream_ingestion import PRODUCT_EVENTS


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class ImageReference(Strict):
    url: str = Field(pattern=r'^/[A-Za-z0-9?][^\x00-\x20\\]*$')
    kind: Literal['catalog', 'generated_demo', 'generated_reference', 'evidence_crop', 'shelf_photo_crop']
    provenance: dict
    @model_validator(mode='after')
    def evidence(self):
        if self.kind == 'evidence_crop' and not all(self.provenance.get(k) is not None for k in ('source_id','source_time_s')):
            raise ValueError('Evidence crops retain their original source and timestamp.')
        if self.kind == 'shelf_photo_crop' and not all(self.provenance.get(k) is not None for k in ('photo_id','source_sha256','bbox')):
            raise ValueError('Shelf crops retain their upload, source hash and rectangle.')
        if not self.provenance:
            raise ValueError('Record the image source.')
        return self


class ProductPrice(Strict):
    amount: Decimal = Field(ge=0,max_digits=14,decimal_places=4)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    basis: Literal['pos_confirmed','catalog_sourced','estimated']
    source: str = Field(min_length=1,max_length=2000)
    observed_at: datetime
    confidence: float | None = Field(default=None,ge=0,le=1)
    @model_validator(mode='after')
    def provenance(self):
        if self.observed_at.tzinfo is None or not self.source.strip():
            raise ValueError('Keep the price source and its timezone-aware observation date.')
        return self


def displayed_price(value):
    if value is None:return None
    price=ProductPrice.model_validate(value).model_dump(mode='json')
    return {**price,'display_basis':{'pos_confirmed':'POS price','catalog_sourced':'Catalog price','estimated':'Estimated price'}[price['basis']]}


PAYMENT_ACTIONS=frozenset({'paid_with_card','handed_cash','card_presented','presented_card','cash_handed_over','payment_observed','payment_action_observed'})


def payment_evidence(events):
    return [{'event_id':e['id'],'type':e['type'],'description':e['description'],
        'basis':'observed_payment_action','source_id':e['source']['clip_id'],
        'source_time_s':e['source']['start_s'],'evidence_url':e.get('evidence_url'),
        'amount':None,'currency':None,'amount_basis':None,'transaction_id':None}
        for e in events if e['type'] in PAYMENT_ACTIONS]


class ProductDefinition(Strict):
    id: str = Field(min_length=1, max_length=160)
    collection: str
    location_id: str
    label: str = Field(min_length=1, max_length=500)
    status: Literal['provisional', 'confirmed'] = 'provisional'
    sku: str | None = Field(default=None, max_length=160)
    image: ImageReference | None = None
    price: ProductPrice | None = None
    attributes: dict = Field(default_factory=dict)
    provenance: dict
    @model_validator(mode='after')
    def identity(self):
        identifier(self.id); identifier(self.collection); identifier(self.location_id)
        if not self.provenance:raise ValueError('Record the catalog source.')
        if self.status == 'confirmed' and not self.sku:
            raise ValueError('Confirmed catalog identities require their source SKU.')
        return self


class LocationCoordinates(Strict):
    latitude: float = Field(ge=-90, le=90, strict=True)
    longitude: float = Field(ge=-180, le=180, strict=True)
    timezone: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=2000)
    provenance: dict
    @model_validator(mode='after')
    def sourced(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Use the location’s IANA timezone.') from None
        if not self.source.strip() or not self.provenance or len(encoded(self.provenance)) > 8192:
            raise ValueError('Keep the coordinate source and bounded provenance.')
        return self


def coordinates_from_weather(value):
    """Import an existing sourced configuration; perform no geocoding or lookup."""
    if value is None:return None
    if not isinstance(value, dict):raise ValueError('Use a sourced location configuration.')
    provenance=value.get('provenance')
    if not provenance:raise ValueError('Keep the existing coordinate provenance.')
    details=dict(provenance) if isinstance(provenance,dict) else {'note':str(provenance)}
    details.update(coordinate_precision=value.get('coordinate_precision'),
        configuration_sha256=hashlib.sha256(encoded(value).encode()).hexdigest())
    return LocationCoordinates.model_validate({'latitude':value.get('latitude'),
        'longitude':value.get('longitude'),'timezone':value.get('timezone'),
        'source':value.get('coordinate_source'),'provenance':details}).model_dump(mode='json')


class LocationDefinition(Strict):
    collection: str
    location_id: str
    coordinates: LocationCoordinates | None = None
    cover_image: ImageReference | None = None
    widgets: list[Literal['visitors','sales','average_basket','conversion','queue_time','product_interest']] = Field(default_factory=lambda:['visitors','sales','average_basket','conversion','queue_time','product_interest'])


class ProductMatch(Strict):
    product_id: str | None = None
    status: Literal['provisional','confirmed','candidate','unresolved']
    score: float | None = Field(default=None, ge=0, le=1)
    evidence: dict
    inspection_id: str
    basis: str | None = Field(default=None,max_length=200)
    created_at: float | None = Field(default=None,ge=0)
    @model_validator(mode='after')
    def proof(self):
        if not self.evidence or not self.inspection_id:raise ValueError('Keep the inspection evidence and identity.')
        if self.status=='confirmed' and not self.product_id:raise ValueError('Use a catalog identity for a confirmed match.')
        return self


class DailyFact(Strict):
    id: str
    collection: str
    location_id: str
    day: date
    provenance: dict
    basis: Literal['pos', 'observations', 'synthetic_demo']
    visitors: int | None = Field(default=None, ge=0)
    transactions: int | None = Field(default=None, ge=0)
    purchasing_visits: int | None = Field(default=None, ge=0)
    revenue_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    queue_wait_total_s: float | None = Field(default=None, ge=0)
    queue_sample_count: int | None = Field(default=None, ge=0)
    @model_validator(mode='after')
    def valid(self):
        identifier(self.id)
        if not self.provenance:raise ValueError('Keep the daily fact source receipt.')
        if self.basis=='observations' and any(getattr(self,k) is not None for k in ('transactions','purchasing_visits','revenue_minor')):
            raise ValueError('Use authoritative POS facts for transactions, purchase conversion and sales amounts.')
        if self.revenue_minor is not None and self.currency is None:raise ValueError('Sales facts require their currency.')
        if self.purchasing_visits is not None and self.visitors is not None and self.purchasing_visits>self.visitors:
            raise ValueError('Match purchases to actual visits before measuring conversion.')
        return self


class LocationIntelligence:
    def __init__(self,state,scope,monitoring=None,*,clock=time.time):
        self.path=Path(state)/'location-intelligence.sqlite'
        self.scope,self.monitoring,self.clock=scope,monitoring,clock

    @contextmanager
    def connect(self,write=False):
        if not write and not self.path.exists():
            yield None;return
        if write:self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        try:
            if write:
                self.path.chmod(0o600)
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS records(owner TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,collection TEXT NOT NULL,location TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(owner,kind,id))')
                db.execute('CREATE INDEX IF NOT EXISTS location_rows ON records(owner,collection,location,kind)')
            yield db
            if write:db.commit()
        finally:db.close()

    def _rows(self,owner,kind=None):
        with self.connect() as db:
            if db is None:return []
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM records WHERE owner=?'+(' AND kind=?' if kind else ''),(owner,kind) if kind else (owner,))]

    def _get(self,owner,kind,key):
        with self.connect() as db:
            row=db.execute('SELECT body FROM records WHERE owner=? AND kind=? AND id=?',(owner,kind,key)).fetchone() if db else None
        if row is None:raise ValueError('Choose a record in this workspace.')
        return json.loads(row[0])

    def _put(self,owner,kind,value,*,immutable=False):
        with self.connect(True) as db:
            row=db.execute('SELECT body FROM records WHERE owner=? AND kind=? AND id=?',(owner,kind,value['id'])).fetchone()
            if row and immutable:
                prior=json.loads(row[0])
                if prior!=value:raise ValueError('Preserve the original source receipt on retry.')
                return prior
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)',(owner,kind,value['id'],value['collection'],value['location_id'],encoded(value)))
        return value

    def authorized(self,owner,collection,location_id,*,write=False):
        actor=ScopeActor(self.scope.company_id,identifier(owner))
        self.scope.authority(actor,write=write)
        view=self.scope.describe(actor,collection)
        if not view['effective']['can_query'] or location_id not in view['effective']['location_ids']:
            raise ValueError('Choose a location in your current access.')
        if write and not view['can_manage']:
            raise ValueError('Use location management access to update its catalog.')
        return view

    def configure_location(self,owner,definition):
        draft=LocationDefinition.model_validate(definition)
        self.authorized(owner,draft.collection,draft.location_id,write=True)
        try:prior=self._get(owner,'location',draft.location_id)
        except ValueError:prior={}
        if prior and prior['collection']!=draft.collection:
            raise ValueError('Keep the location configuration in its original collection.')
        prior.pop('id',None)
        value=LocationDefinition.model_validate({**prior,**draft.model_dump(mode='json',exclude_unset=True)}).model_dump(mode='json')
        return self._put(owner,'location',{'id':value['location_id'],**value})

    def save_product(self,owner,definition):
        value=ProductDefinition.model_validate(definition).model_dump(mode='json')
        self.authorized(owner,value['collection'],value['location_id'],write=True)
        try:prior=self._get(owner,'product',value['id'])
        except ValueError:prior=None
        if prior and (prior['collection'],prior['location_id'])!=(value['collection'],value['location_id']):
            raise ValueError('Keep the catalog identity in its original location.')
        return self._put(owner,'product',value)

    def _event_reader(self,owner):
        """One read names every event's source; each source's access is decided once per caller."""
        try:known=self.monitoring.event_sources(owner)
        except (ValueError,AttributeError):known={}
        decided={}
        def readable(source_id,dataset):
            key=(source_id,dataset)
            if key not in decided:
                try:self.monitoring.source(owner,source_id,dataset);decided[key]=True
                except (ValueError,AttributeError):decided[key]=False
            return decided[key]
        return known,readable

    def products(self,owner,collection,location_id,reader=None):
        self.authorized(owner,collection,location_id)
        known,readable=reader or self._event_reader(owner)
        values=[]
        for product in self._rows(owner,'product'):
            if product['collection']!=collection or product['location_id']!=location_id:continue
            event_id=product.get('provenance',{}).get('event_id')
            if event_id:
                event=known.get(event_id)
                if event is None or not readable(event[0],event[1]):continue
                if event[2]=='dataset_reference' or event[3]=='publisher_annotation':continue
            values.append(product)
        return values

    def _event_place(self,owner,event):
        if self.monitoring is None:raise ValueError('Use the committed observation store.')
        saved=self.monitoring.get_event(owner,event['id'])
        if saved!=event:raise ValueError('Project the original committed observation.')
        source=self.monitoring.source(owner,event['source']['clip_id'],event['dataset'])
        return self._source_place(owner,source,event['dataset'])

    def _source_place(self,owner,source,collection):
        actor=ScopeActor(self.scope.company_id,owner)
        view=self.scope.describe(actor,collection)
        recording=source.get('video_id') or source['id']
        # Exact recording mapping supplies geography; filename/visual similarity does not.
        camera=None
        if recording in view['effective']['source_ids']:
            with self.scope._db() as db:
                _,graph=self.scope._graph(db,collection)
            for candidate in graph['cameras']:
                prior=candidate.get('source_views',{}).get(recording)
                if prior:
                    camera={**candidate,**prior};break
        if camera is None:raise ValueError('Map the original recording to its authorized camera first.')
        self.authorized(owner,collection,camera['location_id'])
        return camera,source

    def ingest_tracks(self,owner,source_id,tracks,*,tracks_sha256):
        """Trusted host adapter for real tracker observations, independent of VLM events."""
        source=self.monitoring.source_lookup(owner,source_id)
        if not source:raise ValueError('Register the original tracking source first.')
        camera,source=self._source_place(owner,source,source['dataset'])
        self.authorized(owner,source['dataset'],camera['location_id'],write=True)
        if not re.fullmatch(r'[a-f0-9]{64}',tracks_sha256) or tracks.get('identity_scope')!='recording_and_camera_local':
            raise ValueError('Keep the source tracking hash and recording-local identity scope.')
        fps=tracks.get('fps');frames=tracks.get('frames')
        if type(fps) not in (int,float) or not math.isfinite(fps) or not 0<fps<=240 or not isinstance(frames,list) or len(frames)>100000:
            raise ValueError('Use bounded timestamped tracker frames.')
        times={};previous=-1
        for frame in frames:
            t=frame.get('video_time_s');rows=frame.get('tracks')
            if type(t) not in (int,float) or not math.isfinite(t) or t<previous or not float(source.get('start',0))<=t<=float(source['end']):
                raise ValueError('Use ordered original source times for tracking.')
            previous=t
            if not isinstance(rows,list) or len(rows)>500:raise ValueError('Keep each tracker frame bounded.')
            seen=set()
            for row in rows:
                raw=row.get('track_id')
                if isinstance(raw,bool) or not isinstance(raw,(str,int)) or not str(raw) or len(str(raw))>100:
                    raise ValueError('Use the saved tracker person identity.')
                person=str(raw)
                if person in seen:raise ValueError('Keep one observation per tracked person and frame.')
                seen.add(person);times.setdefault(person,[]).append(float(t))
        if len(times)>5000:raise ValueError('Use a bounded recording track set.')
        result=[];recording=source.get('video_id') or source['id']
        for person,points in times.items():
            visit='visit-'+hashlib.sha256(json.dumps([owner,source['dataset'],recording,person]).encode()).hexdigest()[:28]
            intervals=[]
            for t in sorted(set(points)):
                if not intervals or t-intervals[-1]['end_s']>1.5/fps:intervals.append({'start_s':t,'end_s':t})
                else:intervals[-1]['end_s']=t
            value={'id':visit,'collection':source['dataset'],'location_id':camera['location_id'],
                'camera_id':camera['id'],'room_ids':camera['room_ids'],'recording_id':recording,
                'source_id':source_id,'person_id':person,'first_s':points[0],'last_s':points[-1],
                'observed_intervals':intervals,'observation_count':len(points),'basis':'tracker_observations',
                'first_observation_semantics':'first_observed_in_recording',
                'provenance':{'tracks_sha256':tracks_sha256,'source_sha256':source.get('sha256'),
                    'tracker':tracks.get('tracker'),'identity_scope':tracks['identity_scope']}}
            result.append(self._put(owner,'tracked_visit',value,immutable=True))
        return result

    def ingest_event(self,owner,event,*,job_id=None):
        camera,source=self._event_place(owner,event)
        try:
            prior=self._get(owner,'event',event['id'])
            if prior.get('origin')!='dataset_reference' and prior.get('review_state')!='publisher_annotation':
                self._project_visit(owner,prior)
            return prior
        except ValueError:pass
        captured=event.get('captured_at')
        try:
            parsed=datetime.fromisoformat(captured.replace('Z','+00:00')) if captured else None
            captured=parsed.astimezone(timezone.utc).isoformat() if parsed and parsed.tzinfo and parsed.year>=2000 else None
        except (ValueError,AttributeError):captured=None
        value={**event,'collection':event['dataset'],'location_id':camera['location_id'],
            'camera_id':camera['id'],'room_ids':camera['room_ids'],'received_at':self.clock(),
            'source_time_s':event['source']['start_s'],'captured_at':captured,'projection_job_id':job_id,
            'product_id':None,'product_match':None}
        if event.get('origin')=='dataset_reference' or event.get('review_state')=='publisher_annotation':
            value['projection_basis']='dataset_reference'
            return self._put(owner,'event',value,immutable=True)
        if event['type'] in PRODUCT_EVENTS:
            key=event.get('item_id') or 'observed-'+hashlib.sha256(event['id'].encode()).hexdigest()[:24]
            try:self._get(owner,'product',key)
            except ValueError:
                self._put(owner,'product',ProductDefinition(id=key,collection=event['dataset'],location_id=camera['location_id'],
                    label=event.get('object_description') or event.get('object_ref') or 'Observed item',
                    provenance={'kind':'camera_observation','event_id':event['id'],'source_id':event['source']['clip_id'],'source_time_s':event['source']['start_s']}).model_dump(mode='json'))
            value['product_id']=key
        value=self._put(owner,'event',value,immutable=True)
        self._project_visit(owner,value)
        return value

    def _project_visit(self,owner,event):
        if not event.get('visit_id'):return
        key=event['visit_id']
        with self.connect(True) as db:
            row=db.execute("SELECT body FROM records WHERE owner=? AND kind='visit' AND id=?",(owner,key)).fetchone()
            visit=json.loads(row[0]) if row else {'id':key,'collection':event['dataset'],'location_id':event['location_id'],
                'recording_id':event['recording_id'],'camera_id':event['camera_id'],'person_id':event['track_id'],
                'first_s':event['source']['start_s'],'last_s':event['source']['end_s'],'event_ids':[]}
            if event['id'] not in visit['event_ids']:visit['event_ids'].append(event['id'])
            visit['first_s']=min(visit['first_s'],event['source']['start_s']);visit['last_s']=max(visit['last_s'],event['source']['end_s'])
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)',(owner,'visit',key,visit['collection'],visit['location_id'],encoded(visit)))

    def resolve_location(self,owner,event):
        camera,_=self._event_place(owner,event)
        return {'collection':event['dataset'],'location_id':camera['location_id'],'camera_id':camera['id'],'room_ids':camera['room_ids']}

    def catalog_products(self,owner,event):
        place=self.resolve_location(owner,event)
        return self.products(owner,place['collection'],place['location_id'])

    def apply_product_match(self,owner,event_id,match):
        match=ProductMatch.model_validate(match).model_dump(mode='json')
        event=self._get(owner,'event',event_id)
        self._event_place(owner,self.monitoring.get_event(owner,event_id))
        if match['product_id']:
            product=self._get(owner,'product',match['product_id'])
            if (product['collection'],product['location_id'])!=(event['collection'],event['location_id']):
                raise ValueError('Match products within the event’s location.')
            if match['status']=='confirmed' and product['status']!='confirmed':
                raise ValueError('Confirm the source catalog identity before linking the observation.')
            if match['status']=='confirmed':event['product_id']=product['id']
        event['product_match']=match
        return self._put(owner,'event',event)

    def product_match(self,owner,event_id):
        """Read a saved matching receipt; this endpoint starts no image work."""
        original=self.monitoring.get_event(owner,event_id)
        self._event_place(owner,original)
        try:event=self._get(owner,'event',event_id)
        except ValueError:
            return {'event_id':event_id,'dataset':original['dataset'],'status':'awaiting_projection','product':None,'match':None}
        product=self._get(owner,'product',event['product_id']) if event.get('product_id') else None
        if product:product={**product,'price':displayed_price(product.get('price'))}
        return {'event_id':event_id,'dataset':event['dataset'],'visit_id':event.get('visit_id'),
            'status':(event.get('product_match') or {}).get('status','awaiting_inspection'),
            'product':product,'match':event.get('product_match'),'source':event['source']}

    def save_daily_fact(self,owner,definition):
        fact=DailyFact.model_validate(definition).model_dump(mode='json')
        self.authorized(owner,fact['collection'],fact['location_id'],write=True)
        return self._put(owner,'daily_fact',fact,immutable=True)

    @staticmethod
    def _metrics(events,facts):
        product=[e for e in events if e['type'] in PRODUCT_EVENTS]
        waits=[];starts={}
        for e in sorted(events,key=lambda e:(e['recording_id'],e['source_time_s'])):
            key=e.get('visit_id')
            if not key:continue
            if e['type']=='entered_queue':starts.setdefault(key,e['source_time_s'])
            elif e['type'] in {'reached_checkout','left_queue','queue_exit'} and key in starts:
                waits.append(e['source_time_s']-starts.pop(key))
        def total(field):
            financial=field in {'transactions','purchasing_visits','revenue_minor'}
            values=[f[field] for f in facts if f.get(field) is not None and (not financial or f['basis'] in {'pos','synthetic_demo'})]
            return sum(values) if values else None
        visitors=total('visitors');transactions=total('transactions');revenue=total('revenue_minor');purchasing=total('purchasing_visits')
        currencies={f['currency'] for f in facts if f.get('currency')}
        if len(currencies)>1:revenue=None
        queue_n=total('queue_sample_count');queue_total=total('queue_wait_total_s')
        return {'observed_interactions':len(product),'observed_pickups':sum(e['type'] in {'pick_up','pickup','picked_up_item'} for e in product),
            'observed_putbacks':sum(e['type'] in {'put_back','put_back_item'} for e in product),
            'observed_tracks':len({e['visit_id'] for e in events if e.get('visit_id')}),
            'observed_visitors':visitors,'revenue':revenue/100 if revenue is not None else None,
            'average_transaction_value':revenue/100/transactions if revenue is not None and transactions else None,
            'conversion_rate':purchasing/visitors if purchasing is not None and visitors else None,
            'average_queue_time_s':queue_total/queue_n if queue_total is not None and queue_n else sum(waits)/len(waits) if waits else None}

    def _projection(self,events,products,facts):
        metrics=self._metrics(events,facts);projection=[]
        by_product={}
        for e in events:by_product.setdefault(e.get('product_id'),[]).append(e)
        for product in products:
            hits=by_product.get(product['id'])
            if not hits:continue
            rows=[]
            for e in sorted(hits,key=lambda e:(e['recording_id'],e['source_time_s'])):
                target={'clipId':e['source']['clip_id'],'personId':e.get('track_id'),'time_s':e['source_time_s']}
                rows.append({'id':e['id'],'type':e['type'],'label':e['type'].replace('_',' ').capitalize(),
                    'description':e['description'],'time_s':e['source_time_s'],'end_s':e['source']['end_s'],
                    'clipId':e['source']['clip_id'],'personId':e.get('track_id'),'sessionTarget':target,
                    'media':e.get('media_url'),'image':(e.get('product_match') or {}).get('evidence',{}).get('image_url') or e.get('thumbnail_url'),'poster':e.get('thumbnail_url'),
                    'image_kind':(e.get('product_match') or {}).get('evidence',{}).get('image_kind','context_frame'),
                    'basis':e['origin'],'observed_at':e['captured_at'],'received_at':e['received_at'],
                    'product_match':e.get('product_match'),'evidence_url':e.get('evidence_url')})
            projection.append({**product,'label':product['label'],'image':product['image']['url'] if product.get('image') else next((r['image'] for r in rows if r['image']),None),
                'image_reference':product.get('image'),'price':displayed_price(product.get('price')),'counts':{'interactions':len(hits),'pickups':sum(e['type'] in {'pick_up','pickup','picked_up_item'} for e in hits),'putbacks':sum(e['type'] in {'put_back','put_back_item'} for e in hits)},
                'events':rows,'sessions':sorted({e['visit_id'] for e in hits if e.get('visit_id')}),'sessionTarget':rows[0]['sessionTarget']})
        sources={k:{'state':'missing' if value is None else 'observed','basis':'committed_source_observations' if k.startswith('observed_') or k=='average_queue_time_s' else 'dated_business_facts',
            'needed_source':'authoritative_pos_and_valid_visit_matching' if k=='conversion_rate' else 'authoritative_pos' if k in {'revenue','average_transaction_value'} else 'validated_visit_count' if k=='observed_visitors' else 'paired_queue_observations'} for k,value in metrics.items()}
        dates=sorted({f['day'] for f in facts}|{e['captured_at'][:10] for e in events if e.get('captured_at')})
        # One reading per day (or per recording), shared by every metric's series.
        by_day={};by_recording={}
        for e in events:
            by_day.setdefault((e.get('captured_at') or '')[:10],[]).append(e);by_recording.setdefault(e['recording_id'],[]).append(e)
        daily={day:self._metrics(by_day.get(day,[]),[f for f in facts if f['day']==day]) for day in dates}
        recorded={} if dates else {recording:self._metrics(rows,[]) for recording,rows in sorted(by_recording.items())}
        for key in metrics:
            sources[key]['series']=[{'at':day,'label':day,'value':daily[day][key]} for day in dates]
            sources[key]['series_label']='By day' if dates else 'By recording'
            for recording,values in recorded.items():
                sources[key]['series'].append({'label':recording,'clipId':by_recording[recording][0]['source']['clip_id'],'value':values[key]})
        return {'metrics':metrics,'metric_sources':sources,'products':projection}

    def snapshot(self,owner,collection=None,*,now=None,scenario=None):
        now=now or datetime.now(timezone.utc);locations=[];scopes=[]
        configs=self._rows(owner,'location');events=[e for e in self._rows(owner,'event') if e.get('origin')!='dataset_reference' and e.get('observation_origin')!='dataset_reference' and e.get('review_state')!='publisher_annotation'];track_visits=self._rows(owner,'tracked_visit');products=self._rows(owner,'product');facts=self._rows(owner,'daily_fact')
        known,readable=self._event_reader(owner)
        for config in configs:
            if collection and config['collection']!=collection:continue
            try:view=self.authorized(owner,config['collection'],config['location_id'])
            except ValueError:continue
            geography=next(l for l in view['locations'] if l['id']==config['location_id']);scopes.append(view['effective'])
            hits=[]
            for e in events:
                if e['collection']!=config['collection'] or e['location_id']!=config['location_id']:continue
                if e['id'] not in known or not readable(*known[e['id']][:2]):continue
                if e['recording_id'] not in view['effective']['source_ids']:continue
                hits.append(e)
            tracked=[]
            for visit in track_visits:
                if visit['collection']!=config['collection'] or visit['location_id']!=config['location_id'] or visit['recording_id'] not in view['effective']['source_ids']:continue
                if not readable(visit['source_id'],visit['collection']):continue
                tracked.append(visit)
            catalog=self.products(owner,config['collection'],config['location_id'],(known,readable))
            with self.scope._db() as db:
                _,graph=self.scope._graph(db,config['collection'])
            location_rooms={r['id'] for r in graph['rooms'] if r['location_id']==config['location_id']}
            complete_location=location_rooms<=set(view['effective']['room_ids'])
            daily=[f for f in facts if complete_location if f['collection']==config['collection'] and f['location_id']==config['location_id'] and (f['basis']!='synthetic_demo' if scenario is None else f['basis']=='synthetic_demo' and f['provenance'].get('scenario')==scenario)]
            item={**geography,**self._projection(hits,catalog,daily),'collection':config['collection'],'cover_image':config['cover_image']['url'] if config.get('cover_image') else None,
                'cover_image_reference':config.get('cover_image'),'coordinates':config.get('coordinates'),'widgets':config['widgets'],
                'rooms':[r for r in view['rooms'] if r['location_id']==config['location_id']],
                'cameras':[c for c in view['cameras'] if c['location_id']==config['location_id']],
                'coverage':{'kind':'source_observations','recording_count':len({e['recording_id'] for e in hits}),'capture_start':min((e['captured_at'] for e in hits if e['captured_at']),default=None),'capture_end':max((e['captured_at'] for e in hits if e['captured_at']),default=None)},
                'period_summaries':{},'availablePeriods':['recordings'],'basis':'synthetic_demo' if scenario else 'committed_observations'}
            for period,days in [('week',7),('month',30),('year',365)]:
                start=(now-timedelta(days=days-1)).date().isoformat();end=now.date().isoformat()
                period_facts=[f for f in daily if start<=f['day']<=end]
                period_events=[e for e in hits if e['captured_at'] and start<=e['captured_at'][:10]<=end]
                if period_facts or period_events:
                    item['period_summaries'][period]={**self._projection(period_events,catalog,period_facts),'period':{'start':start,'end':end},'basis':item['basis']}
                    item['availablePeriods'].append(period)
            sessions=[]
            for visit_id in sorted({e['visit_id'] for e in hits if e.get('visit_id')}):
                observed=sorted([e for e in hits if e.get('visit_id')==visit_id],key=lambda e:e['source_time_s'])
                first=observed[0];last=max(e['source']['end_s'] for e in observed)
                sessions.append({'id':visit_id,'personId':first['track_id'],'clipId':first['source']['clip_id'],'source_recording_id':first['recording_id'],'first_s':first['source_time_s'],'last_s':last,'events':observed,'payment_evidence':payment_evidence(observed),'sessionTarget':{'clipId':first['source']['clip_id'],'personId':first['track_id'],'time_s':first['source_time_s']},'basis':'observed_event_intervals'})
            by_visit={session['id']:session for session in sessions}
            for visit in tracked:
                session=by_visit.get(visit['id']) or {'id':visit['id'],'personId':visit['person_id'],
                    'clipId':visit['source_id'],'source_recording_id':visit['recording_id'],'events':[],
                    'payment_evidence':[],'sessionTarget':{'clipId':visit['source_id'],'personId':visit['person_id'],'time_s':visit['first_s']}}
                session.update(first_s=visit['first_s'],last_s=visit['last_s'],basis='tracker_observations',
                    observed_intervals=visit['observed_intervals'],first_observation_semantics=visit['first_observation_semantics'],
                    tracking_provenance=visit['provenance'])
                by_visit[visit['id']]=session
            sessions=sorted(by_visit.values(),key=lambda s:(s['source_recording_id'],s['first_s'],s['personId']))
            item['sessions']=sessions
            item['metrics']['observed_tracks']=len(sessions)
            if tracked:
                item['metric_sources']['observed_tracks'].update(basis='recording_local_tracker_observations',series_label='By recording',
                    series=[{'label':recording,'clipId':next(s['clipId'] for s in sessions if s['source_recording_id']==recording),
                        'value':sum(s['source_recording_id']==recording for s in sessions)} for recording in sorted({s['source_recording_id'] for s in sessions})])
                item['coverage']['recording_count']=len({e['recording_id'] for e in hits}|{v['recording_id'] for v in tracked})
            currencies={f['currency'] for f in daily if f.get('currency')}
            item['currency']=next(iter(currencies)) if len(currencies)==1 else None
            item['room_count']=len(item['rooms']);locations.append(item)
        return {'schema_version':1,'basis':'synthetic_demo' if scenario else 'committed_observations','locations':locations,'scope_receipts':scopes}
