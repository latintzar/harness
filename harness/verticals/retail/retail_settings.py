"""Canonical setting names read by the retail adapter, with installed-name compatibility.

The first name owns the value even when it is empty. Aliases preserve current host
configuration until that configuration is explicitly changed. Generic tile/origin
settings stay generic; this module supplies their retail consumer's compatibility.
"""
from __future__ import annotations

import os

ALIASES = {
    'ROCOTO_RETAIL_REALTIME_READS': 'ROCOTO_VIDEO_REALTIME_READS',
    'ROCOTO_RETAIL_LOCATION_CONTEXT': 'ROCOTO_VIDEO_LOCATION_CONTEXT',
    'ROCOTO_RETAIL_MAPBOX_TOKEN': 'ROCOTO_VIDEO_MAPBOX_TOKEN',
    'ROCOTO_RETAIL_GOOGLE_MAPS_API_KEY': 'ROCOTO_VIDEO_GOOGLE_MAPS_API_KEY',
    'ROCOTO_RETAIL_ITEM_INSPECTION_ENABLED': 'ROCOTO_ITEM_INSPECTION_ENABLED',
    'ROCOTO_RETAIL_ITEM_INSPECTION_WORKSPACES': 'ROCOTO_ITEM_INSPECTION_ENVIRONMENTS',
    'ROCOTO_RETAIL_SHELF_CATALOG_INSPECTION_ENABLED': 'ROCOTO_SHELF_CATALOG_INSPECTION_ENABLED',
    'ROCOTO_RETAIL_SHELF_CATALOG_WORKSPACES': 'ROCOTO_SHELF_CATALOG_ENVIRONMENTS',
    'ROCOTO_RETAIL_MODAL_PYTHON': 'ROCOTO_MODAL_PYTHON',
    'ROCOTO_RETAIL_MODAL_CONFIG_PATH': 'ROCOTO_MODAL_CONFIG_PATH',
    'ROCOTO_RETAIL_QDRANT_URL': 'ROCOTO_QDRANT_URL',
    'ROCOTO_RETAIL_SOURCE_DERIVATIVES_ENABLED': 'ROCOTO_SOURCE_DERIVATIVES_ENABLED',
    'ROCOTO_TILE_RUNNER': 'ROCOTO_VIDEO_TRACKER_RUNNER',
    'ROCOTO_TILE_SCRIPT_EVENT_RUNS': 'ROCOTO_VIDEO_TRACKER_SCRIPT_EVENT_RUNS',
    'ROCOTO_PUBLIC_ORIGIN': 'ROCOTO_VIDEO_PUBLIC_ORIGIN',
}


def value(name, default=None, *, environ=None):
    source = os.environ if environ is None else environ
    alias = ALIASES[name]
    return source[name] if name in source else source.get(alias, default)


def remove(name, *, environ=None):
    source = os.environ if environ is None else environ
    for key in (name, ALIASES[name]):
        source.pop(key, None)
