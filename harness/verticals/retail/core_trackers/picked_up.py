"""Core Tracker processor: products picked up.

tracker   pick-up events
processor counts the pick-ups of the period
canvas    the pick-up rows with product, visitor and camera
tile      the count, its series and the newest pick-ups
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'picked_up'
TYPES = frozenset({'pick_up', 'pickup', 'picked_up_item'})


def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    return common.count_tracker(KEY, 'items', data, period, [e for e in data.events if e['type'] in TYPES])
