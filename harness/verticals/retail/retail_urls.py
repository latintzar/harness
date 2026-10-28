"""Retail URL ownership and the finite set of retained addresses.

Canonical paths register first. Each legacy alias uses the identical endpoint,
method and dependencies; aliases never redirect a body, token or media request.
Frozen evidence/viewer paths retain their existing addresses. This table is the
only place a new canonical route may inherit an old top-level address.
"""
from starlette.routing import compile_path

LEGACY = {
    '/api/business/settings': '/api/retail/business/settings',
    '/api/business/connections': '/api/retail/business/connections',
    '/api/business/scope': '/api/retail/scope',
    '/api/business/scope/events': '/api/retail/scope/events',
    '/api/business/scope/{kind}': '/api/retail/scope/{kind}',
    '/api/chats/{chat_id}/scope': '/api/retail/chats/{chat_id}/scope',
    '/business-scope.js': '/static/retail/retail-scope.js',
    '/business-scope.css': '/static/retail/retail-scope.css',
    '/static/harness/business-scope.js': '/static/retail/retail-scope.js',
    '/static/harness/business-scope.css': '/static/retail/retail-scope.css',
    '/api/assets/{asset_id}': '/api/retail/assets/{asset_id}',
    '/api/cameras': '/api/retail/cameras',
    '/api/cameras/review': '/api/retail/cameras/review',
    '/api/cameras/{camera_id}': '/api/retail/cameras/{camera_id}',
    '/api/cameras/{camera_id}/confirm': '/api/retail/cameras/{camera_id}/confirm',
    '/api/cameras/{camera_id}/context': '/api/retail/cameras/{camera_id}/context',
    '/api/catalog/import': '/api/retail/catalog/import',
    '/api/catalog/import-file': '/api/retail/catalog/import-file',
    '/api/catalog/mobile-upload/{environment}': '/pages/retail/catalog-upload/{environment}',
    '/api/catalog/photo-handoff': '/api/retail/catalog/photo-handoff',
    '/api/catalog/photos/{key}/image': '/api/retail/catalog/photos/{key}/image',
    '/api/catalog/setup': '/api/retail/catalog/setup',
    '/api/catalog/shelf-crops/{key}/{index}/image': '/api/retail/catalog/shelf-crops/{key}/{index}/image',
    '/api/clip/{clip_id}': '/api/retail/clip/{clip_id}',
    '/api/clip/{clip_id}/manifest': '/api/retail/clip/{clip_id}/manifest',
    '/api/connections/custom/{connector_id}/storage': '/api/retail/storage/{connector_id}',
    '/api/connections/custom/{connector_id}/storage/approve': '/api/retail/storage/{connector_id}/approve',
    '/api/connections/custom/{connector_id}/storage/pause': '/api/retail/storage/{connector_id}/pause',
    '/api/connections/custom/{connector_id}/storage/run': '/api/retail/storage/{connector_id}/run',
    '/api/control-room': '/api/retail/viewer',
    '/api/control-room/bootstrap': '/api/retail/viewer/bootstrap',
    '/api/control-room/catalog': '/api/retail/catalog/products',
    '/api/control-room/location': '/api/retail/location',
    '/api/control-room/map': '/api/retail/location/map',
    '/api/control-room/revision': '/api/tiles/revision',
    '/api/control-room/visit-basket': '/api/retail/visit-basket',
    '/api/control-room/weather': '/api/retail/location/weather',
    '/api/exports': '/api/retail/exports',
    '/api/exports/{payload_id}': '/api/retail/exports/{payload_id}',
    '/api/exports/{payload_id}/download': '/api/retail/exports/{payload_id}/download',
    '/api/inspection-previews/{receipt_id}/{index}.jpg': '/api/retail/inspection-previews/{receipt_id}/{index}.jpg',
    '/api/library': '/api/retail/library',
    '/api/library/imports': '/api/retail/library/imports',
    '/api/library/imports/{upload_id}': '/api/retail/library/imports/{upload_id}',
    '/api/library/imports/{upload_id}/complete': '/api/retail/library/imports/{upload_id}/complete',
    '/api/library/imports/{upload_id}/content': '/api/retail/library/imports/{upload_id}/content',
    '/api/monitoring/events': '/api/retail/monitoring/events',
    '/api/monitoring/events/{event_id}': '/api/retail/monitoring/events/{event_id}',
    '/api/monitoring/profiles': '/api/retail/monitoring/profiles',
    '/api/monitoring/profiles/{profile_id}': '/api/retail/monitoring/profiles/{profile_id}',
    '/api/realtime/sources/{source_id}/assets/{asset_id}/snapshot': '/api/retail/realtime/sources/{source_id}/assets/{asset_id}/snapshot',
    '/api/realtime/sources/{source_id}/epochs/{epoch_id}/snapshot': '/api/retail/realtime/sources/{source_id}/epochs/{epoch_id}/snapshot',
    '/api/search': '/api/retail/search',
    '/api/source-links': '/api/retail/source-links',
    '/api/source-links/{identity}': '/api/retail/source-links/{identity}',
    '/api/source-links/{identity}/retry': '/api/retail/source-links/{identity}/retry',
    '/api/sources': '/api/retail/sources',
    '/api/sources/{source_id}': '/api/retail/sources/{source_id}',
    '/assets/camera-providers/{filename}': '/static/retail/assets/camera-providers/{filename}',
    '/assets/demo-companies/sij-acroni.png': '/static/retail/assets/demo-companies/sij-acroni.png',
    '/camera-streams.css': '/static/retail/camera-streams.css',
    '/camera-streams.js': '/static/retail/camera-streams.js',
    '/catalog-upload.css': '/static/retail/catalog-upload.css',
    '/catalog-upload.js': '/static/retail/catalog-upload.js',
    '/catalog-upload/{environment}': '/pages/retail/catalog-upload/{environment}',
    '/control-room.js': '/static/retail/control-room.js',
    '/retail-shell.js': '/static/retail/retail-shell.js',
    '/retail-ui.css': '/static/retail/retail-ui.css',
    '/retail-ui.js': '/static/retail/retail-ui.js',
    '/tracker-canvas.css': '/static/retail/tracker-canvas.css',
    '/tracker-canvas.js': '/static/retail/tracker-canvas.js',
}

METHODS = {
    '/api/business/settings': ('POST',),
    '/api/business/connections': ('POST',),
    '/api/business/scope': ('GET',),
    '/api/business/scope/events': ('GET',),
    '/api/business/scope/{kind}': ('POST',),
    '/api/chats/{chat_id}/scope': ('GET', 'PUT'),
    '/business-scope.js': ('GET',),
    '/business-scope.css': ('GET',),
    '/static/harness/business-scope.js': ('GET',),
    '/static/harness/business-scope.css': ('GET',),
    '/api/assets/{asset_id}': ('GET',),
    '/api/cameras': ('GET',),
    '/api/cameras/review': ('POST',),
    '/api/cameras/{camera_id}': ('GET',),
    '/api/cameras/{camera_id}/confirm': ('POST',),
    '/api/cameras/{camera_id}/context': ('POST',),
    '/api/catalog/import': ('POST',),
    '/api/catalog/import-file': ('POST',),
    '/api/catalog/mobile-upload/{environment}': ('POST',),
    '/api/catalog/photo-handoff': ('POST',),
    '/api/catalog/photos/{key}/image': ('GET',),
    '/api/catalog/setup': ('GET',),
    '/api/catalog/shelf-crops/{key}/{index}/image': ('GET',),
    '/api/clip/{clip_id}': ('GET',),
    '/api/clip/{clip_id}/manifest': ('GET',),
    '/api/connections/custom/{connector_id}/storage': ('GET',),
    '/api/connections/custom/{connector_id}/storage/approve': ('POST',),
    '/api/connections/custom/{connector_id}/storage/pause': ('POST',),
    '/api/connections/custom/{connector_id}/storage/run': ('POST',),
    '/api/control-room': ('GET',),
    '/api/control-room/bootstrap': ('GET',),
    '/api/control-room/catalog': ('GET',),
    '/api/control-room/location': ('GET',),
    '/api/control-room/map': ('GET',),
    '/api/control-room/revision': ('GET',),
    '/api/control-room/visit-basket': ('GET',),
    '/api/control-room/weather': ('GET',),
    '/api/exports': ('GET', 'POST'),
    '/api/exports/{payload_id}': ('GET',),
    '/api/exports/{payload_id}/download': ('GET',),
    '/api/inspection-previews/{receipt_id}/{index}.jpg': ('GET',),
    '/api/library': ('GET',),
    '/api/library/imports': ('GET', 'POST'),
    '/api/library/imports/{upload_id}': ('DELETE', 'GET'),
    '/api/library/imports/{upload_id}/complete': ('POST',),
    '/api/library/imports/{upload_id}/content': ('PATCH',),
    '/api/monitoring/events': ('GET', 'POST'),
    '/api/monitoring/events/{event_id}': ('GET',),
    '/api/monitoring/profiles': ('GET', 'POST'),
    '/api/monitoring/profiles/{profile_id}': ('GET', 'POST'),
    '/api/realtime/sources/{source_id}/assets/{asset_id}/snapshot': ('GET',),
    '/api/realtime/sources/{source_id}/epochs/{epoch_id}/snapshot': ('GET',),
    '/api/search': ('POST',),
    '/api/source-links': ('GET', 'POST'),
    '/api/source-links/{identity}': ('DELETE', 'GET'),
    '/api/source-links/{identity}/retry': ('POST',),
    '/api/sources': ('GET', 'POST'),
    '/api/sources/{source_id}': ('GET',),
    '/assets/camera-providers/{filename}': ('GET',),
    '/assets/demo-companies/sij-acroni.png': ('GET',),
    '/camera-streams.css': ('GET',),
    '/camera-streams.js': ('GET',),
    '/catalog-upload.css': ('GET',),
    '/catalog-upload.js': ('GET',),
    '/catalog-upload/{environment}': ('GET',),
    '/control-room.js': ('GET',),
    '/retail-shell.js': ('GET',),
    '/retail-ui.css': ('GET',),
    '/retail-ui.js': ('GET',),
    '/tracker-canvas.css': ('GET',),
    '/tracker-canvas.js': ('GET',),
}

FROZEN = {'/downloads/{clip_id}.mp4', '/originals/{asset_id}', '/control-room/', '/api/catalog/inspections/{inspection_id}/image', '/control-room/{asset:path}', '/thumbnails/{clip_id}.jpg', '/clips/{clip_id}.mp4'}
SHARED = {'/api/chats/{chat_id}/promote-agent', '/api/agents/{agent_id}/presentation', '/api/agents/{agent_id}/observations'}

_MATCHES = [(old, new, compile_path(new)[0]) for old, new in LEGACY.items()]


def legacy_path(path, method='GET'):
    """Classify current paths through their established admission policy."""
    for old, new, pattern in _MATCHES:
        if method not in METHODS[old] and not (method == 'HEAD' and 'GET' in METHODS[old]):
            continue
        match = pattern.fullmatch(path)
        if match:
            for name, value in match.groupdict().items():
                old = old.replace('{' + name + '}', value).replace('{' + name + ':path}', value)
            return old
    return path


class RetailRouter:
    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        if name not in {'get', 'post', 'patch', 'put', 'delete', 'head', 'options'}:
            return getattr(self.app, name)
        def register(path, **options):
            if not (path.startswith(('/api/retail/', '/static/retail/', '/pages/retail/'))
                    or path in FROZEN or path in SHARED or path == '/api/tiles/revision'):
                raise ValueError('Register this route in the retail namespace.')
            def decorate(endpoint):
                getattr(self.app, name)(path, **options)(endpoint)
                for old, canonical in LEGACY.items():
                    if path == canonical and name.upper() in METHODS[old]:
                        getattr(self.app, name)(old, **dict(options, include_in_schema=False))(endpoint)
                return endpoint
            return decorate
        return register


def retail_router(app):
    return app if isinstance(app, RetailRouter) else RetailRouter(app)
