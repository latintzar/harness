"""Stock signal/trace bridge fixtures exercise real stream stores; no GPU execution."""
import copy
import json
import pytest
from test_visit_basket import model_fixture
from cosmos_stream_bridge import prepare_prompt,admit_result,final_answer,digest
from stream_runtime import drain_stream


def setup(tmp_path,start=0,relative=False,claim=True):
    basket,loc,monitor,pipeline,tracks,_,_,_,_=model_fixture(tmp_path)
    profile=next(p for p in monitor.list_profiles('owner','business') if p['name']=='Stream basket')
    context={'stream_id':'camera-stream','source_id':'owned-one','profile_id':profile['id'],'profile_revision':1,
        'start_s':start,'end_s':start+10,'input_kind':'dataset_replay','model':'fixture-Cosmos','sampling':'8fps','processing_seconds':0}
    if start:pipeline.accept('owner',{**context,'start_s':0,'end_s':start,'observations':[]})
    tracker={'source_id':'owned-one','source_sha256':'a'*64,'camera_id':None,'identity_scope':'recording_and_camera_local','fps':8,'frames':[
        {'video_time_s':start+i/8,'tracks':[{'track_id':'0'}]} for i in range(80)]}
    preparation={'source_sha256':'a'*64,'tracker_sha256':digest(tracker),'rendered_labels':{'Person0':'0'},
        'clock_basis':'segment_relative' if relative else 'source_absolute','source_offset_s':start if relative else 0,
        'osd_renderer':'nvdsosd','model_input_sha256':'b'*64,'osd_configuration_sha256':'c'*64}
    prepared=prepare_prompt(pipeline,'owner',context,clock_basis='segment_relative' if relative else 'source_absolute',
        source_offset_s=start if relative else 0,tracker_labels={'Person0':'0'},tracker_receipt=tracker,input_preparation=preparation)
    offset=0 if relative else start
    times=[offset+i/8 for i in range(80)]
    event={'key':'e1','start_s':offset+1,'end_s':offset+2,'person_id':'Person0','type':'pick_up',
        'description':'Fixture person lifts a carton','object_description':'Fixture carton','object_ref':'carton',
        'item_claim':{'quantity':1,'relation':'first_observation'} if claim else None}
    envelope={'stream_id':2,'segment_start_s':offset,'segment_end_s':offset+10,
        'result_text':'<think>Discard this thought: {"quantity":999}</think>\n<answer>'+json.dumps({'events':[event]})+'</answer>'}
    trace={'stream_id':2,'segment_start_s':offset,'segment_end_s':offset+10,'frame_times_s':times,
        'video_metadata':{'fps':8,'total_num_frames':80},'llm_generate_elapsed_s':1.25,
        'actual_video_argument_shape':[80,3,448,448],'frame_hashes_sha256':[digest(i) for i in range(80)],
        'input_preparation_sha256':digest(preparation),
        'effective_prompt_config':{'user_prompt':prepared['user_prompt'],'max_tokens':4096},
        'serialized_prompt':'<user>'+prepared['user_prompt'].format(timestamps=' '.join(f'{t:.2f}s' for t in times),num_frames=80,stream_id=2)+'</user>'}
    selected={'stream_ingestion':pipeline,'location_intelligence':loc,'catalog_inspection':type('I',(),{'process':lambda *args:{}})()}
    return basket,loc,monitor,pipeline,tracks,prepared,envelope,trace,selected


def test_saved_profile_formats_through_stock_and_actual_signal_creates_qualified_basket(tmp_path):
    basket,loc,monitor,pipeline,tracks,prepared,envelope,trace,selected=setup(tmp_path)
    assert '{{' in prepared['user_prompt'] and '{timestamps}' in prepared['user_prompt']
    assert 'pick_up' in trace['serialized_prompt'] and '"events"' in trace['serialized_prompt']
    receipt=admit_result(pipeline,'owner',prepared,envelope,trace)
    for _ in range(3):assert all(r['status']=='done' for r in drain_stream(selected))
    event=monitor.get_event('owner',receipt['event_ids'][0])
    assert event['track_id']=='0' and event['visit_id']==tracks[0]['id']
    result=basket.at('owner',tracks[0]['id'],3)
    assert len(result['items'])==1 and result['items'][0]['quantity']==1
    assert result['items'][0]['identity_basis']=='vision_estimate' and result['totals']==[]
    proof=loc._rows('owner','basket_evidence')[0]
    assert len(proof['adapter_receipt_sha256'])==64
    with pipeline.connect() as db:
        native=json.loads(db.execute('SELECT body FROM stream_response_adapters').fetchone()[0])
        assert native['envelope']['result_text']==envelope['result_text']
        assert native['input_trace']==trace
    assert pipeline.history('owner','camera-stream')[0]['processing_seconds']==1.25


def test_explicit_reset_pts_offset_once_and_omitted_claim_leaves_observation(tmp_path):
    basket,loc,monitor,pipeline,tracks,prepared,envelope,trace,selected=setup(tmp_path,10,True,False)
    receipt=admit_result(pipeline,'owner',prepared,envelope,trace)
    for _ in range(2):drain_stream(selected)
    e=monitor.get_event('owner',receipt['event_ids'][0])
    assert e['source']['start_s']==11 and e['source']['end_s']==12
    assert e['visit_id']==tracks[0]['id']
    assert loc._rows('owner','basket_evidence')==[]
    assert basket.at('owner',tracks[0]['id'],13)['totals']==[]
    assert basket.at('owner',tracks[0]['id'],2)['unresolved_observations']==[]


@pytest.mark.parametrize('failure',['clock','label','profile','raw_field','incomplete','prompt','thinking_only','label_map','preparation','pixels'])
def test_invalid_stock_outputs_quarantine_without_authority_or_clock_repairs(tmp_path,failure):
    _,_,monitor,pipeline,_,prepared,envelope,trace,_=setup(tmp_path,10,False)
    final={'events':[{'key':'e1','start_s':11,'end_s':12,'person_id':'Person0','type':'pick_up','description':'Fixture'}]}
    if failure=='clock':final['events'][0].update(start_s=1,end_s=2)
    if failure=='label':final['events'][0]['person_id']='0'
    if failure=='profile':prepared['profile_sha256']='f'*64
    if failure=='raw_field':final['events'][0]['profile_id']='model-selected-profile'
    if failure=='label_map':prepared['tracker_labels']['Person0']='P0'
    if failure=='preparation':trace['input_preparation_sha256']='f'*64
    if failure=='pixels':trace['actual_video_argument_shape'][0]=32
    if failure=='prompt':trace['effective_prompt_config']['user_prompt']='different request'
    envelope['result_text']=json.dumps(final)
    if failure=='incomplete':envelope['result_text']='{"events":['
    if failure=='thinking_only':envelope['result_text']='<think>'+json.dumps(final)
    with pytest.raises(ValueError):admit_result(pipeline,'owner',prepared,envelope,trace)
    assert monitor.list_events('owner','business')==[]
    with pipeline.connect() as db:assert db.execute('SELECT status FROM cosmos_bridge_attempts').fetchone()[0]=='quarantined'


def test_complete_answer_fence_and_reasoning_content_are_separate():
    assert final_answer('<think>{"events":[999]}</think>```json\n{"events":[]}\n```').events==[]
    assert final_answer('{"events":[]}').events==[]
    with pytest.raises(ValueError):final_answer('<answer>{"events":[]}')


def test_model_cannot_supply_implicit_quantity_or_trusted_identity(tmp_path):
    _,_,monitor,pipeline,_,prepared,envelope,trace,_=setup(tmp_path)
    answer=final_answer(envelope['result_text']).model_dump()
    answer['events'][0]['item_claim']['identity_basis']='human_review'
    envelope['result_text']=json.dumps(answer)
    with pytest.raises(ValueError):admit_result(pipeline,'owner',prepared,envelope,trace)
    assert monitor.list_events('owner','business')==[]


def test_foreign_tracker_sidecar_is_rejected_at_preparation(tmp_path):
    _,_,_,pipeline,_,prepared,_,_,_=setup(tmp_path)
    tracker=copy.deepcopy(prepared['tracker_receipt']);tracker['source_id']='owned-wide'
    with pytest.raises(ValueError,match='registered source'):
        prepare_prompt(pipeline,'owner',prepared['context'],clock_basis='source_absolute',source_offset_s=0,
            tracker_labels=prepared['tracker_labels'],tracker_receipt=tracker,input_preparation=prepared['input_preparation'])


def test_executable_native_signal_handoff_uses_actual_trace_and_prompt(tmp_path):
    from cosmos_stream_bridge import connect_stock_results
    _,_,monitor,pipeline,_,prepared,envelope,trace,_=setup(tmp_path)
    class NativeSignalFixture:
        def connect(self,name,handler):self.name=name;self.handler=handler;return 42
    element=NativeSignalFixture();receipts=[]
    assert connect_stock_results(element,pipeline,'owner',prepared_for=lambda *args:prepared,
        trace_for=lambda *args:trace,on_receipt=receipts.append)==42
    assert element.name=='vlm-result'
    element.handler(element,envelope['stream_id'],envelope['segment_start_s'],envelope['segment_end_s'],envelope['result_text'])
    assert len(receipts)==1
    assert monitor.get_event('owner',receipts[0]['event_ids'][0])['track_id']=='0'


def test_scene_observation_with_nullable_item_claim_is_preserved(tmp_path):
    _,_,monitor,pipeline,_,old,envelope,trace,_=setup(tmp_path)
    profile=monitor.save_profile('owner',{'name':'Scene','dataset':'business','vocabulary':[{'type':'shelf_empty','description':'Visible empty shelf section'}]},datasets=['business'])
    prepared=prepare_prompt(pipeline,'owner',{**old['context'],'profile_id':profile['id']},clock_basis='source_absolute',source_offset_s=0,
        tracker_labels=old['tracker_labels'],tracker_receipt=old['tracker_receipt'],input_preparation=old['input_preparation'])
    trace['effective_prompt_config']['user_prompt']=prepared['user_prompt']
    trace['serialized_prompt']=prepared['user_prompt'].format(timestamps=' '.join(f'{t:.2f}s' for t in trace['frame_times_s']))
    envelope['result_text']=json.dumps({'events':[{'key':'scene','start_s':1,'end_s':2,'person_id':None,'type':'shelf_empty','description':'Fixture empty shelf section','item_claim':None}]})
    receipt=admit_result(pipeline,'owner',prepared,envelope,trace)
    event=monitor.get_event('owner',receipt['event_ids'][0])
    assert event['track_id'] is None and event['object_ref'] is None
    with pipeline.connect() as db:assert [r['kind'] for r in db.execute('SELECT kind FROM stream_work')]==['project_event']


def test_stock_cli_prepares_real_config_bytes_then_admits_native_envelope(tmp_path,capsys):
    import hashlib
    from import_cosmos_response import main
    _,_,monitor,_,_,prepared,envelope,trace,_=setup(tmp_path)
    media=tmp_path/'fixture-model-input.mp4';media.write_bytes(b'Explicit synthetic input bytes for CLI contract test')
    preparation=dict(prepared['input_preparation']);preparation['model_input_sha256']=hashlib.sha256(media.read_bytes()).hexdigest()
    values={'context':prepared['context'],'tracker-labels':prepared['tracker_labels'],
        'tracker-receipt':prepared['tracker_receipt'],'input-preparation':preparation,
        'native-config':{'user_prompt':'previous','system_prompt':'Fixture','temperature':.1,'max_tokens':512}}
    args=['--state',str(tmp_path),'--owner','owner','prepare']
    for name,value in values.items():
        path=tmp_path/(name+'.json');path.write_text(json.dumps(value));args+=['--'+name,str(path)]
    saved=tmp_path/'prepared.json';config=tmp_path/'prompt-config.json'
    args+=['--model-input',str(media),'--prepared-output',str(saved),'--prompt-config-output',str(config),
           '--clock-basis','source_absolute','--source-offset-s','0']
    assert main(args)['status']=='prepared'
    actual=json.loads(saved.read_text());native=json.loads(config.read_text())
    assert native['max_tokens']==4096 and native['temperature']==.1 and native['user_prompt']==actual['user_prompt']
    assert saved.stat().st_mode&0o777==0o600
    trace['effective_prompt_config']['user_prompt']=actual['user_prompt']
    trace['serialized_prompt']=actual['user_prompt'].format(timestamps=' '.join(f'{t:.2f}s' for t in trace['frame_times_s']))
    trace['input_preparation_sha256']=actual['input_preparation_sha256']
    envelope_path=tmp_path/'envelope.json';envelope_path.write_text(json.dumps(envelope))
    trace_path=tmp_path/'trace.json';trace_path.write_text(json.dumps(trace))
    receipt=main(['--state',str(tmp_path),'--owner','owner','admit','--prepared',str(saved),'--envelope',str(envelope_path),'--input-trace',str(trace_path)])
    assert monitor.get_event('owner',receipt['event_ids'][0])['track_id']=='0'
