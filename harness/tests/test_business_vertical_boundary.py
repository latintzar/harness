"""Shared profiles stay generic; camera setup belongs to the selected retail host."""
import json

from fastapi.testclient import TestClient

import pack
from business_context import BusinessStore

CAMERA_FIELDS = {'settings', 'connections', 'settings_scope', 'footage_relationship'}
CAMERA_ROUTES = ('/api/business/settings', '/api/business/connections',
                 '/api/retail/business/settings', '/api/retail/business/connections')


def test_generic_business_profile_has_only_shared_fields(tmp_path):
    store = BusinessStore(tmp_path)
    original = store.get('company')
    assert not CAMERA_FIELDS & original.keys()
    saved = store.update('company', 0, {'name': 'Maker'})
    assert not CAMERA_FIELDS & saved.keys()
    assert BusinessStore(tmp_path).get('company') == saved


def test_bare_profile_and_routes_are_generic(tmp_path):
    import server
    with pack.use('bare'):
        app = server.create_app(tmp_path / 'state')
        assert not set(CAMERA_ROUTES) & {getattr(route, 'path', '') for route in app.routes}
        with TestClient(app) as client:
            headers = {'X-Rocoto-Page': '1'}
            assert client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=headers).status_code == 200
            response = client.get('/api/business?collection=company')
            assert response.status_code == 200
            assert not CAMERA_FIELDS & response.json().keys()
            for path in CAMERA_ROUTES:
                assert client.post(path, json={'collection': 'company'}, headers=headers).status_code == 404


def test_existing_retail_profile_bytes_and_fields_survive_reopen(tmp_path):
    # This exact JSON is a previously persisted record; loading it performs no migration write.
    legacy = {'collection': 'company', 'revision': 3, 'profile_revision': 1,
        'profile': {'name': 'Existing retailer'}, 'provenance': {'origin': 'operator_provided'},
        'onboarding_chat_id': 'onboarding-existing', 'settings': {'initial_history_hours': 24, 'analysis_window_seconds': 8},
        'connections': [{'id': 'connection-existing', 'status': 'configuration_required'}],
        'settings_scope': 'future_authorized_imports',
        'footage_relationship': {'status': 'unestablished', 'basis': 'Saved original source'},
        'created_at': '2026-10-01T00:00:00Z', 'updated_at': '2026-10-02T00:00:00Z', 'last_change': None}
    shared = BusinessStore(tmp_path)
    original = json.dumps(legacy, separators=(',', ':'))
    with shared.connect() as db:
        db.execute('INSERT INTO business_revisions VALUES(?,?,?)', ('company', 3, original))
    assert not CAMERA_FIELDS & shared.get('company').keys()
    assert not CAMERA_FIELDS & shared.history('company')[0].keys()
    from retail_business import RetailBusinessStore
    retail = RetailBusinessStore(tmp_path)
    assert retail.get('company') == legacy
    with retail.connect() as db:
        assert db.execute('SELECT body FROM business_revisions').fetchone()[0] == original
    changed = retail.update('company', 3, {'name': 'Updated retailer'})
    for field in CAMERA_FIELDS:
        assert changed[field] == legacy[field]
    assert retail.history('company')[0] == legacy


def test_retail_setup_routes_keep_aliases_authority_and_revision_checks(tmp_path):
    from test_onboarding_http import app_with_sources
    with pack.use('retail'):
        app = app_with_sources(tmp_path)
        with TestClient(app) as client:
            headers = {'X-Rocoto-Page': '1'}
            assert client.post('/api/session/demo', headers=headers).status_code == 200
            body = {'collection': 'cafe', 'expected_revision': 0, 'settings': {'initial_history_hours': 24}}
            for path in ('/api/retail/business/settings', '/api/business/settings'):
                assert client.post(path, json=body, headers=headers).status_code == 403
            assert client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=headers).status_code == 200
            initial = client.get('/api/business?collection=cafe').json()
            assert initial['settings'] == {'initial_history_hours': 48, 'analysis_window_seconds': 8}
            saved = client.post('/api/retail/business/settings', json=body, headers=headers)
            assert saved.status_code == 200
            assert saved.json()['settings']['initial_history_hours'] == 24
            assert client.post('/api/business/settings', json=body, headers=headers).status_code == 400
            for path, revision in (('/api/retail/business/connections', 1), ('/api/business/connections', 2)):
                response = client.post(path, json={'collection': 'cafe', 'connection': {
                    'expected_revision': revision, 'provider': 'Recorder', 'type': 'recorder_export',
                    'source_description': 'Operator selected export'}}, headers=headers)
                assert response.status_code == 200
                assert response.json()['connections'][-1]['history_hours'] == 24
                assert response.json()['connections'][-1]['ingestion_started'] is False
            assert app.state.workspaces['demo']['agent']._business() is app.state.workspaces['demo']['business']
