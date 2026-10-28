"""Exact inspected bytes remain private, durable and atomically admitted."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import inspection_previews as previews

SCOPE = dict(owner_principal_id='alice', chat_id='chat-a', dataset='cafe', clip_id='clip-1')
# Marker-valid synthetic payload: the store deliberately does not decode JPEGs.
JPEG = b'\xff\xd8\xffsynthetic exact sampled bytes\xff\xd9'


def frame(raw=JPEG, **changes):
    return dict(image_bytes=raw, time=1.25, basis='source_frame_pts', width=1920, height=1080, **changes)


def saved(store, frames=None, **changes):
    return store.save(**{**SCOPE, **changes}, frames=frames if frames is not None else [frame()])


def lookup(store, descriptor, **changes):
    receipt, index = descriptor['url'].split('/')[-2:]
    return store.get(owner_principal_id='alice', receipt_id=receipt, index=int(index[:-4]), **changes)


def test_absent_read_leaves_state_absent(tmp_path):
    state = tmp_path / 'absent'
    store = previews.InspectionPreviews(state, create=False)
    assert store.get(owner_principal_id='alice', receipt_id='a' * 32, index=0) is None
    assert not state.exists()


def test_exact_bytes_scope_reopen_and_private_repr(tmp_path):
    store = previews.InspectionPreviews(tmp_path / 'company')
    descriptors = saved(store)
    assert set(descriptors[0]) == {'url', 'time', 'clip_id', 'sha256', 'basis', 'width', 'height'}
    assert descriptors[0]['sha256'] == hashlib.sha256(JPEG).hexdigest()
    assert 'image_bytes' not in json.dumps(descriptors)
    reopened = previews.InspectionPreviews(store.state)
    record = lookup(reopened, descriptors[0], chat_id='chat-a')
    assert record['image_bytes'] == JPEG and record['dataset'] == 'cafe'
    assert record['owner_principal_id'] == 'alice' and record['time'] == 1.25
    assert 'synthetic exact' not in repr(record) and 'synthetic exact' not in str(record)
    assert lookup(reopened, descriptors[0], chat_id='chat-b') is None
    receipt = descriptors[0]['url'].split('/')[-2]
    assert reopened.get(owner_principal_id='bob', receipt_id=receipt, index=0) is None
    assert previews.InspectionPreviews(tmp_path / 'another-company').get(
        owner_principal_id='alice', receipt_id=receipt, index=0) is None
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.state.stat().st_mode & 0o777 == 0o700


def test_dedup_binds_every_scope_and_frame_metadata(tmp_path):
    store = previews.InspectionPreviews(tmp_path)
    first = saved(store)
    assert saved(store) == first
    variants = [saved(store, chat_id='chat-b'), saved(store, owner_principal_id='bob'),
        saved(store, dataset='warehouse'), saved(store, clip_id='clip-2'),
        saved(store, frames=[{**frame(), 'time':2}]),
        saved(store, frames=[{**frame(), 'basis':'sampled_frame'}]),
        saved(store, frames=[frame(JPEG + b'\xff\xd9')])]
    assert len({x[0]['url'] for x in [first, *variants]}) == 8


@pytest.mark.parametrize('raw', [b'', b'ordinary bytes', b'\xff\xd8\xff\xd9', JPEG[:-2], b'\xff\xd8payload\xff\xd9', bytearray(JPEG)])
def test_jpeg_marker_and_type_validation_precedes_storage(tmp_path, raw):
    store = previews.InspectionPreviews(tmp_path / 'absent')
    with pytest.raises(previews.PreviewError):
        saved(store, frames=[frame(raw)])
    assert not store.state.exists()


@pytest.mark.parametrize('change', [{'time':float('nan')}, {'time':float('inf')}, {'time':True},
    {'time':-1}, {'time':10**1000}, {'width':True}, {'height':0}, {'basis':'private\npath'}, {'basis':'x' * 129}])
def test_malformed_metadata_is_controlled(tmp_path, change):
    with pytest.raises(previews.PreviewError):
        saved(previews.InspectionPreviews(tmp_path), frames=[{**frame(), **change}])


def test_per_frame_receipt_and_frame_count_caps(tmp_path, monkeypatch):
    store = previews.InspectionPreviews(tmp_path)
    monkeypatch.setattr(previews, 'MAX_FRAME_BYTES', len(JPEG))
    monkeypatch.setattr(previews, 'MAX_RECEIPT_BYTES', len(JPEG) * 2)
    saved(store, frames=[frame(), frame()])
    for frames in [[], [frame()] * 5, [frame()] * 3, [frame(JPEG + b'\xff\xd9')]]:
        with pytest.raises(previews.PreviewError):saved(store, frames=frames)


def test_company_quota_serializes_concurrent_saves_and_dedup_is_free(tmp_path, monkeypatch):
    monkeypatch.setattr(previews, 'MAX_COMPANY_BYTES', len(JPEG))
    barrier = threading.Barrier(2)
    def attempt(clip):
        store = previews.InspectionPreviews(tmp_path)
        barrier.wait()
        try:return saved(store, clip_id=clip)
        except previews.PreviewError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['clip-a', 'clip-b']))
    assert sum(result is not None for result in results) == 1
    selected = next(result for result in results if result)
    assert saved(previews.InspectionPreviews(tmp_path), clip_id=selected[0]['clip_id']) == selected


def test_guard_blocks_admission_and_rolls_back_whole_receipt(tmp_path):
    store = previews.InspectionPreviews(tmp_path / 'company')
    with pytest.raises(previews.PreviewError):saved(store, guard=lambda:False)
    assert not store.state.exists()
    calls = []
    def revoked_after_insert():
        calls.append(True)
        return len(calls) < 3
    with pytest.raises(previews.PreviewError):saved(store, frames=[frame(), frame()], guard=revoked_after_insert)
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM frames').fetchone()[0] == 0
    assert len(saved(store)) == 1


def test_second_frame_sql_failure_rolls_back_receipt_and_first_frame(tmp_path):
    store = previews.InspectionPreviews(tmp_path, create=True)
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON frames WHEN NEW.position=1
            BEGIN SELECT RAISE(ABORT,'synthetic private storage failure'); END""")
    with pytest.raises(previews.PreviewError) as raised:saved(store, frames=[frame(), frame()])
    assert 'synthetic private' not in str(raised.value)
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM frames').fetchone()[0] == 0


def test_optional_metadata_and_receipt_count_allowance(tmp_path, monkeypatch):
    monkeypatch.setattr(previews, 'MAX_RECEIPTS', 1)
    store = previews.InspectionPreviews(tmp_path)
    descriptor = saved(store, frames=[{'image_bytes':JPEG, 'time':0}])[0]
    assert set(descriptor) == {'url', 'time', 'clip_id', 'sha256'}
    assert lookup(store, descriptor)['image_bytes'] == JPEG
    assert saved(store, frames=[{'image_bytes':JPEG, 'time':0.0}])[0] == descriptor
    with pytest.raises(previews.PreviewError):saved(store, clip_id='second')
    assert len(previews.tool_previews('inspect_clip',
        [{'type':'inputText','text':json.dumps({'frame_previews':[descriptor]})}], True)) == 1


def test_existing_lookup_is_read_only(tmp_path):
    store = previews.InspectionPreviews(tmp_path)
    descriptor = saved(store)[0]
    before = {path.name:(path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.iterdir()}
    assert lookup(previews.InspectionPreviews(tmp_path, create=False), descriptor)
    after = {path.name:(path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.iterdir()}
    assert after == before


def test_guard_is_rechecked_after_waiting_for_writer(tmp_path):
    store = previews.InspectionPreviews(tmp_path, create=True)
    admitted, live = threading.Event(), threading.Event()
    live.set()
    def guard():
        admitted.set()
        return live.is_set()
    db = sqlite3.connect(store.path)
    db.execute('BEGIN IMMEDIATE')
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(saved, store, guard=guard)
        assert admitted.wait(2)
        live.clear(); db.rollback(); db.close()
        with pytest.raises(previews.PreviewError):task.result(timeout=5)
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0


def test_immutable_rows_and_symlink_database_are_rejected(tmp_path):
    store = previews.InspectionPreviews(tmp_path / 'company')
    descriptor = saved(store)[0]
    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.DatabaseError):db.execute("UPDATE frames SET image=X'00'")
        with pytest.raises(sqlite3.DatabaseError):db.execute("UPDATE receipts SET owner='bob'")
    alias = tmp_path / 'alias'; alias.mkdir()
    (alias / store.path.name).symlink_to(store.path)
    with pytest.raises(previews.PreviewError):saved(previews.InspectionPreviews(alias))
    with pytest.raises(previews.PreviewError):lookup(previews.InspectionPreviews(alias), descriptor)


def test_lookup_integrity_and_storage_errors_are_bounded(tmp_path):
    store = previews.InspectionPreviews(tmp_path)
    descriptor = saved(store)[0]
    with sqlite3.connect(store.path) as db:
        db.execute('DROP TRIGGER immutable_frame_update')
        db.execute('UPDATE frames SET image=?', (b'private invalid bytes',))
    with pytest.raises(previews.PreviewError) as raised:lookup(store, descriptor)
    assert 'private invalid bytes' not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_tool_projection_only_accepts_host_inspection_metadata(tmp_path):
    descriptors = saved(previews.InspectionPreviews(tmp_path))
    content = [{'type':'inputText', 'text':json.dumps({'frame_previews':descriptors})},
        {'type':'inputImage', 'imageUrl':'data:image/jpeg;base64,PRIVATE'}]
    assert previews.tool_previews('inspect_clip', content, True) == descriptors
    assert 'PRIVATE' not in repr(previews.tool_previews('inspect_clip', content, True))
    assert previews.tool_previews('provider-tool', content, True) == []
    assert previews.tool_previews('inspect_clip', content, False) == []
    assert previews.tool_previews('inspect_clip', [{'type':'inputText','text':'unavailable'}, *content], True) == []
    for change in [{'url':'https://example.com/frame.jpg'}, {'url':'data:image/jpeg;base64,x'},
        {'url':'/api/inspection-previews/' + 'a'*32 + '/4.jpg'}, {'sha256':'x'},
        {'time':float('nan')}, {'image_bytes':'private'}, {'clip_id':'../private'}]:
        bad = [{**descriptors[0], **change}]
        assert previews.tool_previews('inspect_clip', [{'type':'inputText','text':json.dumps({'frame_previews':bad})}], True) == []
    for text in ['[' * 2000, 'x' * 65537, json.dumps({'frame_previews':descriptors * 5})]:
        assert previews.tool_previews('inspect_clip', [{'type':'inputText','text':text}], True) == []
    duplicate = json.dumps({'frame_previews':descriptors})[:-1] + ',"frame_previews":[]}'
    assert previews.tool_previews('inspect_clip', [{'type':'inputText','text':duplicate}], True) == []
    assert previews.tool_previews('inspect_clip', [{'type':'inputText','text':'\ud800'}], True) == []
    for malformed in [None, {}, 'text', [None], [{'type':'inputImage','imageUrl':'data:private'}]]:
        assert previews.tool_previews('inspect_clip', malformed, True) == []


def test_projection_rejects_mixed_receipts_clips_and_out_of_order_frames(tmp_path):
    store = previews.InspectionPreviews(tmp_path)
    frames = saved(store, frames=[frame(), frame()])
    other = saved(store, clip_id='clip-2', frames=[frame(), frame()])
    for selected in [[frames[0], other[1]], [frames[1], frames[0]],
            [frames[0], {**frames[1], 'clip_id':'clip-2'}]]:
        assert previews.tool_previews('inspect_clip',
            [{'type':'inputText','text':json.dumps({'frame_previews':selected})}], True) == []
