"""Authenticated, uncached profile cover reads; all identities come from the host."""
import inspect
import sqlite3
from fastapi import HTTPException, Request
from fastapi.responses import Response
from custom_coordinator import offload
from profile_covers import CoverError

PRIVATE = {'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; sandbox", 'Cross-Origin-Resource-Policy': 'same-origin'}


def register_profile_cover_routes(app, *, context_for_request):
    """context_for_request(request, cover_id) -> (store, actor_id, collection).

    The host resolves the selected workspace, current membership, and the cover's
    allowed collection. Collection may be None to authorize the stored publication
    collection. It must not derive principal/company from URL parameters.
    Store.download independently rechecks the current profile-view permission.
    """
    @app.get('/api/profile-covers/{cover_id}')
    async def cover(cover_id: str, request: Request):
        if not getattr(request.state, 'session', {}).get('authenticated'):
            raise HTTPException(401, 'profile_access_required', headers=PRIVATE)
        try:
            context = context_for_request(request, cover_id)
            store, actor_id, collection = await context if inspect.isawaitable(context) else context
            _, data = await offload(store.download, cover_id, actor_id=actor_id, collection=collection)
        except CoverError:
            raise HTTPException(404, 'profile_cover_unavailable', headers=PRIVATE) from None
        except (sqlite3.Error, OSError):
            raise HTTPException(503, 'profile_cover_store_unavailable', headers=PRIVATE) from None
        return Response(data, media_type='image/png', headers=PRIVATE)
