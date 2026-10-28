"""Moving private state preserves scope; creating a different company is explicit."""
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from workspace_identity import initialize_new_workspace, onboarding_identity, workspace_scope
from retail_business import RetailBusinessStore as BusinessStore
from company_usage import CompanyUsage
from skills import SkillStore


def _scope(path):
    return workspace_scope(path)


def test_initial_adoption_preserves_old_scope_and_onboarding_ids(tmp_path):
    state=tmp_path/'existing-state'
    expected=hashlib.sha256(str(state.resolve()).encode()).hexdigest()
    expected_chat=hashlib.sha256((str(state.resolve())+'\0company').encode()).hexdigest()[:24]
    assert workspace_scope(state)==expected
    assert onboarding_identity(state,'company')==expected_chat
    identity=state/'.workspace-identity.json'
    assert identity.stat().st_mode&0o777==0o600
    assert CompanyUsage(state).company_scope==SkillStore(state).company_scope==expected


def test_relocation_preserves_funding_binding_skill_snapshot_and_initial_chat(tmp_path):
    source=tmp_path/'dev-state'
    usage=CompanyUsage(source)
    pool=CompanyUsage.openai_pool(source)
    skills=SkillStore(source)
    skill=skills.install('search-planning')
    snapshot=skills.resolve([skill['id']],available_tools=skill['required_tools'])
    index=type('Index',(),{'clips':[]})()
    business=BusinessStore(source,index)
    before=business.get('company')
    # There is deliberately no saved profile revision: initial onboarding IDs
    # must also survive relocation, not just IDs already copied into SQLite.
    target=tmp_path/'var-lib'/'company'
    shutil.copytree(source,target)
    assert CompanyUsage(target).company_scope==usage.company_scope
    migrated_pool=CompanyUsage.openai_pool(target)
    assert migrated_pool.company_scope==pool.company_scope
    assert migrated_pool.summary()['parent_company_scope']==usage.company_scope
    assert SkillStore(target).resolve([skill['id']],available_tools=skill['required_tools'])==snapshot
    assert BusinessStore(target,index).get('company')==before


def test_parallel_adoption_publishes_one_complete_identity(tmp_path):
    state=tmp_path/'company'
    with ProcessPoolExecutor(max_workers=4) as processes:
        scopes=list(processes.map(_scope,[state]*12))
    assert len(set(scopes))==1
    assert json.loads((state/'.workspace-identity.json').read_text())['company_scope']==scopes[0]


def test_explicit_new_company_cannot_reidentify_copied_history(tmp_path):
    old=tmp_path/'old';original=workspace_scope(old)
    copied=tmp_path/'copy';shutil.copytree(old,copied)
    with pytest.raises(ValueError):initialize_new_workspace(copied)
    assert workspace_scope(copied)==original
    fresh=tmp_path/'new'
    created=initialize_new_workspace(fresh)
    assert workspace_scope(fresh)==created['company_scope']
    assert workspace_scope(fresh)!=original
    assert workspace_scope(fresh)!=hashlib.sha256(str(fresh.resolve()).encode()).hexdigest()
    occupied=tmp_path/'occupied';occupied.mkdir();(occupied/'company-usage.sqlite').write_bytes(b'')
    with pytest.raises(ValueError):initialize_new_workspace(occupied)


@pytest.mark.parametrize('kind',['malformed','public','symlink'])
def test_existing_invalid_identity_fails_closed_without_replacement(tmp_path,kind):
    state=tmp_path/'company';workspace_scope(state)
    path=state/'.workspace-identity.json'
    if kind=='malformed':path.write_text('{broken')
    if kind=='public':path.chmod(0o644)
    if kind=='symlink':
        original=tmp_path/'other.json';shutil.copy2(path,original)
        path.unlink();path.symlink_to(original)
    before=path.read_bytes()
    with pytest.raises(ValueError):workspace_scope(state)
    assert path.read_bytes()==before
