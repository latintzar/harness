"""Real store receipts and reducer tests, with explicit synthetic evidence fixtures."""
from datetime import datetime, timezone
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from visit_basket import VisitBasket, ItemEvidence
from test_location_intelligence import setup

NOW = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)


def fixture(tmp_path):
    locations, scope, monitor, _, ids, members, save = setup(tmp_path)
    vocab = ['pick_up','place_in_basket','put_back','put_down','hand_over','touch','examine_item','carry','handed_cash']
    profile = monitor.save_profile('owner', {'name':'Basket observations','dataset':'business',
        'vocabulary':[{'type':kind,'description':kind} for kind in vocab]}, datasets=['business'])
    basket = VisitBasket(locations, verifier=lambda owner, proof, event: proof['proof_id'].startswith('review-fixture-'))
    def event(kind='pick_up', at=1, key=None, person='P1', ref='w0:item1', source='owned-one', origin='pipeline_observation'):
        key = key or f'{kind}-{at}-{person}-{ref}'
        saved = monitor.commit_event('owner', {'profile_id':profile['id'],'profile_revision':1,
            'event_key':key,'type':kind,'description':'Fixture '+kind,
            'source':{'clip_id':source,'start_s':at-.1,'end_s':at},'track_id':person,
            'object_ref':ref,'object_description':'Fixture carton'}, origin=origin)
        return locations.ingest_event('owner',saved)
    def admit(e, instance='physical-carton', quantity=1, recipient=None):
        return basket.admit('owner', {'event_id':e['id'],'instance_ref':instance,'quantity':quantity,
            'quantity_basis':'single_sale_unit' if quantity==1 else 'counted_sale_units',
            'identity_basis':'human_review','proof_id':'review-fixture-'+e['id'],
            'proof_sha256':'b'*64,'source_sha256':'a'*64,'recipient_visit_id':recipient})
    def price(e, amount='2.35', currency='EUR', confirmed=False, basis='estimated', observed='2026-10-09T12:00:00+00:00'):
        product = locations._get('owner','product',e['product_id'])
        product['price']={'amount':amount,'currency':currency,'basis':basis,'source':'Fixture source price','observed_at':observed}
        if confirmed:product.update(status='confirmed',sku='fixture-'+product['id'])
        locations.save_product('owner',product)
        if confirmed:
            locations.apply_product_match('owner',e['id'],{'product_id':product['id'],'status':'confirmed',
                'inspection_id':'fixture-review','evidence':{'source_time_s':e['source']['end_s']}})
        return product
    def at(e,time=20):return basket.at('owner',e['visit_id'],time,valuation_at=NOW)
    return basket,locations,monitor,event,admit,price,at,ids,members,save


def test_evidence_is_explicit_verified_and_immutable(tmp_path):
    basket,loc,_,event,admit,_,_,_,_,_=fixture(tmp_path)
    e=event()
    with pytest.raises(ValueError,match='verifier'):VisitBasket(loc).admit('owner',{'event_id':e['id'],
        'instance_ref':'x','quantity':1,'quantity_basis':'single_sale_unit','identity_basis':'human_review',
        'proof_id':'review','proof_sha256':'b'*64,'source_sha256':'a'*64})
    receipt=admit(e);assert admit(e)==receipt
    with pytest.raises(ValueError,match='original source receipt'):admit(e,instance='changed')
    with pytest.raises(ValueError):ItemEvidence.model_validate({**receipt,'trusted':True})


def test_touch_examine_and_unknown_quantity_do_not_create_units(tmp_path):
    _,_,_,event,_,_,at,_,_,_=fixture(tmp_path)
    e=event('touch');event('examine_item',2);pickup=event('pick_up',3)
    before=at(e,2);assert before['items']==[] and before['totals']==[]
    after=at(pickup,4);assert after['items']==[] and len(after['unresolved_observations'])==1
    assert after['coverage']['state']=='awaiting_item_evidence'


def test_cross_window_identity_dedup_return_and_rewind(tmp_path):
    _,_,_,event,admit,price,at,_,_,_=fixture(tmp_path)
    first=event(at=1,ref='w0:item1');admit(first);price(first)
    repeated=event(at=10,ref='w1:item7');admit(repeated)
    basketed=event('place_in_basket',11,ref='w1:item7');admit(basketed)
    returned=event('put_back',12,ref='w1:item7');admit(returned)
    assert at(first,.5)['items']==[]
    assert at(first,11)['totals'][0]['amount']=='2.35'
    assert at(first,11)['totals'][0]['priced_units']==1
    assert at(first,12)['items']==[] and at(first,12)['totals']==[]
    assert at(first,2)['items'][0]['state']=='held'
    assert at(first,2)['validity']['next_change_s']==10


def test_different_instances_and_currency_partial_estimates(tmp_path):
    _,_,_,event,admit,price,at,_,_,_=fixture(tmp_path)
    first=event(at=1);admit(first,'unit-a');price(first,'0.10','EUR')
    second=event(at=2,ref='w0:item2');admit(second,'unit-b',2);price(second,'0.20','EUR')
    third=event(at=3,ref='w0:item3');admit(third,'unit-c');price(third,'3.00','USD')
    fourth=event(at=4,ref='w0:item4');admit(fourth,'unit-d')
    result=at(first,5)
    assert {t['currency']:t['amount'] for t in result['totals']}=={'EUR':'0.50','USD':'3.00'}
    assert result['coverage']['unpriced_units']==1
    assert all(t['partial'] for t in result['totals'])
    assert len(result['items'])==4


def test_candidate_catalog_price_is_not_borrowed_but_own_estimate_is_allowed(tmp_path):
    _,loc,_,event,admit,price,at,ids,_,_=fixture(tmp_path)
    e=event();admit(e)
    catalog=loc.save_product('owner',{'id':'sku','collection':'business','location_id':ids['a'],
        'label':'Candidate catalog item','status':'confirmed','sku':'123456','provenance':{'source':'fixture catalog'},
        'price':{'amount':'10','currency':'EUR','basis':'catalog_sourced','source':'fixture catalog','observed_at':'2026-10-09T10:00:00+00:00'}})
    match={'product_id':catalog['id'],'status':'candidate','inspection_id':'fixture-inspect','evidence':{'source_time_s':1}}
    loc.apply_product_match('owner',e['id'],match)
    assert at(e,2)['totals']==[]
    price(e,'4.5');estimated=at(e,2)
    assert estimated['totals'][0]['amount']=='4.5'
    assert estimated['items'][0]['product_match']['status']=='candidate'
    assert estimated['items'][0]['unit_price']['basis']=='estimated'
    loc.apply_product_match('owner',e['id'],{**match,'status':'confirmed'})
    assert at(e,2)['totals'][0]['amount']=='10'
    assert at(e,2)['paid_amount'] is None


def test_handover_uncertainty_and_proved_transfer(tmp_path):
    _,_,_,event,admit,price,at,_,_,_=fixture(tmp_path)
    e=event();admit(e);price(e)
    handover=event('hand_over',2);admit(handover)
    uncertain=at(e,2);assert uncertain['items']==[] and uncertain['totals']==[]
    assert uncertain['uncertain_items'][0]['reason']=='handover_recipient_pending'
    receiver=event('touch',3,person='P2')
    transfer=event('hand_over',4);admit(transfer,recipient=receiver['visit_id'])
    assert at(e,4)['items']==[]
    received=at(receiver,4);assert len(received['items'])==1 and received['totals'][0]['amount']=='2.35'


def test_put_down_is_uncertain_and_payment_never_settles(tmp_path):
    _,_,_,event,admit,price,at,_,_,_=fixture(tmp_path)
    e=event();admit(e);price(e,confirmed=True,basis='pos_confirmed')
    payment=event('handed_cash',2)
    paid=at(e,2);assert len(paid['payment_actions'])==1 and paid['paid_amount'] is None
    assert paid['totals'][0]['basis']=='estimated_basket'
    down=event('put_down',3);admit(down)
    assert at(e,3)['uncertain_items'][0]['reason']=='item_location_pending'


def test_future_price_and_item_evidence_do_not_leak(tmp_path):
    _,loc,_,event,admit,price,at,_,_,_=fixture(tmp_path)
    e=event();admit(e);price(e,observed='2026-10-10T12:00:00+00:00')
    assert at(e,2)['totals']==[]
    assert at(e,2)['items'][0]['price_status']=='price_after_valuation_time'
    later=event(at=8,ref='later');admit(later,'later-instance');price(later)
    assert len(at(e,2)['items'])==1


def test_reference_and_wrong_camera_proofs_are_refused(tmp_path):
    basket,_,_,event,admit,_,at,_,_,_=fixture(tmp_path)
    e=event();admit(e)
    reference=event(at=2,origin='dataset_reference')
    with pytest.raises(ValueError):admit(reference)
    other=event(at=3,person='P2',source='owned-wide')
    handover=event('hand_over',4)
    with pytest.raises(ValueError,match='recipient'):admit(handover,recipient=other['visit_id'])
    assert len(at(e,5)['items'])==1


def test_revoked_source_and_foreign_owner_cannot_read_saved_basket(tmp_path):
    basket,_,_,event,admit,_,at,ids,_,save=fixture(tmp_path)
    e=event();admit(e)
    with pytest.raises(ValueError):basket.at('alice',e['visit_id'],2)
    save('cameras',{'id':ids['cam'],'name':'Camera','location_id':ids['a'],'room_ids':[ids['room']],'source_ids':[]})
    with pytest.raises(ValueError):at(e,2)


def test_opposed_simultaneous_actions_are_uncertain(tmp_path):
    _,_,_,event,admit,_,at,_,_,_=fixture(tmp_path)
    e=event(at=1);admit(e)
    returned=event('put_back',1);admit(returned)
    result=at(e,2);assert result['items']==[]
    assert result['uncertain_items'][0]['reason']=='conflicting_item_evidence'


def model_fixture(tmp_path):
    import json
    from stream_ingestion import StreamIngestion
    from stream_runtime import drain_stream
    basket, loc, monitor, _, _, price, _, ids, members, save = fixture(tmp_path)
    profile = monitor.save_profile('owner', {'name':'Stream basket', 'dataset':'business',
        'vocabulary':[{'type':k,'description':k} for k in ['pick_up','carry','put_back','touch','hand_over']]}, datasets=['business'])
    tracks = loc.ingest_tracks('owner', 'owned-one', {
        'identity_scope':'recording_and_camera_local', 'fps':1,
        'frames':[{'video_time_s':t,'tracks':[{'track_id':'0'},{'track_id':'P0'}]} for t in range(20)]}, tracks_sha256='c'*64)
    pipeline=StreamIngestion(tmp_path,monitor)
    selected={'stream_ingestion':pipeline,'location_intelligence':loc,
              'catalog_inspection':type('Inspection',(),{'process':lambda *args:{'status':'fixture_cpu_only'}})()}
    def accept(start=0,kind='pick_up',relation='first_observation',prior=None,quantity=1,person='0',ref='carton',source='owned-one'):
        context={'stream_id':'camera-stream','source_id':source,'profile_id':profile['id'],
            'profile_revision':1,'start_s':start,'end_s':start+10,'input_kind':'dataset_replay',
            'model':'fixture-Cosmos','sampling':'8fps','processing_seconds':1}
        event={'profile_id':profile['id'],'profile_revision':1,'event_key':f'observed-{start}',
            'type':kind,'description':'Synthetic fixture action','track_id':person,'object_ref':ref,
            'object_description':'Synthetic carton','source':{'clip_id':source,'start_s':start+2,'end_s':start+3}}
        claim={'event_key':event['event_key'],'object_ref':ref,'quantity':quantity,'relation':relation}
        if prior:claim['prior_event_key']=prior
        raw=json.dumps({'observations':[event],'item_claims':[claim]})
        receipt=pipeline.accept_model_response('owner',context,raw)
        for _ in range(4):
            processed=drain_stream(selected)
            assert all(r['status']=='done' for r in processed), processed
        return receipt,context,raw
    return basket,loc,monitor,pipeline,tracks,accept,price,save,ids


def test_actual_stream_admits_estimates_and_joins_exact_tracker_identity(tmp_path):
    basket,loc,monitor,pipeline,tracks,accept,price,_,_=model_fixture(tmp_path)
    first,context,raw=accept()
    e=loc._get('owner','event',first['event_ids'][0]); price(e,'2.50')
    second,_,_=accept(10,'carry','same_item','observed-0',ref='new-window-ref')
    visit=next(t for t in tracks if t['person_id']=='0')
    assert e['visit_id']==visit['id']
    assert monitor.get_event('owner',second['event_ids'][0])['visit_id']==visit['id']
    result=basket.at('owner',visit['id'],14,valuation_at=NOW)
    assert len(result['items'])==1 and result['items'][0]['quantity']==1
    assert result['items'][0]['identity_basis']=='vision_estimate'
    assert result['items'][0]['quantity_basis']=='vision_estimated_sale_units'
    assert result['totals'][0]['amount']=='2.50' and result['paid_amount'] is None
    assert result['coverage']['estimated_units']==1
    assert basket.at('owner',next(t for t in tracks if t['person_id']=='P0')['id'],14)['items']==[]
    assert basket.at('owner',visit['id'],2)['items']==[]
    assert len(basket.at('owner',visit['id'],4)['items'][0]['evidence_event_ids'])==1
    assert pipeline.accept_model_response('owner',context,raw)==first
    assert len(loc._rows('owner','basket_evidence'))==2
    proof=next(r for r in loc._rows('owner','basket_evidence') if r['event_id']==first['event_ids'][0])
    assert proof['profile_id']==context['profile_id'] and proof['window_end_s']==10
    assert len(proof['proof_sha256'])==64 and proof['source_sha256']=='a'*64


def test_unlinked_repeated_model_claims_stay_uncertain_instead_of_double_counting(tmp_path):
    basket,loc,_,_,tracks,accept,price,_,_=model_fixture(tmp_path)
    first,_,_=accept(); price(loc._get('owner','event',first['event_ids'][0]))
    accept(10,ref='unlinked-new-reference')
    result=basket.at('owner',tracks[0]['id'],14,valuation_at=NOW)
    assert result['items']==[] and result['totals']==[]
    assert len(result['uncertain_items'])==2
    assert all(i['reason']=='item_association_pending' for i in result['uncertain_items'])


def test_model_estimates_need_raw_response_and_cannot_claim_trust(tmp_path):
    import json
    from pydantic import ValidationError
    _,_,_,pipeline,_,accept,_,_,_=model_fixture(tmp_path)
    first,context,raw=accept()
    response=json.loads(raw)
    with pytest.raises(ValueError,match='original model response'):pipeline.accept('owner',{**context,**response})
    response['item_claims'][0]['identity_basis']='human_review'
    with pytest.raises(ValidationError):pipeline.accept_model_response('owner',context,json.dumps(response))
    response=json.loads(raw);del response['item_claims'][0]['quantity']
    with pytest.raises(ValidationError):pipeline.accept_model_response('owner',context,json.dumps(response))
    with pytest.raises(ValueError,match='original model response'):pipeline.accept_model_response('owner',context,raw+' ')


def test_model_touch_never_admits_units_and_unknown_handover_remains_uncertain(tmp_path):
    basket,loc,_,_,tracks,accept,_,_,_=model_fixture(tmp_path)
    accept(kind='touch')
    assert loc._rows('owner','basket_evidence')==[]
    accept(10,kind='hand_over')
    result=basket.at('owner',tracks[0]['id'],14)
    assert result['items']==[] and result['totals']==[]
    assert result['uncertain_items'][0]['reason']=='handover_recipient_pending'


def test_http_selected_tracker_visit_auth_asof_and_revocation(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    from business_scope import ScopeActor
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        url='/api/control-room/visit-basket'
        query={'source_id':'basket-camera','track_id':'0','at_s':4}
        assert client.get(url,params=query).status_code==401
        client.post('/api/session',json={'code':app.state.access.issue('demo',owner=True,principal_id='peter')},headers=HEADERS)
        ws=app.state.workspaces['demo'];scope=ws['business_scope'];monitor=ws['monitoring'];loc=ws['location_intelligence']
        actor=ScopeActor(scope.company_id,'peter')
        def save(kind,definition):return scope.mutate(actor,'cafe',kind,definition,scope.effective(actor,'cafe')['revision'])
        location=save('locations',{'name':'Fixture shop'})['locations'][0]
        room=save('rooms',{'name':'Fixture aisle','location_id':location['id'],'function_ids':[]})['rooms'][0]
        monitor.register_source('peter',{'id':'basket-camera','video_id':'basket-camera','dataset':'cafe','end':20,'sha256':'a'*64,'media_url':'/api/media/basket-camera'})
        camera=save('cameras',{'name':'Fixture view','location_id':location['id'],'room_ids':[room['id']],'source_ids':['basket-camera']})['cameras'][0]
        loc.configure_location('peter',{'collection':'cafe','location_id':location['id']})
        visits=loc.ingest_tracks('peter','basket-camera',{'identity_scope':'recording_and_camera_local','fps':1,'frames':[{'video_time_s':t,'tracks':[{'track_id':'0'}]} for t in range(20)]},tracks_sha256='c'*64)
        response=client.get(url,params=query)
        assert response.status_code==200,response.text
        assert response.json()['visit_id']==visits[0]['id']
        assert response.json()['as_of_s']==4 and response.json()['totals']==[]
        assert response.headers['cache-control']=='private, no-store'
        assert client.get(url,params={**query,'track_id':'P0'}).status_code==404
        assert client.get(url,params={**query,'at_s':float('nan')}).status_code==404
        save('cameras',{'id':camera['id'],'name':'Fixture view','location_id':location['id'],'room_ids':[room['id']],'source_ids':[]})
        assert client.get(url,params=query).status_code==404
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='other')},headers=HEADERS)
        assert client.get(url,params=query).status_code==404


def test_host_cli_preserves_real_response_and_out_of_order_model_relations(tmp_path,capsys):
    import json
    from import_stream_response import main
    from stream_runtime import drain_stream
    basket,loc,monitor,pipeline,tracks,_,_,_,_=model_fixture(tmp_path)
    profile=next(p for p in monitor.list_profiles('owner','business') if p['name']=='Stream basket')
    def event(key,kind,t):return {'profile_id':profile['id'],'profile_revision':1,'event_key':key,'type':kind,'description':'Synthetic fixture','track_id':'0','object_ref':'item','source':{'clip_id':'owned-one','start_s':t-1,'end_s':t}}
    context={'stream_id':'camera-stream','source_id':'owned-one','profile_id':profile['id'],'profile_revision':1,'start_s':0,'end_s':10,'input_kind':'dataset_replay','model':'fixture-model','sampling':'8fps','processing_seconds':1}
    raw=json.dumps({'observations':[event('b','put_back',5),event('a','pick_up',3)],'item_claims':[
        {'event_key':'b','object_ref':'item','quantity':1,'relation':'same_item','prior_event_key':'a'},
        {'event_key':'a','object_ref':'item','quantity':1,'relation':'first_observation'}]})
    context_path=tmp_path/'context.json';context_path.write_text(json.dumps(context))
    response_path=tmp_path/'response.json';response_path.write_text(raw)
    receipt=main(['--state',str(tmp_path),'--owner','owner','--context',str(context_path),'--response',str(response_path)])
    assert json.loads(capsys.readouterr().out)==receipt
    selected={'stream_ingestion':pipeline,'location_intelligence':loc,'catalog_inspection':type('I',(),{'process':lambda *args:{}})()}
    for _ in range(4):assert all(r['status']=='done' for r in drain_stream(selected))
    assert basket.at('owner',tracks[0]['id'],6)['items']==[]
    assert len(basket.at('owner',tracks[0]['id'],4)['items'])==1
    with pipeline.connect() as db:assert db.execute('SELECT raw FROM stream_model_outputs').fetchone()[0]==raw


def test_window_source_alias_retains_recording_identity_and_original_clock(tmp_path):
    basket,loc,monitor,_,tracks,accept,price,_,_=model_fixture(tmp_path)
    first,_,_=accept();price(loc._get('owner','event',first['event_ids'][0]))
    monitor.register_source('owner',{'id':'owned-one-window10','video_id':'owned-one','dataset':'business','start':10,'end':20,'sha256':'a'*64,'media_url':'/api/media/owned-one'})
    later,_,_=accept(10,'put_back','same_item','observed-0',source='owned-one-window10')
    assert monitor.get_event('owner',later['event_ids'][0])['visit_id']==tracks[0]['id']
    assert len(basket.at('owner',tracks[0]['id'],12)['items'])==1
    assert basket.at('owner',tracks[0]['id'],13)['items']==[]
    assert len(basket.at('owner',tracks[0]['id'],4)['items'])==1
