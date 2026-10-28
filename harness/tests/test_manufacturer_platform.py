"""Manufacturer company state and source boundaries, over the real CompanyStore schema."""
# ruff: noqa: E402 - Install the selected vertical before importing its modules.
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

VERTICAL = Path(__file__).resolve().parents[1] / 'verticals/manufacturer'
sys.path.insert(0, str(VERTICAL))
import manufacturer_tools
from manufacturer_host import ManufacturerWorkspace
from manufacturer_store import (
    CompanySource,
    Conflict,
    Invalid,
    ManufacturerStore,
    SourceUnavailable,
)
from tool_families import ToolContext, ToolRegistry


def company_store(path):
    """Build the actual public order writer, without opening shared state."""
    source = Path(__file__).resolve().parents[2] / 'app/src/prototypes/orders/store.py'
    spec = importlib.util.spec_from_file_location('manufacturer_source_contract', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CompanyStore(path)


def source_db(path, slug='metal-ls'):
    store = company_store(path)
    config = {'slug': slug, 'name': {'en': 'Metal AD'}, 'source': 'https://metal-ls.com/'}
    store.db.execute('INSERT INTO company VALUES(?,?,?)', (slug, json.dumps(config), '{}'))
    store.db.execute('INSERT INTO customers VALUES(?,?)', ('c1', json.dumps({'name': 'Buyer', 'company': 'Example Works', 'email': 'buyer@example.com', 'website': 'https://example.com'})))
    store.db.execute('INSERT INTO orders VALUES(?,?,?,?,?)', ('o1', 'c1', 'requested', json.dumps({'gross': 120, 'currency': 'EUR', 'fulfilment': 'delivery'}), '2026-10-14T08:00:00Z'))
    store.db.execute('INSERT INTO order_lines(order_id,item_id,quantity,data) VALUES(?,?,?,?)', ('o1', None, 4, json.dumps({'item_id': None, 'quantity': 4, 'custom': 'Door set', 'gross': 120, 'confirmation': False})))
    store.db.close()


def bind(state, database, company='workspace-a'):
    state.mkdir(exist_ok=True)
    (state / 'manufacturer-source.json').write_text(json.dumps({'company_id': company, 'company_slug': 'metal-ls', 'database': str(database)}))


def test_real_order_reader_keeps_source_bytes_and_schema_unchanged(tmp_path):
    database = tmp_path / 'source.sqlite'
    source_db(database)
    state = tmp_path / 'workspace'
    bind(state, database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    source = CompanySource(state, 'workspace-a')
    orders = source.orders()
    assert orders[0]['display_number'] == '#L1001'
    assert orders[0]['customer']['company'] == 'Example Works'
    assert orders[0]['items_count'] == 1
    assert orders[0]['total'] == 120
    assert orders[0]['fulfilment'] == 'delivery'
    assert source.order('o1')['lines'][0]['quantity'] == 4
    assert source.company()['domain'] == 'metal-ls.com'
    assert source.orders(offset=1) == []
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'manufacturer_%'").fetchall() == []


def test_unbound_source_is_empty_and_binding_cannot_cross_company(tmp_path):
    source = CompanySource(tmp_path, 'workspace-a')
    assert source.orders() == []
    assert source.status()['status'] == 'unbound'
    database = tmp_path / 'source.sqlite'
    source_db(database)
    bind(tmp_path, database, 'workspace-b')
    with pytest.raises(SourceUnavailable):
        source.orders()
    bind(tmp_path, database)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE company SET id='other-company'")
    with pytest.raises(SourceUnavailable):
        source.orders()


def test_settings_revision_and_owned_draft_evidence(tmp_path):
    store = ManufacturerStore(tmp_path, 'workspace-a')
    first = store.configurator()
    saved = store.save_configurator({'headline': 'Choose your door hardware'}, first['revision'], 'owner', 'Owner requested a new headline', lambda: True)
    assert saved['revision'] == first['revision'] + 1
    assert saved['state'] == 'draft' and saved['published'] is False
    assert saved['settings']['headline'] == 'Choose your door hardware'
    assert saved['settings']['preview_url'].startswith('https://orders-demo.soyrocoto.com/metal-ls/')
    with pytest.raises(Conflict):
        store.save_configurator({'headline': 'Stale'}, first['revision'], 'owner', 'Stale form', lambda: True)
    assert ManufacturerStore(tmp_path, 'workspace-b').configurator()['settings']['headline'] != 'Choose your door hardware'
    with store.connect() as db:
        audit = db.execute('SELECT actor,evidence FROM manufacturer_audit WHERE company_id=?', ('workspace-a',)).fetchone()
        assert tuple(audit) == ('owner', 'Owner requested a new headline')


@pytest.mark.parametrize('settings', [{'accent': 'red; background:url(https://example.com)'}, {'headline': '<script>bad</script>'}, {'preview_url': 'https://example.com/'}, {'company_slug': 'other'}, {'button_label': '\nClick'}])
def test_settings_reject_executable_or_external_configuration(tmp_path, settings):
    store = ManufacturerStore(tmp_path, 'workspace-a')
    with pytest.raises(Invalid):
        store.save_configurator(settings, 0, 'owner', 'Requested settings', lambda: True)


def extension(tmp_path):
    people = {'alice': {'id': 'alice', 'role': 'owner', 'display_name': 'Alice'}, 'bob': {'id': 'bob', 'role': 'operator'}, 'reader': {'id': 'reader', 'role': 'browse'}}
    scope = SimpleNamespace(company_id='workspace-a', member=lambda actor: people.get(actor), members=lambda: list(people.values()))
    selected = {'state': tmp_path, 'business_scope': scope, 'collections': SimpleNamespace(contains=lambda collection, actor: collection == 'company')}
    ext = ManufacturerWorkspace(selected)
    return ext, people


def test_owner_only_invitation_drafts_and_revocation(tmp_path):
    ext, people = extension(tmp_path)
    with pytest.raises(ValueError):
        ext.update_team('bob', {'action': 'invite', 'email': 'new@metal-ls.com', 'role': 'operator', 'expected_revision': 0}, 'Team form')
    result = ext.update_team('alice', {'action': 'invite', 'email': 'new@metal-ls.com', 'role': 'operator', 'expected_revision': 0}, 'Team form')
    assert result['invitations'][0]['state'] == 'draft'
    assert len(people) == 3
    assert result['actor']['can_manage_team'] is True
    admission = ext.read_admission('alice', 'company')
    del people['alice']
    assert not admission.current()
    with pytest.raises(ValueError):
        ext.overview('alice')


def test_write_rechecks_authority_inside_transaction(tmp_path):
    store = ManufacturerStore(tmp_path, 'workspace-a')
    with pytest.raises(ValueError):
        store.save_configurator({'headline': 'Blocked'}, 0, 'owner', 'Revoked during update', lambda: False)
    assert store.configurator()['revision'] == 0


def test_tool_context_rejects_foreign_workspace_and_revoked_actor(tmp_path):
    ext, people = extension(tmp_path)
    ctx = ToolContext(state=tmp_path, company_id='workspace-a', actor='alice', data_owner='alice', collection='company', extension=ext, admission=ext.read_admission('alice', 'company'))
    registry = ToolRegistry([manufacturer_tools])
    assert (asyncio.run(registry.invoke(ctx, 'manufacturer_orders', {})))['orders'] == []
    with pytest.raises(ValueError):
        asyncio.run(registry.invoke(ctx, 'manufacturer_orders', {'company_id': 'another'}))
    foreign = ToolContext(state=tmp_path, company_id='another', actor='alice', data_owner='alice', collection='company', extension=ext, admission=ctx.admission)
    with pytest.raises(ValueError):
        asyncio.run(registry.invoke(foreign, 'manufacturer_orders', {}))
    del people['alice']
    with pytest.raises(ValueError):
        asyncio.run(registry.invoke(ctx, 'manufacturer_orders', {}))


def test_manual_and_agent_edits_share_exact_immutable_history(tmp_path):
    ext, _ = extension(tmp_path)
    original = ext.configurator('alice')
    document = original['document']
    document['configuration']['questionnaire'] = {'id': 'quote', 'questions': [{'id': 'door', 'kind': 'single', 'title': {'en': 'Door type'}, 'options': [{'id': 'entry', 'label': {'en': 'Entrance'}}]}]}
    document['catalogue']['cats'] = [{'code': 'locks', 'items': [{'c': '101353R', 'p': 12.5, 'u': 'pc'}]}]
    manual = ext.save_configurator('alice', {'document': document, 'settings': {'headline': 'Complete door quote'}, 'expected_revision': 0, 'label': 'Catalogue and questions'}, 'Supplier price file and owner request')
    changed = manual['document']
    changed['catalogue']['cats'][0]['items'][0]['p'] = 15
    agent = ext.save_configurator('alice', {'document': changed, 'expected_revision': 1, 'label': 'New prices'}, 'Supplier price file revision 2', actor_type='agent')
    restored = ext.restore('alice', manual['version_id'], agent['revision'], 'Restore reviewed catalogue')
    old = ext.version('alice', manual['version_id'])['version']
    new = ext.version('alice', restored['version_id'])['version']
    assert old['document'] == new['document']
    assert old['sha256'] == new['sha256']
    assert restored['revision'] == 3
    assert old['document']['configuration']['headline'] == 'Complete door quote'
    versions = ext.versions('alice')['versions']
    assert [row['revision'] for row in versions] == [3, 2, 1, 0]
    assert [row['author']['type'] for row in versions] == ['user', 'agent', 'user', 'source']
    assert versions[0]['restored_from'] == manual['version_id']
    assert ext.version('alice', original['version_id'])['version']['document']['catalogue']['cats'] == []
    foreign = ManufacturerStore(tmp_path, 'workspace-b')
    with pytest.raises(ValueError):
        foreign.version(manual['version_id'])


@pytest.mark.parametrize('mutation', [
    lambda d: d['configuration'].update(questionnaire={'questions': [{'id': 'same'}, {'id': 'same'}]}),
    lambda d: d['configuration'].update(accent='url(https://example.com)'),
    lambda d: d['catalogue'].update(prices=[{'amount': float('nan')}]),
    lambda d: d['catalogue'].update(prices=[{'amount': -1}]),
    lambda d: d['configuration'].update(assets=[{'url': 'javascript:alert(1)'}]),
    lambda d: d['configuration'].update(script='alert(1)'),
    lambda d: d['configuration'].update(slug='other-company'),
])
def test_complete_documents_validate_questionnaires_prices_assets_and_scope(tmp_path, mutation):
    store = ManufacturerStore(tmp_path, 'workspace-a')
    document = store.configurator()['document']
    mutation(document)
    with pytest.raises(Invalid):
        store.save_document(document, 0, 'alice', 'Test source', lambda: True)
    assert store.configurator()['revision'] == 0


def test_native_attachment_must_resolve_for_current_actor(tmp_path):
    ext, _ = extension(tmp_path)
    value = ext.configurator('alice')
    value['document']['attachments'] = [{'file_id': 'file-' + 'a' * 32, 'name': 'Price list.pdf'}]
    with pytest.raises(Invalid):
        ext.save_configurator('alice', {'document': value['document'], 'expected_revision': 0}, 'Uploaded prices')
    assert ext.configurator('alice')['revision'] == 0


def test_native_http_membership_history_and_cross_company_scope(tmp_path):
    import os
    import subprocess
    script = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import layout
layout.install('manufacturer')
import server
from fastapi.testclient import TestClient
state=Path(sys.argv[2])
app=server.create_app(state)
app.state.access.create_environment('second','Second company')
with TestClient(app) as client:
    assert client.get('/api/manufacturer/overview').status_code==401
    assert client.get('/static/manufacturer/manufacturer-tabs.js').status_code==200
    def login(environment,person,owner=False):
        code=app.state.access.issue(environment,principal_id=person,owner=owner)
        response=client.post('/api/session',json={'code':code},headers={'X-Rocoto-Page':'1'})
        assert response.status_code==200,response.text
    login('demo','alice',True)
    current=client.get('/api/manufacturer/configurator').json()
    assert current['revision']==0,current
    response=client.post('/api/manufacturer/configurator',json={'settings':{'headline':'A complete quote'},'expected_revision':0},headers={'X-Rocoto-Page':'1'})
    assert response.status_code==200,response.text
    saved=response.json()
    assert saved['revision']==1
    response=client.post('/api/manufacturer/configurator',json={'settings':{'headline':'Stale'},'expected_revision':0},headers={'X-Rocoto-Page':'1'})
    assert response.status_code==409,response.text
    response=client.post('/api/manufacturer/configurator/versions/'+current['version_id']+'/restore',json={'expected_revision':1},headers={'X-Rocoto-Page':'1'})
    assert response.status_code==200,response.text
    assert response.json()['document']==current['document']
    assert len(client.get('/api/manufacturer/configurator/versions').json()['versions'])==3
    selected=app.state.workspaces['demo']
    draft=selected['vertical'].draft_agent('customer-research','company','alice')
    selected['agents'].definition_model.model_validate(draft)
    login('demo','bob')
    response=client.post('/api/manufacturer/team',json={'action':'invite','email':'new@metal-ls.com','role':'operator','expected_revision':0},headers={'X-Rocoto-Page':'1'})
    assert response.status_code==403,response.text
    login('second','carol')
    assert client.get('/api/manufacturer/configurator').json()['revision']==0
    assert client.get('/api/manufacturer/configurator/versions/'+saved['version_id']).status_code==404
    with app.state.access.connect() as db:
        db.execute('DELETE FROM principal_environments WHERE principal_id=? AND environment=?',('carol','second'))
    assert client.get('/api/manufacturer/overview').status_code in {401,403}
print('manufacturer-native-http-ok')
'''
    environment = {**os.environ, 'ROCOTO_VERTICAL': 'manufacturer', 'PYTHONDONTWRITEBYTECODE': '1'}
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-B', '-c', script, str(VERTICAL.parents[1]), str(tmp_path / 'state')], env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'manufacturer-native-http-ok' in result.stdout


def test_native_save_and_restore_publish_exact_versions_and_keep_receipts(tmp_path, monkeypatch):
    ext, _ = extension(tmp_path)
    monkeypatch.setenv('ROCOTO_MANUFACTURER_ORDERS_TOKEN', 't' * 32)
    ext.publisher.source = SimpleNamespace(binding=lambda: {'transport': 'orders-api', 'company_slug': 'metal-ls'})
    delivered = []
    def publish(payload):
        delivered.append(payload)
        version = payload['version']
        return {'ok': True, 'published': True, 'version_id': version['id'], 'revision': version['revision'], 'sha256': version['sha256']}
    ext.publisher.transport = publish
    original = ext.configurator('alice')
    saved = ext.save_configurator('alice', {'settings': {'headline': 'Live version'}, 'expected_revision': 0}, 'Owner edit')
    assert saved['published'] is True and saved['state'] == 'published'
    assert delivered[0]['version']['document'] == saved['document']
    assert delivered[0]['company_id'] == 'workspace-a'
    restored = ext.restore('alice', original['version_id'], 1, 'Restore original')
    assert restored['published'] is True and restored['revision'] == 2
    assert delivered[1]['version']['document'] == original['document']
    assert ext.publisher.status(saved['version_id'])['published'] is True


def test_failed_publication_retains_saved_document_and_honest_pending_status(tmp_path, monkeypatch):
    ext, _ = extension(tmp_path)
    monkeypatch.setenv('ROCOTO_MANUFACTURER_ORDERS_TOKEN', 't' * 32)
    ext.publisher.source = SimpleNamespace(binding=lambda: {'transport': 'orders-api', 'company_slug': 'metal-ls'})
    ext.publisher.transport = lambda value: {'ok': True, 'published': True, 'sha256': 'wrong'}
    saved = ext.save_configurator('alice', {'settings': {'headline': 'Retained edit'}, 'expected_revision': 0}, 'Owner edit')
    assert saved['state'] == 'pending' and saved['published'] is False
    assert ext.configurator('alice')['settings']['headline'] == 'Retained edit'
    assert ext.versions('alice')['versions'][0]['revision'] == 1


def test_versioned_svg_supports_geometry_and_gradients_only(tmp_path):
    ext, _ = extension(tmp_path)
    document = ext.configurator('alice')['document']
    document['configuration']['drawings'] = {'lock': '<svg viewBox="0 0 20 20"><defs><linearGradient id="a"><stop offset="0" stop-color="#eeeeee"/></linearGradient></defs><rect width="20" height="20" fill="url(#a)"/></svg>'}
    saved = ext.save_configurator('alice', {'document': document, 'expected_revision': 0}, 'Source-backed drawing')
    assert saved['document']['configuration']['drawings'] == document['configuration']['drawings']
    document['configuration']['drawings']['lock'] = '<svg><image href="https://example.com/a.svg"/></svg>'
    with pytest.raises(Invalid):
        ext.save_configurator('alice', {'document': document, 'expected_revision': 1}, 'Active drawing refused')


def test_existing_pending_version_retries_on_editor_read_without_publishing_initial_draft(tmp_path, monkeypatch):
    ext, _ = extension(tmp_path)
    monkeypatch.setenv('ROCOTO_MANUFACTURER_ORDERS_TOKEN', 't' * 32)
    ext.publisher.source = SimpleNamespace(binding=lambda: {'transport': 'orders-api', 'company_slug': 'metal-ls'})
    delivered = []
    def transport(payload):
        delivered.append(payload)
        if len(delivered) == 1:
            raise OSError('Temporary receiver outage')
        version = payload['version']
        return {'ok': True, 'published': True, 'version_id': version['id'], 'revision': version['revision'], 'sha256': version['sha256']}
    ext.publisher.transport = transport
    assert ext.configurator('alice')['state'] == 'draft'
    assert delivered == []
    saved = ext.save_configurator('alice', {'settings': {'headline': 'Publish this revision'}, 'expected_revision': 0}, 'Owner request')
    assert saved['state'] == 'pending' and len(delivered) == 1
    assert ext.configurator('reader')['state'] == 'pending' and len(delivered) == 1
    recovered = ext.configurator('alice')
    assert recovered['published'] and recovered['version_id'] == saved['version_id']
    assert delivered[0] == delivered[1] and len(ext.versions('alice')['versions']) == 2


def test_source_picker_appearance_fallback_preserves_exact_historical_document(tmp_path):
    from manufacturer_store import default_document
    document = default_document()
    for field in ('headline', 'description', 'button_label'):
        document['configuration'].pop(field)
    document['configuration'].update(picker={'start': {'spec': True}}, defaultLanguage='en', strings={'en': {'go': 'Find a lock'}, 'bg': {'go': 'Намери брава'}})
    store = ManufacturerStore(tmp_path, 'workspace-a', document)
    original = json.loads(json.dumps(document))
    assert store.configurator()['settings']['headline'] == 'Do you have a specification to upload or paste?'
    assert store.configurator()['document'] == original
    document['configuration']['defaultLanguage'] = 'bg'
    other = ManufacturerStore(tmp_path, 'workspace-b', document)
    assert other.configurator()['settings']['headline'] == 'Имате ли спецификация?'
    assert other.configurator()['settings']['description'] == 'Качете списъка си или изберете обкова за вашия проект.'
    assert other.configurator()['settings']['button_label'] == 'Намери брава'
    assert store.configurator()['settings']['description'] == 'Upload your list, or choose the hardware for your project.'
    assert store.configurator()['settings']['button_label'] == 'Find a lock'
    assert other.configurator()['document'] == document


def test_source_order_projection_handles_quotes_real_products_and_stable_numbers(tmp_path, monkeypatch):
    database = tmp_path / 'source.sqlite'
    store = company_store(database)
    catalogue = {'source': 'https://metal-ls.com/', 'currency': 'EUR', 'vat': 0.2,
        'rules': {}, 'services': [], 'cats': [{'code': 'mortise', 'en': 'Mortise lock 55/72',
        'img': '/metal-ls/wp-content/lock.jpg', 'url': {'en': 'https://metal-ls.com/en/product/5572-en/'},
        'items': [{'c': code, 'p': price, 'q': pending, 'u': 'pc', 'r': {'k': 'whole'},
        'se': 'Nickel, with strike plate', 'a': {'backset': 55}} for code, price, pending in [('Q', 0, 1), ('P', 10, 0), ('F', 0, 0)]]}]}
    store.ingest_catalog(catalogue, {'slug': 'metal-ls', 'hours': {str(day): [510, 1050, 1020] for day in range(1, 6)}}, 'test', 'Source fixture')
    source = Path(__file__).resolve().parents[2] / 'app/src/prototypes/orders/quote.py'
    monkeypatch.syspath_prepend(str(source.parent))
    spec = importlib.util.spec_from_file_location('manufacturer_quote_contract', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identities = []
    for index, codes in enumerate([['Q'], ['P', 'Q'], ['F']]):
        value = module.quote(store.export_catalog(), [{'code': code, 'rows': [{'x': 0, 'n': 2}]} for code in codes])
        created = store.submit_order({'name': 'Alex Buyer', 'email': 'alex@network.rca.ac.uk'}, value['lines'], value['quote'], str(index), 'test', 'Checkout fixture')
        identities.append(created['id'])
    state = tmp_path / 'workspace'
    bind(state, database)
    source = CompanySource(state, 'workspace-a')
    request = source.order(identities[0])
    assert request['number'] == request['display_number'] == '#1001'
    assert request['pricing_state'] == 'to_quote' and request['total'] is None
    assert request['customer']['company'] == '' and request['customer']['company_details_needed']
    assert request['lines'][0]['name'] == 'Mortise lock 55/72'
    assert request['lines'][0]['image_url'] == 'https://orders-demo.soyrocoto.com/metal-ls/wp-content/lock.jpg'
    assert request['lines'][0]['quantity'] == 2 and request['lines'][0]['unit'] == 'pc'
    assert request['lines'][0]['code'] == 'Q' and request['lines'][0]['total'] is None
    mixed = source.order(identities[1])
    assert mixed['pricing_state'] == 'partially_priced' and mixed['total'] is None and mixed['priced_total'] == 20
    free = source.order(identities[2])
    assert free['pricing_state'] == 'priced' and free['total'] == 0 and free['lines'][0]['unit_price'] == 0
    assert source.orders(limit=1, offset=1)[0]['display_number'] == '#1002'
    with sqlite3.connect(tmp_path / 'restored.sqlite') as restored:
        store.db.backup(restored)
        restored.execute('VACUUM')
    bind(state, tmp_path / 'restored.sqlite')
    assert CompanySource(state, 'workspace-a').order(identities[0])['display_number'] == '#1001'
    store.close()


def test_company_display_requires_completed_source_supported_name(tmp_path):
    ext, _ = extension(tmp_path)
    original = {'id': 'order-1', 'customer': {'name': 'Alex Buyer', 'company': '', 'email': 'alex@network.rca.ac.uk'}}
    queued = ext._order_context(json.loads(json.dumps(original)))
    assert queued['customer']['display_name'] == 'Alex Buyer'
    assert queued['customer']['company_details_needed'] and not queued['enrichment']['has_findings']
    source = 'https://example.test/about'
    findings = {'status': 'enriched', 'summary': 'Submitted company match',
        'facts': [{'label': 'Company name', 'text': 'Verified Works', 'source_urls': [source]}],
        'sources': [{'url': source, 'title': 'Company website'}]}
    ext.research = SimpleNamespace(get=lambda *_: findings)
    enriched = ext._order_context(json.loads(json.dumps(original)))
    assert enriched['customer']['display_company'] == 'Verified Works'
    assert enriched['customer']['company'] == '' and enriched['customer']['company_source_url'] == source
    assert enriched['enrichment']['complete'] and enriched['enrichment']['has_findings']
    findings['status'] = 'needs_review'
    ambiguous = ext._order_context(json.loads(json.dumps(original)))
    assert ambiguous['customer']['display_name'] == 'Alex Buyer' and ambiguous['customer']['company_details_needed']
    findings['status'] = 'enriched'
    findings['facts'][0]['source_urls'] = ['https://unverified.test/']
    unsupported = ext._order_context(json.loads(json.dumps(original)))
    assert unsupported['customer']['display_name'] == 'Alex Buyer'


def test_reduced_legacy_qa_source_retains_readable_order_references(tmp_path):
    from manufacturer_seed import seed
    seed(tmp_path, 'workspace-a')
    order = CompanySource(tmp_path, 'workspace-a').order('QA-001')
    assert order['display_number'] == '#L1001' and order['id'] == 'QA-001'
    assert order['lines'][0]['name'] == 'Custom request'
    assert order['pricing_state'] == 'to_quote' and order['total'] is None
