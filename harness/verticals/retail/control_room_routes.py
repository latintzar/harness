"""Authenticated, source-scoped playback of host-published observation bundles.

The viewer code ships with the product. Recordings, observations and crops remain in
private workspace state. A bundle is an explicit host import, never model-supplied paths.
"""
from __future__ import annotations
import asyncio
import time
import hashlib
import json
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

CODE = Path(__file__).parent / 'web' / 'control-room'
# Generic generated illustrations contain no camera footage or business records.
# Camera PNG/JPEG files still require the owner's published source-scoped bundle.
GENERATED_CODE_ASSETS = frozenset({
    'location-images/store-exterior.png',
    'location-images/store-exterior.provenance.json',
})

# Pinned, generic CPU assets; access still passes the workspace authentication gate.
OUTLINE_VENDOR_ROOT = 'vendor/mediapipe-0.10.22-rc.20250304/'
OUTLINE_VENDOR_ASSETS = frozenset(OUTLINE_VENDOR_ROOT + name for name in (
    'vision_bundle.mjs', 'wasm/vision_wasm_internal.js',
    'wasm/vision_wasm_internal.wasm', 'wasm/vision_wasm_nosimd_internal.js',
    'wasm/vision_wasm_nosimd_internal.wasm', 'selfie_segmenter.tflite',
    'LICENSE', 'manifest.json',
))
PRIVATE_VIEWER_CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; "
    "script-src 'self'; worker-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; object-src 'none'"
)
# Only this dedicated worker can compile WebAssembly. General app/page script
# execution retains its existing strict policy; neither blob nor remote origins.
OUTLINE_WORKER_CSP = (
    "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; "
    "connect-src 'self'; worker-src 'none'; base-uri 'none'; object-src 'none'"
)

def owner_directory(state: Path, owner: str) -> Path:
    return Path(state) / 'control-room' / hashlib.sha256(owner.encode()).hexdigest()

def safe_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {'.', '..'} for part in path.parts):
        raise ValueError('Choose a published camera asset.')
    candidate = root.joinpath(path)
    if not candidate.is_file() or candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('Choose a published camera asset.')
    return candidate

def read_bundle(selected, owner):
    root = owner_directory(selected['state'], owner)
    try:
        manifest = json.loads(safe_file(root, 'manifest.json').read_text())
        if manifest.get('schema_version') != 1 or manifest.get('owner_principal_id') != owner:
            return None
        privacy = manifest.get('privacy', {})
        # Footage is served when its faces are anonymized or redacted, or when it is published footage
        # shown as its publisher released it under a named licence.
        published = privacy.get('face_redaction') == 'source_as_published' and bool(privacy.get('licence'))
        if privacy.get('face_redaction') not in {'source_anonymized', 'verified_redacted'} and not published:
            return None
        sources = manifest.get('source_ids')
        if not isinstance(sources, list) or not sources or not all(isinstance(x, str) for x in sources):
            return None
        lookup = selected['monitoring'].source_lookup
        if not all(lookup(owner, key) for key in sources):
            return None
        if not isinstance(manifest.get('files'), list) or 'data.json' not in manifest['files']:
            return None
        return root, manifest
    except (OSError, ValueError, KeyError, TypeError):
        return None

_RECENT_BUNDLES = {}


def state_signature(selected, owner):
    """A fingerprint of what a bundle read depends on: the manifest, the company's stores and the
    source lookup in force. A write to any of them changes it."""
    state = Path(selected['state'])
    marks = [id(selected['monitoring'].source_lookup)]
    for path in [owner_directory(state, owner) / 'manifest.json', *sorted(state.glob('*.sqlite*'))]:
        try:
            stat = path.stat()
            marks.append((path.name, stat.st_ino, stat.st_mtime_ns, stat.st_size))
        except OSError:
            marks.append((path.name, None))
    return tuple(marks)


def recent_bundle(selected, owner, interval=0.25):
    """The owner's bundle, shared by the requests of one page.

    One page asks for dozens of bundle files at once, and each read opens the manifest and every
    source. A read is reused while nothing it depends on has changed, for at most the interval;
    any write to the manifest or to the company's stores brings a fresh read on the next request.
    """
    key = (str(selected['state']), owner)
    now, signature = time.monotonic(), state_signature(selected, owner)
    held = _RECENT_BUNDLES.get(key)
    if held is None or held[1] != signature or now - held[0] >= interval:
        held = (now, signature, read_bundle(selected, owner))
        _RECENT_BUNDLES[key] = held
    return held[2]


def register_control_room_routes(app):
    from retail_urls import retail_router
    app = retail_router(app)
    def context(request):
        selected = request.state.workspace
        owner = selected['chats'].principal(request.state.session)
        if not owner:
            raise HTTPException(403, 'Open your workspace to view its cameras.')
        return selected, owner

    # Weather and the neighbourhood map of the session's location, behind this same check.
    from location_context import register_location_context_routes
    register_location_context_routes(app, context)

    @app.get('/api/catalog/inspections/{inspection_id}/image')
    async def item_image(request: Request, inspection_id: str):
        selected, owner = context(request)
        try:
            store = selected['catalog_inspection']
            def allowed():
                try:
                    store.image(owner, inspection_id, selected['monitoring'].get_event)
                    return True
                except ValueError:
                    return False
            path = store.image(owner, inspection_id, selected['monitoring'].get_event)
            request.state.scope_guards.append(allowed)
            return FileResponse(path, media_type='image/jpeg', headers={
                'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
        except (KeyError, ValueError, OSError) as exc:
            raise HTTPException(404, 'Choose item evidence in your current location access.') from exc

    @app.get('/api/retail/viewer/bootstrap')
    async def bootstrap(request: Request):
        context(request)
        if app.state.realtime_read_authority == 'maintained':
            from playback_media import maintained_bootstrap
            return await maintained_bootstrap(request)
        return JSONResponse({'schema': 'rocoto.player-bootstrap.v1',
                             'read_authority': 'legacy', 'sources': []},
                            headers={'Cache-Control': 'private, no-store'})

    @app.get('/api/retail/viewer')
    async def control_room_status(request: Request):
        selected, owner = context(request)
        if app.state.realtime_read_authority == 'maintained':
            from playback_media import maintained_bootstrap
            await maintained_bootstrap(request)
            return {'ready': True, 'bundle_url': '/control-room/'}
        bundle = read_bundle(selected, owner)
        if not bundle:
            return {'ready': False, 'message': 'Connect a camera or import an annotated recording to open the location view.'}
        _, manifest = bundle
        request.state.scope_guards.append(lambda: read_bundle(selected, owner) is not None)
        return {'ready': True, 'bundle_url': '/control-room/', 'location': manifest.get('location'),
                'revision': manifest.get('revision'), 'privacy': manifest['privacy']}

    @app.get('/api/tiles/revision')
    async def control_room_revision(request: Request):
        """What an open page polls: the revision of its published data, for this session's owner.

        The trackers run by themselves, so the page asks here every few seconds and reads its data
        again once the revision moves. The answer also carries what the tools set and what the last
        pass did (`schedule`, `run`), so the page names a location whose trackers run on request,
        stopped or wait. `If-None-Match` with the last ETag answers 304 while both are as they were.
        """
        selected, owner = context(request)
        if app.state.realtime_read_authority == 'maintained':
            # The maintained reader carries its own live state; its page keeps this poll at rest.
            return JSONResponse({'schema': 'rocoto.home-revision.v1', 'read_authority': 'maintained',
                                 'revision': None, 'day': None, 'outputs': [], 'run': None, 'schedule': None},
                                headers={'Cache-Control': 'private, no-store'})
        bundle = recent_bundle(selected, owner)
        if not bundle:
            raise HTTPException(404, 'This location view is available through its owning workspace.')
        request.state.scope_guards.append(lambda: recent_bundle(selected, owner) is not None)
        from tracker_runner import home_revision, ledger_mark, page_digest
        # The tag joins the page data's revision with a mark of the run ledger, where the schedule and the
        # runs live. The mark is read first, so an answer carries at least what its tag stands for.
        mark = ledger_mark(selected['state'])
        etag = lambda revision: {'ETag': '"' + (revision or 'none') + ('.' + mark if mark else '') + '"', 'Cache-Control': 'private, no-cache'}
        # The usual answer is "unchanged": two file stats decide it, ahead of any store read.
        headers = etag(await asyncio.to_thread(page_digest, selected['state'], owner))
        # A proxy that compresses the answer marks its tag weak; the page sends it back as it got it.
        known = {tag.strip().removeprefix('W/') for tag in request.headers.get('if-none-match', '').split(',')}
        if headers['ETag'] in known:
            return Response(status_code=304, headers=headers)
        place = bundle[1].get('location')
        value = await asyncio.to_thread(home_revision, selected['state'], owner,
                                        place.get('id') if isinstance(place, dict) else None)
        return JSONResponse({**value, 'read_authority': 'legacy'}, headers=etag(value['revision']))

    @app.get('/control-room/')
    @app.get('/control-room/{asset:path}')
    async def control_room_asset(request: Request, asset: str = 'index.html'):
        selected, owner = context(request)
        try:
            # Starlette supplies an empty path for the slash route in some
            # versions. Treat that as the private viewer entrypoint.
            asset = asset or 'index.html'
            # Code assets have no footage or business records. Every route still requires
            # an authenticated workspace; data never falls back to another workspace.
            from control_room_static import static_code_file
            code = static_code_file(asset)
            if code is not None:
                headers = {'Cache-Control': ('private, max-age=31536000, immutable' if asset in OUTLINE_VENDOR_ASSETS else 'private, max-age=60'), 'X-Content-Type-Options': 'nosniff'}
                if asset == 'index.html':
                    headers['Content-Security-Policy'] = PRIVATE_VIEWER_CSP
                elif asset == 'person-outline-worker.js':
                    headers['Content-Security-Policy'] = OUTLINE_WORKER_CSP
                media_type = ('application/wasm' if asset.endswith('.wasm') else
                              'application/javascript' if asset.endswith('.mjs') else None)
                return FileResponse(code, media_type=media_type, headers=headers)
            if app.state.realtime_read_authority == 'maintained':
                if asset.startswith('maintained/'):
                    from playback_media import maintained_media
                    return await maintained_media(request, asset)
                raise ValueError('Choose a maintained playback asset.')
            bundle = recent_bundle(selected, owner)
            if not bundle:
                raise ValueError('Import a recording with its source observations.')
            root, manifest = bundle
            if asset not in manifest['files']:
                raise ValueError('Choose an asset from this location.')
            # The server asks this guard before each chunk; it shares the page's recent read.
            request.state.scope_guards.append(lambda: recent_bundle(selected, owner) is not None)
            return FileResponse(safe_file(root, asset), headers={
                'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                'Content-Security-Policy': "default-src 'none'; media-src 'self' blob:; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"})
        except (OSError, ValueError) as exc:
            raise HTTPException(404, 'This camera asset is available through its owning workspace.') from exc
