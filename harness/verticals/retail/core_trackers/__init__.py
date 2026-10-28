"""Core Trackers: the maintained tracker recipe a business type gets for each location.

Layers, in order:

    tracker    the observed event stream of one kind for a location, per camera
    processor  the script that reads that stream with the catalog and sales and writes one
               output revision (a Tracking Agent's processor may wake its agent for a new summary
               edition; a Core Tracker computes its own summary)
    canvas     the full view data of that revision
    tile       the compact summary of the same revision

Hierarchy: company -> location -> camera -> tracker -> output (tile + canvas).

`install_core_trackers` gives a location the recipe of its business type. Core Trackers are
locked: every location of that type has the same set, with `core: true` and `chat: false`. A
company's own trackers sit beside them in the same list with `core: false`.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import uuid

RECIPES = {'retail': Path(__file__).with_name('retail.json')}
LOCKED = 'Core Trackers come with every location of this business type. Add your own tracker beside them.'


def load_recipe(business_type='retail'):
    if business_type not in RECIPES:
        raise ValueError('Choose a business type with a maintained tracker recipe.')
    return json.loads(RECIPES[business_type].read_text())


def core_scripts(business_type):
    """The maintained processor script of each Core Tracker of a business type, by tracker key."""
    try:
        recipe = load_recipe(business_type)
    except (ValueError, TypeError):
        return {}
    return {entry['key']: entry['processor']['script'] for entry in recipe['trackers']}


def core_script(tracker, scripts=None):
    """The script this service runs for a tracker record in its own process, else None.

    A Core Tracker runs the script its recipe names, and only that one: the record's own
    `processor.script` has to equal it. A company's tracker carries a script of the company's
    choosing, so its script stays out of this process whatever the record says.
    """
    if tracker.get('core') is not True:
        return None
    known = scripts if scripts is not None else {}
    kind = tracker.get('business_type')
    if not isinstance(kind, str):
        return None
    if kind not in known:
        known[kind] = core_scripts(kind)
    script = (tracker.get('processor') or {}).get('script')
    return script if script and known[kind].get(tracker.get('key')) == script else None


def tracker_id(company_id, location_id, key):
    from retail_records import digest
    return 'tracker-' + digest([company_id, location_id, key])


def install_core_trackers(company, location_id, business_type='retail'):
    """Record the business type's Core Trackers for one location. Safe to repeat."""
    import retail_records as records
    recipe = load_recipe(business_type)
    company.locations.authorized(company.owner, company.collection, location_id, write=True)
    trackers = []
    for order, entry in enumerate(recipe['trackers']):
        trackers.append(records.stamp(company, location_id, {
            'id': tracker_id(company.id, location_id, entry['key']), 'key': entry['key'], 'label': entry['label'],
            'core': True, 'chat': False, 'locked': True, 'order': order, 'business_type': business_type,
            'recipe_revision': recipe['revision'], 'playbook': recipe['playbook'], 'interest': entry['interest'],
            'tracker': entry['tracker'], 'processor': entry['processor'], 'canvas': entry['canvas'],
            'tile': entry['tile'], 'requires': entry['requires'], 'agent': {'kind': 'core_tracker', 'chat': False}}))
    records.put_many(company, 'tracker', location_id, trackers)
    records.put_many(company, 'tracker_installation', location_id, [records.stamp(company, location_id, {
        'id': location_id, 'business_type': business_type, 'recipe_revision': recipe['revision'],
        'playbook': recipe['playbook'], 'tracker_ids': [t['id'] for t in trackers]})])
    return installed_trackers(company, location_id)


def installed_trackers(company, location_id):
    """The location's trackers: core first in recipe order, then the company's own."""
    import retail_records as records
    return sorted(records.rows(company, 'tracker', location_id), key=lambda t: (not t['core'], t.get('order', 0), t['key']))


def add_tracker(company, location_id, key, label, observes, *, event_types=(), script=None, agent_id=None):
    """Add one of the company's own trackers beside the core set.

    `agent_id` links the record to the Tracking Agent whose conversation manages it: the location
    then lists one row for the two, and the agent's processing script publishes for this record.
    """
    import retail_records as records
    if any(t['key'] == key for t in installed_trackers(company, location_id)):
        raise ValueError('Choose a new tracker name for this location.')
    value = records.stamp(company, location_id, {'id': tracker_id(company.id, location_id, key), 'generation_id': uuid.uuid4().hex, 'key': key, 'label': label,
        'core': False, 'chat': True, 'locked': False, 'order': 0, 'revision': 1, 'business_type': None, 'recipe_revision': None,
        'playbook': None, 'interest': None, 'tracker': {'observes': observes, 'stream': 'events', 'event_types': list(event_types)},
        'processor': {'script': script, 'reads': ['events'], 'summary': 'agent'}, 'canvas': {'family': 'occurrences'},
        'tile': {'family': 'metric', 'unit': 'events'}, 'requires': ['camera'],
        'agent': {'kind': 'tracking_agent', 'chat': True, **({'agent_id': agent_id} if agent_id else {})},
        **({'agent_id': agent_id} if agent_id else {})})
    records.put_many(company, 'tracker', location_id, [value])
    return value


def agent_tracker_key(agent_id):
    """The tracker key of a Tracking Agent's record: stable, and outside every recipe's keys."""
    return 'agent_' + ''.join(c for c in agent_id.removeprefix('agent-').lower() if c.isalnum())[:32]


def agent_tracker(state, owner, collection, agent_id):
    """The tracker record linked to a Tracking Agent, once it has one."""
    return next((record for record in tracker_records(state, owner, collection) if record.get('agent_id') == agent_id), None)


def link_tracking_agent(company, location_id, agent, *, event_types=()):
    """The tracker record of a Tracking Agent at a location, written once through `add_tracker`.

    An agent keeps the one record it has: a second call returns it, whatever location is named.
    """
    linked = agent_tracker(company.state, company.owner, company.collection, agent['id'])
    if linked is not None:
        return linked
    label = ' '.join(str(agent.get('title') or 'Tracking Agent').split())[:80]
    observes = ' '.join(str(agent.get('description') or agent.get('instruction') or label).split())[:500]
    return add_tracker(company, location_id, agent_tracker_key(agent['id']), label, observes,
                       event_types=event_types, agent_id=agent['id'])


def remove_tracker(company, location_id, key):
    """Remove one of the company's own trackers. Core Trackers stay."""
    tracker = next((t for t in installed_trackers(company, location_id) if t['key'] == key), None)
    if tracker is None:
        raise ValueError('Choose a tracker of this location.')
    if tracker['core']:
        raise ValueError(LOCKED)
    company.locations.authorized(company.owner, company.collection, location_id, write=True)
    suffix = tracker['id'].removeprefix('tracker-')
    with company.locations.connect(True) as db:
        db.execute("DELETE FROM records WHERE owner=? AND kind='tracker' AND id=?", (company.owner, tracker['id']))
        from core_trackers.presentation import forget
        forget(db, company.owner, tracker)
        # What it published and its processing script go with it: the output and each kept revision,
        # the script's head and revisions, a waiting proposal and the readings. A tracker's id follows
        # from its key, so a tracker added later under the same key starts with none of them.
        for kind, name in (('tracker_output', 'output-' + suffix), ('tracker_script', 'script-' + suffix),
                           ('tracker_output_proposal', 'proposal-' + suffix), ('tracker_script_readings', 'readings-' + suffix)):
            db.execute('DELETE FROM records WHERE owner=? AND kind=? AND id=?', (company.owner, kind, name))
        for kind, prefix in (('tracker_output_revision', 'output-'), ('tracker_script_revision', 'script-')):
            db.execute('DELETE FROM records WHERE owner=? AND kind=? AND id LIKE ?', (company.owner, kind, f'{prefix}{suffix}-r%'))
    # Its runs leave the location's run history with it.
    from tracker_runner import TrackerRuns
    TrackerRuns(company.state).forget_tracker(company.owner, company.collection, location_id, key)
    return tracker


def update_tracker(company, location_id, key, changes, *, expected_revision=None):
    """Change one of the company's own trackers. Core Trackers follow their recipe.

    Each change gives the tracker its next revision. A caller that names `expected_revision`
    changes the revision it read.
    """
    import retail_records as records
    tracker = next((t for t in installed_trackers(company, location_id) if t['key'] == key), None)
    if tracker is None:
        raise ValueError('Choose a tracker of this location.')
    if tracker['core']:
        raise ValueError(LOCKED)
    revision = tracker.get('revision', 1)
    if expected_revision is not None and expected_revision != revision:
        raise ValueError('Read the tracker again, then change it with its current revision.')
    allowed = {k: v for k, v in changes.items() if k in {'label', 'tracker', 'processor'}}
    if 'processor' in allowed:
        # The record's `script` names code this service imports for a Core Tracker. A change keeps
        # the value the record has; a company's code is saved as a script and runs in the sandbox.
        supplied = allowed['processor'] if isinstance(allowed['processor'], dict) else {}
        allowed['processor'] = {**{k: v for k, v in supplied.items() if k != 'script'},
                                'script': (tracker.get('processor') or {}).get('script')}
    value = {**tracker, **allowed, 'revision': revision + 1}
    records.put_many(company, 'tracker', location_id, [value])
    return value


def tracker_records(state, owner, collection):
    """The owner's saved tracker records of a collection, read without opening the company for writing."""
    path = Path(state) / 'location-intelligence.sqlite'
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM records WHERE owner=? AND kind='tracker' AND collection=?", (owner, collection))]
    except sqlite3.OperationalError:
        return []


def location_trackers(state, owner, collection, location_id=None, *, agents=None, allowed=lambda record: True):
    """What the harness lists for a location: Core Trackers, then the customer's own.

    Core Trackers carry `core: true`, `chat: false` and are locked against edit and delete.
    The company's own trackers and its Tracking Agents carry `core: false`, `chat: true`.
    """
    saved = tracker_records(state, owner, collection)
    listing = []
    for record in sorted(saved, key=lambda t: (not t['core'], t['location_id'], t.get('order', 0), t['key'])):
        if (location_id and record['location_id'] != location_id) or not allowed(record):
            continue
        listing.append({'id': record['id'], 'key': record['key'], 'label': record['label'], 'core': record['core'],
            'chat': record['chat'], 'locked': record['locked'], 'editable': not record['locked'],
            'removable': not record['locked'], 'kind': record['agent']['kind'], 'company_id': record['company_id'],
            'location_id': record['location_id'], 'business_type': record['business_type'],
            'observes': record['tracker']['observes'], 'requires': record['requires'],
            'layers': {'tracker': record['tracker'], 'processor': record['processor'],
                       'canvas': record['canvas'], 'tile': record['tile']},
            **({'agent_id': record['agent_id']} if record.get('agent_id') else {})})
    if agents is not None:
        # A Tracking Agent with a tracker record is one row: the record's, with the agent's state.
        linked = {row['agent_id']: row for row in listing if row.get('agent_id')}
        every = {record['agent_id'] for record in saved if record.get('agent_id')}
        for agent in agents.list(owner_principal_id=owner):
            if agent.get('agent_kind') == 'tracker' and agent.get('dataset') == collection and agent['id'] in every:
                if agent['id'] in linked:
                    linked[agent['id']].update(url=agent.get('url'), status=agent.get('status'))
                continue
            if agent.get('agent_kind') == 'tracker' and agent.get('dataset') == collection:
                listing.append({'id': agent['id'], 'key': agent['id'], 'label': agent['title'], 'core': False,
                    'chat': True, 'locked': False, 'editable': True, 'removable': True, 'kind': 'tracking_agent',
                    'agent_id': agent['id'], 'url': agent.get('url'), 'status': agent.get('status'),
                    'location_id': None, 'observes': agent.get('description') or agent['title']})
    return listing
