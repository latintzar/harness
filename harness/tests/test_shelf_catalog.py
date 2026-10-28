"""Source-derived shelf catalogs: explicit consent, authority, receipts and recovery."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import pytest
from PIL import Image
sys.path.insert(0,str(Path(__file__).parents[1]))
from catalog_setup import CatalogSetup
from shelf_catalog import ShelfCatalog
from catalog_image_model import ImageFundingUnavailable
from test_location_intelligence import setup


ANSWER={'products':[{'label':'Green carton','description':'A green rectangular carton with a white label.',
    'bbox':[.25,.2,.75,.8],'visible_text':'TEA','barcode_candidates':['1234567890123'],
    'brand':None,'size':None,'uncertainty':'The small label is partly unreadable.'}],
    'notes':'One representative facing.','model':'test-image-model','usage_receipt_sha256':'a'*64}


class Provider:
    def __init__(self):self.calls=[]
    def extract_shelf(self,**kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(ANSWER)


def fixture(tmp_path,*,consent=True,enabled=True,funded=True):
    locations,scope,monitor,event,ids,members,save=setup(tmp_path)
    uploads=CatalogSetup(tmp_path,locations,'demo')
    handoff=uploads.create_handoff('owner','business',ids['a'],origin='https://video-demo.soyrocoto.com')
    image=io.BytesIO();Image.new('RGB',(200,100),'green').save(image,format='PNG')
    uploads.upload(handoff['upload_url'].split('#')[1],image.getvalue(),extraction_consent=consent)
    photo=uploads.list_pending_photos('owner')[0];provider=Provider()
    worker=ShelfCatalog(tmp_path,uploads,locations,provider=provider,enabled=enabled,funding_ready=lambda:funded)
    return worker,uploads,locations,photo,provider,members,ids


def test_extraction_persists_provisional_products_original_crops_and_one_receipt(tmp_path):
    worker,uploads,locations,photo,provider,members,ids=fixture(tmp_path)
    receipt=worker.process('owner',photo['id'])
    product=locations.products('owner','business',ids['a'])[0]
    assert product['status']=='provisional' and product['sku'] is None
    assert product['attributes']['barcode_candidates']==['1234567890123']
    assert 'barcode' not in product['attributes']
    assert product['image']['kind']=='shelf_photo_crop'
    assert product['image']['provenance']['source_sha256']==photo['sha256']
    assert product['image']['provenance']['bbox']==[.25,.2,.75,.8]
    key=provider.calls[0]['request_id']
    with Image.open(worker.image('owner',key,0)) as image:
        assert image.size==(100,60) and not image.getexif()
    assert uploads.photo('owner',photo['id'])[0]['status']=='catalog_extracted'
    restarted=ShelfCatalog(tmp_path,uploads,locations,provider=provider,enabled=True,funding_ready=lambda:True)
    assert restarted.process('owner',photo['id'])==receipt and len(provider.calls)==1
    with pytest.raises(ValueError):restarted.image('alice',key,0)
    members.pop('owner')
    with pytest.raises(ValueError):restarted.image('owner',key,0)


@pytest.mark.parametrize('consent,enabled,funded,status',[
    (False,True,True,'awaiting_upload_consent'),(True,False,True,'awaiting_commissioning'),
    (True,True,False,'awaiting_funding')])
def test_uncommissioned_unfunded_or_unconsented_upload_stays_pending(tmp_path,consent,enabled,funded,status):
    worker,uploads,locations,photo,provider,_,_=fixture(tmp_path,consent=consent,enabled=enabled,funded=funded)
    assert worker.process('owner',photo['id'])['status']==status
    assert provider.calls==[] and len(uploads.list_pending_photos('owner'))==1
    assert uploads.photo('owner',photo['id'])[0]['status']==status


def test_insufficient_reservation_is_retryable_without_remote_attempt(tmp_path):
    worker,uploads,_,photo,provider,_,_=fixture(tmp_path)
    def unfunded(**kwargs):raise ImageFundingUnavailable('Reserve a funded request.')
    provider.extract_shelf=unfunded
    assert worker.process('owner',photo['id'])['status']=='awaiting_funding'
    with worker.connect() as db:assert db.execute('SELECT status FROM jobs').fetchone()[0]=='pending'
    provider.extract_shelf=lambda **kwargs:copy.deepcopy(ANSWER)
    assert worker.process('owner',photo['id'])['status']=='catalog_extracted'


def test_saved_output_repairs_partial_projection_without_second_provider_call(tmp_path,monkeypatch):
    worker,uploads,locations,photo,provider,_,_=fixture(tmp_path)
    original=locations.save_product
    monkeypatch.setattr(locations,'save_product',lambda *a,**k:(_ for _ in ()).throw(OSError('disk fixture')))
    with pytest.raises(OSError):worker.process('owner',photo['id'])
    with worker.connect() as db:assert db.execute('SELECT status FROM jobs').fetchone()[0]=='projecting'
    monkeypatch.setattr(locations,'save_product',original)
    assert worker.process('owner',photo['id'])['status']=='catalog_extracted'
    assert len(provider.calls)==1


def test_invalid_geometry_retains_review_job_and_never_creates_product(tmp_path):
    worker,uploads,locations,photo,provider,_,ids=fixture(tmp_path)
    bad=copy.deepcopy(ANSWER);bad['products'][0]['bbox']=[.8,.2,.2,.8]
    provider.extract_shelf=lambda **kwargs:bad
    with pytest.raises(ValueError):worker.process('owner',photo['id'])
    assert locations.products('owner','business',ids['a'])==[]
    assert worker.process('owner',photo['id'])['status']=='review_required'
    assert worker.drain()==[]


def test_current_grants_are_checked_before_photo_egress(tmp_path):
    worker,_,_,photo,provider,members,_=fixture(tmp_path)
    members.pop('owner')
    with pytest.raises(ValueError):worker.process('owner',photo['id'])
    assert provider.calls==[]


def test_runtime_global_key_alone_does_not_commission_shelf_processing(tmp_path,monkeypatch):
    from stream_runtime import setup_shelf_catalog
    _,uploads,locations,photo,_,_,_=fixture(tmp_path)
    monkeypatch.setenv('ROCOTO_OPENAI_API_KEY','test-key')
    monkeypatch.delenv('ROCOTO_SHELF_CATALOG_INSPECTION_ENABLED',raising=False)
    monkeypatch.delenv('ROCOTO_SHELF_CATALOG_ENVIRONMENTS',raising=False)
    selected={'state':tmp_path,'environment_id':'demo','catalog_setup':uploads,'location_intelligence':locations}
    worker=setup_shelf_catalog(selected)
    assert not worker.enabled and worker.provider is None
    assert worker.drain()[0]['status']=='awaiting_commissioning'
    monkeypatch.setenv('ROCOTO_SHELF_CATALOG_INSPECTION_ENABLED','true')
    monkeypatch.setenv('ROCOTO_SHELF_CATALOG_ENVIRONMENTS','a-different-company')
    selected.pop('shelf_catalog');worker=setup_shelf_catalog(selected)
    assert not worker.enabled and worker.provider is None


def test_background_drain_extracts_consented_photo_and_keeps_unconsented_photo_pending(tmp_path):
    worker,uploads,locations,photo,provider,_,ids=fixture(tmp_path,consent=False)
    handoff=uploads.create_handoff('owner','business',ids['a'],origin='https://video-demo.soyrocoto.com')
    data=io.BytesIO();Image.new('RGB',(200,100),'blue').save(data,format='PNG')
    uploads.upload(handoff['upload_url'].split('#')[1],data.getvalue(),extraction_consent=True)
    from stream_runtime import drain_stream
    from stream_ingestion import StreamIngestion
    selected={'state':tmp_path,'stream_ingestion':StreamIngestion(tmp_path,locations.monitoring),
        'catalog_setup':uploads,'location_intelligence':locations,'shelf_catalog':worker}
    result=drain_stream(selected)
    assert len(result)==1 and result[0]['status']=='catalog_extracted'
    assert len(provider.calls)==1
    assert uploads.list_pending_photos('owner')[0]['id']==photo['id']
    assert drain_stream(selected)==[]


def test_interrupted_attempt_requires_receipt_reconciliation_before_retry(tmp_path):
    worker,uploads,_,photo,provider,_,_=fixture(tmp_path)
    key=hashlib.sha256(json.dumps(['owner',photo['id'],photo['sha256'],'shelf-v1']).encode()).hexdigest()
    with worker.connect() as db:
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?, ?,NULL,'[]',NULL)",(key,'owner',photo['id'],key,'processing',0))
    assert worker.process('owner',photo['id'])['status']=='review_required'
    assert worker.drain()==[] and provider.calls==[]


def test_http_upload_consent_and_private_crop_survive_current_authority_checks(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    from business_scope import ScopeActor
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app,base_url='https://testserver') as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo',owner=True,principal_id='peter')},headers=HEADERS)
        selected=app.state.workspaces['demo'];scope=selected['business_scope']
        loc=scope.mutate(ScopeActor(scope.company_id,'peter'),'cafe','locations',{'name':'Shop'},0)['locations'][0]
        selected['location_intelligence'].configure_location('peter',{'collection':'cafe','location_id':loc['id']})
        handoff=client.post('/api/catalog/photo-handoff',json={'collection':'cafe','location_id':loc['id']},headers=HEADERS).json()
        token=handoff['upload_url'].split('#')[1]
        image=io.BytesIO();Image.new('RGB',(200,100),'green').save(image,format='PNG')
        saved=client.post('/api/catalog/mobile-upload/demo',headers={**HEADERS,'Authorization':'Bearer '+token},
            data={'extraction_consent':'true'},files={'file':('shelf.png',image.getvalue(),'image/png')})
        assert saved.status_code==200
        photo=selected['catalog_setup'].list_pending_photos('peter')[0]
        assert photo['extraction_consent']['granted'] is True
        provider=Provider();worker=ShelfCatalog(selected['state'],selected['catalog_setup'],selected['location_intelligence'],
            provider=provider,enabled=True,funding_ready=lambda:True)
        selected['shelf_catalog']=worker
        worker.process('peter',photo['id'])
        key=provider.calls[0]['request_id'];url='/api/catalog/shelf-crops/'+key+'/0/image'
        response=client.get(url)
        assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
        with Image.open(io.BytesIO(response.content)) as crop:assert crop.size==(100,60)
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='other')},headers=HEADERS)
        assert client.get(url).status_code==404


def test_item_runtime_requires_explicit_company_commissioning_even_with_global_key(tmp_path,monkeypatch):
    import catalog_image_model
    from stream_runtime import setup_catalog_inspection
    locations,_,monitor,_,_,_,_=setup(tmp_path)
    monkeypatch.setenv('ROCOTO_OPENAI_API_KEY','test-key')
    monkeypatch.delenv('ROCOTO_ITEM_INSPECTION_ENABLED',raising=False)
    monkeypatch.delenv('ROCOTO_ITEM_INSPECTION_ENVIRONMENTS',raising=False)
    calls=[]
    monkeypatch.setattr(catalog_image_model,'FundedItemModel',lambda *a,**k:calls.append(a))
    selected={'state':tmp_path,'environment_id':'demo','location_intelligence':locations,'monitoring':monitor}
    setup_catalog_inspection(selected)
    assert calls==[]
    monkeypatch.setenv('ROCOTO_ITEM_INSPECTION_ENABLED','true')
    monkeypatch.setenv('ROCOTO_ITEM_INSPECTION_ENVIRONMENTS','different-company')
    setup_catalog_inspection(selected)
    assert calls==[]
    monkeypatch.setenv('ROCOTO_ITEM_INSPECTION_ENVIRONMENTS','demo')
    setup_catalog_inspection(selected)
    assert len(calls)==1
