"""Publisher labels for an explicit dataset-reference playback mode.

These records are ground-truth annotations, never Cosmos predictions. A conservative
point-in-box association may attach the publisher's subject to a local NVIDIA track.
The interaction coordinate is a point, not an object box or product identity.
"""
from collections import Counter
from datetime import datetime
import hashlib
import math
from pathlib import Path
from retail_stream_import import load
from stream_ingestion import canonical,StreamWindow

LABELS={'take':('pick_up','Pick up item'),'put':('put_down','Put down item'),'touch':('touch','Touch item')}


def associate_subject(metadata,view,mapping,tracks,start,end):
    faces=metadata.get('content',{}).get('action_cam',{}).get(view,{}).get('face_positions',[])
    indexed={round(datetime.fromisoformat(row['sampled_at']).timestamp(),6):(row['col']*tracks['width'],row['row']*tracks['height']) for row in faces if isinstance(row,dict) and isinstance(row.get('sampled_at'),str) and isinstance(row.get('col'),(int,float)) and isinstance(row.get('row'),(int,float))}
    source_times=mapping['source_frame_timestamps_s'];frame_map={row['frame_index']:row for row in mapping['frames']}
    votes=Counter();samples=0;used=set();evidence=[]
    for frame in tracks['frames']:
        if not start<=frame['video_time_s']<=end:continue
        source=frame_map.get(frame['frame_index'])
        if source is None or source['source_frame_index'] in used:continue
        index=source['source_frame_index'];used.add(index)
        point=indexed.get(round(source_times[index],6))
        if point is None:continue
        samples+=1;x,y=point
        matches=[str(track['track_id']) for track in frame['tracks'] if track['bbox_xyxy'][0]<=x<=track['bbox_xyxy'][2] and track['bbox_xyxy'][1]<=y<=track['bbox_xyxy'][3]]
        if len(matches)==1:
            votes[matches[0]]+=1;evidence.append({'source_frame_index':index,'time_s':frame['video_time_s'],'track_id':matches[0]})
    winner,count=votes.most_common(1)[0] if votes else (None,0)
    assigned=winner if count>=2 and samples and count/samples>=.8 else None
    return assigned,{'basis':'publisher_subject_point_in_unique_person_box','status':'associated' if assigned else 'unresolved',
        'source_samples':samples,'unique_box_votes':dict(votes),'support':evidence,'identity_scope':'recording_and_camera_local'}


def reference_windows(run_root,records,profile):
    root=Path(run_root);truth_path=root/'retailaction-ground-truth.json'
    if not truth_path.is_file():return {'windows':[],'events_by_source':{},'counts':{'events':0,'associated_events':0}}
    truth=load(truth_path)
    if not isinstance(truth.get('records'),list):return {'windows':[],'events_by_source':{},'counts':{'events':0,'associated_events':0}}
    source_records={r['source_clip_id']:r for r in truth['records']}
    windows=[];events_by_source={}
    for record in records:
        clip=record['clip_id'];source_id='retailaction-'+clip
        original=source_records.get(record['source_clip_id'],{});events=[];definitions=[]
        meta_path=root/'retail-action-raw'/record['source_clip_id']/'metadata.json'
        metadata=load(meta_path) if meta_path.is_file() else {}
        tracks=load(root/'retail-action-nvidia'/clip/'tracks.json')
        mapping=load(root/'retail-action-nvidia'/clip/'source_mapping.json')
        for index,label in enumerate(original.get('events',[])):
            if label['label'] not in LABELS:continue
            start,end=label['start_s'],label['end_s']
            if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in [start,end]) or not 0<=start<=end<=record['duration_s']:continue
            kind,title=LABELS[label['label']]
            track,association=associate_subject(metadata,record['source_view'],mapping,tracks,start,end)
            key='reference-'+hashlib.sha256(canonical([source_id,index,label]).encode()).hexdigest()[:28]
            description=f'Publisher annotation: {title.lower()}.'
            point=label.get('spatial',{}).get('action_cam',{}).get(record['source_view'])
            provenance={'kind':'dataset_reference','publisher':'standard-cognition/RetailAction','label':label['label'],
                'normalized_start':label['normalized_start'],'normalized_end':label['normalized_end'],
                'source_duration_s':original['source_duration_s'],'annotation_scope':'dataset subject of interest',
                'association':association,'interaction_point':point,'object_identity':'unresolved',
                'source':truth['source']}
            definitions.append({'profile_id':profile['id'],'profile_revision':profile['revision'],'event_key':key,
                'type':kind,'description':description,'source':{'clip_id':source_id,'start_s':start,'end_s':end},
                'track_id':track,'object_ref':None,'object_description':None,'bbox':None})
            events.append({'event_id':key,'event':kind,'person_id':track,'description':description,'item_description':None,
                'playback_start_s':start,'playback_end_s':end,'start_s':start,'end_s':end,
                'eligible_for_overlay':True,'provenance':provenance,'review_state':'publisher_annotation'})
        events_by_source[source_id]=events
        if events:
            windows.append(StreamWindow.model_validate({'stream_id':source_id+'-reference','source_id':source_id,
                'profile_id':profile['id'],'profile_revision':profile['revision'],'start_s':0,'end_s':record['duration_s'],
                'input_kind':'dataset_replay','model':'dataset_reference:standard-cognition/RetailAction',
                'observation_origin':'dataset_reference','sampling':'Publisher normalized labels on original source duration; separate reference layer',
                'processing_seconds':0,'observations':definitions}).model_dump())
    flat=[e for events in events_by_source.values() for e in events]
    return {'windows':windows,'events_by_source':events_by_source,'counts':{'events':len(flat),'associated_events':sum(e['person_id'] is not None for e in flat)}}
