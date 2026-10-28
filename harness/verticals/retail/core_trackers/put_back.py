"""Core Tracker processor: products put back.

tracker   put-back events
processor counts the put-backs of the period
canvas    the put-back rows with product, visitor and camera
tile      the count, its series and the newest put-backs
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'put_back'
TYPES = frozenset({'put_back', 'put_back_item'})


def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    return common.count_tracker(KEY, 'items', data, period, [e for e in data.events if e['type'] in TYPES])
