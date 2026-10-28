"""Fixed offline native CLI materializer. Run only inside its quota sandbox.

No model calls, package hooks, credentials or network. The broker supplies an
immutable archive and exact request; this program writes only the fixed workspace.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile

ARCHIVE_BYTES=32*1024*1024
SCAN_BYTES=512*1024*1024
SCAN_ENTRIES=100000
PACKAGE_BYTES=128*1024*1024
FILE_BYTES=64*1024*1024
PACKAGE_FILES=8192
PACKAGE_ENTRIES=16384
CODEX='/usr/local/bin/codex'
MARKETPLACE='rocoto-pinned-api'

class WorkerError(ValueError):pass

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def sha(value):return hashlib.sha256(value).hexdigest()

def request(value):
    if not isinstance(value,dict) or set(value)!={'source','archive_sha256','archive_bytes'}:raise WorkerError('invalid_install_request')
    source=value['source']
    if not isinstance(source,dict) or set(source)!={'plugin_id','repository','revision','subtree','catalogue_revision'}:raise WorkerError('invalid_install_source')
    if not isinstance(source['plugin_id'],str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',source['plugin_id']):raise WorkerError('invalid_install_source')
    if not isinstance(source['repository'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',source['repository']):raise WorkerError('invalid_install_source')
    for key in ('revision','catalogue_revision'):
        if not isinstance(source[key],str) or not re.fullmatch(r'[a-f0-9]{40}',source[key]):raise WorkerError('invalid_install_source')
    if source['subtree']:parts(source['subtree'])
    elif source['subtree']!='':raise WorkerError('invalid_install_source')
    if type(value['archive_bytes']) is not int or not 0<value['archive_bytes']<=ARCHIVE_BYTES or not isinstance(value['archive_sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',value['archive_sha256']):raise WorkerError('invalid_install_archive')
    return value

def parts(value):
    if not isinstance(value,str) or not value or len(value.encode())>2048:raise WorkerError('invalid_archive_path')
    items=value.split('/')
    if len(items)>24 or any(not p or p in {'.','..'} or '\\' in p or '\0' in p or len(p.encode())>240 for p in items):raise WorkerError('invalid_archive_path')
    return items

def verify_archive(path,expected):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size!=expected['archive_bytes']:
        os.close(fd);raise WorkerError('archive_mismatch')
    stream=os.fdopen(fd,'rb');h=hashlib.sha256()
    while data:=stream.read(65536):h.update(data)
    if h.hexdigest()!=expected['archive_sha256']:
        stream.close();raise WorkerError('archive_mismatch')
    stream.seek(0);return stream

def extract_selected(archive,expected,destination):
    """Stream selected subtree only, rejecting links/devices and ambiguous names."""
    expected=request(expected);source=expected['source'];destination=Path(destination)
    root=source['repository'].split('/')[1]+'-'+source['revision']
    prefix=root+('/'+source['subtree'] if source['subtree'] else '')
    seen=set();selected=set();scan=entries=total=files=0
    destination.mkdir(mode=0o700,parents=True,exist_ok=False)
    with verify_archive(archive,expected) as stream,tarfile.open(fileobj=stream,mode='r|gz') as bundle:
        for item in bundle:
            entries+=1;scan+=item.size
            if entries>SCAN_ENTRIES or scan>SCAN_BYTES or item.size<0:raise WorkerError('archive_scan_limit')
            name=item.name.rstrip('/') if item.isdir() else item.name
            parts(name)
            if name!=root and not name.startswith(root+'/'):raise WorkerError('archive_root_mismatch')
            if name in seen:raise WorkerError('duplicate_archive_entry')
            seen.add(name)
            if name==prefix:
                if not item.isdir():raise WorkerError('invalid_package_root')
                continue
            if not name.startswith(prefix+'/'):continue
            relative=name[len(prefix)+1:];parts(relative);target=destination/relative
            selected.add(relative)
            if len(selected)>PACKAGE_ENTRIES:raise WorkerError('package_entry_limit')
            if item.isdir():target.mkdir(mode=0o700,parents=True,exist_ok=True);continue
            if not item.isfile() or item.islnk() or item.issym():raise WorkerError('unsupported_package_entry')
            files+=1;total+=item.size
            if files>PACKAGE_FILES or item.size>FILE_BYTES or total>PACKAGE_BYTES:raise WorkerError('package_size_limit')
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with bundle.extractfile(item) as source_stream,target.open('xb') as output:
                remaining=item.size
                while remaining:
                    data=source_stream.read(min(remaining,65536))
                    if not data:raise WorkerError('archive_incomplete')
                    output.write(data);remaining-=len(data)
                output.flush();os.fsync(output.fileno());os.fchmod(output.fileno(),0o700 if item.mode&0o111 else 0o600)
    if not files or not (destination/'.codex-plugin/plugin.json').is_file():raise WorkerError('package_manifest_missing')
    return {'files':files,'bytes':total}

def tree_inventory(root):
    files=[];directories=[];total=0
    for path in sorted(Path(root).rglob('*')):
        info=path.lstat();name=path.relative_to(root).as_posix();parts(name)
        if stat.S_ISDIR(info.st_mode):directories.append(name)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
            total+=info.st_size
            if info.st_size>FILE_BYTES or total>PACKAGE_BYTES:raise WorkerError('package_size_limit')
            files.append({'path':name,'size_bytes':info.st_size,'sha256':sha(path.read_bytes()),'executable':bool(info.st_mode&0o111)})
        else:raise WorkerError('unsupported_installed_entry')
        if len(files)>PACKAGE_FILES or len(files)+len(directories)>PACKAGE_ENTRIES:raise WorkerError('package_entry_limit')
    # Canonical relative strings must match the broker and retained host tree.
    # Path ordering compares components: review/ sorts before review-resolver/,
    # while the serialized full path has the opposite lexical order.
    files.sort(key=lambda item:item['path']);directories.sort()
    return {'files':files,'directories':directories}

def materialize(archive,document,workspace,*,execute=subprocess.run):
    document=request(document);workspace=Path(workspace);source=document['source'];market=workspace/'market';home=workspace/'home'
    home.mkdir(mode=0o700);(market/'.agents/plugins').mkdir(mode=0o700,parents=True)
    original=market/'plugins'/source['plugin_id'];extract_selected(archive,document,original)
    inventory=tree_inventory(original)
    manifest_path=original/'.codex-plugin/plugin.json'
    if manifest_path.stat().st_size>1024*1024:raise WorkerError('package_manifest_limit')
    manifest=json.loads(manifest_path.read_bytes())
    if manifest.get('name')!=source['plugin_id']:raise WorkerError('package_manifest_mismatch')
    (market/'.agents/plugins/marketplace.json').write_bytes(canonical({'name':MARKETPLACE,'plugins':[{'name':source['plugin_id'],'source':{'source':'local','path':'./plugins/'+source['plugin_id']}}]}))
    config={'features.plugins':True,'features.apps':False,'features.hooks':False,'features.remote_plugin':False,'features.skip_host_skill_discovery':True,'mcp_servers':{},
        'model_provider':'installer_closed','model_providers.installer_closed.name':'Closed installer','model_providers.installer_closed.base_url':'http://127.0.0.1:1/v1',
        'model_providers.installer_closed.requires_openai_auth':False,'model_providers.installer_closed.wire_api':'responses'}
    argv=[CODEX]+[part for key,value in config.items() for part in ('-c',key+'='+json.dumps(value))]
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(home),'CODEX_HOME':str(home),'LANG':'C.UTF-8',
        'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0','GIT_ASKPASS':'/bin/false','SSH_ASKPASS':'/bin/false','GIT_SSH_COMMAND':'/bin/false'}
    def run(arguments):
        # Outputs live inside the same fixed ext4 quota, never an unbounded host pipe.
        out=workspace/'cli.stdout';err=workspace/'cli.stderr'
        with out.open('wb') as stdout,err.open('wb') as stderr:
            result=execute(argv+arguments,cwd=market,env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,timeout=40,check=False)
        if result.returncode or out.stat().st_size>2*1024*1024 or err.stat().st_size>128*1024:raise WorkerError('native_install_failed')
        try:return json.loads(out.read_bytes())
        except ValueError:raise WorkerError('native_install_invalid_result') from None
    run(['plugin','marketplace','add',str(market),'--json'])
    installed=run(['plugin','add',source['plugin_id']+'@'+MARKETPLACE,'--json'])
    version=installed.get('version');path=Path(installed.get('installedPath',''))
    if not isinstance(version,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}',version) or path!=home/'plugins/cache'/MARKETPLACE/source['plugin_id']/version:raise WorkerError('native_install_wrong_package')
    status=run(['plugin','list','--json'])
    rows=[r for r in status.get('installed',[]) if r.get('pluginId')==source['plugin_id']+'@'+MARKETPLACE]
    if len(rows)!=1 or not rows[0].get('installed') or not rows[0].get('enabled'):raise WorkerError('native_install_unverified')
    if tree_inventory(path)!=inventory:raise WorkerError('native_package_bytes_changed')
    output=workspace/'output';output.mkdir(mode=0o700);path.rename(output/'package')
    result={'schema_version':1,'source':source,'archive_sha256':document['archive_sha256'],'archive_bytes':document['archive_bytes'],
        'version':version,'tree_sha256':sha(canonical(inventory)),**inventory,'native':{'pluginId':source['plugin_id']+'@'+MARKETPLACE,'installed':True,'enabled':True}}
    encoded=canonical(result)
    if len(encoded)>2*1024*1024:raise WorkerError('package_metadata_limit')
    (output/'result.json').write_bytes(encoded)
    return result

if __name__=='__main__':
    try:
        data=Path('/input/request.json').read_bytes()
        if len(data)>8192:raise WorkerError('install_request_limit')
        materialize(Path('/input/source.tar.gz'),json.loads(data),Path('/workspace'))
    except Exception:
        # Detailed source content/native output stays inside the quota sandbox.
        raise SystemExit('native_plugin_installation_failed') from None
