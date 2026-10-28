"""Real event-store integration: replay recovery, identity and source authority."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from monitoring import MonitoringStore
from stream_ingestion import StreamIngestion


def setup(tmp_path):
    sources = {key: {'id': key, 'video_id': key, 'dataset': 'retaily',
        'start': 0, 'end': 20, 'camera_id': 'camera-a', 'sha256': 'a'*64}
        for key in ['recording-a', 'recording-b']}
    monitoring = MonitoringStore(tmp_path, source_lookup=lambda owner, key: sources.get(key) if owner == 'peter' else None)
    profile = monitoring.save_profile('peter', {'name': 'Shop interactions', 'dataset': 'retaily',
        'vocabulary': [{'type': 'pick_up', 'description': 'Visibly lifts an item.'}]}, datasets=['retaily'])
    def window(start=0, source='recording-a'):
        return {'stream_id': 'camera-a', 'source_id': source, 'profile_id': profile['id'],
            'profile_revision': 1, 'start_s': start, 'end_s': start+10,
            'input_kind': 'dataset_replay', 'model': 'nvidia/Cosmos-Reason2-8B',
            'sampling': '8fps_source_mapping', 'processing_seconds': 3.2,
            'observations': [{'profile_id': profile['id'], 'profile_revision': 1,
                'event_key': 'pickup-'+str(start), 'type': 'pick_up',
                'description': 'Lifts a small packet.', 'track_id': '1', 'object_ref': 'packet-1',
                'source': {'clip_id': source, 'start_s': start+2, 'end_s': start+4}}]}
    return sources, monitoring, StreamIngestion(tmp_path, monitoring), window


def test_replay_persists_windows_events_and_automatic_product_jobs(tmp_path):
    _, monitoring, pipeline, window = setup(tmp_path)
    first = pipeline.accept('peter', window())
    second = pipeline.accept('peter', window(10))
    events = monitoring.list_events('peter', 'retaily')
    assert len(events) == 2
    assert events[0]['visit_id'] == events[1]['visit_id']
    assert all(e['captured_at'] is None for e in events)
    restarted = StreamIngestion(tmp_path, monitoring)
    assert restarted.accept('peter', window()) == first
    calls = []
    handler = lambda event, payload, key: calls.append((event, payload, key)) or {'saved': True}
    work = restarted.drain('peter', {'inspect_product': handler, 'project_event': handler})
    assert len(work) == 4 and all(w['status'] == 'done' for w in work)
    assert restarted.drain('peter', {'inspect_product': handler}) == []
    assert len(restarted.history('peter', 'camera-a')) == 2
    assert second['source_end_s'] == 20


def test_invalid_or_changed_window_does_not_silently_rewrite_history(tmp_path):
    _, monitoring, pipeline, window = setup(tmp_path)
    bad = window(); bad['observations'][0]['source']['end_s'] = 11
    with pytest.raises(ValueError): pipeline.accept('peter', bad)
    assert monitoring.list_events('peter', 'retaily') == []
    with pytest.raises(ValueError): pipeline.accept('peter', window(10))
    pipeline.accept('peter', window())
    changed = window(); changed['observations'][0]['description'] = 'Another observation.'
    with pytest.raises(ValueError): pipeline.accept('peter', changed)
    assert len(monitoring.list_events('peter', 'retaily')) == 1


def test_distinct_recordings_and_owners_remain_separate(tmp_path):
    _, monitoring, pipeline, window = setup(tmp_path)
    pipeline.accept('peter', window())
    pipeline.accept('peter', window(source='recording-b'))
    events = monitoring.list_events('peter', 'retaily')
    assert len({e['visit_id'] for e in events}) == 2
    assert pipeline.history('another', 'camera-a') == []
    assert pipeline.drain('another', {'inspect_product': lambda *args: {}}) == []
    with pytest.raises(ValueError): pipeline.accept('another', window())


def test_revoked_source_stops_history_and_followup_work(tmp_path):
    sources, _, pipeline, window = setup(tmp_path)
    pipeline.accept('peter', window())
    sources.clear()
    assert pipeline.history('peter', 'camera-a') == []
    calls = []
    result = pipeline.drain('peter', {'inspect_product': lambda *args: calls.append(args)})
    assert result[0]['status'] == 'failed' and calls == []


def test_failed_projection_recovers_without_duplicate_events(tmp_path):
    _, monitoring, pipeline, window = setup(tmp_path)
    original = monitoring.commit_event
    def fail_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('interrupted after durable event commit')
    monitoring.commit_event = fail_after_commit
    with pytest.raises(RuntimeError): pipeline.accept('peter', window())
    monitoring.commit_event = original
    pipeline.accept('peter', window())
    assert len(monitoring.list_events('peter', 'retaily')) == 1
    assert pipeline.history('peter', 'camera-a')[0]['status'] == 'committed'


def test_reference_labels_never_start_product_inspection_including_old_jobs(tmp_path):
    _, monitoring, pipeline, window = setup(tmp_path)
    reference = window()
    reference['observation_origin'] = 'dataset_reference'
    receipt = pipeline.accept('peter', reference)
    event_id = receipt['event_ids'][0]
    with pipeline.connect() as db:
        queued = db.execute("SELECT kind FROM stream_work").fetchall()
    assert [row['kind'] for row in queued] == ['project_event']
    # An older deployment may have queued a paid image job before this rule.
    with pipeline.connect() as db:
        db.execute("INSERT INTO stream_work(id,owner,window_id,event_id,kind,body,status) VALUES(?,?,?,?,?,?,?)",
                   ('legacy-reference-inspection', 'peter', receipt['window_id'], event_id,
                    'inspect_product', '{}', 'pending'))
    calls = []
    result = pipeline.drain('peter', {'inspect_product': lambda *args: calls.append(args)})
    assert calls == []
    assert result == [{'id': 'legacy-reference-inspection', 'kind': 'inspect_product', 'status': 'skipped'}]
    assert pipeline.history('peter', 'camera-a')[0]['observation_origin'] == 'dataset_reference'


def test_concurrent_drain_waits_for_committed_window(tmp_path):
    """An accept/drain race cannot mark an unfinished model estimate as completed."""
    _,monitoring,pipeline,window=setup(tmp_path)
    original=pipeline.connect
    original_drain=pipeline.drain
    calls=[];between=[];in_probe=False
    class Connection:
        def __init__(self,db):self.db=db
        def execute(self,sql,*args):
            nonlocal in_probe
            if sql.startswith('UPDATE stream_windows SET status=') and not in_probe:
                in_probe=True
                try:
                    between.extend(original_drain('peter',{'project_event':lambda *args:calls.append(args)},limit=10))
                finally:in_probe=False
            return self.db.execute(sql,*args)
        def __getattr__(self,name):return getattr(self.db,name)
        def __enter__(self):self.db.__enter__();return self
        def __exit__(self,*args):return self.db.__exit__(*args)
    pipeline.connect=lambda:Connection(original())
    pipeline.accept('peter',window())
    assert between==[] and calls==[]
    assert original_drain('peter',{'project_event':lambda *args:{'saved':True}})[0]['status']=='done'
