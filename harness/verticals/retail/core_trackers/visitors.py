"""Core Tracker processor: visitors.

tracker   tracked visitor sessions from the person tracker
processor counts the sessions that start in the period and measures their time in view
canvas    one row per visitor with camera, first and last time, time in view and number of actions
tile      the visitor count, its series and the newest visitors
"""
from core_trackers import common
from core_trackers.common import read_location

KEY = 'visitors'
def run(company, location_id, period, data=None):
    data = data or read_location(company, location_id)
    gap = data.missing(KEY)
    if gap:
        return common.needs(data, KEY, period, gap)
    a, b = common.epoch(period['start']), common.epoch(period['end'])
    actions = {}
    for event in data.events:
        if event['visit_id']:
            actions[event['visit_id']] = actions.get(event['visit_id'], 0) + 1
    inside = sorted((v for v in data.visits if common.within(v, a, b)), key=lambda v: (-v['t'], v['id']))
    measure = lambda x, y: sum(1 for v in data.visits if common.within(v, x, y))
    def row(visit):
        seconds = round(visit['last_s'] - visit['first_s'])
        done = actions.get(visit['id'], 0)
        return {'id': visit['id'], 'at': common.records.iso(visit['t']), 'label': 'Visitor',
            'description': f"In view for {seconds} s with {done} action{'s' if done != 1 else ''}",
            'thumb': None, 'visitor_id': visit['id'], 'source_id': visit['source_id'], 'camera_id': visit['camera_id'],
            'person_id': visit['person_id'], 'at_s': visit['first_s'], 'last_at': common.records.iso(visit['t_end']),
            'seconds_in_view': seconds, 'actions': done}
    points, daily = common.series(period, measure), common.trend(period, measure)
    rows = [row(v) for v in inside]
    dwell = common.number(sum(r['seconds_in_view'] for r in rows) / len(rows)) if rows else 0
    summary = common.count_summary(len(rows), 'visitors', period, points, daily)
    if rows:
        summary += f" A visitor stays in view for {dwell} s on average."
    return common.publish(data, KEY, period, value=len(rows), unit='visitors', series=points, trend=daily, events=rows,
        canvas={'rows': rows[:common.CANVAS_ROWS], 'total_rows': len(rows), 'cameras': common.by_camera(data, inside),
                'average_seconds_in_view': dwell}, summary=summary)
