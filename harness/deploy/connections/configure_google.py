"""Hidden terminal input for this preview's Google Web OAuth application.

Stores application credentials only. Customer consent, provider readiness and
write commissioning remain separate. This command makes no network requests.
"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import pwd
import socket
import sys
import tempfile
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if (Path(__file__).resolve().parents[2] / 'layout.py').is_file():
    import layout
    layout.install()
from registered_oauth import CALLBACK_PATH, RegisteredClient, RegisteredOAuthClients
from workspace_identity import workspace_scope

ORIGIN = 'https://video-demo.soyrocoto.com'
STATE = Path('/var/lib/rocoto-video-preview')
DIRECTORY = STATE / 'provider-clients'


def save_registration(state, directory, client_id, client_secret, *, public_origin=ORIGIN):
    scope = workspace_scope(state)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Use the private OAuth directory owned by the preview runtime.')
    destination = directory / (scope + '.json')
    old = RegisteredOAuthClients.load(destination, company_scope=scope,
        public_origin=public_origin) if destination.exists() or destination.is_symlink() else None
    revision = max((client.revision for client in old.clients), default=0) + 1 if old else 1
    callback = public_origin + CALLBACK_PATH
    clients = [RegisteredClient(provider, revision, callback, client_id, client_secret)
               for provider in ('google-drive', 'google-docs')]
    # An intentionally emptied client file retains revocation tombstones in the
    # private broker store. Restoration must advance those revisions as well.
    if (Path(state) / 'mcp-connections/connections.sqlite').exists():
        from mcp_connections import MCPConnections
        broker = MCPConnections(state, public_origin=public_origin)
        with broker._db() as db:
            highest = db.execute('SELECT MAX(revision) FROM registered_clients').fetchone()[0]
        revision = max(revision, (highest or 0) + 1)
        clients = [RegisteredClient(provider, revision, callback, client_id, client_secret)
                   for provider in ('google-drive', 'google-docs')]
    payload = {'schema_version': 1, 'company_scope': scope, 'redirect_uri': callback,
               'clients': {client.provider: {'revision': revision, 'client_id': client.client_id,
                           'client_secret': client.client_secret} for client in clients}}
    fd, temporary = tempfile.mkstemp(prefix='.google-client-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(payload, output, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        RegisteredOAuthClients.load(temporary, company_scope=scope, public_origin=public_origin)
        os.replace(temporary, destination)
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def main():
    if os.geteuid() != 0 or socket.gethostname() != 'rocoto-staging':
        raise SystemExit('Run this setup with sudo on rocoto-staging.')
    warnings.simplefilter('error', getpass.GetPassWarning)
    client_id = getpass.getpass('Rocoto Google Web client ID (hidden): ').strip()
    client_secret = getpass.getpass('Rocoto Google Web client secret (hidden): ').strip()
    account = pwd.getpwnam('rocoto-video-preview')
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    try:
        destination = save_registration(STATE, DIRECTORY, client_id, client_secret)
    except (OSError, ValueError):
        raise SystemExit('Review the Google Web client and private preview configuration.') from None
    print('Saved the Google Web application configuration to ' + str(destination))
    print('Customer consent and provider verification follow host activation.')


if __name__ == '__main__':
    main()
