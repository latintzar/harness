"""Pinned/recent histories belong to explicit principals; observation grants reading."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from access import AccessStore
from chat_permissions import ChatPermissions


def sessions(tmp_path):
    access=AccessStore(tmp_path/'access')
    alice_token,alice=access.login(access.issue('demo',principal_id='alice',display_name='Alice'))
    _,bob=access.login(access.issue('demo',principal_id='bob',display_name='Bob'))
    owner_token,owner=access.login(access.issue('demo',owner=True,principal_id='owner',display_name='Owner'))
    return access,alice,bob,owner_token,owner


def test_explicit_chat_ownership_and_observer_read_only_metadata(tmp_path):
    access,alice,bob,owner_token,owner=sessions(tmp_path)
    store=ChatPermissions(tmp_path/'workspace')
    records={'a':{'id':'a','owner_principal_id':'alice','messages':[]},
        'b':{'id':'b','owner_principal_id':'bob','messages':[]},
        'legacy':{'id':'legacy','messages':[]}}
    assert [row['id'] for row in store.visible_records(records,alice)]==['a']
    assert store.visible_records(records,owner)==[]
    with pytest.raises(ValueError):store.get(records['b'],alice)
    with pytest.raises(ValueError):store.get(records['legacy'],owner)
    pinned=store.update(records['a'],alice,pinned=True,expected_revision=0)
    assert pinned['pinned'] is True and pinned['metadata_revision']==1
    observer=access.observe(owner_token,'demo','alice')
    view=ChatPermissions(tmp_path/'workspace').get(records['a'],observer)
    assert view['read_only'] is True and view['pinned'] is True
    with pytest.raises(ValueError):store.update(records['a'],observer,archived=True)
    with pytest.raises(ValueError):store.get(records['b'],observer)


def test_pin_archive_is_versioned_private_and_persistent(tmp_path):
    _,alice,bob,_,_=sessions(tmp_path);state=tmp_path/'workspace'
    store=ChatPermissions(state);record={'id':'a','owner_principal_id':'alice'}
    archived=store.update(record,alice,archived=True,expected_revision=0)
    assert archived['metadata_revision']==1
    assert ChatPermissions(state).visible_records([record],alice)==[]
    assert store.visible_records([record],alice,include_archived=True)[0]['archived'] is True
    with pytest.raises(ValueError):store.update(record,alice,pinned=True,expected_revision=0)
    with pytest.raises(ValueError):store.update(record,bob,pinned=True)
    assert ChatPermissions(tmp_path/'other-workspace').get(record,alice)['archived'] is False


def test_founder_history_assignment_is_explicit_scoped_and_preserves_answers(tmp_path):
    access,alice,bob,owner_token,owner=sessions(tmp_path)
    store=ChatPermissions(tmp_path/'workspace')
    records={'old':{'id':'old','messages':[{'role':'assistant','content':'Original answer'}],
        'report_sha256':'original-report'},'other':{'id':'other','owner_principal_id':'bob'}}
    note='Explicit founder authorization: all pre-customer Demo activity belongs to this owner.'
    with pytest.raises(ValueError):store.assign_founder_history(records,alice,chat_ids=['old'],authorization_note=note)
    with pytest.raises(ValueError):store.assign_founder_history(records,owner,chat_ids=['other'],authorization_note=note)
    assigned=store.assign_founder_history(records,owner,chat_ids=['old'],authorization_note=note)
    assert records['old'].get('owner_principal_id') is None
    assert assigned[0]['owner_principal_id']=='owner'
    assert assigned[0]['messages']==records['old']['messages']
    assert assigned[0]['report_sha256']=='original-report'
    assert store.get(assigned[0],owner)['id']=='old'
    assert store.assign_founder_history(assigned,owner,chat_ids=['old'],authorization_note=note)==assigned
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM ownership_assignments').fetchone()[0]==1
    observer=access.observe(owner_token,'demo','alice')
    with pytest.raises(ValueError):store.assign_founder_history(records,observer,chat_ids=['old'],authorization_note=note)


def test_legacy_attachment_assignment_requires_references_in_assigned_founder_chats(tmp_path):
    _,alice,_,_,owner=sessions(tmp_path)
    state=tmp_path/'workspace';store=ChatPermissions(state)
    directory=state/'attachments';directory.mkdir()
    key='a'*32;other='b'*32
    (directory/f'{key}.png').write_bytes(b'available legacy image')
    (directory/f'{other}.png').write_bytes(b'unreferenced image')
    records={'old':{'id':'old','owner_principal_id':'owner','messages':[{'attachments':[{'id':key}]}]}}
    note='Explicit founder Demo history authorization'
    with pytest.raises(ValueError):store.assign_founder_attachments(records,alice,chat_ids=['old'],authorization_note=note)
    assert store.assign_founder_attachments(records,owner,chat_ids=['old'],authorization_note=note)==[key]
    assert store.can_read_attachment(key,owner)
    assert not store.can_read_attachment(other,owner)
    assert not store.can_read_attachment(key,alice)
