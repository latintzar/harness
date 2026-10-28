"""The vertical this harness process serves: one validated manifest and the files it names.

Rocoto is one agent harness with one vertical per kind of business
(docs/decisions/0012-vertical-agent-harness.md). A vertical is a folder under verticals/; in code it
is a pack. Its manifest, vertical.json (schema rocoto.vertical.v1), declares what changes with the segment:

    product              the names an agent may say
    prompts              the developer prompt of every conversation, and the prefix of fixed chats
    fixed_chats          the company conversations every workspace has; organization is always one
    agent_classes        one class for each kind of long-running agent the harness stores, with its labels
    tile_sets              the maintained sets a location installs, with their processor scripts
    event_vocabulary     the event types of the segment, read from vertical JSON or a tile_set
    skills               the instruction skills seeded into a new company
    ready_agents      the ready-made agents the vertical offers
    tool_families        the tool families the vertical adds to the registry
    surfaces             the pages it ships and what fills each tab
    connections_expected the connections it expects a company to add
    data_sources         the registry of sources an agent reads, each with its read tools
    flow                 the stages of the work and what attaches to each: a script, an agent, the host
    demo                 starter material for the demo environment
    capabilities         optional: the vertical's own rows of the table of what works (harness_tools.py)

`active()` is the vertical of this process: ROCOTO_VERTICAL names its folder (legacy ROCOTO_PACK, default retail) and is
read once. `load()` validates everything a file can prove by itself and reads every named file,
each inside a stated size, so a broken vertical stops the service at start. The harness's own
bounds are part of that proof: the two stored agent kinds with their authorities and input kinds
(background_agents.py), and the skill store's bounds on a seed (skills.py).

A state directory stays with the vertical that created it, because fixed conversation ids and
seeded skills are written from it. `bind()` keeps that: the first open of a company's state
directory records the vertical's id in `.workspace-template.json`, and every later open under
another vertical is refused with both names.

`check_tools` compares the tool names of a manifest with a registry the caller passes, and
`check_tile_sets` holds each tile_set to the tool that installs it and to the module that holds its
processor scripts. The read tool get_harness (harness_tools.py) runs both against the live registry
on every call and reports a gap as a problem row; tests/test_pack.py runs them for retail against
`agent_bridge.tool_specs`. `use()` swaps the vertical for the length of a test.

Run `python pack.py <id>` to validate one vertical and print what it declares.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading

import layout

HERE = Path(__file__).resolve().parent
PACKS = layout.VERTICALS
MANIFEST = 'vertical.json'
LEGACY_MANIFEST = 'pack.json'
SCHEMA = 'rocoto.vertical.v1'
LEGACY_SCHEMA = 'rocoto.pack.v1'
SHELL_SCHEMA = 'rocoto.harness-shell.v1'
ENVIRONMENT = layout.ENVIRONMENT
DEFAULT = layout.DEFAULT

# The two authorities an agent class can have. They are harness rules, applied by stored kind.
#   participant_reach  phone chat with its own participants, host alert fan-out, a tile on the
#                      surface, starts agents of its own class, provider writes closed
#   granted_effects    acts on other people and business systems under separate grants
AUTHORITIES = ('participant_reach', 'granted_effects')
# The agent kinds the harness stores and the authority its rules give each one, and the inputs an
# agent can take (background_agents.py, agent_kind_policy.py). A vertical gives each kind its own
# labels and keeps the spelling; tests/test_pack.py holds these bounds to the agent store.
KIND_AUTHORITY = {'action': 'granted_effects', 'tracker': 'participant_reach'}
DEFAULT_KIND = 'action'
INPUT_KINDS = ('camera_footage', 'tracker_events')
# What a ready-made agent hands to a draft agent, inside the agent store's bounds: its instruction,
# its interval in seconds (a scheduled agent starts at one minute), its skills, its run allowance
# and the lines that say what it looks for.
AGENT_INSTRUCTION = 8000
AGENT_INTERVAL = (10, 604800)
AGENT_SCHEDULED_FROM = 60
AGENT_SKILLS = 8
AGENT_RUNS = (1, 20)
AGENT_WATCH_FOR = (8, 200)
# One named file holds up to this many bytes: a prompt, and a JSON list or tile_set.
PROMPT_BYTES = 256 * 1024
DATA_BYTES = 1024 * 1024
PROMPT_FOLDER = 'prompts'
# A skill seed is a skill definition, so it meets the skill store's bounds (skills.py `_definition`).
SEED_INSTRUCTIONS = 16000
SEED_TOOLS = 32
SEED_CONNECTIONS = 8
SEED_SCOPES = 16
# The record that ties a company's state directory to its template, and the file that shows the
# skill store has seeded a directory (skills.py).
BINDING = '.workspace-template.json'
BINDING_SCHEMA = 'rocoto.workspace-template.v1'
SEEDED = 'skills.sqlite'
REQUIRED_PURPOSE = 'organization'
OPENS = ('chat', 'member_setup')
DISPLAYS = ('tiles', 'canvases', 'player', 'list')
NAVS = ('primary', 'secondary')
ATTACHES = ('script', 'agent', 'host')
DISPLAY_SET_KINDS = ('core_trackers',)
AGENT_MODES = ('ad_hoc', 'scheduled', 'ongoing')
# The two sections every sidebar has beside the agent classes (`Pack.shell`). A class label is the
# key of its own section, so it stays apart from these two and from the other labels.
FIXED_SECTION = 'Organization'
RECENT_SECTION = 'Recent'
# The states a row of the table of what works can have, and the sentences a row holds (harness_tools.py).
CAPABILITY_STATES = ('real', 'partial', 'planned')
CAPABILITY_TEXTS = ('works', 'missing', 'for_show')

_ID = re.compile(r'[a-z][a-z0-9-]{0,39}\Z')
_KEY = re.compile(r'[a-z][a-z0-9_]{0,39}\Z')
_SLUG = re.compile(r'[a-z][a-z0-9_-]{0,79}\Z')
_TOOL = re.compile(r'[a-z][a-z0-9_]{0,79}\Z')
_REVISION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z')
_FAMILY = re.compile(r'[a-z_][a-z0-9_]{0,79}(?:\.[a-z_][a-z0-9_]{0,79}){0,3}(?::[A-Za-z_][A-Za-z0-9_]{0,79})?\Z')
_TEMPLATE_ID = re.compile(r'[a-zA-Z0-9_-]{1,100}\Z')
_PROVIDER = re.compile(r'[a-z][a-z0-9_-]{0,79}\Z')
_SCOPE = re.compile(r'read:[a-z][a-z0-9_.-]{0,79}\Z')
_REF = re.compile(r'(?:tile_sets|display_sets|recipes)/([a-z][a-z0-9-]{0,39})#([a-z][a-z0-9_]{0,39})\Z')
_FILE = re.compile(r'(?!/)(?!.*\.\.)[A-Za-z0-9_./-]{1,120}\Z')
_TOP = {'schema', 'id', 'revision', 'product', 'prompts', 'fixed_chats', 'agent_classes', 'tile_sets',
        'event_vocabulary', 'skills', 'ready_agents', 'tool_families', 'surfaces',
        'connections_expected', 'data_sources', 'flow', 'demo'}
_TOP_OPTIONAL = {'ready_agents_retired', 'capabilities', 'host_module', 'builtin_capabilities', 'recipes', 'sub_agents', 'wake_sources'}
_HOST_MODULE = re.compile(r'[a-z][a-z0-9_]{0,79}\Z')


class PackError(ValueError):
    """A vertical the harness declines to serve, with the reason as a sentence."""


def _need(condition, message):
    if not condition:
        raise PackError(message)


def _listed(names, last='or'):
    names = [str(name) for name in names]
    return ', '.join(names[:-1]) + f' {last} ' + names[-1] if len(names) > 1 else ''.join(names)


def _object(where, value, required, optional=()):
    _need(isinstance(value, dict), f'{where} is an object in the manifest.')
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    _need(not missing, f'{where} needs {_listed(missing, "and")}.')
    _need(not extra, f'{where} carries {_listed(extra, "and")}, outside the contract {SCHEMA}.')
    return value


def _text(where, value, maximum=2000):
    _need(isinstance(value, str) and value.strip() and len(value) <= maximum
          and all(ord(char) >= 32 for char in value), f'{where} is one line of readable text.')
    return value


def _match(where, value, pattern, shape):
    _need(isinstance(value, str) and pattern.fullmatch(value), f'{where} is {shape}.')
    return value


def _choice(where, value, choices):
    _need(isinstance(value, str) and value in choices, f'{where} is one of {_listed(choices)}.')
    return value


def _rows(where, value, minimum=0):
    _need(isinstance(value, list) and len(value) >= minimum,
          f'{where} is a list' + (f' with at least {minimum} entry.' if minimum else '.'))
    return value


def _unique(where, values, noun):
    seen = set()
    for value in values:
        _need(value not in seen, f'{where} names the {noun} {value} twice; each {noun} appears once.')
        seen.add(value)


def _pairs(pairs):
    keys = [key for key, _ in pairs]
    _unique('The manifest', keys, 'key')
    return dict(pairs)


def _constant(name):
    raise ValueError(f'{name} is outside JSON')


def _parse(where, data):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except PackError:
        raise
    except (ValueError, RecursionError) as error:
        # ValueError covers a wrong encoding, a syntax error, NaN and Infinity, and an oversized number.
        raise PackError(f'{where} is UTF-8 JSON: {error}.') from None


def _plain(char):
    """A printing character, a line feed or a tab."""
    return char in '\n\t' or (ord(char) >= 32 and ord(char) != 127)


def _bytes(where, path, limit):
    """The bytes of one regular file that has a single name on disk and fits its size."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise PackError(f'{where}; add that regular file to the vertical.') from None
    try:
        info = os.fstat(descriptor)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
              f'{where}; keep it as one regular file with a single name on disk.')
        data = b''
        while len(data) <= limit:
            more = os.read(descriptor, limit + 1 - len(data))
            if not more:
                break
            data += more
    finally:
        os.close(descriptor)
    _need(len(data) <= limit, f'{where}; that file holds up to {limit} bytes.')
    return data


class _Reader:
    """Reads the files a manifest names and keeps their bytes, each inside its allowed folder."""

    def __init__(self, root):
        self.root = root
        self.files = {}

    def read(self, where, relative, suffix, bounds=None, limit=DATA_BYTES):
        _need(isinstance(relative, str) and relative and '\\' not in relative and '\0' not in relative
              and not relative.startswith('/'), f'{where} is a relative path inside the vertical folder.')
        _need(relative.endswith(suffix), f'{where} names a {suffix} file.')
        plain = Path(os.path.normpath(self.root / relative))
        bounds = bounds or (self.root,)
        _need(any(plain.is_relative_to(bound) for bound in bounds),
              f'{where} names {relative}, outside the vertical folder; keep each file of a vertical inside it.')
        _need(plain.is_file() and plain.resolve() == plain,
              f'{where} names {relative}; add that regular file to the vertical.')
        key = plain.relative_to(self.root).as_posix() if plain.is_relative_to(self.root) else relative
        if key not in self.files:
            self.files[key] = _bytes(f'{where} names {relative}', plain, limit)
        return key

    def text(self, where, relative):
        key = self.read(where, relative, '.md', limit=PROMPT_BYTES)
        _need(key.startswith(PROMPT_FOLDER + '/'),
              f'{where} names {relative}; keep each prompt in the vertical\'s {PROMPT_FOLDER}/ folder.')
        try:
            text = self.files[key].decode('utf-8')
        except UnicodeDecodeError:
            raise PackError(f'{where} names {relative}; save it as UTF-8 text.') from None
        _need(text.strip(), f'{where} names {relative}; write the prompt\'s words in that file.')
        _need(text[0] != '\ufeff',
              f'{where} names {relative}; save it as UTF-8 that starts with its first word, the byte order mark removed.')
        odd = next((char for char in text if not _plain(char)), None)
        _need(odd is None, f'{where} names {relative}; a prompt holds printing characters, line feeds and tabs, '
                           f'and this one holds U+{ord(odd or " "):04X}.')
        return key

    def data(self, where, relative, bounds=None):
        key = self.read(where, relative, '.json', bounds)
        return key, _parse(f'{where} ({relative})', self.files[key])


def _skill(where, value, skill_ids):
    _need(isinstance(value, str) and value in skill_ids,
          f'{where}.skill names {value}; add that skill to the vertical\'s skills.')
    return value


def _fixed_chats(reader, rows, skill_ids):
    chats = []
    for index, row in enumerate(_rows('fixed_chats', rows, 1)):
        where = f'fixed_chats[{index}]'
        _object(where, row, {'purpose', 'title', 'icon', 'job', 'prompt'}, {'sidebar_title', 'skill', 'opens'})
        chat = {'purpose': _match(f'{where}.purpose', row['purpose'], _KEY, 'a lowercase key such as organization'),
                'title': _text(f'{where}.title', row['title'], 80),
                'icon': _match(f'{where}.icon', row['icon'], _SLUG, 'an icon name'),
                'job': _text(f'{where}.job', row['job'], 400),
                'opens': _choice(f'{where}.opens', row.get('opens', 'chat'), OPENS),
                'prompt': None if row['prompt'] is None else reader.text(f'{where}.prompt', row['prompt'])}
        if 'sidebar_title' in row:
            chat['sidebar_title'] = _text(f'{where}.sidebar_title', row['sidebar_title'], 80)
        if 'skill' in row:
            chat['skill'] = _skill(where, row['skill'], skill_ids)
        chats.append(chat)
    _unique('fixed_chats', [chat['purpose'] for chat in chats], 'purpose')
    # A person chooses a fixed chat by its title, and the sentence that lists them names each once.
    _unique('fixed_chats', [chat['title'] for chat in chats], 'title')
    _need(any(chat['purpose'] == REQUIRED_PURPOSE for chat in chats),
          f'fixed_chats includes the {REQUIRED_PURPOSE} conversation; every vertical has one.')
    return chats


def _agent_classes(rows):
    classes = []
    for index, row in enumerate(_rows('agent_classes', rows, 1)):
        where = f'agent_classes[{index}]'
        _object(where, row, {'kind', 'authority', 'label', 'plural', 'icon', 'create_label', 'empty_label',
                             'starter_prompt', 'input_kinds'}, {'default'})
        _need(isinstance(row['authority'], str) and row['authority'] in AUTHORITIES,
              f'{where}.authority is one of the harness\'s authorities: {_listed(AUTHORITIES)}.')
        kind = _match(f'{where}.kind', row['kind'], _TOOL, 'a lowercase agent class identifier')
        _need(kind not in KIND_AUTHORITY or row['authority'] == KIND_AUTHORITY[kind],
              f'{where}: the existing class {kind} keeps its authority {KIND_AUTHORITY.get(kind)}.')
        inputs = [_match(f'{where}.input_kinds', item, _TOOL, 'a lowercase input provider identifier')
                  for item in _rows(f'{where}.input_kinds', row['input_kinds'], 1)]
        _unique(f'{where}.input_kinds', inputs, 'input kind')
        _need(type(row.get('default', False)) is bool, f'{where}.default is true or false.')
        classes.append({
            'kind': kind, 'authority': row['authority'], 'default': row.get('default', False),
            'label': _text(f'{where}.label', row['label'], 40), 'plural': _text(f'{where}.plural', row['plural'], 60),
            'icon': _match(f'{where}.icon', row['icon'], _SLUG, 'an icon name'),
            'create_label': _text(f'{where}.create_label', row['create_label'], 60),
            'empty_label': _text(f'{where}.empty_label', row['empty_label'], 120),
            'starter_prompt': _text(f'{where}.starter_prompt', row['starter_prompt'], 1000),
            'input_kinds': inputs})
    _unique('agent_classes', [item['kind'] for item in classes], 'kind')
    _unique('agent_classes', [item['label'] for item in classes], 'label')
    # The sidebar keys each section by its name and by that name in lowercase letters and digits.
    taken = {section_slug(FIXED_SECTION): FIXED_SECTION, section_slug(RECENT_SECTION): RECENT_SECTION}
    for item in classes:
        slug = section_slug(item['label'])
        _need(slug not in taken, f"agent_classes gives a class the label {item['label']}, which the sidebar reads as its section "
                                 f"{taken.get(slug)}; give each class a label of its own.")
        taken[slug] = item['label']
    _need(sum(item['default'] for item in classes) == 1, 'agent_classes marks exactly one class as the default.')
    return classes


def section_slug(name):
    """A section name as the sidebar keys it: lowercase letters and digits, every other run a dash."""
    return re.sub(r'[^a-z0-9]+', '-', name.lower())


def _tile_sets(reader, rows):
    tile_sets, parsed = [], {}
    for index, row in enumerate(_rows('tile_sets', rows)):
        where = f'tile_sets[{index}]'
        _object(where, row, {'id', 'kind', 'path', 'revision', 'install_tool', 'processors'})
        tile_set = {'id': _match(f'{where}.id', row['id'], _ID, 'a lowercase id such as retail'),
                  'kind': _choice(f'{where}.kind', row['kind'], DISPLAY_SET_KINDS),
                  'revision': _match(f'{where}.revision', row['revision'], _REVISION, 'a revision name'),
                  'install_tool': _match(f'{where}.install_tool', row['install_tool'], _TOOL, 'a tool name'),
                  'processors': _match(f'{where}.processors', row['processors'], re.compile(r'[a-z_][a-z0-9_.]{0,79}\.\Z'),
                                       'a module prefix that ends with a dot, such as core_trackers.')}
        # A tile_set is the one file a vertical may keep beside the harness modules that run it.
        tile_set['path'], body = reader.data(f'{where}.path', row['path'], (reader.root,))
        _need(isinstance(body, dict) and body.get('revision') == tile_set['revision'],
              f'{where}.revision equals the revision written in {row["path"]}.')
        trackers = body.get('trackers')
        _need(isinstance(trackers, list) and trackers, f'{where} names a tile_set file with its trackers.')
        for tracker in trackers:
            processor = tracker.get('processor') if isinstance(tracker, dict) else None
            script = processor.get('script') if isinstance(processor, dict) else None
            _need(isinstance(tracker, dict) and isinstance(tracker.get('key'), str) and isinstance(script, str)
                  and script.startswith(tile_set['processors']),
                  f'{where}: each tracker has a key and a processor script under {tile_set["processors"]}')
        _unique(where, [tracker['key'] for tracker in trackers], 'tracker')
        tile_sets.append(tile_set)
        parsed[tile_set['id']] = body
    _unique('tile_sets', [tile_set['id'] for tile_set in tile_sets], 'tile_set')
    return tile_sets, parsed


def _vocabulary(reader, value, parsed):
    if value is None:
        return None, {}
    _object('event_vocabulary', value, {'groups'}, {'ref', 'path'})
    _need(('ref' in value) != ('path' in value),
          'event_vocabulary declares one source: a vertical JSON path or a tile_set ref.')
    if 'path' in value:
        relative = value['path']
        _need(isinstance(relative, str) and '..' not in Path(relative).parts,
              'event_vocabulary.path stays inside the vertical folder.')
        path, types = reader.data('event_vocabulary.path', relative)
        source = {'path': path}
    else:
        found = _REF.fullmatch(value['ref']) if isinstance(value['ref'], str) else None
        _need(found and found.group(1) in parsed and isinstance(parsed[found.group(1)].get(found.group(2)), dict),
              'event_vocabulary.ref points at a tile_set of this vertical, written tile_sets/<id>#vocabulary.')
        types = parsed[found.group(1)][found.group(2)]
        source = {'ref': 'tile_sets/' + value['ref'].split('/', 1)[1]}
    groups = [_match('event_vocabulary.groups', group, _KEY, 'a list of group keys')
              for group in _rows('event_vocabulary.groups', value['groups'], 1)]
    _need(len(groups) <= 64, 'event_vocabulary declares up to 64 groups.')
    _unique('event_vocabulary.groups', groups, 'group')
    _need(isinstance(types, dict) and len(types) <= 512,
          'event_vocabulary source is an object of up to 512 named event types.')
    for name, entry in types.items():
        _match('event_vocabulary event name', name, _KEY, 'a lowercase event key')
        _object(f'event_vocabulary.{name}', entry, {'group'}, {'label', 'scope', 'description'})
        _need(entry['group'] in groups,
              f'event_vocabulary.groups lists the group of every event type; {name} uses {entry["group"]}.')
        for key in ('label', 'description'):
            if key in entry:
                _text(f'event_vocabulary.{name}.{key}', entry[key], 400)
        if 'scope' in entry:
            _match(f'event_vocabulary.{name}.scope', entry['scope'], _KEY, 'a lowercase scope key')
    return {**source, 'groups': groups}, copy.deepcopy(types)


def _strings(where, value, pattern, shape):
    return [_match(where, item, pattern, shape) for item in _rows(where, value)]


def _seed_connections(where, value):
    """The connections a seed needs: up to eight providers, each with its read scopes."""
    rows = _rows(where, value)
    _need(len(rows) <= SEED_CONNECTIONS, f'{where} names {len(rows)} connections; a skill needs up to {SEED_CONNECTIONS}.')
    result = []
    for row in rows:
        _object(where, row, {'provider', 'scopes'})
        scopes = _strings(f'{where}.scopes', row['scopes'], _SCOPE, 'a list of read scopes, each written read:<name>')
        _need(1 <= len(scopes) <= SEED_SCOPES, f'{where}.scopes names 1 to {SEED_SCOPES} read scopes.')
        _unique(f'{where}.scopes', scopes, 'scope')
        result.append({'provider': _match(f'{where}.provider', row['provider'], _PROVIDER, 'a lowercase provider id'),
                       'scopes': scopes})
    _unique(where, [row['provider'] for row in result], 'provider')
    return result


def _skills(reader, value):
    if value is None or value == []:
        return None, ()
    key, rows = reader.data('skills', value)
    seeds = []
    for index, row in enumerate(_rows(f'skills ({value})', rows)):
        where = f'{value}[{index}]'
        _need(isinstance(row, dict) and ('instructions' in row) != ('instructions_from' in row),
              f'{where} gives its text once: instructions, or instructions_from with the prompt files to join.')
        _object(where, row, {'id', 'name', 'description', 'required_tools'},
                {'instructions', 'instructions_from', 'required_connections'})
        if 'instructions' in row:
            _need(isinstance(row['instructions'], str) and row['instructions'].strip()
                  and all(_plain(char) for char in row['instructions']), f'{where}.instructions is text.')
            instructions = row['instructions']
        else:
            parts = _rows(f'{where}.instructions_from', row['instructions_from'], 1)
            instructions = ''.join(reader.files[reader.text(f'{where}.instructions_from', part)].decode('utf-8')
                                   for part in parts)
        _need(len(instructions) <= SEED_INSTRUCTIONS,
              f'{where} carries {len(instructions)} characters of instructions; a skill holds up to {SEED_INSTRUCTIONS}.')
        tools = _strings(f'{where}.required_tools', row['required_tools'], _TOOL, 'a list of tool names')
        _need(len(tools) <= SEED_TOOLS, f'{where}.required_tools names {len(tools)} tools; a skill needs up to {SEED_TOOLS}.')
        _unique(f'{where}.required_tools', tools, 'tool')
        seeds.append({'id': _match(f'{where}.id', row['id'], _SLUG, 'a lowercase skill id'),
                      'name': _text(f'{where}.name', row['name'], 200),
                      'description': _text(f'{where}.description', row['description'], 2000),
                      'instructions': instructions, 'required_tools': tools,
                      'required_connections': _seed_connections(f'{where}.required_connections',
                                                                row.get('required_connections', []))})
    _unique(f'skills ({value})', [seed['id'] for seed in seeds], 'skill')
    return key, tuple(seeds)


def _templates(reader, where, value, skill_ids):
    if value is None or value == []:
        return None, ()
    if isinstance(value, str):
        key, rows = reader.data(where, value)
    else:
        key, rows = None, value
    for index, row in enumerate(_rows(where, rows)):
        spot = f'{where}[{index}]'
        _need(isinstance(row, dict), f'{spot} is an object.')
        _match(f'{spot}.id', row.get('id'), _TEMPLATE_ID, 'a vertical id')
        _text(f'{spot}.name', row.get('name'), 200)
        mode = _choice(f'{spot}.mode', row.get('mode'), AGENT_MODES)
        # The Customize page draws both texts, and a draft agent starts from the instructions.
        _text(f'{spot}.description', row.get('description'), 2000)
        instructions = row.get('instructions')
        _need(isinstance(instructions, str) and instructions.strip() and len(instructions) <= AGENT_INSTRUCTION
              and all(_plain(char) for char in instructions),
              f'{spot}.instructions is text of up to {AGENT_INSTRUCTION} characters.')
        if mode == 'scheduled' or 'interval_seconds' in row:
            first = AGENT_SCHEDULED_FROM if mode == 'scheduled' else AGENT_INTERVAL[0]
            interval = row.get('interval_seconds')
            _need(type(interval) is int and first <= interval <= AGENT_INTERVAL[1],
                  f'{spot}.interval_seconds is a whole number of seconds from {first} to {AGENT_INTERVAL[1]}.')
        if 'skill_ids' in row:
            chosen = _rows(f'{spot}.skill_ids', row['skill_ids'])
            _need(len(chosen) <= AGENT_SKILLS, f'{spot}.skill_ids names {len(chosen)} skills; an agent takes up to {AGENT_SKILLS}.')
            for skill in chosen:
                _need(isinstance(skill, str) and skill in skill_ids,
                      f'{spot}.skill_ids names {skill}; add that skill to the vertical\'s skills.')
            _unique(f'{spot}.skill_ids', chosen, 'skill')
        # What a draft agent takes beside its text: its class, its input, its run allowance, the
        # event types it reads and the lines that say what it looks for.
        if 'agent_kind' in row:
            _match(f'{spot}.agent_kind', row['agent_kind'], _TOOL, 'a lowercase agent class identifier')
        if 'input_kind' in row:
            _match(f'{spot}.input_kind', row['input_kind'], _TOOL, 'a lowercase input provider identifier')
        if 'max_runs' in row:
            _need(type(row['max_runs']) is int and AGENT_RUNS[0] <= row['max_runs'] <= AGENT_RUNS[1],
                  f'{spot}.max_runs is a whole number of runs from {AGENT_RUNS[0]} to {AGENT_RUNS[1]}.')
        if 'event_types' in row:
            _unique(f'{spot}.event_types', _strings(f'{spot}.event_types', row['event_types'], _KEY, 'a list of event type names'),
                    'event type')
        if 'watch_for' in row:
            lines = _rows(f'{spot}.watch_for', row['watch_for'])
            _need(len(lines) <= AGENT_WATCH_FOR[0], f'{spot}.watch_for holds up to {AGENT_WATCH_FOR[0]} lines.')
            for line in lines:
                _text(f'{spot}.watch_for', line, AGENT_WATCH_FOR[1])
    return key, tuple(copy.deepcopy(rows))


def _ready_made(rows, classes, types):
    """Each ready-made agent on offer takes an input of its class and reads event types of this vertical."""
    inputs = {item['kind']: item['input_kinds'] for item in classes}
    default = next(item['kind'] for item in classes if item['default'])
    for index, row in enumerate(rows):
        spot = f'ready_agents[{index}]'
        kind = row.get('agent_kind', default)
        _need(kind in inputs, f'{spot}.agent_kind names a declared agent class.')
        if 'input_kind' in row:
            _need(row['input_kind'] in inputs[kind],
                  f'{spot}.input_kind is an input of the class {kind}: {_listed(inputs[kind])}.')
        for name in row.get('event_types', ()):
            _need(name in types, f'{spot}.event_types names {name}; add that event type to the vocabulary of the vertical.')


def _capabilities(reader, value):
    """The vertical's rows of the table of what works: rows of its own, and sentences for a harness row.

    A row of its own has an id, a title, a state, at least one sentence and the files behind it.
    A row with `adds_to` names a row of the harness's table (harness_tools.CAPABILITIES) and adds
    its sentences, its files, and a state or a title where the segment changes them.
    """
    if value is None:
        return None, ()
    key, rows = reader.data('capabilities', value)
    result = []
    for index, row in enumerate(_rows(f'capabilities ({value})', rows)):
        where = f'{value}[{index}]'
        _need(isinstance(row, dict) and ('id' in row) != ('adds_to' in row),
              f'{where} is a row of its own with an id, or sentences for a harness row named in adds_to.')
        own = 'id' in row
        _object(where, row, {'id', 'title', 'state', 'where'} if own else {'adds_to'},
                set(CAPABILITY_TEXTS) if own else {'title', 'state', 'where', *CAPABILITY_TEXTS})
        entry = {'id' if own else 'adds_to': _match(f'{where}.{"id" if own else "adds_to"}', row['id' if own else 'adds_to'],
                                                    _KEY, 'a lowercase key')}
        if 'title' in row:
            entry['title'] = _text(f'{where}.title', row['title'], 120)
        if 'state' in row:
            entry['state'] = _choice(f'{where}.state', row['state'], CAPABILITY_STATES)
        for name in CAPABILITY_TEXTS:
            if name in row:
                entry[name] = _text(f'{where}.{name}', row[name], 1200)
        if 'where' in row:
            entry['where'] = _strings(f'{where}.where', row['where'], _FILE, 'a list of file names inside the repository')
        _need(len(entry) > 1 and (not own or (entry['where'] and any(name in entry for name in CAPABILITY_TEXTS))),
              f'{where} says something: a sentence under works, missing or for_show' + (', and the files behind it.' if own else '.'))
        result.append(entry)
    _unique(f'capabilities ({value})', [entry['id'] for entry in result if 'id' in entry], 'row')
    _unique(f'capabilities ({value})', [entry['adds_to'] for entry in result if 'adds_to' in entry], 'harness row')
    return key, tuple(result)


def _surfaces(rows):
    surfaces = []
    for index, row in enumerate(_rows('surfaces', rows)):
        where = f'surfaces[{index}]'
        _object(where, row, {'id', 'title', 'nav', 'tabs'})
        tabs = []
        for number, tab in enumerate(_rows(f'{where}.tabs', row['tabs'], 1)):
            spot = f'{where}.tabs[{number}]'
            _object(spot, tab, {'id', 'title', 'display', 'content', 'produced_by', 'read_with'})
            tabs.append({'id': _match(f'{spot}.id', tab['id'], _KEY, 'a lowercase key'),
                         'title': _text(f'{spot}.title', tab['title'], 80),
                         'display': _choice(f'{spot}.display', tab['display'], DISPLAYS),
                         'content': _text(f'{spot}.content', tab['content'], 200),
                         'produced_by': _text(f'{spot}.produced_by', tab['produced_by'], 200),
                         'read_with': _match(f'{spot}.read_with', tab['read_with'], _TOOL, 'the name of one read tool')})
        _unique(where, [tab['id'] for tab in tabs], 'tab')
        surfaces.append({'id': _match(f'{where}.id', row['id'], _KEY, 'a lowercase key'),
                         'title': _text(f'{where}.title', row['title'], 80),
                         'nav': _choice(f'{where}.nav', row['nav'], NAVS), 'tabs': tabs})
    _unique('surfaces', [surface['id'] for surface in surfaces], 'surface')
    return surfaces


def _connections(rows, skill_ids):
    result = []
    for index, row in enumerate(_rows('connections_expected', rows)):
        where = f'connections_expected[{index}]'
        _object(where, row, {'id', 'kind', 'purpose'}, {'skill'})
        entry = {'id': _match(f'{where}.id', row['id'], _KEY, 'a lowercase key'),
                 'kind': _match(f'{where}.kind', row['kind'], _KEY, 'a lowercase kind such as custom_connector'),
                 'purpose': _text(f'{where}.purpose', row['purpose'], 200)}
        if 'skill' in row:
            entry['skill'] = _skill(where, row['skill'], skill_ids)
        result.append(entry)
    _unique('connections_expected', [entry['id'] for entry in result], 'connection')
    return result


def _data_sources(rows, skill_ids):
    result = []
    for index, row in enumerate(_rows('data_sources', rows)):
        where = f'data_sources[{index}]'
        _object(where, row, {'id', 'title', 'read_with'}, {'skill'})
        reads = _strings(f'{where}.read_with', row['read_with'], _TOOL, 'a list of read tool names')
        _need(reads, f'{where}.read_with names at least one read tool.')
        entry = {'id': _match(f'{where}.id', row['id'], _KEY, 'a lowercase key'),
                 'title': _text(f'{where}.title', row['title'], 120), 'read_with': reads}
        if 'skill' in row:
            entry['skill'] = _skill(where, row['skill'], skill_ids)
        result.append(entry)
    _unique('data_sources', [entry['id'] for entry in result], 'data source')
    return result


def _wake_sources(rows):
    result = []
    for row in _rows('wake_sources', rows):
        _object('wake_sources', row, {'kind', 'state', 'note'})
        _match('wake source kind', row['kind'], _SLUG, 'a stable lowercase kind')
        _choice('wake source state', row['state'], ('real', 'partial', 'planned'))
        _text('wake source note', row['note'])
        result.append(dict(row))
    _unique('wake_sources', [row['kind'] for row in result], 'kind')
    return result


def _flow(rows):
    result = []
    for index, row in enumerate(_rows('flow', rows)):
        where = f'flow[{index}]'
        _object(where, row, {'stage', 'does', 'attaches'})
        result.append({'stage': _match(f'{where}.stage', row['stage'], _KEY, 'a lowercase key'),
                       'does': _text(f'{where}.does', row['does'], 200),
                       'attaches': _choice(f'{where}.attaches', row['attaches'], ATTACHES)})
    _unique('flow', [entry['stage'] for entry in result], 'stage')
    return result


@dataclass(frozen=True, eq=False)
class Pack:
    """One validated vertical. Every accessor returns its own copy, so callers can keep what they get."""
    id: str
    revision: str
    root: Path
    manifest: dict
    files: dict = field(repr=False)
    _manifest_name: str = field(repr=False, default=MANIFEST)
    _tile_sets: dict = field(repr=False, default_factory=dict)
    _event_types: dict = field(repr=False, default_factory=dict)
    _jobs: tuple = field(repr=False, default=())
    _sub_agents: tuple = field(repr=False, default=())
    _skills: tuple = field(repr=False, default=())
    _templates: tuple = field(repr=False, default=())
    _retired: tuple = field(repr=False, default=())
    _capabilities: tuple = field(repr=False, default=())

    @property
    def manifest_path(self):
        return self.root / self._manifest_name

    # Names and prompts.
    def product(self):
        return dict(self.manifest['product'])

    def prompt(self, name):
        """The text of 'system' or 'fixed_chat_common', exactly as its file holds it."""
        _need(name in ('system', 'fixed_chat_common'), 'Choose the system prompt or the fixed chat prefix.')
        key = self.manifest['prompts'][name]
        return None if key is None else self.files[key].decode('utf-8')

    # Fixed company conversations.
    def fixed_chats(self):
        return copy.deepcopy(self.manifest['fixed_chats'])

    def purposes(self):
        """{purpose: title} in the vertical's order."""
        return {chat['purpose']: chat['title'] for chat in self.manifest['fixed_chats']}

    def choose_purpose(self):
        return f"Choose {_listed(chat['title'] for chat in self.manifest['fixed_chats'])}."

    def fixed_chat(self, purpose):
        for chat in self.manifest['fixed_chats']:
            if chat['purpose'] == purpose:
                return dict(chat)
        raise PackError(self.choose_purpose())

    def fixed_chat_text(self, purpose):
        """The standing instruction of one fixed chat by itself; None for a chat that carries the developer prompt alone."""
        key = self.fixed_chat(purpose)['prompt']
        return None if key is None else self.files[key].decode('utf-8')

    def fixed_chat_prompt(self, purpose):
        """What a fixed chat adds to the developer prompt: the shared prefix, a newline, its own text."""
        own = self.fixed_chat_text(purpose)
        if own is None:
            return None
        common = self.prompt('fixed_chat_common')
        return own if common is None else common + '\n' + own

    # Agent classes.
    def agent_classes(self):
        return copy.deepcopy(self.manifest['agent_classes'])

    def kinds(self):
        return tuple(item['kind'] for item in self.manifest['agent_classes'])

    def agent_class(self, kind):
        for item in self.manifest['agent_classes']:
            if item['kind'] == kind:
                return copy.deepcopy(item)
        raise PackError(f"Choose an agent class of this product: {_listed(item['label'] for item in self.manifest['agent_classes'])}.")

    def default_kind(self):
        return next(item['kind'] for item in self.manifest['agent_classes'] if item['default'])

    def authority(self, kind):
        return self.agent_class(kind)['authority']

    # Display sets and vocabulary.
    def tile_sets(self):
        return copy.deepcopy(self.manifest['tile_sets'])

    def tile_set_path(self, tile_set_id):
        entry = next((item for item in self.manifest['tile_sets'] if item['id'] == tile_set_id), None)
        if entry is None:
            raise PackError('Choose a tile_set of this vertical'
                            + (f": {_listed(item['id'] for item in self.manifest['tile_sets'])}." if self.manifest['tile_sets'] else '.'))
        return Path(os.path.normpath(self.root / entry['path']))

    def tile_set(self, tile_set_id):
        """The tile_set file, parsed, as it was read when the vertical loaded."""
        self.tile_set_path(tile_set_id)
        return copy.deepcopy(self._tile_sets[tile_set_id])

    # One-release Python aliases for callers of the original manifest contract.
    display_sets = tile_sets
    display_set_path = tile_set_path
    display_set = tile_set

    def vocabulary(self):
        """{'groups': [...], 'types': {event type: entry}}; empty for a vertical that declares zero event types."""
        declared = self.manifest['event_vocabulary']
        if declared is None:
            return {'groups': [], 'types': {}}
        return {'groups': list(declared['groups']), 'types': copy.deepcopy(self._event_types)}

    def recipes(self):
        """Complete jobs with declared steps and completion criteria."""
        return list(copy.deepcopy(self._jobs))

    def recipe(self, recipe_id):
        for row in self._jobs:
            if row['id'] == recipe_id:
                return copy.deepcopy(row)
        raise PackError('Choose a recipe declared by this vertical.')

    def sub_agents(self):
        return list(copy.deepcopy(self._sub_agents))

    def sub_agent(self, role_id):
        for row in self._sub_agents:
            if row['id'] == role_id:
                return copy.deepcopy(row)
        raise PackError('Choose a sub-agent role declared by this vertical.')

    # Skills and ready-made agents.
    def skills(self):
        """The skill seeds of a new company: id, name, description, instructions, required tools and connections."""
        return copy.deepcopy(self._skills)

    def ready_agents(self):
        """The ready-made agents the vertical offers (`ready_agents`)."""
        return list(copy.deepcopy(self._templates))

    def retired_ready_agents(self):
        """Templates kept readable for the conversations that were started from them."""
        return list(copy.deepcopy(self._retired))

    agent_templates = ready_agents
    retired_agent_templates = retired_ready_agents

    def import_host(self):
        """Load the explicitly declared composition module from this vertical's folder."""
        name = self.manifest.get('host_module')
        if name is None:
            return None
        expected = self.root / (name + '.py')
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError) as error:
            raise PackError(f'host_module {name} must resolve inside its vertical folder.') from error
        _need(spec is not None and spec.origin is not None
              and Path(spec.origin).absolute() == expected and expected.resolve() == expected,
              f'host_module {name} must resolve inside its vertical folder.')
        _need(_bytes(f'host_module {name}', expected, DATA_BYTES) == self.files[name + '.py'],
              f'host_module {name} must match the source validated at startup.')
        module = importlib.import_module(name)
        _need(Path(module.__file__).resolve() == expected,
              f'host_module {name} must load from its vertical folder.')
        for hook in ('install_workspace', 'register_routes', 'lifespan'):
            _need(callable(getattr(module, hook, None)), f'host_module {name} supplies {hook}.')
        return module

    # Tools.
    def tool_families(self):
        return tuple(self.manifest['tool_families'])

    def import_tool_families(self):
        """Import declared families only from this vertical's own module tree."""
        families = []
        for entry in self.manifest['tool_families']:
            module, _, attribute = entry.partition(':')
            parts = module.split('.')
            try:
                # Check each parent before find_spec can execute a package initializer.
                for count in range(1, len(parts) + 1):
                    name = '.'.join(parts[:count])
                    base = self.root.joinpath(*parts[:count])
                    expected = base / '__init__.py' if base.is_dir() else base.with_suffix('.py')
                    _need(expected.is_file() and expected.resolve() == expected,
                          f'tool_families names {entry}; keep its module inside the vertical folder.')
                    spec = importlib.util.find_spec(name)
                    _need(spec is not None and spec.origin is not None
                          and Path(spec.origin).absolute() == expected,
                          f'tool_families names {entry}; load its module from the vertical folder.')
                    found = importlib.import_module(name)
                    _need(Path(found.__file__).resolve() == expected,
                          f'tool_families names {entry}; load its module from the vertical folder.')
                families.append(getattr(found, attribute) if attribute else found)
            except (ImportError, AttributeError, TypeError, ValueError) as error:
                raise PackError(f'tool_families names {entry}; that family has to import: {error}.') from None
        return families

    def tool_references(self):
        """Every (place, tool name) the manifest relies on outside its skills."""
        found = []
        for surface in self.manifest['surfaces']:
            found += [(f"surface {surface['id']}, tab {tab['id']}", tab['read_with']) for tab in surface['tabs']]
        for source in self.manifest['data_sources']:
            found += [(f"data source {source['id']}", name) for name in source['read_with']]
        found += [(f"tile_set {tile_set['id']}", tile_set['install_tool']) for tile_set in self.manifest['tile_sets']]
        return found

    def check_tools(self, registered_names):
        """The tool names of the manifest that a registry lacks, as sentences. Empty means every name is registered.

        The caller passes every tool name a conversation can call. The loader leaves this check to
        the code that owns the registry, so loading a vertical stays independent of it. Its callers
        are get_harness (harness_tools.py), with the live registry, and tests/test_pack.py.
        """
        registered = set(registered_names)
        return [f'{where} uses {name}; register that tool or correct the name in {MANIFEST}.'
                for where, name in self.tool_references() if name not in registered]

    def check_tile_sets(self, tool_specs):
        """What stands between each tile_set and its install, as sentences. Empty means every tile_set installs.

        The caller passes the specs of the tools a conversation can call (`name`, `inputSchema`).
        A tile_set installs once its install tool takes the tile_set's id (the id is a value of one
        of the tool's enumerated fields, or the tool has zero enumerated fields) and the module
        that holds its processor scripts imports.
        """
        specs = {spec.get('name'): spec for spec in tool_specs}
        problems = []
        for tile_set in self.manifest['tile_sets']:
            fields = (specs.get(tile_set['install_tool']) or {}).get('inputSchema', {}).get('properties', {})
            taken = [field['enum'] for field in fields.values() if isinstance(field, dict) and isinstance(field.get('enum'), list)]
            if taken and all(tile_set['id'] not in values for values in taken):
                problems.append(f"tile_set {tile_set['id']} installs through {tile_set['install_tool']}, which takes "
                                f"{_listed(sorted({str(value) for values in taken for value in values}))}; "
                                f"add {tile_set['id']} to the values that tool takes.")
            module = tile_set['processors'].rstrip('.')
            try:
                found = importlib.util.find_spec(module)
            except (ImportError, ValueError):
                found = None
            if found is None:
                problems.append(f"tile_set {tile_set['id']} keeps its processor scripts in the module {module}; "
                                f"add that module to the service.")
        return problems

    check_display_sets = check_tile_sets

    def skill_tool_gaps(self, registered_names):
        """{skill id: the required tools these names lack}, for each skill they hold back.

        A turn admits a skill against the tools of `agent_bridge.tool_specs` and `web_search`,
        which the turn adds by itself; pass both for the answer a turn gives.
        """
        registered = set(registered_names)
        gaps = {seed['id']: tuple(name for name in seed['required_tools'] if name not in registered)
                for seed in self._skills}
        return {skill: missing for skill, missing in gaps.items() if missing}

    # Surfaces, connections, data sources, flow.
    def surfaces(self):
        return copy.deepcopy(self.manifest['surfaces'])

    def builtin_capabilities(self):
        return dict(self.manifest['builtin_capabilities'])

    def connections_expected(self):
        return copy.deepcopy(self.manifest['connections_expected'])

    def data_sources(self):
        return copy.deepcopy(self.manifest['data_sources'])

    def wake_sources(self):
        return copy.deepcopy(self.manifest['wake_sources'])

    def flow(self):
        return copy.deepcopy(self.manifest['flow'])

    def demo(self):
        return copy.deepcopy(self.manifest['demo'])

    def capabilities(self):
        """The vertical's rows of the table of what works: its own rows and its sentences for harness rows."""
        return [dict(row) for row in copy.deepcopy(self._capabilities)]

    def shell(self):
        """What the web app needs to draw the sidebar and the pages (schema rocoto.harness-shell.v1)."""
        manifest = self.manifest
        sections = [{'key': FIXED_SECTION, 'type': 'fixed'}]
        sections += [{'key': item['label'], 'type': 'agent_class', 'kind': item['kind'], 'authority': item['authority'],
                      'icon': item['icon'], 'create_label': item['create_label'], 'empty_label': item['empty_label'],
                      'starter_prompt': item['starter_prompt'], 'default': item['default'],
                      'input_kinds': list(item['input_kinds'])} for item in manifest['agent_classes']]
        sections.append({'key': RECENT_SECTION, 'type': 'recent'})
        return {
            'schema': SHELL_SCHEMA,
            'vertical': {'id': self.id, 'revision': self.revision},
            'product': {'name': manifest['product']['name']},
            'fixed_chats': [{'purpose': chat['purpose'], 'title': chat.get('sidebar_title', chat['title']),
                             'icon': chat['icon'], 'opens': chat['opens'], 'job': chat['job']} for chat in manifest['fixed_chats']],
            'sections': sections,
            'surfaces': [{'id': surface['id'], 'title': surface['title'], 'nav': surface['nav']}
                         for surface in manifest['surfaces']],
        }

    def digest(self):
        """sha256 over the manifest and every file it names: one value for 'this exact template'."""
        value = hashlib.sha256(json.dumps(self.manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        for key in sorted(self.files):
            value.update(b'\0' + key.encode() + b'\0' + hashlib.sha256(self.files[key]).digest())
        return value.hexdigest()


def _folder(source):
    if isinstance(source, Pack):
        return source.root
    if isinstance(source, Path):
        return (source.parent if source.name in {MANIFEST, LEGACY_MANIFEST} else source).resolve()
    _need(isinstance(source, str) and _ID.fullmatch(source),
          f'{ENVIRONMENT} names one vertical folder under verticals/, written like {DEFAULT}.')
    return PACKS / source


def manifest_path(root):
    """Read the canonical manifest first; an invalid new file never selects an old one."""
    root = Path(root)
    for name in (MANIFEST, LEGACY_MANIFEST):
        candidate = root / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return root / MANIFEST


def _canonical_manifest(raw):
    _need(isinstance(raw, dict), f'{MANIFEST} is an object in the manifest.')
    raw = dict(raw)
    legacy = raw.get('schema') == LEGACY_SCHEMA
    _need(isinstance(raw.get('schema'), str) and raw['schema'] in {SCHEMA, LEGACY_SCHEMA}, f'{MANIFEST} declares the schema {SCHEMA}.')
    # The earliest legacy manifest used recipes for installable tile sets.
    # A whole-job recipe has steps/instructions and never enters this conversion.
    prior_recipes = raw.get('recipes')
    if (legacy and 'tile_sets' not in raw and 'display_sets' not in raw
            and isinstance(prior_recipes, list)
            and all(isinstance(row, dict) and 'kind' in row and 'path' in row for row in prior_recipes)):
        raw['tile_sets'] = raw.pop('recipes')
        raw['recipes'] = []
    raw['schema'] = SCHEMA
    if isinstance(raw.get('product'), dict):
        raw['product'] = dict(raw['product'])
        if 'segment' in raw['product']:
            raw['product'].setdefault('business', raw['product'].pop('segment'))
    for old, new in (('display_sets', 'tile_sets'), ('agent_templates', 'ready_agents'),
                     ('agent_templates_retired', 'ready_agents_retired')):
        if old in raw:
            if new not in raw:
                raw[new] = raw[old]
            del raw[old]
    return raw


def load(source):
    """Validate one vertical and read every file it names. A string is a folder name under verticals/; a Path is a folder."""
    root = _folder(source)
    path = manifest_path(root)
    _need(path.is_file() and path.resolve() == path, f'The vertical {root.name} has its manifest at {root.name}/{MANIFEST}.')
    reader = _Reader(root)
    raw = _canonical_manifest(_parse(path.name, _bytes(f'The vertical {root.name} has {path.name}', path, DATA_BYTES)))
    _object(MANIFEST, raw, _TOP, _TOP_OPTIONAL)
    _need(raw['schema'] == SCHEMA, f'{MANIFEST} declares the schema {SCHEMA}.')
    _match('id', raw['id'], _ID, 'a lowercase id such as retail')
    _need(raw['id'] == root.name, f'The vertical id {raw["id"]} equals its folder name {root.name}.')
    _match('revision', raw['revision'], _REVISION, 'a revision name that changes with every change to the vertical')
    product = _object('product', raw['product'], {'name', 'business', 'capability_name', 'summary'})
    product = {key: _text(f'product.{key}', product[key], 400) for key in ('name', 'business', 'capability_name', 'summary')}
    prompts = _object('prompts', raw['prompts'], {'system', 'fixed_chat_common'})
    prompts = {'system': reader.text('prompts.system', prompts['system']),
               'fixed_chat_common': None if prompts['fixed_chat_common'] is None
               else reader.text('prompts.fixed_chat_common', prompts['fixed_chat_common'])}
    skills_key, seeds = _skills(reader, raw['skills'])
    skill_ids = {seed['id'] for seed in seeds}
    tile_sets, parsed = _tile_sets(reader, raw['tile_sets'])
    templates_key, templates = _templates(reader, 'ready_agents', raw['ready_agents'], skill_ids)
    retired_key, retired = _templates(reader, 'ready_agents_retired', raw.get('ready_agents_retired'), skill_ids)
    capabilities_key, capabilities = _capabilities(reader, raw.get('capabilities'))
    from capability_contract import selection
    try:
        builtins = selection(raw.get('builtin_capabilities'))
    except ValueError as error:
        raise PackError(str(error)) from None
    _unique('ready_agents', [row['id'] for row in templates + retired], 'ready-made agent')
    families = _strings('tool_families', raw['tool_families'], _FAMILY, 'a list of module or module:attribute names')
    _unique('tool_families', families, 'family')
    _need(raw['demo'] is None or isinstance(raw['demo'], dict), 'demo is an object or null.')
    host_module = raw.get('host_module')
    if host_module is not None:
        _match('host_module', host_module, _HOST_MODULE, 'one lowercase module name in the vertical folder')
        reader.read('host_module', host_module + '.py', '.py')
    classes = _agent_classes(raw['agent_classes'])
    vocabulary, event_types = _vocabulary(reader, raw['event_vocabulary'], parsed)
    _ready_made(templates, classes, event_types)
    from job_recipes import load_jobs
    jobs, roles = load_jobs(reader, raw.get('recipes', []), raw.get('sub_agents', []), classes, raw['fixed_chats'])
    manifest = {
        'schema': SCHEMA, 'id': raw['id'], 'revision': raw['revision'], 'product': product, 'prompts': prompts,
        'fixed_chats': _fixed_chats(reader, raw['fixed_chats'], skill_ids),
        'agent_classes': classes,
        'tile_sets': tile_sets, 'event_vocabulary': vocabulary,
        'recipes': copy.deepcopy(raw.get('recipes', [])), 'sub_agents': copy.deepcopy(raw.get('sub_agents', [])),
        'skills': skills_key,
        'ready_agents': templates_key if templates_key else [dict(row) for row in templates],
        'ready_agents_retired': retired_key,
        'tool_families': families, 'surfaces': _surfaces(raw['surfaces']),
        'connections_expected': _connections(raw['connections_expected'], skill_ids),
        'data_sources': _data_sources(raw['data_sources'], skill_ids), 'flow': _flow(raw['flow']),
        'demo': copy.deepcopy(raw['demo']),
        'capabilities': capabilities_key, 'host_module': host_module,
        'builtin_capabilities': builtins, 'wake_sources': _wake_sources(raw.get('wake_sources', [])),
    }
    return Pack(id=raw['id'], revision=raw['revision'], root=root, manifest=manifest, files=dict(reader.files),
                _manifest_name=path.name, _tile_sets=parsed, _event_types=event_types, _jobs=jobs, _sub_agents=roles, _skills=seeds, _templates=templates, _retired=retired, _capabilities=capabilities)


_LOCK = threading.Lock()
_ACTIVE = None


def active():
    """The vertical of this process, loaded on first use from the selected vertical and kept for its life."""
    global _ACTIVE
    if _ACTIVE is None:
        with _LOCK:
            if _ACTIVE is None:
                try:
                    chosen = layout.selected_vertical()
                except ValueError as error:
                    raise PackError(str(error)) from error
                _ACTIVE = load(chosen)
    return _ACTIVE


@contextmanager
def use(source):
    """Serve another vertical for the length of a with block. For tests: one template per state directory (`bind`)."""
    global _ACTIVE
    chosen = source if isinstance(source, Pack) else load(source)
    with _LOCK:
        previous, _ACTIVE = _ACTIVE, chosen
    try:
        yield chosen
    finally:
        with _LOCK:
            _ACTIVE = previous


def _binding(path):
    """The vertical id a state directory records, or None while the record is absent."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        descriptor = None
    data = b''
    if descriptor is not None:
        try:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                data = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
    try:
        record = json.loads(data.decode('utf-8')) if 0 < len(data) <= 4096 else None
    except ValueError:
        record = None
    recorded = record.get('template') if isinstance(record, dict) and set(record) == {'schema', 'template'} else None
    _need(isinstance(recorded, str) and _ID.fullmatch(recorded) and record['schema'] == BINDING_SCHEMA,
          f'{path} records the vertical of this state directory; restore that file from the directory\'s backup.')
    return recorded


def _record(state, path, template_id):
    """Write the record once and return the id on disk: this one, or the one another process wrote first."""
    descriptor, name = tempfile.mkstemp(prefix='.workspace-template-', suffix='.tmp', dir=state)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'schema': BINDING_SCHEMA, 'template': template_id}, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)  # appears whole, and only while the name is free
        except FileExistsError:
            return _binding(path)
        return template_id
    finally:
        os.unlink(name)


def bind(state, template=None):
    """Tie one company's state directory to the vertical of this process, and return that template.

    The first open writes the vertical's id to `.workspace-template.json`, beside the workspace
    identity. A directory the skill store seeded before this record existed belongs to the default
    template, the one every company had then. Every later open compares the two ids, and a
    directory that another vertical created stays closed to this one.
    """
    template = active() if template is None else template
    state = Path(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state / BINDING
    recorded = _binding(path)
    if recorded is None:
        recorded = DEFAULT if (state / SEEDED).exists() else template.id
        if recorded == template.id:
            recorded = _record(state, path, recorded)
    _need(recorded == template.id,
          f'This state directory belongs to the vertical {recorded}; this service serves {template.id}. '
          f'Start it with {ENVIRONMENT}={recorded}, or give {template.id} a state directory of its own.')
    return template


def describe(template):
    """A vertical in a dozen lines, for a person who is writing one."""
    manifest = template.manifest
    return '\n'.join([
        f"vertical {template.id}, revision {template.revision}, digest {template.digest()[:16]}",
        f"product: {manifest['product']['name']} for {manifest['product']['business']} ({manifest['product']['capability_name']})",
        'fixed chats: ' + ', '.join(f"{chat['title']} ({chat['purpose']})" for chat in manifest['fixed_chats']),
        'agent classes: ' + ', '.join(f"{item['label']} ({item['kind']}, {item['authority']})" for item in manifest['agent_classes']),
        'tile_sets: ' + (', '.join(f"{item['id']} at {item['revision']}, {len(template._tile_sets[item['id']]['trackers'])} trackers"
                                 for item in manifest['tile_sets']) or 'zero'),
        f"recipes: {len(template._jobs)}; sub-agent roles: {len(template._sub_agents)}",
        f"event types: {len(template.vocabulary()['types'])} in {len(template.vocabulary()['groups'])} groups",
        f"skills: {len(template._skills)}; ready-made agents: {len(template._templates)} available, {len(template._retired)} retired",
        'tool families: ' + (', '.join(manifest['tool_families']) or 'zero'),
        'surfaces: ' + (', '.join(f"{surface['title']} ({', '.join(tab['display'] for tab in surface['tabs'])})"
                                  for surface in manifest['surfaces']) or 'zero'),
        'connections expected: ' + (', '.join(item['purpose'] for item in manifest['connections_expected']) or 'zero'),
        'data sources: ' + (', '.join(item['title'] for item in manifest['data_sources']) or 'zero'),
        'flow: ' + ' > '.join(f"{stage['stage']} ({stage['attaches']})" for stage in manifest['flow']),
        f"files read: {len(template.files)}",
    ])


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    name = arguments[0] if arguments else layout.selected_vertical()
    try:
        print(describe(load(Path(name).resolve() if '/' in name else name)))
    except PackError as error:
        print(f'RED · {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
