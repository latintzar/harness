"""Source-scoped, as-of possession estimates. Admission is a trusted host seam.

Object-track/review receipts require a host verifier. Model estimates instead
bind to the original admitted response, source bytes, window and profile; their
identity and quantity remain explicitly estimated.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import math
from typing import Literal
from pydantic import Field, model_validator
from location_intelligence import Strict, ProductPrice, encoded, payment_evidence
from monitoring import is_reference_event

HELD = frozenset({'pick_up', 'pickup', 'picked_up_item', 'carry'})
BASKET = frozenset({'place_in_basket'})
RETURNED = frozenset({'put_back', 'put_back_item'})
UNCERTAIN = frozenset({'put_down', 'place_elsewhere', 'drop_item'})
TRANSITIONS = HELD | BASKET | RETURNED | UNCERTAIN | {'hand_over'}
ACTIVE = frozenset({'held', 'in_basket'})


class ItemEvidence(Strict):
    event_id: str = Field(min_length=1, max_length=160)
    instance_ref: str = Field(min_length=1, max_length=160)
    quantity: int = Field(ge=1, le=1000, strict=True)
    quantity_basis: Literal['single_sale_unit', 'counted_sale_units']
    identity_basis: Literal['object_tracker', 'human_review']
    proof_id: str = Field(min_length=1, max_length=200)
    proof_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    recipient_visit_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode='after')
    def counted(self):
        if self.quantity_basis == 'single_sale_unit' and self.quantity != 1:
            raise ValueError('A single visible sale unit has quantity one.')
        if not self.instance_ref.strip() or not self.proof_id.strip():
            raise ValueError('Keep the original object identity and proof reference.')
        return self


def evidence_key(owner, event_id):
    return 'basket-evidence-' + hashlib.sha256(encoded([owner, event_id]).encode()).hexdigest()[:28]


def item_key(owner, collection, recording, reference):
    return 'basket-item-' + hashlib.sha256(encoded([owner, collection, recording, reference]).encode()).hexdigest()[:28]


def project(events, receipts, products, visit_id, at_s, valuation_at):
    """Pure reducer over already-authorized inputs; quantities are absolute counts."""
    evidence = {r['event_id']: r for r in receipts}
    states, unresolved = {}, []
    rows = sorted((e for e in events if e['source']['end_s'] <= at_s),
                  key=lambda e: (e['source']['end_s'], e['id']))
    for event in rows:
        kind, actor = event['type'], event.get('visit_id')
        if kind not in TRANSITIONS:
            continue
        proof = evidence.get(event['id'])
        if not proof:
            if actor == visit_id:
                unresolved.append({'event_id': event['id'], 'type': kind,
                    'description': event.get('object_description') or event['description'],
                    'at_s': event['source']['end_s'], 'reason': 'item_identity_or_quantity_pending'})
            continue
        key, at = proof['instance_id'], event['source']['end_s']
        item = states.get(key)
        if item is None:
            item = states[key] = {'instance_id': key, 'quantity': proof['quantity'],
                'quantity_basis': proof['quantity_basis'], 'identity_basis': proof['identity_basis'],
                'identity_status': proof.get('identity_status', 'evidenced'), 'state': 'uncertain',
                'owner_visit_id': actor, 'at_s': at, 'events': [], 'transition': None}
        previous = item['events'][-1] if item['events'] else None
        item['events'].append(event)
        if proof['identity_basis'] == 'vision_estimate':
            item.update(identity_basis='vision_estimate', identity_status=proof['identity_status'],
                        quantity_basis=proof['quantity_basis'])
        quantity_conflict = item['quantity'] != proof['quantity']
        simultaneous_conflict = (previous is not None and item['at_s'] == at and
            (previous.get('visit_id') != actor or
             ((previous['type'] in HELD | BASKET) != (kind in HELD | BASKET))))
        if quantity_conflict or simultaneous_conflict:
            item.update(state='uncertain', reason='conflicting_item_evidence', at_s=at)
            continue
        item.update(owner_visit_id=actor, at_s=at, transition=kind)
        item.pop('reason', None)
        if proof.get('identity_status') == 'unresolved':
            # A new unlinked claim cannot silently duplicate an earlier item or
            # leave that earlier item confidently held after an ambiguous return.
            for prior in states.values():
                if prior['owner_visit_id'] == actor and prior['identity_basis'] == 'vision_estimate' and prior['state'] in ACTIVE:
                    prior.update(state='uncertain', reason='item_association_pending')
            item.update(state='uncertain', reason='item_association_pending')
            continue
        if kind in HELD:
            # A repeated observation of a physical instance does not add a unit.
            if not (item['state'] == 'in_basket' and kind == 'carry'):
                item['state'] = 'held'
        elif kind in BASKET:
            item['state'] = 'in_basket'
        elif kind in RETURNED:
            item['state'] = 'returned'
        elif kind == 'hand_over':
            recipient = proof.get('recipient_visit_id')
            item.update(state='held' if recipient else 'uncertain',
                        owner_visit_id=recipient or actor)
            if not recipient:
                item['reason'] = 'handover_recipient_pending'
        else:
            item.update(state='uncertain', reason='item_location_pending')

    def describe(item):
        observations = item['events']
        description = next((e.get('object_description') for e in reversed(observations)
                            if e.get('object_description')), 'Observed item')
        confirmed, estimates, candidate = {}, {}, None
        image = None
        for event in observations:
            match = event.get('product_match') or {}
            proof_time = match.get('evidence', {}).get('source_time_s')
            if proof_time is not None and (not isinstance(proof_time, (int, float)) or proof_time > at_s):
                continue
            product = products.get(event.get('product_id'))
            if match.get('status') == 'confirmed' and product and product['status'] == 'confirmed':
                confirmed[product['id']] = product
            elif match.get('status') == 'candidate':
                candidate = {'status': 'candidate', 'product_id': match.get('product_id'),
                             'basis': match.get('basis')}
            if product and product['status'] == 'provisional' and (product.get('price') or {}).get('basis') == 'estimated':
                if product.get('provenance', {}).get('event_id') in {e['id'] for e in observations}:
                    estimates[product['id']] = product
            image = match.get('evidence', {}).get('image_url') or image
        product = next(iter(confirmed.values())) if len(confirmed) == 1 else None
        price_status = 'conflicting_product_matches' if len(confirmed) > 1 else 'price_pending'
        if product is None and not confirmed and len(estimates) == 1:
            product = next(iter(estimates.values()))
        price = None
        if product:
            description = product['label']
            image = (product.get('image') or {}).get('url') or image
            if product.get('price'):
                parsed = ProductPrice.model_validate(product['price'])
                if parsed.observed_at <= valuation_at:
                    price = parsed.model_dump(mode='json')
                    price_status = parsed.basis
                else:
                    price_status = 'price_after_valuation_time'
        return {k: v for k, v in item.items() if k not in {'events', 'owner_visit_id', 'transition'}} | {
            'description': description, 'image': image,
            'product_match': {'status': 'confirmed', 'product_id': product['id']} if product and confirmed else candidate,
            'unit_price': price, 'price_status': price_status,
            'line_estimate': {'amount': str(Decimal(price['amount']) * item['quantity']),
                              'currency': price['currency'], 'basis': 'estimated_basket'} if price else None,
            'evidence_event_ids': [e['id'] for e in observations]}

    owned = [describe(item) for item in states.values() if item['owner_visit_id'] == visit_id]
    current = sorted((i for i in owned if i['state'] in ACTIVE), key=lambda i: (i['at_s'], i['instance_id']))
    uncertain = [i for i in owned if i['state'] == 'uncertain']
    totals = {}
    for item in current:
        price = item['line_estimate']
        if not price:
            continue
        total = totals.setdefault(price['currency'], {'amount': Decimal(0), 'priced_units': 0})
        total['amount'] += Decimal(price['amount']); total['priced_units'] += item['quantity']
    unpriced = sum(i['quantity'] for i in current if i['line_estimate'] is None)
    partial = bool(unpriced or uncertain or unresolved)
    future = [e['source']['end_s'] for e in events if e['source']['end_s'] > at_s]
    return {'items': current, 'uncertain_items': uncertain, 'unresolved_observations': unresolved,
        'totals': [{'currency': currency, 'amount': str(total['amount']),
                    'priced_units': total['priced_units'], 'basis': 'estimated_basket',
                    'partial': partial} for currency, total in sorted(totals.items())],
        'coverage': {'basis': 'observed_items_only', 'unpriced_units': unpriced,
                     'unresolved_observations': len(unresolved), 'uncertain_instances': len(uncertain),
                     'estimated_units': sum(i['quantity'] for i in current if i['identity_basis'] == 'vision_estimate'),
                     'state': 'observed_items' if owned else 'awaiting_item_evidence'},
        'payment_actions': payment_evidence([e for e in rows if e.get('visit_id') == visit_id]),
        'paid_amount': None,
        'validity': {'from_s': max((e['source']['end_s'] for e in rows), default=0),
                     'next_change_s': min(future) if future else None}}


class VisitBasket:
    def __init__(self, locations, *, verifier=None):
        self.locations, self.monitoring, self.verifier = locations, locations.monitoring, verifier

    def _visit(self, owner, visit_id):
        try:
            visit = self.locations._get(owner, 'tracked_visit', visit_id)
            source_id = visit['source_id']
        except ValueError:
            visit = self.locations._get(owner, 'visit', visit_id)
            first = self.monitoring.get_event(owner, visit['event_ids'][0])
            source_id = first['source']['clip_id']
        source = self.monitoring.source(owner, source_id, visit['collection'])
        camera, _ = self.locations._source_place(owner, source, visit['collection'])
        if (camera['id'], camera['location_id']) != (visit['camera_id'], visit['location_id']):
            raise ValueError('Keep the visit in its original camera and location.')
        view = self.locations.authorized(owner, visit['collection'], visit['location_id'])
        return visit, source, view

    def admit(self, owner, definition):
        """Host-only receipt admission; verifier is executable host policy, not input."""
        draft = ItemEvidence.model_validate(definition)
        if self.verifier is None:
            raise ValueError('Connect an object-track or review receipt verifier first.')
        event = self.monitoring.get_event(owner, draft.event_id)
        if is_reference_event(event) or not event.get('visit_id') or event['type'] not in TRANSITIONS:
            raise ValueError('Use a primary possession observation with its tracked visitor.')
        visit, source, _ = self._visit(owner, event['visit_id'])
        self.locations.authorized(owner, visit['collection'], visit['location_id'], write=True)
        if source.get('sha256') != draft.source_sha256:
            raise ValueError('Verify the original source bytes for this item evidence.')
        if draft.recipient_visit_id:
            recipient, _, _ = self._visit(owner, draft.recipient_visit_id)
            if event['type'] != 'hand_over' or recipient['recording_id'] != visit['recording_id'] or recipient['camera_id'] != visit['camera_id']:
                raise ValueError('Transfer only to an evidenced recipient in this recording.')
        if self.verifier(owner, draft.model_dump(mode='json'), event) is not True:
            raise ValueError('Validate the original object-track or review proof before admission.')
        value = draft.model_dump(mode='json') | {'id': evidence_key(owner, event['id']),
            'collection': visit['collection'], 'location_id': visit['location_id'],
            'recording_id': visit['recording_id'], 'camera_id': visit['camera_id'],
            'source_id': event['source']['clip_id'],
            'instance_id': item_key(owner, visit['collection'], visit['recording_id'], draft.instance_ref)}
        return self.locations._put(owner, 'basket_evidence', value, immutable=True)

    def admit_model(self, owner, event_id, pipeline):
        """Produce an immutable qualified estimate from an admitted raw response.

        Scene text cannot choose proof hashes or trust levels. The host resolves
        those from the durable stream inbox. Cross-window associations remain
        candidates, and unlinked repeated claims never increase basket totals.
        """
        event = self.monitoring.get_event(owner, event_id)
        proof = pipeline.model_claim(owner, event_id)
        if not proof or event['type'] not in TRANSITIONS:
            return {'status': 'not_a_possession_estimate'}
        if is_reference_event(event) or not event.get('visit_id'):
            raise ValueError('Use a primary possession observation with its tracked visitor.')
        self.locations.ingest_event(owner, event)
        visit, source, _ = self._visit(owner, event['visit_id'])
        self.locations.authorized(owner, visit['collection'], visit['location_id'], write=True)
        if source.get('sha256') != proof['source_sha256']:
            raise ValueError('Preserve the admitted source bytes.')
        claim = proof['claim']
        existing = [r for r in self.locations._rows(owner, 'basket_evidence')
                    if r['recording_id'] == visit['recording_id'] and r['camera_id'] == visit['camera_id']]
        old = next((r for r in existing if r['event_id'] == event_id), None)
        if old:
            if old.get('proof_sha256') != proof['response_sha256']:
                raise ValueError('Preserve the original item estimate receipt.')
            return old
        related = None
        if claim['prior_event_key']:
            candidates = []
            for receipt in existing:
                try:
                    prior = self.monitoring.get_event(owner, receipt['event_id'])
                    prior_camera, _ = self.locations._event_place(owner, prior)
                except ValueError:
                    continue
                if (prior['event_key'] == claim['prior_event_key'] and prior['profile_id'] == event['profile_id']
                        and prior['source']['end_s'] < event['source']['end_s'] and prior_camera['id'] == visit['camera_id']):
                    candidates.append(receipt)
            if len(candidates) != 1:
                raise ValueError('Reference one admitted earlier observation in this recording.')
            related = candidates[0]
        reference = proof['window_id'] + ':' + claim['object_ref']
        instance_id = item_key(owner, visit['collection'], visit['recording_id'], reference)
        identity_status = 'candidate'
        if claim['relation'] == 'distinct_item' and instance_id == related['instance_id']:
            raise ValueError('Give a distinct observed item its own object reference.')
        if claim['relation'] == 'same_item':
            instance_id = related['instance_id']
        elif claim['relation'] == 'unresolved':
            identity_status = 'unresolved'
        elif claim['relation'] == 'first_observation':
            # Same-window explicit object references establish only provisional
            # identity. Across windows, a relationship must be recorded explicitly.
            for receipt in existing:
                prior = self.monitoring.get_event(owner, receipt['event_id'])
                if (prior.get('visit_id') == event['visit_id'] and prior['source']['end_s'] < event['source']['end_s']
                        and receipt.get('window_id') != proof['window_id']):
                    identity_status = 'unresolved'
                    break
        value = {'id': evidence_key(owner, event_id), 'event_id': event_id,
            'instance_ref': reference, 'instance_id': instance_id,
            'quantity': claim['quantity'], 'quantity_basis': 'vision_estimated_sale_units',
            'identity_basis': 'vision_estimate', 'identity_status': identity_status,
            'association': claim['relation'], 'related_event_id': related['event_id'] if related else None,
            'proof_id': proof['window_id'], 'proof_sha256': proof['response_sha256'],
            'window_id': proof['window_id'], 'window_start_s': proof['start_s'], 'window_end_s': proof['end_s'],
            'adapter_receipt_sha256': proof.get('adapter_receipt_sha256'),
            'model': proof['model'], 'profile_id': proof['profile_id'], 'profile_revision': proof['profile_revision'],
            'source_sha256': proof['source_sha256'], 'source_id': event['source']['clip_id'],
            'collection': visit['collection'], 'location_id': visit['location_id'],
            'recording_id': visit['recording_id'], 'camera_id': visit['camera_id']}
        return self.locations._put(owner, 'basket_evidence', value, immutable=True)

    def at(self, owner, visit_id, at_s, *, valuation_at=None):
        visit, source, view = self._visit(owner, visit_id)
        if isinstance(at_s, bool) or not isinstance(at_s, (int, float)) or not math.isfinite(at_s) or not source.get('start', 0) <= at_s <= source['end']:
            raise ValueError('Choose a time inside the original recording.')
        valuation_at = valuation_at or datetime.now(timezone.utc)
        if not isinstance(valuation_at, datetime) or valuation_at.tzinfo is None:
            raise ValueError('Keep the estimate valuation time timezone-aware.')
        events = []
        for event in self.locations._rows(owner, 'event'):
            if event['recording_id'] != visit['recording_id'] or event['collection'] != visit['collection'] or is_reference_event(event):
                continue
            try:
                original = self.monitoring.get_event(owner, event['id'])
                camera, event_source = self.locations._event_place(owner, original)
            except ValueError:
                continue
            if camera['id'] == visit['camera_id'] and event_source.get('sha256') == event.get('source_sha256'):
                events.append(event)
        if len(events) > 20000:
            raise ValueError('Use a bounded recording for the visit projection.')
        by_id = {e['id']: e for e in events}
        receipts = [r for r in self.locations._rows(owner, 'basket_evidence')
                    if r['event_id'] in by_id and r['source_sha256'] == by_id[r['event_id']].get('source_sha256')]
        products = {p['id']: p for p in self.locations.products(owner, visit['collection'], visit['location_id'])}
        result = project(events, receipts, products, visit_id, at_s, valuation_at)
        return result | {'visit_id': visit_id, 'recording_id': visit['recording_id'],
            'camera_id': visit['camera_id'], 'collection': visit['collection'],
            'as_of_s': at_s, 'valuation_at': valuation_at.isoformat(),
            'valuation_basis': 'current_sourced_estimate', 'scope_revision': view['revision'],
            'scope_receipt': view['effective']}
