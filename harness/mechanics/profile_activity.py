"""Read-only activity from owned chats, durable runs and measured API receipts.

A saved last-request context snapshot is not a token ledger. Only settled provider
receipts contribute tokens; delivery counts stay unavailable until delivery is recorded.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import stat

UTC = timezone.utc
MAX_ROWS = 100_000
FIELDS = ('tokens', 'input_tokens', 'cached_input_tokens', 'output_tokens',
          'chats', 'agent_runs', 'agent_runs_completed', 'alerts_sent')


@contextmanager
def reader(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError('Activity records are being prepared.')
    db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


def _agent_pool_path(state):
    from company_usage import CompanyUsage
    return CompanyUsage.agent_pool_path(state)


def bind_model_run(state, run_id, owner):
    """Host-only forward attribution before dispatch; the model supplies neither ID."""
    if (not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', run_id)
            or not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', owner)):
        raise ValueError('Bind model activity to the saved workspace owner.')
    state = Path(state)
    path = state / 'profile-activity.sqlite'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('Keep activity ownership in the private workspace store.')
    finally:
        os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    try:
        db.executescript("""CREATE TABLE IF NOT EXISTS model_owners (
            run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, bound_at TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS immutable_activity_owner_update BEFORE UPDATE ON model_owners
                BEGIN SELECT RAISE(ABORT,'Activity ownership is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_activity_owner_delete BEFORE DELETE ON model_owners
                BEGIN SELECT RAISE(ABORT,'Activity ownership is immutable'); END;""")
        with db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT owner FROM model_owners WHERE run_id=?', (run_id,)).fetchone()
            if prior:
                if prior[0] != owner:
                    raise ValueError('Preserve the model run’s original owner.')
                return
            gateway = _agent_pool_path(state) / 'gateway/runs.sqlite'
            if gateway.exists():
                with reader(gateway) as requests:
                    if requests.execute('SELECT 1 FROM requests WHERE run=? LIMIT 1', (run_id,)).fetchone():
                        raise ValueError('Record activity ownership before the first model request.')
            db.execute('INSERT INTO model_owners VALUES(?,?,?)', (run_id, owner, datetime.now(UTC).isoformat()))
    finally:
        db.close()


def _attribution(state, owner):
    with reader(state / 'profile-activity.sqlite') as db:
        rows = db.execute('SELECT run_id,bound_at FROM model_owners WHERE owner=? LIMIT ?', (owner, MAX_ROWS + 1)).fetchall()
    if len(rows) > MAX_ROWS:
        raise ValueError('Activity record limit reached.')
    bindings = {run: stamp(at) for run, at in rows}
    if not bindings or any(at is None for at in bindings.values()):
        return {}, None
    with reader(_agent_pool_path(state) / 'gateway/runs.sqlite') as db:
        requests = db.execute('SELECT id,run FROM requests LIMIT ?', (MAX_ROWS + 1,)).fetchall()
    if len(requests) > MAX_ROWS:
        raise ValueError('Activity record limit reached.')
    return {key: bindings[run] for key, run in requests if run in bindings}, min(bindings.values())


def stamp(value):
    """Known persisted seconds or explicitly zoned ISO timestamps, normalized UTC."""
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str) and len(value) <= 64:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.astimezone(UTC) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError):
        pass
    return None


def access_scope(access_path, session, scope):
    """Fresh membership for the selected host company, including owner observation."""
    if scope not in {'person', 'organisation'} or not session or not session.get('authenticated'):
        raise PermissionError('Open activity from your workspace.')
    environment = session.get('environment', {}).get('id')
    actor = session.get('principal', {}).get('id')
    viewing = session.get('viewing_as')
    target = viewing.get('principal', {}).get('id') if viewing else actor
    if (session.get('role') not in {'owner', 'operator'} or
            (not viewing and not session.get('can_query')) or
            (viewing and (session.get('role') != 'owner' or viewing.get('environment', {}).get('id') != environment))):
        raise PermissionError('Open activity from your workspace.')
    with reader(access_path) as db:
        def member(principal):
            return db.execute('''SELECT p.display_name,m.role,e.name FROM principals p
                JOIN principal_environments m ON m.principal_id=p.id
                JOIN environments e ON e.id=m.environment WHERE p.id=? AND e.id=?''',
                (principal, environment)).fetchone()
        own, selected = member(actor), member(target)
    if not own or not selected or own[1] not in {'owner', 'operator'}:
        raise PermissionError('Open activity from a current workspace membership.')
    if viewing and own[1] != 'owner':
        raise PermissionError('Open activity from your workspace.')
    organisation_allowed = not viewing and session.get('role') == own[1] == 'owner'
    if scope == 'organisation' and not organisation_allowed:
        raise PermissionError('Open organisation activity from your owner workspace.')
    return {'principal_id': target, 'identity': {'name': own[2] if scope == 'organisation' else selected[0],
            'avatar_url': None}, 'organisation_allowed': organisation_allowed}


def _json(value):
    try:
        return json.loads(value) if isinstance(value, str) and len(value) <= 2_000_000 else None
    except (ValueError, TypeError):
        return None


def _sum(rows):
    rows = list(rows)
    return {key: None if any(row[key] is None for row in rows) else sum(row[key] for row in rows)
            for key in FIELDS}


def activity(state, *, scope, principal_id, identity, organisation_allowed, chats, days=364, now=None):
    if scope not in {'person', 'organisation'} or type(days) is not int or not 1 <= days <= 366:
        raise ValueError('Choose an activity window of 1 to 366 days.')
    if not isinstance(principal_id, str) or not principal_id or (scope == 'organisation' and not organisation_allowed):
        raise PermissionError('Open activity from your workspace.')
    state = Path(state)
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError('Use an explicit activity timezone.')
    now = now.astimezone(UTC)
    end = now.date()
    start = end - timedelta(days=days - 1)
    series = {str(start + timedelta(days=i)): {key: 0 for key in FIELDS} for i in range(days)}
    for row in series.values():
        row.update(tokens=None, input_tokens=None, cached_input_tokens=None, output_tokens=None, alerts_sent=None)
    metrics = {'tokens': {'available': False, 'coverage': 'unavailable', 'reason': 'owner_attribution_unavailable'},
        'chats': {'available': True, 'coverage': 'saved_root_chats'},
        'agent_runs': {'available': True, 'coverage': 'saved_agent_runs'},
        'alerts_sent': {'available': False, 'coverage': 'unavailable', 'reason': 'delivery_tracking_unavailable'}}
    def owned(row):
        owner = row.get('owner_principal_id') if isinstance(row, dict) else None
        return isinstance(owner, str) and bool(owner) and (scope == 'organisation' or owner == principal_id)
    def day(value):
        parsed = stamp(value)
        return series.get(str(parsed.date())) if parsed and parsed <= now else None
    seen = set()
    for chat in chats:
        if not owned(chat) or chat.get('parent_id') or chat.get('kind') == 'agent_run' or not isinstance(chat.get('id'), str) or chat['id'] in seen:
            continue
        seen.add(chat['id'])
        if stamp(chat.get('created_at')) is None:
            metrics['chats']['undated_records'] = metrics['chats'].get('undated_records', 0) + 1
        row = day(chat.get('created_at'))
        if row is not None:
            row['chats'] += 1
    agents = {}
    try:
        with reader(state / 'background-agents.sqlite') as db:
            definitions = {key: _json(body) for key, body in db.execute('SELECT id,body FROM agents LIMIT 101')}
            runs = db.execute('SELECT body FROM runs LIMIT ?', (MAX_ROWS + 1,)).fetchall()
        if len(runs) > MAX_ROWS or len(definitions) > 100:
            raise ValueError('Activity record limit reached.')
        for (body,) in runs:
            run = _json(body)
            if not owned(run):
                continue
            definition = definitions.get(run.get('agent_id'))
            if not owned(definition) or definition.get('owner_principal_id') != run.get('owner_principal_id'):
                continue
            row = day(run.get('created_at'))
            finished = day(run.get('completed_at')) if run.get('status') == 'completed' else None
            if row is None and finished is None:
                continue
            if row is not None:
                row['agent_runs'] += 1
            if finished is not None:
                finished['agent_runs_completed'] += 1
            key = run['agent_id']
            item = agents.setdefault(key, {'id': key, 'title': definition['title'], 'url': '/?agent=' + key,
                'runs': 0, 'completed': 0, 'last_run_at': None})
            item['runs'] += int(row is not None)
            item['completed'] += int(finished is not None)
            when = (stamp(run.get('created_at')) or stamp(run['completed_at'])).isoformat()
            item['last_run_at'] = max(item['last_run_at'] or when, when)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        metrics['agent_runs'] = {'available': False, 'coverage': 'unavailable', 'reason': 'run_records_unavailable'}
        agents = {}
        for row in series.values():
            row['agent_runs'] = row['agent_runs_completed'] = None
    if scope in {'person', 'organisation'}:
        try:
            attribution, since = ({}, None) if scope == 'organisation' else _attribution(state, principal_id)
            if scope == 'person' and since is None:
                raise FileNotFoundError('Owner activity attribution starts with the next model run.')
            from workspace_identity import IDENTITY_FILE, _read
            company = _read(state / IDENTITY_FILE)
            with reader(_agent_pool_path(state) / 'company-usage.sqlite') as db:
                binding = db.execute('SELECT provider,parent_company_scope FROM pool_binding WHERE singleton=1').fetchone()
                if not company or binding != ('openai', company['company_scope']):
                    raise ValueError('Activity pool belongs to another company.')
                events = db.execute("SELECT at,data FROM events WHERE kind='settled' ORDER BY sequence LIMIT ?", (MAX_ROWS + 1,)).fetchall()
            if len(events) > MAX_ROWS:
                raise ValueError('Activity record limit reached.')
            for date, row in series.items():
                for key in ('tokens', 'input_tokens', 'cached_input_tokens', 'output_tokens'):
                    row[key] = 0 if since is None or date >= str(since.date()) else None
            invalid = False
            for at, body in events:
                event = _json(body)
                source = event.get('source', {}) if isinstance(event, dict) else {}
                if not isinstance(source, dict) or source.get('provider') != 'openai':
                    invalid = True
                    continue
                usage = source.get('usage') or {}
                when = stamp(source.get('received_at'))
                if scope == 'person':
                    bound_at = attribution.get(source.get('request_id'))
                    if bound_at is None:
                        continue
                    if when is None or when < bound_at:
                        invalid = True
                        continue
                if not when or not isinstance(usage, dict) or any(type(usage.get(k)) is not int or not 0 <= usage[k] <= 100_000_000
                    for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')) or usage['cached_input_tokens'] > usage['input_tokens']:
                    invalid = True
                    continue
                row = day(when.isoformat())
                if row is not None:
                    row['input_tokens'] += usage['input_tokens']
                    row['cached_input_tokens'] += usage['cached_input_tokens']
                    row['output_tokens'] += usage['output_tokens']
                    row['tokens'] += usage['input_tokens'] + usage['output_tokens']
            metrics['tokens'] = {'available': True, 'coverage': 'partial_settled_receipts' if invalid else ('attributed_api_receipts' if scope == 'person' else 'settled_api_receipts'),
                'since': since.isoformat() if since else None,
                'source': 'openai_usage_ledger', 'definition': 'Input plus output tokens; cached input is included once.',
                'owner_attribution': 'forward_only',
                'includes_subscription_usage': False, 'includes_unsettled_requests': False}
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            metrics['tokens'] = {'available': False, 'coverage': 'unavailable',
                'reason': 'owner_attribution_unavailable' if scope == 'person' else 'token_records_unavailable'}
            for row in series.values():
                for key in ('tokens', 'input_tokens', 'cached_input_tokens', 'output_tokens'):
                    row[key] = None
    # Count accepted operational deliveries once; OTPs and enrollment greetings
    # are setup actions. The member is the verified delivery recipient.
    try:
        with reader(state / 'messaging.sqlite') as db:
            deliveries = db.execute("""SELECT member,created FROM outbox
                WHERE kind IN ('text','utility') AND status IN ('sent','delivered','read')
                AND provider_message_id IS NOT NULL LIMIT ?""", (MAX_ROWS + 1,)).fetchall()
        if len(deliveries) > MAX_ROWS:
            raise ValueError('Activity record limit reached.')
        for row in series.values():
            row['alerts_sent'] = 0
        for member, created in deliveries:
            if scope == 'organisation' or member == principal_id:
                row = day(created)
                if row is not None:
                    row['alerts_sent'] += 1
        metrics['alerts_sent'] = {'available': True, 'coverage': 'provider_accepted_deliveries',
            'definition': 'Operational messages accepted by the provider, grouped by admission date; personal totals use the verified recipient.'}
    except (OSError, sqlite3.Error, ValueError):
        pass
    daily = [{'date': date, **row} for date, row in series.items()]
    weeks = {}
    for row in daily:
        date = datetime.fromisoformat(row['date']).date()
        monday = date - timedelta(days=date.weekday())
        weeks.setdefault(monday, []).append(row)
    weekly = [{'week': f'{monday.isocalendar().year}-W{monday.isocalendar().week:02d}',
        'start_date': group[0]['date'], 'end_date': group[-1]['date'], **_sum(group)}
        for monday, group in weeks.items()]
    return {'schema_version': 1, 'scope': scope, 'timezone': 'UTC', 'start_date': str(start), 'end_date': str(end),
        'days': days, 'generated_at': now.isoformat(), 'identity': identity, 'organisation_allowed': organisation_allowed,
        'metrics': metrics, 'totals': {key: sum(row[key] for row in daily if row[key] is not None)
            if any(row[key] is not None for row in daily) else None for key in FIELDS}, 'daily': daily, 'weekly': weekly,
        'agents': sorted(agents.values(), key=lambda item: (-item['runs'], item['id']))}
