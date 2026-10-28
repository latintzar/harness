"""Acquire unchanged public plugin archives for OpenAI-hosted execution.

Installation downloads and verifies files; it never launches an agent core,
package hook or provider operation. The package store owns private selections.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile

from native_install_source import fetch_archive, source_spec
from native_install_worker import extract_selected
from native_packages import PackageError, _read_packages


class HostedPluginInstaller:
    def __init__(self, *, transport=None):
        self.transport = transport

    @asynccontextmanager
    async def acquire(self, entry, catalogue):
        spec = source_spec(entry, catalogue)
        with tempfile.TemporaryDirectory(prefix='rocoto-hosted-plugin-') as directory:
            archive = Path(directory) / 'source.tar.gz'
            receipt = await fetch_archive(spec, archive, transport=self.transport)
            document = {'source':spec.document(), 'archive_sha256':receipt['sha256'],
                        'archive_bytes':receipt['size_bytes']}
            root = Path(directory) / 'package'
            # Drain a cancelled extraction before removing its private directory.
            await _read_packages(extract_selected, archive, document, root)
            manifest = json.loads((root / '.codex-plugin/plugin.json').read_bytes())
            if manifest.get('name') != spec.plugin_id:
                raise PackageError('Use the selected original plugin manifest.')
            yield root, {'runtime':'openai_agents', 'version':manifest.get('version'),
                         'installed':True, 'enabled':True, 'source':spec.document()}
