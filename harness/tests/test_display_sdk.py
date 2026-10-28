"""A second vertical uses the exact display SDK, with explicit ownership and independent kits."""
import copy
from pathlib import Path
import subprocess
import sys

import pytest

from display_presentation import PresentationKit, Unfilled, extend_kit


def made():
    kit = PresentationKit()
    spec = kit.default_presentation({'label': 'Orders ready', 'canvas': {'family': 'metric'}})
    rows = [{'id': 'order-1', 'description': 'Order ready', 'href': '/orders/1'}]
    points = [{'t': '2026-10-11T12:00:00Z', 'value': 2}, {'t': '2026-10-11T13:00:00Z', 'value': 3}]
    output = {'revision': 4, 'tile': {'value': 3, 'series': points},
              'canvas': {'value': 3, 'series': points, 'summary': 'Three orders ready.', 'rows': rows}}
    return kit, spec, output


def test_bare_display_sdk_imports_and_projects_without_any_vertical_path():
    root = Path(__file__).parents[1]
    code = '''import json, sys
import layout
layout.install('bare')
from display_presentation import PresentationKit
kit = PresentationKit()
assert set(kit.families) == {'metric', 'live', 'occurrences'}
assert not {'shelves', 'ranked'} & kit.shapes.keys()
assert all('/verticals/retail/' not in str(getattr(module, '__file__', '')) for module in sys.modules.values())
print('bare display kit')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'bare display kit'


def test_display_owner_and_output_revision_are_shared_by_tile_and_canvas():
    kit, spec, output = made()
    value = kit.display('orders-ready', 'agent-production', output, spec)
    for part in (value, value['tile'], value['canvas']):
        assert part['owner_agent_id'] == 'agent-production'
        assert part['display_id'] == 'orders-ready'
    assert value['revision'] == 4
    assert value['tile']['value'] == value['canvas']['value'] == 3
    with pytest.raises(ValueError, match='owning agent'):
        kit.display('orders-ready', '', output, spec)


def test_vertical_shapes_and_aliases_are_local_to_their_kit():
    extension = {'shapes': {'orders': 'A list of order numbers.'}}
    first = PresentationKit(extend_kit(extension), shapes={'orders': lambda value: isinstance(value, list)})
    second = PresentationKit()
    first.kit['families']['metric']['name'] = 'Orders'
    assert 'orders' in first.output_fields({}, {'orders': ['one']})['canvas']['orders']
    assert 'orders' not in second.shapes
    assert second.families['metric']['name'] == 'Chart'


def test_saved_revision_rejects_stale_write_and_missing_bound_data():
    kit, spec, output = made()
    prior = {**spec, 'revision': 2}
    change = {'tile': {**spec['tile'], 'label': 'Ready orders'}}
    saved = kit.revise(prior, change, expected_revision=2, output=output, saved_by='agent-production', saved_at='now')
    assert saved['revision'] == 3 and saved['saved_by'] == 'agent-production'
    with pytest.raises(ValueError, match='current revision'):
        kit.revise(saved, change, expected_revision=2, output=output)
    with pytest.raises(Unfilled):
        kit.revise(saved, {'canvas': {'family': 'metric', 'blocks': [
            {'block': 'figure'}, {'block': 'line', 'bind': {'series': 'missing'}}]}}, expected_revision=3, output=output)
    assert prior['revision'] == 2


def test_projection_preserves_host_rows_and_removes_untrusted_links():
    kit, spec, output = made()
    output['canvas']['summary'] = '[Open outside](https://example.test/)'
    output['canvas']['custom_rows'] = [{'id': 'text', 'label': 'Agent words', 'href': 'https://example.test/', 'thumb': '/leak'}]
    custom = copy.deepcopy(spec)
    custom['canvas']['blocks'][-1]['bind']['rows'] = 'custom_rows'
    projected = kit.display('orders-ready', 'agent-production', output, custom)
    assert projected['canvas']['rows'][0]['href'] == '/orders/1'
    assert projected['canvas']['custom_rows'] == [{'id': 'text', 'label': 'Agent words'}]
    assert projected['canvas']['summary'] == 'Open outside'
