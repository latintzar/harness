"""Product types from observed items, and the import of the products a finder returns.

Without a till or a catalog file, the location's catalog starts from what the cameras saw:

1. `product_types` groups the observed items into product types from the model's descriptions and
   any readable text, and `write_product_types` saves them as `product_types.json`.
2. A finder (a small model with web search, instructed by `skills/catalog-finder.md`) reads that
   file and returns `found_products.json`. This module never calls it.
3. `import_found_products` validates the returned file, downloads nothing, and saves one catalog
   product per type: name, brand, category, image URL and a price with `basis: "estimated"` and its
   source URL. `retail_records.derive` then links every observed item of that type to it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

import retail_records as records

COLOURS = ('green', 'red', 'blue', 'yellow', 'orange', 'white', 'black', 'brown', 'pink', 'purple', 'grey', 'gray', 'silver')
FAMILIES = (
    ('bottled-beverage', 'Bottled beverage', ('beverage', 'drink', 'bottle', 'soda', 'juice', 'water')),
    ('frozen-food', 'Frozen food', ('frozen', 'freezer', 'ice cream')),
    ('chilled-packaged-food', 'Chilled packaged food', ('refrigerat', 'chilled', 'dairy', 'yogurt', 'yoghurt')),
    ('packaged-food', 'Packaged food', ('food', 'snack', 'cereal', 'candy', 'chocolate')),
)
TEXT = re.compile(r'(?:labell?ed|reading|reads|marked|text)\s+["\'“‘]([^"\'”’]{2,60})["\'”’]', re.I)


def readable_text(description):
    return sorted({match.strip() for match in TEXT.findall(description or '')})


def classify(description):
    """A stable product type for one item description: (key, label)."""
    text = (description or '').lower()
    for key, label, words in FAMILIES:
        if any(word in text for word in words):
            return key, label
    colour = next((c for c in COLOURS if re.search(rf'\b{c}\b', text)), None)
    if colour:
        colour = 'grey' if colour == 'gray' else colour
        return f'{colour}-packaged-item', f'{colour.capitalize()} packaged item'
    return 'unidentified-item', 'Unidentified item'


def product_types(company, location_id):
    """Group the camera-observed items of a location into product types."""
    items = [i for i in records.rows(company, 'observed_item', location_id) if i['data_basis'] == 'model_output']
    shelf_text = set()
    for event in company.locations._rows(company.owner, 'event'):
        if event['collection'] == company.collection and event['location_id'] == location_id:
            shelf_text.update(re.findall(r"'([A-Z][A-Za-z ]{2,40})'", event.get('object_description') or ''))
    grouped = {}
    for item in items:
        entry = grouped.setdefault(item['product_type'], {'type': item['product_type'], 'label': item['product_type_label'],
            'descriptions': set(), 'readable_text': set(), 'evidence_crops': [], 'count': 0, 'observed_item_ids': []})
        entry['descriptions'].update(item['descriptions'] or [item['description']])
        entry['readable_text'].update(item['readable_text'])
        entry['evidence_crops'].extend(u for u in item['evidence'] if u not in entry['evidence_crops'])
        entry['count'] += 1
        entry['observed_item_ids'].append(item['id'])
    types = []
    for key in sorted(grouped):
        entry = grouped[key]
        descriptions = sorted(entry['descriptions'])
        types.append({'type': key, 'label': entry['label'], 'description': descriptions[0] if descriptions else entry['label'],
            'descriptions': descriptions, 'readable_text': sorted(entry['readable_text']),
            'evidence_crops': entry['evidence_crops'], 'count': entry['count'],
            'observed_item_ids': sorted(entry['observed_item_ids'])})
    return {'schema_version': 1, 'company_id': company.id, 'location_id': location_id,
        'shelf_text': sorted(shelf_text), 'product_types': types}


def write_product_types(company, location_id, path):
    value = product_types(company, location_id)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')
    return value


class FoundProduct(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: str = Field(pattern=r'^[a-z][a-z0-9-]{0,79}$')
    name: str = Field(min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    category: str = Field(min_length=1, max_length=120)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    price_source_url: str = Field(pattern=r'^https?://[^\s]{4,1900}$')
    image_url: str | None = Field(default=None, pattern=r'^https?://[^\s]{4,1900}$')
    confidence: float = Field(ge=0, le=1)


class FoundProducts(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: int = Field(ge=1, le=1)
    found_at: datetime | None = None
    products: list[FoundProduct] = Field(max_length=200)

    @model_validator(mode='after')
    def distinct(self):
        if len({p.type for p in self.products}) != len(self.products):
            raise ValueError('Return one product for each product type.')
        if self.found_at is not None and self.found_at.tzinfo is None:
            raise ValueError('Use a timezone-aware found_at time.')
        return self


def import_found_products(company, location_id, path):
    """Validate a finder result and save its products in the location catalog. No downloads."""
    path = Path(path)
    if path.stat().st_size > 1_000_000:
        raise ValueError('Use a bounded finder result.')
    raw = path.read_bytes()
    found = FoundProducts.model_validate_json(raw)
    known = {t['type']: t for t in product_types(company, location_id)['product_types']}
    unknown = sorted(p.type for p in found.products if p.type not in known)
    if unknown:
        raise ValueError('Return products for the listed product types: ' + ', '.join(unknown))
    found_at = found.found_at or datetime.fromtimestamp(int(path.stat().st_mtime), timezone.utc)
    receipt = hashlib.sha256(raw).hexdigest()
    saved = []
    for product in found.products:
        label = product.name if not product.brand or product.brand.lower() in product.name.lower() else f'{product.brand} {product.name}'
        saved.append(company.locations.save_product(company.owner, {
            'id': 'catalog-' + product.type, 'collection': company.collection, 'location_id': location_id,
            'label': label, 'status': 'provisional',
            'price': {'amount': product.price, 'currency': product.currency, 'basis': 'estimated',
                      'source': product.price_source_url, 'observed_at': found_at, 'confidence': product.confidence},
            'attributes': {'product_type': product.type, 'name': product.name, 'brand': product.brand,
                'category': product.category, 'image_url': product.image_url, 'confidence': product.confidence,
                'price_source_url': product.price_source_url, 'company_id': company.id},
            'provenance': {'kind': 'catalog_finder', 'found_products_sha256': receipt,
                           'found_at': found_at.isoformat(), 'product_type': product.type}}))
    linked = records.derive(company, location_id)
    return {'products': len(saved), 'product_ids': [p['id'] for p in saved],
            'unfound_types': sorted(set(known) - {p.type for p in found.products}), 'derived': linked}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('state', 'access-state'):
        parser.add_argument('--' + flag, required=True, type=Path)
    for flag in ('environment-id', 'owner', 'location-id'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--collection', default='retail-demo')
    parser.add_argument('--product-types', type=Path, help='Write product_types.json here.')
    parser.add_argument('--found-products', type=Path, help='Import this finder result.')
    args = parser.parse_args(argv)
    company = records.company_from_access(args.state, args.access_state, args.environment_id, args.owner, args.collection)
    result = {}
    # This command and the service's tracker pass share one lease per location while either rebuilds
    # the derived rows. The service then reads the imported products and runs the trackers by itself.
    from tracker_runner import host_pass
    with host_pass(company, args.location_id):
        if args.product_types:
            records.derive(company, args.location_id)
            result['product_types'] = [t['type'] for t in write_product_types(company, args.location_id, args.product_types)['product_types']]
            result['product_types_path'] = str(args.product_types)
        if args.found_products:
            result['found_products'] = import_found_products(company, args.location_id, args.found_products)
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    main()
