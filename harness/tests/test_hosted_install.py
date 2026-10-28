"""Original archives install without a local agent core or package execution."""
import asyncio
from pathlib import Path
import tarfile

import httpx
import pytest
from hosted_install import HostedPluginInstaller
from native_install_worker import WorkerError
from native_packages import NativePackageStore, StablePluginCLI
from test_native_packages import snapshot
from test_native_install import archive, entry, Stream


def test_original_bytes_modes_and_owner_selection_without_cli(tmp_path,monkeypatch,snapshot):
    calls=[]
    path,_=archive(tmp_path,[('.codex-plugin/plugin.json',b'{"name":"notion","version":"0.1.7"}'),
        ('run.sh',b'#!/bin/sh\nexit 0\n'),('skills/notes/SKILL.md',b'Original skill bytes.\n')])
    data=path.read_bytes()
    def forbidden(*args,**kwargs):raise AssertionError('Installation invoked a local agent core.')
    monkeypatch.setattr(StablePluginCLI,'__init__',forbidden)
    def transport(request):
        calls.append(request)
        assert request.url.host=='codeload.github.com' and 'authorization' not in request.headers
        return httpx.Response(200,stream=Stream([data]),headers={'content-length':str(len(data))})
    async def run():
        installer=HostedPluginInstaller(transport=httpx.MockTransport(transport))
        store=NativePackageStore(tmp_path/'state',catalogue_snapshot=snapshot,materializer=installer)
        first=await store.install('notion',principal_id='alice')
        assert first['runtime']=='openai_agents' and first['upstream_revision']=='a'*40
        assert store.list(principal_id='bob')['plugins']==[]
        selected=store.snapshot(principal_id='alice').packages[0]
        assert (selected.root/'skills/notes/SKILL.md').read_bytes()==b'Original skill bytes.\n'
        assert (selected.root/'run.sh').stat().st_mode&0o111
        assert await store.install('notion',principal_id='alice')==first
        assert len(calls)==1
        assert isinstance(NativePackageStore(tmp_path/'default',catalogue_snapshot=snapshot).materializer,HostedPluginInstaller)
    asyncio.run(run())


def test_hostile_archive_publishes_no_installation(tmp_path,snapshot):
    bad=tarfile.TarInfo();bad.type=tarfile.SYMTYPE;bad.linkname='/etc/shadow'
    path,_=archive(tmp_path,[('.codex-plugin/plugin.json',b'{"name":"notion","version":"0.1.7"}'),('bad',bad)])
    data=path.read_bytes()
    async def run():
        store=NativePackageStore(tmp_path/'state',catalogue_snapshot=snapshot,
            materializer=HostedPluginInstaller(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=Stream([data]),headers={'content-length':str(len(data))}))))
        with pytest.raises(WorkerError):await store.install('notion',principal_id='alice')
        assert store.list(principal_id='alice')['plugins']==[]
        assert list(store.trees.iterdir())==[]
    asyncio.run(run())


def test_archive_extraction_directory_is_removed_after_use(tmp_path):
    path,_=archive(tmp_path);data=path.read_bytes()
    async def run():
        installer=HostedPluginInstaller(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=Stream([data]),headers={'content-length':str(len(data))})))
        async with installer.acquire(entry(),{'revision':'a'*40}) as (root,receipt):
            assert root.is_dir() and receipt['runtime']=='openai_agents'
        assert not root.exists()
    asyncio.run(run())
