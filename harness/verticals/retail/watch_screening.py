"""CPU candidate screening for explicit model-authored ongoing watch plans.

Similarity cutoffs select candidates for investigation. They establish neither an
alarm nor exhaustive recall. This module has no native-agent or provider client.
"""
from __future__ import annotations

from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

import numpy as np
from pydantic import BaseModel,ConfigDict,Field,model_validator

from investigation_plans import InvestigationPlans
from workspace_identity import workspace_scope


class WatchFamily(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    id: str=Field(pattern=r'^[a-z][a-z0-9_-]{0,47}$')
    observable: str=Field(min_length=1,max_length=400)
    queries: list[str]=Field(min_length=1,max_length=4)
    score_threshold: float=Field(ge=-1,le=1)
    threshold_basis: str=Field(min_length=1,max_length=600)
    verification_question: str=Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def nonempty(self):
        for text in [self.observable,self.threshold_basis,self.verification_question,*self.queries]:
            if not text.strip():raise ValueError('Describe the observable criteria and screening assumptions.')
        if any(len(q)>300 for q in self.queries):raise ValueError('Use focused visual queries of up to 300 characters.')
        self.queries=list(dict.fromkeys(q.strip() for q in self.queries))
        return self


class WatchPlan(BaseModel):
    model_config=ConfigDict(extra='forbid')
    families: list[WatchFamily]=Field(min_length=1,max_length=8)

    @model_validator(mode='after')
    def bounds(self):
        if len({f.id for f in self.families})!=len(self.families):raise ValueError('Use distinct watch family IDs.')
        if sum(len(f.queries) for f in self.families)>24:raise ValueError('Use up to 24 observable watch queries.')
        return self


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def source_key(clip):
    return hashlib.sha256(json.dumps([clip['id'],clip.get('embedded_clip_sha256'),clip.get('model_revision')]).encode()).hexdigest()


def encoder_identity(index):
    identity=InvestigationPlans._model(index)
    if (not isinstance(identity['name'],str) or not identity['name']
            or not isinstance(identity['revision'],str) or not identity['revision']
            or type(identity['dimensions']) is not int or not 1<=identity['dimensions']<=4096):
        raise ValueError('Prepare the matching versioned video and text encoder before compiling this watch.')
    return identity


def author_plan(state,plan,revision,owner,context):
    if plan is None or context is None:return None
    if (not isinstance(context,dict) or set(context)!={'chat_id','encoder'}
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(context['chat_id']))):
        raise ValueError('Bind the watch plan to its actual authoring chat.')
    encoder=context['encoder']
    if (not isinstance(encoder,dict) or set(encoder)!={'name','revision','dimensions'}
            or not isinstance(encoder['name'],str) or not encoder['name']
            or not isinstance(encoder['revision'],str) or not encoder['revision']
            or type(encoder['dimensions']) is not int or not 1<=encoder['dimensions']<=4096):
        raise ValueError('Bind the watch plan to the matching retrieval encoder.')
    return {'kind':'agent','chat_id':context['chat_id'],'company_scope':workspace_scope(state),
        'owner_principal_id':owner,'definition_revision':revision,'plan_sha256':digest(plan),
        'encoder':encoder,'authored_at':datetime.now(timezone.utc).isoformat()}


def validate_plan(record,state):
    plan=WatchPlan.model_validate(record.get('watch_plan')).model_dump()
    provenance=record.get('watch_plan_provenance') or {}
    if (provenance.get('kind')!='agent' or provenance.get('definition_revision')!=record['revision']
            or provenance.get('plan_sha256')!=digest(plan)
            or provenance.get('company_scope')!=workspace_scope(state)
            or provenance.get('owner_principal_id')!=record.get('owner_principal_id')):
        raise ValueError('Review an agent-authored screening plan for this saved revision before activating the watch.')
    return plan,provenance


def coalesce(records):
    intervals=[]
    for clip in sorted(records,key=lambda c:(c['video_id'],c['start'],c['end'],c['id'])):
        if intervals and intervals[-1]['video_id']==clip['video_id'] and clip['start']<=intervals[-1]['end']+1e-6:
            row=intervals[-1];row['end']=max(row['end'],clip['end']);row['window_count']+=1
            if len(row['clip_ids'])<8:row['clip_ids'].append(clip['id'])
        else:intervals.append({'video_id':clip['video_id'],'start':clip['start'],'end':clip['end'],
            'window_count':1,'clip_ids':[clip['id']]})
    return intervals


class WatchScreener:
    def __init__(self,state,index):
        self.state,self.index=Path(state),index
        # Reuse the existing company-private durable query-vector cache and the
        # already loaded encoder; watches never create a separate model tower.
        self.plans=InvestigationPlans(state,index)

    def screen(self,record,sources,*,snapshot=None):
        plan,provenance=validate_plan(record,self.state)
        view=snapshot if snapshot is not None else self.plans._snapshot(self.index)
        model=encoder_identity(view)
        if model!=provenance['encoder']:
            raise ValueError('Recompile this watch plan for the current retrieval encoder revision.')
        clips={source_key(c):(i,c) for i,c in enumerate(view.clips) if c['dataset']==record['dataset']}
        if any(key not in clips or clips[key][1]['id']!=cid for key,cid in sources.items()):
            raise ValueError('Screen only the current committed windows in this watch collection.')
        if len(view.embeddings)!=len(view.clips) or view.embeddings.ndim!=2 or view.embeddings.shape[1]!=model['dimensions']:
            raise ValueError('Use matching committed source metadata and vector rows.')
        stats={'hits':0,'misses':0};rows=[];counts={f['id']:0 for f in plan['families']}
        if sources:
            encode=self.plans._cached_encoder(view,model,stats)
            queries=list(dict.fromkeys(q for f in plan['families'] for q in f['queries']))
            vectors=np.stack([encode(q) for q in queries]).astype(np.float32)
            vectors/=np.linalg.norm(vectors,axis=1,keepdims=True)
            positions={q:i for i,q in enumerate(queries)}
            items=list(sources.items())
            for start in range(0,len(items),1024):
                batch=items[start:start+1024]
                matrix=np.asarray(view.embeddings[[clips[k][0] for k,_ in batch]],dtype=np.float32)
                norms=np.linalg.norm(matrix,axis=1,keepdims=True)
                if not np.isfinite(matrix).all() or np.any(norms<1e-8):raise ValueError('Screen finite nonzero source vectors.')
                scores=np.clip((matrix/norms)@vectors.T,-1,1)
                for row,(key,cid) in zip(scores,batch):
                    matches=[]
                    for family in plan['families']:
                        best=max((float(row[positions[q]]),q) for q in family['queries'])
                        if best[0]>=family['score_threshold']:
                            counts[family['id']]+=1
                            matches.append({'family_id':family['id'],'score':round(best[0],7),'query':best[1]})
                    rows.append({'source_key':key,'clip_id':cid,'matches':matches})
        hits=[clips[r['source_key']][1] for r in rows if r['matches']]
        intervals=coalesce(hits)
        receipt={'id':'screen-'+uuid.uuid4().hex,'agent_id':record['id'],'definition_revision':record['revision'],
            'plan_sha256':provenance['plan_sha256'],'encoder':model,'dataset':record['dataset'],
            'company_scope':provenance['company_scope'],'screened_window_count':len(rows),
            'candidate_window_count':len(hits),'candidate_interval_count':len(intervals),
            'source_keys_sha256':digest(sorted(sources)),'family_candidate_counts':counts,
            'candidate_intervals':intervals[:8],'query_vector_cache':stats,
            'status':'candidates' if hits else 'no_candidates','created_at':datetime.now(timezone.utc).isoformat(),
            'meaning':'Similarity candidates for source verification; thresholds have unmeasured recall.',
            'native_agent_started':False,'provider_spend_usd':'0'}
        return {'receipt':receipt,'rows':rows}
