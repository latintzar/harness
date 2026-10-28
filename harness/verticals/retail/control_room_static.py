"""One fixed-source classifier for authenticated, tenant-independent viewer code.

This selects neither private bundle paths nor business API responses. HTTP login
and workspace membership stay in the ordinary request middleware.
"""
from pathlib import Path


def static_code_file(asset: str) -> Path | None:
    # Read the route's source directory and explicit generated/vendor lists at
    # call time; admission and serving therefore use the same path policy.
    from control_room_routes import CODE, GENERATED_CODE_ASSETS, OUTLINE_VENDOR_ASSETS, safe_file
    asset = asset or 'index.html'
    if (asset == 'index.html' or asset in GENERATED_CODE_ASSETS or asset in OUTLINE_VENDOR_ASSETS
            or asset.endswith(('.js', '.css', '.woff2', '.svg'))):
        return safe_file(CODE, asset)
    return None


def is_static_viewer_request(path: str, method: str) -> bool:
    if method not in {'GET', 'HEAD'} or not path.startswith('/control-room/'):
        return False
    try:
        return static_code_file(path.removeprefix('/control-room/')) is not None
    except (OSError, ValueError):
        return False
