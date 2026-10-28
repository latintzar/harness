"""The harness's tools for a tracker's presentation: its tile on Home and its canvas.

    get_tracker_presentation    read: the saved presentation, the kit with what each block needs,
                                the fields of the tracker's current output, and each binding the
                                output leaves unfilled
    save_tracker_presentation   write: the tile, the canvas or both, as the next revision

The read reaches the hosted conversation, native sub-agents and a customer's own agent over `/mcp`
through `READ_NAMES`; the local MCP command lists each read as one function in `mcp_server.py`.
The write is a tool of the hosted parent conversation, and a person's canvas pick reaches the same
`save` through `POST /api/agents/{agent_id}/presentation`. A screen draws what these tools saved:
every display setting of a tracker lives here.

A Core Tracker is shown as its recipe draws it, so its read answers that and the recipe's own
layer names. A Tracking Agent ahead of its tracker record gets the record with its first saved
presentation: the change is held to the kit first, so a refused save leaves every store as it was.

The document, its kit and its checks live in `core_trackers/presentation.py`. This module joins
`monitoring_tools.FAMILIES`; its imports of that module stay inside its functions.
"""
from pathlib import Path

READ_NAMES = frozenset({'get_tracker_presentation'})
WRITE_NAMES = frozenset({'save_tracker_presentation'})

UNKNOWN = 'Choose a tracker from list_trackers.'
AGENT_LOCATION = ('A Tracking Agent takes a presentation once its tracker belongs to a location. '
                  'Give it a processing script for a location, then read again.')
REVISION = 'Choose a revision this presentation has kept.'
MAINTAINED = ('A Core Tracker is shown as its recipe draws it, the same at every location of its business type. '
              'Its tile and its full view are maintained with the recipe. A tracker of the company’s own takes a presentation.')


def specs(write=True):
    from monitoring_tools import tool
    values = [tool('get_tracker_presentation',
        'Read how one tracker is shown: its saved presentation (a tile for Home and a canvas for its full view), the kit of '
        'marks, number formats, canvas families and blocks with the data each one needs, the fields of the tracker’s current '
        'output with the shapes they fit, and each binding the output leaves unfilled. Use the id from list_trackers. '
        'Read this before save_tracker_presentation. A Core Tracker answers that its recipe maintains how it is shown.',
        {'tracker_id': {'type': 'string'}, 'revision': {'type': 'integer', 'minimum': 1, 'description': 'An earlier saved revision to read.'}},
        ['tracker_id'])]
    if write:
        values.append(tool('save_tracker_presentation',
            'Save how one of the company’s own trackers is shown, as its next revision. Give the tile, the canvas or both; a part '
            'replaces whole, and a canvas named by its family alone takes that family’s own blocks. Choose every mark, format, '
            'family and block from the kit of get_tracker_presentation and bind each block to a field of the tracker’s output. '
            'Pass the revision you read as expected_revision. The tracker, its script and its agent keep running as they are. '
            'Core Trackers keep the presentation of their recipe.',
            {'tracker_id': {'type': 'string'}, 'expected_revision': {'type': 'integer', 'minimum': 0},
             'tile': {'type': 'object', 'description': 'label, mark, format, unit for a count, and bind {series, fraction}.'},
             'canvas': {'type': 'object', 'description': 'family and blocks: a list of {block, bind, title, chart} in drawing order.'}},
            ['tracker_id']))
    return values


def tracker_of(state, owner, collection, tracker_id):
    """The tracker record an id names: the record's own id, or the id of its Tracking Agent."""
    from core_trackers import tracker_records
    if not isinstance(tracker_id, str) or not tracker_id:
        return None
    return next((record for record in tracker_records(state, owner, collection)
                 if tracker_id in (record['id'], record.get('agent_id'))), None)


def agent_tracker_record(company, agent, change, *, profiles=(), expected_revision=None, allowed=lambda location_id: True):
    """The tracker record a save for a Tracking Agent writes to.

    An agent that has its record keeps it. An agent ahead of its record gets one at the location
    its observation profiles name, else the one location of its collection, once the change is a
    first presentation the kit accepts: the checks run before the record is written, so a refused
    save leaves the agent as it was. An agent whose location stays open answers one sentence.
    """
    import core_trackers
    from core_trackers import presentation as kit
    record = tracker_of(company.state, company.owner, company.collection, agent['id'])
    if record is not None:
        return record
    location_id = next((profile.get('location_id') for profile in profiles if profile.get('location_id')), None)
    if location_id is None:
        try:
            places = company.scope.describe(company.actor, company.collection)['locations']
        except ValueError:
            places = []
        location_id = places[0]['id'] if len(places) == 1 else None
    link = getattr(core_trackers, 'link_tracking_agent', None)
    if location_id is None or link is None:
        raise ValueError(AGENT_LOCATION)
    if not allowed(location_id):
        raise ValueError(UNKNOWN)
    try:
        company.locations.authorized(company.owner, company.collection, location_id, write=True)
    except ValueError:
        raise ValueError(kit.MANAGEMENT) from None
    if expected_revision not in (None, 0):
        raise ValueError(kit.READ_AGAIN)
    draft = {'label': ' '.join(str(agent.get('title') or 'Tracking Agent').split())}
    kit.validate_presentation(kit.merge_presentation(kit.presentation_of(draft), change))
    return link(company, location_id, agent)


def describe(state, owner, collection, tracker, *, covered=lambda cameras: True, revision=None):
    """What `get_tracker_presentation` answers for one tracker record."""
    from core_trackers import presentation as kit
    if tracker.get('core'):
        # Home draws a Core Tracker from its recipe: its mark, its number format and its full view.
        return {'tracker_id': tracker['id'], 'key': tracker['key'], 'label': tracker['label'], 'core': True, 'locked': tracker['locked'],
                'location_id': tracker['location_id'], 'presentation': None, 'maintained': MAINTAINED,
                'recipe': {'revision': tracker.get('recipe_revision'), 'tile': tracker['tile'], 'canvas': tracker['canvas']},
                'kit': kit.KIT, 'output': None, 'families': [], 'problems': [], 'revisions': []}
    saved = kit.saved_presentations(state, owner, collection).get(tracker['id'])
    output = kit.current_output(state, owner, tracker)
    readable = output is not None and covered(tuple(sorted(output.get('camera_ids') or ())))
    shown = output if readable else None
    current = kit.presentation_of(tracker, saved, shown)
    revisions = kit.saved_revisions(state, owner, tracker)
    if revision is not None:
        current = next((row for row in revisions if row['revision'] == revision), None)
        if current is None:
            raise ValueError(REVISION)
    answer = {'tracker_id': tracker['id'], 'key': tracker['key'], 'label': tracker['label'], 'core': tracker['core'],
        'locked': tracker['locked'], 'location_id': tracker['location_id'],
        **({'agent_id': tracker['agent_id']} if tracker.get('agent_id') else {}),
        'presentation': current, 'kit': kit.KIT,
        'output': {'revision': shown['revision'], 'state': shown['tile'].get('state'), 'value': shown['tile'].get('value'),
                   'unit': shown['tile'].get('unit'), 'fields': kit.output_fields(shown['tile'], shown['canvas'])} if shown else None,
        'families': [{k: v for k, v in family.items() if k != 'blocks'} for family in kit.families_for(shown)],
        'problems': kit.check_output(current, shown['tile'], shown['canvas']) if shown else [],
        'revisions': [{k: row[k] for k in ('revision', 'saved_at', 'saved_by')} for row in revisions]}
    if output is not None and not readable:
        from monitoring_tools import CAMERA_SCOPE
        answer['output_access'] = CAMERA_SCOPE
    return answer


def save(company, tracker, change, *, expected_revision=None, saved_by='harness'):
    """Save a tracker's presentation and carry it into the published page. One function for the
    harness tool and for a person's route."""
    from core_trackers import presentation as kit
    saved = kit.save_presentation(company, tracker, change, expected_revision=expected_revision, saved_by=saved_by)
    shown = False
    try:
        from import_control_room_bundle import show_presentation
        shown = show_presentation(company, tracker['location_id'], tracker['id'])
    except (OSError, ValueError):
        pass  # The presentation is saved; the page takes it with the location's next pass.
    return {'tracker_id': tracker['id'], 'key': tracker['key'], 'presentation': saved, 'page_updated': shown}


def read(context, name, args):
    state, owner, dataset = context.state, context.owner, context.dataset
    tracker = tracker_of(state, owner, dataset, args['tracker_id'])
    if tracker is None or not context.allowed(tracker['location_id']):
        agents = context.agents
        if tracker is None and agents is not None and any(agent['id'] == args['tracker_id'] and agent.get('agent_kind') == 'tracker'
                                                          for agent in agents.list(owner_principal_id=owner)):
            raise ValueError(AGENT_LOCATION)
        raise ValueError(UNKNOWN)
    return describe(state, owner, dataset, tracker, covered=lambda cameras: context.allowed(tracker['location_id'], cameras),
                    revision=args.get('revision'))


def call(context, name, args):
    state, owner, dataset = context.state, context.owner, context.dataset
    company = context.company()
    change = {part: args[part] for part in ('tile', 'canvas') if part in args}
    tracker = tracker_of(state, owner, dataset, args['tracker_id'])
    if tracker is None and context.agents is not None:
        agent = next((agent for agent in context.agents.list(owner_principal_id=owner)
                      if agent['id'] == args['tracker_id'] and agent.get('agent_kind') == 'tracker' and agent.get('dataset') == dataset), None)
        if agent is not None:
            tracker = agent_tracker_record(company, agent, change, profiles=_profiles(context.store, owner, agent),
                                           expected_revision=args.get('expected_revision'), allowed=context.allowed)
    if tracker is None or not context.allowed(tracker['location_id']):
        raise ValueError(UNKNOWN)
    return save(company, tracker, change, expected_revision=args.get('expected_revision'), saved_by='harness')


def _profiles(monitoring, owner, agent):
    found = []
    for key in agent.get('profile_ids') or []:
        try:
            found.append(monitoring.get_profile(owner, key))
        except ValueError:
            continue
    return found


def save_for_agent(selected, agent, change, *, expected_revision=None):
    """A person's save for a Tracking Agent's tracker, from the host's own stores."""
    import retail_records as records
    owner = agent['owner_principal_id']
    company = records.Company(Path(selected['state']), owner, agent['dataset'], selected['business_scope'], selected['monitoring'],
                              selected['location_intelligence'])
    tracker = agent_tracker_record(company, agent, change, profiles=_profiles(selected['monitoring'], owner, agent), expected_revision=expected_revision)
    return save(company, tracker, change, expected_revision=expected_revision, saved_by='person')
