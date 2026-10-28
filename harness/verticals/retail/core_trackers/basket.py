"""Core Tracker processor: basket.

tracker   the pick-up, basket and put-back events of each visitor
processor reads each visitor's timed basket at their last observation and values it with catalog prices
canvas    each basket with its lines: product, quantity, price and the "estimated" basis, and its timeline
tile      the average basket, its series and the newest baskets
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'basket'
def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    gap = data.missing(KEY)
    if gap:
        return common.needs(data, KEY, period, gap)
    a, b = common.epoch(period['start']), common.epoch(period['end'])
    filled = [k for k in data.baskets if k['units']]
    currency = next((k['currency'] for k in filled if k['amount'] is not None), None)
    size = (lambda k: float(k['amount'] or 0)) if currency else (lambda k: k['units'])
    def measure(x, y):
        hits = [size(k) for k in filled if common.within(k, x, y)]
        return sum(hits) / len(hits) if hits else 0
    inside = sorted((k for k in filled if common.within(k, a, b)), key=lambda k: (-k['t'], k['id']))
    def row(basket):
        names = ', '.join(f"{line['quantity']} x {line['label']}" for line in basket['lines'])
        return {'id': basket['id'], 'at': basket['at'], 'label': 'Basket', 'description': names,
            'thumb': next((line['image'] for line in basket['lines'] if line['image']), None), 'visitor_id': basket['visit_id'],
            'source_id': basket['source_id'], 'camera_id': basket['camera_id'], 'person_id': basket['person_id'],
            'units': basket['units'], 'amount': basket['amount'], 'currency': basket['currency'], 'basis': basket['basis'],
            'lines': [{'product': line['label'], 'product_id': line['product_id'], 'quantity': line['quantity'],
                       'unit_price': line['unit_price'], 'amount': line['amount'], 'currency': line['currency'],
                       'basis': line['basis'], 'image': line['image']} for line in basket['lines']],
            'timeline': basket['timeline']}
    value = common.number(measure(a, b), 2)
    unit = currency or 'items'
    rows = [row(k) for k in inside]
    summary = f"{len(rows)} visitor{'s' if len(rows) != 1 else ''} held a basket {common.when(period)}."
    if rows:
        summary += f" The average basket is {common.money(value, currency) if currency else common.plural(value, unit)}."
    return common.publish(data, KEY, period, value=value, unit=unit, series=common.series(period, measure),
        trend=common.trend(period, measure), events=rows,
        canvas={'rows': rows[:common.CANVAS_ROWS], 'total_rows': len(rows), 'baskets': len(rows),
                'units': sum(k['units'] for k in inside), 'basis': 'estimated' if currency else None}, summary=summary,
        tile_extra={'currency': currency, 'basis': 'estimated' if currency else None})
