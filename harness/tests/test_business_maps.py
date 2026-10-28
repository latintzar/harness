"""Actual file, business revision, parent tool and HTTP map authority boundaries."""
import asyncio
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
import business_maps as maps
from agent_bridge import CodexVideoAgent, tool_specs
from retail_business import RetailBusinessStore as BusinessStore
from chat_files import ChatFiles
from test_server_access import FixtureIndex, fixture_app
from test_workflow_routes import login


def document():
    empty={'type':'FeatureCollection','features':[]}
    return {'kind':'business_neighborhood','schema_version':1,
        'binding':{'business_name':'Fixture café','profile_location':'One square'},
        'origin':{'name':'Fixture café','coordinates':[-3.7,40.4],'address':'One square',
            'source':'Controlled map fixture','url':'https://www.openstreetmap.org/node/1'},
        'bbox':[-3.71,40.39,-3.69,40.41], 'buildings':copy.deepcopy(empty),'roads':copy.deepcopy(empty),'green':copy.deepcopy(empty),
        'pois':[{'id':'bus','name':'Bus stop','category':'transit','coordinates':[-3.701,40.401],
            'url':'https://www.crtm.es/widgets/?lang=en#/stop/6_808','walkMinutes':2}],
        'catchments':[{'minutes':5,'geometry':{'type':'Polygon','coordinates':[
            [[-3.705,40.395],[-3.695,40.395],[-3.695,40.405],[-3.705,40.395]]]}}],
        'stats':{'totalBuildings':0,'taggedBuildings':0,'counts':{k:0 for k in maps.USES}},
        'methodology':{'source':'Controlled source','sourceTimestamp':None,'walkingMethod':'network',
            'walkingSpeedKmh':4.8,'catchmentNote':'Supplied graph result for this controlled fixture.',
            'uncertainty':['Temporary closures remain outside the supplied graph.']},
        'sources':[{'url':'https://www.openstreetmap.org/node/1','retrieved_at':'2026-10-05T01:00:00Z'}]}


def raw(value=None):return json.dumps(document() if value is None else value,ensure_ascii=False,separators=(',',':')).encode()


def setup(state):
    business=BusinessStore(state,FixtureIndex());files=ChatFiles(state);store=maps.BusinessMaps(state)
    business.update('cafe',0,{'name':'Fixture café','location':'One square'})
    return business,files,store


def file(files,value=None,*,owner='alice',chat='root',collection='cafe',native=False):
    body=raw(value)
    if native:
        digest=hashlib.sha256(body).hexdigest()
        return files.import_native(io.BytesIO(body),name='map.json',size_bytes=len(body),sha256=digest,
            publication_key=digest,provenance={'fixture':'native'},owner_principal_id=owner,chat_id=chat,dataset=collection)
    return files.create({'name':'map.json','content':body.decode()},owner_principal_id=owner,chat_id=chat,dataset=collection)


def publish(store,business,files,record,**changes):
    return store.publish(files,business,**{'file_id':record['id'],'expected_profile_revision':1,
        'owner':'alice','chat_id':'root','collection':'cafe','current':lambda:True,'created':[],**changes})


@pytest.mark.parametrize('mutate',[
    lambda d:d.update(schema_version=True),
    lambda d:d['origin'].update(coordinates=[0,90]),
    lambda d:d.update(bbox=[-180,-85,180,85]),
    lambda d:d['origin'].update(name='Different business'),
    lambda d:d['sources'][0].update(url='http://127.0.0.1/private'),
    lambda d:d['sources'][0].update(url='https://public.example/?access_token=private'),
    lambda d:d['sources'][0].update(url='https://public.example/#access_token=private'),
    lambda d:d['sources'][0].update(retrieved_at='2026-10-05T12:00:00'),
    lambda d:d['methodology'].update(uncertainty=[]),
    lambda d:d['methodology'].update(walkingMethod=[]),
    lambda d:d['catchments'][0]['geometry'].update(type=['Polygon']),
    lambda d:d['catchments'][0]['geometry'].update(coordinates=[]),
    lambda d:d['catchments'].append(copy.deepcopy(d['catchments'][0])),
    lambda d:d['pois'][0].update(url='javascript:alert(1)'),
    lambda d:d['pois'][0].update(coordinates=[float('nan'),40.4]),
    lambda d:d['pois'][0].update(id='origin'),
    lambda d:d['stats'].update(totalBuildings=999),
])
def test_untrusted_map_geometry_sources_and_bindings_are_strict(mutate):
    value=document();mutate(value)
    with pytest.raises(ValueError):maps.validate(raw(value))


def test_json_duplicate_keys_nested_active_properties_and_size_are_rejected():
    for value in [b'{"kind":"a","kind":"b"}',b'{"__proto__":{}}',b' '* (maps.MAX_BYTES+1),b'['*1500+b']'*1500]:
        with pytest.raises(ValueError):maps.validate(value)


def test_profile_tool_guide_builds_publishable_sourced_origin_with_empty_unsourced_layers(tmp_path):
    async def scenario():
        agent=CodexVideoAgent(tmp_path,FixtureIndex(),lambda:{})
        business,files,store=setup(tmp_path);agent.business_store=business;agent.file_store=files
        agent.children={'root':{'id':'root','dataset':'cafe','owner_principal_id':'alice'}}
        agent.active['root']={'cancel':asyncio.Event(),'tool_events':asyncio.Queue()}
        parts,_,ok=await agent._tool('get_business_profile',{},'cafe','root')
        assert ok
        result=json.loads(parts[0]['text']);guide=result['map_format'];pack=guide['template']
        assert pack['binding']=={'business_name':'Fixture café','profile_location':'One square'}
        assert guide['publish']['expected_profile_revision']==result['profile_revision']
        # All populated geography and citations come from the controlled source.
        source=document();pack['origin']=source['origin'];pack['bbox']=source['bbox'];pack['sources']=source['sources']
        pack['methodology'].update(source='Controlled source',catchmentNote='Walking coverage awaits mapped route evidence.',
            uncertainty=['Only the sourced business origin is available in this fixture.'])
        assert pack['catchments']==pack['pois']==[]
        assert all(pack[layer]['features']==[] for layer in maps.LAYERS)
        assert pack['methodology']['walkingMethod'] is None and pack['methodology']['walkingSpeedKmh'] is None
        assert maps.validate(raw(pack))==pack
        record=file(files,pack)
        parts,_,ok=await agent._tool('publish_business_map',{'file_id':record['id'],
            'expected_profile_revision':guide['publish']['expected_profile_revision']},'cafe','root')
        assert ok,parts
        assert business.get('cafe')=={k:v for k,v in result.items() if k!='map_format'}
        # Guide copies are not a second persisted profile or a shared mutable template.
        next_parts,_,_=await agent._tool('get_business_profile',{},'cafe','root')
        assert json.loads(next_parts[0]['text'])['map_format']['template']['origin']['coordinates'] is None
    asyncio.run(scenario())


@pytest.mark.parametrize('estimates',['catchment','poi'])
def test_supplied_walking_estimate_requires_explicit_method_and_speed(estimates):
    pack=document()
    if estimates=='poi':pack['catchments']=[]
    else:pack['pois']=[]
    pack['methodology'].update(walkingMethod=None,walkingSpeedKmh=None)
    with pytest.raises(ValueError):maps.validate(raw(pack))


def test_aggregate_bounds_and_supplied_empty_source_feature(monkeypatch):
    value=document()
    feature={'type':'Feature','properties':{'use':'unknown'},'geometry':{'type':'Polygon','coordinates':[]}}
    value['buildings']['features']=[feature]
    value['stats'].update(totalBuildings=1,counts={**value['stats']['counts'],'unknown':1})
    assert maps.validate(raw(value))==value
    with monkeypatch.context() as p:
        p.setattr(maps,'MAX_FEATURES',0)
        with pytest.raises(ValueError):maps.validate(raw(value))
    with monkeypatch.context() as p:
        p.setattr(maps,'MAX_COORDINATES',3)
        with pytest.raises(ValueError):maps.validate(raw(value))
    value['pois']*=201
    with pytest.raises(ValueError):maps.validate(raw(value))


def test_original_native_json_pack_is_immutable_idempotent_and_separate_from_profile(tmp_path):
    business,files,store=setup(tmp_path);record=file(files,native=True);created=[]
    before=business.history('cafe')
    result=publish(store,business,files,record,created=created)
    assert created==[result['id']] and 'data' not in result and len(json.dumps(result))<1000
    again=[]
    assert publish(store,business,files,record,created=again)==result and again==[]
    assert business.history('cafe')==before
    assert store.get(business,owner='alice',collection='cafe')['data']==document()
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT source_sha256 FROM maps').fetchone()[0]==record['sha256']
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):db.execute("UPDATE maps SET business_name='other'")
    assert store.path.stat().st_mode & 0o077 == 0


def test_file_scope_profile_cas_stop_and_stale_historical_read(tmp_path):
    business,files,store=setup(tmp_path);record=file(files)
    for changes in [{'owner':'bob'},{'chat_id':'child'},{'collection':'company'},
            {'expected_profile_revision':0},{'current':lambda:False}]:
        with pytest.raises(ValueError):publish(store,business,files,record,**changes)
    saved=publish(store,business,files,record)
    for owner,collection in [('bob','cafe'),('alice','company')]:
        with pytest.raises(ValueError):store.get(business,owner=owner,collection=collection,map_id=saved['id'])
    business.update_settings('cafe',1,{'initial_history_hours':12})
    assert store.get(business,owner='alice',collection='cafe')['status']=='ready'
    business.update('cafe',2,{'name':'Fixture café','location':'Another location'})
    latest=store.get(business,owner='alice',collection='cafe')
    assert latest['status']=='stale' and latest['data'] is None
    historical=store.get(business,owner='alice',collection='cafe',map_id=saved['id'])
    assert historical['status']=='stale' and historical['data']==document()
    with pytest.raises(ValueError,match='Refresh the business'):publish(store,business,files,record)


def test_commit_guard_rollback_and_bounded_storage(tmp_path,monkeypatch):
    business,files,store=setup(tmp_path);record=file(files);checks=[]
    def current():checks.append(True);return len(checks)<3
    with pytest.raises(ValueError,match='finished'):publish(store,business,files,record,current=current)
    assert store.get(business,owner='alice',collection='cafe')['status']=='empty'
    monkeypatch.setattr(maps,'MAX_STORED_BYTES',100)
    with pytest.raises(ValueError,match='allowance'):publish(store,business,files,record)


def test_http_map_exact_owner_observer_current_membership_and_private_cache(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch)
    business,files,store=setup(tmp_path/'state');saved=publish(store,business,files,file(files))
    url=saved['url']
    with TestClient(app) as alice,TestClient(app) as bob,TestClient(app) as owner:
        assert alice.get(url).status_code==401
        login(alice,app,'alice');login(bob,app,'bob');login(owner,app,'owner',owner=True)
        response=alice.get(url)
        assert response.status_code==200 and response.json()['data']==document()
        assert response.headers['cache-control']=='private, no-store'
        assert bob.get(url).status_code==404
        assert bob.get('/api/business/map?collection=cafe').json()['status']=='empty'
        assert owner.get(url).status_code==404
        owner.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers={'X-Video-Demo':'1'})
        assert owner.get(url).json()['data']==document()
        with app.state.access.connect() as db:db.execute("DELETE FROM principal_environments WHERE principal_id='alice'")
        assert alice.get(url).status_code==403


def test_only_fixed_map_renderer_assets_are_public(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);web=Path(__file__).parents[1]/'web'
    with TestClient(app) as client:
        for path in ('business-map.js','business-map.css','vendor/d3-7.9.0/d3.min.js'):
            response=client.get('/'+path)
            assert response.status_code==200 and response.content==(web/path).read_bytes()
        assert client.get('/vendor/d3-7.9.0/manifest.json').status_code==404
        assert client.get('/api/business/map?collection=cafe').status_code==401


def test_parent_tool_emits_small_persistent_map_reference_and_rejects_children(tmp_path):
    async def scenario():
        agent=CodexVideoAgent(tmp_path,FixtureIndex(),lambda:{})
        business,files,store=setup(tmp_path);agent.business_store=business;agent.file_store=files
        agent.children={'root':{'id':'root','dataset':'cafe','owner_principal_id':'alice'},
            'child':{'id':'child','dataset':'cafe','owner_principal_id':'alice','parent_id':'root'}}
        agent.active['root']={'cancel':asyncio.Event(),'tool_events':asyncio.Queue()}
        record=file(files);args={'file_id':record['id'],'expected_profile_revision':1}
        assert 'publish_business_map' in {x['name'] for x in tool_specs()}
        assert 'publish_business_map' not in {x['name'] for x in tool_specs(False)}
        parts,_,ok=await agent._tool('publish_business_map',args,'cafe','root')
        assert ok and len(parts[0]['text'])<1000
        event=agent.active['root']['tool_events'].get_nowait();answer={}
        agent._record_event(answer,event)
        assert answer['business_map']==json.loads(parts[0]['text'])
        for chat,scope in [('child','cafe'),('root','company'),('missing','cafe')]:
            assert not (await agent._tool('publish_business_map',args,scope,chat))[2]
        agent.active['root']['cancel'].set()
        assert not (await agent._tool('publish_business_map',args,'cafe','root'))[2]
    asyncio.run(scenario())


def test_stop_during_storage_drains_and_removes_only_new_map(tmp_path,monkeypatch):
    async def scenario():
        agent=CodexVideoAgent(tmp_path,FixtureIndex(),lambda:{})
        business,files,store=setup(tmp_path);agent.business_store=business;agent.file_store=files;agent.business_map_store=store
        preserved=publish(store,business,files,file(files))
        agent.children={'root':{'id':'root','dataset':'cafe','owner_principal_id':'alice'}}
        run={'cancel':asyncio.Event(),'tool_events':asyncio.Queue()};agent.active['root']=run
        entered=threading.Event();release=threading.Event();original=store.publish
        def delayed(*args,**kwargs):
            result=original(*args,**kwargs);entered.set();assert release.wait(3);return result
        monkeypatch.setattr(store,'publish',delayed)
        args={'file_id':file(files)['id'],'expected_profile_revision':1}
        task=asyncio.create_task(agent._tool('publish_business_map',args,'cafe','root'))
        assert await asyncio.to_thread(entered.wait,3)
        run['cancel'].set();task.cancel();await asyncio.sleep(.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert store.get(business,owner='alice',collection='cafe')['map']['id']==preserved['id']
        assert run['tool_events'].empty()
    asyncio.run(scenario())


def test_membership_revoked_after_file_read_stops_publication_and_logout_preserves_membership(tmp_path,monkeypatch):
    from access import AccessStore
    async def scenario():
        agent=CodexVideoAgent(tmp_path,FixtureIndex(),lambda:{})
        business,files,store=setup(tmp_path);agent.business_store=business;agent.file_store=files;agent.business_map_store=store
        access=AccessStore(tmp_path)
        token,_=access.login(access.issue('demo',principal_id='alice'))
        access.logout(token)
        assert maps.current_member(access.path,'demo','alice')
        agent.business_map_authorized=lambda owner:maps.current_member(access.path,'demo',owner)
        agent.children={'root':{'id':'root','dataset':'cafe','owner_principal_id':'alice'}}
        agent.active['root']={'cancel':asyncio.Event(),'tool_events':asyncio.Queue()}
        record=file(files);download=files.download
        def revoke(*args,**kwargs):
            result=download(*args,**kwargs)
            with access.connect() as db:db.execute("DELETE FROM principal_environments WHERE principal_id='alice'")
            return result
        monkeypatch.setattr(files,'download',revoke)
        assert not (await agent._tool('publish_business_map',{'file_id':record['id'],'expected_profile_revision':1},'cafe','root'))[2]
        assert store.get(business,owner='alice',collection='cafe')['status']=='empty'
        assert agent.active['root']['tool_events'].empty()
    asyncio.run(scenario())


def test_profile_location_change_during_file_read_cannot_publish_old_binding(tmp_path,monkeypatch):
    business,files,store=setup(tmp_path);record=file(files);download=files.download
    def changed(*args,**kwargs):
        result=download(*args,**kwargs)
        business.update('cafe',1,{'name':'Fixture café','location':'New location'})
        return result
    monkeypatch.setattr(files,'download',changed)
    with pytest.raises(ValueError,match='Refresh the business'):publish(store,business,files,record)
    assert store.get(business,owner='alice',collection='cafe')['status']=='empty'
