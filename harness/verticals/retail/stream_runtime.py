"""Application-owned automatic projections and item inspections for stream input."""
from __future__ import annotations
from retail_settings import value as setting
import asyncio
import os
from urllib.parse import urlsplit
from catalog_inspection import CatalogInspection
from control_room_routes import read_bundle, safe_file


def setup_catalog_inspection(selected):
    monitoring = selected['monitoring']
    def authorized(owner, event):
        try:
            return monitoring.get_event(owner, event['id']) == event
        except ValueError:
            return False
    def resolve(owner, source_id):
        source = monitoring.source_lookup(owner, source_id)
        bundle = read_bundle(selected, owner)
        if not source or not bundle:
            raise ValueError('Publish the registered, anonymized source for item inspection.')
        root, manifest = bundle
        path = urlsplit(source['media_url']).path
        if not path.startswith('/control-room/'):
            raise ValueError('Resolve this source through its owning media adapter.')
        relative = path[len('/control-room/'):]
        if relative not in manifest['files']:
            raise ValueError('Use a published source recording.')
        return {**source, 'path': safe_file(root, relative)}
    provider = None
    key = os.environ.get('ROCOTO_OPENAI_API_KEY')
    enabled=(setting('ROCOTO_RETAIL_ITEM_INSPECTION_ENABLED','').lower() in {'1','true'}
        and selected['environment_id'] in {s.strip() for s in setting('ROCOTO_RETAIL_ITEM_INSPECTION_WORKSPACES','').split(',')})
    if enabled and key:
        from catalog_image_model import FundedItemModel
        provider = FundedItemModel(selected['state'], key)
    selected['catalog_inspection'] = CatalogInspection(selected['state'],
        authorized=authorized, source_resolver=resolve,
        catalog=selected['location_intelligence'].catalog_products,
        publish_match=selected['location_intelligence'].apply_product_match,
        inspect_image=provider)



def setup_shelf_catalog(selected):
    """Explicit commissioning plus per-photo consent precede paid photo egress."""
    if 'shelf_catalog' in selected:return selected['shelf_catalog']
    from shelf_catalog import ShelfCatalog
    from decimal import Decimal
    enabled=(setting('ROCOTO_RETAIL_SHELF_CATALOG_INSPECTION_ENABLED','').lower() in {'1','true'}
        and selected['environment_id'] in {s.strip() for s in setting('ROCOTO_RETAIL_SHELF_CATALOG_WORKSPACES','').split(',')})
    provider=None
    if enabled and os.environ.get('ROCOTO_OPENAI_API_KEY'):
        from catalog_image_model import FundedItemModel
        provider=FundedItemModel(selected['state'],os.environ['ROCOTO_OPENAI_API_KEY'])
    def funded():
        if provider is None:return False
        summary=provider.pool.summary()
        return min(Decimal(summary['available_usd']),Decimal(summary['monthly_available_usd']))>0
    service=ShelfCatalog(selected['state'],selected['catalog_setup'],selected['location_intelligence'],
        provider=provider,enabled=enabled,funding_ready=funded)
    selected['shelf_catalog']=service
    return service


def drain_stream(selected):
    from visit_basket import VisitBasket
    pipeline = selected['stream_ingestion']
    baskets = VisitBasket(selected['location_intelligence'])
    with pipeline.connect() as db:
        owners = [row[0] for row in db.execute("SELECT DISTINCT owner FROM stream_work WHERE status='pending' LIMIT 20")]
    result = []
    for owner in owners:
        result.extend(pipeline.drain(owner, {
            'project_event': lambda event,payload,key: selected['location_intelligence'].ingest_event(owner,event,job_id=key),
            'project_basket': lambda event,payload,key: baskets.admit_model(owner,event['id'],pipeline),
            'inspect_product': lambda event,payload,key: selected['catalog_inspection'].process(owner,event,payload,key),
        },limit=2))
    if 'catalog_setup' in selected:
        result.extend(setup_shelf_catalog(selected).drain(limit=1))
    return result


async def stream_tick(selected):
    previous = selected.get('_stream_task')
    if previous and not previous.done():
        return
    if previous:
        try:previous.result()
        except Exception:pass  # Durable job status/receipts contain the actionable state.
    selected['_stream_task'] = asyncio.create_task(asyncio.to_thread(drain_stream, selected))
