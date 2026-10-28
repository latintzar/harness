"""Core Tracker processor: product interactions.

tracker   every event where a visitor handles or examines a product
processor counts the events of the period and ranks the product types behind them
canvas    the event rows, the ranking by product type and the count per camera
tile      the count, its series and the newest events
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'product_interactions'
from stream_ingestion import PRODUCT_EVENTS


def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    return common.count_tracker(KEY, 'interactions', data, period, [e for e in data.events if e['type'] in PRODUCT_EVENTS])
