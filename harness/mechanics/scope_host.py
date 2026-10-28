"""Common workspace membership, conversation authority and resource-policy admission."""
from __future__ import annotations
import asyncio
from contextvars import ContextVar
from contextlib import asynccontextmanager, contextmanager, nullcontext
import sqlite3

from business_scope import WorkspaceAuthority, ScopeActor, ScopeError, identifier
from host import Admission

_ADMISSION = ContextVar("workspace_admission", default=None)

def current_admission():
    return _ADMISSION.get()

@contextmanager
def bind_admission(admission):
    token=_ADMISSION.set(admission)
    try:
        with admission.bind():
            yield admission
    finally:
        _ADMISSION.reset(token)


def setup_scope(selected, member, members):
    company=identifier(selected['organization_chats'].company_id)
    selected['business_scope']=WorkspaceAuthority(selected['state'],company_id=company,
        member=lambda principal:member(selected,principal), members=lambda:members(selected))
    selected['agent'].business_scope_host=selected
    guard=lambda record,session:history_allowed(selected,record,selected['chats'].principal(session))
    selected['chats'].history_authorized=guard
    selected['organization_chats'].history_authorized=guard
    return selected['business_scope']


def scope_current(value):
    check=getattr(value,'current',None) or getattr(value,'business_scope_current',None)
    try:return callable(check) and check() is True
    except Exception:return False


def conversation_root(agent, record):
    """Resolve saved descendants through a bounded, same-owner ancestry chain."""
    if not isinstance(record, dict):
        raise ScopeError('Open a conversation in your current workspace.')
    scope = (record.get('owner_principal_id'), record.get('dataset'))
    seen, declared = set(), set()
    current = record
    for _ in range(256):
        key = current.get('chat_id') or current.get('id')
        if not isinstance(key, str) or key in seen or (current.get('owner_principal_id'), current.get('dataset')) != scope:
            raise ScopeError('Open a conversation in your current workspace.')
        seen.add(key)
        hints = [current.get(field) for field in ('managed_root_id', 'native_root_id') if current.get(field)]
        if any(not isinstance(value, str) for value in hints):
            raise ScopeError('Open a conversation in your current workspace.')
        declared.update(hints)
        parent = current.get('parent_id') or next((value for value in hints if value != key), None)
        if parent is None:
            if declared and declared != {key}:
                raise ScopeError('Open a conversation in your current workspace.')
            return key, current
        if not isinstance(parent, str):
            raise ScopeError('Open a conversation in your current workspace.')
        current = agent.children.get(parent)
        if not isinstance(current, dict):
            raise ScopeError('Open a conversation in your current workspace.')
    raise ScopeError('Open a conversation in your current workspace.')


def history_allowed(selected,record,principal):
    try:
        store=selected['business_scope']
        store.authority(ScopeActor(store.company_id,principal))
        conversation_root(selected['agent'], record)
        policy=selected.get('scope_policy')
        return policy.history_allowed(record,principal) if policy is not None else True
    except (ScopeError,ValueError,TypeError,KeyError,OSError,sqlite3.Error):
        return False


def turn_actor(agent,chat_id,sender=None):
    record=agent.children.get(chat_id,{})
    organization=getattr(agent,'organization_chats',None)
    if organization is not None and organization.owns(record):
        root, _ = conversation_root(agent, record)
        actual=sender or getattr(agent,'_turn_senders',{}).get(root,{})
        return actual.get('principal_id') if isinstance(actual,dict) else None
    return record.get('owner_principal_id')


def _conversation_authority(agent, chat_id, dataset, sender):
    selected=agent.business_scope_host
    record=agent.children.get(chat_id)
    principal=turn_actor(agent,chat_id,sender)
    if (not isinstance(record,dict) or not principal
            or record.get('environment_id',selected['environment_id'])!=selected['environment_id']):
        raise ScopeError('Open a conversation in your current workspace.')
    root_id, root = conversation_root(agent, record)
    owner=record.get('owner_principal_id')
    collection=record.get('dataset')
    organization=selected.get('organization_chats')
    shared=bool(organization and organization.owns(record))
    if ((owner!=principal and not shared) or dataset is not None and dataset!=collection
            or root.get('owner_principal_id')!=owner or root.get('dataset')!=collection):
        raise ScopeError('Open a conversation in your current workspace.')
    member=selected['business_scope'].authority(ScopeActor(selected['business_scope'].company_id,principal))
    requester=(sender or {}).get('principal_id') if isinstance(sender,dict) else principal
    requester=requester or principal
    request_member=selected['business_scope'].member(requester)
    phone=getattr(agent,'phone_chat',None)
    participant=requester!=principal
    if not request_member or participant and not (phone and phone.participant_chat(chat_id,requester)):
        raise ScopeError('Open a conversation in your current workspace.')
    binding=(owner,collection,root_id)
    def current():
        live=agent.children.get(chat_id,{})
        current_root=agent.children.get(root_id,{})
        current_member=selected['business_scope'].member(principal)
        live_requester=selected['business_scope'].member(requester)
        automation=getattr(agent,'automation_store',None)
        signal_id=current_root.get('active_signal_id')
        if signal_id and (automation is None or not automation.wakes.signal_current(signal_id)):
            return False
        background_run=current_root.get('active_run_id')
        if background_run and (automation is None or not automation.execution_current(background_run)):
            return False
        return bool(current_member and current_member.get('role')==member['role']
            and live_requester and live_requester.get('role')==request_member.get('role')
            and (not participant or phone.participant_chat(chat_id,requester))
            and (live.get('owner_principal_id'),live.get('dataset'),conversation_root(agent, live)[0])==binding
            and (current_root.get('owner_principal_id'),current_root.get('dataset'))==binding[:2]
            and history_allowed(selected,live,principal))
    if not current():raise ScopeError('Refresh your current workspace access.')
    return root_id,current


@asynccontextmanager
async def agent_scope(agent,chat_id,dataset=None,*,sender=None,admit=False):
    selected=getattr(agent,'business_scope_host',None)
    if selected is None:
        yield None
        return
    root_id,current=_conversation_authority(agent,chat_id,dataset,sender)
    inherited=getattr(agent,'_harness_scope_runs',{}).get(root_id) or current_admission()
    if not admit and inherited is not None:
        if not inherited.current():raise ScopeError('The workspace access changed during this request.')
        own_current=current
        current=lambda:own_current() and inherited.current()
    policy=selected.get('scope_policy')
    @asynccontextmanager
    async def empty():
        yield None
    context=policy.agent_scope(agent,chat_id,dataset,sender=sender,admit=admit) if policy else empty()
    async with context as resource:
        grant=Admission(lambda:current() and (resource is None or scope_current(resource)),
            (lambda:policy.bind(resource)) if policy else nullcontext,
            getattr(resource,'business_scope_context',None),resource=resource,
            history_scope=getattr(resource,'history_scope',None))
        if not grant.current():raise ScopeError('Refresh your current workspace access.')
        runs=getattr(agent,'_harness_scope_runs',None)
        if runs is None:agent._harness_scope_runs=runs={}
        prior=runs.get(root_id)
        if admit:runs[root_id]=grant
        token = _ADMISSION.set(grant)
        try:
            yield grant
            if not grant.current():raise ScopeError('The workspace access changed during this request.')
        finally:
            _ADMISSION.reset(token)
            if admit:
                if prior is None:runs.pop(root_id,None)
                else:runs[root_id]=prior


def active_scope_current(agent,chat_id):
    if getattr(agent,'business_scope_host',None) is None:return True
    record=agent.children.get(chat_id,{})
    try:
        key, _ = conversation_root(agent, record)
    except ScopeError:
        return False
    grant=getattr(agent,'_harness_scope_runs',{}).get(key)
    return grant is not None and grant.current()


def filter_view(selected,view,principal,collection=None,chat_id=None,observer=False):
    policy=selected.get('scope_policy')
    if policy is None:raise ScopeError('This resource is unavailable in the selected workspace.')
    return policy.filter_view(view,principal,collection,chat_id,observer)


def _owned_views(selected,principal,collection):
    policy=selected.get('scope_policy')
    return policy.owned_views(principal,collection) if policy else ()


def history_within_view(selected,record,view):
    policy=selected.get('scope_policy')
    return bool(policy and policy.history_within_view(record,getattr(view,'_resource',view)))
