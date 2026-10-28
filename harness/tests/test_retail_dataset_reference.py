from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
from retail_dataset_reference import associate_subject


def test_ambiguous_overlapping_ids_remain_unassigned():
    metadata={'content':{'action_cam':{'rank0':{'face_positions':[
        {'col':.3,'row':.3,'sampled_at':f'1970-01-01T00:00:0{i}+00:00'} for i in range(3)]}}}}
    mapping={'source_frame_timestamps_s':[0,1,2],'frames':[{'frame_index':i,'source_frame_index':i} for i in range(3)]}
    tracks={'width':600,'height':600,'frames':[{'frame_index':i,'video_time_s':i,
        'tracks':[{'track_id':id,'bbox_xyxy':[100,100,300,400]} for id in [1,2]]} for i in range(3)]}
    track,receipt=associate_subject(metadata,'rank0',mapping,tracks,0,2)
    assert track is None and receipt['status']=='unresolved'
    assert receipt['unique_box_votes']=={}


def test_held_repeated_frame_is_one_association_vote():
    metadata={'content':{'action_cam':{'rank0':{'face_positions':[{'col':.3,'row':.3,'sampled_at':'1970-01-01T00:00:00+00:00'}]}}}}
    mapping={'source_frame_timestamps_s':[0],'frames':[{'frame_index':i,'source_frame_index':0} for i in range(8)]}
    tracks={'width':600,'height':600,'frames':[{'frame_index':i,'video_time_s':i/8,
        'tracks':[{'track_id':1,'bbox_xyxy':[100,100,300,400]}]} for i in range(8)]}
    track,receipt=associate_subject(metadata,'rank0',mapping,tracks,0,1)
    assert track is None and receipt['source_samples']==1
