"""Thirty days of history for the Retail demo company, in the shape Cosmos output takes.

This module is also the written bar for what Cosmos must produce for a small shop. Each generated
recording is one camera window of up to 58 seconds with:

    tracker output   {identity_scope, fps, frames: [{video_time_s, tracks: [{track_id, bbox_xyxy}]}]}
    model answer     {people, objects, events, coverage}, where every event carries
                     person_id, event, start_s, end_s, object_ref, item_description,
                     recipient_person_id, object_box, object_box_at_s, reason_hypothesis,
                     location_description, evidence, certainty, id_association,
                     continues_from_previous, continues_after_clip

Event types it emits: reach_toward, pick_up, examine_object, put_back, place_in_basket,
entered_queue, reached_checkout, presented_card, handed_cash (person scope) and display_empty,
restocked (scene scope, person_id null, object_ref names the shelf).

Everything goes through `cosmos_archive_import.ingest_recording`, the same admission path as the
preserved Cosmos answers, so visitors, events, items, baskets, queue intervals, shelf states and
estimated sales come out of the same ingestion and accumulation code. Every generated record
carries `data_basis: "generated_history"`. The plan is deterministic: one fixed seed, a weekday
rhythm and a time-of-day rhythm of a small shop that opens 09:00 to 20:00.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import random

import retail_records as records
from stream_ingestion import StreamIngestion, canonical

SEED = 20261010
DATA_BASIS = 'generated_history'
MODEL = 'generated-history/retail-1'
RECORDINGS_BY_WEEKDAY = (11, 10, 11, 12, 14, 16, 8)  # Monday to Sunday
HOUR_WEIGHTS = {9: 2, 10: 3, 11: 4, 12: 6, 13: 7, 14: 5, 15: 4, 16: 4, 17: 6, 18: 7, 19: 4}
BEHAVIOURS = (('pass', 25), ('look', 20), ('return', 15), ('buy_one', 30), ('buy_two', 10))
SHELVES = (('shelf_1', 'Shelf 1, top pegboard row'), ('shelf_2', 'Shelf 2, middle pegboard row'),
           ('shelf_3', 'Shelf 3, lower shelf'))
ZONES = (('queue', 'queue', 'Queue'), ('checkout', 'checkout', 'Till')) + tuple((key, 'shelf', label) for key, label in SHELVES)
MAX_SECONDS = 58.0


def event(person, kind, start, end, reference, item, where, evidence):
    """One event with every field a Cosmos answer event carries."""
    return {'person_id': person, 'event': kind, 'start_s': round(start, 2), 'end_s': round(end, 2),
        'object_ref': reference, 'item_description': item, 'recipient_person_id': None, 'object_box': None,
        'object_box_at_s': None, 'reason_hypothesis': None, 'location_description': where, 'evidence': evidence,
        'certainty': 'clear', 'id_association': 'clear', 'continues_from_previous': False, 'continues_after_clip': False}


def visitor(rng, person, products, counter, hour):
    """One visitor's events inside a recording, and the seconds the tracker sees them."""
    enter = round(rng.uniform(0, 4), 1)
    clock = enter + rng.uniform(1.5, 3)
    events, objects = [], {}
    behaviour = rng.choices([name for name, _ in BEHAVIOURS], [weight for _, weight in BEHAVIOURS])[0]
    shelf, shelf_label = rng.choice(SHELVES)

    def step(kind, seconds, reference, item, evidence, where=shelf_label):
        nonlocal clock
        events.append(event(person, kind, clock, clock + seconds, reference, item, where, evidence))
        clock += seconds + rng.uniform(0.3, 1.0)

    def take():
        key, description = rng.choices(products, [max(1, len(products) - i) for i in range(len(products))])[0]
        counter[0] += 1
        reference = f'object_{counter[0]}'
        objects[reference] = description
        step('pick_up', rng.uniform(1.5, 2.5), reference, description, f'Person {person} takes an item from {shelf_label.lower()}')
        return reference, description

    if behaviour == 'look':
        step('reach_toward', rng.uniform(2, 4), shelf, shelf_label, f'Person {person} reaches toward {shelf_label.lower()}')
    elif behaviour == 'return':
        reference, description = take()
        step('examine_object', rng.uniform(3, 5), reference, description, f'Person {person} holds the item and looks at it closely')
        step('put_back', rng.uniform(1.5, 2.5), reference, description, f'Person {person} returns the item to {shelf_label.lower()}')
    elif behaviour in {'buy_one', 'buy_two'}:
        for index in range(2 if behaviour == 'buy_two' else 1):
            reference, description = take()
            if index == 0 and rng.random() < 0.5:
                step('examine_object', rng.uniform(2, 5), reference, description, f'Person {person} holds the item and looks at it closely')
            step('place_in_basket', rng.uniform(1.5, 2.5), reference, description,
                 f'Person {person} places the item in the shopping basket', 'Shopping basket')
        clock += rng.uniform(2, 5)
        busy = HOUR_WEIGHTS.get(hour, 3) / 7
        step('entered_queue', 1.0, None, None, f'Person {person} joins the queue at the till', 'Queue')
        clock += rng.uniform(3, 6 + 13 * busy)
        step('reached_checkout', 1.0, None, None, f'Person {person} reaches the till', 'Till')
        if rng.random() < 0.7:
            step('presented_card', 2.0, None, None, f'Person {person} presents a card at the till', 'Till')
        else:
            step('handed_cash', 2.0, None, None, f'Person {person} hands cash over at the till', 'Till')
    else:
        clock += rng.uniform(4, 10)
    return events, objects, enter, round(clock + 1.0, 1)


def tracker_frames(people, duration):
    """Two frames per second; each visible person has one box that moves along the aisle."""
    frames, moment, index = [], 0.0, 0
    while moment <= duration + 1e-9:
        tracks = []
        for order, (person, (enter, leave)) in enumerate(sorted(people.items())):
            if enter <= moment <= leave:
                x = 200 + 1200 * (moment - enter) / max(leave - enter, 1)
                tracks.append({'track_id': person, 'bbox_xyxy': [round(x, 1), 380.0 + 40 * order, round(x + 260, 1), 1040.0], 'confidence': 0.9})
        frames.append({'frame_index': index, 'video_time_s': round(moment, 2), 'tracks': tracks})
        moment, index = moment + 0.5, index + 1
    return {'identity_scope': 'recording_and_camera_local', 'fps': 2, 'width': 1920, 'height': 1080,
            'tracker': 'generated-history/person-tracks', 'frames': frames}


def plan(products, last_day, days=30, seed=SEED, last_day_until=None):
    """The deterministic history: one entry per recording, oldest first.

    products: [(product type key, item description)], most popular first.
    last_day: the history covers the `days` days that end on this date.
    last_day_until: "HH:MM"; the last day's recordings start before this time.
    """
    rng = random.Random(seed)
    recordings, empty = [], {}
    for offset in range(days - 1, -1, -1):
        day = last_day - timedelta(days=offset)
        count = max(4, RECORDINGS_BY_WEEKDAY[day.weekday()] + rng.choice((-2, -1, 0, 0, 1, 2)))
        starts = sorted(datetime(day.year, day.month, day.day, rng.choices(list(HOUR_WEIGHTS), list(HOUR_WEIGHTS.values()))[0],
                                 rng.randrange(60), rng.randrange(60), tzinfo=timezone.utc) for _ in range(count))
        for index in range(1, len(starts)):
            if (starts[index] - starts[index - 1]).total_seconds() < 120:
                starts[index] = starts[index - 1] + timedelta(seconds=120)
        scene = {index: [] for index in range(count)}
        for shelf, label in SHELVES:
            if shelf in empty:
                scene[0].append(('restocked', shelf, label))
                del empty[shelf]
        if rng.random() < 0.4:
            shelf, label = rng.choice(SHELVES)
            first = rng.randrange(1, max(2, count * 2 // 3))
            scene[first].append(('display_empty', shelf, label))
            if rng.random() < 0.8 and first + 1 < count:
                scene[rng.randrange(first + 1, count)].append(('restocked', shelf, label))
            else:
                empty[shelf] = label
        for index, start in enumerate(starts):
            people, events, objects, counter = {}, [], {}, [0]
            for number in range(rng.choices((1, 2, 3), (60, 30, 10))[0]):
                person = f'P{number}'
                acts, held, enter, leave = visitor(rng, person, products, counter, start.hour)
                people[person] = (enter, min(leave, MAX_SECONDS))
                events.extend(e for e in acts if e['end_s'] <= MAX_SECONDS)
                objects.update(held)
            for kind, shelf, label in scene[index]:
                words = 'is empty' if kind == 'display_empty' else 'is full again'
                events.append(event(None, kind, 0.5, 1.5, shelf, label, label, f'{label} {words}'))
            duration = min(MAX_SECONDS, round(max(leave for _, leave in people.values()) + 0.5, 1))
            answer = {'people': {person: f'Visitor {person} in the shop aisle' for person in people}, 'objects': objects,
                'events': sorted(events, key=lambda e: (e['start_s'], e['person_id'] or '', e['event'])),
                'coverage': [{'person_id': person, 'visible_start_s': enter, 'visible_end_s': min(leave, duration),
                              'activity': 'Shopping in the aisle', 'association': 'clear'} for person, (enter, leave) in people.items()]}
            if offset == 0 and last_day_until and start.strftime('%H:%M') >= last_day_until:
                continue
            source_id = f'history-{day:%Y%m%d}-{index:02d}'
            recordings.append({'source_id': source_id, 'capture_start_utc': start.isoformat(), 'duration_s': duration,
                'tracks': tracker_frames({p: (enter, min(leave, duration)) for p, (enter, leave) in people.items()}, duration),
                'answer': answer})
    return recordings


def generate(company, location_id, room_id, *, last_day, days=30, camera='Shop aisle', seed=SEED, last_day_until=None):
    """Write the planned history into the location through the ordinary ingestion. Safe to repeat."""
    from catalog_finder import product_types
    from core_trackers import load_recipe
    from cosmos_archive_import import ingest_recording, retail_profile
    types = product_types(company, location_id)['product_types']
    products = [(t['type'], t['description']) for t in sorted(types, key=lambda t: (-t['count'], t['type']))] \
        or [('unidentified-item', 'An item from the shelf')]
    recordings = plan(products, last_day, days, seed, last_day_until)
    camera_id = records.ensure_camera(company, location_id, room_id, camera, [r['source_id'] for r in recordings])
    for key, kind, label in ZONES:
        records.register_zone(company, location_id, camera_id, key, kind, label, data_basis=DATA_BASIS)
    profile, vocabulary = retail_profile(company, location_id), load_recipe('retail')['vocabulary']
    pipeline = StreamIngestion(company.state, company.monitoring)
    done = {r['id']: r for r in records.rows(company, 'recording_receipt', location_id)}
    summary = {'recordings': len(recordings), 'written': 0, 'visitors': 0, 'events': 0, 'skipped': 0,
               'first_day': (last_day - timedelta(days=days - 1)).isoformat(), 'last_day': last_day.isoformat(), 'seed': seed}
    for recording in recordings:
        answer_sha = hashlib.sha256(canonical(recording['answer']).encode()).hexdigest()
        content = hashlib.sha256(canonical([recording['source_id'], recording['capture_start_utc'],
                                            recording['duration_s'], answer_sha]).encode()).hexdigest()
        prior = done.get(recording['source_id'])
        if prior and prior['sha256'] == content:
            summary['visitors'] += len(prior['visit_ids'])
            summary['events'] += prior['admitted_events']
            continue
        result = ingest_recording(company, location_id, camera_id,
            {'source_id': recording['source_id'], 'duration_s': recording['duration_s'], 'sha256': content,
             'media_url': '/control-room/history/' + recording['source_id'], 'thumbnail_url': None,
             'capture_start_utc': recording['capture_start_utc']},
            recording['tracks'], [{'start_s': 0.0, 'end_s': recording['duration_s'], 'model': MODEL,
                'sampling': '2 fps person tracks', 'processing_seconds': 0.0, 'answer': recording['answer'],
                'answer_ref': {'file': None, 'sha256': answer_sha, 'generator': 'retail_demo_history', 'seed': seed}}],
            profile=profile, vocabulary=vocabulary, data_basis=DATA_BASIS, pipeline=pipeline)
        summary['written'] += 1
        summary['visitors'] += result['visitors']
        summary['events'] += len(result['admitted'])
        summary['skipped'] += len(result['skipped'])
    return summary
