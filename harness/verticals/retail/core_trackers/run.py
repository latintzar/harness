"""Run the installed trackers of one location and store each output with a revision.

    python -m core_trackers.run --state STATE --access-state ACCESS --environment-id ID \
        --owner OWNER --location-id LOCATION [--day YYYY-MM-DD | --days N] [--only KEY] [--publish]

The run first accumulates the location's committed events into items, baskets, sales, shelf
states and queue intervals (`retail_records.derive`), then calls each tracker's processor script.
The service runs the same pass by itself (`tracker_runner.py`); this command and the service
share one lease per location, so one pass of a location runs at a time.
"""
from __future__ import annotations

import argparse
import contextvars
from datetime import date, datetime, timezone
import importlib
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import retail_records as records
from core_trackers import common, core_script

SANDBOX = 'Script runs in the sandbox.'
RECIPE_SCRIPT = 'A Core Tracker runs the script of its recipe.'

# Set by a pass: the location's current local day, where the week, month and year readings end.
TODAY = contextvars.ContextVar('tracker_today', default=None)
# Set by a pass around its page publish: (clock, deadline). The readings stop between trackers once
# the clock reaches the deadline, so the time budget holds for the whole pass.
BUDGET = contextvars.ContextVar('tracker_budget', default=None)
# Set by a pass around its script trackers: {'trigger', 'run_id', 'clock', 'deadline', 'day', 'daily'}.
# A company tracker's script run reads what started the pass, the location's local day, whether
# the pass stands for that day, and how much of the pass's script time is left.
PASS = contextvars.ContextVar('tracker_pass', default=None)


class BudgetReached(Exception):
    """The pass reached its time budget while it read the week, the month and the year."""


def zone_day(zone, now=None):
    """The calendar day at `now` (epoch seconds) in a named timezone, UTC without one."""
    moment = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    if zone:
        try:
            moment = moment.astimezone(ZoneInfo(zone))
        except (KeyError, ValueError, OSError):
            pass  # A zone this host has no data for reads as UTC.
    return moment.date()


def location_zone(company, location_id):
    """The timezone of the location's coordinates, when it has them."""
    try:
        config = company.locations._get(company.owner, 'location', location_id)
    except ValueError:
        return None
    return (config.get('coordinates') or {}).get('timezone')


def local_day(company, location_id, now=None):
    """The location's current calendar day: in the timezone of its coordinates, UTC otherwise."""
    return zone_day(location_zone(company, location_id), now)


def observed_day(data):
    """The UTC day of the location's latest observation, once it has one."""
    times = [row['t'] for rows_ in (data.visits, data.events, data.sales) for row in rows_ if row.get('t') is not None]
    return datetime.fromtimestamp(max(times), timezone.utc).date() if times else None


def latest_day(data):
    """The UTC day of the location's latest observation."""
    return observed_day(data) or datetime.now(timezone.utc).date()


def brief(error, what='processor'):
    """A run row's words for a failure: the product's own sentence, or the kind of fault.

    A plain ValueError here is a sentence the product wrote for a person. Any other fault, a
    validation or decoding error included, is named by its kind alone, so a run row carries no
    payload and no file path.
    """
    from business_scope import ScopeError
    if isinstance(error, ScopeError):
        return 'The installing member’s access to this location changed.'
    if type(error) is ValueError and str(error).strip():
        return ' '.join(str(error).split())[:300]
    return f'The {what} stopped with {type(error).__name__}.'


def run_trackers(company, location_id, period=None, *, only=None, deadline=None, custom=None, clock=time.monotonic, defer=False):
    """Derive the location's records, read them once, then run each installed tracker's processor.

    Core Trackers run first, in this process, each from the script its recipe names. A company's
    tracker with a processing script goes to `custom(tracker, company, location_id, period, data)`
    when the caller supplies one and is listed in `skipped` otherwise; its script stays out of this
    process. `custom` returns the stored output, or a dict with `script_run` (what the script's
    run did), `output` (the output it published, when it published one) and `error` (the sentence
    of a run that stopped). One tracker's failure is recorded in `errors` and the others still run.
    `deadline` is a reading of `clock`: once it is reached the pass stops between trackers with
    the status `budget_exceeded`.

    With `defer` the script trackers are left for the caller: the result names them in `deferred`
    and carries the location's `data`, and `run_script_trackers` runs them after the pass's own
    work, in a time of their own. A script job then costs the pass's time budget nothing.

    Returns `period`, `derived`, `outputs` (one stored output per tracker that ran), `errors`
    ([{key, core, error}]), `skipped` ([{key, status, reason}]), `scripts` (one entry per script
    run) and `status`: `completed`, `failed` when a Core Tracker failed, or `budget_exceeded`.
    """
    derived = records.derive(company, location_id)
    data = common.read_location(company, location_id)
    period = period or common.day_period(latest_day(data))
    outputs, errors, skipped, scripts, status, script_runs, deferred = [], [], [], {}, 'completed', [], []
    for tracker in data.trackers:
        key = tracker['key']
        if only and key not in only:
            continue
        if defer and custom is not None and sandboxed(tracker):
            deferred.append(tracker)
            continue
        if deadline is not None and clock() >= deadline:
            status = 'budget_exceeded'
            break
        script = core_script(tracker, scripts)
        try:
            if script:
                outputs.append(importlib.import_module(script).run(company, location_id, period, data))
            elif tracker.get('core'):
                skipped.append({'key': key, 'status': 'skipped', 'reason': RECIPE_SCRIPT})
            elif sandboxed(tracker):
                if custom is None:
                    skipped.append({'key': key, 'status': 'skipped', 'reason': SANDBOX})
                    continue
                _taken(key, custom(tracker, company, location_id, period, data), outputs, errors, script_runs)
        except Exception as exc:  # One tracker's fault stays its own; the run row carries it.
            errors.append({'key': key, 'core': tracker.get('core') is True, 'error': brief(exc)})
    if status == 'completed' and any(error['core'] for error in errors):
        status = 'failed'
    result = {'period': period, 'derived': derived, 'outputs': outputs, 'errors': errors, 'skipped': skipped,
              'scripts': script_runs, 'status': status}
    if defer:
        result.update(deferred=deferred, data=data)
    return result


def _taken(key, output, outputs, errors, script_runs):
    """File what `custom` returned for one script tracker: its run, its sentence and its output."""
    if isinstance(output, dict) and 'script_run' in output:
        if output['script_run']:
            script_runs.append(output['script_run'])
        if output.get('error'):
            errors.append({'key': key, 'core': False, 'error': output['error']})
        output = output.get('output')
    if output:
        outputs.append(output)


def run_script_trackers(company, location_id, period, data, trackers, custom):
    """The script trackers a pass deferred, each through `custom`, in order. The caller sets `PASS`
    around this with the script time of the pass. One tracker's fault stays its own.

    Returns {'outputs', 'errors', 'scripts'} in the shapes of `run_trackers`.
    """
    outputs, errors, script_runs = [], [], []
    for tracker in trackers:
        try:
            _taken(tracker['key'], custom(tracker, company, location_id, period, data), outputs, errors, script_runs)
        except Exception as exc:
            errors.append({'key': tracker['key'], 'core': False, 'error': brief(exc)})
    return {'outputs': outputs, 'errors': errors, 'scripts': script_runs}


def sandboxed(tracker):
    """Whether a company's tracker has a processing script: a published sandbox script, or a
    script name on its record. Either way its code stays out of this process."""
    processor = tracker.get('processor') or {}
    return tracker.get('core') is not True and bool(processor.get('sandbox') or processor.get('script'))


def reading_periods(company, location_id, data, last_day=None):
    """The week, the month and the year a location's trackers are read over: {name: period}."""
    if not last_day:
        last_day = TODAY.get() or local_day(company, location_id)
        last_day = max(last_day, observed_day(data) or last_day)
    return {name: common.days_period(last_day, days) for name, days in PERIOD_DAYS.items()}


PERIOD_DAYS = {'week': 7, 'month': 30, 'year': 365}


def period_readings(company, location_id, last_day=None, data=None):
    """Each tracker's output over the week, the month and the year. Nothing is stored.

    A Core Tracker is computed here. A company's script tracker brings the readings its last
    published run computed in the sandbox, in the same shape.

    The windows end on the location's current local day, so they move with the calendar: the day
    a pass set (`TODAY`), else the day it is now at the location. Days are counted in UTC, and west
    of UTC a location's evening already carries the next UTC date: the windows then end on that
    date, the day the tile itself shows, so every reading holds the newest observation.

    Inside a pass (`BUDGET`) the readings stop between trackers at the pass's deadline with
    `BudgetReached`, before anything is written.
    """
    data = data or common.read_location(company, location_id)
    readings, scripts = {}, {}
    budget = BUDGET.get()
    token = common.READING.set(True)
    try:
        for name, period in reading_periods(company, location_id, data, last_day).items():
            for tracker in data.trackers:
                script = core_script(tracker, scripts)
                if not script:
                    continue
                if budget is not None and budget[0]() >= budget[1]:
                    raise BudgetReached()
                try:
                    readings.setdefault(tracker['key'], {})[name] = importlib.import_module(script).run(company, location_id, period, data)
                except Exception:
                    continue  # The tracker keeps its other readings; its run row carries the fault.
    finally:
        common.READING.reset(token)
    if any(sandboxed(tracker) for tracker in data.trackers):
        try:
            from tracker_scripts import saved_readings
            for key, saved in saved_readings(company, location_id, data.trackers).items():
                readings.setdefault(key, saved)
        except Exception:
            pass  # A script tracker then shows its day; its run row carries the fault.
    return readings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ('state', 'access-state'):
        parser.add_argument('--' + flag, required=True, type=Path)
    for flag in ('environment-id', 'owner', 'location-id'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--collection', default='retail-demo')
    parser.add_argument('--day', type=date.fromisoformat)
    parser.add_argument('--days', type=int, help='Run over the N days that end on --day.')
    parser.add_argument('--only', action='append')
    parser.add_argument('--publish', action='store_true', help='Write the page data of the location after the run.')
    args = parser.parse_args(argv)
    company = records.company_from_access(args.state, args.access_state, args.environment_id, args.owner, args.collection)
    period = None
    if args.day:
        period = common.days_period(args.day, args.days) if args.days else common.day_period(args.day)
    from tracker_runner import host_pass, location_name
    page = None
    with host_pass(company, args.location_id) as held:
        result = run_trackers(company, args.location_id, period, only=args.only)
        if args.publish:
            from import_control_room_bundle import publish_core_trackers
            page = publish_core_trackers(company, args.location_id, location_name=location_name(company, args.location_id))
        # A run over a chosen day or a chosen few leaves the location's own pass to the service: the
        # lease records that these outputs are another reading, and the next probe runs the whole pass.
        held.done(result, page, covers_inputs=period is None and not args.only and args.publish)
    print(json.dumps({'period': result['period'], 'derived': result['derived'], 'status': result['status'],
        'errors': result['errors'], 'skipped': result['skipped'], 'page': page, 'tiles': [
        {k: o['tile'][k] for k in ('key', 'label', 'value', 'unit', 'state', 'needs', 'revision')} for o in result['outputs']]}, indent=2))
    return result


if __name__ == '__main__':
    main()
