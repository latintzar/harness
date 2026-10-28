"""Shared pieces of the Core Tracker processors: the location's data, periods, and publishing.

A processor script exposes `run(company, location_id, period, data=None)`. It reads the location's
events, visitors, catalog and sales for the period and writes that tracker's output revision:
the canvas (full view) and the tile (compact summary of the same revision).
"""
from __future__ import annotations

import contextvars

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import retail_records as records
from core_trackers import installed_trackers, load_recipe
from monitoring import is_reference_event

TILE_EVENTS = 40
CANVAS_ROWS = 500
TREND_DAYS = 30


# Set while a caller reads a tracker over another period: the output is returned and nothing is stored.
READING = contextvars.ContextVar('tracker_reading', default=False)


def epoch(text):
    return None if not text else datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def day_period(day):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return {'start': records.iso(start.timestamp()), 'end': records.iso((start + timedelta(days=1)).timestamp()),
            'label': start.strftime('%a %-d %b %Y'), 'step': 'hour'}


def days_period(last_day, days):
    end = datetime(last_day.year, last_day.month, last_day.day, tzinfo=timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=days)
    return {'start': records.iso(start.timestamp()), 'end': records.iso(end.timestamp()),
            'label': f"{start.strftime('%-d %b' if start.year == (end - timedelta(days=1)).year else '%-d %b %Y')} to {(end - timedelta(days=1)).strftime('%-d %b %Y')}", 'step': 'day'}


def when(period):
    """The period as a sentence says it: «on Sat 10 Oct 2026», «from 4 Oct to 10 Oct 2026»."""
    return f"{'on' if period.get('step') == 'hour' else 'from'} {period['label']}"


def buckets(period):
    start, end = epoch(period['start']), epoch(period['end'])
    step = 3600 if period.get('step') == 'hour' else 86400
    return [(a, min(a + step, end)) for a in range(int(start), int(end), step)]


def within(row, a, b, key='t'):
    return row.get(key) is not None and a <= row[key] < b


@dataclass
class LocationData:
    company: object
    location_id: str
    recipe: dict
    trackers: list
    cameras: list
    visits: list
    events: list
    items: list
    baskets: list
    sales: list
    sale_lines: dict
    shelves: list
    queues: list
    catalog: list
    zones: list

    def camera_name(self, camera_id):
        return next((c['name'] for c in self.cameras if c['id'] == camera_id), None)

    def available(self):
        """The sources this location has, named as the recipe's `requires` names them."""
        have = set()
        if self.cameras:
            have.add('camera')
        if self.visits:
            have.add('person_tracking')
        if self.queues or any(z['kind'] == 'queue' for z in self.zones):
            have.add('queue_zone')
        if self.shelves or any(z['kind'] == 'shelf' for z in self.zones):
            have.add('shelf_zone')
        if self.sales:
            have.add('sales')
        return have

    def missing(self, key):
        tracker = next(t for t in self.trackers if t['key'] == key)
        gap = next((need for need in tracker['requires'] if need not in self.available()), None)
        return self.recipe['needs'][gap] if gap else None


def read_location(company, location_id):
    """Load one location's records once for a tracker run."""
    store, owner = company.locations, company.owner
    store.authorized(owner, company.collection, location_id)
    here = lambda r: r['collection'] == company.collection and r['location_id'] == location_id
    trackers = installed_trackers(company, location_id)
    recipe = load_recipe(next((t['business_type'] for t in trackers if t['core']), 'retail'))
    labels = {kind: entry['label'] for kind, entry in recipe['vocabulary'].items()}
    clocks = records.source_clocks(company)
    receipts = {r['id']: r for r in records.rows(company, 'event_receipt', location_id)}
    items = records.rows(company, 'observed_item', location_id)
    item_of = {event_id: item for item in items for event_id in item['event_ids']}
    visits = []
    for visit in store._rows(owner, 'tracked_visit'):
        if here(visit):
            start = clocks.get(visit['source_id'])
            visits.append({**visit, 't': start + visit['first_s'] if start is not None else None,
                           't_end': start + visit['last_s'] if start is not None else None})
    events = []
    for event in store._rows(owner, 'event'):
        if not here(event) or is_reference_event(event):
            continue
        start = clocks.get(event['source']['clip_id'])
        item = item_of.get(event['id'])
        events.append({'id': event['id'], 'type': event['type'], 'label': labels.get(event['type'], event['type'].replace('_', ' ').capitalize()),
            'description': event['description'], 'object_description': event.get('object_description'),
            't': start + event['source']['start_s'] if start is not None else None,
            't_end': start + event['source']['end_s'] if start is not None else None,
            'at_s': event['source']['start_s'], 'end_s': event['source']['end_s'],
            'visit_id': event.get('visit_id'), 'person_id': event.get('track_id'), 'source_id': event['source']['clip_id'],
            'camera_id': event['camera_id'], 'thumb': receipts.get(event['id'], {}).get('thumb'),
            'answer_file': receipts.get(event['id'], {}).get('answer_file'),
            'data_basis': receipts.get(event['id'], {}).get('data_basis'),
            'product_type': item['product_type'] if item else None, 'product_type_label': item['product_type_label'] if item else None,
            'product_id': item['product_id'] if item else None})
    timed = lambda rows_, key: [{**r, 't': epoch(r.get(key))} for r in rows_]
    lines = {}
    for line in records.rows(company, 'sale_line', location_id):
        lines.setdefault(line['sale_id'], []).append(line)
    return LocationData(company, location_id, recipe, trackers, records.cameras(company, location_id),
        sorted(visits, key=lambda v: (v['t'] or 0, v['id'])), sorted(events, key=lambda e: (e['t'] or 0, e['id'])),
        items, timed(records.rows(company, 'basket', location_id), 'at'), timed(records.rows(company, 'sale', location_id), 'at'),
        lines, records.rows(company, 'shelf_state', location_id), timed(records.rows(company, 'queue_interval', location_id), 'end_at'),
        records.catalog(company, location_id), records.rows(company, 'zone', location_id))


def money(amount, currency):
    """An amount as a person reads it: «€10.60», or «10.60 CHF» for a currency without a common sign."""
    sign = {'EUR': '€', 'USD': '$', 'GBP': '£'}.get(currency)
    text = f'{float(amount):,.2f}'
    return f'{sign}{text}' if sign else f'{text} {currency}'


def number(value, digits=1):
    if value is None:
        return None
    value = round(float(value), digits)
    return int(value) if value == int(value) else value


def series(period, measure):
    return [{'t': records.iso(a), 'value': number(measure(a, b), 2) or 0} for a, b in buckets(period)]


def trend(period, measure, days=TREND_DAYS):
    last = datetime.fromtimestamp(epoch(period['end']) - 1, timezone.utc).date()
    return [{'t': records.iso(a), 'value': number(measure(a, b), 2) or 0} for a, b in buckets(days_period(last, days))]


def event_row(event):
    """The tile's event shape: one observation behind the number."""
    return {'id': event['id'], 'at': records.iso(event['t']), 'label': event['label'], 'description': event['description'],
        'thumb': event['thumb'], 'visitor_id': event['visit_id'], 'source_id': event['source_id'],
        'camera_id': event['camera_id'], 'person_id': event['person_id'], 'at_s': event['at_s'], 'type': event['type'],
        'product': event['product_type_label']}


def by_camera(data, rows_):
    counts = {}
    for row in rows_:
        counts[row['camera_id']] = counts.get(row['camera_id'], 0) + 1
    return [{'camera_id': camera, 'camera': data.camera_name(camera), 'value': value} for camera, value in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] or ''))]


def busiest(points):
    best = max(points, key=lambda p: p['value'], default=None)
    return best if best and best['value'] else None


def plural(value, unit):
    return f"{value} {unit[:-1] if value == 1 and unit.endswith('s') else unit}"


def count_summary(value, unit, period, points, daily):
    text = f"{plural(value, unit)} {when(period)}."
    peak = busiest(points)
    if peak and period.get('step') == 'hour':
        text += f" The busiest hour started at {peak['t'][11:16]} with {number(peak['value'])}."
    active = [p['value'] for p in daily if p['value']]
    if len(active) > 1:
        text += f" The daily average over the last {len(daily)} days is {number(sum(p['value'] for p in daily) / len(daily))}."
    return text


def count_tracker(key, unit, data, period, rows_):
    """A count over the period: the tile number, its series, and the event rows on the canvas."""
    gap = data.missing(key)
    if gap:
        return needs(data, key, period, gap)
    a, b = epoch(period['start']), epoch(period['end'])
    measure = lambda x, y: sum(1 for row in rows_ if within(row, x, y))
    inside = sorted((row for row in rows_ if within(row, a, b)), key=lambda r: (-r['t'], r['id']))
    points, daily = series(period, measure), trend(period, measure)
    ranking = {}
    for row in inside:
        if row.get('product_type_label'):
            ranking[row['product_type_label']] = ranking.get(row['product_type_label'], 0) + 1
    return publish(data, key, period, value=len(inside), unit=unit, series=points, trend=daily,
        events=[event_row(row) for row in inside],
        canvas={'rows': [event_row(row) for row in inside[:CANVAS_ROWS]], 'total_rows': len(inside), 'cameras': by_camera(data, inside),
                'products': [{'product': label, 'value': n} for label, n in sorted(ranking.items(), key=lambda kv: (-kv[1], kv[0]))]},
        summary=count_summary(len(inside), unit, period, points, daily))


def needs(data, key, period, phrase):
    """A measure whose source is missing: state "needs" with its three words."""
    setup = {'Needs a camera': 'This tracker starts when a camera is connected to the location.',
             'Needs person tracking': 'This tracker starts when the cameras report tracked visitors.',
             'Needs queue zone': 'Queue time starts when a queue zone is set on a camera.',
             'Needs shelf zones': 'Shelf states start when shelf zones are set on a camera.',
             'Needs till data': 'Sales start when the till is connected or a visitor leaves with a basket.',
             'Needs catalog prices': 'Sales values start when the catalog has prices for the observed products.'}
    return publish(data, key, period, value=None, unit=None, state='needs', needs=phrase, series=[], trend=[],
                   events=[], canvas={'rows': [], 'total_rows': 0}, summary=setup.get(phrase, phrase + '.'))


def publish(data, key, period, *, value, unit, series, events, canvas, summary, trend=(), state='ready', needs=None, tile_extra=None):
    """Write the tracker's output revision: canvas (full view) and tile (its compact summary)."""
    company, location_id = data.company, data.location_id
    tracker = next(t for t in data.trackers if t['key'] == key)
    scope = {'company_id': company.id, 'location_id': location_id, 'camera_ids': sorted(c['id'] for c in data.cameras),
             'tracker_id': tracker['id'], 'core': tracker['core']}
    public_period = {'start': period['start'], 'end': period['end'], 'label': period['label']}
    tile = {'key': key, 'label': tracker['label'], 'value': value, 'unit': unit, 'state': state, 'needs': needs,
            'series': list(series), 'events': list(events)[:TILE_EVENTS], 'period': public_period, 'trend': list(trend),
            'summary': summary, 'tile_family': tracker['tile']['family'], 'canvas_family': tracker['canvas']['family'],
            **(tile_extra or {}), **scope}
    view = {'key': key, 'label': tracker['label'], 'family': tracker['canvas']['family'], 'value': value, 'unit': unit,
            'state': state, 'needs': needs, 'period': public_period, 'summary': summary, 'series': list(series),
            'history': list(trend), **canvas, **scope}
    if READING.get():
        return {'tile': tile, 'canvas': view}
    content = records.digest({'tile': tile, 'canvas': view}, 64)
    output_id = 'output-' + tracker['id'].removeprefix('tracker-')
    try:
        prior = company.locations._get(company.owner, 'tracker_output', output_id)
    except ValueError:
        prior = None
    if prior and prior['content_sha256'] == content:
        return prior
    revision = prior['revision'] + 1 if prior else 1
    tile['revision'] = view['revision'] = revision
    output = records.stamp(company, location_id, {'id': output_id, 'tracker_id': tracker['id'], 'tracker_key': key,
        'core': tracker['core'], 'camera_ids': scope['camera_ids'], 'period': public_period, 'revision': revision,
        'content_sha256': content, 'tile': tile, 'canvas': view})
    records.put_many(company, 'tracker_output', location_id, [output])
    records.put_many(company, 'tracker_output_revision', location_id, [{**output, 'id': f'{output_id}-r{revision}'}])
    return output


def outputs(company, location_id):
    """Current output of each installed tracker, core first."""
    saved = {o['tracker_id']: o for o in records.rows(company, 'tracker_output', location_id)}
    return [(tracker, saved.get(tracker['id'])) for tracker in installed_trackers(company, location_id)]
