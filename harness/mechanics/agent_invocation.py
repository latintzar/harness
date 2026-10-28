"""One foreground chat can invoke one owned agent run with current host authority."""
from __future__ import annotations
import time

SPEC = {'type':'function','name':'invoke_agent',
    'description':'Start one requested run of an existing agent from this chat and return its persistent conversation card. Read get_agent first and use its current revision. This invokes one run; recurring schedule activation remains in its info controls.',
    'inputSchema':{'type':'object','properties':{'agent_id':{'type':'string'},
        'expected_revision':{'type':'integer','minimum':1}},
        'required':['agent_id','expected_revision'],'additionalProperties':False}}


def invoke(agent, chat_id, arguments, *, now=None):
    now = time.time() if now is None else now
    if not isinstance(arguments,dict) or set(arguments) != {'agent_id','expected_revision'}:
        raise ValueError('Choose the saved agent and its current revision.')
    run = agent.active.get(chat_id)
    origin = agent.children.get(chat_id,{})
    grant = getattr(agent,'invocation_grants',{}).get(chat_id,{})
    member = getattr(agent,'messaging_member',lambda principal:None)(grant.get('principal'))
    if (not run or run['cancel'].is_set() or origin.get('parent_id') or not member
            or member.get('role') not in {'owner','operator'}
            or grant.get('principal') != origin.get('owner_principal_id')
            or type(grant.get('expires_at')) not in (int,float) or grant['expires_at'] <= now):
        raise ValueError('Invoke the agent from your current authenticated chat.')
    store=agent.automation_store
    saved=store.get(arguments['agent_id'])
    from agent_kind_policy import has_participant_reach_context, declared_class_authority
    if has_participant_reach_context(agent,chat_id) and declared_class_authority(store,saved)!='participant_reach':
        raise ValueError('Use an agent granted external actions or your own chat to start this workflow.')
    if (saved.get('owner_principal_id') != grant['principal']
            or saved.get('dataset') != origin.get('dataset') or saved.get('chat_id') == chat_id
            or saved.get('revision') != arguments['expected_revision']):
        raise ValueError('Choose another agent in your current workspace and review its latest revision.')
    # Stable tool retries return the same activation, without buying another run.
    receipt=run.get('invoked_agent')
    if receipt:
        if receipt['id'] == saved['id'] and receipt['revision'] == saved['revision']:
            return receipt
        raise ValueError('Continue in the invoked agent conversation before starting another agent.')
    execution=agent.workspace_capabilities.execution(saved)
    if not execution['ready']:
        raise ValueError(execution.get('notice') or 'Connect the input required by this agent.')
    store.inputs.validate_resource_budget(saved)
    result=store.activate(saved['id'],grant['expires_at'],once=True,
        expected_revision=arguments['expected_revision'],now=now)
    run['invoked_agent']=result
    return result
