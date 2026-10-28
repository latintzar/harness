"""Conversation-bound tools for signed webhooks and finite self-set wakes."""
import sys
import time
from signal_chain import CHAIN_SCHEMA

READ_NAMES=frozenset({'list_wakes'})
WRITE_NAMES=frozenset({'create_webhook','set_wake','cancel_wake','register_endpoint','send_webhook','revoke_endpoint'})
ASYNC_NAMES=frozenset({'send_webhook'})
SURFACE_READS={surface:frozenset() for surface in ('public','native','local')}


def specs(write=True):
    label={'type':'string','minLength':1,'maxLength':80}
    rows=[{'name':'list_wakes','description':'Read this conversation’s webhook and timer grants, expiry and limits.',
        'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]
    if write:
        fields={
            'create_webhook':('Create a signed webhook for this conversation. Copy its signing secret once in the app.',
                {'label':label,'days':{'type':'integer','minimum':1,'maximum':30},
                 'signature':{'type':'string','enum':['hmac_sha256','bearer']},'chain':CHAIN_SCHEMA},['label']),
            'set_wake':('Set a future message for this conversation within the current actor’s finite grant.',
                {'label':label,'note':{'type':'string','minLength':1,'maxLength':8000},
                 'delay_seconds':{'type':'integer','minimum':60,'maximum':604800},
                 'every_seconds':{'type':'integer','minimum':300,'maximum':604800},
                 'fires':{'type':'integer','minimum':1,'maximum':50},'chain':CHAIN_SCHEMA},['label','note']),
            'register_endpoint':('Register a signed outgoing endpoint during the workspace owner’s conversation turn.',
                {'label':label,'url':{'type':'string','minLength':1,'maxLength':2048},
                 'daily_sends':{'type':'integer','minimum':1,'maximum':200}},['label','url']),
            'send_webhook':('Send one signed event to this conversation’s registered endpoint.',
                {'endpoint_id':{'type':'string','pattern':'^endpoint-[a-f0-9]{32}$'},
                 'event':{'type':'string','pattern':'^[a-z][a-z0-9_.]{0,63}$'},'payload':{'type':'object'}},
                ['endpoint_id','event','payload']),
            'revoke_endpoint':('Revoke this conversation’s registered outgoing endpoint.',
                {'id':{'type':'string','pattern':'^endpoint-[a-f0-9]{32}$'}},['id']),
            'cancel_wake':('Revoke one of this conversation’s webhook or timer grants.',
                {'id':{'type':'string','pattern':'^(hook|timer)-[a-f0-9]{32}$'}},['id'])}
        rows.extend({'name':name,'description':description,'inputSchema':{'type':'object','properties':properties,
            'required':required,'additionalProperties':False}} for name,(description,properties,required) in fields.items())
    return rows


def _sources(context):
    context.require_current()
    if not context.agent or not context.chat_id:raise ValueError('Open the owning conversation to inspect its wakes.')
    service=getattr(context.agent,'signal_sources',None)
    if service is None:raise ValueError('Install the workspace wake service.')
    return service


def read(context,name,arguments):
    service=_sources(context)
    return {'sources':service.list(actor=context.actor,chat_id=context.chat_id,collection=context.collection),
        'daily_limits':service.store.signals.usage(),'steps':service.store.signals.declarations(),
        'endpoints':_endpoints(context).list(actor=context.actor,chat_id=context.chat_id)
            if getattr(context.agent,'signal_endpoints',None) is not None else []}


def call(context,name,arguments):
    context.require_parent()
    if name=='register_endpoint':return _endpoints(context).register(context,**arguments)
    if name=='revoke_endpoint':return _endpoints(context).revoke(arguments['id'],actor=context.actor,chat_id=context.chat_id)
    service=_sources(context)
    if name=='cancel_wake':return service.cancel(arguments['id'],actor=context.actor,chat_id=context.chat_id)
    sender=getattr(context.agent,'_turn_senders',{}).get(context.chat_id,{})
    inherited=sender.get('authority_until')
    if sender.get('channel') in {'timer','webhook'}:
        if name=='create_webhook':raise ValueError('Create a webhook during a person’s conversation turn.')
        source=service.get(sender.get('wake_source_id'))
        inherited=source['authority_until']
        if not service.current(source) and not (source['kind']=='timer' and source['status']=='done'
                and inherited>time.time()):raise ValueError('Continue within the original wake authority.')
    elif context.record.get('active_run_id'):
        if name=='create_webhook':raise ValueError('Create a webhook during a person’s conversation turn.')
        run=service.store.run_report(context.record['agent_id'],context.record['active_run_id'])
        inherited=run['expires_at']
    identity={'owner':context.data_owner,'actor':context.actor,'chat_id':context.chat_id,
        'collection':context.collection,'authority_until':inherited}
    if name=='create_webhook':return service.create_hook(**identity,**arguments)
    if name=='set_wake':return service.set_timer(**identity,**arguments)
    raise ValueError('Choose a registered wake operation.')


def _endpoints(context):
    context.require_current()
    service=getattr(context.agent,'signal_endpoints',None)
    if service is None:raise ValueError('Install this workspace’s outgoing endpoint service.')
    return service


async def acall(context,name,arguments):
    if name!='send_webhook':raise ValueError('Choose a registered outgoing webhook operation.')
    from managed_turn import current_managed_call
    context.require_parent()
    return await _endpoints(context).send(context,**arguments,call_identity=current_managed_call())


FAMILY=sys.modules[__name__]
