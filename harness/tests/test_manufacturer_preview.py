"""Saved configurator previews use the snapshot and keep browser effects isolated."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'verticals/manufacturer'
spec = importlib.util.spec_from_file_location('manufacturer_preview_under_test', ROOT / 'manufacturer_preview.py')
preview = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = preview
spec.loader.exec_module(preview)


def fake_fetch(calls):
    def fetch(url, limit):
        calls.append(url)
        if url.endswith('configurator.js'):
            return b'/* actual widget contract */\nwindow.RocotoConfig={"accent":"#123456","base":"/metal-ls/rocoto/","root":"/metal-ls","logo":"/metal-ls/logo.png"};\nwindow.rendererExecuted=true;'
        return b'{}'
    return fetch


def test_snapshot_catalogue_questions_and_colour_replace_baked_configuration(tmp_path):
    calls = []
    doc = {'configuration': {'accent': '#654321', 'questionnaire': {'questions': [{'id': 'door', 'title': {'en': 'Saved question'}}]}}, 'catalogue': {'cats': [], 'forms': [], 'metals': [], 'company': {'en': 'Saved company'}}}
    page, headers = preview.html(tmp_path, 'metal-ls', doc, fetch=fake_fetch(calls))
    assert 'Saved question' in page and 'Saved company' in page
    assert '"accent":"#654321"' in page
    assert 'window.rendererExecuted=true' in page
    assert "connect-src 'none'" in headers['Content-Security-Policy']
    assert "form-action 'none'" in headers['Content-Security-Policy']
    assert headers['Cache-Control'] == 'private, no-store'
    # A second saved revision reuses the same renderer, even if the public page changed.
    other, other_headers = preview.html(tmp_path, 'metal-ls', doc, fetch=lambda *_: pytest.fail('renderer already pinned'))
    assert other_headers['X-Configurator-Renderer'] == headers['X-Configurator-Renderer']
    assert len(calls) == 3


def test_snapshot_text_cannot_close_the_script_or_insert_markup(tmp_path):
    hostile = '</script><img src=x onerror=alert(1)>'
    page, _ = preview.html(tmp_path, 'metal-ls', {'configuration': {'headline': hostile}, 'catalogue': {'cats': []}}, {'headline': hostile}, fetch=fake_fetch([]))
    assert hostile not in page
    assert '\\u003c/script\\u003e' in page


def test_renderer_pin_detects_tampering_and_rejects_foreign_company_paths(tmp_path):
    entry = preview.renderer(tmp_path, 'metal-ls', fetch=fake_fetch([]))
    (tmp_path / 'manufacturer-preview/metal-ls' / (entry['sha256'] + '.js')).write_text('changed')
    with pytest.raises(ValueError, match='verification'):
        preview.renderer(tmp_path, 'metal-ls', fetch=lambda *_: pytest.fail('tampering must fail closed'))
    for slug in ('../metal-m', 'https://example.com', 'metal-ls/../../x'):
        with pytest.raises(ValueError, match='bound company'):
            preview.renderer(tmp_path, slug, fetch=lambda *_: pytest.fail('foreign path'))


def test_browser_adapter_has_only_snapshot_reads_and_parent_scoped_updates():
    script = (ROOT / 'web/manufacturer-preview.js').read_text()
    assert 'event.source !== parent' in script
    assert "relative === 'catalog.json'" in script
    assert 'saved.catalogue' in script
    assert 'nativeFetch' not in script
    assert 'XMLHttpRequest' not in script
