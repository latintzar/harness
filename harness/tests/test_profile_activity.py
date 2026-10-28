"""Measured activity, calendar boundaries and current selected-owner authority."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
import profile_activity as pa
from background_agents import AgentStore
from company_usage import CompanyUsage
from test_server_access import fixture_app, FixtureAgent
from test_workflow_routes import login

NOW = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


def setup(state):
    pool = CompanyUsage.openai_pool(state)
    AgentStore(state)
    gateway = state / 'billing/openai-api/gateway/runs.sqlite'
    gateway.parent.mkdir(exist_ok=True)
    with sqlite3.connect(gateway) as db:
        db.execute('CREATE TABLE requests(id TEXT PRIMARY KEY, run TEXT, held TEXT)')
    return pool, gateway


def settled(pool, key, when, tokens=10, output=2, cached=3):
    source = {'provider': 'openai', 'request_id': key, 'response_id': 'response-'+key,
        'received_at': when, 'usage': {'input_tokens': tokens, 'output_tokens': output, 'cached_input_tokens': cached}}
    with pool.connect() as db:
        pool._append(db, 'settled:'+key, 'settled', source=source)


def run(state, key, owner, created, completed=None, title=None):
    definition = {'id': key, 'title': title or key, 'owner_principal_id': owner}
    value = {'id': 'run-'+key, 'agent_id': key, 'owner_principal_id': owner, 'created_at': created,
        'completed_at': completed, 'status': 'completed' if completed else 'running'}
    with sqlite3.connect(state/'background-agents.sqlite') as db:
        db.execute('INSERT INTO agents VALUES(?,?)', (key, json.dumps(definition)))
        db.execute('INSERT INTO runs VALUES(?,?,?)', (value['id'], key, json.dumps(value)))


def activity(state, scope='person', owner='alice', chats=(), days=7, now=NOW):
    return pa.activity(state, scope=scope, principal_id=owner, identity={'name':'Fixture','avatar_url':None},
        organisation_allowed=scope=='organisation', chats=chats, days=days, now=now)


def bind(state, run_id, owner, monkeypatch, at=NOW):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):return at
    with monkeypatch.context() as patch:
        patch.setattr(pa, 'datetime', Clock)
        pa.bind_model_run(state, run_id, owner)


def test_person_runs_chats_and_unavailable_fields_exclude_other_and_unowned(tmp_path):
    setup(tmp_path)
    run(tmp_path, 'agent-a', 'alice', '2026-01-04T23:30:00-02:00', '2026-01-05T03:00:00Z')
    run(tmp_path, 'agent-b', 'bob', NOW.timestamp(), title='Private other person')
    run(tmp_path, 'legacy', None, NOW.timestamp())
    chats = [{'id':'a','owner_principal_id':'alice','created_at':NOW.timestamp()},
        {'id':'b','owner_principal_id':'bob','created_at':NOW.timestamp()},
        {'id':'child','owner_principal_id':'alice','created_at':NOW.timestamp(),'parent_id':'a'},
        {'id':'automatic','owner_principal_id':'alice','created_at':NOW.timestamp(),'kind':'agent_run'},
        {'id':'undated','owner_principal_id':'alice'}, {'id':'unowned','created_at':NOW.timestamp()}]
    result=activity(tmp_path,chats=chats+chats)
    assert result['totals']['chats']==result['totals']['agent_runs']==result['totals']['agent_runs_completed']==1
    assert result['totals']['tokens'] is None and result['totals']['alerts_sent'] is None
    assert result['metrics']['chats']['undated_records']==1
    assert 'Private other person' not in json.dumps(result)
    assert result['agents'][0]['id']=='agent-a'


def test_exact_settled_company_tokens_normalize_offsets_iso_week_and_future(tmp_path):
    pool,_=setup(tmp_path)
    settled(pool,'one','2025-12-31T23:30:00-02:00')
    settled(pool,'two','2026-01-04T23:30:00-02:00',tokens=20,output=4,cached=7)
    settled(pool,'future','2026-01-06T00:00:00Z',tokens=100)
    result=activity(tmp_path,'organisation')
    assert result['totals']['tokens']==36 and result['totals']['cached_input_tokens']==10
    assert result['weekly'][0]['week']=='2026-W01' and result['weekly'][0]['tokens']==12
    assert result['weekly'][1]['week']=='2026-W02' and result['weekly'][1]['tokens']==24
    assert next(d for d in result['daily'] if d['date']=='2026-01-01')['tokens']==12
    assert result['metrics']['tokens']['includes_subscription_usage'] is False
    assert result['metrics']['tokens']['includes_unsettled_requests'] is False


def test_leap_day_and_separate_completion_day(tmp_path):
    setup(tmp_path)
    run(tmp_path,'a','alice','2024-02-28T23:59:59Z','2024-02-29T01:00:00Z')
    result=activity(tmp_path,days=3,now=datetime(2024,3,1,tzinfo=timezone.utc))
    assert [d['date'] for d in result['daily']]==['2024-02-28','2024-02-29','2024-03-01']
    assert result['daily'][0]['agent_runs']==1 and result['daily'][0]['agent_runs_completed']==0
    assert result['daily'][1]['agent_runs']==0 and result['daily'][1]['agent_runs_completed']==1


@pytest.mark.parametrize('value',[None,True,float('nan'),float('inf'),'2026-01-05T12:00:00','invalid','9'*100])
def test_unknown_or_unzoned_dates_are_not_invented(value):assert pa.stamp(value) is None


def test_forward_only_binding_receipts_and_owner_conflict(tmp_path,monkeypatch):
    pool,gateway=setup(tmp_path)
    settled(pool,'historical','2026-01-01T01:00:00Z',tokens=300)
    with sqlite3.connect(gateway) as db:db.execute('INSERT INTO requests VALUES(?,?,?)',('historical','old','0'))
    with pytest.raises(ValueError,match='before'):bind(tmp_path,'old','alice',monkeypatch)
    earlier=datetime(2026,1,4,tzinfo=timezone.utc)
    bind(tmp_path,'owned','alice',monkeypatch,earlier)
    bind(tmp_path,'owned','alice',monkeypatch)
    with pytest.raises(ValueError,match='original owner'):bind(tmp_path,'owned','bob',monkeypatch)
    bind(tmp_path,'other','bob',monkeypatch,earlier)
    with sqlite3.connect(gateway) as db:
        db.executemany('INSERT INTO requests VALUES(?,?,?)',[('a','owned','0'),('b','other','0')])
    settled(pool,'a','2026-01-05T01:00:00Z')
    settled(pool,'b','2026-01-05T02:00:00Z',tokens=200)
    result=activity(tmp_path)
    assert result['totals']['tokens']==12
    assert result['daily'][0]['tokens'] is None and result['daily'][-1]['tokens']==12
    assert result['metrics']['tokens']['coverage']=='attributed_api_receipts'
    assert activity(tmp_path,'organisation')['totals']['tokens']==516
    assert activity(tmp_path,owner='bob')['totals']['tokens']==202
    with sqlite3.connect(tmp_path/'profile-activity.sqlite') as db:
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            db.execute("UPDATE model_owners SET owner='bob' WHERE run_id='owned'")


def test_foreign_pool_binding_and_missing_stores_are_unavailable(tmp_path):
    setup(tmp_path/'one');other=CompanyUsage.openai_pool(tmp_path/'two')
    import shutil
    shutil.copyfile(other.path,tmp_path/'one/billing/openai-api/company-usage.sqlite')
    assert activity(tmp_path/'one','organisation')['totals']['tokens'] is None
    result=activity(tmp_path/'absent','organisation')
    assert result['totals']['agent_runs'] is None
    assert not (tmp_path/'absent').exists()


def test_http_person_organisation_observer_membership_and_company_scope(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);access=app.state.access
    with TestClient(app) as alice,TestClient(app) as bob,TestClient(app) as owner:
        login(alice,app,'alice');login(bob,app,'bob');login(owner,app,'owner',owner=True)
        FixtureAgent.instances['demo'].children={
            'alice-chat':{'id':'alice-chat','owner_principal_id':'alice','created_at':datetime.now(timezone.utc).timestamp()},
            'bob-chat':{'id':'bob-chat','owner_principal_id':'bob','created_at':datetime.now(timezone.utc).timestamp()}}
        response=alice.get('/api/profile/activity?days=7')
        assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
        assert response.json()['totals']['chats']==1
        assert alice.get('/api/profile/activity?scope=organisation').status_code==403
        assert owner.get('/api/profile/activity?scope=organisation').json()['totals']['chats']==2
        assert owner.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers={'X-Video-Demo':'1'}).status_code==200
        assert owner.get('/api/profile/activity').json()['totals']['chats']==1
        assert owner.get('/api/profile/activity?scope=organisation').status_code==403
        with access.connect() as db:db.execute("DELETE FROM principal_environments WHERE principal_id='alice'")
        assert alice.get('/api/profile/activity').status_code==403
        assert owner.get('/api/profile/activity').status_code in {401,403}
        for query in ['scope=company','days=0','days=367','days=-1']:
            assert bob.get('/api/profile/activity?'+query).status_code==400


def test_http_denies_anonymous_browse_and_cross_company_overrides(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);access=app.state.access
    access.create_environment('other','Other organisation')
    with TestClient(app) as alice,TestClient(app) as other,TestClient(app) as browse:
        assert browse.get('/api/profile/activity').status_code==401
        browse.post('/api/session/demo',headers={'X-Video-Demo':'1'})
        assert browse.get('/api/profile/activity').status_code==403
        login(alice,app,'alice',owner=True)
        other.post('/api/session',json={'code':access.issue('other',owner=True,principal_id='alice')},headers={'X-Video-Demo':'1'})
        assert other.post('/api/environment',json={'environment_id':'other'},headers={'X-Video-Demo':'1'}).status_code==200
        FixtureAgent.instances['demo'].children={'ours':{'id':'ours','owner_principal_id':'alice','created_at':datetime.now(timezone.utc).timestamp()}}
        result=other.get('/api/profile/activity?scope=organisation&environment=demo&principal_id=alice').json()
        assert result['identity']['name']=='Other organisation' and result['totals']['chats']==0
        assert alice.get('/api/profile/activity?scope=organisation').json()['totals']['chats']==1


def test_http_rechecks_membership_after_aggregation_before_output(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);original=pa.activity
    def revoke(*args,**kwargs):
        result=original(*args,**kwargs)
        with app.state.access.connect() as db:db.execute("DELETE FROM principal_environments WHERE principal_id='alice'")
        return result
    with TestClient(app) as client:
        login(client,app,'alice',owner=True)
        monkeypatch.setattr(pa,'activity',revoke)
        response=client.get('/api/profile/activity?scope=organisation')
        assert response.status_code==403
        assert 'totals' not in response.json()


def test_empty_measured_receipts_are_zero_without_creating_or_mutating_records(tmp_path):
    pool,gateway=setup(tmp_path)
    before={path:path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    result=activity(tmp_path,'organisation')
    assert result['totals']['tokens']==0 and result['metrics']['tokens']['available']
    assert result['totals']['agent_runs']==0
    assert {path:path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}==before
    assert not (tmp_path/'profile-activity.sqlite').exists()


@pytest.mark.parametrize('source',[[],{'provider':'openai','usage':[]},
    {'provider':'openai','received_at':'2026-01-04T00:00:00Z','usage':'bad'},
    {'provider':'openai','received_at':'2026-01-04T00:00:00Z','usage':{'input_tokens':1,'output_tokens':2,'cached_input_tokens':3}}])
def test_corrupt_usage_is_reported_as_partial_coverage_not_fabricated_tokens(tmp_path,source):
    pool,_=setup(tmp_path)
    settled(pool,'good','2026-01-04T00:00:00Z')
    with pool.connect() as db:pool._append(db,'settled:bad','settled',source=source)
    result=activity(tmp_path,'organisation')
    assert result['totals']['tokens']==12
    assert result['metrics']['tokens']['coverage']=='partial_settled_receipts'


@pytest.mark.anyio
async def test_runtime_attribution_inherits_root_owner_before_gateway_opens(tmp_path):
    from agent_auth import AgentAuth
    from test_agent_auth import funded
    funded(tmp_path);opened=[]
    class Gateway:
        def __init__(self,state,*,run_id,**kwargs):
            with pa.reader(state/'profile-activity.sqlite') as db:
                assert db.execute('SELECT owner FROM model_owners WHERE run_id=?',(run_id,)).fetchone()==('alice',)
            opened.append(run_id);self.token='local-fixture-capability';self.cli_config={}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    auth=AgentAuth(tmp_path,mode='openai_api',environ={'ROCOTO_OPENAI_API_KEY':'fixture'},runtime_host='rocoto-staging')
    async with auth.lease(run_id='root',activity_owner='alice',gateway_factory=Gateway):
        async with auth.lease(run_id='child',activity_owner='alice',gateway_factory=Gateway):pass
        with pytest.raises(ValueError,match='original owner'):
            async with auth.lease(run_id='foreign',activity_owner='bob',gateway_factory=Gateway):pass
    assert opened==['root','root']
    with pa.reader(tmp_path/'profile-activity.sqlite') as db:
        assert db.execute('SELECT run_id,owner FROM model_owners').fetchall()==[('root','alice')]


def test_attribution_refuses_shared_or_linked_store(tmp_path):
    (tmp_path/'profile-activity.sqlite').touch(mode=0o644)
    with pytest.raises(ValueError,match='private'):pa.bind_model_run(tmp_path,'root','alice')


def test_settings_components_are_public_code_while_activity_stays_private(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        for module in ('profile-settings','plugin-settings','camera-streams'):
            for suffix in ('js','css'):
                response=client.get('/'+module+'.'+suffix)
                assert response.status_code==200
                assert response.content==((Path(__file__).parents[1]/('verticals/retail/web' if module=='camera-streams' else 'web'))/f'{module}.{suffix}').read_bytes()
        assert client.get('/api/profile/activity').status_code==401


@pytest.fixture
def anyio_backend():return 'asyncio'


def test_alert_counts_use_accepted_operational_receipts_and_member_scope(tmp_path):
    setup(tmp_path)
    with sqlite3.connect(tmp_path/'messaging.sqlite') as db:
        db.execute('CREATE TABLE outbox(member TEXT,created REAL,kind TEXT,status TEXT,provider_message_id TEXT)')
        for member,kind,status,key in [('alice','text','delivered','a'),('alice','utility','sent','b'),
            ('bob','text','read','c'),('alice','otp','sent','otp'),('alice','hello','sent','hello'),
            ('alice','text','uncertain',None),('alice','utility','failed',None)]:
            db.execute('INSERT INTO outbox VALUES(?,?,?,?,?)',(member,NOW.timestamp(),kind,status,key))
    assert activity(tmp_path)['totals']['alerts_sent']==2
    assert activity(tmp_path,'organisation')['totals']['alerts_sent']==3
    assert activity(tmp_path,owner='other')['totals']['alerts_sent']==0


def test_alert_counts_cover_every_phone_channel_in_the_current_store(tmp_path):
    """Outbox rows carry their channel; an accepted alert counts once on whichever channel carried it."""
    from messaging import Messaging
    setup(tmp_path)
    store = Messaging(tmp_path, company_id='company', authorized=lambda actor: True)
    with store._db(write=True):
        pass
    with sqlite3.connect(tmp_path/'messaging.sqlite') as db:
        assert 'channel' in [row[1] for row in db.execute('PRAGMA table_info(outbox)')]
        for index,(member,channel,kind,status,key) in enumerate([('alice','viber','text','delivered','v1'),
                ('alice','sms','utility','sent','s1'),('alice','whatsapp','text','read','w1'),('bob','sms','text','sent','s2'),
                ('alice','sms','otp','sent','s3'),('alice','viber','hello','sent','v2'),('alice','sms','text','uncertain',None)]):
            db.execute('''INSERT INTO outbox(id,member,account,generation,address,chat,collection,kind,text,request_id,
                intent_sha256,status,provider_message_id,created,updated,channel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (f'msg-{index}',member,channel+'-account',1,'+34611111111','chat','shop',kind,None,f'request-{index}','0'*64,
                 status,key,NOW.timestamp(),NOW.timestamp(),channel))
    assert activity(tmp_path)['totals']['alerts_sent']==3
    assert activity(tmp_path,'organisation')['totals']['alerts_sent']==4


def test_a_host_alert_delivery_counts_once_for_its_verified_recipient(tmp_path):
    """Each delivery attempt is an outbox row on its channel; held, cancelled and uncertain ones stay out."""
    from datetime import timedelta
    from test_phone_delivery import alert, delivery_fixture, work
    f = delivery_fixture(tmp_path, verified=('alice', 'bob'))
    f.phone.add_participant(f.key, 'bob', by='alice')
    f.phone.add_participant(f.key, 'carol', by='alice')  # no verified number: held, never an outbox row
    alert(f)
    work(f)
    f.spend.update(stopped=True)
    alert(f, 'event-2')  # the host's Stop: held for everyone
    work(f)
    with sqlite3.connect(f.store.path) as db:
        assert sorted(db.execute("SELECT member,channel,kind,status FROM outbox WHERE kind!='otp'").fetchall()) == [
            ('alice', 'whatsapp', 'utility', 'sent'), ('bob', 'whatsapp', 'utility', 'sent')]
        assert db.execute("SELECT status,count(*) FROM phone_deliveries GROUP BY status ORDER BY status").fetchall() == [('held', 4), ('sent', 2)]
    setup(f.store.state)
    now = datetime.fromtimestamp(f.now[0], timezone.utc) + timedelta(hours=1)
    assert activity(f.store.state, now=now)['totals']['alerts_sent'] == 1
    assert activity(f.store.state, owner='bob', now=now)['totals']['alerts_sent'] == 1
    assert activity(f.store.state, owner='carol', now=now)['totals']['alerts_sent'] == 0
    assert activity(f.store.state, 'organisation', now=now)['totals']['alerts_sent'] == 2
    assert activity(f.store.state, 'organisation', now=now)['metrics']['alerts_sent']['coverage'] == 'provider_accepted_deliveries'


def test_a_delivery_that_moved_to_its_next_channel_counts_once(tmp_path):
    from datetime import timedelta
    from messaging_provider import SendRejected
    from test_phone_delivery import SUMMARY, multi_fixture, work
    f = multi_fixture(tmp_path)
    f.sms.failure = SendRejected('The provider refused this message.')
    written = f.delivery.fan_out(f.key, alert_id='event-1', summary=SUMMARY)
    work(f, ticks=2)
    with sqlite3.connect(f.store.path) as db:
        assert db.execute('SELECT channel,status FROM outbox WHERE request_id LIKE ? ORDER BY request_id',
            (written[0]['id'] + ':%',)).fetchall() == [('sms', 'failed'), ('whatsapp', 'sent')]
    setup(f.store.state)
    now = datetime.fromtimestamp(f.now[0], timezone.utc) + timedelta(hours=1)
    assert activity(f.store.state, owner='bob', now=now)['totals']['alerts_sent'] == 1
    assert activity(f.store.state, 'organisation', now=now)['totals']['alerts_sent'] == 1
