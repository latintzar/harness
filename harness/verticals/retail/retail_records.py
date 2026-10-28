"""Retail records of one company location, kept in the location store.

Hierarchy on every row: company -> location -> camera -> visitor -> record. The rows live in
`location-intelligence.sqlite` (table `records`) beside the events, tracked visits and products
that `LocationIntelligence` already owns. This module adds the retail kinds:

    recording_receipt  one per ingested recording: camera, capture time, data basis, counts
    event_receipt      one per event: the answer it came from, its evidence crop, its basis
    item_grounding     an item description a model grounded on one visitor at one time
    observed_item      an item a visitor handled, with its product type and catalog link
    basket             what a visitor holds at their last observation, with a timeline
    sale / sale_line   transactions and lines; `basis` is "estimated" or "pos"
    shelf_state        a shelf's empty/stocked state and its transitions
    queue_interval     one visitor's wait from joining the queue to the till
    zone               a shelf, queue or checkout zone registered on a camera

`data_basis` is internal provenance: "model_output", "generated_history" or "pos".
`derive` is the accumulation step: it rebuilds items, baskets, estimated sales, shelf states and
queue intervals from the location's committed events, and is safe to repeat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from business_scope import ScopeActor

from retail_business_scope import RetailBusinessScope
from location_intelligence import LocationIntelligence, ProductPrice, encoded
from monitoring import MonitoringStore, is_reference_event
from stream_ingestion import PRODUCT_EVENTS
from visit_basket import TRANSITIONS, project
from workspace_identity import workspace_scope

SHELF_EMPTY = frozenset({'display_empty', 'out_of_stock', 'stock_out', 'shelf_empty'})
SHELF_STOCKED = frozenset({'restocked', 'display_item_added'})
QUEUE_START = frozenset({'entered_queue'})
QUEUE_END = frozenset({'reached_checkout', 'left_queue', 'queue_exit'})
DERIVED = ('observed_item', 'basket', 'sale_line', 'shelf_state', 'queue_interval')
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


@dataclass
class Company:
    """Host handle for one company: its state, owner, business collection and stores."""
    state: Path
    owner: str
    collection: str
    scope: RetailBusinessScope
    monitoring: MonitoringStore
    locations: LocationIntelligence
    host_sources: set = field(default_factory=set)
    owner_agent_resolver: object = None

    def __post_init__(self):
        if self.owner_agent_resolver is None:
            self.owner_agent_resolver = getattr(self.scope, 'display_owner_agent', None)

    def display_owner_agent(self, *, create=False):
        """Actual agent identity supplied by the host; legacy data keeps its principal owner."""
        if self.owner_agent_resolver is None:
            return None
        value = self.owner_agent_resolver(self.owner, self.collection, create=create)
        return value if value and value != self.owner else None


    @property
    def id(self):
        return self.scope.company_id

    @property
    def actor(self):
        return ScopeActor(self.scope.company_id, self.owner)


def open_company(state, owner, collection, *, member):
    """Open a company's stores. `member(principal)` is the host's membership lookup."""
    state = Path(state)
    monitoring = MonitoringStore(state)
    sources = set()
    scope = RetailBusinessScope(state, company_id=workspace_scope(state), member=member,
        source_authorized=lambda actor, name, source: actor.principal_id == owner and name == collection
            and (source in sources or monitoring.registered_source(owner, source) is not None))
    return Company(state, owner, collection, scope, monitoring,
        LocationIntelligence(state, scope, monitoring), sources)


def company_from_access(state, access_state, environment_id, owner, collection):
    """Open a company through its access registry, as the host commands do."""
    access = Path(access_state) / 'access.sqlite'
    if not access.is_file():
        raise ValueError('Use the company access registry.')
    def member(principal):
        with sqlite3.connect(access.as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT role FROM principal_environments WHERE principal_id=? AND environment=?',
                             (principal, environment_id)).fetchone()
        return {'id': principal, 'role': row[0]} if row else None
    return open_company(state, owner, collection, member=member)


def digest(value, length=28):
    return hashlib.sha256(encoded(value).encode()).hexdigest()[:length]


def rows(company, kind, location_id):
    values = [r for r in company.locations._rows(company.owner, kind)
              if r['collection'] == company.collection and r['location_id'] == location_id]
    return sorted(values, key=lambda r: r['id'])


def stamp(company, location_id, value, camera_id=None):
    return {'company_id': company.id, 'collection': company.collection, 'location_id': location_id,
            'camera_id': camera_id, **value}


def put_many(company, kind, location_id, values, *, replace_all=False, keep=lambda row: False):
    """One transaction per kind. `replace_all` rebuilds a derived kind for the location."""
    company.locations.authorized(company.owner, company.collection, location_id, write=True)
    with company.locations.connect(True) as db:
        if replace_all:
            old = db.execute('SELECT id,body FROM records WHERE owner=? AND kind=? AND collection=? AND location=?',
                (company.owner, kind, company.collection, location_id)).fetchall()
            for row in old:
                if not keep(json.loads(row['body'])):
                    db.execute('DELETE FROM records WHERE owner=? AND kind=? AND id=?', (company.owner, kind, row['id']))
        for value in values:
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)',
                (company.owner, kind, value['id'], company.collection, location_id, encoded(value)))
    return len(values)


def cameras(company, location_id):
    view = company.scope.describe(company.actor, company.collection)
    return [c for c in view['cameras'] if c['location_id'] == location_id]


def ensure_camera(company, location_id, room_id, name, source_ids):
    """Create the named camera in the location, or add recordings to it. One scope write."""
    view = company.scope.describe(company.actor, company.collection)
    camera = next((c for c in view['cameras'] if c['location_id'] == location_id and c['name'] == name), None)
    wanted = sorted(set(source_ids) | set(camera['source_ids'] if camera else []))
    if camera and wanted == sorted(camera['source_ids']):
        return camera['id']
    company.host_sources.update(source_ids)
    value = {'name': name, 'location_id': location_id, 'room_ids': camera['room_ids'] if camera else [room_id],
             'source_ids': wanted, **({'id': camera['id']} if camera else {})}
    view = company.scope.mutate(company.actor, company.collection, 'cameras', value, view['revision'])
    return next(c['id'] for c in view['cameras'] if c['location_id'] == location_id and c['name'] == name)


def register_zone(company, location_id, camera_id, key, kind, label, *, data_basis):
    if kind not in {'shelf', 'queue', 'checkout'}:
        raise ValueError('Use a shelf, queue or checkout zone.')
    value = stamp(company, location_id, {'id': 'zone-' + digest([camera_id, key]), 'key': key, 'kind': kind,
        'label': label, 'data_basis': data_basis}, camera_id)
    put_many(company, 'zone', location_id, [value])
    return value


def source_clocks(company):
    """Capture start (epoch seconds) of each registered recording that has one."""
    from retail_agent_inputs import _capture_interval
    clocks = {}
    for source in company.monitoring.registered_sources(company.owner, company.collection):
        interval = _capture_interval(source)
        if interval:
            clocks[source['id']] = interval[0] - float(source.get('start', 0))
    return clocks


def iso(epoch):
    return None if epoch is None else datetime.fromtimestamp(round(epoch, 3), timezone.utc).isoformat().replace('+00:00', 'Z')


def record_till_sales(company, location_id, transactions):
    """Write till transactions into the same sale tables with `basis: "pos"`."""
    sales, lines = [], []
    for row in transactions:
        at = datetime.fromisoformat(str(row['at']).replace('Z', '+00:00'))
        if at.tzinfo is None or not row.get('transaction_id') or not row.get('lines'):
            raise ValueError('Use till transactions with an identifier, a timezone-aware time and lines.')
        sale_id = 'sale-pos-' + digest([location_id, row['transaction_id']])
        total = Decimal(0)
        for index, line in enumerate(row['lines']):
            amount = Decimal(str(line['unit_price'])) * int(line['quantity'])
            total += amount
            lines.append(stamp(company, location_id, {'id': f'{sale_id}-{index}', 'sale_id': sale_id,
                'product_id': line.get('product_id'), 'label': line['label'], 'quantity': int(line['quantity']),
                'unit_price': str(Decimal(str(line['unit_price']))), 'amount': str(amount), 'currency': row['currency'],
                'basis': 'pos', 'price_source': 'till', 'data_basis': 'pos'}, row.get('camera_id')))
        sales.append(stamp(company, location_id, {'id': sale_id, 'visit_id': row.get('visit_id'),
            'at': iso(at.timestamp()), 'basis': 'pos', 'currency': row['currency'], 'total': str(total),
            'units': sum(int(line['quantity']) for line in row['lines']), 'data_basis': 'pos',
            'provenance': {'kind': 'till', 'transaction_id': row['transaction_id']}}, row.get('camera_id')))
    put_many(company, 'sale', location_id, sales)
    put_many(company, 'sale_line', location_id, lines)
    return {'sales': len(sales), 'lines': len(lines)}


def catalog(company, location_id):
    """Catalog products of the location: the ones a catalog source or the finder supplied."""
    return sorted((p for p in company.locations._rows(company.owner, 'product')
        if p['collection'] == company.collection and p['location_id'] == location_id
        and p.get('provenance', {}).get('kind') != 'camera_observation'), key=lambda p: p['id'])


def derive(company, location_id):
    """Accumulate committed events into items, baskets, estimated sales, shelves and queues."""
    from catalog_finder import classify, readable_text
    store, owner = company.locations, company.owner
    store.authorized(owner, company.collection, location_id, write=True)
    here = lambda r: r['collection'] == company.collection and r['location_id'] == location_id
    events = sorted((e for e in store._rows(owner, 'event') if here(e) and not is_reference_event(e)),
                    key=lambda e: (e['recording_id'], e['source']['start_s'], e['source']['end_s'], e['id']))
    receipts = {r['id']: r for r in rows(company, 'event_receipt', location_id)}
    recordings = {r['id']: r for r in rows(company, 'recording_receipt', location_id)}
    visits = {v['id']: v for v in store._rows(owner, 'tracked_visit') if here(v)}
    clocks = source_clocks(company)
    at = lambda source_id, seconds: iso(clocks[source_id] + seconds) if source_id in clocks else None
    basis = lambda source_id: recordings.get(source_id, {}).get('data_basis', 'model_output')
    by_type = {p['attributes']['product_type']: p for p in catalog(company, location_id)
               if p.get('attributes', {}).get('product_type')}

    # 1. Observed items: one per visitor and object reference, typed and linked to the catalog.
    grouped = {}
    for event in events:
        if event['type'] in PRODUCT_EVENTS and event.get('visit_id'):
            grouped.setdefault((event['visit_id'], event.get('product_id') or event['id']), []).append(event)
    items, item_of_event = [], {}
    for (visit_id, product_key), hits in grouped.items():
        first = hits[0]
        description = next((e['object_description'] for e in hits if e.get('object_description')), None) \
            or first.get('object_ref') or 'Observed item'
        items.append({'id': 'observed-item-' + digest([visit_id, product_key]), 'visit_id': visit_id,
            'person_id': first.get('track_id'), 'source_id': first['source']['clip_id'],
            'recording_id': first['recording_id'], 'object_ref': first.get('object_ref'),
            'description': description, 'descriptions': sorted({e['object_description'] for e in hits if e.get('object_description')}),
            'observed_product_id': first.get('product_id'), 'event_ids': [e['id'] for e in hits],
            'first_s': first['source']['start_s'], 'last_s': max(e['source']['end_s'] for e in hits),
            'first_at': at(first['source']['clip_id'], first['source']['start_s']),
            'last_at': at(first['source']['clip_id'], max(e['source']['end_s'] for e in hits)),
            'evidence': [u for u in dict.fromkeys(receipts.get(e['id'], {}).get('thumb') for e in hits) if u],
            'data_basis': basis(first['source']['clip_id']), 'camera': first['camera_id'],
            'provenance': {'answer_files': sorted({receipts[e['id']]['answer_file'] for e in hits
                if receipts.get(e['id'], {}).get('answer_file')})}})
    for grounding in rows(company, 'item_grounding', location_id):
        same = [i for i in items if i['visit_id'] == grounding['visit_id']]
        if len(same) == 1:
            same[0]['descriptions'] = sorted(set(same[0]['descriptions']) | {grounding['description']})
            same[0]['provenance']['answer_files'] = sorted(set(same[0]['provenance']['answer_files']) | {grounding['answer_file']})
        else:
            items.append({'id': 'observed-item-' + digest([grounding['visit_id'], grounding['id']]),
                'visit_id': grounding['visit_id'], 'person_id': grounding['person_id'], 'source_id': grounding['source_id'],
                'recording_id': grounding['source_id'], 'object_ref': None, 'description': grounding['description'],
                'descriptions': [grounding['description']], 'observed_product_id': None, 'event_ids': [],
                'first_s': grounding['at_s'], 'last_s': grounding['at_s'],
                'first_at': at(grounding['source_id'], grounding['at_s']), 'last_at': at(grounding['source_id'], grounding['at_s']),
                'evidence': [], 'data_basis': grounding['data_basis'], 'camera': grounding['camera_id'],
                'provenance': {'answer_files': [grounding['answer_file']]}})
    for item in items:
        key, label = classify(' '.join(item['descriptions']) or item['description'])
        product = by_type.get(key)
        item.update(product_type=key, product_type_label=label, readable_text=readable_text(' '.join(item['descriptions'])),
            product_id=product['id'] if product else None,
            match={'basis': 'product_type_finder', 'confidence': product['attributes'].get('confidence')} if product else None)
        for event_id in item['event_ids']:
            item_of_event[event_id] = item
    put_many(company, 'observed_item', location_id,
        [stamp(company, location_id, {k: v for k, v in i.items() if k != 'camera'}, i['camera']) for i in items], replace_all=True)

    # 2. A linked item carries its catalog product's sourced estimated price on its observed product.
    products = {p['id']: p for p in store._rows(owner, 'product') if here(p)}
    changed = []
    for item in items:
        observed, linked = products.get(item['observed_product_id']), by_type.get(item['product_type'])
        if not observed or not linked or not linked.get('price'):
            continue
        wanted = {**observed, 'label': linked['label'], 'price': linked['price'],
            'attributes': {**observed.get('attributes', {}), 'catalog_product_id': linked['id'],
                           'observed_description': item['description']}}
        if wanted != observed:
            products[observed['id']] = wanted
            changed.append(wanted)
    put_many(company, 'product', location_id, changed)

    # 3. Baskets: the timed reducer in visit_basket, read at each visitor's last observation.
    valuation = max((ProductPrice.model_validate(p['price']).observed_at for p in products.values() if p.get('price')), default=EPOCH)
    evidence = [r for r in store._rows(owner, 'basket_evidence') if here(r)]
    by_recording, proof_by_recording = {}, {}
    for event in events:
        by_recording.setdefault((event['recording_id'], event['camera_id']), []).append(event)
    for proof in evidence:
        proof_by_recording.setdefault((proof['recording_id'], proof['camera_id']), []).append(proof)
    holders = {e['visit_id'] for e in events if e['type'] in TRANSITIONS and e.get('visit_id')}

    def line(entry):
        item = next((item_of_event[i] for i in entry['evidence_event_ids'] if i in item_of_event), None)
        linked = by_type.get(item['product_type']) if item else None
        price = entry['unit_price']
        return {'instance_id': entry['instance_id'], 'product_id': linked['id'] if linked else None,
            'product_type': item['product_type'] if item else None, 'label': entry['description'],
            'quantity': entry['quantity'], 'state': entry['state'],
            'unit_price': price['amount'] if price else None, 'amount': entry['line_estimate']['amount'] if price else None,
            'currency': price['currency'] if price else None, 'basis': 'estimated' if price else None,
            'price_source': price['source'] if price else None,
            'image': (linked or {}).get('attributes', {}).get('image_url') or (item['evidence'][0] if item and item['evidence'] else None),
            'event_ids': entry['evidence_event_ids']}

    baskets, sales, sale_lines = [], [], []
    for visit_id in sorted(holders):
        visit = visits.get(visit_id)
        if not visit:
            continue
        key = (visit['recording_id'], visit['camera_id'])
        scene, proofs = by_recording.get(key, []), proof_by_recording.get(key, [])
        mine = [e for e in scene if e.get('visit_id') == visit_id]
        last_s = max([visit['last_s']] + [e['source']['end_s'] for e in mine])
        state = project(scene, proofs, products, visit_id, last_s, valuation)
        timeline = []
        for moment in sorted({e['source']['end_s'] for e in mine if e['type'] in TRANSITIONS}):
            then = project(scene, proofs, products, visit_id, moment, valuation)
            timeline.append({'at_s': moment, 'at': at(visit['source_id'], moment),
                'units': sum(i['quantity'] for i in then['items']),
                'amount': then['totals'][0]['amount'] if len(then['totals']) == 1 else None})
        lines = [line(entry) for entry in state['items']]
        total = state['totals'][0] if len(state['totals']) == 1 else None
        basket = {'id': 'basket-' + visit_id.removeprefix('visit-'), 'visit_id': visit_id, 'person_id': visit['person_id'],
            'source_id': visit['source_id'], 'recording_id': visit['recording_id'], 'as_of_s': last_s,
            'at': at(visit['source_id'], last_s), 'lines': lines, 'uncertain': [line(entry) for entry in state['uncertain_items']],
            'units': sum(entry['quantity'] for entry in lines), 'amount': total['amount'] if total else None,
            'currency': total['currency'] if total else None, 'basis': 'estimated' if total else None,
            'timeline': timeline, 'payment_actions': state['payment_actions'], 'data_basis': basis(visit['source_id'])}
        baskets.append(stamp(company, location_id, basket, visit['camera_id']))
        if not lines:
            continue
        # 4. Without till data, the basket at the last observation is the estimated sale.
        sale_id = 'sale-' + digest([visit_id, 'estimated'])
        sales.append(stamp(company, location_id, {'id': sale_id, 'visit_id': visit_id, 'person_id': visit['person_id'],
            'source_id': visit['source_id'], 'at': basket['at'], 'basis': 'estimated', 'currency': basket['currency'],
            'total': basket['amount'], 'units': basket['units'], 'data_basis': basket['data_basis'],
            'provenance': {'kind': 'basket_at_last_observation', 'basket_id': basket['id'], 'as_of_s': last_s}}, visit['camera_id']))
        for index, entry in enumerate(lines):
            sale_lines.append(stamp(company, location_id, {'id': f'{sale_id}-{index}', 'sale_id': sale_id,
                'product_id': entry['product_id'], 'label': entry['label'], 'quantity': entry['quantity'],
                'unit_price': entry['unit_price'], 'amount': entry['amount'], 'currency': entry['currency'],
                'basis': 'estimated', 'price_source': entry['price_source'], 'data_basis': basket['data_basis']}, visit['camera_id']))
    till = lambda row: row.get('basis') == 'pos'
    put_many(company, 'basket', location_id, baskets, replace_all=True)
    put_many(company, 'sale', location_id, sales, replace_all=True, keep=till)
    put_many(company, 'sale_line', location_id, sale_lines, replace_all=True, keep=till)

    # 5. Shelf states: empty since / replenished, per camera and shelf reference.
    shelves = {}
    for event in sorted(events, key=lambda e: (at(e['source']['clip_id'], e['source']['start_s']) or '', e['id'])):
        if event['type'] not in SHELF_EMPTY | SHELF_STOCKED:
            continue
        when = at(event['source']['clip_id'], event['source']['start_s'])
        shelf = shelves.setdefault((event['camera_id'], event.get('object_ref') or 'shelf'), {
            'id': 'shelf-' + digest([event['camera_id'], event.get('object_ref') or 'shelf']),
            'shelf': event.get('object_ref') or 'shelf', 'label': event.get('object_description') or event.get('object_ref') or 'Shelf',
            'state': 'stocked', 'empty_since': None, 'replenished_at': None, 'intervals': [], 'event_ids': [],
            'camera': event['camera_id'], 'data_basis': basis(event['source']['clip_id'])})
        shelf['event_ids'].append(event['id'])
        if event['type'] in SHELF_EMPTY and shelf['state'] != 'empty':
            shelf.update(state='empty', empty_since=when, replenished_at=None)
            shelf['intervals'].append({'empty_since': when, 'replenished_at': None, 'empty_event_id': event['id'], 'replenished_event_id': None})
        elif event['type'] in SHELF_STOCKED and shelf['state'] == 'empty':
            shelf.update(state='stocked', replenished_at=when)
            shelf['intervals'][-1].update(replenished_at=when, replenished_event_id=event['id'])
    put_many(company, 'shelf_state', location_id,
        [stamp(company, location_id, {k: v for k, v in s.items() if k != 'camera'}, s['camera']) for s in shelves.values()], replace_all=True)

    # 6. Queue intervals: joined the queue -> reached the till or left the queue.
    waits, open_waits = [], {}
    for event in events:
        visit_id = event.get('visit_id')
        if not visit_id:
            continue
        if event['type'] in QUEUE_START:
            open_waits.setdefault(visit_id, event)
        elif event['type'] in QUEUE_END and visit_id in open_waits:
            start = open_waits.pop(visit_id)
            waits.append(stamp(company, location_id, {'id': 'queue-' + digest([visit_id, start['id']]), 'visit_id': visit_id,
                'person_id': event.get('track_id'), 'source_id': event['source']['clip_id'],
                'start_s': start['source']['start_s'], 'end_s': event['source']['start_s'],
                'start_at': at(start['source']['clip_id'], start['source']['start_s']),
                'end_at': at(event['source']['clip_id'], event['source']['start_s']),
                'wait_s': round(event['source']['start_s'] - start['source']['start_s'], 3), 'outcome': event['type'],
                'event_ids': [start['id'], event['id']], 'data_basis': basis(event['source']['clip_id'])}, event['camera_id']))
    put_many(company, 'queue_interval', location_id, waits, replace_all=True)
    return {'events': len(events), 'visitors': len(visits), 'observed_items': len(items), 'baskets': len(baskets),
            'estimated_sales': len(sales), 'sale_lines': len(sale_lines), 'shelves': len(shelves), 'queue_intervals': len(waits)}
