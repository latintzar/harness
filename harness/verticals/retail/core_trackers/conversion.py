"""Core Tracker processor: conversion.

tracker   visitor sessions and sales
processor divides the visitors who buy by the visitors of the period
canvas    the two counts, the ratio and its basis
tile      the conversion percentage and its series
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'conversion'
from core_trackers.sales import chosen


def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    gap = data.missing(KEY)
    if gap:
        return common.needs(data, KEY, period, gap)
    a, b = common.epoch(period['start']), common.epoch(period['end'])
    visitors = lambda x, y: sum(1 for v in data.visits if common.within(v, x, y))
    buyers = lambda x, y: len({s.get('visit_id') or s['id'] for s in chosen(data, x, y)})
    measure = lambda x, y: 100 * buyers(x, y) / visitors(x, y) if visitors(x, y) else 0
    seen, bought = visitors(a, b), buyers(a, b)
    value = common.number(measure(a, b))
    basis = 'pos' if any(s['basis'] == 'pos' for s in chosen(data, a, b)) else 'estimated'
    summary = f"{bought} of {seen} visitors bought {common.when(period)}, a conversion of {value}%."
    return common.publish(data, KEY, period, value=value, unit='%', series=common.series(period, measure),
        trend=common.trend(period, measure), events=[],
        canvas={'rows': [], 'total_rows': 0, 'visitors': seen, 'purchases': bought, 'ratio': common.number(bought / seen, 4) if seen else 0,
                'basis': basis}, summary=summary, tile_extra={'basis': basis})
