"""Persistent location projections use actual source evidence and current grants."""
import sys
from pathlib import Path
from datetime import datetime, timezone
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from location_intelligence import LocationIntelligence
from monitoring import MonitoringStore
from test_business_scope import fixture


def setup(tmp_path):
    scope,actor,members,save,ids=fixture(tmp_path)
    monitor=MonitoringStore(tmp_path)
    for key in ['owned-one','owned-wide']:
        monitor.register_source('owner',{'id':key,'video_id':key,'dataset':'business','end':20,'sha256':'a'*64,'media_url':'/api/media/'+key,'capture_start_utc':'1970-01-01T00:00:00+00:00','capture_end_utc':'1970-01-01T00:00:20+00:00'})
    profile=monitor.save_profile('owner',{'name':'Interactions','dataset':'business','vocabulary':[{'type':t,'description':t} for t in ['pick_up','put_back','entered_queue','reached_checkout']]},datasets=['business'])
    store=LocationIntelligence(tmp_path,scope,monitor,clock=lambda:1800000000)
    store.configure_location('owner',{'collection':'business','location_id':ids['a']})
    def event(kind='pick_up',source='owned-one',start=2,end=3,key='first'):
        return monitor.commit_event('owner',{'profile_id':profile['id'],'profile_revision':1,'event_key':key,'type':kind,'description':'Observed '+kind,
            'source':{'clip_id':source,'start_s':start,'end_s':end},'track_id':'P1','object_ref':'packet','object_description':'A green carton'})
    return store,scope,monitor,event,ids,members,save


def test_stream_projection_idempotency_separate_visits_and_original_clock(tmp_path):
    store,scope,monitor,event,ids,_,_=setup(tmp_path)
    a=store.ingest_event('owner',event(),job_id='job-a')
    assert store.ingest_event('owner',event(),job_id='retry')==a
    b=store.ingest_event('owner',event(source='owned-wide'),job_id='job-b')
    assert a['visit_id']!=b['visit_id'] and a['product_id']!=b['product_id']
    assert a['captured_at'] is None and a['received_at']==1800000000 and a['source_time_s']==2
    data=LocationIntelligence(tmp_path,scope,monitor).snapshot('owner')['locations'][0]
    assert data['metrics']['observed_interactions']==2 and data['metrics']['observed_tracks']==2
    assert data['metrics']['revenue'] is None and data['period_summaries']=={}
    assert len(data['products'])==2 and len(store._rows('owner','visit'))==2
    assert data['products'][0]['events'][0]['sessionTarget']['personId']=='P1'


def test_catalog_candidate_keeps_observed_identity_until_confirmed(tmp_path):
    store,_,_,event,ids,_,_=setup(tmp_path)
    original=event();projected=store.ingest_event('owner',original)
    product=store.save_product('owner',{'id':'sku-green','collection':'business','location_id':ids['a'],'label':'Green tea','status':'confirmed','sku':'1234',
        'image':{'url':'/catalog/green.png','kind':'catalog','provenance':{'import':'pos1'}},'provenance':{'kind':'pos','record_id':'1234'}})
    candidate=store.apply_product_match('owner',original['id'],{'product_id':product['id'],'status':'candidate','score':.8,'evidence':{'ocr':'green'},'inspection_id':'inspect1'})
    assert candidate['product_id']==projected['product_id']
    confirmed=store.apply_product_match('owner',original['id'],{'product_id':product['id'],'status':'confirmed','score':1,'evidence':{'barcode':'1234'},'inspection_id':'inspect2'})
    assert confirmed['product_id']=='sku-green' and confirmed['item_id']==original['item_id']
    assert store.ingest_event('owner',original)==confirmed
    assert len(store.catalog_products('owner',original))==2
    assert store.snapshot('owner')['locations'][0]['products'][0]['image_reference']['kind']=='catalog'


def test_removed_source_and_member_hide_projection_and_reject_rewrites(tmp_path):
    store,scope,_,event,ids,members,save=setup(tmp_path)
    e=event();store.ingest_event('owner',e)
    save('cameras',{'id':ids['cam'],'name':'Kitchen camera','location_id':ids['a'],'room_ids':[ids['room']],'source_ids':[]})
    assert store.snapshot('owner')['locations'][0]['metrics']['observed_interactions']==0
    with pytest.raises(ValueError):store.ingest_event('owner',e)
    members.pop('owner')
    assert store.snapshot('owner')['locations']==[]
    with pytest.raises(ValueError):store.save_product('owner',{'id':'new','collection':'business','location_id':ids['a'],'label':'Item','provenance':{'source':'pos'}})


def test_daily_fact_weighting_and_synthetic_scenario_separation(tmp_path):
    store,_,_,_,ids,_,_=setup(tmp_path)
    base={'collection':'business','location_id':ids['a'],'basis':'pos','currency':'EUR','provenance':{'receipt':'pos'}}
    store.save_daily_fact('owner',{**base,'id':'day1','day':'2026-10-08','visitors':10,'transactions':2,'purchasing_visits':2,'revenue_minor':600,'queue_wait_total_s':20,'queue_sample_count':2})
    store.save_daily_fact('owner',{**base,'id':'day2','day':'2026-10-09','visitors':20,'transactions':3,'purchasing_visits':3,'revenue_minor':900,'queue_wait_total_s':90,'queue_sample_count':3})
    store.save_daily_fact('owner',{**base,'id':'mock','day':'2026-10-09','basis':'synthetic_demo','provenance':{'scenario':'ui-check'},'revenue_minor':999999})
    data=store.snapshot('owner',now=datetime(2026,10,9,tzinfo=timezone.utc))['locations'][0]
    assert data['metrics']['revenue']==15 and data['metrics']['average_transaction_value']==3
    assert data['metrics']['conversion_rate']==5/30 and data['metrics']['average_queue_time_s']==22
    assert data['period_summaries']['week']['metrics']['revenue']==15
    assert len(data['metric_sources']['revenue']['series'])==2
    assert store.snapshot('owner',scenario='ui-check')['locations'][0]['metrics']['revenue']==9999.99


def test_seed_uses_existing_company_and_owner_is_idempotent(tmp_path):
    from access import AccessStore
    from seed_control_room import seed_location
    state=tmp_path/'state';access=AccessStore(state)
    access.create_environment('retail-demo','Retail demo')
    access.issue('retail-demo',owner=True,principal_id='peter')
    kwargs={'lifecycle':'development','collection':'retail-demo','address':'Vitosha Boulevard 85, Sofia'}
    a=seed_location(state/'environments/retail-demo',access.path,'retail-demo','peter',**kwargs)
    b=seed_location(state/'environments/retail-demo',access.path,'retail-demo','peter',**kwargs)
    assert a==b
    with pytest.raises(ValueError):seed_location(state,access.path,'retail-demo','peter',**kwargs)
    with pytest.raises(ValueError):seed_location(state/'environments/retail-demo',access.path,'retail-demo','stranger',**kwargs)
    with pytest.raises(ValueError):seed_location(state/'environments/retail-demo',access.path,'retail-demo','peter',**{**kwargs,'lifecycle':'production'})


def test_location_http_auth_and_owner_data_isolation(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/control-room/location').status_code==401
        client.post('/api/session',json={'code':app.state.access.issue('demo',owner=True,principal_id='peter')},headers=HEADERS)
        ws=app.state.workspaces['demo'];scope=ws['business_scope']
        from business_scope import ScopeActor
        actor=ScopeActor(scope.company_id,'peter')
        geo=scope.mutate(actor,'cafe','locations',{'name':'Shop','address':'Sofia'},0)['locations'][0]
        ws['location_intelligence'].configure_location('peter',{'collection':'cafe','location_id':geo['id']})
        assert client.get('/api/control-room/location').json()['locations'][0]['name']=='Shop'
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='other')},headers=HEADERS)
        assert client.get('/api/control-room/location').json()['locations']==[]
        assert client.get('/api/control-room/catalog?collection=cafe&location_id='+geo['id']).status_code==403


def test_full_inspection_receipt_projects_saved_image_without_fabricating_catalog(tmp_path):
    store,_,_,event,_,_,_=setup(tmp_path)
    e=event();projected=store.ingest_event('owner',e)
    receipt={'status':'unresolved','product_id':None,'basis':'object_evidence','inspection_id':'inspect',
        'created_at':1800000010,'evidence':{'source_id':'owned-one','source_time_s':2,'image_url':'/api/catalog/inspections/inspect/image','image_kind':'object_crop'}}
    saved=store.apply_product_match('owner',e['id'],receipt)
    assert saved['product_id']==projected['product_id']
    row=store.snapshot('owner')['locations'][0]['products'][0]
    assert row['status']=='provisional' and row['sku'] is None
    assert row['events'][0]['image']==receipt['evidence']['image_url']
    assert row['events'][0]['image_kind']=='object_crop'


@pytest.mark.parametrize('basis,label',[('estimated','Estimated price'),('catalog_sourced','Catalog price'),('pos_confirmed','POS price')])
def test_price_provenance_is_explicit_and_never_becomes_sales(tmp_path,basis,label):
    store,_,_,event,ids,_,_=setup(tmp_path)
    observed=event();projection=store.ingest_event('owner',observed)
    product=store._get('owner','product',projection['product_id'])
    price={'amount':'3.2500','currency':'EUR','basis':basis,'source':'fixture catalog receipt',
        'observed_at':'2026-10-09T12:00:00+00:00','confidence':.7 if basis=='estimated' else 1}
    product.update(price=price,status='confirmed',sku='tea-1')
    store.save_product('owner',product)
    snapshot=store.snapshot('owner')['locations'][0]
    shown=snapshot['products'][0]['price']
    assert shown['amount']=='3.2500' and shown['basis']==basis and shown['display_basis']==label
    assert snapshot['metrics']['revenue'] is None
    assert snapshot['metrics']['average_transaction_value'] is None
    assert snapshot['metrics']['conversion_rate'] is None
    assert store.product_match('owner',observed['id'])['product']['price']==shown


@pytest.mark.parametrize('change',[{'amount':'-1'},{'currency':'euros'},{'confidence':1.01},{'source':'   '},{'observed_at':'2026-10-09T12:00:00'}])
def test_product_price_requires_valid_amount_and_traceable_basis(tmp_path,change):
    from location_intelligence import ProductPrice
    value={'amount':'3.25','currency':'EUR','basis':'estimated','source':'supplier listing',
        'observed_at':'2026-10-09T12:00:00+00:00','confidence':.7}
    with pytest.raises(ValueError):ProductPrice.model_validate({**value,**change})


@pytest.mark.parametrize('payment_kind',['handed_cash','presented_card','paid_with_card'])
def test_visible_payment_event_keeps_its_evidence_without_amount_or_settlement(tmp_path,payment_kind):
    store,_,monitor,_,ids,_,_=setup(tmp_path)
    profile=monitor.save_profile('owner',{'name':'Payment actions','dataset':'business',
        'vocabulary':[{'type':payment_kind,'description':'Visible payment action.'}]},datasets=['business'])
    e=monitor.commit_event('owner',{'profile_id':profile['id'],'profile_revision':1,'event_key':'cash',
        'type':payment_kind,'description':'The visitor performs a visible payment action.',
        'source':{'clip_id':'owned-one','start_s':5,'end_s':6},'track_id':'P1'})
    store.ingest_event('owner',e)
    result=store.snapshot('owner')['locations'][0]
    evidence=result['sessions'][0]['payment_evidence'][0]
    assert evidence['event_id']==e['id'] and evidence['source_time_s']==5
    assert evidence['basis']=='observed_payment_action'
    assert evidence['amount'] is None and evidence['amount_basis'] is None and evidence['transaction_id'] is None
    assert result['metrics']['revenue'] is None
    with pytest.raises(ValueError):store.save_daily_fact('owner',{'id':'unverified-sale','collection':'business',
        'location_id':ids['a'],'day':'2026-10-09','basis':'observations','provenance':{'event_id':e['id']},
        'transactions':1,'revenue_minor':500,'currency':'EUR'})


def test_native_matching_read_returns_saved_receipt_and_rechecks_scope(tmp_path):
    from types import SimpleNamespace
    from catalog_setup_tools import call,specs
    store,_,_,event,_,members,_=setup(tmp_path)
    e=event();store.ingest_event('owner',e)
    store.apply_product_match('owner',e['id'],{'status':'unresolved','product_id':None,'inspection_id':'saved',
        'evidence':{'source_id':'owned-one','source_time_s':2,'image_url':'/api/catalog/inspections/saved/image'}})
    agent=SimpleNamespace(children={'chat':{'owner_principal_id':'owner','dataset':'business'}},
        business_scope_host={'catalog_setup':object(),'location_intelligence':store},organization_chats=None)
    result=call(agent,'chat','business','get_product_match',{'event_id':e['id']},{})
    assert result['match']['inspection_id']=='saved' and result['status']=='unresolved'
    assert 'get_product_match' in {s['name'] for s in specs(False)}
    members.pop('owner')
    with pytest.raises(ValueError):call(agent,'chat','business','get_product_match',{'event_id':e['id']},{})


def test_matching_read_obeys_the_active_chat_source_selection(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from catalog_setup_tools import call
    from execution_policy import RETRIEVAL_VIEW
    import monitoring_tools
    store,_,_,event,_,_,_=setup(tmp_path)
    e=event();store.ingest_event('owner',e)
    agent=SimpleNamespace(children={'chat':{'owner_principal_id':'owner','dataset':'business'}},
        business_scope_host={'catalog_setup':object(),'location_intelligence':store},organization_chats=None)
    def deny(*args):raise ValueError('The current chat source selection excludes this recording.')
    monkeypatch.setattr(monitoring_tools,'scoped',lambda *args,**kwargs:SimpleNamespace(get_event=deny))
    monkeypatch.setattr(store,'product_match',lambda *args:pytest.fail('Excluded event must never expose its saved match'))
    token=RETRIEVAL_VIEW.set(object())
    try:
        with pytest.raises(ValueError,match='source selection'):call(agent,'chat','business','get_product_match',{'event_id':e['id']},{})
    finally:RETRIEVAL_VIEW.reset(token)


def test_publisher_reference_projection_remains_out_of_primary_metrics_and_products(tmp_path):
    store,_,monitor,event,ids,_,_=setup(tmp_path)
    normal=event();store.ingest_event('owner',normal)
    fields=['profile_id','profile_revision','event_key','type','description','source','track_id','object_ref','object_description']
    reference=monitor.commit_event('owner',{**{k:normal[k] for k in fields},'event_key':'publisher-reference',
        'track_id':'P9','object_ref':'reference-item'},origin='dataset_reference')
    store.ingest_event('owner',reference)
    assert len(store._rows('owner','event'))==2
    primary=store.snapshot('owner')['locations'][0]
    assert primary['metrics']['observed_interactions']==1
    assert primary['metrics']['observed_tracks']==1
    assert len(primary['products'])==1 and len(primary['sessions'])==1
    assert primary['products'][0]['events'][0]['id']==normal['id']
    assert len(store.products('owner','business',ids['a']))==1


def test_tracker_visits_exist_without_model_events_and_keep_gaps_and_recording_identity(tmp_path):
    store,scope,monitor,_,ids,_,save=setup(tmp_path)
    tracks={'fps':8,'identity_scope':'recording_and_camera_local','tracker':'NvDCF',
        'frames':[{'video_time_s':t,'tracks':[{'track_id':1}]} for t in [1,1.125,8]]}
    first=store.ingest_tracks('owner','owned-one',tracks,tracks_sha256='b'*64)
    second=store.ingest_tracks('owner','owned-wide',tracks,tracks_sha256='c'*64)
    assert first==store.ingest_tracks('owner','owned-one',tracks,tracks_sha256='b'*64)
    assert first[0]['id']!=second[0]['id']
    assert first[0]['observed_intervals']==[{'start_s':1,'end_s':1.125},{'start_s':8,'end_s':8}]
    result=store.snapshot('owner')['locations'][0]
    assert result['metrics']['observed_tracks']==2 and result['metrics']['observed_interactions']==0
    assert result['products']==[] and len(result['sessions'])==2
    assert all(s['events']==[] and s['first_observation_semantics']=='first_observed_in_recording' for s in result['sessions'])
    assert result['coverage']['recording_count']==2
    assert result['metric_sources']['observed_tracks']['basis']=='recording_local_tracker_observations'
    save('cameras',{'id':ids['cam'],'name':'Kitchen camera','location_id':ids['a'],'room_ids':[ids['room']],'source_ids':[]})
    with pytest.raises(ValueError):store.ingest_tracks('owner','owned-one',tracks,tracks_sha256='b'*64)
    assert len(store.snapshot('owner')['locations'][0]['sessions'])<2
