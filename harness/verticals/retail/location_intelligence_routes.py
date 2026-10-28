"""Authenticated location projections backed by the existing location grants."""
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from business_scope import ScopeActor


def register_location_intelligence_routes(app):
    from retail_urls import retail_router
    app = retail_router(app)
    @app.get('/api/retail/location')
    async def location(request:Request,collection:str|None=None):
        selected=request.state.workspace
        owner=selected['chats'].principal(request.state.session)
        if not owner:raise HTTPException(403,'Open your workspace to view its locations.')
        # The snapshot reads the whole location; it runs beside the event loop so other requests keep moving.
        try:result=await run_in_threadpool(selected['location_intelligence'].snapshot,owner,collection)
        except ValueError as exc:raise HTTPException(403,'Choose an available location.') from exc
        for receipt in result.pop('scope_receipts',[]):
            actor=ScopeActor(selected['business_scope'].company_id,owner)
            request.state.scope_guards.append(lambda receipt=receipt,actor=actor:selected['business_scope'].current(actor,receipt['collection'],receipt['revision'],account_role=receipt['account_role']))
        return result

    @app.get('/api/retail/catalog/products')
    async def catalog(request:Request,collection:str,location_id:str):
        selected=request.state.workspace;owner=selected['chats'].principal(request.state.session)
        if not owner:raise HTTPException(403,'Open your workspace to view its catalog.')
        try:
            store=selected['location_intelligence'];view=store.authorized(owner,collection,location_id)
            products=store.products(owner,collection,location_id)
        except ValueError as exc:raise HTTPException(403,'Choose an available location.') from exc
        actor=ScopeActor(selected['business_scope'].company_id,owner)
        request.state.scope_guards.append(lambda:selected['business_scope'].current(actor,collection,view['revision'],account_role=view['effective']['account_role']))
        return {'products':products,'location_id':location_id,'collection':collection}

    if any(getattr(route,'path',None)=='/api/retail/visit-basket' for route in app.routes):
        return  # The commissioned maintained-source adapter owns this endpoint.
    @app.get('/api/retail/visit-basket')
    async def visit_basket(request: Request, source_id: str, track_id: str, at_s: float):
        from fastapi.responses import JSONResponse
        from visit_basket import VisitBasket
        import hashlib
        import json
        selected = request.state.workspace
        owner = selected['chats'].principal(request.state.session)
        if not owner:
            raise HTTPException(403, 'Open your workspace to view its visitors.')
        try:
            if not source_id or len(source_id) > 160 or not track_id or len(track_id) > 100:
                raise ValueError('Choose a tracked visitor in this recording.')
            source = selected['monitoring'].source_lookup(owner, source_id)
            if not source:
                raise ValueError('Choose an accessible recording.')
            recording = source.get('video_id') or source['id']
            key = 'visit-' + hashlib.sha256(json.dumps([owner, source['dataset'], recording, track_id]).encode()).hexdigest()[:28]
            basket = VisitBasket(selected['location_intelligence'])
            result = basket.at(owner, key, at_s)
        except (ValueError, KeyError):
            raise HTTPException(404, 'Choose an available tracked visitor and recording time.') from None
        receipt = result.pop('scope_receipt')
        actor = ScopeActor(selected['business_scope'].company_id, owner)
        event_ids = {event_id for item in result['items'] + result['uncertain_items'] for event_id in item['evidence_event_ids']}
        event_ids.update(row['event_id'] for row in result['unresolved_observations'] + result['payment_actions'])
        def current():
            try:
                if not selected['business_scope'].current(actor, receipt['collection'], receipt['revision'], account_role=receipt['account_role']):
                    return False
                basket._visit(owner, key)
                for event_id in event_ids:
                    selected['location_intelligence']._event_place(owner, selected['monitoring'].get_event(owner, event_id))
                return True
            except (ValueError, KeyError):
                return False
        request.state.scope_guards.append(current)
        return JSONResponse(result, headers={'Cache-Control': 'private, no-store'})
