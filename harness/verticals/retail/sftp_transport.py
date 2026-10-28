"""Fixed read-only SFTP transport: exact host key, public socket, bounded reads."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict,dataclass,field
import hashlib
import json
import posixpath
import re
import socket
import stat

import asyncssh
from custom_http import public_addresses,validated_addresses

class SFTPError(ValueError):pass
def require(value,code='sftp_invalid_configuration'):
    if not value:raise SFTPError(code)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def text(value,maximum):return isinstance(value,str)and 0<len(value.encode())<=maximum and not any(ord(c)<32 or ord(c)==127 for c in value)

def validate_target(value):
    require(isinstance(value,dict)and set(value)=={'host','port','root','host_key'})
    host,port,root,key=value['host'],value['port'],value['root'],value['host_key']
    require(text(host,253)and re.fullmatch(r'[a-zA-Z0-9.:-]+',host)and not host.endswith('.'))
    require(type(port)is int and 1<=port<=65535)
    require(text(root,1024)and root.startswith('/')and '\\'not in root and posixpath.normpath(root)==root and not root.startswith('//'))
    require(isinstance(key,str)and text(key.strip(),8192))
    try:parsed=asyncssh.import_public_key(key.strip())
    except Exception:raise SFTPError('sftp_invalid_host_key')from None
    require(not parsed.get_algorithm().endswith('-cert-v01@openssh.com'),'sftp_exact_host_key_required')
    return {'host':host.lower(),'port':port,'root':root,'host_key':parsed.export_public_key('openssh').decode().strip()}

@dataclass(frozen=True)
class SFTPConfig:
    company_id:str
    owner_id:str
    collection:str
    source_id:str
    generation:int
    host:str
    port:int
    root:str
    host_key:str
    mode:str='one_time'
    stable_seconds:int=60
    poll_seconds:int=60
    def __post_init__(self):
        require(all(text(v,160)for v in(self.company_id,self.owner_id,self.collection,self.source_id)))
        require(type(self.generation)is int and self.generation>=1)
        normalized=validate_target({k:getattr(self,k)for k in ('host','port','root','host_key')})
        for key,value in normalized.items():object.__setattr__(self,key,value)
        require(self.mode in {'one_time','watch'}and type(self.stable_seconds)is int and 30<=self.stable_seconds<=86400
            and type(self.poll_seconds)is int and 30<=self.poll_seconds<=86400)
    @property
    def sha256(self):return digest(asdict(self))
    @property
    def target_sha256(self):return digest({k:getattr(self,k)for k in('host','port','root','host_key')})
    @property
    def fingerprint(self):return asyncssh.import_public_key(self.host_key).get_fingerprint('sha256')

@dataclass(frozen=True)
class SFTPCredentials:
    config_sha256:str
    username:str=field(repr=False)
    password:str|None=field(default=None,repr=False)
    private_key:str|None=field(default=None,repr=False)
    passphrase:str|None=field(default=None,repr=False)
    def __post_init__(self):
        require(re.fullmatch(r'[a-f0-9]{64}',self.config_sha256 or '')and text(self.username,256))
        require((self.password is None)!=(self.private_key is None),'sftp_credentials_required')
        if self.password is not None:require(text(self.password,8192)and self.passphrase is None)
        if self.private_key is not None:require(isinstance(self.private_key,str)and 1<=len(self.private_key.encode())<=32768)
        if self.passphrase is not None:require(text(self.passphrase,8192))

@dataclass(frozen=True)
class RemoteFile:
    name:str
    size:int
    mtime:int
    mtime_ns:int
    permissions:int
    @classmethod
    def from_attrs(cls,name,attrs):
        require(text(name,255)and name not in {'.','..'}and '/'not in name and '\\'not in name,'sftp_invalid_filename')
        require(type(attrs.size)is int and attrs.size>0 and type(attrs.mtime)is int and attrs.mtime>=0
            and type(attrs.permissions)is int and stat.S_ISREG(attrs.permissions),'sftp_regular_file_required')
        nanos=attrs.mtime_ns or 0;require(type(nanos)is int and 0<=nanos<10**9,'sftp_invalid_metadata')
        return cls(name,attrs.size,attrs.mtime,nanos,attrs.permissions)
    @property
    def version(self):return digest(asdict(self))

EXTENSIONS={'.mp4','.mov','.mkv','.avi','.webm','.m4v','.ts','.mts','.m2ts'}
CHUNK_BYTES=1024*1024
CLOSE_SECONDS=2
MAX_DIRECTORY_ENTRIES=10000

async def public_socket(config,current):
    values=validated_addresses(await public_addresses(config.host))
    require(current(),'sftp_authority_changed')
    address=values[0];family=socket.AF_INET6 if ':'in address else socket.AF_INET
    sock=socket.socket(family,socket.SOCK_STREAM);sock.setblocking(False)
    try:
        async with asyncio.timeout(10):await asyncio.get_running_loop().sock_connect(sock,(address,config.port))
        require(current(),'sftp_authority_changed')
        return sock
    except BaseException:sock.close();raise

class SFTPReader:
    def __init__(self,config,client,current,private_values=()):
        self.config,self.client,self.current=config,client,current
        self._private_values=tuple(value for value in private_values if value)
    def metadata(self,value):
        require(not any(secret in value for secret in self._private_values),'sftp_credential_echo')
    def check(self):require(self.current(),'sftp_authority_changed')
    async def path(self,name=None):
        self.check();path=posixpath.join(self.config.root,name)if name else self.config.root
        require(await self.client.realpath(path)==path,'sftp_path_changed')
        attrs=await self.client.lstat(path)
        if name is None:require(attrs.permissions is not None and stat.S_ISDIR(attrs.permissions),'sftp_directory_required')
        else:RemoteFile.from_attrs(name,attrs)
        self.check();return path,attrs
    async def list_page(self,cursor='',limit=64):
        require(isinstance(cursor,str)and len(cursor.encode())<=255 and type(limit)is int and 1<=limit<=64)
        path,_=await self.path();entries=[];count=0
        async for entry in self.client.scandir(path):
            self.check();count+=1;require(count<=MAX_DIRECTORY_ENTRIES,'sftp_directory_limit')
            name=entry.filename
            require(isinstance(name,str)and len(name.encode())<=255,'sftp_invalid_filename')
            self.metadata(name)
            if name in {'.','..'}or name.startswith('.')or posixpath.splitext(name)[1].lower()not in EXTENSIONS:continue
            if entry.attrs.permissions is None or not stat.S_ISREG(entry.attrs.permissions):continue
            if entry.attrs.size==0:continue
            item=RemoteFile.from_attrs(name,entry.attrs)
            if name>cursor:entries.append(item)
        # SFTP directory handles expire with the connection. A bounded fresh
        # listing plus a lexical cursor replays safely across reconnects; every
        # completed sweep starts at the beginning to include late names.
        entries.sort(key=lambda x:x.name);page=entries[:limit]
        return page,(page[-1].name if len(entries)>limit else '')
    @asynccontextmanager
    async def open(self,expected):
        path,attrs=await self.path(expected.name)
        require(RemoteFile.from_attrs(expected.name,attrs)==expected,'sftp_version_changed')
        handle=await self.client.open(path,'rb',encoding=None,block_size=CHUNK_BYTES,max_requests=1)
        try:
            require(RemoteFile.from_attrs(expected.name,await handle.stat())==expected,'sftp_version_changed')
            yield handle
            self.check()
            require(RemoteFile.from_attrs(expected.name,await handle.stat())==expected,'sftp_version_changed')
            _,attrs=await self.path(expected.name)
            require(RemoteFile.from_attrs(expected.name,attrs)==expected,'sftp_version_changed')
        finally:
            # A cancelled read must not wait forever for a remote CLOSE reply.
            # The enclosing SSH context aborts its socket after this bounded wait.
            async with asyncio.timeout(CLOSE_SECONDS):await handle.close()
    async def read(self,handle,offset,length):
        self.check();require(type(offset)is int and offset>=0 and type(length)is int and 1<=length<=CHUNK_BYTES)
        async with asyncio.timeout(15):value=await handle.read(length,offset)
        self.check();require(isinstance(value,bytes)and 0<len(value)<=length,'sftp_short_read');return value

@asynccontextmanager
async def connect(config,credentials,current,*,socket_factory=public_socket):
    """socket_factory is host-only fixture injection, never a serialized setting."""
    require(isinstance(config,SFTPConfig)and isinstance(credentials,SFTPCredentials)and credentials.config_sha256==config.sha256)
    require(current(),'sftp_authority_changed');sock=None;connection=None
    try:
        host_key=asyncssh.import_public_key(config.host_key)
        keys=[]
        if credentials.private_key is not None:keys=[asyncssh.import_private_key(credentials.private_key,credentials.passphrase)]
        sock=await socket_factory(config,current);require(current(),'sftp_authority_changed')
        async with asyncio.timeout(15):
            connection=await asyncssh.connect(sock=sock,config=None,known_hosts=([host_key],[],[]),
                username=credentials.username,password=credentials.password,client_keys=keys,
                agent_path=None,agent_forwarding=False,proxy_command=None,tunnel=None,
                host_based_auth=False,gss_auth=False,gss_kex=False,kbdint_auth=False,
                public_key_auth=bool(keys),password_auth=credentials.password is not None,
                preferred_auth=['publickey']if keys else ['password'],compression_algs=['none'],
                login_timeout=10,connect_timeout=10,keepalive_interval=0)
            client=await connection.start_sftp_client(env={},send_env=[],sftp_version=3)
        require(current(),'sftp_authority_changed')
        yield SFTPReader(config,client,current,(credentials.username,credentials.password,credentials.private_key,credentials.passphrase))
    except asyncio.CancelledError:raise
    except SFTPError:raise
    except asyncssh.HostKeyNotVerifiable:raise SFTPError('sftp_host_key_mismatch')from None
    except asyncssh.PermissionDenied:raise SFTPError('sftp_authentication_failed')from None
    except (asyncssh.Error,OSError,TimeoutError):raise SFTPError('sftp_connection_failed')from None
    finally:
        if connection:
            connection.abort()
            task=asyncio.create_task(connection.wait_closed())
            while not task.done():
                try:await asyncio.shield(task)
                except asyncio.CancelledError:continue
            task.result()
        if sock:sock.close()
