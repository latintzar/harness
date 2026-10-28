"""Core Tracker processor: empty shelves.

tracker   shelf-empty and shelf-restocked events in the shelf zones
processor keeps the state of each shelf with its "empty since" and "replenished" times
canvas    each shelf with its state and its empty intervals
tile      the number of shelves empty at the end of the period and the newest changes
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'empty_shelves'
def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    gap = data.missing(KEY)
    if gap:
        return common.needs(data, KEY, period, gap)
    a, b = common.epoch(period['start']), common.epoch(period['end'])
    spans = [(shelf, common.epoch(i['empty_since']), common.epoch(i['replenished_at']), i)
             for shelf in data.shelves for i in shelf['intervals'] if i['empty_since']]
    empty_at = lambda moment: sum(1 for _, start, end, _ in spans if start <= moment and (end is None or end > moment))
    started = lambda x, y: sum(1 for _, start, _, _ in spans if x <= start < y)
    value = empty_at(b - 1)
    changes = []
    for shelf, start, end, interval in spans:
        if a <= start < b:
            changes.append({'id': interval['empty_event_id'], 'at': interval['empty_since'], 'label': 'Shelf empty',
                'description': f"{shelf['label']} is empty", 'thumb': None, 'visitor_id': None, 'source_id': None,
                'camera_id': shelf['camera_id'], 'shelf': shelf['shelf']})
        if end is not None and a <= end < b:
            changes.append({'id': interval['replenished_event_id'], 'at': interval['replenished_at'], 'label': 'Shelf replenished',
                'description': f"{shelf['label']} is replenished after {common.number((end - start) / 60)} min",
                'thumb': None, 'visitor_id': None, 'source_id': None, 'camera_id': shelf['camera_id'], 'shelf': shelf['shelf']})
    changes.sort(key=lambda c: (c['at'], c['id']), reverse=True)
    shelves = [{'shelf': s['shelf'], 'label': s['label'], 'camera_id': s['camera_id'], 'camera': data.camera_name(s['camera_id']),
                'state': s['state'], 'empty_since': s['empty_since'] if s['state'] == 'empty' else None,
                'replenished_at': s['replenished_at'],
                'intervals': [{'empty_since': i['empty_since'], 'replenished_at': i['replenished_at']} for i in s['intervals']]}
               for s in data.shelves]
    zones = [{'zone': z['key'], 'label': z['label'], 'camera_id': z['camera_id']} for z in data.zones if z['kind'] == 'shelf']
    times = sorted((end - start) / 60 for _, start, end, _ in spans if end is not None and a <= start < b)
    summary = f"{value} shel{'ves are' if value != 1 else 'f is'} empty at the end of {period['label']}. {started(a, b)} became empty in the period."
    if times:
        summary += f" Replenishment took {common.number(sum(times) / len(times))} min on average."
    return common.publish(data, KEY, period, value=value, unit='shelves',
        series=[{'t': common.records.iso(x), 'value': empty_at(y - 1)} for x, y in common.buckets(period)],
        trend=common.trend(period, started), events=changes,
        canvas={'rows': changes[:common.CANVAS_ROWS], 'total_rows': len(changes), 'shelves': shelves, 'zones': zones,
                'became_empty': started(a, b)}, summary=summary)
