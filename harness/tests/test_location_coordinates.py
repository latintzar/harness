"""Source-backed map/weather coordinates survive storage and current-scope reads."""
import json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from location_intelligence import LocationCoordinates,LocationIntelligence,coordinates_from_weather
from test_location_intelligence import setup

WEATHER={'name':'Vitosha Boulevard 85','address':'85 Vitosha Boulevard, Sofia','city':'Sofia',
    'latitude':42.6842677,'longitude':23.3162691,'timezone':'Europe/Sofia',
    'coordinate_source':'https://bg.near-place.com/ps-food-drinks-1463-bulevard-vitosha-85-sofia',
    'coordinate_precision':'address_listing','provenance':'Previously supplied address listing; retained without a fresh lookup.'}


def test_coordinates_roundtrip_preserves_provenance_and_existing_presentation(tmp_path):
    store,scope,monitor,_,ids,_,_=setup(tmp_path)
    cover={'url':'/cover.png','kind':'generated_demo','provenance':{'kind':'illustration'}}
    store.configure_location('owner',{'collection':'business','location_id':ids['a'],'cover_image':cover,'widgets':['visitors']})
    coordinates=coordinates_from_weather(WEATHER)
    configured=store.configure_location('owner',{'collection':'business','location_id':ids['a'],'coordinates':coordinates})
    assert configured['cover_image']==cover and configured['widgets']==['visitors']
    row=LocationIntelligence(tmp_path,scope,monitor).snapshot('owner')['locations'][0]
    assert row['coordinates']==coordinates
    assert row['coordinates']['source']==WEATHER['coordinate_source']
    assert row['coordinates']['provenance']['note']==WEATHER['provenance']
    assert len(row['coordinates']['provenance']['configuration_sha256'])==64
    assert row['metrics']['observed_interactions']==0


@pytest.mark.parametrize('change',[{'latitude':91},{'longitude':181},{'latitude':float('nan')},
    {'latitude':True},{'timezone':'Sofia'},{'source':' '},{'provenance':{}},
    {'provenance':{'bad':float('nan')}}])
def test_coordinates_reject_unbounded_or_unsourced_values(change):
    with pytest.raises(ValueError):LocationCoordinates.model_validate({**coordinates_from_weather(WEATHER),**change})


def test_location_grants_apply_to_configuration_and_projection(tmp_path):
    store,_,_,_,ids,members,save=setup(tmp_path)
    coordinates=coordinates_from_weather(WEATHER)
    store.configure_location('owner',{'collection':'business','location_id':ids['a'],'coordinates':coordinates})
    save('members',{'member_id':'alice','role':'colleague','location_ids':[ids['a']],'function_ids':[]})
    assert store.snapshot('alice')['locations']==[] # owner-private configuration
    with pytest.raises(ValueError):store.configure_location('alice',{'collection':'business','location_id':ids['b'],'coordinates':coordinates})
    members.pop('owner')
    assert store.snapshot('owner')['locations']==[]
    with pytest.raises(ValueError):store.configure_location('owner',{'collection':'business','location_id':ids['a'],'coordinates':coordinates})


def test_manual_native_configuration_is_typed_parent_bound_and_persists(tmp_path):
    from catalog_setup_tools import specs,call
    store,_,_,_,ids,_,_=setup(tmp_path)
    definitions={x['name']:x for x in specs()}
    schema=definitions['configure_location']['inputSchema']['properties']['coordinates']
    assert schema['properties']['latitude']['maximum']==90
    assert {'latitude','longitude','timezone','source','provenance'}<=set(schema['required'])
    assert 'configure_location' not in {x['name'] for x in specs(False)}
    agent=SimpleNamespace(children={'chat':{'owner_principal_id':'owner','dataset':'business'}},
        business_scope_host={'catalog_setup':object(),'location_intelligence':store},organization_chats=None)
    coordinates=coordinates_from_weather(WEATHER);args={'location_id':ids['a'],'coordinates':coordinates}
    result=call(agent,'chat','business','configure_location',args,{'active':True})
    assert result['coordinates']==coordinates
    with pytest.raises(ValueError):call(agent,'chat','other','configure_location',args,{'active':True})
    agent.children['chat']['parent_id']='root'
    with pytest.raises(ValueError):call(agent,'chat','business','configure_location',args,{'active':True})


def test_weather_cli_import_configures_canonical_location_and_preserves_viewer_fallback(tmp_path,monkeypatch,capsys):
    from access import AccessStore
    from retail_business_scope import RetailBusinessScope as BusinessScope
    from workspace_identity import workspace_scope
    import import_control_room_bundle as importer
    from test_import_control_room_bundle import fixture
    run=tmp_path/'run';fixture(run)
    access_state=tmp_path/'access';access=AccessStore(access_state)
    access.create_environment('retail-demo','Retail demo');access.issue('retail-demo',owner=True,principal_id='peter')
    state=access_state/'environments/retail-demo';weather=tmp_path/'weather.json';weather.write_text(json.dumps(WEATHER))
    original=importer.import_bundle
    def imported(*a,**kw):
        return original(*a,**kw,poster_builder=lambda video,target,t:target.write_bytes(b'fixture-poster'))
    monkeypatch.setattr(importer,'import_bundle',imported)
    monkeypatch.setattr(sys,'argv',['import_control_room_bundle','--run-root',str(run),'--state',str(state),
        '--access-state',str(access_state),'--environment-id','retail-demo','--owner','peter','--lifecycle','development',
        '--weather-json',str(weather),'--address',WEATHER['address']])
    importer.main();result=json.loads(capsys.readouterr().out)
    scope=BusinessScope(state,company_id=workspace_scope(state),member=lambda p:{'id':p,'role':'owner'} if p=='peter' else None)
    from monitoring import MonitoringStore
    projection=LocationIntelligence(state,scope,MonitoringStore(state)).snapshot('peter')['locations'][0]
    assert projection['coordinates']==coordinates_from_weather(WEATHER)
    data=json.loads((Path(result['bundle_directory'])/'data.json').read_text())
    assert data['location']['weather']==WEATHER
