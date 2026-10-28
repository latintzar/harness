"""Fixed stdlib entrypoint for the network-isolated custom read adapter profile.

Source and returned JSON are untrusted. Only the host gateway journal attests
HTTP activity; this runner never creates activation evidence or sees credentials.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

PROFILE='https-python-read-v1'
SOURCE_BYTES=1024*1024
SOURCE_FILES=32
FILE_BYTES=128*1024
DOCUMENT_BYTES=128*1024
ARGUMENT_BYTES=16*1024
RESULT_BYTES=512*1024
RUN_SECONDS=35
SOCKET='/run/rocoto/custom-http.sock'
PYTHON='/usr/local/bin/python3'
HASH=re.compile(r'[a-f0-9]{64}')
NAME=re.compile(r'[a-z][a-z0-9_]{0,63}')

class WorkerError(ValueError):pass
def need(value):
    if not value:raise WorkerError('invalid_custom_job')
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def path_parts(value):
    need(isinstance(value,str)and 1<=len(value.encode())<=1024)
    parts=value.split('/')
    need(len(parts)<=16 and all(p and p not in {'.','..'}and '\\'not in p and '\x00'not in p and len(p.encode())<=200 for p in parts))
    return parts

def request(value):
    need(isinstance(value,dict)and set(value)=={'snapshot_sha256','document_bytes','source_bytes','source_files','tool','arguments_sha256'})
    need(all(isinstance(value[k],str)and HASH.fullmatch(value[k])for k in ('snapshot_sha256','arguments_sha256')))
    need(type(value['document_bytes'])is int and 1<=value['document_bytes']<=DOCUMENT_BYTES)
    need(type(value['source_bytes'])is int and 1<=value['source_bytes']<=SOURCE_BYTES)
    need(type(value['source_files'])is int and 1<=value['source_files']<=SOURCE_FILES)
    need(isinstance(value['tool'],str)and NAME.fullmatch(value['tool']))
    return value

def bundle(value,expected,*,company,owner,image,worker_sha256):
    expected=request(expected)
    need(isinstance(value,dict)and set(value)=={'snapshot','arguments'})
    data=value['snapshot'];need(isinstance(data,dict))
    raw=canonical(data);need(len(raw)==expected['document_bytes']and hashlib.sha256(raw).hexdigest()==expected['snapshot_sha256'])
    need(data.get('schema')==1 and data.get('company')==company and data.get('owner')==owner)
    need(data.get('runtime')=={'profile':PROFILE,'image_digest':image,'worker_sha256':worker_sha256})
    files=data.get('files');need(isinstance(files,list)and len(files)==expected['source_files'])
    names=set();total=0
    for item in files:
        need(isinstance(item,dict)and set(item)=={'path','size_bytes','sha256'})
        parts=path_parts(item['path']);need(item['path']not in names)
        need(type(item['size_bytes'])is int and 0<=item['size_bytes']<=FILE_BYTES)
        need(isinstance(item['sha256'],str)and HASH.fullmatch(item['sha256']))
        names.add(item['path']);total+=item['size_bytes']
    need(total==expected['source_bytes']and not any('/'.join(path_parts(name)[:i])in names for name in names for i in range(1,len(path_parts(name)))))
    manifest=data.get('manifest');need(isinstance(manifest,dict)and manifest.get('profile')==PROFILE)
    need(manifest.get('entrypoint')in names and manifest['entrypoint'].endswith('.py'))
    need(digest(manifest)==data.get('manifest_sha256'))
    tools=manifest.get('tools');need(isinstance(tools,list)and len(tools)<=16)
    selected=[t for t in tools if isinstance(t,dict)and t.get('name')==expected['tool']];need(len(selected)==1)
    need(isinstance(value['arguments'],dict)and len(canonical(value['arguments']))<=ARGUMENT_BYTES and digest(value['arguments'])==expected['arguments_sha256'])
    return value

def read_regular(path,limit):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd);need(stat.S_ISREG(info.st_mode)and info.st_nlink==1 and info.st_size<=limit)
        with os.fdopen(fd,'rb',closefd=False)as stream:raw=stream.read(limit+1)
        need(len(raw)<=limit);return raw
    finally:os.close(fd)

def run(source,config,workspace,*,execute=subprocess.run):
    source,workspace=Path(source),Path(workspace)
    need(isinstance(config,dict)and set(config)=={'snapshot_sha256','entrypoint','tool','arguments','gateway_token'})
    path_parts(config['entrypoint']);need(config['entrypoint'].endswith('.py'))
    need(isinstance(config['gateway_token'],str)and re.fullmatch(r'[A-Za-z0-9_-]{32,128}',config['gateway_token']))
    document={'tool':config['tool'],'arguments':config['arguments'],'gateway':{'socket':SOCKET,'token':config['gateway_token']}}
    # The entire container has a fixed quota and wall deadline. Raw diagnostics
    # stay in that quota and never cross the broker response boundary.
    stdin=workspace/'stdin.json';stdin.write_bytes(canonical(document));stdin.chmod(0o600)
    with stdin.open('rb')as incoming,(workspace/'stdout').open('wb')as outgoing,(workspace/'stderr').open('wb')as errors:
        result=execute([PYTHON,'-I',str(source/config['entrypoint'])],stdin=incoming,stdout=outgoing,stderr=errors,
            cwd=source,env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/workspace','TMPDIR':'/tmp'},timeout=RUN_SECONDS,check=False)
    need(result.returncode==0)
    raw=read_regular(workspace/'stdout',RESULT_BYTES);value=json.loads(raw)
    need(isinstance(value,dict)and len(canonical(value))<=RESULT_BYTES)
    # Host revalidates this untrusted result's schema and obtains operation
    # receipts from its own gateway journal, never from adapter output.
    final=workspace/'result.json';final.write_bytes(canonical(value));final.chmod(0o600)
    return value

if __name__=='__main__':
    try:run('/source',json.loads(read_regular('/input/run.json',DOCUMENT_BYTES)),Path('/workspace'))
    except BaseException:raise SystemExit('custom_adapter_failed')from None
