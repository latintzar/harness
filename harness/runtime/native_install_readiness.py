"""Fresh, allocation-free native installer readiness for authenticated hosts."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from native_executor import IMAGE, NativeScope
from native_install_client import BrokerPluginInstaller, InstallerClient
from native_install_source import source_spec
import native_install_worker
from workspace_identity import workspace_scope

CAPABILITY_TIMEOUT = 2.0
PROFILE = 'native-plugin-install-v1'


class NativeInstallerReadiness:
    def __init__(self, state, *, enabled=False, client=None):
        self.state = Path(state).absolute()
        self.company = workspace_scope(self.state)
        self.enabled = enabled is True
        self.client = client or InstallerClient()
        self.worker_sha256 = hashlib.sha256(Path(native_install_worker.__file__).read_bytes()).hexdigest()

    async def probe(self, store, *, principal_id):
        closed = {'available': False, 'installable_ids': []}
        if (not self.enabled or not isinstance(principal_id, str) or not principal_id
                or store.state != self.state or not isinstance(store.materializer, BrokerPluginInstaller)
                or store.materializer.state != self.state or store.materializer.company != self.company):
            return closed
        # No positive cache: broker restarts, source changes and disconnection
        # take effect on the next request. This protocol allocates no worker.
        try:
            scope = NativeScope(self.company, principal_id, 'plugin-readiness')
            async with asyncio.timeout(CAPABILITY_TIMEOUT):
                result = await self.client.capabilities(scope)
            if not self.enabled: return closed
            revision = store.snapshot_catalogue['revision']
            if (not isinstance(result, dict) or type(result.get('schema_version')) is not int or result.get('schema_version') != 1
                    or result.get('available') is not True or result.get('profile') != PROFILE
                    or result.get('image') != IMAGE or result.get('worker_sha256') != self.worker_sha256
                    or result.get('catalogue_revisions') != [revision]):
                return closed
            identifiers = result.get('supported_plugin_ids')
            if (not isinstance(identifiers, list) or len(identifiers) > 64
                    or any(not isinstance(value, str) for value in identifiers)
                    or len(set(identifiers)) != len(identifiers)):
                return closed
            verified = []
            for identifier in identifiers:
                entry = store.entries.get(identifier)
                if entry is None: continue
                try: source_spec(entry, store.snapshot_catalogue)
                except (ValueError, TypeError, KeyError): continue
                verified.append(identifier)
            return {'available': True, 'installable_ids': sorted(verified)}
        except Exception:
            # Root socket/protocol errors and private paths never reach the UI.
            return closed
