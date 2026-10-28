"""Ready-made agents of the vertical this process serves (pack.py).

A vertical lists the agents a company can add in `ready_agents` and keeps the ones whose
work has been retired in `ready_agents_retired`. Saved definitions and findings have their
own records.

    available()   the ready-made agents the template offers
    retired()     templates kept readable for the conversations that were started from them
    template(id)  one template by id, from either list
    catalogue()   both lists as one answer, available first, each retired row marked `retired: true`
    draft(id, collection, profiles)
                  what a draft agent takes from one available template, in the agent store's own keys

The Customize listing and new-chat selection serve `available()`. Selecting one creates an
inactive saved draft in its own conversation, using `draft()` for the vertical's class, input,
interval, run allowance and scoped profiles. Historical chats still resolve retired definitions
through `template()` or `catalogue()`.
"""
import pack

PUBLISHER = 'Rocoto'


def available():
    return {'publisher': PUBLISHER, 'templates': pack.active().ready_agents()}


def retired():
    return {'publisher': PUBLISHER, 'templates': pack.active().retired_ready_agents()}


def template(template_id):
    """The template with this id, or None."""
    return next((item for item in catalogue()['templates'] if item['id'] == template_id), None)


def draft(template_id, dataset, profiles=()):
    """The definition a draft agent takes from one available template, for one business collection.

    The keys are the agent store's own (`AgentDefinition` in background_agents.py): the template's
    name is the title and its instructions are the instruction. `profiles` are the company's saved
    observation profiles. The draft follows the profiles of the collection whose vocabulary holds
    an event type the template reads, and every profile of the collection while that list is empty.
    """
    current = pack.active()
    row = next((item for item in current.ready_agents() if item['id'] == template_id), None)
    if row is None:
        raise ValueError('Choose an available ready-made agent.')
    kind = row.get('agent_kind', current.default_kind())
    reads = set(row.get('event_types', ()))
    follows = [profile['id'] for profile in profiles
               if profile.get('dataset') == dataset and (not reads or reads & {entry.get('type') for entry in profile.get('vocabulary', [])})]
    return {
        'title': row['name'], 'instruction': row['instructions'],
        'description': row['description'] if len(row['description']) <= 220 else '',
        'watch_for': list(row.get('watch_for', [])), 'dataset': dataset, 'agent_kind': kind,
        'input_kind': row.get('input_kind', current.agent_class(kind)['input_kinds'][0]),
        'profile_ids': follows, 'event_types': list(row.get('event_types', [])), 'mode': row['mode'], 'interval_seconds': row.get('interval_seconds', 3600),
        'max_runs': row.get('max_runs', 1), 'skill_ids': list(row.get('skill_ids', [])),
    }


def catalogue():
    current = pack.active()
    return {'publisher': PUBLISHER, 'templates': current.ready_agents()
            + [{**row, 'retired': True} for row in current.retired_ready_agents()]}
