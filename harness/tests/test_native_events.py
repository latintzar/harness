"""Public streaming preserves phases and measured context while excluding hidden reasoning."""
from pathlib import Path
import copy
import json
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_events import PublicActivity, context_usage, plan_usage


def test_public_commentary_and_final_are_separate_from_hidden_reasoning():
    stream=PublicActivity(result_fields=retail_result_fields)
    assert stream.translate('item/started',{'item':{'id':'a','type':'agentMessage','phase':'commentary'}}) is None
    commentary = stream.translate('item/agentMessage/delta',{'itemId':'a','delta':'Opening evidence'})
    assert commentary['type']=='progress' and commentary['activity']=='commentary'
    stream.translate('item/started',{'item':{'id':'b','type':'agentMessage','phase':'final_answer'}})
    assert stream.translate('item/agentMessage/delta',{'itemId':'b','delta':'Result'})['type']=='delta'
    assert stream.translate('item/reasoning/textDelta',{'delta':'private'}) is None
    assert stream.translate('item/reasoning/summaryTextDelta',{'delta':'private'}) is None


def test_context_uses_last_request_and_plan_omits_account_identifiers():
    result=context_usage({'last':{'totalTokens':100,'inputTokens':80,'outputTokens':20},'total':{'totalTokens':99000},'modelContextWindow':1000})
    assert result['used_tokens']==100 and result['window_tokens']==1000
    assert context_usage({'last':{}}) is None
    result=plan_usage({'accountId':'private','rateLimits':{'planType':'pro','credits':{'balance':'unknown-unit'},'primary':{'usedPercent':17,'resetsAt':1000}}})
    assert result['limits'][0]['used_percent']==17
    assert 'private' not in str(result) and 'unknown-unit' not in str(result)


def test_actual_web_tool_identity_survives_start_and_completion():
    stream=PublicActivity(result_fields=retail_result_fields)
    item={'id':'search-1','type':'webSearch','query':'cafe','action':{'type':'openPage','url':'https://example.com'},'results':None}
    start=stream.translate('item/started',{'item':item})
    end=stream.translate('item/completed',{'item':item})
    assert start['name']=='open_web_page' and start['arguments']=={'url':'https://example.com'}
    assert start['id']==end['id']=='search-1' and end['results'] is None


def test_compaction_and_thinking_lifecycle_exclude_reasoning_contents():
    stream=PublicActivity(result_fields=retail_result_fields)
    thinking=stream.translate('item/started',{'item':{'id':'r','type':'reasoning','content':['SECRET'],'summary':['SECRET']}})
    assert thinking['activity']=='thinking' and 'SECRET' not in str(thinking)
    item={'id':'compact','type':'contextCompaction'}
    assert stream.translate('item/started',{'item':item})['name']=='compact_context'
    assert stream.translate('item/completed',{'item':item})['success'] is True


def test_completed_public_message_recovers_missing_delta_without_duplication():
    stream=PublicActivity(result_fields=retail_result_fields)
    item={'id':'a','type':'agentMessage','phase':'final_answer','text':'A full answer'}
    stream.translate('item/started',{'item':{**item,'text':''}})
    stream.translate('item/agentMessage/delta',{'itemId':'a','delta':'A full '})
    assert stream.translate('item/completed',{'item':item})['text']=='answer'
    assert stream.translate('item/completed',{'item':item}) is None


def test_plan_and_mcp_progress_are_public_bounded_events():
    stream=PublicActivity(result_fields=retail_result_fields)
    event=stream.translate('turn/plan/updated',{'turnId':'t','plan':[{'step':'Inspect source','status':'inProgress'}]})
    assert event['activity']=='plan' and 'Inspect source' in event['text']
    event=stream.translate('item/mcpToolCall/progress',{'itemId':'m','message':'Read 2 clips'})
    assert event['activity']=='tool_progress' and event['text']=='Read 2 clips\n'


def retail_result_fields(name, content, success):
    from inspection_previews import tool_previews
    previews = tool_previews(name, content, success)
    return {'frame_previews': previews} if previews else {}


def inspection_call():
    preview={'url':'/api/inspection-previews/'+'a'*32+'/0.jpg','time':2.5,
        'clip_id':'clip-1','sha256':'b'*64,'basis':'source_frame_pts','width':1920,'height':1080}
    return preview, {'id':'inspect-1','type':'mcpToolCall','server':'rocoto','tool':'inspect_clip',
        'status':'completed','arguments':{'clip_id':'clip-1'},'result':{'content':[
            {'type':'text','text':json.dumps({'frame_previews':[preview]})},
            {'type':'image','mimeType':'image/jpeg','data':'PRIVATE_IMAGE_BYTES'}],'isError':False}}


def test_actual_native_workspace_inspection_preserves_typed_preview_without_image_bytes():
    preview,item=inspection_call();before=copy.deepcopy(item);stream=PublicActivity(result_fields=retail_result_fields)
    start=stream.translate('item/started',{'item':{**item,'status':'inProgress','result':None}})
    event=stream.translate('item/completed',{'item':item})
    assert start['name']==event['name']=='mcp__rocoto__inspect_clip'
    assert 'frame_previews' not in start and event['frame_previews']==[preview]
    assert 'PRIVATE_IMAGE_BYTES' not in json.dumps(event)
    assert item==before


@pytest.mark.parametrize('change',[
    {'server':'notion'}, {'server':'rocoto_provider'}, {'tool':'inspect_clip_other'},
    {'status':'failed'}, {'error':{'message':'failed'}}])
def test_native_inspection_preview_requires_exact_workspace_tool_and_success(change):
    _,item=inspection_call();item.update(change)
    assert 'frame_previews' not in PublicActivity(result_fields=retail_result_fields).translate('item/completed',{'item':item})


def test_native_inspection_preview_rejects_provider_error_malformed_metadata_and_identity_change():
    _,item=inspection_call()
    error=copy.deepcopy(item);error['result']['isError']=True
    assert 'frame_previews' not in PublicActivity(result_fields=retail_result_fields).translate('item/completed',{'item':error})
    for text in ['{}', 'malformed', json.dumps({'frame_previews':[{'url':'https://provider.example/frame.jpg'}]})]:
        bad=copy.deepcopy(item);bad['result']['content'][0]['text']=text
        assert 'frame_previews' not in PublicActivity(result_fields=retail_result_fields).translate('item/completed',{'item':bad})
    stream=PublicActivity(result_fields=retail_result_fields)
    stream.translate('item/started',{'item':{**item,'server':'notion','result':None}})
    assert 'frame_previews' not in stream.translate('item/completed',{'item':item})


def test_native_inspection_uses_original_first_metadata_block_before_output_truncation():
    preview,item=inspection_call()
    item['result']['content'][0]['text']=json.dumps({'context':'x'*33000,'frame_previews':[preview]})
    event=PublicActivity(result_fields=retail_result_fields).translate('item/completed',{'item':item})
    assert event['frame_previews']==[preview]
    item['result']['content'].insert(0,{'type':'text','text':'malformed first metadata'})
    assert 'frame_previews' not in PublicActivity(result_fields=retail_result_fields).translate('item/completed',{'item':item})


def test_real_native_mcp_conversion_keeps_preview_descriptors_separate_from_vision_bytes():
    import base64
    from native_mcp import NativeMCP
    preview,item=inspection_call()
    raw=b'\xff\xd8\xffprivate exact vision bytes\xff\xd9'
    encoded=base64.b64encode(raw).decode()
    response=NativeMCP._result([
        {'type':'inputText','text':json.dumps({'frame_previews':[preview]})},
        {'type':'inputImage','imageUrl':'data:image/jpeg;base64,'+encoded}])
    assert response.isError is False and response.content[1].data==encoded
    item['result']=response.model_dump(mode='json',exclude_none=True)
    event=PublicActivity(result_fields=retail_result_fields).translate('item/completed',{'item':item})
    assert event['frame_previews']==[preview]
    assert encoded not in json.dumps(event)


def test_generic_projection_needs_no_renderer_and_provider_cannot_call_one():
    _, item = inspection_call()
    assert 'frame_previews' not in PublicActivity().translate('item/completed', {'item': item})
    calls = []
    def render(name, content, success):
        calls.append(name)
        return {'display': {'kind': 'test'}}
    item['server'] = 'provider'
    event = PublicActivity(result_fields=render).translate('item/completed', {'item': item})
    assert calls == [] and 'display' not in event
    item['server'] = 'rocoto'
    event = PublicActivity(result_fields=render).translate('item/completed', {'item': item})
    assert calls == ['inspect_clip'] and event['display'] == {'kind': 'test'}
