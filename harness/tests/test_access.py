from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
from access import AccessStore,DAY
import pytest

def test_single_use_expiry_and_company_isolation(tmp_path):
    store=AccessStore(tmp_path)
    store.create_environment('client','Client company')
    code=store.issue('client',now=100)
    token,session=store.login(code,now=101)
    assert session['environment']['id']=='client'
    assert [e['id'] for e in session['environments']]==['client']
    with pytest.raises(ValueError): store.login(code,now=102)
    assert store.session(token,now=101+DAY-1)
    assert store.session(token,now=101+DAY) is None
    with pytest.raises(ValueError): store.switch(token,'demo')
    assert code not in store.path.read_bytes().decode('latin1')
    assert token not in store.path.read_bytes().decode('latin1')

def test_logout_revokes_all_chats_and_owner_can_switch(tmp_path):
    store=AccessStore(tmp_path)
    store.create_environment('client','Client company')
    token,session=store.login(store.issue('demo',owner=True))
    assert store.switch(token,'client')['environment']['id']=='client'
    store.logout(token)
    assert store.session(token) is None


def test_owner_observes_actual_principal_read_only_and_restores_own_workspace(tmp_path):
    store=AccessStore(tmp_path);store.create_environment('client','Client company')
    _,customer=store.login(store.issue('client',principal_id='customer-a',display_name='Customer A'))
    owner_token,owner=store.login(store.issue('demo',owner=True,principal_id='peter',display_name='Peter'))
    assert owner['role']=='owner'
    listed=store.principals(owner_token)
    assert any(p['id']=='customer-a' and any(e['id']=='client' for e in p['environments']) for p in listed)
    observed=store.observe(owner_token,'client','customer-a')
    assert observed['principal']['id']=='peter'
    assert observed['effective_principal_id']=='customer-a'
    assert observed['viewing_as']['read_only'] is True
    assert observed['can_query'] is False
    assert observed['read_only'] is True
    restored=AccessStore(tmp_path).clear_observer(owner_token)
    assert restored['environment']['id']=='demo'
    assert restored['viewing_as'] is None and restored['can_query'] is True
    assert restored['effective_principal_id']=='peter'


def test_operator_cannot_observe_even_with_multiple_environment_grants(tmp_path):
    import json
    from access import digest
    store=AccessStore(tmp_path);store.create_environment('client','Client')
    token,session=store.login(store.issue('demo',principal_id='operator-a',display_name='Operator'))
    with store.connect() as db:
        db.execute('UPDATE sessions SET grants=? WHERE hash=?',(json.dumps(['demo','client']),digest(token)))
    assert store.session(token)['role']=='operator'
    with pytest.raises(ValueError):store.principals(token)
    with pytest.raises(ValueError):store.observe(token,'demo','operator-a')


def test_observer_targets_must_belong_to_granted_environment_and_identity(tmp_path):
    store=AccessStore(tmp_path);store.create_environment('client','Client')
    _,person=store.login(store.issue('client',principal_id='customer-a',display_name='Customer A'))
    owner,_=store.login(store.issue('demo',owner=True,principal_id='owner',display_name='Owner'))
    store.create_environment('later','Later company')
    store.issue('later',principal_id='later-user',display_name='Later user')
    assert not any(p['id']=='later-user' for p in store.principals(owner))
    with pytest.raises(ValueError):store.observe(owner,'demo','customer-a')
    with pytest.raises(ValueError):store.observe(owner,'later','later-user')
    with pytest.raises(ValueError):store.observe(owner,'client','unknown-user')


def test_legacy_sessions_get_unknown_identity_without_owner_promotion(tmp_path):
    import json,sqlite3,time
    from access import digest
    path=tmp_path/'access.sqlite';db=sqlite3.connect(path)
    db.executescript('CREATE TABLE environments(id TEXT PRIMARY KEY,name TEXT NOT NULL);'
        'CREATE TABLE sessions(hash TEXT PRIMARY KEY,environment TEXT NOT NULL,grants TEXT NOT NULL,expires REAL NOT NULL,can_query INTEGER NOT NULL DEFAULT 0);')
    db.execute('INSERT INTO environments VALUES(?,?)',('demo','Demo company'))
    db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(digest('old-token'),'demo',json.dumps(['demo']),time.time()+1000,1))
    db.commit();db.close()
    store=AccessStore(tmp_path);session=store.session('old-token')
    assert session['role']=='operator'
    assert session['principal']['display_name']=='Legacy session'
    assert session['principal']['id'].startswith('legacy-')
    assert store.session('old-token')['principal']==session['principal']
