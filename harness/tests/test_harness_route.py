"""GET /api/harness: the shell the page draws its sidebar from, behind the session every /api/ route needs.

The answer is the active template's `Pack.shell()` with the reader's fixed conversation ids. Under
the job-shop test template the same route answers Organization and Office, Dispatch and Desk.
Zero remote calls: the app is the fixture app of the access tests.
"""
import json
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import harness_routes
import pack
from test_server_access import fixture_app

HEADERS = {'X-Video-Demo': '1'}
JOBSHOP = Path(__file__).resolve().parent / 'verticals' / 'jobshop'


def login(client, app, person, environment='demo'):
    answer = client.post('/api/session', json={'code': app.state.access.issue(environment, principal_id=person,
        display_name=person.title())}, headers=HEADERS)
    assert answer.status_code == 200, answer.text


def fixed(answer):
    return {chat['purpose']: chat['chat_id'] for chat in answer['fixed_chats']}


def without_ids(answer):
    return {**answer, 'fixed_chats': [{key: value for key, value in chat.items() if key != 'chat_id'} for chat in answer['fixed_chats']]}


def test_the_route_requires_a_session(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as visitor, TestClient(app) as member:
        refused = visitor.get('/api/harness')
        assert refused.status_code == 401 and 'schema' not in refused.text and 'Organization' not in refused.text
        visitor.cookies.set('rocoto_video_session', 'forged')
        assert visitor.get('/api/harness').status_code == 401
        visitor.cookies.clear()
        login(member, app, 'alice')
        assert member.get('/api/harness').status_code == 200
        assert visitor.get('/api/harness').status_code == 401, 'one member\'s session opens the route for that member alone'
    # The handler asks for the session itself as well: mounted behind zero middleware it stays closed.
    bare = FastAPI()
    harness_routes.register_harness_routes(bare)
    with TestClient(bare) as client:
        closed = client.get('/api/harness')
        assert closed.status_code == 401 and closed.json() == {'detail': 'Enter your company access code.'}
        assert closed.headers['cache-control'] == 'private, no-store'


def test_the_answer_is_the_shell_of_the_active_template_with_the_reader_s_fixed_chat_ids(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    retail = pack.load('retail')
    with TestClient(app) as alice, TestClient(app) as outsider:
        login(alice, app, 'alice')
        first = alice.get('/api/harness?collection=cafe')
        assert first.status_code == 200 and first.headers['cache-control'] == 'private, no-store'
        answer = first.json()
        assert without_ids(answer) == retail.shell() and answer['schema'] == 'rocoto.harness-shell.v1'
        assert [section['key'] for section in answer['sections']] == ['Organization', 'Action', 'Tracker', 'Recent']
        assert [chat['title'] for chat in answer['fixed_chats']] == ['Organization', 'Cameras', 'Messaging setup']
        # A read creates nothing: each fixed chat gets its id once its conversation exists.
        assert fixed(answer) == {'organization': None, 'cameras': None, 'messaging': None}
        assert not [record for record in app.state.workspaces['demo']['agent'].children.values() if record.get('kind') == 'organization_chat']
        made = alice.post('/api/organization/chats', json={'purpose': 'cameras', 'collection': 'cafe'}, headers=HEADERS).json()
        assert fixed(alice.get('/api/harness?collection=cafe').json()) == {'organization': None, 'cameras': made['id'], 'messaging': None}
        rows = {row['organization_purpose']: row['id'] for row in alice.get('/api/organization/chats?collection=cafe').json()['chats']}
        assert fixed(alice.get('/api/harness?collection=cafe').json()) == rows and len(set(rows.values())) == 3
        # A conversation this member's present access leaves out keeps its id: the host's own read check decides.
        selected = app.state.workspaces['demo']
        admitted = selected['chats'].history_authorized
        selected['chats'].history_authorized = selected['organization_chats'].history_authorized = (
            lambda record, session: record.get('organization_purpose') != 'cameras' and admitted(record, session))
        assert fixed(alice.get('/api/harness?collection=cafe').json()) == {**rows, 'cameras': None}
        selected['chats'].history_authorized = selected['organization_chats'].history_authorized = admitted
        assert fixed(alice.get('/api/harness?collection=cafe').json()) == rows
        # The ids are one collection's and one company's.
        assert fixed(alice.get('/api/harness').json()) == {'organization': None, 'cameras': None, 'messaging': None}
        app.state.access.create_environment('other', 'Other')
        login(outsider, app, 'outsider', 'other')
        assert fixed(outsider.get('/api/harness?collection=cafe').json()) == {'organization': None, 'cameras': None, 'messaging': None}
        # A member who has left the company gets the closed answer of the host, ids and all.
        with app.state.access.connect() as db:
            db.execute("DELETE FROM principal_environments WHERE principal_id='alice'")
        left = alice.get('/api/harness?collection=cafe')
        assert left.status_code == 403 and 'orgchat-' not in left.text
        # Names, labels and conversation ids: file locations and prompt text stay on the host.
        text = json.dumps(answer)
        for banned in (str(HERE), str(tmp_path), 'prompts/', '.md', retail.prompt('system')[:60]):
            assert banned not in text, banned
        assert login(alice, app, 'bob') is None and alice.get('/api/harness?collection=bad/name').status_code == 400


def test_under_the_job_shop_template_the_route_answers_organization_and_office_dispatch_and_desk(tmp_path, monkeypatch):
    with pack.use(JOBSHOP) as jobshop:
        app = fixture_app(tmp_path, monkeypatch)
        with TestClient(app) as client:
            login(client, app, 'alice')
            answer = client.get('/api/harness?collection=cafe').json()
            assert without_ids(answer) == jobshop.shell()
            assert answer['vertical'] == {'id': 'jobshop', 'revision': jobshop.revision}
            assert [section['key'] for section in answer['sections']] == ['Organization', 'Dispatch', 'Desk', 'Recent']
            assert [(chat['purpose'], chat['title']) for chat in answer['fixed_chats']] == [('organization', 'Organization'), ('office', 'Office')]
            assert [surface['id'] for surface in answer['surfaces']] == ['backlog']
            assert answer['sections'][2]['starter_prompt'].startswith('Help me create a Desk')
            office = client.post('/api/organization/chats', json={'purpose': 'office', 'collection': 'cafe'}, headers=HEADERS)
            assert office.status_code == 200, office.text
            assert fixed(client.get('/api/harness?collection=cafe').json()) == {'organization': None, 'office': office.json()['id']}


def test_the_shell_stands_alone_for_a_caller_with_no_workspace():
    """`shell()` with the template alone: what a page gets before a company's stores are in reach."""
    with pack.use('retail') as retail:
        answer = harness_routes.shell()
        assert without_ids(answer) == retail.shell() and set(fixed(answer).values()) == {None}
