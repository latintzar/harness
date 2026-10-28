import asyncio
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
from types import SimpleNamespace

import httpx
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
import native_install_source as source
import native_install_worker as worker

@pytest.fixture
def anyio_backend():return 'asyncio'

SOURCE={'plugin_id':'notion','repository':'openai/plugins','revision':'a'*40,'subtree':'plugins/notion','catalogue_revision':'a'*40}
ROOT='plugins-'+'a'*40
PREFIX=ROOT+'/plugins/notion/'

def archive(tmp_path,entries=None):
    values=entries or [('.codex-plugin/plugin.json',json.dumps({'name':'notion','version':'0.1.7'}).encode()),
                      ('skills/notes/SKILL.md',b'---\nname: notes\ndescription: Notes.\n---\nExact original skill\n'),('LICENSE',b'MIT fixture\n')]
    path=tmp_path/'source.tar.gz'
    with tarfile.open(path,'w:gz') as stream:
        for name,value in values:
            if isinstance(value,tarfile.TarInfo):info=value;info.name=PREFIX+name;stream.addfile(info);continue
            info=tarfile.TarInfo(PREFIX+name);info.size=len(value);info.mode=0o755 if name.endswith('.sh') else 0o644
            stream.addfile(info,io.BytesIO(value))
    data=path.read_bytes()
    return path,{'source':dict(SOURCE),'archive_sha256':hashlib.sha256(data).hexdigest(),'archive_bytes':len(data)}

def entry(source_value=None):return {'id':'notion','supported_api':True,'catalogue_entry':{'source':source_value or {'source':'local','path':'./plugins/notion'}}}

def test_canonical_source_and_explicit_pinned_external_source():
    spec=source.source_spec(entry(),{'revision':'a'*40})
    assert spec.url=='https://codeload.github.com/openai/plugins/tar.gz/'+'a'*40
    assert spec.subtree=='plugins/notion'
    external=source.source_spec(entry({'source':'git-subdir','url':'https://github.com/publisher/repo.git','path':'package','sha':'b'*40}),{'revision':'a'*40})
    assert external.repository=='publisher/repo' and external.revision=='b'*40

@pytest.mark.parametrize('value',[
 {'source':'git-subdir','url':'https://github.com/publisher/repo.git','path':'package'},
 {'source':'git-subdir','url':'https://user:secret@github.com/publisher/repo.git','path':'package','sha':'b'*40},
 {'source':'git-subdir','url':'https://github.com.evil/repo.git','path':'package','sha':'b'*40},
 {'source':'local','path':'../../private'}, {'source':'local','path':'/private'}, {'source':'local','path':'plugins/../notion'},
])
def test_unpinned_private_or_escaping_sources_rejected(value):
    with pytest.raises(source.InstallSourceError):source.source_spec(entry(value),{'revision':'a'*40})

def test_extract_exact_original_files_empty_directories_and_modes(tmp_path):
    folder=tarfile.TarInfo();folder.type=tarfile.DIRTYPE
    path,document=archive(tmp_path,[('.codex-plugin/plugin.json',b'{"name":"notion","version":"0.1.7"}'),('run.sh',b'exit 0\n'),('empty',folder)])
    result=worker.extract_selected(path,document,tmp_path/'output')
    assert result['files']==2 and (tmp_path/'output/empty').is_dir()
    assert (tmp_path/'output/run.sh').stat().st_mode&0o111
    assert (tmp_path/'output/run.sh').read_bytes()==b'exit 0\n'

@pytest.mark.parametrize('kind',['symlink','hardlink','fifo','escape','duplicate','wrongroot'])
def test_archive_boundary_rejects_ambiguous_and_special_entries(tmp_path,kind):
    bad=tarfile.TarInfo();bad.type={'symlink':tarfile.SYMTYPE,'hardlink':tarfile.LNKTYPE,'fifo':tarfile.FIFOTYPE}.get(kind,tarfile.REGTYPE);bad.linkname='/etc/shadow'
    entries=[('.codex-plugin/plugin.json',b'{"name":"notion"}')]
    entries.append(('../escape',b'bad') if kind=='escape' else ('.codex-plugin/plugin.json',b'duplicate') if kind=='duplicate' else ('bad',bad))
    path,document=archive(tmp_path,entries)
    if kind=='wrongroot':document['source']['revision']='b'*40
    with pytest.raises((worker.WorkerError,OSError)):worker.extract_selected(path,document,tmp_path/'output')

@pytest.mark.parametrize('limit',['SCAN_BYTES','SCAN_ENTRIES','PACKAGE_BYTES','FILE_BYTES','PACKAGE_FILES','PACKAGE_ENTRIES'])
def test_resource_limits_fail_before_native_install(tmp_path,monkeypatch,limit):
    path,document=archive(tmp_path);monkeypatch.setattr(worker,limit,1)
    with pytest.raises(worker.WorkerError):worker.extract_selected(path,document,tmp_path/'output')

class Native:
    def __init__(self,alter=False):self.calls=[];self.alter=alter
    def __call__(self,args,**kwargs):
        self.calls.append((args,kwargs));home=Path(kwargs['env']['CODEX_HOME']);market=Path(kwargs['cwd']);plugin=home/'plugins/cache/rocoto-pinned-api/notion/0.1.7'
        assert set(kwargs['env'])=={'PATH','HOME','CODEX_HOME','LANG','GIT_CONFIG_NOSYSTEM','GIT_CONFIG_GLOBAL','GIT_TERMINAL_PROMPT','GIT_ASKPASS','SSH_ASKPASS','GIT_SSH_COMMAND'}
        assert args[0]=='/usr/local/bin/codex' and kwargs['timeout']==40
        command=args[args.index('plugin'):]
        if command[1]=='marketplace':value={'marketplaceName':'rocoto-pinned-api'}
        elif command[1]=='add':
            shutil.copytree(market/'plugins/notion',plugin)
            if self.alter:(plugin/'LICENSE').write_text('changed')
            value={'version':'0.1.7','installedPath':str(plugin)}
        elif command[1]=='list':value={'installed':[{'pluginId':'notion@rocoto-pinned-api','installed':True,'enabled':True}]}
        else:raise AssertionError(command)
        kwargs['stdout'].write(json.dumps(value).encode());return SimpleNamespace(returncode=0)

def test_native_cli_bytes_must_match_original_and_output_bound_to_archive(tmp_path):
    path,document=archive(tmp_path);run=Native();workspace=tmp_path/'workspace';workspace.mkdir()
    result=worker.materialize(path,document,workspace,execute=run)
    assert len(run.calls)==3 and result['source']==SOURCE and result['archive_sha256']==document['archive_sha256']
    assert result['tree_sha256']==worker.sha(worker.canonical({'files':result['files'],'directories':result['directories']}))
    assert (workspace/'output/package/LICENSE').read_bytes()==b'MIT fixture\n'
    assert (workspace/'output/result.json').exists()

def test_changed_native_install_bytes_fail_closed(tmp_path):
    path,document=archive(tmp_path);workspace=tmp_path/'workspace';workspace.mkdir()
    with pytest.raises(worker.WorkerError,match='bytes_changed'):worker.materialize(path,document,workspace,execute=Native(alter=True))
    assert not (workspace/'output').exists()

class Stream(httpx.AsyncByteStream):
    def __init__(self,values):self.values=values
    async def __aiter__(self):
        for value in self.values:yield value

@pytest.mark.anyio
async def test_public_fetch_uses_fixed_pinned_url_and_scrubbed_auth(tmp_path,monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://invalid.proxy:123');monkeypatch.setenv('OPENAI_API_KEY','secret')
    spec=source.source_spec(entry(),{'revision':'a'*40});seen=[]
    def remote(request):
        seen.append(request)
        assert request.url==spec.url and 'authorization' not in request.headers and 'cookie' not in request.headers
        return httpx.Response(200,headers={'content-length':'6'},stream=Stream([b'abc',b'def']))
    result=await source.fetch_archive(spec,tmp_path/'archive',transport=httpx.MockTransport(remote))
    assert len(seen)==1 and result['size_bytes']==6 and (tmp_path/'archive').read_bytes()==b'abcdef'

@pytest.mark.anyio
@pytest.mark.parametrize('failure',['redirect','oversize','truncated','encoding'])
async def test_bad_fetch_removes_partial_file(tmp_path,monkeypatch,failure):
    spec=source.source_spec(entry(),{'revision':'a'*40})
    def remote(request):
        if failure=='redirect':return httpx.Response(302,headers={'location':'https://evil.invalid/private'})
        if failure=='encoding':return httpx.Response(200,headers={'content-encoding':'gzip'},stream=Stream([b'bad']))
        return httpx.Response(200,headers={'content-length':'10' if failure=='truncated' else '1000000000'},stream=Stream([b'bad']))
    with pytest.raises(source.InstallSourceError):await source.fetch_archive(spec,tmp_path/'archive',transport=httpx.MockTransport(remote))
    assert not (tmp_path/'archive').exists()

@pytest.mark.anyio
async def test_cancelled_fetch_removes_partial_file(tmp_path):
    ready=asyncio.Event()
    class Wait(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first';ready.set();await asyncio.Event().wait()
    task=asyncio.create_task(source.fetch_archive(source.source_spec(entry(),{'revision':'a'*40}),tmp_path/'archive',transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Wait()))))
    await ready.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not (tmp_path/'archive').exists()
