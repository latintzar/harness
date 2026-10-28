import hashlib
import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from control_room_routes import read_bundle,owner_directory
from import_control_room_bundle import import_bundle,viewer_tracks
from monitoring import MonitoringStore
from retail_stream_import import event_key
from stream_ingestion import canonical,StreamIngestion


def dump(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))


def fixture(root):
    records=[];results=[];reviews={};camera_ids={}
    for index,clip in enumerate(['000027-rank0','000027-rank1'],1):
        directory=root/'retail-action-nvidia'/clip;directory.mkdir(parents=True)
        (directory/'clean.mp4').write_bytes(b'fixture-source-'+clip.encode())
        dump(directory/'source_mapping.json',{'source_frame_timestamps_s':[0,1,2],
            'frames':[{'video_time_s':0,'source_frame_index':0}]})
        dump(directory/'tracks.json',{'identity_scope':'recording_and_camera_local',
            'fps':8,'width':600,'height':600,'frames':[{'frame_index':8,'video_time_s':1,
                'tracks':[{'track_id':1,'bbox_xyxy':[100,100,200,400],'confidence':.9}]}]})
        records.append({'clip_id':clip,'source_clip_id':'000027','source_view':clip[-5:],
            'duration_s':2.125,'source_duration_s':2,'width':600,'height':600,'fps':8,
            'temporal_sampling':'motion_selected_frames_held_on_source_timeline','unique_source_frames':3})
        event={'start':'00:01.00','end':'00:01.50','person_id':'Person 1','type':'touch',
            'description':'Touches a packet on the shelf.','product_description':None}
        results.append({'stream_id':index-1,'segment_start_s':0,'segment_end_s':10,'result_text':json.dumps([event])})
        dump(root/f'resumed-stock/results/input-trace-{index:03d}.json',{'frame_times_s':[0,1,2], 'llm_generate_elapsed_s':.4})
        source='retailaction-'+clip;key=event_key(source,index,0,event)
        reviews[key]={'decision':'verified','reviewer':'fixture-review','evidence':['source-frame1.jpg'],
            'raw_sha256':hashlib.sha256(canonical(event).encode()).hexdigest()}
        camera_ids[source]='camera-'+str(index)
    dump(root/'retail-action-nvidia/manifest.json',{'records':records})
    dump(root/'resumed-stock/results/receipt.private.json',{'input_paths_by_stream':[
        f'/workspace/retail-action-nvidia/{r["clip_id"]}/stock-tracked.mp4' for r in records],
        'results':results,'sdk_modified':False,'plugin_sha256':'a'*64})
    (root/'resumed-stock/config.yaml').write_text('model: official\n')
    dump(root/'retailaction-ground-truth.json',{'labels':[{'type':'pick_up','start_s':.5}]})
    return reviews,camera_ids


def install(root,state,reviews,cameras):
    return import_bundle(root,state,'peter',location_id='location-retail',room_id='room-shop',
        camera_ids=cameras,reviews=reviews,collection='retail',poster_builder=lambda video,target,t:target.write_bytes(b'jpeg-fixture'))


def test_private_bundle_contains_real_scoped_tracks_and_reviewed_stream_events(tmp_path):
    run=tmp_path/'run';reviews,cameras=fixture(run);state=tmp_path/'state'
    result=install(run,state,reviews,cameras)
    assert result['coverage']['tracked_views']==2
    assert result['coverage']['reviewed_observations']==2
    monitor=MonitoringStore(state);bundle=read_bundle({'state':state,'monitoring':monitor},'peter')
    assert bundle is not None
    directory,manifest=bundle
    data=json.loads((directory/'data.json').read_text())
    assert all(clip['source_provenance']['face_redaction']=='source_anonymized' for clip in data['clips'])
    assert all(clip['capture_start_utc'] is None for clip in data['clips'])
    assert len({clip['camera_id'] for clip in data['clips']})==2
    for clip in data['clips']:
        assert (directory/clip['media']).read_bytes().startswith(b'fixture-source')
        trace=json.loads((directory/clip['tracks_url']).read_text())
        assert trace['frames']['8'][0]['id']=='1'
        assert clip['models']['8b']['windows'][0]['events'][0]['event']=='touch'
    events=monitor.list_events('peter','retail')
    assert len(events)==2 and len({event['visit_id'] for event in events})==2
    assert all(event['type']=='touch' for event in events)  # Ground truth pickup never imported.
    assert all(event['object_description'] is None for event in events)
    assert any(path.endswith('/qa/publisher-labels.json') for path in manifest['files'])
    assert read_bundle({'state':state,'monitoring':monitor},'another') is None
    assert install(run,state,reviews,cameras)['revision']==result['revision']
    assert len(monitor.list_events('peter','retail'))==2
    with monitor.connect() as db:db.execute('DELETE FROM monitoring_sources WHERE owner=?',('peter',))
    assert read_bundle({'state':state,'monitoring':monitor},'peter') is None


def test_unreviewed_predictions_are_retained_only_in_qa(tmp_path):
    root=tmp_path/'run';_,cameras=fixture(root);state=tmp_path/'state'
    result=install(root,state,{},cameras)
    assert result['coverage']['reviewed_observations']==0
    data=json.loads((Path(result['bundle_directory'])/'data.json').read_text())
    assert all(not w['events'] for clip in data['clips'] for w in clip['models']['8b']['windows'])
    audit=next(Path(result['bundle_directory']).glob('revisions/*/qa/model-output-audit.json'))
    assert json.loads(audit.read_text())['counts']['quarantined']==2
    assert MonitoringStore(state).list_events('peter','retail')==[]


def test_import_rejects_source_symlink_and_unknown_camera(tmp_path):
    root=tmp_path/'run';reviews,cameras=fixture(root)
    with pytest.raises(ValueError,match='Bind each recording'):
        install(root,tmp_path/'missing-camera',reviews,{})
    source=root/'retail-action-nvidia/000027-rank0/clean.mp4';source.unlink()
    outside=tmp_path/'outside.mp4';outside.write_bytes(b'unrelated');source.symlink_to(outside)
    with pytest.raises(ValueError,match='verified import directory'):
        install(root,tmp_path/'state',reviews,cameras)


def test_publisher_labels_are_separate_and_keep_source_geometry(tmp_path):
    root=tmp_path/'run';reviews,cameras=fixture(root);state=tmp_path/'state'
    labels={'source':'https://huggingface.co/datasets/standard-cognition/RetailAction','records':[
        {'source_clip_id':'000027','source_duration_s':2,'events':[
            {'label':'take','start_s':0,'end_s':2,'normalized_start':0,'normalized_end':1,
             'spatial':{'action_cam':{'rank0':{'x':.3,'y':.4},'rank1':{'x':.3,'y':.4}}}}]}]}
    dump(root/'retailaction-ground-truth.json',labels)
    for rank in ['rank0','rank1']:
        folder=root/'retail-action-nvidia'/('000027-'+rank)
        dump(folder/'tracks.json',{'identity_scope':'recording_and_camera_local','fps':8,'width':600,'height':600,
            'frames':[{'frame_index':i*8,'video_time_s':i,'tracks':[{'track_id':1,'bbox_xyxy':[100,100,300,400]}]} for i in range(3)]})
        dump(folder/'source_mapping.json',{'source_frame_timestamps_s':[0,1,2],
            'frames':[{'frame_index':i*8,'source_frame_index':i,'video_time_s':i} for i in range(3)]})
    dump(root/'retail-action-raw/000027/metadata.json',{'content':{'action_cam':{rank:{'face_positions':[
        {'col':.3,'row':.3,'sampled_at':f'1970-01-01T00:00:0{i}+00:00'} for i in range(3)]} for rank in ['rank0','rank1']}}})
    result=install(root,state,reviews,cameras)
    assert result['coverage']['dataset_reference_events']==2
    assert result['coverage']['dataset_reference_associated_events']==2
    monitor=MonitoringStore(state);events=monitor.list_events('peter','retail',include_reference=True)
    reference=[e for e in events if e['origin']=='dataset_reference']
    assert len(reference)==2 and all(e['type']=='pick_up' for e in reference)
    assert all(e['review_state']=='publisher_annotation' and e['bbox'] is None for e in reference)
    assert len({e['profile_id'] for e in events})==2
    data=json.loads((Path(result['bundle_directory'])/'data.json').read_text())
    for clip in data['clips']:
        event=clip['models']['dataset_reference']['windows'][0]['events'][0]
        assert event['provenance']['kind']=='dataset_reference'
        assert event['provenance']['association']['basis']=='publisher_subject_point_in_unique_person_box'
        assert event['snapshot_kind']=='source_frame' and not event['object_crop_verified']
        assert clip['models']['8b']['windows'][0]['events'][0]['event']=='touch'
    assert install(root,state,reviews,cameras)['revision']==result['revision']


def test_real_tracker_import_projects_visits_without_model_or_reference_events(tmp_path, monkeypatch):
    from business_scope import ScopeActor
    from retail_business_scope import RetailBusinessScope as BusinessScope
    from workspace_identity import workspace_scope
    from location_intelligence import LocationIntelligence
    from import_control_room_bundle import ingest_tracker_visits
    root = tmp_path / 'run'
    _, camera_ids = fixture(root)
    state = tmp_path / 'state'
    sources = set(camera_ids)
    scope = BusinessScope(state, company_id=workspace_scope(state),
        member=lambda owner:{'id':owner, 'role':'owner'} if owner == 'peter' else None,
        source_authorized=lambda actor, collection, source:actor.principal_id == 'peter' and source in sources)
    actor = ScopeActor(scope.company_id, 'peter')
    view = scope.mutate(actor, 'retail', 'locations', {'name':'Retail demo'}, 0)
    location_id = view['locations'][-1]['id']
    view = scope.mutate(actor, 'retail', 'rooms', {'name':'Shop floor', 'location_id':location_id, 'function_ids':[]}, view['revision'])
    room_id = view['rooms'][-1]['id']
    for source in sources:
        view = scope.mutate(actor, 'retail', 'cameras', {'name':source, 'location_id':location_id,
            'room_ids':[room_id], 'source_ids':[source]}, view['revision'])
        camera_ids[source] = view['cameras'][-1]['id']
    import import_control_room_bundle as module
    from tracker_runner import TrackerRuns
    def leased_visits(*args):
        assert TrackerRuns(state).claim('peter', 'retail', location_id, trigger='manual', stamp='racing', day='2026-10-11') is None
        return ingest_tracker_visits(*args)
    monkeypatch.setattr(module, 'ingest_tracker_visits', leased_visits)
    imported = import_bundle(root, state, 'peter', location_id=location_id, room_id=room_id,
        camera_ids=camera_ids, reviews={}, collection='retail', visit_scope=scope,
        poster_builder=lambda video,target,t:target.write_bytes(b'jpeg-fixture'))
    monitor = MonitoringStore(state)
    assert monitor.list_events('peter', 'retail') == []
    result = ingest_tracker_visits(root, state, 'peter', scope)
    assert result == imported['tracker_visits']
    assert result['tracked_visit_count'] == 2
    assert len({visit for source in result['sources'] for visit in source['visit_ids']}) == 2
    assert all(source['basis'] == 'first_observed_tracker_presence' for source in result['sources'])
    assert ingest_tracker_visits(root, state, 'peter', scope) == result
    store = LocationIntelligence(state, scope, monitor)
    visits = store._rows('peter', 'tracked_visit')
    assert len(visits) == 2
    assert all(visit['first_observation_semantics'] == 'first_observed_in_recording' for visit in visits)
    assert all(visit['observed_intervals'] == [{'start_s':1.0, 'end_s':1.0}] for visit in visits)
    store.configure_location('peter', {'collection':'retail', 'location_id':location_id})
    snapshot = store.snapshot('peter')['locations'][0]
    assert snapshot['metrics']['observed_tracks'] == 2
    assert snapshot['metrics']['observed_interactions'] == 0
    assert monitor.list_events('peter', 'retail') == []


def test_generated_cover_is_explicit_hashed_and_published_with_provenance(tmp_path):
    from import_control_room_bundle import generated_cover
    root = tmp_path / 'run'
    reviews, cameras = fixture(root)
    state = tmp_path / 'state'
    selected = 'location-images/store-exterior.png'
    result = import_bundle(root, state, 'peter', location_id='location-retail', room_id='room-shop',
        camera_ids=cameras, reviews=reviews, collection='retail', cover_image=selected,
        poster_builder=lambda video,target,t:target.write_bytes(b'jpeg-fixture'))
    folder = Path(result['bundle_directory'])
    data = json.loads((folder / 'data.json').read_text())
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert data['location']['cover_image']['kind'] == 'generated_demo'
    assert data['location']['cover_image']['url'] == '/control-room/' + selected
    for name in [selected, 'location-images/store-exterior.provenance.json']:
        assert name in manifest['files']
        assert (folder / name).is_file()
        assert manifest['generated_assets'][name]['sha256'] == hashlib.sha256((folder / name).read_bytes()).hexdigest()
    assert generated_cover(None) == (None, {})
    with pytest.raises(ValueError):
        generated_cover('../../private.png')


def test_bundle_import_waits_for_the_locations_running_pass_before_writing(tmp_path, monkeypatch):
    import tracker_runner as runner
    root = tmp_path / 'run'
    reviews, cameras = fixture(root)
    state = tmp_path / 'state'
    runs = runner.TrackerRuns(state)
    held = runs.claim('peter', 'retail', 'location-retail', trigger='manual', stamp='before', day='2026-10-11')
    host_pass = runner.host_pass
    monkeypatch.setattr(runner, 'host_pass', lambda company, location: host_pass(company, location, wait_seconds=0))
    try:
        with pytest.raises(ValueError, match=runner.BUSY):
            install(root, state, reviews, cameras)
        assert not (state / 'monitoring.sqlite').exists()
        assert not (owner_directory(state, 'peter') / 'data.json').exists()
    finally:
        runs.finish(held['id'], held['lease_token'], status='completed')


def test_bundle_import_holds_the_location_until_its_manifest_is_written(tmp_path, monkeypatch):
    import import_control_room_bundle as module
    import tracker_runner as runner
    root = tmp_path / 'run'
    reviews, cameras = fixture(root)
    state = tmp_path / 'state'
    runs = runner.TrackerRuns(state)
    checked = []
    save_profile, write_json = MonitoringStore.save_profile, module.write_json
    def verify_lease(stage):
        assert runs.claim('peter', 'retail', 'location-retail', trigger='manual', stamp='racing', day='2026-10-11') is None
        checked.append(stage)
    def save(store, *args, **kwargs):
        verify_lease('profile')
        return save_profile(store, *args, **kwargs)
    def write(path, value):
        verify_lease(Path(path).name)
        return write_json(path, value)
    monkeypatch.setattr(MonitoringStore, 'save_profile', save)
    monkeypatch.setattr(module, 'write_json', write)
    install(root, state, reviews, cameras)
    assert checked[0] == 'profile' and checked[-1] == 'manifest.json'
    assert runs.runs('peter', 'retail', 'location-retail')[0]['status'] == 'completed'
    assert not runs.state('peter', 'retail', 'location-retail')['running']


def test_failed_bundle_import_releases_the_location_lease(tmp_path):
    import tracker_runner as runner
    root = tmp_path / 'run'
    reviews, cameras = fixture(root)
    state = tmp_path / 'state'
    with pytest.raises(ValueError, match='Bind each recording'):
        install(root, state, reviews, {})
    runs = runner.TrackerRuns(state)
    assert runs.runs('peter', 'retail', 'location-retail')[0]['status'] == 'failed'
    assert not runs.state('peter', 'retail', 'location-retail')['running']
