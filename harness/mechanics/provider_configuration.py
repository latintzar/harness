"""Host configuration for a company's OAuth applications and remote MCP policies.

The environment selects a private configuration directory. Request bodies,
provider metadata and agent arguments cannot select a client or a secret path.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from workspace_identity import workspace_scope


def commissioned_writes_for(requested, registered_clients, remote_providers=None):
    """Apply a host's write selection to this company's available provider policies."""
    from provider_policy import effective_policies
    policies = effective_policies(remote_providers)
    selected = set()
    for provider in requested:
        policy = policies.get(provider)
        if policy is None:
            selected.add(provider)  # The broker reports unknown host configuration.
        elif ('read_write' in policy.get('scopes_by_mode', {})
                and ((policy.get('registration') == 'static'
                      and registered_clients is not None and registered_clients.get(provider) is not None)
                     or policy.get('registration') != 'static' and policy.get('available'))):
            selected.add(provider)
    return frozenset(selected)


def _company_configuration(state, *, public_origin, environment, loader):
    configured = os.environ.get(environment, '')
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError('Use an absolute private OAuth configuration directory.')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError('Open the private OAuth configuration directory.') from None
    try:
        info = os.fstat(descriptor)
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o077:
            raise ValueError('Use a private OAuth configuration directory owned by the host.')
        scope = workspace_scope(state)
        filename = scope + '.json'
        try:
            entry = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError('Use a private regular OAuth configuration file.')
        return loader(path / filename,
            company_scope=scope, public_origin=public_origin)
    finally:
        os.close(descriptor)


def registered_clients_for(state, *, public_origin):
    from registered_oauth import RegisteredOAuthClients
    return _company_configuration(state, public_origin=public_origin,
        environment='ROCOTO_REGISTERED_OAUTH_DIRECTORY', loader=RegisteredOAuthClients.load)


def remote_providers_for(state, *, public_origin):
    from catalogue_providers import RemoteProviderCommissioning
    return _company_configuration(state, public_origin=public_origin,
        environment='ROCOTO_REMOTE_MCP_DIRECTORY', loader=RemoteProviderCommissioning.load)
