import hashlib
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from retail_stream_import import audit_event, final_events, event_key, timestamp
from stream_ingestion import canonical


def raw(**changes):
    return dict(start='00:12.00',end='00:13.00',person_id='Person 1',type='touch',
        description='Touches the blue packet on the shelf.',product_description='Blue packet',**changes)


def audit(event, **changes):
    review={'decision':'verified','reviewer':'visual-review','evidence':['frame12.jpg','frame13.jpg'],
        'raw_sha256':hashlib.sha256(canonical(event).encode()).hexdigest()}
    options=dict(source_id='retailaction-000027-rank0',output_index=1,event_index=0,
        start_s=10,end_s=14,observed_start_s=10,observed_end_s=13.875,
        tracks={'frames':[{'video_time_s':12,'tracks':[{'track_id':1}]}]},
        profile_id='profile-one',profile_revision=1,review=review)
    options.update(changes)
    return audit_event(event,**options)


def test_explicit_review_accepts_unchanged_observed_action():
    result=audit(raw())
    assert result['status']=='reviewed_observation'
    assert result['observation']['track_id']=='1'
    assert result['observation']['source']['start_s']==12
    assert result['observation']['object_description']=='Blue packet'
    assert result['observation']['bbox'] is None


def test_later_window_zero_clock_never_shifted():
    event=raw();event.update(start='00:00.12',end='00:00.50')
    result=audit(event)
    assert 'outside_observed_window' in result['issues']
    assert result['raw_start']=='00:00.12' and result['parsed_start_s']==.12
    assert result['observation'] is None


@pytest.mark.parametrize('text',['Places it in a hypothetical basket.','Hands over likely cash.','Completing the payment process.'])
def test_speculation_cannot_be_approved_by_generic_review(text):
    event=raw();event['description']=text
    assert 'speculative_or_inferred_outcome' in audit(event)['issues']
    assert audit(event)['observation'] is None


def test_optional_app_field_requires_explicit_acknowledgement():
    event=raw();del event['product_description']
    result=audit(event)
    assert result['observation'] is None
    review=result['review'];review['acknowledged_issues']=['missing_product_description']
    accepted=audit(event,review=review)
    assert accepted['observation']['object_description'] is None
    assert 'missing_product_description' in accepted['issues']


def test_camera_identity_and_review_hash_cannot_be_reused_for_changed_event():
    event=raw();other=raw();other['description']='Lifts a different product.'
    assert event_key('camera-one',1,0,event)!=event_key('camera-two',1,0,event)
    review=audit(other)['review']
    assert audit(event,review=review)['observation'] is None
    assert 'track_absent_in_event_interval' in audit(event,tracks={'frames':[]})['issues']
    assert audit(event,review=None)['observation'] is None


def test_original_final_answer_and_timestamps_are_strict():
    assert final_events('<think>private reasoning</think>\n```json\n[]\n```')==[]
    for value in ['00:61',12,float('nan'),'12 seconds']:
        with pytest.raises(ValueError):timestamp(value)
    with pytest.raises(ValueError):final_events('{"events":[]}')
