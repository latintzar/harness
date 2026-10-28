"""Catalog onboarding authority, upload-only capability and source provenance."""
import io
import sys
from pathlib import Path
import pytest
from PIL import Image
sys.path.insert(0,str(Path(__file__).parents[1]))
from catalog_setup import CatalogSetup,MAX_BYTES
from test_location_intelligence import setup


def photo():
    output=io.BytesIO();image=Image.new('RGB',(160,120),'green');exif=Image.Exif();exif[0x010F]='Private camera owner';image.save(output,format='JPEG',exif=exif)
    return output.getvalue()


def test_catalog_import_keeps_confirmed_skus_and_validates_batch_before_writing(tmp_path):
    locations,_,_,_,ids,_,_=setup(tmp_path);store=CatalogSetup(tmp_path,locations,'demo')
    args=('owner','business',ids['a']);rows=[{'sku':'milk-1','name':'Milk','barcode':'123456789'}]
    receipt=store.import_products(*args,rows,source_name='Till export',source_kind='pos_export')
    assert store.import_products(*args,rows,source_name='Till export',source_kind='pos_export')==receipt
    product=locations.products(*args)[0]
    assert product['status']=='confirmed' and product['sku']=='milk-1' and product['attributes']['barcode']=='123456789'
    with pytest.raises(ValueError):store.import_products(*args,[{'sku':'bread','name':'Bread'},{'sku':'bad'}],source_name='Broken')
    assert len(locations.products(*args))==1
    assert store.overview(*args)['methods'][0]['id']=='catalog_import'


def test_phone_token_is_upload_only_expiring_bounded_and_strips_exif(tmp_path):
    locations,_,_,_,ids,members,_=setup(tmp_path);now=[1000]
    store=CatalogSetup(tmp_path,locations,'demo',clock=lambda:now[0])
    result=store.create_handoff('owner','business',ids['a'],origin='https://video-demo.soyrocoto.com')
    token=result['upload_url'].split('#')[1]
    assert result['qr_svg'].startswith('<svg') and token not in store.path.read_bytes().decode('latin1')
    saved=store.upload(token,photo());assert saved['remaining']==7
    pending=store.list_pending_photos('owner');assert len(pending)==1
    value,path=store.photo('owner',pending[0]['id'])
    with Image.open(path) as image:assert not image.getexif() and image.size==(160,120)
    assert value['provenance']['kind']=='shelf_photo_upload'
    with pytest.raises(ValueError):store.photo('alice',value['id'])
    with pytest.raises(ValueError):store.upload(token,b'x'*(MAX_BYTES+1))
    now[0]=1901
    with pytest.raises(ValueError):store.upload(token,photo())
    now[0]=1001;members.pop('owner')
    with pytest.raises(ValueError):store.upload(token,photo())


def test_qr_decodes_the_exact_private_fragment_link():
    import re
    import numpy as np
    import cv2
    from catalog_setup import qr_svg
    from PIL import ImageDraw
    url='https://video-demo.soyrocoto.com/catalog-upload/retail-demo#'+'a'*64
    svg=qr_svg(url);size=int(re.search(r'viewBox="0 0 (\d+)',svg).group(1));image=Image.new('RGB',(size*8,size*8),'white');draw=ImageDraw.Draw(image)
    for x,y in re.findall(r'M(\d+) (\d+)h1v1h-1z',svg):
        x,y=int(x)*8,int(y)*8;draw.rectangle((x,y,x+7,y+7),fill='black')
    value,_,_=cv2.QRCodeDetector().detectAndDecode(np.asarray(image))
    assert value==url


def test_http_mobile_handoff_has_upload_authority_without_catalog_read(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    from business_scope import ScopeActor
    monkeypatch.setenv('ROCOTO_VIDEO_PUBLIC_ORIGIN','https://video-demo.soyrocoto.com')
    app=make_app(tmp_path,monkeypatch)
    phone=TestClient(app,base_url='https://video-demo.soyrocoto.com')
    with TestClient(app,base_url='https://video-demo.soyrocoto.com') as desktop:
        desktop.post('/api/session',json={'code':app.state.access.issue('demo',owner=True,principal_id='peter')},headers=HEADERS)
        selected=app.state.workspaces['demo'];scope=selected['business_scope'];actor=ScopeActor(scope.company_id,'peter')
        location=scope.mutate(actor,'cafe','locations',{'name':'Shop'},0)['locations'][0]
        selected['location_intelligence'].configure_location('peter',{'collection':'cafe','location_id':location['id']})
        context={'collection':'cafe','location_id':location['id']}
        handoff=desktop.post('/api/catalog/photo-handoff',json=context,headers=HEADERS)
        assert handoff.status_code==200,handoff.text
        token=handoff.json()['upload_url'].split('#')[1]
        assert phone.get('/catalog-upload/demo').status_code==200
        assert phone.get('/api/catalog/setup',params=context).status_code==401
        sent=phone.post('/api/catalog/mobile-upload/demo',files={'file':('shelf.jpg',photo(),'image/jpeg')},headers={**HEADERS,'Authorization':'Bearer '+token})
        assert sent.status_code==200,sent.text
        metadata=desktop.get('/api/catalog/setup',params=context).json()['photos'][0]
        assert desktop.get(metadata['image_url']).status_code==200
        assert phone.get(metadata['image_url'],headers={'Authorization':'Bearer '+token}).status_code==401
        bad=phone.post('/api/catalog/mobile-upload/other',files={'file':('shelf.jpg',photo(),'image/jpeg')},headers={**HEADERS,'Authorization':'Bearer '+token})
        assert bad.status_code==403
        huge=phone.post('/api/catalog/mobile-upload/demo',content=b'x'*(MAX_BYTES+65537),headers=HEADERS)
        assert huge.status_code==413
        assert desktop.post('/api/catalog/import',json={**context,'source_name':'Products','rows':[{'sku':'123','name':'Milk'}]},headers=HEADERS).status_code==200


def test_native_catalog_tools_use_location_grants_and_reject_child_writes(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from catalog_setup_tools import call,specs
    locations,_,_,_,ids,_,_=setup(tmp_path)
    store=CatalogSetup(tmp_path,locations,'demo')
    agent=SimpleNamespace(business_scope_host={'catalog_setup':store},children={
        'parent':{'owner_principal_id':'owner','dataset':'business'},
        'child':{'owner_principal_id':'owner','dataset':'business','parent_id':'parent'}},organization_chats=None)
    args={'location_id':ids['a']}
    result=call(agent,'parent','business','get_catalog_setup',args,{})
    assert result['product_count']==0
    run={'cancel':asyncio.Event()}
    imported=call(agent,'parent','business','import_catalog_products',{**args,'source_name':'Customer list','rows':[{'sku':'one','name':'Tea','price':'3.20','currency':'EUR'}]},run)
    assert imported['count']==1
    assert locations.products('owner','business',ids['a'])[0]['price']['amount']=='3.20'
    with pytest.raises(ValueError):call(agent,'child','business','import_catalog_products',{**args,'source_name':'User','rows':[{'sku':'two','name':'Coffee'}]},run)
    assert {s['name'] for s in specs(False)}=={'get_catalog_setup','get_product_match'}


def test_imported_prices_populate_shared_sourced_contract_without_creating_sales(tmp_path):
    locations,_,_,_,ids,_,_=setup(tmp_path)
    clock=[1791565200]
    store=CatalogSetup(tmp_path,locations,'demo',clock=lambda:clock[0])
    args=('owner','business',ids['a'])
    rows=[{'sku':'milk','name':'Milk','price':'2.4500','currency':'EUR'}]
    first=store.import_products(*args,rows,source_name='Till catalog',source_kind='pos_export')
    product=locations.products(*args)[0]
    assert product['price']['amount']=='2.4500'
    assert product['price']['basis']=='catalog_sourced'
    assert product['price']['currency']=='EUR'
    assert product['price']['source']=='Till catalog'
    observed=product['price']['observed_at']
    clock[0]+=300
    assert store.import_products(*args,rows,source_name='Till catalog',source_kind='pos_export')==first
    assert locations.products(*args)[0]['price']['observed_at']==observed
    assert locations._rows('owner','daily_fact')==[]
    for price,currency in [('2.45',None),('-2','EUR'),('nan','EUR'),('2.45231','EUR')]:
        with pytest.raises(ValueError):
            store.import_products(*args,[{'sku':'bad','name':'Invalid','price':price,'currency':currency}],source_name='Invalid catalog')
    assert len(locations.products(*args))==1
