import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from native_packages import NativePackageStore, StablePluginCLI, PackageError, _scan_tree


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.fixture
def package(tmp_path):
    root=tmp_path/'original';(root/'.codex-plugin').mkdir(parents=True)
    (root/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'notion','version':'0.1.7','license':'MIT','skills':'./skills','mcpServers':'./.mcp.json'}))
    (root/'skills/notes/references').mkdir(parents=True)
    (root/'skills/notes/SKILL.md').write_text('---\nname: notes\ndescription: Notes for a connected account.\n---\nRead [reference](references/detail.md).\n')
    (root/'skills/notes/references/detail.md').write_text('Exact original reference.\n')
    (root/'.mcp.json').write_text('{"mcpServers":{"notion":{"type":"http","url":"https://mcp.notion.com/mcp"}}}')
    (root/'LICENSE').write_text('Fixture MIT license\n')
    (root/'run.sh').write_text('#!/bin/sh\nexit 0\n');(root/'run.sh').chmod(0o755)
    return root


@pytest.fixture
def snapshot():
    return {'revision':'a'*40,'source_url':'https://github.com/openai/plugins/tree/'+'a'*40,'plugins':[
        {'id':'notion','supported_api':True,'source_url':'https://github.com/openai/plugins/tree/'+'a'*40+'/plugins/notion',
         'catalogue_entry':{'name':'notion','source':{'source':'local','path':'./plugins/notion'}},'manifest':{}},
        {'id':'gmail','supported_api':False,'catalogue_entry':{'name':'gmail'}}]}


class Fake:
    def __init__(self,path):self.path=path;self.calls=0;self.wait=None;self.started=asyncio.Event()
    @asynccontextmanager
    async def acquire(self,entry,catalogue):
        self.calls+=1;self.started.set()
        if self.wait: await self.wait.wait()
        yield self.path,{'version':'0.1.7','installed':True,'enabled':True}


def make(tmp_path,package,snapshot):
    fake=Fake(package);return NativePackageStore(tmp_path/'state',catalogue_snapshot=snapshot,materializer=fake),fake


@pytest.mark.anyio
async def test_complete_original_snapshot_and_private_selection(tmp_path,package,snapshot):
    store,fake=make(tmp_path,package,snapshot)
    result=await store.install('notion',principal_id='alice')
    assert result['installed'] and not result['connected'] and not result['callable']
    assert store.list(principal_id='bob')['plugins']==[]
    selection=store.snapshot(principal_id='alice');tree=selection.packages[0]
    assert tree.files==_scan_tree(package)[0]
    assert (tree.root/'skills/notes/references/detail.md').read_bytes()==(package/'skills/notes/references/detail.md').read_bytes()
    assert tree.root.stat().st_mode&0o777==0o500
    assert (tree.root/'run.sh').stat().st_mode&0o777==0o500
    assert (tree.root/'LICENSE').stat().st_mode&0o777==0o400
    assert store.validate_snapshot(selection,principal_id='alice')==selection
    for principal in ['bob','../alice']:
        with pytest.raises(PackageError):store.validate_snapshot(selection,principal_id=principal)
    with pytest.raises(PackageError):store.snapshot(principal_id='bob',plugin_ids=['notion'])
    assert str(tmp_path) not in json.dumps(result)
    await store.install('notion',principal_id='alice');assert fake.calls==1
    await store.install('notion',principal_id='bob');assert len(list(store.trees.iterdir()))==1
    assert store.snapshot(principal_id='bob').owner_binding!=selection.owner_binding


@pytest.mark.anyio
async def test_uninstall_fence_is_durable_and_other_owner_unchanged(tmp_path,package,snapshot):
    store,fake=make(tmp_path,package,snapshot)
    await store.install('notion',principal_id='alice');await store.install('notion',principal_id='bob')
    alice=store.snapshot(principal_id='alice');bob=store.snapshot(principal_id='bob')
    await store.uninstall('notion',principal_id='alice')
    with pytest.raises(PackageError):store.validate_snapshot(alice,principal_id='alice')
    assert store.validate_snapshot(bob,principal_id='bob')==bob
    reopened=NativePackageStore(store.state,catalogue_snapshot=snapshot,materializer=fake)
    assert reopened.list(principal_id='alice')['plugins']==[]
    await reopened.install('notion',principal_id='alice')
    with pytest.raises(PackageError):reopened.validate_snapshot(alice,principal_id='alice')
    assert reopened.snapshot(principal_id='alice').generation==3


@pytest.mark.anyio
async def test_cross_company_and_forged_mount_root_denied(tmp_path,package,snapshot):
    a,fake=make(tmp_path,package,snapshot)
    b=NativePackageStore(tmp_path/'different',catalogue_snapshot=snapshot,materializer=fake)
    await a.install('notion',principal_id='owner');await b.install('notion',principal_id='owner')
    selected=a.snapshot(principal_id='owner')
    with pytest.raises(PackageError):b.validate_snapshot(selected,principal_id='owner')
    forged=replace(selected,packages=(replace(selected.packages[0],root=package),))
    with pytest.raises(PackageError):a.validate_snapshot(forged,principal_id='owner')


@pytest.mark.anyio
async def test_tampered_cache_rejected_by_every_mount_validation(tmp_path,package,snapshot):
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='owner')
    selected=store.snapshot(principal_id='owner');p=selected.packages[0].root/'LICENSE'
    p.chmod(0o600);p.write_text('modified')
    for action in [lambda:store.snapshot(principal_id='owner'),lambda:store.validate_snapshot(selected,principal_id='owner')]:
        with pytest.raises(PackageError):action()
    with pytest.raises(PackageError):await store.install('notion',principal_id='owner')


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['symlink','hardlink','fifo','escaping_dir'])
async def test_special_files_never_published(tmp_path,package,snapshot,kind):
    store,_=make(tmp_path,package,snapshot)
    if kind=='symlink':(package/'bad').symlink_to('/etc/passwd')
    if kind=='hardlink':os.link(package/'LICENSE',package/'bad')
    if kind=='fifo':os.mkfifo(package/'bad')
    if kind=='escaping_dir':(package/'bad').symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises((PackageError,OSError)):await store.install('notion',principal_id='owner')
    assert store.list(principal_id='owner')['plugins']==[]
    assert list(store.trees.iterdir())==[]


@pytest.mark.anyio
async def test_quota_and_oversized_file_prevent_publication(tmp_path,package,snapshot,monkeypatch):
    import native_packages
    store,_=make(tmp_path,package,snapshot)
    monkeypatch.setattr(native_packages,'MAX_STORE_BYTES',1)
    with pytest.raises(PackageError):await store.install('notion',principal_id='owner')
    assert list(store.trees.iterdir())==[]
    monkeypatch.setattr(native_packages,'MAX_STORE_BYTES',1024**3)
    monkeypatch.setattr(native_packages,'MAX_FILE_BYTES',1)
    with pytest.raises(PackageError):await store.install('notion',principal_id='owner')


@pytest.mark.anyio
async def test_cancelled_waiter_does_not_cancel_other_install(tmp_path,package,snapshot):
    a,fake=make(tmp_path,package,snapshot);b=NativePackageStore(a.state,catalogue_snapshot=snapshot,materializer=fake)
    fake.wait=asyncio.Event();first=asyncio.create_task(a.install('notion',principal_id='alice'))
    await fake.started.wait();second=asyncio.create_task(b.install('notion',principal_id='bob'))
    await asyncio.sleep(.03);second.cancel()
    with pytest.raises(asyncio.CancelledError):await second
    fake.wait.set();assert (await first)['installed']
    assert a.list(principal_id='bob')['plugins']==[] and fake.calls==1


@pytest.mark.anyio
async def test_rejected_ids_never_invoke_cli(tmp_path,package,snapshot):
    store,fake=make(tmp_path,package,snapshot)
    for identifier in ['gmail','../outside','unknown','notion@personal']:
        with pytest.raises(PackageError):await store.install(identifier,principal_id='owner')
    assert fake.calls==0


def test_cli_environment_drops_provider_personal_and_git_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','private');monkeypatch.setenv('GITHUB_TOKEN','private')
    monkeypatch.setenv('GIT_CONFIG_COUNT','1');monkeypatch.setenv('SSH_AUTH_SOCK','private')
    value=StablePluginCLI._environment(tmp_path)
    assert 'private' not in str(value) and value['GIT_CONFIG_GLOBAL']=='/dev/null'
    assert not {'OPENAI_API_KEY','GITHUB_TOKEN','GIT_CONFIG_COUNT','SSH_AUTH_SOCK'}&value.keys()


def test_external_unpinned_or_credential_url_rejected(tmp_path,snapshot):
    cli=StablePluginCLI()
    for source in [{'source':'url','url':'https://github.com/org/repo.git'},
                   {'source':'url','url':'https://private@github.com/org/repo.git','sha':'a'*40},
                   {'source':'url','url':'https://localhost/repo','sha':'a'*40}]:
        with pytest.raises(PackageError):cli._source({'catalogue_entry':{'source':source}},snapshot,tmp_path)


@pytest.mark.anyio
async def test_cancellation_reaps_native_process(tmp_path):
    executable=tmp_path/'slow';executable.write_text('#!/usr/bin/python3\nimport os,time\nopen("pid", "w").write(str(os.getpid()))\ntime.sleep(30)\n');executable.chmod(0o700)
    cli=StablePluginCLI(executable)
    task=asyncio.create_task(cli._command(['plugin','list','--json'],home=tmp_path,cwd=tmp_path))
    for _ in range(100):
        if (tmp_path/'pid').exists():break
        await asyncio.sleep(.01)
    pid=int((tmp_path/'pid').read_text());task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    with pytest.raises(ProcessLookupError):os.kill(pid,0)


@pytest.mark.anyio
async def test_new_revision_of_identical_package_preserves_original_tree(tmp_path,package,snapshot,monkeypatch):
    import native_packages
    store,fake=make(tmp_path,package,snapshot)
    await store.install('notion',principal_id='owner');old=store.snapshot(principal_id='owner')
    size=sum(f.size_bytes for f in old.packages[0].files)
    monkeypatch.setattr(native_packages,'MAX_STORE_BYTES',size)
    updated=json.loads(json.dumps(snapshot));updated['revision']='b'*40
    newer=NativePackageStore(store.state,catalogue_snapshot=updated,materializer=fake)
    await newer.install('notion',principal_id='owner');current=newer.snapshot(principal_id='owner')
    assert old.packages[0].root==current.packages[0].root
    assert current.packages[0].upstream_revision=='b'*40 and current.generation==2
    with pytest.raises(PackageError):newer.validate_snapshot(old,principal_id='owner')


@pytest.mark.anyio
async def test_empty_directories_preserved_and_bound_to_digest(tmp_path,package,snapshot):
    (package/'scratch').mkdir()
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='owner')
    original=store.snapshot(principal_id='owner');root=original.packages[0].root
    assert 'scratch' in original.packages[0].directories and (root/'scratch').is_dir()
    root.chmod(0o700);(root/'unexpected-empty').mkdir()
    with pytest.raises(PackageError):store.validate_snapshot(original,principal_id='owner')


@pytest.mark.anyio
async def test_crash_after_tree_rename_does_not_install_or_leak_quota(tmp_path,package,snapshot,monkeypatch):
    import native_packages
    store,fake=make(tmp_path,package,snapshot)
    original=native_packages._fsync_dir
    def fail_after_rename(path):
        original(path)
        if path==store.trees and any(len(p.name)==64 for p in path.iterdir()):
            raise OSError('synthetic crash after atomic rename')
    monkeypatch.setattr(native_packages,'_fsync_dir',fail_after_rename)
    with pytest.raises(OSError):await store.install('notion',principal_id='owner')
    assert store.list(principal_id='owner')['plugins']==[]
    assert len(list(store.trees.iterdir()))==1
    monkeypatch.setattr(native_packages,'_fsync_dir',original)
    reopened=NativePackageStore(store.state,catalogue_snapshot=snapshot,materializer=fake)
    assert (await reopened.install('notion',principal_id='owner'))['installed']
    assert len(list(store.trees.iterdir()))==1


@pytest.mark.anyio
async def test_scan_rejects_file_swapped_after_fd_open(tmp_path,package,snapshot,monkeypatch):
    import native_packages
    store,_=make(tmp_path,package,snapshot);real_read=native_packages.os.read;changed=False
    def swap(fd,count):
        nonlocal changed
        data=real_read(fd,count)
        if data and not changed:
            changed=True
            target=package/'LICENSE';target.rename(package/'old-license');target.write_text('replacement')
        return data
    monkeypatch.setattr(native_packages.os,'read',swap)
    with pytest.raises(PackageError):await store.install('notion',principal_id='owner')
    assert not store.list(principal_id='owner')['plugins']


@pytest.mark.anyio
async def test_parallel_same_owner_install_materializes_once(tmp_path,package,snapshot):
    store,fake=make(tmp_path,package,snapshot)
    second=NativePackageStore(store.state,catalogue_snapshot=snapshot,materializer=fake)
    results=await asyncio.gather(store.install('notion',principal_id='owner'),second.install('notion',principal_id='owner'))
    assert results[0]==results[1] and fake.calls==1
    assert store.snapshot(principal_id='owner').generation==1


@pytest.mark.anyio
async def test_native_output_limit_reaps_process(tmp_path):
    executable=tmp_path/'flood';executable.write_text('#!/usr/bin/python3\nimport os\nopen("pid", "w").write(str(os.getpid()))\nwhile True: os.write(1,b"x"*65536)\n');executable.chmod(0o700)
    cli=StablePluginCLI(executable)
    with pytest.raises(PackageError):await cli._command(['plugin','list','--json'],home=tmp_path,cwd=tmp_path)
    with pytest.raises(ProcessLookupError):os.kill(int((tmp_path/'pid').read_text()),0)


@pytest.mark.anyio
async def test_installer_path_traversal_never_becomes_package_root(tmp_path,package,snapshot):
    class UnsafeCLI(StablePluginCLI):
        async def _command(self,args,*,home,cwd):
            if args[:2]==['plugin','marketplace']:return {}
            if args[:2]==['plugin','add']:
                return {'name':'notion','marketplaceName':'rocoto-pinned-api','version':'0.1.7',
                    'installedPath':str(home/'plugins/cache/rocoto-pinned-api/notion/../../../../../outside')}
            raise AssertionError('Traversal should be rejected before inventory.')
    cli=UnsafeCLI()
    with pytest.raises(PackageError):
        async with cli.acquire(snapshot['plugins'][0],snapshot):raise AssertionError('Unexpected package')


@pytest.mark.anyio
async def test_snapshot_detects_generation_change_while_verifying(tmp_path,package,snapshot,monkeypatch):
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='owner')
    real=store._verified
    def raced(body):
        value=real(body)
        with store._db() as db:db.execute('UPDATE owners SET generation=generation+1')
        return value
    monkeypatch.setattr(store,'_verified',raced)
    with pytest.raises(PackageError):store.snapshot(principal_id='owner')


@pytest.mark.anyio
async def test_cancel_during_accepted_spawn_drains_then_reaps(tmp_path,monkeypatch):
    import native_packages
    executable=tmp_path/'slow-spawn';executable.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n');executable.chmod(0o700)
    real=native_packages.asyncio.create_subprocess_exec;accepted=asyncio.Event();release=asyncio.Event();processes=[]
    async def delayed(*args,**kwargs):
        process=await real(*args,**kwargs);processes.append(process);accepted.set();await release.wait();return process
    monkeypatch.setattr(native_packages.asyncio,'create_subprocess_exec',delayed)
    cli=StablePluginCLI(executable);task=asyncio.create_task(cli._command(['plugin','list','--json'],home=tmp_path,cwd=tmp_path))
    await accepted.wait();task.cancel();await asyncio.sleep(.01);assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):os.kill(processes[0].pid,0)


@pytest.mark.anyio
async def test_native_timeout_closes_both_pipe_transports(tmp_path):
    executable=tmp_path/'slow-pipes';executable.write_text('#!/usr/bin/python3\nimport os,time\nos.write(1,b"partial");os.write(2,b"partial");time.sleep(30)\n');executable.chmod(0o700)
    cli=StablePluginCLI(executable,timeout=.2)
    with pytest.raises(PackageError):await cli._command(['plugin','list','--json'],home=tmp_path,cwd=tmp_path)

@pytest.mark.anyio
async def test_external_revision_refresh_is_not_hidden_by_unchanged_catalogue_revision(tmp_path,package,snapshot):
    row=snapshot['plugins'][0]
    row['catalogue_entry']['source']={'source':'git-subdir','url':'https://github.com/publisher/plugin.git','path':'package'}
    row['resolved_source']={**row['catalogue_entry']['source'],'sha':'b'*40}
    store,fake=make(tmp_path,package,snapshot)
    await store.install('notion',principal_id='alice')
    first=store.snapshot(principal_id='alice')
    assert fake.calls==1
    assert first.packages[0].upstream_revision=='b'*40
    store.entries['notion']['resolved_source']['sha']='c'*40
    await store.install('notion',principal_id='alice')
    assert fake.calls==2
    assert store.snapshot(principal_id='alice').generation==first.generation+1
    with pytest.raises(PackageError):store.validate_snapshot(first,principal_id='alice')
    from native_packages import _principal
    assert store._rows(_principal('alice'))[1][0]['source']['sha']=='c'*40

@pytest.mark.anyio
async def test_external_original_skills_retain_actual_package_revision(tmp_path,package,snapshot):
    from native_skills import NativePluginSkills
    row=snapshot['plugins'][0]
    row['catalogue_entry']['source']={'source':'git-subdir','url':'https://github.com/publisher/plugin.git','path':'package'}
    row['resolved_source']={**row['catalogue_entry']['source'],'sha':'b'*40}
    row['skills']=[{'name':'notes','path':'package/skills/notes/SKILL.md','metadata_status':'available','description':'Original notes.'}]
    store,fake=make(tmp_path,package,snapshot)
    installed=await store.install('notion',principal_id='alice')
    assert installed['upstream_revision']=='b'*40
    assert store._rows(__import__('native_packages')._principal('alice'))[1][0]['catalogue_revision']=='a'*40
    skills=NativePluginSkills(store).list('alice')
    assert len(skills)==1 and skills[0]['command']=='notion:notes'
    assert NativePluginSkills(store).list('bob')==[]
    store.entries['notion']['resolved_source']['sha']='c'*40
    assert NativePluginSkills(store).list('alice')==skills
    await store.install('notion',principal_id='alice')
    assert NativePluginSkills(store).list('alice')[0]['id']!=skills[0]['id']
