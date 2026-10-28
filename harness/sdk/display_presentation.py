"""Shared display kit: bounded presentations, output bindings and safe page projections.

Each vertical supplies data and optional shapes/families. Instances keep their kit and
validators isolated, so two verticals can coexist without changing one another.
Persistence and source authorization belong to the host adapter.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re

BASE_KIT = json.loads(Path(__file__).with_name('presentation_kit.json').read_text())
MISSING = object()
LINK = re.compile(r'\[([^\]\n]*)\]\(([^)\s]*)\)')
SHAPE = 'A presentation is an object with schema, tile and canvas.'
PARTS = 'A presentation has three parts: schema, tile and canvas.'
FIELD_NAME = 'A binding names one field of the output: lowercase letters, digits and underscores, starting with a letter.'
SHARE = ('A ring or an arc shows a share of a whole. Bind fraction to an output field that holds a number from 0 to 1, '
         'or choose the rate format.')
OWN_UNIT = 'The unit is a word of up to {unit} characters for the count format. The money, duration and rate formats write their own unit.'
ONCE = 'Use each block once.'
NAME_A_PART = 'Name the tile or the canvas to change.'


def extend_kit(extension=None):
    """Merge a vertical's authored kit additions over the shared kit, into a fresh document."""
    def merge(base, change):
        for key, value in change.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = deepcopy(value)
        return base
    return merge(deepcopy(BASE_KIT), extension or {})

class Unfilled(ValueError):
    """A presentation the kit accepts and the display's current output leaves unfilled. It reads as
    its one sentence, and carries the binding with the block's own `needs` for a person's screen."""

    def __init__(self, problem):
        super().__init__(problem['sentence'])
        self.problem = problem

def listed(names):
    return ', '.join(names)

def _fields(schema):
    return {'type': 'object', 'additionalProperties': False, **schema}

def build_schema(kit):
    """The JSON Schema of a presentation, built from the kit."""
    limits, field = kit['limits'], {'$ref': '#/$defs/field'}
    marks, formats = kit['tile']['marks'], kit['tile']['formats']
    shares = [name for name, mark in marks.items() if 'fraction' in mark['fields']]
    blocks = []
    for name, block in kit['blocks'].items():
        required = [key for key, shape in block['fields'].items() if not shape.endswith('?')]
        entry = {'block': {'const': name}}
        if block['fields']:
            entry['bind'] = _fields({'required': required, 'properties': {key: field for key in block['fields']}})
        for option, choice in (block.get('options') or {}).items():
            entry[option] = {'enum': choice} if isinstance(choice, list) else {'type': 'string', 'minLength': 1, 'maxLength': limits['title'], 'pattern': r'\S'}
        blocks.append(_fields({'required': ['block'] + (['bind'] if required else []), 'properties': entry}))
    families = []
    for name, family in kit['families'].items():
        families.append({'if': {'properties': {'family': {'const': name}}}, 'then': {'properties': {'blocks': {
            'items': {'properties': {'block': {'enum': family['allows']}}},
            'allOf': [{'contains': {'properties': {'block': {'const': block}}}} for block in family['requires']]}}}})
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': kit['presentation'],
        **_fields({'required': ['schema', 'tile', 'canvas'], 'properties': {
            'schema': {'const': kit['presentation']}, 'tile': {'$ref': '#/$defs/tile'}, 'canvas': {'$ref': '#/$defs/canvas'}}}),
        '$defs': {
            'field': {'type': 'string', 'pattern': limits['field']},
            'tile': {**_fields({'required': ['label', 'mark', 'format'], 'properties': {
                'label': {'type': 'string', 'minLength': 1, 'maxLength': limits['label'], 'pattern': r'\S'},
                'mark': {'enum': list(marks)}, 'format': {'enum': list(formats)},
                'unit': {'type': 'string', 'maxLength': limits['unit']},
                'bind': _fields({'properties': {'series': {'enum': ['series', 'trend']}, 'fraction': field}})}}),
                'allOf': [
                    {'if': {'required': ['format'], 'properties': {'format': {'enum': [name for name in formats if name != 'count']}}},
                     'then': {'properties': {'unit': False}}},
                    {'if': {'required': ['mark', 'format'], 'properties': {'mark': {'enum': shares}, 'format': {'enum': [name for name in formats if name != 'rate']}}},
                     'then': {'required': ['bind'], 'properties': {'bind': {'required': ['fraction']}}}}]},
            'block': {'type': 'object', 'required': ['block'], 'oneOf': blocks},
            'canvas': {**_fields({'required': ['family', 'blocks'], 'properties': {
                'family': {'enum': list(kit['families'])},
                'blocks': {'type': 'array', 'minItems': 1, 'maxItems': limits['blocks'], 'items': {'$ref': '#/$defs/block'}}}}),
                'allOf': families}}}

def _number(value):
    return type(value) in (int, float) and math.isfinite(value)

def _text(value):
    return isinstance(value, str)

def _records(value, each, least=0, most=None):
    return (isinstance(value, list) and len(value) >= least and (most is None or len(value) <= most)
            and all(isinstance(item, dict) and each(item) for item in value))

def _row(item):
    return _text(item.get('id')) and (_text(item.get('description')) or _text(item.get('label')))

def holds(value):
    """What a field holds, in words a sentence can carry."""
    if value is MISSING:
        return 'a name the output leaves out'
    if value is None:
        return 'an empty value'
    if isinstance(value, bool):
        return 'true or false'
    if _number(value):
        return 'a number'
    if isinstance(value, str):
        return 'text'
    if isinstance(value, list):
        return f"a list of {len(value)} {'entry' if len(value) == 1 else 'entries'}"
    return 'an object'

def bound(document, name, shape):
    """The value a binding reads: the output's field of that name; for text, also a written text slot."""
    if isinstance(document, dict) and name in document:
        return document[name]
    if shape == 'text' and isinstance(document, dict):
        for slot in document.get('texts') or []:
            if isinstance(slot, dict) and slot.get('slot') == name:
                return slot.get('text') or ''
    return MISSING

def short(text, most):
    """Text within `most` characters, ending on its last whole word there."""
    text = ' '.join(str(text or '').split())
    if len(text) <= most:
        return text
    head = text[:most + 1]
    return (head.rsplit(' ', 1)[0] if ' ' in head else text[:most]).rstrip(' ,;:·-') or text[:most]

def inert(text):
    """Text a script wrote, as the page draws it: a link's own words stay and its address goes."""
    return LINK.sub(lambda match: match.group(1) or match.group(2), text) if isinstance(text, str) else text

def _inert_field(value):
    if isinstance(value, str):
        return inert(value)
    if isinstance(value, list):
        return [_inert_field(item) for item in value]
    if isinstance(value, dict):
        return {key: (_inert_field(item) if key in {'text', 'recommendation', 'summary'} else item) for key, item in value.items()}
    return value

def _own_list(value, fields):
    """A list a script wrote, as the kit's own fields of each entry: text, numbers and times."""
    scalar = lambda item: item is None or isinstance(item, (str, int, float, bool))
    return [{key: item[key] for key in fields if key in item and scalar(item[key])} for item in value if isinstance(item, dict)]

BASE_FITS = {
    'number': _number, 'text': _text,
    'texts': lambda value: isinstance(value, list) and len(value) <= 5 and all(_text(item) for item in value),
    'share': lambda value: _number(value) and 0 <= value <= 1,
    'points': lambda value: _records(value, lambda p: _text(p.get('t')) and _number(p.get('value')), least=2),
    'bands': lambda value: _records(value, lambda b: (_text(b.get('label')) or _text(b.get('band'))) and _number(b.get('value')), least=1, most=12),
    'rows': lambda value: _records(value, _row),
    'timed_rows': lambda value: _records(value, lambda r: _row(r) and _text(r.get('at')) and bool(r['at'])),
    'comments': lambda value: _records(value, lambda c: 'text' in c and (c['text'] is None or _text(c['text']))),
}


class PresentationKit:
    """One instance-isolated display vocabulary and its output policy."""
    def __init__(self, kit=None, *, shapes=None, aliases=None, envelope=(), trusted_lists=('rows',), row_fields=None, views=None, default_label="Display"):
        self.kit = deepcopy(kit if kit is not None else BASE_KIT)
        self.schema_id = self.kit['presentation']
        self.limits, self.families, self.blocks, self.shapes = (self.kit[key] for key in ('limits', 'families', 'blocks', 'shapes'))
        self.marks, self.formats = self.kit['tile']['marks'], self.kit['tile']['formats']
        self.field = re.compile(self.limits['field'])
        self.fits = {**BASE_FITS, **(shapes or {})}
        self.recipe_families = {name: name for name in self.families} | dict(aliases or {})
        self.envelope = frozenset({'id', 'display_id', 'owner_agent_id', 'owner_principal_id', 'family', 'state', 'needs', 'period', 'periods', 'revision', 'presentation', 'problems', *envelope})
        self.host_lists = frozenset(trusted_lists)
        self.own_fields = {'rows': ('id', 'at', 'label', 'description', 'at_s'), 'timed_rows': ('id', 'at', 'label', 'description', 'at_s')} | dict(row_fields or {})
        self.views = dict(views or {})
        self.default_label = default_label
        self.schema = build_schema(self.kit)
        self.too_large = f"A presentation holds up to {self.limits['bytes']:,} bytes: one tile and up to {self.limits['blocks']} blocks."


    def output_fields(self, tile, canvas):
        """The fields of a display's output a presentation can bind, each with the kit shapes it fits."""
        fits = lambda value: [name for name, check in self.fits.items() if check(value)]
        return {'canvas': {name: fits(value) for name, value in sorted((canvas or {}).items()) if name not in self.envelope and name != 'texts'},
                'tile': {name: fits((tile or {}).get(name)) for name in ('series', 'trend') if name in (tile or {})},
                'slots': [slot['slot'] for slot in (canvas or {}).get('texts') or [] if isinstance(slot, dict) and _text(slot.get('slot'))]}

    def _problem(self, block, field, name, shape, needs, document, value, *, fixed=False):
        owner = 'tile' if block == 'tile' else f'{block} block'
        if fixed:
            sentence = f"The {owner} reads the output’s {field}, which is {holds(value)}. {needs}"
        else:
            fitting = [key for key, item in sorted(document.items()) if key not in self.envelope and self.fits[shape](item)]
            if shape == 'text':
                fitting += [slot['slot'] for slot in document.get('texts') or [] if isinstance(slot, dict) and _text(slot.get('slot'))]
            offer = f"Output fields that fit: {listed(fitting)}." if fitting else f"It reads: {self.shapes[shape]}"
            sentence = f"The {owner} binds {field} to «{name}», which is {holds(value)}. {needs} {offer}"
        return {'block': block, 'field': field, 'bound_to': name, 'shape': shape, 'needs': needs, 'sentence': sentence}

    def check_output(self, spec, tile, canvas):
        """Each binding of a presentation that the output leaves unfilled: [{block, field, bound_to, shape, needs, sentence}].

        A display that still needs its source has an output without data; its presentation is
        checked once the first numbers arrive.
        """
        tile, canvas = tile or {}, canvas or {}
        if tile.get('state') == 'needs':
            return []
        problems = []
        face, mark = spec['tile'], self.marks[spec['tile']['mark']]
        bind = face.get('bind') or {}
        if 'series' in mark['fields']:
            name = bind.get('series', 'series')
            if not self.fits['points'](tile.get(name)):
                problems.append(self._problem('tile', 'series', name, 'points', mark['needs'], tile, tile.get(name, MISSING)))
        elif 'fraction' in bind:
            value = bound(canvas, bind['fraction'], 'share')
            if not self.fits['share'](value):
                problems.append(self._problem('tile', 'fraction', bind['fraction'], 'share', mark['needs'], canvas, value))
        for entry in spec['canvas']['blocks']:
            block = self.blocks[entry['block']]
            for field, shape in (block.get('reads') or {}).items():
                if shape in self.fits and not self.fits[shape](canvas.get(field)):
                    problems.append(self._problem(entry['block'], field, field, shape, block['needs'], canvas, canvas.get(field, MISSING), fixed=True))
            for field, shape in block['fields'].items():
                name = (entry.get('bind') or {}).get(field)
                if name is None:
                    continue
                shape = shape.rstrip('?')
                value = bound(canvas, name, shape)
                if not self.fits[shape](value):
                    problems.append(self._problem(entry['block'], field, name, shape, block['needs'], canvas, value))
        return problems

    def family_blocks(self, family, canvas=None):
        """A family's own blocks. With an output at hand, the ones its fields can fill join them."""
        blocks = [dict(block, **({'bind': dict(block['bind'])} if 'bind' in block else {})) for block in self.families[family]['default']]
        if not isinstance(canvas, dict):
            return blocks
        allows = self.families[family]['allows']
        at = lambda name: next((index for index, block in enumerate(blocks) if block['block'] == name), None)
        rail = at(self.families[family]['rail'])
        if 'key_points' in allows and self.fits['texts'](canvas.get('key_points')) and canvas['key_points']:
            blocks.insert(len(blocks) if rail is None else rail, {'block': 'key_points', 'bind': {'items': 'key_points'}})
        if 'recommendation' in allows and _text(canvas.get('recommendation')) and canvas['recommendation'].strip():
            after, rail = at('summary'), at(self.families[family]['rail'])
            blocks.insert(after + 1 if after is not None else len(blocks) if rail is None else rail, {'block': 'recommendation', 'bind': {'text': 'recommendation'}})
        if family == 'live':
            comments = next((name for name in ('memos', 'comments', 'texts') if canvas.get(name) and self.fits['comments'](canvas.get(name))), None)
            if comments:
                blocks[-1]['bind']['comments'] = comments
        return blocks

    def default_presentation(self, display, output=None):
        """The presentation a display has before one is saved: its label, a line of its series, a
        count, and the canvas family its definition names."""
        family = self.recipe_families.get((display.get('canvas') or {}).get('family'), 'occurrences')
        label = short(display.get('label'), self.limits['label']) or self.default_label
        return {'schema': self.schema_id, 'tile': {'label': label, 'mark': 'line', 'format': 'count', 'bind': {'series': 'series'}},
                'canvas': {'family': family, 'blocks': self.family_blocks(family, (output or {}).get('canvas'))}}

    def merge_presentation(self, saved, change, output=None):
        """A saved presentation with a change of its tile, its canvas or both. A part replaces whole.
        A canvas named by its family alone takes that family's own blocks."""
        if not isinstance(change, dict) or not {'tile', 'canvas'} & set(change):
            raise ValueError(NAME_A_PART)
        tile, canvas = change.get('tile', saved['tile']), change.get('canvas', saved['canvas'])
        if isinstance(canvas, dict) and 'blocks' not in canvas and isinstance(canvas.get('family'), str) and canvas['family'] in self.families:
            canvas = {**canvas, 'blocks': self.family_blocks(canvas['family'], (output or {}).get('canvas'))}
        return {'schema': self.schema_id, 'tile': tile, 'canvas': canvas}

    def _named(self, value, most=40):
        return f'«{value[:most]}»' if isinstance(value, str) and value.strip() else 'That name'

    def validate_presentation(self, spec, *, output=None, parts=('tile', 'canvas')):
        """A presentation that the kit and, with `output`, the display's current output can draw.

        Returns the document with its label tidied. A document outside the kit answers ValueError with
        one sentence that says what to choose. With `output`, each binding of the named `parts` is
        read from it, and one the output leaves unfilled answers `Unfilled`: a change of the canvas is
        held to the canvas, so a person's pick stays open while the tile waits for its field.
        """
        try:
            size = len(json.dumps(spec, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
        except (TypeError, ValueError):
            raise ValueError(SHAPE) from None
        if not isinstance(spec, dict):
            raise ValueError(SHAPE)
        if size > self.limits['bytes']:
            raise ValueError(self.too_large)
        if set(spec) != {'schema', 'tile', 'canvas'}:
            raise ValueError(PARTS)
        if spec['schema'] != self.schema_id:
            raise ValueError(f'Set schema to {self.schema_id}.')
        tile, canvas = spec['tile'], spec['canvas']
        if not isinstance(tile, dict) or not set(tile) <= {'label', 'mark', 'format', 'unit', 'bind'}:
            raise ValueError('A tile has a label, a mark, a format, a unit for a count, and bind.')
        label = ' '.join(tile['label'].split()) if isinstance(tile.get('label'), str) else ''
        if not 1 <= len(label) <= self.limits['label']:
            raise ValueError(f"Give the tile a label of 1 to {self.limits['label']} characters.")
        if not isinstance(tile.get('mark'), str) or tile['mark'] not in self.marks:
            raise ValueError(f'Choose the tile mark from the kit: {listed(self.marks)}.')
        if not isinstance(tile.get('format'), str) or tile['format'] not in self.formats:
            raise ValueError(f'Choose the number format from the kit: {listed(self.formats)}.')
        if 'unit' in tile and (tile['format'] != 'count' or not isinstance(tile['unit'], str) or len(tile['unit']) > self.limits['unit']):
            raise ValueError(OWN_UNIT.format(unit=self.limits['unit']))
        bind = tile.get('bind', {})
        if not isinstance(bind, dict) or not set(bind) <= {'series', 'fraction'}:
            raise ValueError('A tile binds series for a line or bars, and fraction for a ring or an arc.')
        if 'series' in bind and bind['series'] not in ('series', 'trend'):
            raise ValueError('Bind the tile series to series, the buckets of the period, or to trend, the last 30 days.')
        if 'fraction' in bind and (not isinstance(bind['fraction'], str) or not self.field.fullmatch(bind['fraction'])):
            raise ValueError(FIELD_NAME)
        if 'fraction' in self.marks[tile['mark']]['fields'] and tile['format'] != 'rate' and 'fraction' not in bind:
            raise ValueError(SHARE)
        if not isinstance(canvas, dict) or set(canvas) != {'family', 'blocks'}:
            raise ValueError('A canvas has a family and its blocks.')
        if not isinstance(canvas['family'], str) or canvas['family'] not in self.families:
            raise ValueError(f'Choose the canvas family from the kit: {listed(self.families)}.')
        family, blocks = self.families[canvas['family']], canvas['blocks']
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= self.limits['blocks']:
            raise ValueError(f"Give the canvas 1 to {self.limits['blocks']} blocks.")
        seen = set()
        for entry in blocks:
            name = entry.get('block') if isinstance(entry, dict) else None
            if not isinstance(name, str) or name not in self.blocks:
                raise ValueError(f'{self._named(name)} is outside the kit. Choose each block from the kit: {listed(self.blocks)}.')
            block = self.blocks[name]
            if name not in family['allows']:
                raise ValueError(f"The {canvas['family']} canvas takes these blocks: {listed(family['allows'])}.")
            if name in seen:
                raise ValueError(ONCE)
            seen.add(name)
            options = block.get('options') or {}
            if not set(entry) <= {'block', 'bind', *options}:
                takes = ['bind'] * bool(block['fields']) + list(options)
                raise ValueError(f"The {name} block takes {listed(takes)}." if takes else f'The {name} block stands by its name alone.')
            given = entry.get('bind', {})
            required = [key for key, shape in block['fields'].items() if not shape.endswith('?')]
            if not isinstance(given, dict) or not set(given) <= set(block['fields']) or not set(required) <= set(given):
                if not block['fields']:
                    raise ValueError(f'The {name} block stands by its name alone.')
                raise ValueError(f"The {name} block binds {listed(block['fields'])}. {block['needs']}")
            if any(not isinstance(value, str) or not self.field.fullmatch(value) for value in given.values()):
                raise ValueError(FIELD_NAME)
            for option, choice in options.items():
                if option not in entry:
                    continue
                if isinstance(choice, list) and entry[option] not in choice:
                    raise ValueError(f'Choose the {name} block’s {option} from: {listed(choice)}.')
                if not isinstance(choice, list) and (not isinstance(entry[option], str) or not 1 <= len(entry[option].strip()) <= self.limits['title']
                                                   or len(entry[option]) > self.limits['title']):
                    raise ValueError(f"Give the {name} block a {option} of 1 to {self.limits['title']} characters.")
        lacking = [name for name in family['requires'] if name not in seen]
        if lacking:
            raise ValueError(f"The {canvas['family']} canvas keeps its {' and '.join(family['requires'])} "
                             f"block{'s' if len(family['requires']) > 1 else ''}. {family['needs']}")
        from jsonschema import Draft202012Validator
        fault = next(iter(sorted(Draft202012Validator(self.schema).iter_errors(spec), key=lambda error: list(map(str, error.absolute_path)))), None)
        if fault is not None:
            raise ValueError('Use the fields of the presentation schema at ' + ('/'.join(map(str, fault.absolute_path)) or 'its top level') + '.')
        spec = {'schema': self.schema_id, 'tile': {**tile, 'label': label}, 'canvas': canvas}
        if output is not None:
            problems = [problem for problem in self.check_output(spec, output.get('tile'), output.get('canvas'))
                        if ('tile' if problem['block'] == 'tile' else 'canvas') in parts]
            if problems:
                raise Unfilled(problems[0])
        return spec

    def families_for(self, output):
        """The kit's families for one output: each with its sentence, and whether the output's fields
        fill the family's own blocks."""
        listing = []
        for name, family in self.families.items():
            blocks = self.family_blocks(name, (output or {}).get('canvas'))
            entry = {'id': name, 'name': family['name'], 'needs': family['needs'], 'blocks': blocks}
            if output is not None:
                # The canvas alone decides: a rate ring binds nothing of the tile.
                spec = {'tile': {'mark': 'ring', 'format': 'rate'}, 'canvas': {'family': name, 'blocks': blocks}}
                entry['available'] = (output.get('tile') or {}).get('state') != 'needs' and not self.check_output(spec, output.get('tile'), output.get('canvas'))
            listing.append(entry)
        return listing

    def _unit(self, reading, spec):
        """The unit a reading carries on the page. A rate above 1 is the percentage itself: it reads
        with `%`, whatever unit came with it. A rate from 0 to 1 is a ratio and reads as one, unless
        its script published it with `%`."""
        face, value = spec['tile'], reading.get('value')
        if face['format'] == 'rate':
            return '%' if reading.get('unit') == '%' or _number(value) and value > 1 else None
        if face['format'] == 'count' and 'unit' in face:
            return face['unit']
        unit = reading.get('unit')
        # A count's unit stands beside the number on a tile: a longer one the script published stays with the output.
        return None if face['format'] == 'count' and isinstance(unit, str) and len(unit) > self.limits['unit'] else unit

    def _fraction(self, canvas, spec):
        name = (spec['tile'].get('bind') or {}).get('fraction')
        value = bound(canvas or {}, name, 'share') if name else MISSING
        return max(0, min(1, value)) if _number(value) else None

    def _tile_problems(self, reading, canvas, spec, series=None):
        """Each binding of the tile that this reading leaves unfilled, as the page carries it."""
        face = spec['tile'] if series is None else {**spec['tile'], 'bind': {**(spec['tile'].get('bind') or {}), 'series': series}}
        found = self.check_output({'tile': face, 'canvas': {'blocks': []}}, reading, canvas)
        return [{k: problem[k] for k in ('block', 'field', 'needs', 'sentence')} for problem in found]

    def canvas_for_page(self, canvas, spec):
        """A canvas as the page reads it: the published fields, the text a script wrote made inert, a
        list a script wrote whole cut to the kit's own fields, the presentation's family and blocks,
        and each binding the output leaves unfilled."""
        page = dict(canvas)
        names = {'summary', 'recommendation', 'key_points', 'memos', 'comments', 'texts'}
        for entry in spec['canvas']['blocks']:
            for field, shape in self.blocks[entry['block']]['fields'].items():
                name, shape = (entry.get('bind') or {}).get(field), shape.rstrip('?')
                if not name:
                    continue
                if shape in {'text', 'texts', 'comments'}:
                    names.add(name)
                # A picture, a clip and a link reach the page on the rows the host rebuilt from its own records.
                elif shape in self.own_fields and name not in self.host_lists and self.fits[shape](canvas.get(name)):
                    page[name] = _own_list(canvas[name], self.own_fields[shape])
        for name in names:
            if name in page:
                page[name] = _inert_field(page[name])
        page['presentation'] = {'family': spec['canvas']['family'], 'blocks': spec['canvas']['blocks'], 'revision': spec.get('revision', 0)}
        page['problems'] = [{k: problem[k] for k in ('block', 'field', 'needs', 'sentence')} for problem in
                            self.check_output(spec, {'state': canvas.get('state'), 'series': canvas.get('series'), 'trend': canvas.get('history')}, canvas)
                            if problem['block'] != 'tile']
        return page

    def apply_to_tile(self, row, spec, canvas=None, *, agent_id=None):
        """A company display's tile row with its presentation applied: the names the workspace reads.

        `kind` is the number format, `mark` the mark, `view` the full view the the workspace sheet draws for it.
        `problems` names each binding of the tile the output leaves unfilled, so the page rests the
        mark and says what it needs. A reading of another period carries the same unit rule, its own
        share, its own problems and its own canvas with the presentation.
        """
        face = spec['tile']
        bind = face.get('bind') or {}
        page = {**row, 'label': face['label'], 'unit': self._unit(row, spec), 'kind': face['format'], 'mark': face['mark'],
                'view': self.views.get(spec['canvas']['family'], 'rate' if face['format'] == 'rate' else 'count'),
                'presentation_revision': spec.get('revision', 0), 'problems': self._tile_problems(row, canvas, spec)}
        if isinstance(page.get('summary'), str):
            page['summary'] = inert(page['summary'])
        if bind.get('series') == 'trend' and isinstance(row.get('trend'), list):
            page['series'] = row['trend']
        fraction = self._fraction(canvas, spec)
        if fraction is not None:
            page['fraction'] = fraction
        if agent_id:
            page['agent_id'] = agent_id
        if isinstance(row.get('periods'), dict):
            periods = {}
            for name, reading in row['periods'].items():
                # A period's reading draws its own series, whichever of the day's two the tile binds.
                part = {**reading, 'unit': self._unit(reading, spec), 'problems': self._tile_problems(reading, reading.get('canvas'), spec, 'series')}
                if isinstance(part.get('summary'), str):
                    part['summary'] = inert(part['summary'])
                if isinstance(reading.get('canvas'), dict):
                    share = self._fraction(reading['canvas'], spec)
                    if share is not None:
                        part['fraction'] = share
                    part['canvas'] = self.canvas_for_page(reading['canvas'], spec)
                periods[name] = part
            page['periods'] = periods
        return page

    def revise(self, current, change, *, output=None, expected_revision=None, saved_by='harness', saved_at=None):
        """Validate a change and advance its revision inside the adapter's write transaction."""
        revision = current.get('revision', 0)
        if expected_revision != revision and (revision or expected_revision is not None):
            raise ValueError('Read the presentation again, then save with its current revision.')
        parts = tuple(part for part in ('tile', 'canvas') if isinstance(change, dict) and part in change)
        spec = self.validate_presentation(self.merge_presentation(current, change, output), output=output, parts=parts)
        return {**spec, 'revision': revision + 1, 'saved_by': saved_by, 'saved_at': saved_at}

    def display(self, display_id, owner_agent_id, output, presentation):
        """Project an admitted output, with one explicit owning agent, into its tile and canvas.

        The caller's host adapter performs source and agent read admission before this pure step.
        """
        for value in (display_id, owner_agent_id):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', value):
                raise ValueError('Give the display and its owning agent their registered identifiers.')
        spec = self.validate_presentation({key: presentation[key] for key in ('schema', 'tile', 'canvas')})
        spec['revision'] = presentation.get('revision', 0)
        flags = {'display_id': display_id, 'owner_agent_id': owner_agent_id}
        tile = self.apply_to_tile({**output['tile'], **flags}, spec, output['canvas'], agent_id=owner_agent_id)
        canvas = self.canvas_for_page({**output['canvas'], **flags}, spec)
        return {**flags, 'revision': output.get('revision', 0), 'presentation': spec, 'tile': tile, 'canvas': canvas}
