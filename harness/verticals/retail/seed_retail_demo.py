"""Seed the Retail demo company: one command, safe to run twice.

The products and the data structures are the retail product's own. This file is only the thin
demo seed: it confirms the demo location as a retail business, installs the retail Core Trackers
through the same function every retail company gets, ingests the preserved Cosmos output, writes
`product_types.json` for the catalog finder, imports the finder's result when it is given, adds
the deterministic 30-day history, runs the trackers and publishes the page data.

    python seed_retail_demo.py --state STATE --access-state ACCESS --environment-id retail-demo \
        --owner peter --lifecycle demo --archive ARCHIVE [--found-products found_products.json]

See DEMO-SEED.md for each step.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import retail_records as records

# The demo's recordings: preserved clip -> the camera it belongs to and its time on the seed day.
CLIPS = {
    'intel-clip2': ('Shop aisle', '19:31:00'),
    'intel-clip8': ('Shop aisle', '19:38:00'),
    'intel-clip14': ('Shop aisle', '19:44:00'),
    '000027-tracked': ('Chilled cabinet', '19:50:00'),
    '000028-tracked': ('Freezer aisle', '19:55:00'),
    # Preserved answers without tracker output: read and counted, never admitted.
    '000027': (None, None), '000028': (None, None), '000072': (None, None), '000110': (None, None), '000193': (None, None),
}


HISTORY_UNTIL = '19:25'  # on the seed day the generated history ends here; the preserved clips follow


def seed_day(company, requested=None):
    """The day the preserved clips sit on: the one already registered, else the requested or latest full day."""
    for clip in CLIPS:
        source = company.monitoring.registered_source(company.owner, clip)
        if source and source.get('source_capture_start_utc'):
            return datetime.fromisoformat(source['source_capture_start_utc']).date()
    if requested:
        return requested
    now = datetime.now(timezone.utc)
    return now.date() if now.hour >= 20 else now.date() - timedelta(days=1)


def seed(state, access_state, environment_id, owner, *, lifecycle, archive, found_products=None, day=None,
         history_days=30, product_types_output=None, collection='retail-demo', name='Retail demo', address='',
         coordinates=None, publish_media=True):
    from catalog_finder import import_found_products, write_product_types
    from core_trackers import install_core_trackers
    from core_trackers.run import run_trackers
    from cosmos_archive_import import import_archive
    from import_control_room_bundle import publish_core_trackers
    from retail_demo_history import generate
    from seed_control_room import seed_location
    state = Path(state)
    # 1. The demo company's location, through the existing commissioning command.
    seeded = seed_location(state, Path(access_state) / 'access.sqlite', environment_id, owner, lifecycle=lifecycle,
                           collection=collection, name=name, address=address, coordinates=coordinates)
    location_id, room_id = seeded['location_id'], seeded['room_id']
    company = records.company_from_access(state, access_state, environment_id, owner, collection)
    # 2. The retail recipe: the same Core Trackers every retail location gets.
    trackers = install_core_trackers(company, location_id, 'retail')
    # 3. The preserved Cosmos answers and tracker output.
    day = seed_day(company, day)
    starts = {clip: f'{day.isoformat()}T{time}+00:00' for clip, (_, time) in CLIPS.items() if time}
    ingested = import_archive(company, archive, location_id, room_id, clips=list(CLIPS),
        cameras={clip: camera for clip, (camera, _) in CLIPS.items()}, capture_starts=starts, publish_media=publish_media)
    # The seed and the service's own tracker pass share one lease per location: one of them at a time
    # rebuilds its derived rows. From here on the service keeps the trackers current by itself.
    from tracker_runner import host_pass
    with host_pass(company, location_id) as held:
        # 4. Observed items -> product types -> the finder boundary.
        records.derive(company, location_id)
        types_path = Path(product_types_output) if product_types_output else state / 'catalog-finder' / 'product_types.json'
        types = write_product_types(company, location_id, types_path)
        found = import_found_products(company, location_id, found_products) if found_products else None
        # 5. The deeper history of the demo company, through the same ingestion.
        history = generate(company, location_id, room_id, last_day=day, days=history_days,
                           last_day_until=HISTORY_UNTIL) if history_days else None
        # 6. The trackers' processors, then the page data.
        held.mark()
        run = run_trackers(company, location_id)
        page = publish_core_trackers(company, location_id, location_name=name)
        held.done(run, page)
    return {'company_id': company.id, 'location_id': location_id, 'room_id': room_id, 'day': day.isoformat(),
        'core_trackers': [t['key'] for t in trackers if t['core']],
        'ingested': {k: v for k, v in ingested.items() if k != 'skipped'}, 'skipped': ingested['skipped'],
        'product_types': [t['type'] for t in types['product_types']], 'product_types_path': str(types_path),
        'found_products': found, 'history': history, 'derived': run['derived'], 'period': run['period'],
        'tiles': [{k: o['tile'][k] for k in ('key', 'label', 'value', 'unit', 'state', 'needs', 'revision')} for o in run['outputs']],
        'page': page}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('state', 'access-state', 'archive'):
        parser.add_argument('--' + flag, required=True, type=Path)
    for flag in ('environment-id', 'owner'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--lifecycle', choices=['development', 'demo'], required=True)
    parser.add_argument('--found-products', type=Path, help='The finder result to import.')
    parser.add_argument('--product-types-output', type=Path, help='Where to write product_types.json.')
    parser.add_argument('--day', type=date.fromisoformat, help='The day of the preserved clips on a first seed.')
    parser.add_argument('--history-days', type=int, default=30)
    parser.add_argument('--collection', default='retail-demo')
    parser.add_argument('--name', default='Retail demo')
    parser.add_argument('--address', default='')
    parser.add_argument('--latitude', type=float, help='With --longitude and --timezone: where the shop stands.')
    parser.add_argument('--longitude', type=float)
    parser.add_argument('--timezone')
    args = parser.parse_args(argv)
    placed = [args.latitude, args.longitude, args.timezone]
    if any(value is not None for value in placed) and not all(value is not None for value in placed):
        parser.error('Give --latitude, --longitude and --timezone together.')
    coordinates = {'latitude': args.latitude, 'longitude': args.longitude, 'timezone': args.timezone,
                   'source': 'seed command', 'provenance': {'kind': 'operator_entry', 'address': args.address}} if args.timezone else None
    result = seed(args.state, args.access_state, args.environment_id, args.owner, lifecycle=args.lifecycle,
        archive=args.archive, found_products=args.found_products, day=args.day, history_days=args.history_days,
        product_types_output=args.product_types_output, collection=args.collection, name=args.name, address=args.address,
        coordinates=coordinates)
    print(json.dumps({k: v for k, v in result.items() if k != 'skipped'}, indent=2))
    return result


if __name__ == '__main__':
    main()
