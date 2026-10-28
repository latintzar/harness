"""The shared membership authority exposes no vertical graph or source policy."""
from pathlib import Path

import pytest


def test_shared_membership_is_inert_and_rechecks_actor_role(tmp_path):
    from business_scope import WorkspaceAuthority, ScopeActor, ScopeError
    members={'alice':{'role':'owner'},'bob':{'role':'operator'},'reader':{'role':'browse'}}
    authority=WorkspaceAuthority(tmp_path,company_id='workspace',member=members.get)
    alice=ScopeActor('workspace','alice')
    assert authority.authority(alice,write=True)=={'role':'owner'}
    assert authority.authority(ScopeActor('workspace','reader'))=={'role':'browse'}
    for actor in (ScopeActor('elsewhere','alice'),ScopeActor('workspace','missing'),
                  ScopeActor('workspace','reader'),ScopeActor('workspace','alice',True)):
        with pytest.raises(ScopeError):authority.authority(actor,write=True)
    members.pop('alice')
    with pytest.raises(ScopeError):authority.authority(alice)
    assert not list(Path(tmp_path).iterdir())
    assert not any(hasattr(authority,name) for name in ('_graph','mutate','camera','source_authorized','chat_scope','admit_history'))
