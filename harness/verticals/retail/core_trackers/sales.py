"""Core Tracker processor: sales.

tracker   till transactions, or the basket each visitor leaves with when the till is absent
processor adds up the transactions of the period; till rows take the place of estimates when they exist
canvas    each transaction with its lines: product, quantity, price and basis ("pos" or "estimated")
tile      the sales total, its series and the newest transactions
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'sales'
def chosen(data, a, b):
    """Till transactions when the period has them, else the estimated sales."""
    inside = [s for s in data.sales if common.within(s, a, b)]
    till = [s for s in inside if s['basis'] == 'pos']
    return till or inside


def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    gap = data.missing(KEY)
    if gap:
        return common.needs(data, KEY, period, gap)
    if all(s['total'] is None for s in data.sales):
        return common.needs(data, KEY, period, data.recipe['needs']['catalog_prices'])
    a, b = common.epoch(period['start']), common.epoch(period['end'])
    measure = lambda x, y: sum(float(s['total'] or 0) for s in chosen(data, x, y))
    inside = sorted(chosen(data, a, b), key=lambda s: (-s['t'], s['id']))
    currency = next((s['currency'] for s in data.sales if s['currency']), None)
    def row(sale):
        lines = data.sale_lines.get(sale['id'], [])
        return {'id': sale['id'], 'at': sale['at'], 'label': 'Sale',
            'description': ', '.join(f"{line['quantity']} x {line['label']}" for line in lines),
            'thumb': None, 'visitor_id': sale.get('visit_id'), 'source_id': sale.get('source_id'), 'camera_id': sale['camera_id'],
            'basis': sale['basis'], 'total': sale['total'], 'currency': sale['currency'], 'units': sale['units'],
            'lines': [{'product': line['label'], 'product_id': line['product_id'], 'quantity': line['quantity'],
                       'unit_price': line['unit_price'], 'amount': line['amount'], 'currency': line['currency'],
                       'basis': line['basis']} for line in lines]}
    rows = [row(s) for s in inside]
    value = common.number(measure(a, b), 2)
    basis = 'pos' if any(s['basis'] == 'pos' for s in inside) else 'estimated'
    summary = f"{common.money(value, currency) if currency else value} in {len(rows)} sale{'s' if len(rows) != 1 else ''} {common.when(period)}."
    if rows:
        summary += f" The average sale is {common.money(value / len(rows), currency) if currency else common.number(value / len(rows), 2)}."
    return common.publish(data, KEY, period, value=value, unit=currency, series=common.series(period, measure),
        trend=common.trend(period, measure), events=rows,
        canvas={'rows': rows[:common.CANVAS_ROWS], 'total_rows': len(rows), 'basis': basis, 'transactions': len(rows),
                'units': sum(s['units'] for s in inside)}, summary=summary, tile_extra={'currency': currency, 'basis': basis})
