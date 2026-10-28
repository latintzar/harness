"""Observable watch screening, durable coverage and native admission with fixture vectors."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from background_agents import AgentDefinition,AgentStore,AgentScheduler
from watch_screening import WatchScreener,encoder_identity,source_key
from execution_policy import RETRIEVAL_VIEW

PLAN={'families':[{'id':'handoff','observable':'A person passing an object across the counter.',
    'queries':['a person hands an object to another person'], 'score_threshold':.75,
    'threshold_basis':'Uncalibrated candidate cutoff for this fixture; recall is unknown.',
    'verification_question':'What object visibly moves, from which position to which position?'}]}

class Index:
    def __init__(self,rows=()):
        self._lock=threading.RLock();self.calls=[]
        self.report={'model':'fixture-watch','model_revision':'fixture-r1','embedding_dimensions':2}
        self.replace(rows)
    def replace(self,rows):
        self.clips=[];vectors=[]
        for item in rows:
            key,vector,*extra=item;options=extra[0] if extra else {}
            self.clips.append({'id':key,'dataset':'cafe','video_id':'source-a','camera':'counter',
                'start':0,'end':8,'model':'fixture-watch','model_revision':'fixture-r1',
                'embedded_clip_sha256':key+'-hash',**options})
            vectors.append(vector)
        self.embeddings=np.asarray(vectors,dtype=np.float32).reshape((-1,2))
    def _encode_text(self,query):self.calls.append(query);return np.asarray([1,0],dtype=np.float32)
    def status(self):return {'datasets':['cafe','other'],'model':self.report['model'],'model_revision':self.report['model_revision']}


def spec(**changes):
    return {'title':'Counter watch','instruction':'Investigate visible handoffs.','dataset':'cafe',
        'mode':'ongoing','interval_seconds':10,'max_runs':4,'fresh_inspections_per_run':0,
        'watch_plan':copy.deepcopy(PLAN),**changes}


def create_watch(store,index,**changes):
    return store.create(spec(**changes),['cafe'],owner_principal_id='person-a',
        watch_plan_context={'chat_id':'author-chat','encoder':encoder_identity(index)})


def claim_watch(store,key,index,now=None):
    sources={source_key(c):c['id'] for c in index.clips if c['dataset']=='cafe'}
    records=[c for c in index.clips if c['dataset']=='cafe']
    prepared=store.prepare_screening(key,sources,now=now,source_records=records)
    screen=WatchScreener(store.state,index).screen(*prepared) if prepared else None
    return store.claim(key,sources,now=now,source_records=records,screening=screen)


class Harness:
    def __init__(self):self.children={};self.child_tasks={};self.turns=[];self.fail=False
    def _save_child(self,child):pass
    async def turn(self,message,chat_id,dataset,**kwargs):
        view=RETRIEVAL_VIEW.get()
        self.turns.append({'ids':[c['id'] for c in view.clips],'message':message,'chat_id':chat_id})
        if self.fail:yield {'type':'error','message':'Fixture interrupted'}
        else:yield {'type':'done','status':'completed'}


def test_no_hit_commits_screening_without_native_run_then_late_hit_wakes_once(tmp_path):
    async def prove():
        index=Index([('negative',[0,1]),('foreign',[1,0],{'dataset':'other'})]);store=AgentStore(tmp_path);harness=Harness()
        scheduler=AgentScheduler(store,harness,index,asyncio.Semaphore(2),live_input_ready=lambda _:True)
        record=create_watch(store,index);store.activate(record['id'],time.time()+1000)
        await scheduler.tick()
        checked=store.get(record['id'])
        assert harness.turns==[] and checked['runs_started']==0
        assert checked['last_screening']['status']=='no_candidates'
        assert checked['last_screening']['screened_window_count']==1
        assert checked['screening_coverage']['screened_window_count']==1
        assert len(index.calls)==1
        # Late source time is irrelevant: this new content identity must be screened.
        index.replace([('negative',[0,1]),('late',[1,0],{'video_id':'earlier-source','start':0,'end':4})])
        with store.connect() as db:
            row=store._read(db,'agents',record['id']);row['next_run_at']=0;store._save(db,'agents',row)
        await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
        assert len(harness.turns)==1 and harness.turns[0]['ids']==['late']
        assert len(index.calls)==1 # persisted query vector cache reused
        done=store.get(record['id']);assert done['last_run']['status']=='completed'
        assert done['screening_coverage']['investigated_window_count']==1
        with store.connect() as db:
            row=store._read(db,'agents',record['id']);row['next_run_at']=0;store._save(db,'agents',row)
        await scheduler.tick()
        assert len(harness.turns)==1 and store.get(record['id'])['runs_started']==1
    asyncio.run(prove())


def test_hits_coalesce_and_only_touching_scoped_neighbors_enter_context(tmp_path):
    index=Index([('a',[1,0],{'start':0,'end':8}),('b',[1,0],{'start':8,'end':16}),
        ('neighbor',[0,1],{'start':16,'end':24}),('distant',[0,1],{'start':40,'end':48}),
        ('foreign',[1,0],{'dataset':'other','start':8,'end':16})])
    store=AgentStore(tmp_path);record=create_watch(store,index);store.activate(record['id'],time.time()+1000)
    run=claim_watch(store,record['id'],index)
    assert set(run['source_ids'])=={'a','b','neighbor'}
    assert run['screening']['candidate_window_count']==2
    assert run['screening']['candidate_intervals'][0]['window_count']==2
    assert run['screening']['candidate_intervals'][0]['end']==16
    assert run['screening']['context_clip_ids']==['neighbor']
    assert run['definition']['watch_plan']==PLAN
    assert run['owner_principal_id']=='person-a'


def test_failed_investigation_retains_candidates_and_restart_requires_resume(tmp_path):
    index=Index([('hit',[1,0]),('miss',[0,1],{'video_id':'other-video'})]);store=AgentStore(tmp_path)
    record=create_watch(store,index);store.activate(record['id'],time.time()+1000)
    first=claim_watch(store,record['id'],index)
    store.finish(first['id'],'failed','Provider interrupted')
    failed=store.get(record['id'])
    assert failed['screening_coverage']=={'screened_window_count':2,'pending_candidate_count':1,'investigated_window_count':0,'investigating_window_count':0}
    assert failed['status']=='needs_attention'
    store.activate(record['id'],time.time()+1000)
    second=claim_watch(store,record['id'],index)
    assert second['source_ids']==['hit'] and len(index.calls)==1
    reopened=AgentStore(tmp_path);reopened.recover()
    assert reopened.get(record['id'])['status']=='paused'
    assert reopened.get(record['id'])['last_run']['status']=='interrupted'
    assert reopened.get(record['id'])['screening_coverage']['pending_candidate_count']==1
    assert reopened.prepare_screening(record['id'],{source_key(c):c['id'] for c in index.clips}) is None


def test_encoder_failure_does_not_advance_coverage_and_changed_encoder_requires_review(tmp_path):
    async def prove():
        index=Index([('hit',[1,0])]);store=AgentStore(tmp_path);harness=Harness()
        scheduler=AgentScheduler(store,harness,index,asyncio.Semaphore(2),live_input_ready=lambda _:True);record=create_watch(store,index)
        store.activate(record['id'],time.time()+1000)
        index.report['model_revision']='different-revision'
        await scheduler.tick()
        failed=store.get(record['id'])
        assert failed['status']=='needs_attention' and 'Recompile' in failed['screening_error']
        assert failed['screening_coverage']['screened_window_count']==0 and harness.turns==[]
    asyncio.run(prove())


def test_same_clip_replacement_and_revised_plan_have_distinct_coverage(tmp_path):
    index=Index([('same',[0,1])]);store=AgentStore(tmp_path);record=create_watch(store,index)
    now=time.time();store.activate(record['id'],now+1000,now=now)
    assert claim_watch(store,record['id'],index,now=now) is None
    index.replace([('same',[1,0],{'embedded_clip_sha256':'replacement'})])
    run=claim_watch(store,record['id'],index,now=now+11)
    assert run['source_ids']==['same']
    store.finish(run['id'],'completed',now=now+12);store.pause(record['id'])
    changed=copy.deepcopy(PLAN);changed['families'][0]['score_threshold']=.99
    updated=store.update(record['id'],spec(watch_plan=changed),['cafe'],expected_revision=1,
        watch_plan_context={'chat_id':'revision-chat','encoder':encoder_identity(index)})
    assert updated['revision']==2 and updated['screening_coverage']['screened_window_count']==0
    assert updated['watch_plan_provenance']['definition_revision']==2


def test_missing_plan_and_self_asserted_authorship_cannot_activate(tmp_path):
    store=AgentStore(tmp_path)
    for body in [spec(watch_plan=None),spec()]:
        record=store.create(body,['cafe'])
        assert record['screening_status']=='plan_review_required'
        with pytest.raises(ValueError,match='agent-authored'):store.activate(record['id'],time.time()+1000)
    with pytest.raises(ValueError):AgentDefinition.model_validate(spec(watch_plan_provenance={'kind':'agent'}))


def test_legacy_active_watch_stops_for_review_before_native_call(tmp_path):
    async def prove():
        store=AgentStore(tmp_path);index=Index([('hit',[1,0])]);harness=Harness()
        scheduler=AgentScheduler(store,harness,index,asyncio.Semaphore(2),live_input_ready=lambda _:True)
        record=store.create(spec(watch_plan=None),['cafe'])
        with store.connect() as db:
            legacy=store._read(db,'agents',record['id']);legacy.update(status='active',expires_at=time.time()+100,next_run_at=0)
            store._save(db,'agents',legacy)
        await scheduler.tick()
        assert harness.turns==[] and store.get(record['id'])['status']=='needs_attention'
    asyncio.run(prove())


def test_screening_completion_cannot_cross_revision_or_scope(tmp_path):
    index=Index([('hit',[1,0])]);store=AgentStore(tmp_path);record=create_watch(store,index)
    store.activate(record['id'],time.time()+1000)
    sources={source_key(c):c['id'] for c in index.clips};prepared=store.prepare_screening(record['id'],sources)
    screening=WatchScreener(tmp_path,index).screen(*prepared)
    screening['receipt']['dataset']='other'
    with pytest.raises(ValueError,match='scope'):store.claim(record['id'],sources,source_records=index.clips,screening=screening)
    assert store.get(record['id'])['screening_coverage']['screened_window_count']==0
    assert store.get(record['id'])['runs_started']==0


def test_concurrent_claims_start_one_investigation_and_commit_one_screen(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    index=Index([('hit',[1,0])]);store=AgentStore(tmp_path);record=create_watch(store,index)
    store.activate(record['id'],time.time()+1000)
    sources={source_key(c):c['id'] for c in index.clips}
    proof=WatchScreener(tmp_path,index).screen(*store.prepare_screening(record['id'],sources))
    def claim(_):return store.claim(record['id'],sources,source_records=index.clips,screening=proof)
    with ThreadPoolExecutor(max_workers=2) as pool:runs=list(pool.map(claim,range(2)))
    assert sum(run is not None for run in runs)==1
    assert store.get(record['id'])['runs_started']==1
    with store.connect() as db:assert db.execute('SELECT COUNT(*) FROM watch_screens').fetchone()[0]==1


def test_agent_tools_require_context_and_stamp_current_model_owned_plan(tmp_path):
    from agent_bridge import CodexVideoAgent,tool_specs
    async def prove():
        index=Index([('hit',[1,0])]);agent=CodexVideoAgent(tmp_path,index,lambda:[])
        agent.automation_store=AgentStore(tmp_path)
        agent.children['author-chat']={'id':'author-chat','owner_principal_id':'person-a','dataset':'cafe'}
        agent.active['author-chat']={}
        body=spec(description='Counter events',watch_for=['Observable handoffs'],evidence_window='last_interval',lookback_seconds=600)
        body.pop('dataset')
        _,_,okay=await agent._tool('propose_agent',body,'cafe','author-chat')
        assert not okay and agent.automation_store.list()==[]
        agent.active['author-chat']['business_context_read']=True
        content,_,okay=await agent._tool('propose_agent',body,'cafe','author-chat')
        assert okay
        created=json.loads(content[0]['text'])
        assert created['screening_status']=='ready'
        assert created['watch_plan_provenance']['chat_id']=='author-chat'
        assert created['watch_plan_provenance']['encoder']==encoder_identity(index)
        assert created['owner_principal_id']=='person-a'
        assert created['lookback_seconds']==600 and created['description']=='Counter events'
        updated={**body,'agent_id':created['id'],'expected_revision':1,'title':'Updated counter watch'}
        content,_,okay=await agent._tool('update_agent',updated,'cafe','author-chat')
        assert okay and json.loads(content[0]['text'])['watch_plan_provenance']['definition_revision']==2
    asyncio.run(prove())
    schemas={tool['name']:tool['inputSchema'] for tool in tool_specs()}
    for name in ['propose_agent','update_agent']:
        assert {'watch_plan','description','watch_for','skill_ids','evidence_window','lookback_seconds'}<=set(schemas[name]['properties'])
        assert 'dataset' not in schemas[name]['properties']
        assert 'watch_plan_provenance' not in schemas[name]['properties']


def test_query_vector_cache_is_company_scoped_and_bad_encoder_output_keeps_coverage(tmp_path):
    one=tmp_path/'one';two=tmp_path/'two';index=Index([('hit',[1,0])])
    for path in [one,two]:
        store=AgentStore(path);record=create_watch(store,index);store.activate(record['id'],time.time()+1000)
        assert claim_watch(store,record['id'],index)
    assert len(index.calls)==2
    third=tmp_path/'third';store=AgentStore(third);record=create_watch(store,index);store.activate(record['id'],time.time()+1000)
    index._encode_text=lambda query:np.asarray([float('nan'),0],dtype=np.float32)
    with pytest.raises(ValueError,match='finite'):claim_watch(store,record['id'],index)
    assert store.get(record['id'])['screening_coverage']['screened_window_count']==0


def test_large_negative_batch_receipt_is_bounded_and_does_not_consume_runs(tmp_path):
    index=Index([(f'miss-{n}',[0,1],{'start':n*8,'end':(n+1)*8}) for n in range(2500)])
    store=AgentStore(tmp_path);record=create_watch(store,index);store.activate(record['id'],time.time()+1000)
    assert claim_watch(store,record['id'],index) is None
    checked=store.get(record['id'])
    assert checked['runs_started']==0 and checked['screening_coverage']['screened_window_count']==2500
    assert len(json.dumps(checked['last_screening']))<3000
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM watch_windows').fetchone()[0]==2500
        assert db.execute('SELECT COUNT(*) FROM watch_screens').fetchone()[0]==1


def test_manual_once_without_candidates_pauses_after_its_screening(tmp_path):
    index=Index([('miss',[0,1])]);store=AgentStore(tmp_path);record=create_watch(store,index)
    store.activate(record['id'],time.time()+1000,once=True)
    assert claim_watch(store,record['id'],index) is None
    checked=store.get(record['id'])
    assert checked['status']=='paused' and checked['next_run_at'] is None
    assert checked['runs_started']==0 and checked['last_screening']['status']=='no_candidates'
