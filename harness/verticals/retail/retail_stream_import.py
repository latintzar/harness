"""Audit preserved NVIDIA outputs before admitting reviewed RetailAction observations.

This host-only adapter never changes model timestamps, joins camera identities, uses
labels as predictions, or manufactures object localization. Review receipts are
separate from immutable raw answers. A review may acknowledge an omitted optional
app field; it cannot override invalid clocks, unknown IDs or speculative outcomes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from stream_ingestion import StreamWindow, canonical

VOCABULARY = tuple(('touch pick_up put_down put_back rotate_item hand_over place_in_basket carry '
    'drop_item entered_queue left_queue reached_checkout handed_cash presented_card left_checkout '
    'left_with_purchase left_without_purchase enter_view leave_view sit_down stand_up exit_with_item '
    'conceal_item remove_packaging shelf_empty display_item_added display_item_removed product_misplaced '
    'dirty_area dirty_table_occupied table_cleared spill bin_full other_observation').split())
SCENE_EVENTS = frozenset(('shelf_empty display_item_added display_item_removed product_misplaced dirty_area '
    'dirty_table_occupied table_cleared spill bin_full other_observation').split())
SPECULATION = re.compile(r'\b(hypothetical|implied|likely|possibly|presumably|probably|prepare|preparing|intent|payment process|completing (?:the )?payment|transactional action)\b', re.I)
REQUIRED = ('start', 'end', 'person_id', 'type', 'description', 'product_description')


def load(path):
    return json.loads(Path(path).read_text())


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{2,}:[0-5]\d(?:\.\d+)?', value):
        raise ValueError('Use the original mm:ss timestamp.')
    minutes, seconds = value.split(':')
    return int(minutes) * 60 + float(seconds)


def final_events(text):
    final = text.rsplit('</think>', 1)[-1].strip()
    if final.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', final, re.S)
        if not match:
            raise ValueError('The final answer must be a complete JSON array.')
        final = match.group(1)
    result = json.loads(final)
    if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
        raise ValueError('The final answer must contain event objects.')
    return result


def event_key(source_id, output_index, event_index, raw):
    return 'ra-' + hashlib.sha256(canonical([source_id, output_index, event_index, raw]).encode()).hexdigest()[:32]


def audit_event(raw, *, source_id, output_index, event_index, start_s, end_s,
                observed_start_s, observed_end_s, tracks, profile_id, profile_revision,
                review=None, vocabulary=VOCABULARY):
    key = event_key(source_id, output_index, event_index, raw)
    issues = ['missing_' + name for name in REQUIRED if name not in raw]
    start = end = None
    try:
        start, end = timestamp(raw.get('start')), timestamp(raw.get('end'))
        if end < start:
            issues.append('reversed_interval')
        if start < start_s or end > end_s or start < observed_start_s or end > observed_end_s:
            issues.append('outside_observed_window')
    except (ValueError, TypeError):
        issues.append('invalid_timestamp')
    kind = raw.get('type')
    if kind not in vocabulary:
        issues.append('unknown_event_type')
    description = raw.get('description')
    if not isinstance(description, str) or not description.strip():
        issues.append('missing_description')
    elif SPECULATION.search(description):
        issues.append('speculative_or_inferred_outcome')
    if kind in {'left_with_purchase', 'left_without_purchase'}:
        issues.append('pos_confirmation_required')
    identity = raw.get('person_id')
    track_id = None
    if identity is not None:
        match = re.fullmatch(r'(?:(?:person|id)\s*|P)?(\d+)', str(identity).strip(), re.I)
        if not match:
            issues.append('invalid_track_id')
        else:
            track_id = str(int(match.group(1)))
            rows = [frame for frame in tracks.get('frames', [])
                    if start is not None and end is not None and start <= frame['video_time_s'] <= end]
            if not any(str(track['track_id']) == track_id for frame in rows for track in frame['tracks']):
                issues.append('track_absent_in_event_interval')
    elif kind not in SCENE_EVENTS:
        issues.append('person_action_requires_track')
    if raw.get('product_description') is not None and not isinstance(raw['product_description'], str):
        issues.append('invalid_product_description')
    if raw.get('object_bbox') is not None:
        # Input coordinates refer to the padded model frame. Publication requires an
        # explicit, verified padding transform and frame association; never copy blindly.
        issues.append('object_box_transform_requires_review')
    issues = list(dict.fromkeys(issues))
    reviewed = bool(review and review.get('decision') == 'verified'
                    and review.get('reviewer') and review.get('evidence')
                    and review.get('raw_sha256') == hashlib.sha256(canonical(raw).encode()).hexdigest())
    acknowledged = set(review.get('acknowledged_issues', [])) if reviewed else set()
    recoverable = {'missing_product_description'}
    blockers = [issue for issue in issues if not (issue in recoverable and issue in acknowledged)]
    accepted = reviewed and not blockers
    observation = None
    if accepted:
        observation = {'profile_id': profile_id, 'profile_revision': profile_revision,
            'event_key': key, 'type': kind, 'description': description,
            'source': {'clip_id': source_id, 'start_s': start, 'end_s': end},
            'track_id': track_id, 'object_ref': None,
            'object_description': raw.get('product_description'), 'bbox': None}
    return {'event_key': key, 'output_index': output_index, 'event_index': event_index,
        'source_id': source_id, 'raw': raw, 'raw_start': raw.get('start'), 'raw_end': raw.get('end'),
        'parsed_start_s': start, 'parsed_end_s': end, 'track_id': track_id,
        'identity_scope': 'recording_and_camera_local', 'issues': issues,
        'status': 'reviewed_observation' if accepted else 'quarantined',
        'review': review, 'observation': observation}


def build_import(run_root, *, profile_id, profile_revision=1, reviews=None, vocabulary=VOCABULARY):
    root = Path(run_root)
    receipt = load(root / 'resumed-stock/results/receipt.private.json')
    manifest = load(root / 'retail-action-nvidia/manifest.json')
    records = {row['clip_id']: row for row in manifest['records']}
    results_dir = root / 'resumed-stock/results'
    audit, windows = [], []
    reviews = reviews or {}
    for output_index, result in enumerate(receipt['results'], 1):
        clip = Path(receipt['input_paths_by_stream'][result['stream_id']]).parent.name
        if clip not in records or not re.fullmatch(r'\d+-rank[01]', clip):
            raise ValueError('Match every model input to its trusted recording receipt.')
        source_id = 'retailaction-' + clip
        record = records[clip]
        trace = load(results_dir / f'input-trace-{output_index:03d}.json')
        times = trace.get('frame_times_s', [])
        if not times or any(not isinstance(x, (int,float)) or not math.isfinite(x) for x in times):
            raise ValueError('Keep the actual input clock with every output.')
        observed_start, observed_end = min(times), max(times)
        window_start = float(result['segment_start_s'])
        # The nominal final window may extend past the file. Admission uses the
        # verified media extent while the exact input frame bounds stay in the audit.
        window_end = min(float(result['segment_end_s']), float(record['duration_s']))
        tracks = load(root / 'retail-action-nvidia' / clip / 'tracks.json')
        output_audit = {'output_index': output_index, 'source_id': source_id,
            'raw_result': result['result_text'], 'nominal_start_s': result['segment_start_s'],
            'nominal_end_s': result['segment_end_s'], 'window_start_s': window_start,
            'window_end_s': window_end, 'observed_start_s': observed_start,
            'observed_end_s': observed_end, 'events': [], 'parse_error': None,
            'trace_file': f'resumed-stock/results/input-trace-{output_index:03d}.json'}
        try:
            raw_events = final_events(result['result_text'])
        except (ValueError, json.JSONDecodeError) as exc:
            raw_events = []
            output_audit['parse_error'] = str(exc)
        for event_index, raw in enumerate(raw_events):
            key = event_key(source_id, output_index, event_index, raw)
            row = audit_event(raw, source_id=source_id, output_index=output_index,
                event_index=event_index, start_s=window_start, end_s=window_end,
                observed_start_s=observed_start, observed_end_s=observed_end, tracks=tracks,
                profile_id=profile_id, profile_revision=profile_revision,
                review=reviews.get(key), vocabulary=vocabulary)
            output_audit['events'].append(row)
        window = StreamWindow.model_validate({'stream_id': source_id, 'source_id': source_id,
            'profile_id': profile_id, 'profile_revision': profile_revision,
            'start_s': window_start, 'end_s': window_end, 'input_kind': 'dataset_replay',
            'model': 'nvidia/Cosmos-Reason2-8B',
            'sampling': '8fps; 32 motion-selected source frames held on original timeline; 448px padded model input',
            'processing_seconds': trace['llm_generate_elapsed_s'],
            'observations': [row['observation'] for row in output_audit['events'] if row['observation']]})
        windows.append(window.model_dump())
        audit.append(output_audit)
    windows.sort(key=lambda row: (row['source_id'], row['start_s']))
    flat = [event for row in audit for event in row['events']]
    return {'schema_version': 1, 'dataset': 'retail', 'model': 'nvidia/Cosmos-Reason2-8B',
        'sdk_modified': receipt['sdk_modified'], 'plugin_sha256': receipt['plugin_sha256'],
        'identity_scope': 'recording_and_camera_local', 'capture_dates_known': False,
        'windows': windows, 'audit': audit,
        'counts': {'raw_events': len(flat), 'reviewed_observations': sum(bool(row['observation']) for row in flat),
                   'quarantined': sum(not row['observation'] for row in flat)},
        'catalog': {'sku_fields_found': [], 'product_boxes_returned': sum(row['raw'].get('object_bbox') is not None for row in flat),
                    'identity_status': 'unresolved', 'labels_used_as_predictions': False}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, required=True)
    parser.add_argument('--profile-id', required=True)
    parser.add_argument('--profile-revision', type=int, default=1)
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build_import(args.run_root, profile_id=args.profile_id,
        profile_revision=args.profile_revision, reviews=load(args.reviews) if args.reviews else None)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False))
    args.output.chmod(0o600)
    print(json.dumps(result['counts']))


if __name__ == '__main__':
    main()
