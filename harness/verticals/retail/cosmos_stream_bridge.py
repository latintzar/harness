"""Stock nvvllmvlm prompt/result boundary; no inference implementation or SDK patch.

The ordinary runner writes the prepared user_prompt into its prompt config,
then passes its vlm-result envelope and actual input trace to admit_result.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
from pydantic import BaseModel, ConfigDict, Field
from stream_ingestion import StreamWindow, VisionItemClaim, canonical


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ItemClaim(BaseModel):
    model_config=ConfigDict(extra='forbid')
    quantity:int=Field(ge=1,le=1000,strict=True)
    relation:str
    prior_event_key:str|None=None


class CosmosEvent(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    key:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')
    start_s:float=Field(ge=0)
    end_s:float=Field(ge=0)
    person_id:str|None=None
    type:str
    description:str=Field(min_length=1,max_length=4000)
    object_description:str|None=Field(default=None,max_length=2000)
    object_ref:str|None=Field(default=None,pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$')
    item_claim:ItemClaim|None=None


class CosmosAnswer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    events:list[CosmosEvent]=Field(max_length=500)


def final_answer(text):
    """Accept final JSON only, including official think/answer tags and JSON fence."""
    if not isinstance(text,str) or len(text.encode())>2_000_000:
        raise ValueError('Use the bounded original stock result_text.')
    final=text.strip()
    if final.startswith('<think>'):
        if '</think>' not in final:raise ValueError('Wait for a complete final answer after reasoning.')
        final=final.split('</think>',1)[1].strip()
    if '<think>' in final or '</think>' in final:
        raise ValueError('Keep reasoning separate from the final structured answer.')
    if final.startswith('<answer>'):
        if not final.endswith('</answer>'):raise ValueError('Wait for the complete answer block.')
        final=final[len('<answer>'):-len('</answer>')].strip()
    if final.startswith('```'):
        match=re.fullmatch(r'```(?:json)?\s*(.*?)\s*```',final,re.S)
        if not match:raise ValueError('Use a complete JSON final answer.')
        final=match.group(1)
    return CosmosAnswer.model_validate_json(final)


def prepare_prompt(pipeline, owner, context, *, clock_basis, source_offset_s,
                   tracker_labels, tracker_receipt, input_preparation):
    """Resolve the saved native profile and persistable host input contract."""
    spec=StreamWindow.model_validate({**context,'observations':[]})
    source=pipeline._authorize(owner,spec)
    if clock_basis not in {'source_absolute','segment_relative'}:
        raise ValueError('Declare the clock actually supplied to the stock plugin.')
    if (type(source_offset_s) not in (int,float) or not math.isfinite(source_offset_s)
            or source_offset_s<0 or (clock_basis=='source_absolute' and source_offset_s!=0)
            or (clock_basis=='segment_relative' and source_offset_s!=spec.start_s)):
        raise ValueError('Use the verified source offset from the prepared input segment.')
    if not isinstance(tracker_labels,dict) or len(tracker_labels)>500 or not all(isinstance(k,str) and 0<len(k)<=100 and isinstance(v,str) and 0<len(v)<=100 for k,v in tracker_labels.items()):
        raise ValueError('Preserve the exact visible tracker label mapping.')
    if len(set(tracker_labels.values()))!=len(tracker_labels):raise ValueError('Use one visible label for each exact tracker identity.')
    if (tracker_receipt.get('source_id')!=spec.source_id or tracker_receipt.get('source_sha256')!=source.get('sha256')
            or 'camera_id' not in tracker_receipt or tracker_receipt['camera_id']!=source.get('camera_id')):
        raise ValueError('Bind the tracker sidecar to this registered source and camera.')
    if tracker_receipt.get('identity_scope')!='recording_and_camera_local' or not isinstance(tracker_receipt.get('frames'),list):
        raise ValueError('Use the actual source-clock tracker observations.')
    if len(canonical(tracker_receipt).encode())>2_000_000:raise ValueError('Use the bounded tracker window receipt.')
    seen=set()
    for frame in tracker_receipt['frames']:
        t=frame.get('video_time_s')
        if type(t) not in (int,float) or not math.isfinite(t):raise ValueError('Use finite original tracker frame times.')
        if spec.start_s<=t<=spec.end_s:
            for track in frame.get('tracks',[]):
                identity=track.get('track_id')
                if isinstance(identity,bool) or not isinstance(identity,(str,int)):raise ValueError('Keep exact tracker IDs.')
                seen.add(str(identity))
    if any(v not in seen for v in tracker_labels.values()):raise ValueError('Supply labels actually observed in this window.')
    if (not isinstance(input_preparation,dict) or input_preparation.get('source_sha256')!=source['sha256']
            or input_preparation.get('tracker_sha256')!=digest(tracker_receipt)
            or input_preparation.get('rendered_labels')!=tracker_labels
            or input_preparation.get('clock_basis')!=clock_basis or input_preparation.get('source_offset_s')!=source_offset_s
            or input_preparation.get('osd_renderer')!='nvdsosd'
            or any(not re.fullmatch(r'[a-f0-9]{64}',str(input_preparation.get(key,''))) for key in ('model_input_sha256','osd_configuration_sha256'))):
        raise ValueError('Bind model input bytes, official OSD labels and source clock to their host preparation receipt.')
    profile=pipeline.monitoring.get_profile(owner,spec.profile_id,revision=spec.profile_revision)
    contract={'context':spec.model_dump(exclude={'observations','item_claims'}),'source_sha256':source['sha256'],
        'clock_basis':clock_basis,'source_offset_s':source_offset_s,'tracker_labels':tracker_labels,
        'tracker_sha256':digest(tracker_receipt),'profile_sha256':digest(profile),
        'input_preparation_sha256':digest(input_preparation)}
    contract_sha=digest(contract)
    recording=source.get('video_id') or source['id']
    hints=[]
    for event in reversed(pipeline.monitoring.list_events(owner,profile['dataset'],profile_ids=[profile['id']],limit=100)):
        if event['recording_id']!=recording or event['source']['end_s']>spec.start_s:continue
        proof=pipeline.model_claim(owner,event['id'])
        if proof:hints.append({'event_key':event['event_key'],'person_track_id':event.get('track_id'),
            'description':event.get('object_description'),'quantity_estimate':proof['claim']['quantity'],
            'identity_status':'candidate','source_end_s':event['source']['end_s']})
    schema=CosmosAnswer.model_json_schema()
    prompt=('Describe directly visible actions for every visible tracked person and scene change. '
        'Use the saved event vocabulary and return one final JSON object matching the schema. '
        'Report start_s/end_s in seconds on the timestamps supplied with this input, including its final visible frame interval. '
        'Use person_id exactly as a visible label listed below, or null when attribution is uncertain. '
        'Give each event a distinct short key. Keep object_ref local to this input and reuse it only when you can associate the same visible object. '
        'Describe visible packaging, color, shape and legible markings; keep uncertain details explicit. '
        'item_claim is null unless an explicit visible sale-unit quantity and item association are supported. '
        'Touching/examining alone does not establish possession or quantity. Never invent a unit from an event count. '
        'Use same_item or distinct_item with one earlier event key only when that relationship is visually supported; otherwise use unresolved. '
        'Earlier hints are model estimates, not verified objects. Do not assume a candidate SKU or price. '
        'Cash handover/card presentation are physical actions; purchase completion and payment amounts require separate transaction evidence. '
        'For sparse input, report supported observed changes rather than filling unseen intervals. '
        'Put-back requires the same item returning to its earlier place; put-down elsewhere and ambiguous handovers remain explicit.\n'
        +'Host input contract: '+contract_sha+'\n'
        +'Vocabulary: '+canonical(profile['vocabulary'])+'\n'
        +'Business criteria: '+canonical(profile.get('criteria',[]))+'\n'
        +'Visible labels to exact tracker IDs: '+canonical(tracker_labels)+'\n'
        +'Earlier candidate item observations: '+canonical(hints)+'\n'
        +'Output schema: '+canonical(schema)+'\n')
    # nvvllmvlm uses Python str.format; only its native timestamps placeholder is live.
    template=prompt.replace('{','{{').replace('}','}}')+'Sampled input timestamps: {timestamps}\n'
    return {'adapter':'nvvllmvlm-observations-v1','context':spec.model_dump(exclude={'observations','item_claims'}),
        'source_sha256':source['sha256'],'clock_basis':clock_basis,'source_offset_s':source_offset_s,
        'tracker_labels':tracker_labels,'tracker_receipt':tracker_receipt,'tracker_sha256':digest(tracker_receipt),
        'profile_sha256':digest(profile),'user_prompt':template,'prompt_sha256':hashlib.sha256(template.encode()).hexdigest(),
        'max_tokens_minimum':4096,'input_preparation':input_preparation,
        'input_preparation_sha256':digest(input_preparation),'input_contract_sha256':contract_sha}


def _admit_result(pipeline, owner, prepared, envelope, input_trace):
    """Validate the stock signal envelope, original trace and model's structured final."""
    context=dict(prepared['context']);spec=StreamWindow.model_validate({**context,'observations':[]})
    source=pipeline._authorize(owner,spec)
    profile=pipeline.monitoring.get_profile(owner,spec.profile_id,revision=spec.profile_revision)
    contract={key:prepared[key] for key in ('context','source_sha256','clock_basis','source_offset_s',
        'tracker_labels','tracker_sha256','profile_sha256','input_preparation_sha256')}
    if (digest(contract)!=prepared['input_contract_sha256']
            or 'Host input contract: '+prepared['input_contract_sha256']+'\n' not in prepared['user_prompt']
            or digest(prepared['input_preparation'])!=prepared['input_preparation_sha256']
            or input_trace.get('input_preparation_sha256')!=prepared['input_preparation_sha256']):
        raise ValueError('Bind the actual input bytes, labels, clock and source to the rendered prompt.')
    if source.get('sha256')!=prepared['source_sha256'] or digest(profile)!=prepared['profile_sha256']:
        raise ValueError('Keep the prepared source bytes and saved profile revision.')
    if digest(prepared['tracker_receipt'])!=prepared['tracker_sha256']:
        raise ValueError('Preserve the actual tracker input receipt.')
    if hashlib.sha256(prepared['user_prompt'].encode()).hexdigest()!=prepared['prompt_sha256']:
        raise ValueError('Preserve the prepared stock prompt template.')
    trace_prompt=input_trace.get('effective_prompt_config',{}).get('user_prompt')
    if trace_prompt!=prepared['user_prompt']:
        raise ValueError('Use the prepared saved-profile prompt in the actual stock plugin.')
    if envelope.get('stream_id')!=input_trace.get('stream_id'):
        raise ValueError('Match the response to its original stream input.')
    for field in ('segment_start_s','segment_end_s'):
        if envelope.get(field)!=input_trace.get(field):raise ValueError('Match the stock signal and input segment bounds.')
    times=input_trace.get('frame_times_s')
    if not isinstance(times,list) or not 1<=len(times)<=1000 or any(type(t) not in (int,float) or not math.isfinite(t) for t in times) or times!=sorted(times):
        raise ValueError('Keep the actual sampled input timestamps in order.')
    metadata=input_trace.get('video_metadata')
    if isinstance(metadata,list):metadata=metadata[0] if len(metadata)==1 else None
    fps=metadata.get('fps') if isinstance(metadata,dict) else None
    if type(fps) not in (int,float) or not math.isfinite(fps) or not 0<fps<=240:
        raise ValueError('Keep the stock input sampling rate with its timestamps.')
    shape=input_trace.get('actual_video_argument_shape')
    hashes=input_trace.get('frame_hashes_sha256')
    if (not isinstance(shape,list) or len(shape)!=4 or any(type(n) is not int or n<=0 for n in shape)
            or shape[0]!=len(times) or shape[1]!=3 or shape[2]>8192 or shape[3]>8192
            or metadata.get('total_num_frames')!=len(times)
            or not isinstance(hashes,list) or len(hashes)!=len(times)
            or any(not isinstance(h,str) or not re.fullmatch(r'[a-f0-9]{64}',h) for h in hashes)):
        raise ValueError('Keep actual input frame count, pixels, sampling rate and frame hashes.')
    context['sampling']=f'stock {fps:g}fps; {shape[3]}x{shape[2]}; {len(times)} frames; '+prepared['clock_basis']
    offset=prepared['source_offset_s']
    segment_start=envelope.get('segment_start_s');segment_end=envelope.get('segment_end_s')
    if (type(segment_start) not in (int,float) or type(segment_end) not in (int,float)
            or not all(math.isfinite(t) for t in (segment_start,segment_end))
            or abs(segment_start-(spec.start_s-offset))>1e-6
            or not segment_start<segment_end<=segment_start+60 or segment_end<times[-1]):
        raise ValueError('Bind the native segment to its prepared source window.')
    serialized=input_trace.get('serialized_prompt')
    expected=prepared['user_prompt'].format(num_frames=len(times),stream_id=envelope['stream_id'],timestamps=' '.join(f'{t:.2f}s' for t in times))
    if serialized is not None and (not isinstance(serialized,str) or expected not in serialized):
        raise ValueError('Preserve the actual formatted user prompt supplied to the model.')
    elapsed=input_trace.get('llm_generate_elapsed_s')
    if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<0:
        raise ValueError('Keep the measured inference duration in its input trace.')
    context['processing_seconds']=elapsed
    if times[0]+offset<spec.start_s or times[-1]+offset>spec.end_s:
        raise ValueError('Keep the declared input clock within its registered source window.')
    if prepared['clock_basis']=='source_absolute' and offset!=0 or prepared['clock_basis']=='segment_relative' and (offset!=spec.start_s or abs(times[0])>1/fps):
        raise ValueError('Use the prepared clock, without inferring an offset from the answer.')
    end_limit=min(times[-1]+1/fps,spec.end_s-offset)
    raw=envelope.get('result_text')
    native={'prepared':prepared,'envelope':envelope,'input_trace':input_trace,
        'envelope_sha256':digest(envelope),'input_trace_sha256':digest(input_trace),
        'timestamp_policy':'explicit_input_clock_with_one_sample_display_interval'}
    if len(canonical(native).encode())>4_000_000:raise ValueError('Use a bounded native response receipt.')
    answer=final_answer(raw);observations=[];claims=[];keys=set()
    namespace='w'+digest([spec.source_id,spec.profile_id,spec.profile_revision,spec.start_s,spec.end_s])[:18]+'-'
    for event in answer.events:
        if event.key in keys or event.end_s<event.start_s or event.start_s<times[0] or event.end_s>end_limit:
            raise ValueError('Keep distinct event keys and observed input-clock intervals.')
        keys.add(event.key)
        if event.type in {'left_with_purchase','left_without_purchase','paid','payment_completed'}:
            raise ValueError('Use transaction evidence for financial outcomes.')
        person=None
        if event.person_id is not None:
            person=prepared['tracker_labels'].get(event.person_id)
            if person is None:raise ValueError('Use the exact supplied visible tracker label.')
            if not any(event.start_s+offset<=f['video_time_s']<=event.end_s+offset and
                any(str(t['track_id'])==person for t in f.get('tracks',[])) for f in prepared['tracker_receipt']['frames']):
                raise ValueError('Associate this event with an observed tracker in its source interval.')
        event_key=namespace+event.key
        object_ref=namespace+event.object_ref if event.object_ref else None
        observation={'profile_id':spec.profile_id,'profile_revision':spec.profile_revision,
            'event_key':event_key,'type':event.type,'description':event.description,
            'source':{'clip_id':spec.source_id,'start_s':event.start_s+offset,'end_s':event.end_s+offset},
            'track_id':person,'object_ref':object_ref,'object_description':event.object_description,'bbox':None}
        observations.append(observation)
        if event.item_claim is not None:
            claim=event.item_claim.model_dump()
            if claim['prior_event_key'] in {e.key for e in answer.events}:
                claim['prior_event_key']=namespace+claim['prior_event_key']
            claims.append(VisionItemClaim.model_validate({'event_key':event_key,'object_ref':object_ref,**claim}).model_dump())
    receipt=pipeline.accept_model_response(owner,context,canonical({'observations':observations,'item_claims':claims}),adapter_receipt=native)
    return receipt


def admit_result(pipeline, owner, prepared, envelope, input_trace):
    """Preserve authorized raw attempts, including rejected clocks/labels/answers."""
    spec=StreamWindow.model_validate({**prepared['context'],'observations':[]})
    pipeline._authorize(owner,spec)
    native={'prepared':prepared,'envelope':envelope,'input_trace':input_trace,
        'envelope_sha256':digest(envelope),'input_trace_sha256':digest(input_trace),
        'timestamp_policy':'explicit_input_clock_with_one_sample_display_interval'}
    body=canonical(native)
    if len(body.encode())>4_000_000:raise ValueError('Use a bounded native response receipt.')
    attempt_id=digest([owner,native])
    with pipeline.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS cosmos_bridge_attempts(id TEXT PRIMARY KEY,owner TEXT,source_id TEXT,body TEXT,status TEXT)')
        db.execute('INSERT OR IGNORE INTO cosmos_bridge_attempts VALUES(?,?,?,?,?)',(attempt_id,owner,spec.source_id,body,'pending'))
    try:receipt=_admit_result(pipeline,owner,prepared,envelope,input_trace)
    except (ValueError,TypeError,KeyError):
        with pipeline.connect() as db:db.execute("UPDATE cosmos_bridge_attempts SET status='quarantined' WHERE id=?",(attempt_id,))
        raise
    with pipeline.connect() as db:db.execute("UPDATE cosmos_bridge_attempts SET status='admitted' WHERE id=?",(attempt_id,))
    return receipt


def connect_stock_results(element, pipeline, owner, *, prepared_for, trace_for, on_receipt):
    """Attach to stock GStreamer's documented vlm-result signal without SDK edits.

    The host runner owns prepared_for/trace_for: they return the actual admitted
    input preparation and observer trace for this stream/window, never model data.
    on_receipt records success; exceptions leave the raw attempt quarantined.
    """
    def receive(_element, stream_id, start_sec, end_sec, result_text):
        prepared=prepared_for(stream_id,start_sec,end_sec)
        trace=trace_for(stream_id,start_sec,end_sec)
        envelope={'stream_id':stream_id,'segment_start_s':start_sec,
                  'segment_end_s':end_sec,'result_text':result_text}
        receipt=admit_result(pipeline,owner,prepared,envelope,trace)
        on_receipt(receipt)
    return element.connect('vlm-result',receive)
