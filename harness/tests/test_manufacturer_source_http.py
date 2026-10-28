"""Scoped remote source projection and fixed-origin transport invariants."""
import json
import sys
from pathlib import Path

import pytest

VERTICAL = Path(__file__).resolve().parents[1] / 'verticals/manufacturer'
sys.path.insert(0, str(VERTICAL))
from manufacturer_source_http import RemoteCompanySource, source_for  # noqa: E402
from manufacturer_store import SourceUnavailable  # noqa: E402


def binding(tmp_path, scope='a' * 64, **extra):
    (tmp_path / 'manufacturer-source.json').write_text(json.dumps({
        'company_id': scope, 'company_slug': 'metal-ls', 'transport': 'orders-api', **extra}))


def test_source_binding_is_exact_and_precedes_network(tmp_path):
    binding(tmp_path)
    assert isinstance(source_for(tmp_path, 'a' * 64), RemoteCompanySource)
    with pytest.raises(SourceUnavailable):
        source_for(tmp_path, 'b' * 64)
    binding(tmp_path, origin='https://attacker.test')
    with pytest.raises(SourceUnavailable):
        source_for(tmp_path, 'a' * 64)


def test_source_request_uses_fixed_company_and_scope(tmp_path, monkeypatch):
    binding(tmp_path)
    monkeypatch.setenv('ROCOTO_MANUFACTURER_ORDERS_TOKEN', 'x' * 40)
    calls = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, maximum):
            return b'{"ok":true,"orders":[]}'
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    assert source_for(tmp_path, 'a' * 64).orders() == []
    request = calls[0]
    assert request.full_url.startswith('https://orders-demo.soyrocoto.com/metal-ls/rocoto/api/platform-state?')
    assert request.get_header('X-rocoto-company') == 'a' * 64
    assert request.get_header('Authorization') == 'Bearer ' + 'x' * 40


def test_source_company_identity_cannot_drift(tmp_path, monkeypatch):
    binding(tmp_path)
    source = source_for(tmp_path, 'a' * 64)
    monkeypatch.setattr(source, '_read', lambda *args, **kwargs: {'document': {'configuration': {'slug': 'other'}}})
    with pytest.raises(SourceUnavailable):
        source.document()


def test_remote_order_projection_preserves_human_reference_and_quote_state(tmp_path, monkeypatch):
    binding(tmp_path)
    source = source_for(tmp_path, 'a' * 64)
    order = {'id': 'a' * 32, 'display_number': '#1001', 'number': '#1001',
        'pricing_state': 'to_quote', 'total': None, 'priced_total': None,
        'customer': {'name': 'Buyer', 'company': '', 'display_company': '', 'company_details_needed': True},
        'lines': [{'code': '101081', 'name': 'Mortise lock 55/72', 'quantity': 6,
                   'pricing_state': 'to_quote', 'unit_price': None, 'total': None}]}
    monkeypatch.setattr(source, '_read', lambda resource, **kwargs: {'order': order, 'orders': [order]})
    assert source.order(order['id']) == source.orders()[0] == order
