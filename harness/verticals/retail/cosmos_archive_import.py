"""Ingest preserved Cosmos answers and tracker output into a company location.

`ingest_recording` is the one admission path for a recording, its tracker output and its model
answers. It validates structure only: the person label maps to a person the tracker saw in that
recording during the event, the times lie inside the window, the type is in the retail vocabulary.
Anything else is skipped and counted with its reason. Admitted events go through
`StreamIngestion.accept_model_response`, so the exact structured response is kept with the answer
it was adapted from, and the ordinary projection work writes events, visits and basket estimates.

`import_archive` reads a preserved answer archive (the review viewer layout: `data.json`,
`tracks.json`, `raw/<clip>-8b.json`, `raw/<clip>-2b.json`, `raw/<clip>-all-prompts.json`,
`media/`) and calls `ingest_recording` once per clip. It runs no model and makes no network call.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

import retail_records as records
from core_trackers import load_recipe
from stream_ingestion import StreamIngestion, canonical
from visit_basket import TRANSITIONS, VisitBasket

MODELS = {'8b': 'nvidia/Cosmos-Reason2-8B', '2b': 'nvidia/Cosmos-Reason2-2B'}
NO_REFERENCE = {None, '', 'unknown', 'none', 'null'}
FIELDS = ('person_id', 'event', 'start_s', 'end_s', 'object_ref', 'item_description', 'evidence')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def collapse(text):
    """A doubled sentence ("A. A.") reads once; every other text stays as the model wrote it."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    half = len(text) // 2
    if len(text) > 20 and len(text) % 2 == 1 and text[:half] == text[half + 1:] and text[half] == ' ':
        return text[:half]
    return text or None


def fenced_json(text):
    """The final JSON of a model answer, with or without a reasoning block or a code fence."""
    final = text.rsplit('</think>', 1)[-1].strip()
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', final, re.S)
    return json.loads(match.group(1) if match else final)


def answer_events(answer):
    """Flatten the two event shapes Cosmos returns: a list, or person -> type -> event."""
    events = answer.get('events') if isinstance(answer, dict) else None
    rows = []
    if isinstance(events, list):
        for index, raw in enumerate(events):
            if isinstance(raw, dict):
                rows.append((f'events/{index}', raw))
    elif isinstance(events, dict):
        for person, by_type in events.items():
            for kind, raw in (by_type.items() if isinstance(by_type, dict) else []):
                for index, one in enumerate(raw if isinstance(raw, list) else [raw]):
                    if isinstance(one, dict):
                        rows.append((f'events/{person}/{kind}/{index}', {'person_id': person, 'event': kind, **one}))
    return rows


def presence(tracks):
    """Tracker person -> the source times the tracker saw that person."""
    seen = {}
    for frame in tracks['frames']:
        for row in frame['tracks']:
            seen.setdefault(str(row['track_id']), []).append(float(frame['video_time_s']))
    return seen


def person_of(label, seen):
    if label is None:
        return None
    text = str(label).strip()
    if text in seen:
        return text
    match = re.fullmatch(r'(?:(?:person|track|id)\s*|P)?(\d+)', text, re.I)
    if not match:
        return None
    number = int(match.group(1))
    return next((key for key in seen if re.fullmatch(r'P?0*%d' % number, key)), None)


def validate(raw, *, seen, start_s, end_s, vocabulary):
    """Structure check for one raw event. Returns (fields, None) or (None, reason)."""
    kind = raw.get('event') or raw.get('type')
    if not isinstance(kind, str) or kind not in vocabulary:
        return None, 'type_outside_vocabulary'
    begin, finish = raw.get('start_s'), raw.get('end_s')
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (begin, finish))
    # Answers round times to four decimals; one millisecond covers that rounding.
    if not numbers or finish < begin or begin < start_s - 1e-3 or finish > end_s + 1e-3:
        return None, 'time_outside_recording'
    person = None
    if vocabulary[kind]['scope'] == 'person':
        person = person_of(raw.get('person_id'), seen)
        if person is None:
            return None, 'person_label_without_tracked_person'
        if not any(begin - 0.5 <= t <= finish + 0.5 for t in seen[person]):
            return None, 'tracked_person_absent_during_event'
    description = collapse(raw.get('evidence')) or collapse(raw.get('description')) \
        or collapse(raw.get('person_description')) or collapse(raw.get('item_description'))
    if not description:
        return None, 'event_without_description'
    reference = raw.get('object_ref')
    reference = None if not isinstance(reference, str) or reference.strip().lower() in NO_REFERENCE else reference.strip()[:100]
    return {'type': kind, 'start_s': float(max(begin, start_s)), 'end_s': float(min(finish, end_s)), 'track_id': person,
        'object_ref': reference, 'object_description': (collapse(raw.get('item_description')) or '')[:2000] or None,
        'description': description[:4000]}, None


def retail_profile(company, location_id):
    """The saved observation profile for the retail vocabulary of this location."""
    recipe = load_recipe('retail')
    definition = {'name': 'Retail observations', 'dataset': company.collection, 'location_id': location_id,
        'room_id': None, 'camera_ids': [],
        'criteria': 'Directly visible actions of each tracked person and visible shelf, queue and till states, with their source times.',
        'vocabulary': [{'type': kind, 'description': entry['label'], 'group': entry['group']}
                       for kind, entry in recipe['vocabulary'].items()]}
    for profile in company.monitoring.list_profiles(company.owner, company.collection):
        if all(profile.get(k) == v for k, v in definition.items()):
            return profile
    return company.monitoring.save_profile(company.owner, definition, datasets=[company.collection])


def drain(company, pipeline):
    """Run the projection work the stream queued. Image inspection of real evidence stays queued."""
    owner, baskets = company.owner, VisitBasket(company.locations)
    handlers = {'project_event': lambda event, payload, key: company.locations.ingest_event(owner, event, job_id=key),
                'project_basket': lambda event, payload, key: baskets.admit_model(owner, event['id'], pipeline)}
    while True:
        batch = pipeline.drain(owner, handlers, limit=100)
        failed = [row for row in batch if row['status'] != 'done']
        if failed:
            raise ValueError('Review the projection of this recording: ' + failed[0]['kind'])
        if not batch:
            return


def ingest_recording(company, location_id, camera_id, recording, tracks, windows, *, profile, vocabulary,
                     data_basis, evidence=None, title=None, files=(), clip=None, pipeline=None, publisher_note=None):
    """Admit one recording: source, tracked visitors, model events, receipts.

    recording: {source_id, duration_s, sha256, media_url, thumbnail_url, capture_start_utc}
    tracks:    {identity_scope, fps, tracker, frames: [{video_time_s, tracks: [{track_id, bbox_xyxy}]}]}
    windows:   [{start_s, end_s, model, sampling, processing_seconds, answer, answer_ref}], contiguous from 0
    """
    owner, monitor, store = company.owner, company.monitoring, company.locations
    source_id = recording['source_id']
    descriptor = {'id': source_id, 'video_id': source_id, 'dataset': company.collection, 'start': 0,
        'end': recording['duration_s'], 'sha256': recording['sha256'], 'camera_id': camera_id,
        'media_url': recording['media_url'], 'thumbnail_url': recording.get('thumbnail_url'),
        'evidence_url': '/control-room/?clip=' + source_id,
        'source_capture_start_utc': recording.get('capture_start_utc')}
    previous = monitor.registered_source(owner, source_id)
    if previous:
        if any(previous.get(k) != descriptor.get(k) for k in ('sha256', 'end', 'dataset', 'camera_id')):
            raise ValueError('Keep recording content and camera association immutable.')
    else:
        monitor.register_source(owner, descriptor)
    tracks_sha = hashlib.sha256(canonical(tracks).encode()).hexdigest()
    visits = store.ingest_tracks(owner, source_id, tracks, tracks_sha256=tracks_sha)
    seen = presence(tracks)
    pipeline = pipeline or StreamIngestion(company.state, monitor)
    admitted, skipped, receipts = [], [], []
    for index, window in enumerate(windows):
        observations, raws, claimable = [], {}, set()
        for ref, raw in answer_events(window['answer']):
            fields, reason = validate(raw, seen=seen, start_s=window['start_s'], end_s=window['end_s'], vocabulary=vocabulary)
            if reason:
                skipped.append({'source_id': source_id, 'answer': window['answer_ref'].get('file'), 'ref': f'window/{index}/{ref}',
                                'reason': reason, 'event': raw.get('event'), 'person_id': raw.get('person_id')})
                continue
            key = 'cosmos-' + hashlib.sha256(canonical([source_id, window['model'], index, ref, raw]).encode()).hexdigest()[:32]
            observations.append({'profile_id': profile['id'], 'profile_revision': profile['revision'], 'event_key': key,
                'type': fields['type'], 'description': fields['description'],
                'source': {'clip_id': source_id, 'start_s': fields['start_s'], 'end_s': fields['end_s']},
                'track_id': fields['track_id'], 'object_ref': fields['object_ref'],
                'object_description': fields['object_description'], 'bbox': None})
            raws[key] = (ref, raw)
            if fields['type'] in TRANSITIONS and fields['track_id'] and fields['object_ref']:
                claimable.add(key)
        # One sale unit per object reference: the answers describe single items. A later
        # observation of the same reference by the same person continues the same item.
        claims, earlier = [], {}
        for seen_event in sorted(observations, key=lambda o: (o['source']['end_s'], o['source']['start_s'], o['event_key'])):
            if seen_event['event_key'] not in claimable:
                continue
            item, end = (seen_event['track_id'], seen_event['object_ref']), seen_event['source']['end_s']
            claim = {'event_key': seen_event['event_key'], 'object_ref': seen_event['object_ref'], 'quantity': 1}
            if item in earlier and earlier[item][1] < end:
                claims.append({**claim, 'relation': 'same_item', 'prior_event_key': earlier[item][0]})
            else:
                claims.append({**claim, 'relation': 'first_observation'})
                earlier.setdefault(item, (seen_event['event_key'], end))
        context = {'stream_id': source_id, 'source_id': source_id, 'profile_id': profile['id'],
            'profile_revision': profile['revision'], 'start_s': window['start_s'], 'end_s': window['end_s'],
            'input_kind': 'dataset_replay', 'model': window['model'], 'sampling': window['sampling'],
            'processing_seconds': window['processing_seconds']}
        response = json.dumps({'observations': observations, 'item_claims': claims}, sort_keys=True)
        adapter = {'adapter': 'cosmos_archive_import/1', 'answer_ref': window['answer_ref'], 'data_basis': data_basis,
                   'quantity_basis': 'one_unit_per_object_reference'}
        receipt = pipeline.accept_model_response(owner, context, response, adapter_receipt=adapter)
        if data_basis == 'generated_history':
            # History carries its catalogue type; there are no pixels for the image inspection job.
            with pipeline.connect() as db:
                db.execute("UPDATE stream_work SET status='skipped',result=? WHERE owner=? AND window_id=? AND kind='inspect_product' AND status='pending'",
                    (canonical({'reason': 'History record carries its catalogue type.'}), owner, receipt['window_id']))
        drain(company, pipeline)
        for event_id in receipt['event_ids']:
            event = monitor.get_event(owner, event_id)
            ref, raw = raws[event['event_key']]
            proof = (evidence or {}).get((event['track_id'], event['type'], event['source']['start_s']), {})
            receipts.append(records.stamp(company, location_id, {'id': event_id, 'event_key': event['event_key'],
                'source_id': source_id, 'visit_id': event.get('visit_id'), 'data_basis': data_basis,
                'answer_file': window['answer_ref'].get('file'), 'answer_sha256': window['answer_ref'].get('sha256'),
                'answer_ref': ref, 'model': window['model'], 'thumb': proof.get('thumb'), 'evidence': proof, 'raw': raw}, camera_id))
            admitted.append(event_id)
    records.put_many(company, 'event_receipt', location_id, receipts)
    records.put_many(company, 'recording_receipt', location_id, [records.stamp(company, location_id, {
        'id': source_id, 'data_basis': data_basis, 'title': title, 'duration_s': recording['duration_s'],
        'capture_start_utc': recording.get('capture_start_utc'), 'sha256': recording['sha256'],
        'tracks_sha256': tracks_sha, 'visit_ids': [v['id'] for v in visits],
        'answers': [w['answer_ref'] for w in windows], 'admitted_events': len(admitted), 'skipped': skipped,
        'files': sorted(files), 'clip': clip, 'publisher_note': publisher_note}, camera_id)])
    return {'source_id': source_id, 'visitors': len(visits), 'admitted': admitted, 'skipped': skipped}


def host_tracks(viewer):
    """The archive's viewer track format, as the tracker frames `ingest_tracks` reads."""
    times = viewer.get('frame_times_s')
    frames = []
    for key in sorted(viewer['frames'], key=int):
        index = int(key)
        frames.append({'frame_index': index, 'video_time_s': times[index] if times else index / viewer['fps'],
            'tracks': [{'track_id': str(row['id']), 'bbox_xyxy': row['box']} for row in viewer['frames'][key]]})
    return {'identity_scope': 'recording_and_camera_local', 'fps': viewer['fps'], 'width': viewer['width'],
            'height': viewer['height'], 'tracker': viewer.get('source') or 'NVIDIA PeopleNet + NvDCF', 'frames': frames}


def archive_windows(root, clip, duration, suffix):
    """The preserved answer windows of one clip for one model, or None when the file is absent."""
    path = root / 'raw' / f'{clip}-{suffix}.json'
    if not path.is_file():
        return None
    saved = json.loads(path.read_text())
    reference = {'file': f'raw/{path.name}', 'sha256': sha(path)}
    if isinstance(saved.get('output'), dict):
        measurement = saved.get('measurement', {})
        return [{'start_s': 0.0, 'end_s': duration, 'model': MODELS[suffix],
            'sampling': f"{measurement.get('fps_requested', 4)} fps requested",
            'processing_seconds': float(measurement.get('elapsed_s', 0)), 'answer': saved['output'], 'answer_ref': reference}]
    windows = []
    for index, window in enumerate(saved.get('windows', [])):
        start, end = float(window['segment']['start_time']), min(float(window['segment']['end_time']), duration)
        if start >= duration:
            continue
        try:
            answer = fenced_json(window['raw']['result'])
        except (ValueError, KeyError, TypeError):
            answer = {'events': []}
        windows.append({'start_s': start, 'end_s': end, 'model': MODELS[suffix],
            'sampling': f"{saved.get('receipt', {}).get('sampling_fps', 2)} fps sampled",
            'processing_seconds': float(window.get('processing_s', 0)), 'answer': answer,
            'answer_ref': {**reference, 'window': index}})
    return windows


def valid_count(windows, seen, vocabulary):
    return sum(validate(raw, seen=seen, start_s=w['start_s'], end_s=w['end_s'], vocabulary=vocabulary)[1] is None
               for w in windows for _, raw in answer_events(w['answer']))


def import_archive(company, archive_root, location_id, room_id, *, clips, cameras, capture_starts=None,
                   publish_media=True):
    """Ingest the listed archive clips into the location. Safe to repeat."""
    from control_room_routes import owner_directory
    root = Path(archive_root).resolve()
    data = {clip['id']: clip for clip in json.loads((root / 'data.json').read_text())['clips']}
    all_tracks = json.loads((root / 'tracks.json').read_text())
    vocabulary = load_recipe('retail')['vocabulary']
    profile = retail_profile(company, location_id)
    bundle = owner_directory(company.state, company.owner)
    pipeline = StreamIngestion(company.state, company.monitoring)
    summary = {'recordings': [], 'admitted_events': 0, 'visitors': 0, 'skipped': [], 'item_groundings': 0}

    def publish(source, relative):
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
            target.chmod(0o600)
        return relative

    for clip in clips:
        meta = data[clip]
        duration = float(meta['duration_s'])
        answers = {suffix: archive_windows(root, clip, duration, suffix) for suffix in MODELS}
        if clip not in all_tracks:
            for suffix, windows in answers.items():
                for index, window in enumerate(windows or []):
                    for ref, raw in answer_events(window['answer']):
                        summary['skipped'].append({'source_id': clip, 'answer': window['answer_ref']['file'], 'ref': f'window/{index}/{ref}',
                            'reason': 'recording_without_tracker_output', 'event': raw.get('event'), 'person_id': raw.get('person_id')})
            continue
        tracks = host_tracks(all_tracks[clip])
        seen = presence(tracks)
        chosen = next((s for s in MODELS if answers[s] and valid_count(answers[s], seen, vocabulary)), None) \
            or next((s for s in MODELS if answers[s]), None)
        for suffix, windows in answers.items():
            if windows and suffix != chosen:
                for index, window in enumerate(windows):
                    for ref, raw in answer_events(window['answer']):
                        reason = validate(raw, seen=seen, start_s=window['start_s'], end_s=window['end_s'], vocabulary=vocabulary)[1]
                        summary['skipped'].append({'source_id': clip, 'answer': window['answer_ref']['file'], 'ref': f'window/{index}/{ref}',
                            'reason': reason or 'second_model_answer_for_recording', 'event': raw.get('event'), 'person_id': raw.get('person_id')})
        windows = answers[chosen] if chosen else []
        if not windows:
            windows = [{'start_s': 0.0, 'end_s': duration, 'model': MODELS['8b'], 'sampling': 'tracker output only',
                        'processing_seconds': 0.0, 'answer': {'events': []}, 'answer_ref': {'file': None, 'sha256': None}}]
        media = root / meta['media']
        prefix = f'sources/{clip}'
        files = [publish(media, f'{prefix}/clip.mp4')] if publish_media else []
        poster = root / meta.get('poster', '')
        if meta.get('poster') and poster.is_file():
            files.append(publish(poster, f'{prefix}/poster.jpg'))
        (bundle / prefix).mkdir(parents=True, exist_ok=True)
        (bundle / prefix / 'tracks.json').write_text(json.dumps(all_tracks[clip]))
        (bundle / prefix / 'tracks.json').chmod(0o600)
        files.append(f'{prefix}/tracks.json')
        evidence = {}
        for layer in (meta.get('models') or {}).get(chosen or '8b', {}).get('windows', []):
            for shown in layer.get('events', []):
                person = person_of(shown.get('person_id'), seen)
                crops = {}
                for name in ('object_crop_url', 'person_crop_url'):
                    source = root / shown[name] if shown.get(name) else None
                    if source and source.is_file():
                        crops[name] = '/control-room/' + publish(source, f'{prefix}/evidence/{source.name}')
                        files.append(f'{prefix}/evidence/{source.name}')
                if crops and isinstance(shown.get('start_s'), (int, float)):
                    evidence[(person, shown.get('event'), float(shown['start_s']))] = {
                        'thumb': crops.get('object_crop_url') or crops.get('person_crop_url'), **crops,
                        'snapshot_time_s': shown.get('snapshot_time_s'),
                        'object_crop_provenance': shown.get('object_crop_provenance')}
        camera_id = records.ensure_camera(company, location_id, room_id, cameras[clip], [clip])
        start = (capture_starts or {}).get(clip)
        prior = company.monitoring.registered_source(company.owner, clip)
        if prior:
            start = prior.get('source_capture_start_utc')
        page_clip = {'id': clip, 'source_recording_id': clip, 'title': meta.get('title') or cameras[clip], 'location_id': location_id,
            'room_id': room_id, 'room_name': 'Shop floor', 'camera_id': camera_id, 'duration_s': duration,
            'media': f'{prefix}/clip.mp4', 'poster': f'{prefix}/poster.jpg', 'tracks_url': f'{prefix}/tracks.json',
            'width': tracks['width'], 'height': tracks['height'], 'fps': tracks['fps'], 'capture_start_utc': start,
            'is_primary_trial': True, 'viewer_enabled': True, 'viewer_default': False,
            'source_provenance': {'kind': 'model_answer_archive', 'identity_scope': 'recording_and_camera_local',
                'face_redaction': 'source_as_published'}}
        result = ingest_recording(company, location_id, camera_id,
            {'source_id': clip, 'duration_s': duration, 'sha256': sha(media), 'media_url': f'/control-room/{prefix}/clip.mp4',
             'thumbnail_url': f'/control-room/{prefix}/poster.jpg', 'capture_start_utc': start},
            tracks, windows, profile=profile, vocabulary=vocabulary, data_basis='model_output', evidence=evidence,
            title=meta.get('title'), files=files, clip=page_clip, pipeline=pipeline,
            publisher_note=meta.get('reference_note') or meta.get('note'))
        groundings = []
        archive = root / 'raw' / f'{clip}-all-prompts.json'
        for index, run in enumerate(json.loads(archive.read_text()) if archive.is_file() else []):
            if run.get('name') != 'object-grounding':
                continue
            try:
                answer = fenced_json(run['answer'])
            except ValueError:
                continue
            person = person_of(answer.get('person_id'), seen) if isinstance(answer, dict) else None
            moment = answer.get('timestamp_s') if isinstance(answer, dict) else None
            if person is None or not isinstance(moment, (int, float)) or not 0 <= moment <= duration or not answer.get('description'):
                summary['skipped'].append({'source_id': clip, 'answer': f'raw/{archive.name}', 'ref': f'{index}/object-grounding',
                    'reason': 'grounding_without_tracked_person_or_time', 'event': None, 'person_id': answer.get('person_id') if isinstance(answer, dict) else None})
                continue
            visit = 'visit-' + hashlib.sha256(json.dumps([company.owner, company.collection, clip, person]).encode()).hexdigest()[:28]
            groundings.append(records.stamp(company, location_id, {'id': 'grounding-' + records.digest([clip, index]),
                'visit_id': visit, 'person_id': person, 'source_id': clip, 'at_s': float(moment), 'box_2d': answer.get('box_2d'),
                'description': collapse(answer['description']), 'answer_file': f'raw/{archive.name}', 'answer_sha256': sha(archive),
                'answer_ref': f'{index}/object-grounding', 'data_basis': 'model_output'}, camera_id))
        records.put_many(company, 'item_grounding', location_id, groundings)
        summary['item_groundings'] += len(groundings)
        summary['recordings'].append({'source_id': clip, 'camera_id': camera_id, 'model': MODELS.get(chosen),
            'visitors': result['visitors'], 'admitted_events': len(result['admitted'])})
        summary['admitted_events'] += len(result['admitted'])
        summary['visitors'] += result['visitors']
        summary['skipped'].extend(result['skipped'])
    reasons = {}
    for row in summary['skipped']:
        reasons[row['reason']] = reasons.get(row['reason'], 0) + 1
    summary['skipped_events'] = len(summary['skipped'])
    summary['skipped_by_reason'] = dict(sorted(reasons.items()))
    records.put_many(company, 'ingestion_audit', location_id, [records.stamp(company, location_id,
        {'id': 'answer-archive', 'archive': str(root), **summary})])
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('state', 'access-state', 'archive'):
        parser.add_argument('--' + flag, required=True, type=Path)
    for flag in ('environment-id', 'owner', 'location-id', 'room-id'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--collection', default='retail-demo')
    parser.add_argument('--clip', action='append', required=True, help='clip=Camera name; repeat per clip')
    args = parser.parse_args(argv)
    company = records.company_from_access(args.state, args.access_state, args.environment_id, args.owner, args.collection)
    cameras = dict(item.split('=', 1) for item in args.clip)
    result = import_archive(company, args.archive, args.location_id, args.room_id, clips=list(cameras), cameras=cameras)
    print(json.dumps({k: v for k, v in result.items() if k != 'skipped'}, indent=2))
    return result


if __name__ == '__main__':
    main()
