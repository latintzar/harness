"""Stream funded captions into the existing scoped evidence presentation event."""
import asyncio
import copy
from evidence_captions import EvidenceCaptions, CaptionScope, CaptionUnavailable


async def enrich(agent, chat_id, run, collection, index, presentation):
    record=agent.children[chat_id]
    owner=record.get('owner_principal_id')
    principal=getattr(agent,'_authority_principal',lambda _:owner)(chat_id)
    member=getattr(agent,'messaging_member',lambda _:None)
    def current():
        live=agent.children.get(chat_id,{})
        check=getattr(index,'business_scope_current',None)
        return (agent.active.get(chat_id) is run and not run['cancel'].is_set()
            and live.get('owner_principal_id')==owner and live.get('dataset')==collection
            and getattr(agent,'_authority_principal',lambda _:owner)(chat_id)==principal
            and (check is None or check()))
    messages=record.get('messages',[])
    question=next((m.get('content','') for m in reversed(messages) if m.get('role')=='user'),'')
    if not isinstance(question,str) or not question.strip() or not current():return presentation
    factory=getattr(agent,'evidence_caption_factory',None)
    if factory:
        service=factory()
    else:
        key=getattr(getattr(agent,'auth',None),'environment',{}).get('ROCOTO_OPENAI_API_KEY')
        if not key:return presentation
        try:
            service=EvidenceCaptions(agent.state,api_key=key,authorized=lambda scope:
                current() and scope.owner==owner and bool(member(principal)))
        except CaptionUnavailable:
            return presentation
    scope=CaptionScope(record['owner_principal_id'],chat_id,collection,run['generation'])
    queue=run.setdefault('tool_events',asyncio.Queue())
    for item in presentation['items']:
        if not current():raise asyncio.CancelledError
        original=copy.deepcopy(item)
        async def emit(event):
            if current() and event.get('type')=='evidence_caption_delta':
                item.update(caption=event['caption'],caption_kind='contextual_cosmos_summary',caption_status='streaming')
                queue.put_nowait({'type':'evidence_presentation','presentation':copy.deepcopy(presentation)})
        try:
            result=await service.caption(scope,index=index,clip_id=item['id'],question=question,
                current=current,emit=emit)
            item.update(caption=result['caption'],caption_kind=result['caption_kind'],
                caption_status='complete',caption_provenance=result)
        except CaptionUnavailable:
            item.clear();item.update(original)
        if not current():raise asyncio.CancelledError
        queue.put_nowait({'type':'evidence_presentation','presentation':copy.deepcopy(presentation)})
    return presentation
