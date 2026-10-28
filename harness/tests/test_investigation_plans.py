"""Immutable model plans execute all queries with scoped, traceable candidate receipts."""
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from investigation_plans import InvestigationPlans
from camera_context import CameraStore


class Index:
    def __init__(self, root):
        self.report = {'model':'fixture-embed','model_revision':'revision-1'}
        self.embeddings = np.array([[1.,0.],[.8,.6],[0.,1.],[1.,0.]],dtype=np.float32)
        self.encoded, self.searched = [], []
        self.clips=[]
        for i,(asset,start,dataset) in enumerate([('camera-a',0,'cafe'),('camera-a',8,'cafe'),('camera-b',0,'cafe'),('elsewhere',0,'warehouse')]):
            path=root/(asset+'.mp4');path.write_bytes(asset.encode())
            self.clips.append(dict(id=f'clip-{i}',video_id=asset,dataset=dataset,camera=asset,start=start,end=start+8,
                sample_time=start+4,path=str(path),thumbnail_path=str(path),embedded_clip_sha256=f'sha-{i}',
                model='fixture-embed',model_revision='revision-1',source_reference='https://example.org/source'))

    def get_clip(self,key):return next((c for c in self.clips if c['id']==key),None)

    def _encode_text(self,query):
        self.encoded.append(query)
        return np.array([1.,0.] if query=='person carrying a tray' else [0.,1.],dtype=np.float32)

    def search(self,query,limit=6,dataset=None):
        self.searched.append(query)
        vector=self._encode_text(query)
        eligible=[i for i,c in enumerate(self.clips) if dataset is None or c['dataset']==dataset]
        scores=self.embeddings[eligible]@vector
        return [{**self.clips[eligible[i]],'score':float(scores[i])} for i in np.argsort(-scores,kind='stable')[:limit]]


def document():
    return {'question':'What happened around the counter?', 'business_context':'A model interpretation of the task',
        'assumptions':['Service quality is not observable from posture alone.'],'coverage_goal':'examples',
        'categories':[
            {'id':'service','label':'Visible service activity','observable_criteria':['A person carries an object'],
             'queries':['person carrying a tray','person waiting at counter'],
             'verification_question':'What objects are visibly carried?','counterexamples':['A person passing behind the counter']},
            {'id':'waiting','label':'Visible waiting','observable_criteria':['A person remains near the counter'],
             'queries':['person waiting at counter'],
             'verification_question':'Is waiting visibly supported?','counterexamples':['A person briefly walking past']} ]}


def test_plans_persist_immutable_context_and_enforce_chat_collection_scope(tmp_path):
    index=Index(tmp_path);state=tmp_path/'state';store=InvestigationPlans(state,index)
    body=document();plan=store.save('cafe','chat-a',body)
    body['categories'][0]['queries'][0]='mutated caller input'
    index.report['model_revision']='revision-2'
    loaded=InvestigationPlans(state,index).get(plan['id'],'cafe','chat-a')
    assert loaded['categories'][0]['queries'][0]=='person carrying a tray'
    assert loaded['context_snapshot']['retrieval_model']['revision']=='revision-1'
    assert loaded['authorship']['business_context']=='model_interpretation'
    assert loaded['context_snapshot']['indexed_window_count']==3
    for dataset,chat in [('warehouse','chat-a'),('cafe','chat-b')]:
        with pytest.raises(ValueError):store.get(plan['id'],dataset,chat)
    new=store.save('cafe','chat-a',document())
    assert new['id']!=plan['id']


def test_all_planned_queries_run_and_duplicate_candidates_keep_category_provenance(tmp_path):
    index=Index(tmp_path);store=InvestigationPlans(tmp_path/'state',index)
    plan=store.save('cafe','chat-a',document())
    result=store.retrieve(plan['id'],'cafe','chat-a',limit_per_query=2)
    assert set(index.searched)=={'person carrying a tray','person waiting at counter'}
    assert len(result['query_runs'])==3
    assert len(result['results'])==3
    assert len({c['id'] for c in result['results']})==3
    middle=next(c for c in result['results'] if c['id']=='clip-1')
    assert set(middle['matched_categories'])=={'service','waiting'}
    assert len(middle['matched_queries'])==3
    assert result['coverage']['scored_window_count']==3
    assert result['coverage']['candidate_window_count']==3
    assert result['coverage']['verified_window_count']==0
    assert result['coverage']['complete_recall_established'] is False
    merged=next(i for i in result['intervals'] if i['video_id']=='camera-a')
    assert merged['clip_ids']==['clip-0','clip-1']
    assert (merged['start'],merged['end'])==(0,16)
    assert str(tmp_path) not in json.dumps(result)
    receipt=store.get_receipt(result['receipt_id'],'cafe','chat-a')
    assert receipt['plan_id']==plan['id']
    assert receipt['query_runs']==result['query_runs']
    store.retrieve(plan['id'],'cafe','chat-a',limit_per_query=2)
    assert len(index.encoded)==2, 'Matching query vectors should be reused on a second local run'


def test_incremental_view_restricts_scoring_and_direct_ids_restrict_source_scope(tmp_path):
    index=Index(tmp_path);store=InvestigationPlans(tmp_path/'state',index)
    plan=store.save('cafe','chat-a',document())
    view=copy.copy(index);view.clips=[index.clips[2]];view.embeddings=index.embeddings[[2]]
    result=store.retrieve(plan['id'],'cafe','chat-a',index=view)
    assert [c['id'] for c in result['results']]==['clip-2']
    assert result['coverage']['scored_window_count']==1
    assert result['coverage']['collection_window_count']==3
    body=document();body['direct_clip_ids']=['clip-0']
    direct=store.save('cafe','chat-a',body)
    assert direct['mode']=='direct'
    assert [c['id'] for c in store.retrieve(direct['id'],'cafe','chat-a')['results']]==['clip-0']
    body['direct_clip_ids']=['clip-3']
    with pytest.raises(ValueError):store.save('cafe','chat-a',body)
    view.clips=[{**index.clips[0],'id':'foreign-clip'}]
    with pytest.raises(ValueError):store.retrieve(plan['id'],'cafe','chat-a',index=view)


def test_plan_limits_are_structural_and_model_revision_changes_require_new_plan(tmp_path):
    index=Index(tmp_path);store=InvestigationPlans(tmp_path/'state',index)
    body=document();body['categories'][1]['id']='service'
    with pytest.raises(ValueError):store.save('cafe','chat-a',body)
    body=document();body['categories']=[{**body['categories'][0],'id':f'category-{n}','queries':['a','b','c','d']} for n in range(7)]
    with pytest.raises(ValueError):store.save('cafe','chat-a',body)
    plan=store.save('cafe','chat-a',document())
    index.report['model_revision']='revision-2'
    with pytest.raises(ValueError):store.retrieve(plan['id'],'cafe','chat-a')


def test_inspection_receipts_require_actual_matching_scoped_invocation(tmp_path):
    index=Index(tmp_path);store=InvestigationPlans(tmp_path/'state',index)
    plan=store.save('cafe','chat-a',document())
    question='What is visible?'
    result={'model':'nvidia/Cosmos3-Super','model_revision':'pinned-revision','run_id':'actual-run',
        'budget':{'token':'private-provider-value'},'requests':[{'clip_ids':['clip-0'],'question':question,
        'sources':[{'id':'clip-0','source_video_id':'camera-a','start':0,'end':8,'sha256':'prepared-sha','path':'/private/source'}],
        'final_text':'Model observation from supplied source','parsed':True,
        'result':{'observations':[{'clip_id':'clip-0','summary':'Model observation from supplied source'}]}}]}
    receipt_id=store.record_inspection(plan['id'],'cafe','chat-a',['service'],question,['clip-0'],result)
    receipt=store.get_inspection(receipt_id,'cafe','chat-a')
    assert receipt['evidence_type']=='model_observations'
    assert receipt['result']['requests'][0]['final_text']=='Model observation from supplied source'
    assert 'private-provider-value' not in json.dumps(receipt)
    assert '/private/source' not in json.dumps(receipt)
    assert receipt['source_clips'][0]['id']=='clip-0'
    for categories,asked,clips in [(['unknown'],question,['clip-0']),(['service'],'Different question',['clip-0']),
                                    (['service'],question,['clip-3'])]:
        with pytest.raises(ValueError):store.record_inspection(plan['id'],'cafe','chat-a',categories,asked,clips,result)


def test_confirmed_camera_context_is_snapshotted_without_rewriting_prior_plan(tmp_path):
    index=Index(tmp_path);state=tmp_path/'state'
    cameras=CameraStore(state,index)
    camera=next(c for c in cameras.list('cafe')['cameras'] if c['asset_ids']==['camera-a'])
    body={'expected_revision':camera['revision'],'anchor_clip_id':'clip-0','valid_from_seconds':0,'room':'Earlier context'}
    draft=cameras.propose(camera['id'],body,origin='operator_note')
    confirmed=cameras.confirm(camera['id'],draft['draft']['id'],expected_revision=draft['revision'],operator='Test operator')
    store=InvestigationPlans(state,index);plan=store.save('cafe','chat-a',document())
    body.update(expected_revision=confirmed['revision'],room='Revised context')
    draft=cameras.propose(camera['id'],body,origin='operator_note')
    cameras.confirm(camera['id'],draft['draft']['id'],expected_revision=draft['revision'],operator='Test operator')
    assert store.get(plan['id'],'cafe','chat-a')['context_snapshot']['camera_contexts'][0]['room']=='Earlier context'
    assert store.context('cafe')['camera_contexts'][0]['room']=='Revised context'


def test_large_candidate_union_caps_evidence_cards_and_reports_omitted_windows(tmp_path):
    index=Index(tmp_path)
    template=index.clips[0]
    index.clips=[{**template,'id':f'window-{i:03d}','start':i*8,'end':(i+1)*8} for i in range(288)]
    index.embeddings=np.repeat(np.eye(24,dtype=np.float32),12,axis=0)
    queries={f'visible-query-{i}':np.eye(24,dtype=np.float32)[i] for i in range(24)}
    index._encode_text=lambda query:queries[query]
    body=document()
    body['categories']=[{**body['categories'][0],'id':f'category-{i}',
        'queries':[f'visible-query-{j}' for j in range(i*4,i*4+4)]} for i in range(6)]
    store=InvestigationPlans(tmp_path/'state',index);plan=store.save('cafe','chat-a',body)
    result=store.retrieve(plan['id'],'cafe','chat-a',limit_per_query=12)
    assert len(result['results'])==96
    assert result['coverage']['candidate_window_count']==288
    assert result['coverage']['omitted_candidate_window_count']==192
    assert len(result['query_runs'])==24
