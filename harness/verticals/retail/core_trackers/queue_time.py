"""Core Tracker processor: queue time.

tracker   queue-joined and till-reached events in the queue zone
processor pairs each visitor's queue start and end into an interval and averages the waits
canvas    the intervals and their distribution
tile      the average wait in seconds, its series and the newest waits
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'queue_time'
BANDS = ((0, 10, 'under 10 s'), (10, 20, '10 to 20 s'), (20, 30, '20 to 30 s'), (30, 10**9, '30 s and over'))


def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    gap = data.missing(KEY)
    if gap:
        return common.needs(data, KEY, period, gap)
    a, b = common.epoch(period['start']), common.epoch(period['end'])
    def measure(x, y):
        waits = [q['wait_s'] for q in data.queues if common.within(q, x, y)]
        return sum(waits) / len(waits) if waits else 0
    inside = sorted((q for q in data.queues if common.within(q, a, b)), key=lambda q: (-q['t'], q['id']))
    rows = [{'id': q['id'], 'at': q['end_at'], 'label': 'Queue wait', 'description': f"Waited {common.number(q['wait_s'])} s",
             'thumb': None, 'visitor_id': q['visit_id'], 'source_id': q['source_id'], 'camera_id': q['camera_id'],
             'person_id': q['person_id'], 'start_at': q['start_at'], 'end_at': q['end_at'], 'wait_s': q['wait_s']} for q in inside]
    value = common.number(measure(a, b))
    longest = max((q['wait_s'] for q in inside), default=0)
    summary = f"{len(rows)} visitor{'s' if len(rows) != 1 else ''} queued {common.when(period)}."
    if rows:
        summary += f" The average wait is {value} s and the longest is {common.number(longest)} s."
    return common.publish(data, KEY, period, value=value, unit='s', series=common.series(period, measure),
        trend=common.trend(period, measure), events=rows,
        canvas={'rows': rows[:common.CANVAS_ROWS], 'total_rows': len(rows), 'longest_s': common.number(longest),
                'distribution': [{'band': label, 'value': sum(1 for q in inside if low <= q['wait_s'] < high)} for low, high, label in BANDS]},
        summary=summary)
