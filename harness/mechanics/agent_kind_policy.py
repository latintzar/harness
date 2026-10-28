"""Declared class authority follows saved conversations and native descendants.

Participant-reach classes message their own participants. Granted-effects classes
use the host's ordinary action grants. Class names belong to the selected vertical.
"""
from __future__ import annotations

from messaging_provider import CHANNELS


def messaging_channels(agent):
    declared=getattr(getattr(agent,'messaging_store',None),'channel_names',CHANNELS)
    return tuple(declared) if isinstance(declared,(tuple,list,set,frozenset)) else ()
PARTICIPANTS_ONLY = 'This agent’s phone messages reach its own participants.'
OWN_CONVERSATION = 'Send participant messages from this agent’s own saved conversation.'


def declared_class_authority(store, definition):
    classes=getattr(store,'agent_classes',{})
    kind=definition.get('agent_kind') if isinstance(definition,dict) else None
    entry=classes.get(kind) if isinstance(classes,dict) and isinstance(kind,str) else None
    authority=entry.get('authority') if isinstance(entry,dict) else None
    return authority if authority in {'participant_reach','granted_effects'} else None


def has_participant_reach_context(agent, chat_id):
    """An ancestor's declared restrictions survive both descendant mechanisms.

    Persisted class and host-owned presentation metadata can each restrict the
    context. Unknown declarations or incomplete ancestry keep effects restricted.
    Ordinary personal conversations have neither an agent identity nor a class.
    """
    children=getattr(agent,'children',{})
    store=getattr(agent,'automation_store',None)
    pending=[(chat_id,frozenset())];seen=set()
    while pending and len(seen)<64:
        key,path=pending.pop()
        if not isinstance(key,str) or not key or key in path or key not in children:return True
        if key in seen:continue
        seen.add(key)
        record=children[key]
        if not isinstance(record,dict):return True
        setup=record.get('agent_setup') or {}
        if not isinstance(setup,dict):return True
        for metadata in (record,setup):
            if metadata.get('agent_kind') is not None and declared_class_authority(store,metadata)!='granted_effects':
                return True
        agent_id=record.get('agent_id') or setup.get('agent_id')
        if agent_id:
            if not callable(getattr(store,'get',None)):return True
            try:saved=store.get(agent_id)
            except Exception:return True
            if declared_class_authority(store,saved)!='granted_effects':return True
        pending.extend((record[link],path|{key}) for link in ('parent_id','native_root_id') if record.get(link))
    return bool(pending)


def participant_recipient(agent, chat_id, sender):
    """Resolve the one member a participant-reach context may message: the turn's own participant.

    The host takes the member from the admitted turn sender. Tool arguments,
    alert payloads and model text carry no address and no member reference. A
    turn started by a schedule or an event has no human sender, so it uses the
    creator's binding and requires it enabled, under the installed vertical's
    current delivery authority. Every missing record refuses.
    """
    record = getattr(agent, 'children', {}).get(chat_id) or {}
    agent_id = record.get('agent_id')
    if (not isinstance(agent_id, str) or not agent_id
            or record.get('parent_id') or record.get('native_root_id')):
        raise ValueError(OWN_CONVERSATION)
    store = getattr(agent, 'phone_chat', None)
    member = sender.get('principal_id') if isinstance(sender, dict) else None
    current = getattr(agent, 'messaging_member', None)
    if store is None or not isinstance(member, str) or not callable(current) or not current(member):
        raise ValueError(PARTICIPANTS_ONLY)
    channel = sender.get('channel') if sender.get('channel') in messaging_channels(agent) else None
    host = bool(record.get('active_run_id'))
    if host:
        current_delivery=getattr(getattr(agent,'vertical',None),'participant_delivery_current',None)
        if not callable(current_delivery) or current_delivery(chat_id) is not True:
            raise ValueError('Use this run’s current participant delivery authority.')
    recipient = store.recipient(agent_id, chat_id, member, channel=channel, host=host)
    if not isinstance(recipient, dict) or recipient.get('member_id') != member or recipient.get('chat_id') != chat_id:
        raise ValueError(PARTICIPANTS_ONLY)
    return recipient
