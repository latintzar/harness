"""Host wiring for current business scope over existing Ready/owner authority.

There is no transport, credential store or compute admission here. The supplied
member callbacks belong to the application's owning AccessStore. Index inputs
must already be filtered for their actual data owner; scope can only remove rows.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import sqlite3

from fastapi import HTTPException

from business_scope import ScopeActor, ScopeError, identifier

from retail_business_scope import RetailBusinessScope
from retail_scope_views import selected_index_view
from chat_permissions import workspace_writable
from scope_host import conversation_root


def install_scope(selected, messaging_member, members):
    """Callbacks: member(selected, principal), members(selected), both current."""
    # The host has already initialized this tenant's portable identity and shared
    # chats. Reuse it so even setup here cannot create an identity on a read path.
    company=identifier(selected['organization_chats'].company_id)
    def source_authorized(actor,collection,source):
        if actor.company_id!=company or not messaging_member(selected,actor.principal_id):return False
        for view in _owned_views(selected,actor.principal_id,collection):
            if any(row.get('dataset')==collection and row.get('video_id')==source for row in view.clips):return True
        monitoring=selected.get('monitoring')
        return bool(monitoring and any(row['video_id']==source for row in monitoring.registered_sources(actor.principal_id,collection)))
    store=RetailBusinessScope(selected['state'],company_id=company,
        member=lambda principal:messaging_member(selected,principal),
        members=lambda:members(selected),source_authorized=source_authorized)
    selected['business_scope']=store
    selected['index'].scope_filter=lambda view,principal:filter_view(selected,view,principal)
    selected['agent'].business_scope_host=selected
    selected['agent'].business_scope_view=lambda view,principal,collection=None,chat_id=None:filter_view(
        selected,view,principal,collection,chat_id)
    import scope_host
    guard=lambda record,session:scope_host.history_allowed(selected,record,selected['chats'].principal(session))
    selected['chats'].history_authorized=guard
    selected['organization_chats'].history_authorized=guard
    selected["scope_policy"] = RetailScope(selected)
    return store


def _owned_views(selected,principal,collection):
    # Exact principal Ready snapshot protects private source receipts. Organization
    # ownership is separately admitted only through an existing shared chat ACL.
    yield selected['index'].snapshot(principal,apply_scope=False)
    organization=selected.get('organization_chats')
    if organization is None:return
    member=selected['business_scope'].member(principal)
    if not member or member.get('role') not in {'owner','operator'}:return
    session={'authenticated':True,'can_query':True,'role':member['role'],
        'principal':{'id':principal},'environment':{'id':selected['environment_id']}}
    if any(row.get('dataset')==collection and organization.can_read(row,session)
           for row in selected['agent'].children.values()):
        yield selected['index'].snapshot(organization.owner,apply_scope=False)


def filter_view(selected,view,principal,collection=None,chat_id=None,observer=False):
    """Return an immutable narrowed index with a synchronous current() fence.

    Aggregate HTTP reads call with collection=None. Native turns supply the fixed
    chat collection and host-resolved live person, independent of its data owner.
    ScopeError or contention produces an empty view whose fence is false. Callers
    check that fence before dispatch and after awaited work before publishing.
    """
    store=selected['business_scope'];effective=[];valid=False;selector_id=chat_id
    if getattr(view,'source_visibility_company',None)!=str(Path(selected['state']).absolute()):
        output=selected_index_view(view,[]);output.business_scope_current=lambda:False
        return output
    if principal is None:
        # Existing anonymous Demo browsing remains limited to legacy rev0 data.
        # Configuring a business closes public reads of that collection.
        try:
            keys={row['dataset'] for row in view.clips}
            def public_revisions():
                with store._db() as db:return {key:store._graph(db,key)[0] for key in keys}
            revisions=public_revisions()
            output=selected_index_view(view,[i for i,row in enumerate(view.clips)
                if not revisions[row['dataset']] and (collection is None or row['dataset']==collection)])
            output.business_scopes=[]
            def public_current():
                try:return public_revisions()==revisions
                except (OSError,sqlite3.Error,ScopeError):return False
            output.business_scope_current=public_current
            return output
        except (OSError,sqlite3.Error,ScopeError):
            output=selected_index_view(view,[]);output.business_scope_current=lambda:False
            return output
    try:
        if getattr(view,'source_visibility_company',None)!=str(Path(selected['state']).absolute()):
            raise ScopeError('scope_source_company_required')
        actor=ScopeActor(store.company_id,principal,observer)
        member=store.authority(actor)
        organization=selected.get('organization_chats')
        data_owner=getattr(view,'source_visibility_principal',None)
        if data_owner!=principal and not (organization is not None and data_owner==organization.owner):
            raise ScopeError('scope_source_owner_required')
        def shared_collections():
            session={'authenticated':True,'can_query':True,'role':member['role'],
                'principal':{'id':principal},'environment':{'id':selected['environment_id']}}
            return {row.get('dataset') for row in selected['agent'].children.values()
                if organization.can_read(row,session)}
        shared_allowed=shared_collections() if data_owner!=principal else None
        if shared_allowed is not None and (not shared_allowed or collection is not None and collection not in shared_allowed):
            raise ScopeError('scope_source_owner_required')
        if collection is not None:identifier(collection)
        if chat_id is not None:
            record=selected['agent'].children.get(chat_id)
            if not isinstance(record,dict) or record.get('environment_id',selected['environment_id'])!=selected['environment_id']:
                raise ScopeError('scope_chat_unavailable')
            if record.get('dataset') is not None:
                if collection is not None and collection!=record['dataset']:raise ScopeError('scope_chat_business_fixed')
                collection=record['dataset']
            # Native children are owned by the same host root and original owner.
            if record.get('owner_principal_id')!=principal and not (
                    organization is not None and organization.owns(record)):
                raise ScopeError('scope_chat_unavailable')
            selector_id, root = conversation_root(selected['agent'], record)
            if (not isinstance(root,dict) or root.get('owner_principal_id')!=record.get('owner_principal_id')
                    or root.get('dataset')!=record.get('dataset')):
                raise ScopeError('scope_chat_unavailable')
            binding=(record.get('owner_principal_id'),record.get('dataset'),selector_id)
        collections={collection} if collection is not None else {
            row.get('dataset') for row in view.clips if isinstance(row.get('dataset'),str)}
        if shared_allowed is not None:
            collections &= shared_allowed
            if not collections:raise ScopeError('scope_source_owner_required')
        # Include the selected empty business so a first graph mutation invalidates
        # already admitted work even when that business currently has no footage.
        if not collections:collections={'company'}
        for key in sorted(collections):
            value=store.chat_scope(actor,selector_id,key) if selector_id is not None and collection is not None else store.effective(actor,key)
            effective.append(value)
        def current():
            if shared_allowed is not None and not collections<=shared_collections():return False
            if chat_id is not None:
                live=selected['agent'].children.get(chat_id,{})
                current_root=selected['agent'].children.get(selector_id,{})
                if ((live.get('owner_principal_id'),live.get('dataset'),
                        conversation_root(selected['agent'], live)[0])!=binding
                        or (current_root.get('owner_principal_id'),current_root.get('dataset'))!=binding[:2]):return False
            return all(store.current(actor,value['collection'],value['revision'],account_role=member['role'],
                chat_id=selector_id if 'selection_revision' in value else None,
                selection_revision=value.get('selection_revision')) for value in effective)
        by_collection={value['collection']:value for value in effective}
        positions=[]
        for position,row in enumerate(view.clips):
            value=by_collection.get(row.get('dataset'))
            if value is None:continue
            # Before a graph exists, preserve the already admitted Ready snapshot.
            # First configuration switches non-owner members to explicit grants.
            if not value['configured'] or value['can_query'] and (
                    value['includes_unassigned'] or row.get('video_id') in value['source_ids']):
                positions.append(position)
        valid=current()
        output=selected_index_view(view,positions if valid else [])
    except (ScopeError,ValueError,OSError,sqlite3.Error):
        output=selected_index_view(view,[])
        current=lambda:False
    output.business_scopes=effective
    output.business_scope_current=current if valid else lambda:False
    return output


def scope_current(view):
    """Explicitly fail closed for unbound/invalid views at host dispatch seams."""
    check=getattr(view,'business_scope_current',None)
    try:return callable(check) and check() is True
    except Exception:return False



def history_allowed(selected,record,principal):
    """Whole-chat admission: historical context never widens current membership."""
    try:
        store=selected['business_scope'];actor=ScopeActor(store.company_id,principal)
        member=store.authority(actor)
        root_id, root = conversation_root(selected['agent'], record)
        if record.get('dataset') is not None and root.get('dataset')!=record['dataset']:return False
        receipt=store.history(root_id)
        keys=set(receipt['scopes']) if receipt else set()
        if root.get('dataset'):keys.add(root['dataset'])
        else:
            with store._db() as db:
                if db is not None:keys.update(row[0] for row in db.execute('SELECT collection FROM graphs'))
        scopes={key:store.effective(actor,key) for key in keys}
        configured={key:value for key,value in scopes.items() if value['configured']}
        if not configured:return True
        if receipt is None or receipt.get('legacy'):
            return not (root.get('messages') or record.get('report_messages')) or member['role']=='owner'
        owner=root.get('owner_principal_id')
        source=selected['index'].snapshot(owner,apply_scope=False)
        for key,effective in configured.items():
            if not effective['can_query']:return False
            required=receipt['scopes'].get(key,{})
            if any(not set(required.get(field,[]))<=set(effective[field]) for field in ('location_ids','room_ids','function_ids','camera_ids')):return False
            available={row['video_id'] for row in source.clips if row['dataset']==key and
                (effective['includes_unassigned'] or row['video_id'] in effective['source_ids'])}
            monitoring=selected.get('monitoring')
            if monitoring:
                available.update(row['video_id'] for row in monitoring.registered_sources(owner,key) if effective['includes_unassigned'] or row['video_id'] in effective['source_ids'])
            if not set(receipt['sources'].get(key,[]))<=available:return False
        return all(store.current(actor,key,value['revision'],account_role=member['role']) for key,value in scopes.items())
    except (ScopeError,ValueError,TypeError,KeyError,OSError,sqlite3.Error):return False


def turn_actor(agent,chat_id,sender=None):
    record=agent.children.get(chat_id,{})
    organization=getattr(agent,'organization_chats',None)
    if organization is not None and organization.owns(record):
        root, _ = conversation_root(agent, record)
        actual=sender or getattr(agent,'_turn_senders',{}).get(root,{})
        return actual.get('principal_id') if isinstance(actual,dict) else None
    return record.get('owner_principal_id')


@asynccontextmanager
async def agent_scope(agent,chat_id,dataset=None,*,sender=None,admit=False):
    """Fresh tool authority, cumulative history admission and native isolation."""
    selected=getattr(agent,'business_scope_host',None)
    if selected is None:
        yield None;return
    from custom_coordinator import offload
    from execution_policy import RETRIEVAL_VIEW
    # Preserve rev0 admission timing and avoid creating a scope DB merely for a
    # chat. Absent-state reads follow the existing synchronous Ready path; once
    # a graph exists, its bounded storage work is drained off the event loop.
    legacy=not selected['business_scope'].path.exists()
    async def read(function,*args,**kwargs):
        return function(*args,**kwargs) if legacy else await offload(function,*args,**kwargs)
    principal=turn_actor(agent,chat_id,sender)
    record=agent.children.get(chat_id)
    if not record or not await read(history_allowed,selected,record,principal):
        raise ScopeError('Open a conversation permitted by your current location assignments.')
    root_id, root = conversation_root(agent, record)
    # Preserve an upstream host admission (scheduler window/candidate subset).
    # Ownership/company are checked by filter_view; no fresh snapshot may widen it.
    source=RETRIEVAL_VIEW.get()
    admitted=getattr(agent,'_business_scope_runs',{}).get(root_id)
    if not admit and admitted is not None:
        if not scope_current(admitted):raise ScopeError('The business scope changed during this request.')
        if source is None:source=admitted
        else:
            permitted={row['id'] for row in admitted.clips}
            source=selected_index_view(source,[i for i,row in enumerate(source.clips) if row['id'] in permitted])
    if source is None:
        source=await read(selected['index'].snapshot,record.get('owner_principal_id'),apply_scope=False)
    view=await read(filter_view,selected,source,principal,dataset,chat_id)
    if not scope_current(view):raise ScopeError('Refresh the current business scope before continuing.')
    previous_view=getattr(agent,'_business_scope_runs',{}).get(root_id)
    if not admit and previous_view is not None and not scope_current(previous_view):
        raise ScopeError('The business scope changed during this request.')
    if admit:
        scopes=view.business_scopes
        configured=any(value['configured'] for value in scopes)
        if any(value['configured'] and not value['can_query'] for value in scopes):
            raise ScopeError('Ask an organization administrator for your location assignments.')
        if configured and getattr(agent,'managed_runtime',None) is None:
            raise ScopeError('Use the managed workspace for location-scoped conversations.')
        if configured:
            context=[]
            for value in scopes:
                description=await read(selected['business_scope'].describe,ScopeActor(selected['business_scope'].company_id,principal),value['collection'])
                context.append({'collection':value['collection'],'revision':value['revision'],
                    'locations':[{'id':row['id'],'name':row['name']} for row in description['locations'] if row['id'] in value['location_ids']],
                    'functions':[{'id':row['id'],'name':row['name']} for row in description['functions'] if row['id'] in value['function_ids']],
                    'room_ids':value['room_ids'],'camera_ids':value['camera_ids'],
                    'can_query':value['can_query'],'selected_location_ids':value.get('selected_location_ids'),
                    'available_recordings':len({row['video_id'] for row in view.clips if row['dataset']==value['collection']})})
            view.business_scope_context=context
            sources={value['collection']:sorted({row['video_id'] for row in view.clips if row['dataset']==value['collection']}) for value in scopes}
            receipt=await read(selected['business_scope'].admit_history,
                ScopeActor(selected['business_scope'].company_id,principal),root_id,scopes,sources,
                legacy=bool(root.get('messages')))
            if not scope_current(view):raise ScopeError('Refresh the current business scope before continuing.')
            # Never migrate/inject old native or public history across a changed
            # authority, including first adoption of an unreceipted thread.
            if configured and receipt['reset']:
                previous=agent.registry.get(root_id)
                if previous:
                    root.setdefault('scope_continuations',[]).append({'thread_id':previous,'binding':root.get('scope_binding')})
                    agent._remember(root_id,None)
                root['scope_fresh_native']=True
            if configured:root['scope_workspace_binding']=receipt['binding']
            view.history_scope=receipt['binding']
            root['scope_binding']=receipt['binding']
            root['scope_receipt']={'binding':receipt['binding'],'principal_id':principal,
                'collections':[value['collection'] for value in scopes]}
            agent._save_child(root)
    token=RETRIEVAL_VIEW.set(view)
    prior=getattr(agent,'_business_scope_runs',{}).get(root_id)
    if not hasattr(agent,'_business_scope_runs'):agent._business_scope_runs={}
    if admit:agent._business_scope_runs[root_id]=view
    try:
        with selected['index'].bind(view):
            yield view
            if not scope_current(view):raise ScopeError('The business scope changed during this request.')
    finally:
        RETRIEVAL_VIEW.reset(token)
        if admit:
            if prior is None:agent._business_scope_runs.pop(root_id,None)
            else:agent._business_scope_runs[root_id]=prior


def active_scope_current(agent,chat_id):
    selected=getattr(agent,'business_scope_host',None)
    if selected is None:return True
    record=agent.children.get(chat_id,{})
    try:
        root_id, _ = conversation_root(agent, record)
    except ScopeError:
        return False
    view=getattr(agent,'_business_scope_runs',{}).get(root_id)
    return view is not None and scope_current(view)


def history_within_view(selected,record,view):
    """Old derived files/context require the whole admitted historical scope."""
    scopes={value['collection']:value for value in getattr(view,'business_scopes',[]) if value['configured']}
    if not scopes:return True
    try:
        root_id, _ = conversation_root(selected['agent'], record)
    except ScopeError:
        return False
    receipt=selected['business_scope'].history(root_id)
    if not receipt or receipt['legacy']:
        return all(value['includes_unassigned'] for value in scopes.values())
    for key,value in scopes.items():
        required=receipt['scopes'].get(key,{})
        if any(not set(required.get(field,[]))<=set(value[field]) for field in ('location_ids','room_ids','function_ids','camera_ids')):return False
        available={row['video_id'] for row in view.clips if row['dataset']==key}
        if not set(receipt['sources'].get(key,[]))<=available:return False
    return True



class RetailScope:
    def __init__(self, selected):
        self.selected = selected

    def history_allowed(self, record, principal):
        return history_allowed(self.selected, record, principal)

    def filter_view(self, view, principal, collection=None, chat_id=None, observer=False):
        return filter_view(self.selected, view, principal, collection, chat_id, observer)

    def owned_views(self, principal, collection):
        return _owned_views(self.selected, principal, collection)

    def history_within_view(self, record, view):
        return history_within_view(self.selected, record, view)

    def source_options(self, principal, collection):
        options = {}
        for source in self.owned_views(principal, collection):
            visible = self.filter_view(source, principal, collection)
            if not scope_current(visible):
                raise ScopeError('scope_authority_changed')
            for row in visible.clips:
                key = row.get('video_id')
                if isinstance(key, str) and len(options) < 500:
                    options.setdefault(key, {'id': key, 'name': str(row.get('camera') or key)[:200]})
        return list(options.values())

    def agent_scope(self, agent, chat_id, dataset=None, *, sender=None, admit=False):
        return agent_scope(agent, chat_id, dataset, sender=sender, admit=admit)

    @asynccontextmanager
    async def request_scope(self, request):
        selected = self.selected
        principal = selected['chats'].principal(request.state.session)
        try:
            view = await asyncio.to_thread(selected['index'].snapshot, principal, refresh=True)
        except (ValueError, OSError):
            view = await asyncio.to_thread(selected['index'].snapshot, principal)
        request.state.scope_views = [view]
        with selected['index'].bind(view, check_exit=False):
            yield view

    def bind(self, view):
        from contextlib import contextmanager
        from execution_policy import RETRIEVAL_VIEW
        @contextmanager
        def bound():
            token = RETRIEVAL_VIEW.set(view)
            try:
                with self.selected['index'].bind(view):
                    yield
            finally:
                RETRIEVAL_VIEW.reset(token)
        return bound()
