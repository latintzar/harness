"""Job recipes retain their declared revision and scoped evidence across restarts."""
import asyncio
import copy
import dataclasses
import json
import shutil
from types import SimpleNamespace

import pytest
import pack
from host import Admission
from job_recipes import instructions_for
from recipe_runs import RecipeRuns, call
from tool_families import ToolContext, build_registry
from workspace_identity import workspace_scope


def context(tmp_path):
    record={'id':'chat','owner_principal_id':'alice','dataset':'workspace','messages':[]}
    run={'cancel':asyncio.Event()}
    agent=SimpleNamespace(children={'chat':record},active={'chat':run},
        organization_chats=SimpleNamespace(canonical=lambda row:row is record))
    record['organization_purpose']='organization'
    return ToolContext(state=tmp_path,company_id=workspace_scope(tmp_path),actor='alice',data_owner='alice',
        collection='workspace',chat_id='chat',record=record,run=run,agent=agent,admission=Admission(lambda:True))


def test_onboarding_is_an_actual_job_with_declared_role_and_readable_tools():
    with pack.use('bare') as vertical:
        job=vertical.recipe('onboarding');role=vertical.sub_agent('workspace_reader')
        assert job['steps'][0]['agent']==role['id'] and role['limit_cents']==20
        assert job['instructions'].startswith('Start by reading')
        names=build_registry().read_names('hosted')
        assert set(role['tools'])<=names
        job['steps'].clear();role['tools'].clear()
        assert vertical.recipe('onboarding')['steps'] and vertical.sub_agent('workspace_reader')['tools']
        assert vertical.tile_sets()==[]


def test_job_progress_survives_restart_and_keeps_original_recipe_snapshot(tmp_path):
    ctx=context(tmp_path)
    with pack.use('bare'):
        saved=call(ctx,'begin_recipe',{'recipe_id':'onboarding'})
        later=copy.deepcopy(saved['recipe']);later['revision']='future';later['steps'][0]['title']='Changed'
        store=RecipeRuns(tmp_path)
        assert store.begin(ctx,later)==saved
        with pytest.raises(ValueError,match='prerequisites'):
            store.update(ctx,run_id=saved['id'],expected_revision=1,step_id='business',status='completed',note='Confirmed',evidence=['message:1'])
        with pytest.raises(ValueError,match='evidence'):
            store.update(ctx,run_id=saved['id'],expected_revision=1,step_id='workspace',status='completed',note='Read',evidence=[])
        for version,step in enumerate(saved['recipe']['steps'],1):
            saved=RecipeRuns(tmp_path).update(ctx,run_id=saved['id'],expected_revision=version,
                step_id=step['id'],status='completed',note='Observed the completion criterion.',evidence=['message:'+str(version)])
        assert saved['status']=='completed' and saved['recipe']['revision']=='onboarding-1'
        assert RecipeRuns(tmp_path).get(ctx,saved['id'])==saved
        assert all(row['reported_by']=='alice' for row in saved['steps'].values())
        assert call(ctx,'begin_recipe',{'recipe_id':'onboarding'})['id']!=saved['id']


def test_revision_and_current_authority_fence_updates_and_foreign_reads(tmp_path):
    ctx=context(tmp_path)
    with pack.use('bare'):
        store=RecipeRuns(tmp_path);saved=call(ctx,'begin_recipe',{'recipe_id':'onboarding'})
        fields=dict(run_id=saved['id'],expected_revision=1,step_id='workspace',status='running',note='Reading.',evidence=[])
        store.update(ctx,**fields)
        with pytest.raises(ValueError,match='revision'):store.update(ctx,**fields)
        foreign_record={**ctx.record,'owner_principal_id':'bob'}
        foreign=dataclasses.replace(ctx,data_owner='bob',actor='bob',record=foreign_record)
        with pytest.raises(ValueError,match='conversation'):store.get(foreign,saved['id'])
        assert store.list(foreign)==[]
        ctx.run['cancel'].set()
        with pytest.raises(ValueError,match='parent'):store.update(ctx,**{**fields,'expected_revision':2})
        assert store.get(ctx,saved['id'])['revision']==2


def test_only_canonical_organization_gets_the_onboarding_recipe(tmp_path):
    ctx=context(tmp_path)
    with pack.use('bare') as vertical:
        assert 'onboarding' in instructions_for(ctx.agent,ctx.chat_id,vertical)
        ctx.agent.organization_chats.canonical=lambda record:False
        assert instructions_for(ctx.agent,ctx.chat_id,vertical)==''
        with pytest.raises(ValueError,match='declared conversation'):
            call(ctx,'begin_recipe',{'recipe_id':'onboarding'})


@pytest.mark.parametrize('change', ['future_step','missing_role','role_tool','budget','launcher','extra','escaping_prompt'])
def test_invalid_jobs_refuse_before_execution(tmp_path,change):
    root=tmp_path/'bare';shutil.copytree(pack.PACKS/'bare',root)
    roles=json.loads((root/'sub_agents.json').read_text())
    jobs=json.loads((root/'recipes/onboarding.json').read_text())
    if change=='future_step':jobs[0]['steps'][0]['after']=['setup']
    if change=='missing_role':jobs[0]['steps'][0]['agent']='invented'
    if change=='role_tool':jobs[0]['steps'][0]['tools']=['write_connection']
    if change=='budget':roles[0]['limit_cents']=100
    if change=='launcher':roles[0]['launched_by']=['invented']
    if change=='extra':jobs[0]['execute_shell']='anything'
    if change=='escaping_prompt':roles[0]['instructions']='../private.md'
    (root/'sub_agents.json').write_text(json.dumps(roles))
    (root/'recipes/onboarding.json').write_text(json.dumps(jobs))
    with pytest.raises(pack.PackError):pack.load(root)
