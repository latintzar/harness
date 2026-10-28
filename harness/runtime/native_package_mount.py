"""Root-side validation of bounded original plugin trees; no account credentials.

The authenticated preview process supplies package bytes, never host paths. This
module is deliberately stdlib-only so the provisioning broker has a small closure.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MARKETPLACE = 'rocoto-pinned-api'
SELECTION_BYTES = 128 * 1024 * 1024
METADATA_BYTES = 2 * 1024 * 1024
FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 8192
MAX_DIRECTORIES = 16384
MAX_PACKAGES = 64
CHUNK_BYTES = 65536
UPLOAD_SECONDS = 45
CONFIG_BYTES = 32 * 1024
_HEX = re.compile(r'^[a-f0-9]{64}$')
_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$')

class PackageMountError(ValueError):
    pass

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()

def parts(value):
    if not isinstance(value, str) or len(value.encode()) > 2048:
        raise PackageMountError('invalid_package_path')
    result=value.split('/')
    if len(result)>24 or any(not x or x in {'.','..'} or '\\' in x or '\0' in x or len(x.encode())>240 for x in result):
        raise PackageMountError('invalid_package_path')
    return result

def validate_metadata(value, company, owner):
    if not isinstance(value,dict) or set(value)!={'company','owner','generation','selection_sha256','packages'}:
        raise PackageMountError('invalid_package_selection')
    if value['company']!=company or value['owner']!=hashlib.sha256(owner.encode()).hexdigest():
        raise PackageMountError('package_scope_mismatch')
    if type(value['generation']) is not int or value['generation']<0:
        raise PackageMountError('invalid_package_generation')
    packages=value['packages']
    if not isinstance(packages,list) or not packages or len(packages)>MAX_PACKAGES:
        raise PackageMountError('invalid_package_selection')
    ids=[];count=dirs=total=0
    for package in packages:
        if not isinstance(package,dict) or set(package)!={'id','version','tree_sha256','files','directories'}:
            raise PackageMountError('invalid_package_metadata')
        for field in ('id','version'):
            if not isinstance(package[field],str) or not _NAME.fullmatch(package[field]) or package[field] in {'.','..'}:
                raise PackageMountError('invalid_package_name')
        ids.append(package['id'])
        files=package['files'];directories=package['directories']
        if not isinstance(files,list) or not isinstance(directories,list):raise PackageMountError('invalid_package_inventory')
        names=[]
        for item in files:
            if not isinstance(item,dict) or set(item)!={'path','size_bytes','sha256','executable'}:raise PackageMountError('invalid_package_file')
            parts(item['path']);names.append(item['path'])
            if type(item['size_bytes']) is not int or not 0<=item['size_bytes']<=FILE_BYTES or type(item['executable']) is not bool or not isinstance(item['sha256'],str) or not _HEX.fullmatch(item['sha256']):
                raise PackageMountError('invalid_package_file')
            total+=item['size_bytes']
        for name in directories:parts(name)
        if names!=sorted(set(names)) or directories!=sorted(set(directories)) or set(names)&set(directories):raise PackageMountError('invalid_package_inventory')
        all_dirs=set(directories)
        for name in names+directories:
            p=parts(name)
            if any('/'.join(p[:n]) not in all_dirs for n in range(1,len(p))):raise PackageMountError('incomplete_package_inventory')
        if '.codex-plugin/plugin.json' not in names:raise PackageMountError('package_manifest_missing')
        if package['tree_sha256']!=digest({'files':files,'directories':directories}):raise PackageMountError('package_tree_mismatch')
        count+=len(files);dirs+=len(directories)
    if ids!=sorted(set(ids)) or total>SELECTION_BYTES or count>MAX_FILES or dirs>MAX_DIRECTORIES:raise PackageMountError('package_selection_limit')
    expected=digest({'company':company,'owner':value['owner'],'generation':value['generation'],
                     'packages':[(p['id'],p['version'],p['tree_sha256']) for p in packages]})
    if value['selection_sha256']!=expected:raise PackageMountError('package_selection_mismatch')
    return value

def selection_metadata(selection):
    from dataclasses import asdict
    return {'company':selection.company_scope,'owner':selection.owner_binding,'generation':selection.generation,
            'selection_sha256':selection.selection_sha256,'packages':[
                {'id':p.id,'version':p.version,'tree_sha256':p.tree_sha256,
                 'files':[asdict(f) for f in p.files],'directories':list(p.directories)} for p in selection.packages]}

def _read_json(path):
    if path.stat().st_size>1024*1024:raise PackageMountError('package_config_limit')
    try:return json.loads(path.read_bytes())
    except (ValueError,UnicodeError):raise PackageMountError('invalid_package_config') from None

def native_config(root, metadata):
    """Enable original skills; explicitly disable every bundled MCP server.

    Native plugin policy has no URL substitution. Provider access is separately
    granted through a host relay; package installation itself grants none.
    """
    plugins={}
    for package in metadata['packages']:
        tree=Path(root)/MARKETPLACE/package['id']/package['version']
        manifest=_read_json(tree/'.codex-plugin/plugin.json')
        if not isinstance(manifest,dict) or manifest.get('name')!=package['id'] or str(manifest.get('version'))!=package['version']:
            raise PackageMountError('package_manifest_mismatch')
        if (tree/'.agents/plugins').exists():
            raise PackageMountError('unsupported_package_manifest')
        # Codex 0.153.4 ignores unrelated root JSON, but a declared Agent Plugins
        # v1 manifest selects its fixed skills/ and mcp.json loader. Preserve the
        # original metadata-only form (including Qodo's dual manifests). Its
        # provider/extension routing needs a separately verified host policy.
        if (tree/'plugin.json').exists():
            original=_read_json(tree/'plugin.json')
            schema=original.get('$schema') if isinstance(original,dict) else None
            if isinstance(schema,str) and schema.startswith('https://agent-plugins.org/schemas/'):
                fields={'$schema','name','version','description','author','homepage','repository','license','keywords'}
                if (schema!='https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'
                        or set(original)-fields or original.get('name')!=package['id']
                        or original.get('version')!=package['version'] or (tree/'mcp.json').exists()):
                    raise PackageMountError('unsupported_package_manifest')
        mcp=manifest.get('mcpServers')
        if isinstance(mcp,str):
            mcp_path='/'.join(parts(mcp.removeprefix('./')))
            if mcp_path not in {f['path'] for f in package['files']}:raise PackageMountError('package_mcp_missing')
            mcp=_read_json(tree/mcp_path)
        elif mcp is None and (tree/'.mcp.json').exists():mcp=_read_json(tree/'.mcp.json')
        if mcp is None:mcp={}
        if not isinstance(mcp,dict):raise PackageMountError('unsupported_package_mcp')
        servers=mcp.get('mcpServers',mcp)
        if not isinstance(servers,dict) or any(not isinstance(k,str) or not _NAME.fullmatch(k) or not isinstance(v,dict) for k,v in servers.items()):
            raise PackageMountError('unsupported_package_mcp')
        plugins[package['id']+'@'+MARKETPLACE]={'enabled':True,'mcp_servers':{name:{'enabled':False} for name in sorted(servers)}}
    if len(encoded(plugins))>CONFIG_BYTES:raise PackageMountError('package_config_limit')
    return plugins

def seal(root):
    for directory,subdirs,files in os.walk(root,topdown=False):
        for name in files:
            path=Path(directory)/name
            path.chmod(0o555 if path.stat().st_mode&0o111 else 0o444)
        Path(directory).chmod(0o555)
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

def ensure_mount_target(volume):
    """Never follow model-created links when preparing Docker's nested target."""
    descriptor=os.open(Path(volume)/'codex-state',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for name in ('plugins','cache'):
            try:os.mkdir(name,mode=0o755,dir_fd=descriptor)
            except FileExistsError:pass
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            # The broker unit uses UMask0077. The worker must traverse these
            # verified mount parents even when root created them privately.
            os.close(descriptor);descriptor=child
            os.fchmod(descriptor,0o755)
    except OSError:raise PackageMountError('invalid_package_mount_target') from None
    finally:os.close(descriptor)

def open_source(root,relative):
    """Held descriptor chain prevents app-owned source symlinks from redirecting."""
    descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        path=parts(relative)
        for name in path[:-1]:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        fd=os.open(path[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            os.close(fd);raise PackageMountError('invalid_package_source')
        return os.fdopen(fd,'rb')
    finally:os.close(descriptor)


def toml_value(value):
    """Codex -c expects TOML values; JSON object colons are not TOML assignments."""
    if isinstance(value,dict):
        return '{'+','.join(json.dumps(key)+'='+toml_value(item) for key,item in value.items())+'}'
    if value is None:raise PackageMountError('invalid_native_config')
    return json.dumps(value)
