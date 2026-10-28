"""Artwork survives reload and obeys source, profile and current member authority."""
import hashlib
import io
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from PIL import Image, PngImagePlugin
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from retail_business import RetailBusinessStore as BusinessStore
from chat_files import ChatFiles
import profile_covers as covers
from profile_cover_routes import register_profile_cover_routes


class Index:
    clips = [{'dataset': 'cafe'}, {'dataset': 'shop'}]


def raster(color='blue', fmt='PNG'):
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo(); metadata.add_text('Private note', 'original private metadata')
    Image.new('RGB',(400,200),color).save(output,format=fmt,pnginfo=metadata)
    return output.getvalue()


@pytest.fixture
def fixture(tmp_path):
    roles = {'alice':'owner','bob':'operator','eve':'observer'}
    def authorize(actor, *, target, target_id, collection, write):
        return (actor in roles and collection in {'cafe','shop'}
            and (target != 'member' or target_id in roles)
            and (not write or roles[actor] == 'owner' or (target == 'member' and target_id == actor)))
    store = covers.ProfileCovers(tmp_path, authorize=authorize)
    business = BusinessStore(tmp_path, Index())
    business.update('cafe',0,{'name':'Café','location':'One square'})
    return SimpleNamespace(store=store,business=business,files=ChatFiles(tmp_path),roles=roles,authorize=authorize)


def artifact(f, raw=None, *, owner='alice', chat='chat', collection='cafe', name='cover.png'):
    raw = raster() if raw is None else raw
    digest=hashlib.sha256(raw).hexdigest()
    return f.files.import_native(io.BytesIO(raw),name=name,size_bytes=len(raw),sha256=digest,
        publication_key=hashlib.sha256((digest+owner+chat+collection+name).encode()).hexdigest(),provenance={'fixture':True},
        owner_principal_id=owner,chat_id=chat,dataset=collection)


def publish(f, file=None, **kwargs):
    file = artifact(f) if file is None else file
    return f.store.publish(f.files,f.business,**{**dict(file_id=file['id'],target='business',owner='alice',
        actor_id='alice',chat_id='chat',collection='cafe',expected_profile_revision=1,current=lambda:True,created=[]),**kwargs})


def test_business_artwork_persists_and_shares_only_display_raster(fixture):
    f=fixture; file=artifact(f); created=[]
    result=publish(f,file,created=created)
    fresh=covers.ProfileCovers(f.store.state,authorize=f.authorize)
    assert fresh.get(actor_id='bob',target='business',target_id='cafe',collection='cafe') == result
    metadata,body=fresh.download(result['id'],actor_id='eve',collection='cafe')
    assert metadata['profile_revision']==1 and created==[result['id']]
    assert not {'owner','actor_id','chat_id','source_file_id','source_sha256'} & metadata.keys()
    with Image.open(io.BytesIO(body)) as image:
        assert image.size==(400,200) and image.getpixel((10,10))==(0,0,255)
        assert image.info=={}
    original=f.files.download(file['id'],owner_principal_id='alice',chat_id='chat',dataset='cafe')[1]
    assert b'original private metadata' in original and b'original private metadata' not in body
    assert (fresh.path.stat().st_mode & 0o777)==0o600


def test_each_member_has_own_cover_and_current_permissions(fixture):
    f=fixture
    first=publish(f,target='member')
    second=publish(f,artifact(f,raster('red'),owner='bob'),target='member',owner='bob',actor_id='bob')
    assert first['target_id']=='alice' and second['target_id']=='bob'
    assert f.store.get(actor_id='alice',target='member',target_id='bob',collection='cafe')==second
    assert f.store.get(actor_id='alice',target='member',target_id='alice',collection='shop')==first
    f.roles.pop('bob')
    with pytest.raises(covers.CoverError):f.store.download(second['id'],actor_id='bob',collection='cafe')
    with pytest.raises(covers.CoverError):f.store.download(second['id'],actor_id='alice',collection='cafe')


@pytest.mark.parametrize('kwargs',[{'owner':'bob'},{'chat_id':'another'},{'collection':'shop'},
    {'actor_id':'bob'},{'expected_profile_revision':0},{'expected_profile_revision':True},
    {'current':lambda:False},{'target':'someone-else'}])
def test_source_scope_admin_revision_and_turn_guard(fixture,kwargs):
    file=artifact(fixture)
    with pytest.raises(ValueError):publish(fixture,file,**kwargs)
    assert fixture.store.get(actor_id='alice',target='business',target_id='cafe',collection='cafe') is None


def test_default_deny_and_cross_company_stores(fixture,tmp_path):
    f=fixture; file=artifact(f)
    f.store.authorize=lambda *args,**kwargs:False
    with pytest.raises(covers.CoverError):publish(f,file)
    f.store.authorize=f.authorize
    other=tmp_path/'other';other.mkdir()
    store=covers.ProfileCovers(other,authorize=f.authorize)
    with pytest.raises(covers.CoverError):store.publish(f.files,f.business,file_id=file['id'],target='business',owner='alice',actor_id='alice',chat_id='chat',collection='cafe',expected_profile_revision=1,current=lambda:True,created=[])
    with pytest.raises(covers.CoverError):covers.ProfileCovers(tmp_path).get(actor_id='alice',target='business',target_id='cafe',collection='cafe')


def test_retries_latest_and_interrupted_cleanup(fixture):
    f=fixture; original=artifact(f); first=publish(f,original); second=publish(f,artifact(f,raster('red')))
    assert publish(f,original)==first
    assert f.store.get(actor_id='alice',target='business',target_id='cafe',collection='cafe')==second
    f.store.discard([second['id']],owner='bob',chat_id='chat',collection='cafe')
    assert f.store.get(actor_id='alice',target='business',target_id='cafe',collection='cafe')==second
    f.store.discard([second['id']],owner='alice',chat_id='chat',collection='cafe')
    assert f.store.get(actor_id='alice',target='business',target_id='cafe',collection='cafe')==first
    calls=iter([True,True,False])
    with pytest.raises(covers.CoverError):publish(f,artifact(f,raster('green')),current=lambda:next(calls))
    assert f.store.get(actor_id='alice',target='business',target_id='cafe',collection='cafe')==first


def test_mid_publication_revocation_rolls_back(fixture):
    f=fixture; file=artifact(f); calls=[]
    def authorize(*args,**kwargs):
        calls.append(1)
        return len(calls)<3
    f.store.authorize=authorize
    with pytest.raises(covers.CoverError):publish(f,file)
    f.store.authorize=f.authorize
    assert f.store.get(actor_id='alice',target='business',target_id='cafe',collection='cafe') is None


def test_revision_changes_require_refresh(fixture):
    f=fixture; old=publish(f)
    f.business.update('cafe',1,{'name':'New café'})
    with pytest.raises(covers.CoverError):publish(f)
    new=publish(f,expected_profile_revision=2)
    assert old['id']!=new['id'] and new['profile_revision']==2


@pytest.mark.parametrize('body',[b'\x89PNG\r\n\x1a\n<truncated>',b'<svg><script>active()</script></svg>',b'x'*(covers.MAX_SOURCE_BYTES+1)])
def test_malformed_active_and_oversize_images_are_rejected(body):
    with pytest.raises(covers.CoverError):covers.image_bytes(body)


def test_image_formats_animation_dimensions_and_quota(fixture,monkeypatch):
    for fmt in ['PNG','JPEG','WEBP']:
        body,w,h=covers.image_bytes(raster(fmt=fmt));assert (w,h)==(400,200) and body.startswith(b'\x89PNG')
    animated=io.BytesIO();frames=[Image.new('RGB',(50,50),color)for color in ['red','blue']]
    frames[0].save(animated,format='PNG',save_all=True,append_images=frames[1:])
    with pytest.raises(covers.CoverError):covers.image_bytes(animated.getvalue())
    wide=io.BytesIO();Image.new('RGB',(8193,1)).save(wide,format='PNG')
    with pytest.raises(covers.CoverError):covers.image_bytes(wide.getvalue())
    monkeypatch.setattr(covers,'MAX_COVERS',0)
    with pytest.raises(covers.CoverError):publish(fixture)


def test_private_route_membership_errors_and_original_file_acls(fixture):
    f=fixture; published=publish(f)
    app=FastAPI(); current={'actor':'bob','authenticated':True,'collection':'cafe'}
    @app.middleware('http')
    async def session(request,call_next):
        request.state.session={'authenticated':current['authenticated']}
        return await call_next(request)
    register_profile_cover_routes(app,context_for_request=lambda request,key:(f.store,current['actor'],current['collection']))
    client=TestClient(app)
    response=client.get(published['url']);assert response.status_code==200
    assert response.headers['content-type']=='image/png'
    assert response.headers['cache-control']=='private, no-store'
    assert response.headers['x-content-type-options']=='nosniff'
    current['collection']='shop'; assert client.get(published['url']).status_code==404
    current['collection']='cafe';f.roles.pop('bob');assert client.get(published['url']).status_code==404
    current['authenticated']=False;assert client.get(published['url']).status_code==401


def test_store_integrity_and_immutability(fixture):
    result=publish(fixture)
    with sqlite3.connect(fixture.store.path) as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE covers SET target_id=? WHERE id=?',('bob',result['id']))
    with pytest.raises(covers.CoverError):fixture.store.download('cover-'+'a'*32,actor_id='alice',collection='cafe')
