"""The harness describes itself: one read tool, `get_harness`, on every tool surface.

Rocoto is one agent harness that ships to a kind of business with one vertical
(docs/decisions/0012-vertical-agent-harness.md). An agent of the harness asks this tool what the
product is and how it was built, and gets the answer from the code that is running:

    product              the names of the active vertical (pack.py)
    layers               the three levels; the module counts of layers.py for the harness layers, and
                         for the vertical whose code this directory holds
    fixed_conversations  the company chats of the vertical
    agent_classes        the dividers, each with the reader's own agents where the surface holds them
    ready_agents      the ready-made agents the vertical offers
    tools                the live registry: every tool of a conversation with its family, and the
                         names a sub-agent, a native sub-agent and a customer's agent over MCP get
    wakes, channels      what wakes an agent and how it reaches people, each with its state
    recipes, event_vocabulary, flow
    skills               the vertical's seeds; installed states where the surface holds the store
    surfaces             the pages and what fills each tab
    data_sources         the registry of sources: title, read tools, skill, and whether the company
                         has rows in it today
    capabilities         what is real, partial and planned: the harness rows of CAPABILITIES below
                         joined with the rows of the active vertical
    problems             what the manifest names and the running service lacks, as sentences

The answer holds names, titles, counts and states. People's ids, file locations, prompt text,
credentials, host names and host settings stay out of it, so the same answer is safe on a
customer's own agent.

This module is a tool family of `monitoring_tools.FAMILIES` (READ_NAMES, WRITE_NAMES, specs, read,
call). It reads the registry at the moment of a call: `agent_bridge`, `monitoring_tools`,
`native_mcp` and `public_mcp` are imported inside functions, because each of them reads the family
list while it loads.

The table of what works has two homes. `CAPABILITIES` holds the harness rows, true for every
segment. A vertical keeps its own rows and the sentences it adds to a harness row in its folder
(`capabilities` in its manifest; verticals/retail/capabilities.json). `capability_rows(template)` joins
them: get_harness answers with the rows of the active vertical, and README.md prints the rows of
the default one. tests/test_harness_tools.py keeps README.md equal to that table. Change a row in
the same change that makes it true, then run `python harness_tools.py --readme`, which writes the
table into README.md; with no argument the command prints it.

`developer_prompt(template)` introduces this tool on every native turn. The bridge composes it
with the active vertical and shared workflow instructions (agent_bridge.py).
"""
from __future__ import annotations

import layout

from collections import Counter
from pathlib import Path
import sqlite3
import sys
import types

import layers
import pack

SCHEMA = 'rocoto.harness.v1'
READ_NAMES = frozenset({'get_harness'})
WRITE_NAMES = frozenset()
SECTIONS = ('all', 'layers', 'conversations', 'agents', 'tools', 'wakes', 'channels', 'recipes', 'skills',
            'surfaces', 'data', 'capabilities')
STATES = ('real', 'partial', 'planned')
UNKNOWN = 'unknown on this surface'
RULE = 'Every capability is a tool first; a screen shows what the tools made.'
DESCRIPTION = (
    'Read what this product is and how it is built: its three levels, its fixed conversations, its agent '
    'classes with your agents, the tools on each surface, what wakes an agent, the channels to people, the '
    'recipes, the skills, the surfaces, the registry of data sources and the present state of every '
    'capability (real, partial or planned). Pass section to read one part. Describe each capability to a '
    'person by the state this tool reports.')

# What each section returns. `schema`, `section`, `product` and `problems` ride with every answer.
PARTS = {
    'layers': ('layers', 'built_with'),
    'conversations': ('fixed_conversations',),
    'agents': ('agent_classes', 'ready_agents'),
    'tools': ('tools',),
    'wakes': ('wakes',),
    'channels': ('channels',),
    'recipes': ('recipes', 'sub_agents', 'tile_sets', 'event_vocabulary', 'flow'),
    'skills': ('skills',),
    'surfaces': ('surfaces',),
    'data': ('data_sources', 'connections_expected'),
    'capabilities': ('capabilities',),
}

# What each layer gives the ones above it. The names and the module counts come from layers.py.
PROVIDES = {
    layers.RUNTIME: ('model loop', 'threads', 'tool calls', 'sub-agents', 'plugins', 'web search'),
    layers.HARNESS: ('conversations', 'long-running agents', 'wakes', 'channels to people', 'connections',
                     'sandbox', 'usage bounds', 'identity and grants', 'skills', 'this description'),
    layers.TEMPLATE: ('fixed conversations', 'agent classes', 'recipes', 'prompts', 'event vocabulary',
                      'skills', 'ready-made agents', 'data sources', 'flow of work'),
}
# The vertical whose code the modules of layers 3 and 4 in this directory are (layers.py). Another
# template runs on the harness with those modules still in the service, so its answer leaves their
# counts out.
CODE_OF = pack.DEFAULT
DOCUMENTS = (
    (0, 'README.md', 'The three levels and the source entry point'),
    (layers.RUNTIME, 'runtime/README.md', 'OpenAI-managed core and Rocoto adapter'),
    (layers.HARNESS, 'mechanics/README.md', 'Workspace mechanics and builder needs'),
    (layers.HARNESS, 'mechanics/CHANNELS.md', 'Messages and ordinary replies'),
    (layers.HARNESS, 'mechanics/CONNECTIONS.md', 'Plugins, remote MCP and custom connectors'),
    (layers.HARNESS, 'mechanics/MCP.md', 'Customer and hosted tool access'),
    (layers.HARNESS, 'sdk/README.md', 'Add a vertical folder'),
    (layers.HARNESS, 'sdk/TILES-AND-TABS.md', 'Shared displays and navigation'),
    (layers.HARNESS, 'deploy/README.md', 'Targets, publication and rollback'),
    (layers.TEMPLATE, 'verticals/retail/README.md', 'Retail role and workflows'),
    (layers.TEMPLATE, 'verticals/retail/PAGES.md', 'Retail pages'),
    (layers.TEMPLATE, 'verticals/retail/DATA.md', 'Retail data and preserved history'),
)

# What wakes an agent. Each row agrees with a capability row below.
WAKES = (
    ('chat_message', 'real', 'A message starts a turn; follow-ups steer the active turn.'),
    ('phone_reply', 'partial', 'A verified provider reply returns to its exact conversation as an ordinary message; live delivery requires workspace commissioning.'),
    ('event', 'real', 'A declared event producer submits one durable signal through the shared chain and current authority checks.'),
    ('schedule', 'real', 'Standing agents, one-time timers and bounded repeating timers retain their schedules across restart.'),
    ('inbound_webhook', 'real', 'A signed bounded webhook submits its original event once, with revocable authority and a declared signal chain.'),
    ('agent_set', 'real', 'An agent sets, lists and cancels bounded wake sources or invokes an authorized agent.'),
)

# The harness's rows of the one table of what works: id, title, state, what is real today, what is
# partly real and what it still needs, what a visitor sees ahead of the code behind it, and the files
# behind the row. Every sentence here holds for every business. A vertical keeps its own rows, and the
# sentences it adds to a harness row, in its folder (the manifest's `capabilities`, for retail
# verticals/retail/capabilities.json); `capability_rows(template)` joins the two. README.md prints the
# joined rows of the default vertical; get_harness returns the joined rows of the active one as
# `capabilities`, the files left out.
# States: real works today, partial works for the part named, planned has zero code.
CAPABILITIES = (('ready_made_agents',
  'Ready-made agents',
  'real',
  'A vertical lists its ready-made agents and the page offers the active ones. Starting a conversation '
  'saves an inactive draft with the declared class, input, mode, interval, run allowance, event types '
  "and the acting member's matching profiles. The conversation keeps the draft and its instructions "
  'together.',
  '',
  '',
  'agent_catalogue.py pack.py server.py'),
 ('agents_running_unattended',
  'Agents that run by themselves',
  'real',
  'Standing grants keep an owner’s recurring agent active across service restarts, rechecking '
  'membership, class authority, revision and funded limits before each run. Expiring grants remain '
  'available. The definition bounds daily runs.',
  '',
  '',
  'background_agents.py agent_wakes.py'),
 ('action_agents',
  'Agents with granted effects',
  'real',
  'A vertical declares classes with participant reach or granted effects. Managed provider writes '
  'require a commissioned connection and an approval by the current acting member for the exact call.',
  '',
  '',
  'background_agents.py agent_kind_policy.py managed_connections.py'),
 ('scheduled_work',
  'Scheduled work',
  'partial',
  'Intervals, one-time timers and bounded repeating timers produce durable signals. Standing agent '
  'grants survive restart.',
  'Calendar recurrence by a named local clock time is planned.',
  '',
  'background_agents.py signal_sources.py'),
 ('agent_set_wakes',
  'A wake an agent sets for itself',
  'real',
  'An agent sets a bounded timer or repeating wake, lists or cancels its wake sources, and invokes an '
  'authorized agent. Timer notes return as ordinary conversation messages.',
  '',
  '',
  'wake_tools.py signal_sources.py agent_invocation.py'),
 ('webhooks_in',
  'Incoming webhooks',
  'real',
  'Signed webhook events become ordinary messages in their owning conversation and pass through the '
  'shared signal chain with current membership, expiry and daily limits.',
  '',
  '',
  'signal_sources.py signal_dispatcher.py signal_chain.py'),
 ('webhooks_out',
  'Outgoing webhooks',
  'real',
  'An owner registers a fixed HTTPS endpoint and copies its private signing secret once. Each send uses '
  'a durable effect identity, current authority and the shared daily send allowance.',
  '',
  '',
  'signal_endpoints.py wake_tools.py web/wake-controls.js'),
 ('cheap_screening',
  'Screening before a heavy run',
  'real',
  'A signal’s declared chain runs scope filters, deterministic conditions, optional funded small-model '
  'screening and scripts before its destination. Each step keeps its original input, result and effect '
  'identity across restart.',
  '',
  '',
  'signal_chain.py signal_screening.py signal_dispatcher.py'),
 ('phone_alerts',
  'Phone alerts',
  'partial',
  'Participant messaging uses verified contacts, signed replies, durable delivery receipts and shared '
  'daily send limits. A vertical can fan out a committed alert through this shared service.',
  'Live delivery requires that workspace’s commissioned provider account, templates where required, and '
  'spending grant.',
  '',
  'phone_delivery.py phone_chat.py phone_spend.py messaging.py'),
 ('viber_and_sms',
  'Viber and SMS',
  'partial',
  'The Viber adapter supports authenticated delivery and replies through the shared messaging ledger.',
  'SMS awaits an adapter and provider commissioning.',
  '',
  'messaging_provider.py messaging.py'),
 ('customer_mcp_access',
  "A customer's own agent over MCP",
  'partial',
  "The /mcp endpoint serves the registry's read tools over HTTPS under hashed, revocable read grants, "
  'once the public origin is set.',
  'Conversation commands, questions, writes and subscriptions over MCP are a written contract with zero '
  'code.',
  '',
  'public_mcp.py mcp_access.py mcp_host.py mechanics/MCP.md'),
 ('conversations',
  'Conversations several people write in',
  'real',
  'Every conversation keeps messages and authors durably and continues through page reload. Authorized '
  'members share the vertical’s fixed company conversations; remote replies retain their originating '
  'channel.',
  '',
  '',
  'agent_bridge.py chat_permissions.py organization_chats.py'),
 ('agent_runtime',
  'The agent runtime',
  'real',
  'OpenAI-managed Agents API sessions run the core and its hosted environment. Rocoto adds workspace '
  'authority, durable tool receipts, provider approvals, retained skills and funded spending limits. '
  'Live proofs cover hosted execution, deployed Organization/files, a stock-child MCP read and '
  'authority withdrawal with root/child quiescence after Stop.',
  '',
  '',
  'managed_runtime.py managed_codex.py managed_policy.py managed_sessions.py'),
 ('vertical',
  'One vertical per business role',
  'real',
  'One vertical folder declares its classes, fixed conversations, prompts, complete-job recipes, named '
  'sub-agent roles, tool families, optional communication capabilities, tabs and tile sets. Every '
  'business role uses the same host.',
  '',
  '',
  'pack.py job_recipes.py host.py'),
 ('self_description',
  "The harness's description of itself",
  'real',
  'get_harness describes the active vertical and the live registry: classes, tools, wakes, channels, '
  'recipes, sub-agents, displays and data sources. Hosted reads include selected communication '
  'capabilities and current workspace readiness.',
  '',
  '',
  'harness_tools.py harness_routes.py workspace_capabilities.py'),
 ('data_sources',
  'The registry of data sources',
  'partial',
  'The vertical lists its sources, each with its read tools and its skill. get_harness adds whether the '
  'company has rows in a source today, for the sources the tool registry reads.',
  "A source read by the conversation's own tools reports unknown and names the tool to call.",
  '',
  'pack.py harness_tools.py'),
 ('skills',
  'Skills',
  'real',
  'Instruction skills with numbered revisions: seeded from the vertical, installed by a person, '
  'admitted on a turn once every tool they need is registered.',
  '',
  '',
  'skills.py pack.py'),
 ('connections',
  'Connections and plugins',
  'real',
  'The harness combines reviewed remote MCP connections, exact installed OpenAI plugin packages and '
  'company-owned custom connectors. Hosted capability snapshots retain their original installed bytes '
  'and revocation state.',
  '',
  '',
  'mcp_connections.py provider_policy.py plugin_catalogue.py hosted_capabilities.py'),
 ('sandbox_and_custom_connectors',
  'Sandbox and custom connectors',
  'real',
  'OpenAI hosts the core agent environment. Rocoto’s separate gVisor service executes bounded custom '
  'connector reads and vertical scripts through a commissioned broker. Each run preserves its cleanup '
  'receipt.',
  '',
  '',
  'native_custom_broker.py custom_connectors.py custom_runtime.py'),
 ('usage_bounds',
  'Usage and spend bounds',
  'real',
  'The harness reserves funded company money before managed execution, records durable usage, and '
  'retains uncertain reservations until reconciled. Stop cancels the owned session and its descendants; '
  'uncertain cleanup keeps the claim held.',
  '',
  '',
  'managed_usage.py managed_policy.py company_usage.py'),
 ('postgresql',
  'PostgreSQL',
  'partial',
  'The maintained package holds 32 tables under forced row security: admission, an outbox with leases, '
  'subjects, replay, module projections, configuration and location trackers, with database tests.',
  'The review deployment reads SQLite and JSON files. In maintained mode agents, chats, wakes and '
  'messaging stay in the file stores.',
  '',
  'backend/realtime/ realtime_read_routes.py'))
ROW_TEXTS = ('works', 'missing', 'for_show')


def _listed(names):
    names = [str(name) for name in names]
    return ', '.join(names[:-1]) + ' and ' + names[-1] if len(names) > 1 else ''.join(names)


def source_reference(name):
    """Repository documentation references follow the selected source layout."""
    if name.startswith('packs/'):
        return 'verticals/' + name[len('packs/'):]
    if name == 'layers.py':
        return 'sdk/layers.py'
    if name.endswith('.py') and '/' not in name:
        try:
            return layout.source(name[:-3]).relative_to(layout.ROOT).as_posix()
        except FileNotFoundError:
            return name
    if name.startswith('core_trackers/'):
        return 'verticals/' + layout.selected_vertical() + '/' + name
    if name.startswith('web/') and not (layout.ROOT / name).exists():
        candidate = layout.vertical_root() / name
        if candidate.exists():
            return candidate.relative_to(layout.ROOT).as_posix()
    if name == 'packs/':
        return 'verticals/'
    return name


def capability_rows(template=None):
    """The table for one template: its own rows first, then the harness rows with the sentences it adds.

    Each row is {id, title, state, works, missing, for_show, where}. A vertical row with `adds_to`
    joins its sentences and its files to the harness row of that id and may set the row's state
    and title for its segment.
    """
    template = pack.active() if template is None else template
    declared = template.capabilities()
    adds = {row['adds_to']: row for row in declared if 'adds_to' in row}
    rows = [{'id': row['id'], 'title': row['title'], 'state': row['state'],
             **{name: row.get(name, '') for name in ROW_TEXTS}, 'where': list(row['where'])}
            for row in declared if 'id' in row]
    join = lambda *parts: ' '.join(part for part in parts if part)
    for key, title, state, works, missing, show, where in CAPABILITIES:
        more = adds.get(key, {})
        rows.append({'id': key, 'title': more.get('title', title), 'state': more.get('state', state),
                     'works': join(works, more.get('works')), 'missing': join(missing, more.get('missing')),
                     'for_show': join(show, more.get('for_show')), 'where': where.split() + list(more.get('where', []))})
    for row in rows:
        row['where'] = [source_reference(name) for name in row['where']]
    return rows


def capability_problems(template=None):
    """What keeps a vertical's rows from joining the table, as sentences. Empty means the table is whole."""
    template = pack.active() if template is None else template
    harness = {row[0] for row in CAPABILITIES}
    problems = []
    for row in template.capabilities():
        if 'adds_to' in row and row['adds_to'] not in harness:
            problems.append(f"capabilities adds sentences to {row['adds_to']}; name a row of the harness's table.")
        if row.get('id') in harness:
            problems.append(f"capabilities gives a row the id {row['id']}, a row of the harness's table; write adds_to for it.")
    for row in capability_rows(template):
        states = {'real': bool(row['works']), 'partial': bool(row['missing']),
                  'planned': bool(row['for_show'] or row['missing']) and not row['works']}
        if not states[row['state']]:
            problems.append(f"the capability row {row['id']} reads {row['state']}; a real row says what works, a partial "
                            'row says what is missing, and a planned row leaves works empty.')
    return problems


def capabilities(template=None):
    """The table as rows an agent reads: id, title, state and the three statements that hold words."""
    return [{'id': row['id'], 'title': row['title'], 'state': row['state'],
             **{name: row[name] for name in ROW_TEXTS if row[name]}} for row in capability_rows(template)]


def capability_table(template=None):
    """The table as README.md prints it: the rows of the default vertical, one markdown row each with its files."""
    lines = ['| Capability | Real today | Partly real, and what is missing | For show | Where |',
             '| --- | --- | --- | --- | --- |']
    for row in capability_rows(pack.load(pack.DEFAULT) if template is None else template):
        files = ', '.join(f'`{name}`' for name in row['where'])
        lines.append(f"| {row['title']} | {row['works'] or '—'} | {row['missing'] or '—'} | {row['for_show'] or '—'} | {files} |")
    return '\n'.join(lines)


def write_readme(path=None):
    """Put the table into the front door, in place of the one it holds."""
    path = (layout.ROOT / 'README.md') if path is None else Path(path)
    lines = path.read_text(encoding='utf-8').split('\n')
    first = next((index for index, line in enumerate(lines) if line.startswith('| Capability | Real today |')), None)
    if first is None:
        raise ValueError('Give README.md its capability table: one line that starts with | Capability | Real today |.')
    # The front door prints the table of the default vertical, whatever template this process serves.
    last = first
    while last < len(lines) and lines[last].startswith('|'):
        last += 1
    path.write_text('\n'.join(lines[:first] + capability_table().split('\n') + lines[last:]), encoding='utf-8')


def developer_prompt(template=None):
    """The paragraph that tells an agent what it is part of and that get_harness answers the rest.

    A pure function of the vertical: the same revision gives the same text, so the thread digest of
    a conversation that carries it stays stable.
    """
    template = pack.active() if template is None else template
    product = template.product()
    return (
        f"You are an agent of {product['name']}, built as three levels. OpenAI's managed Agents API core "
        'runs your model loop, your thread, your tool calls and your sub-agents. The Rocoto harness gives you '
        'persistent conversations, long-running agents, wakes, phone channels to people, connections, a '
        f"sandbox for scripts, skills and usage bounds. The {product['business']} vertical gives this product its "
        f"fixed conversations ({_listed(chat['title'] for chat in template.fixed_chats())}), its agent classes "
        f"({_listed(item['label'] for item in template.agent_classes())}), its recipes, its vocabulary and these "
        f"instructions. The product surfaces ({_listed(surface['title'] for surface in template.surfaces())}) "
        'show what your tools and scripts publish. Call get_harness to read the current conversations, agents, '
        'tools, wakes, channels, recipes, skills, surfaces and data sources with their present state, and '
        'describe each capability by the state it reports. Save hosted file outputs under /workspace/outputs; '
        'the harness retains files up to 16 MiB each in this conversation’s Files.')


# ── The tool family ───────────────────────────────────────────────────────────────────────────

def specs(write=True):
    return [{'type': 'function', 'name': 'get_harness', 'description': DESCRIPTION, 'inputSchema': {
        'type': 'object', 'properties': {'section': {'type': 'string', 'enum': list(SECTIONS)}},
        'required': [], 'additionalProperties': False}}]


def read(context, name, args):
    if name not in READ_NAMES:
        raise ValueError('Read the harness with get_harness.')
    section = (args or {}).get('section', 'all')
    if section not in SECTIONS:
        raise ValueError('Choose a section of the harness: ' + ', '.join(SECTIONS) + '.')
    return describe(context, section)


def call(context, name, args):
    raise ValueError('Read the harness with get_harness; this family holds reads.')


# ── The live registry ─────────────────────────────────────────────────────────────────────────

def registry(agent=None):
    """Report the selected workspace registry used by its live tool transports."""
    from agent_bridge import workspace_registry
    live = workspace_registry(agent)
    families = []
    registered = []
    for wrapped in live.families:
        family = getattr(wrapped, 'family', wrapped)
        module = getattr(family, 'module', getattr(family, '__name__', type(family).__module__))
        name = getattr(family, 'name', module.removesuffix('_tools'))
        families.append({'name': name, 'module': module,
                         'reads': set(family.READ_NAMES), 'writes': set(family.WRITE_NAMES)})
        registered.append(family)
    def serve(context, name, arguments):
        family = live.family_of(name)
        if name not in live.reads or not callable(getattr(family, 'read', None)):
            raise ValueError('Use the declared read tool for this source.')
        live.declared(name, arguments)
        guard = getattr(context, 'require_current', None)
        if guard is not None:
            guard()
            selected = family.context(context) if hasattr(family, 'context') else context
        else:
            selected = context
        result = family.read(selected, name, arguments)
        if guard is not None:
            guard()
        return result
    return types.SimpleNamespace(parent=[spec['name'] for spec in live.specs()],
        child=[spec['name'] for spec in live.specs(False)], specs=live.specs(), families=families,
        registered=registered, reads=set(live.reads), writes=set(live.writes),
        native=set(live.read_names('native')), public=set(live.read_names('public')),
        serve=serve)


def _tools(live):
    owner = {name: family for family in live.families for name in family['reads'] | family['writes']}
    reviewed = live.reads | live.native | live.public
    conversation = []
    for name in live.parent:
        family = owner.get(name)
        row = {'name': name, 'family': family['name'] if family else 'conversation',
               'effect': 'read' if name in reviewed else 'write' if name in live.writes else 'conversation'}
        layer = layers.LAYER.get(family['module']) if family else None
        if layer is not None:
            row['layer'] = layer
        conversation.append(row)
    surfaces = {
        'sub_agent': list(live.child),
        'native_sub_agent': [name for name in live.child if name in live.native],
        'customer_mcp': [name for name in live.child if name in live.public],
    }
    return {
        'conversation': conversation, **surfaces,
        'families': [{'name': family['name'], 'reads': len(family['reads']), 'writes': len(family['writes']),
                      **({'layer': layers.LAYER[family['module']]} if family['module'] in layers.LAYER else {})}
                     for family in live.families],
        'counts': {'conversation': len(conversation), **{key: len(names) for key, names in surfaces.items()}},
        'effects': {'read': 'a reviewed read, offered beyond the parent conversation',
                    'write': 'a write of a tool family, a tool of the parent conversation',
                    'conversation': 'a tool of the conversation itself; its description states its effect'},
    }


def _problems(template, live):
    """What the manifest names and the running service lacks, as sentences. Empty means they agree.

    The tools a tab, a source or a recipe names; a recipe its install tool takes and whose
    processor scripts import; the vertical's rows of the table of what works; the tool families.
    """
    problems = list(template.check_tools(live.parent)) + template.check_tile_sets(live.specs) + capability_problems(template)
    try:
        declared = template.import_tool_families()
    except pack.PackError as error:
        return problems + [str(error)]
    held = {id(family) for family in live.registered}
    for entry, family in zip(template.tool_families(), declared):
        if id(family) not in held:
            problems.append(f'tool_families names {entry}; add that family to the registry, or take the name out of {pack.MANIFEST}.')
    return problems


# ── The parts of the answer ───────────────────────────────────────────────────────────────────

def _layers(template):
    counted = Counter(layers.LAYER.values())
    surfaces = template.surfaces()
    rows = [
        {'layer': 1, 'name': 'Core',
         'is': "OpenAI's managed Agents API, including OpenAI-hosted execution",
         'provides': list(PROVIDES[layers.RUNTIME]), 'modules': 0},
        {'layer': 2, 'name': 'Harness',
         'is': 'Rocoto mechanics and SDK, including the adapter to the managed core',
         'provides': list(PROVIDES[layers.HARNESS]),
         'modules': counted[layers.RUNTIME] + counted[layers.HARNESS],
         'runtime_adapter_modules': counted[layers.RUNTIME]},
        {'layer': 3, 'name': 'Vertical',
         'is': f"{template.product()['business']}, the vertical named {template.id}",
         'provides': list(PROVIDES[layers.TEMPLATE]) + sorted({
             tab['display'] for surface in surfaces for tab in surface['tabs']})},
    ]
    if template.id == CODE_OF:
        rows[2]['modules'] = counted[layers.TEMPLATE]
    try:
        import managed_policy
        rows[0]['model'] = managed_policy.MODEL
    except (ImportError, AttributeError):
        pass
    return rows


def _built_with(template):
    return {'rule': RULE,
            'documents': [{'layer': layer, 'name': name, 'holds': holds} if layer else {'name': name, 'holds': holds}
                          for layer, name, holds in DOCUMENTS if layer != layers.TEMPLATE or template.id == 'retail'],
            'layer_rule': 'Shared runtime adapters, mechanics and SDK reach a vertical through its declared interface.',
            'imports_that_cross_it_today': sum(len(targets) for targets in layers.CROSSINGS.values()),
            'modules_that_hold_two_layers': len(layers.FUSED)}


def _fixed_conversations(template):
    return [{key: chat[key] for key in ('purpose', 'title', 'job', 'opens', 'skill') if key in chat}
            for chat in template.fixed_chats()]


def _agents(context, kind, default):
    """The reader's own agents of one class in this collection, where the surface holds the agent store."""
    store = getattr(context, 'agents', None)
    if store is None:
        return UNKNOWN
    try:
        saved = store.list(owner_principal_id=context.owner)
    except (ValueError, OSError, sqlite3.Error):
        return UNKNOWN
    rows = []
    for agent in saved:
        if agent.get('dataset') != context.dataset or agent.get('agent_kind', default) != kind:
            continue
        last = agent.get('last_run')
        rows.append({'id': agent['id'], 'title': agent['title'], 'status': agent['status'], 'mode': agent['mode'],
                     'input_kind': agent.get('input_kind'), 'chat_id': agent.get('chat_id'),
                     'runs_started': agent.get('runs_started', 0),
                     'last_run': {'status': last.get('status'), 'completed_at': last.get('completed_at')} if last else None})
    return rows


def _agent_classes(context, template):
    default = template.default_kind()
    return [{**{key: item[key] for key in ('kind', 'label', 'plural', 'authority', 'default', 'input_kinds')},
             'agents': _agents(context, item['kind'], default)} for item in template.agent_classes()]


def _ready_agents(template):
    keys = ('id', 'name', 'description', 'agent_kind', 'input_kind', 'mode', 'interval_seconds', 'event_types', 'skill_ids')
    return [{key: row[key] for key in keys if key in row} for row in template.ready_agents()]


def _channels(context, template):
    from capability_contract import NAMES, LABELS
    builtins = getattr(getattr(context, 'agent', None), 'builtin_capabilities', None)
    status = builtins.status() if builtins is not None else {}
    return [{'channel': name, 'label': LABELS[name], 'adapter': 'built', 'state': 'partial',
             'selected': template.builtin_capabilities()[name],
             'ready': status[name]['ready'] if name in status else UNKNOWN,
             'note': 'Workspace provider, sender, callback and spending commissioning determine live readiness.'}
            for name in NAMES]


def _tile_sets(template):
    return [{'id': recipe['id'], 'kind': recipe['kind'], 'revision': recipe['revision'], 'install_tool': recipe['install_tool'],
             'items': [{'key': tracker['key'], 'label': tracker.get('label', tracker['key'])}
                       for tracker in template.tile_set(recipe['id'])['trackers']]}
            for recipe in template.tile_sets()]


def _vocabulary(template):
    declared = template.vocabulary()
    groups = {group: sorted(name for name, entry in declared['types'].items() if entry.get('group') == group)
              for group in declared['groups']}
    return {'types': len(declared['types']), 'groups': groups}


def _skills(context, template, live):
    """The skills of the company where the surface holds its stores; the vertical's seeds everywhere."""
    admitted = set(live.parent) | {'web_search'}  # a turn adds web_search by itself (agent_bridge.resolve_skills)
    waits = lambda tools: [name for name in tools if name not in admitted]
    row = lambda item, origin, installed: {
        'id': item['id'], 'name': item['name'], 'origin': origin, 'installed': installed,
        'tools_ready': not waits(item['required_tools']), 'waits_for': waits(item['required_tools']),
        'connections': [connection['provider'] for connection in item.get('required_connections', [])]}
    seeds = [row(seed, 'vertical', UNKNOWN) for seed in template.skills()]
    # The agent store marks the hosted conversation and its sub-agents: the surfaces that hold the
    # company's stores. A customer's own agent over MCP reads the vertical's seeds.
    if getattr(context, 'agents', None) is None or not (context.state / pack.SEEDED).is_file():
        return seeds
    try:
        from skills import SkillStore
        catalogue = SkillStore(context.state).catalogue()['skills']
    except (ValueError, OSError, sqlite3.Error):
        return seeds
    return [row(item, 'vertical' if item.get('origin') == 'builtin' else 'company', bool(item.get('installed')))
            for item in catalogue]


def _count(result):
    """How many rows a listing read returned, by the name of each list it holds."""
    if isinstance(result, list):
        return {'rows': len(result)}
    if isinstance(result, dict):
        return {key: len(value) for key, value in result.items() if isinstance(value, list)}
    return {}


def _connected(context, source, live):
    """Whether the company has rows in one data source today, from the reads the registry serves.

    A read of a tool family answers from the context this call received, on every surface. Each
    read is called once with its own defaults, so `first_page` holds how many rows that first
    page returned: a number that says rows exist, and the read itself gives the total. A source
    whose read tools belong to the conversation itself reports UNKNOWN and names the tool to call.
    """
    probes = [name for name in source['read_with'] if name in live.reads and name not in READ_NAMES]
    rows = {}
    for name in probes:
        try:
            counted = _count(live.serve(context, name, {}))
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            continue  # a read that needs an argument, or a store this reader has yet to get
        if counted:
            rows[name] = counted
    if not rows:
        return {'connected': UNKNOWN, 'read_first': source['read_with'][0]}
    return {'connected': any(count for counted in rows.values() for count in counted.values()), 'first_page': rows}


def _data_sources(context, template, live):
    registered = set(live.parent)
    sources = []
    for source in template.data_sources():
        sources.append({'id': source['id'], 'title': source['title'], 'read_with': list(source['read_with']),
                        'skill': source.get('skill'),
                        'tools_registered': all(name in registered for name in source['read_with']),
                        **_connected(context, source, live)})
    return sources


def describe(context, section='all', template=None):
    """The answer of get_harness for one reader: `context` is the ReadContext the registry hands a read."""
    template = pack.active() if template is None else template
    live = registry(getattr(context, 'agent', None))
    builders = {
        'layers': lambda: _layers(template),
        'built_with': lambda: _built_with(template),
        'fixed_conversations': lambda: _fixed_conversations(template),
        'agent_classes': lambda: _agent_classes(context, template),
        'ready_agents': lambda: _ready_agents(template),
        'tools': lambda: _tools(live),
        'wakes': lambda: [{'kind': kind, 'state': state, 'note': note} for kind, state, note in WAKES] + template.wake_sources(),
        'channels': lambda: _channels(context, template),
        'recipes': lambda: template.recipes(),
        'sub_agents': lambda: template.sub_agents(),
        'tile_sets': lambda: _tile_sets(template),
        'event_vocabulary': lambda: _vocabulary(template),
        'flow': template.flow,
        'skills': lambda: _skills(context, template, live),
        'surfaces': template.surfaces,
        'data_sources': lambda: _data_sources(context, template, live),
        'connections_expected': template.connections_expected,
        'capabilities': lambda: capabilities(template),
    }
    wanted = [key for name, keys in PARTS.items() if section in ('all', name) for key in keys]
    answer = {'schema': SCHEMA, 'section': section,
              'product': {**template.product(), 'vertical': {'id': template.id, 'revision': template.revision}}}
    for key in wanted:
        answer[key] = builders[key]()
    answer['problems'] = _problems(template, live)
    return answer


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments == ['--readme']:
        write_readme()
        print('README.md holds the capability table.')
    else:
        print(capability_table())
    return 0


if __name__ == '__main__':
    sys.exit(main())
