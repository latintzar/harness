"""Private host client for immutable, credential-free custom adapter jobs."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass,field,asdict
import hashlib
import json
import re

import custom_worker as worker
import native_executor as native
from custom_execution import ExecutionSnapshot

@dataclass(frozen=True)
class CustomLease:
    id:str
    token:str=field(repr=False)
    scope_key:str
    socket_dir:str
    def __post_init__(self):
        if(not isinstance(self.id,str)or not re.fullmatch(r'[a-f0-9]{48}',self.id)
            or not isinstance(self.token,str)or not re.fullmatch(r'[a-f0-9]{64}',self.token)
            or not isinstance(self.scope_key,str)or not re.fullmatch(r'[a-f0-9]{64}',self.scope_key)
            or not isinstance(self.socket_dir,str)or not self.socket_dir.startswith('/')or len(self.socket_dir)>4096):
            raise native.ExecutorError('invalid_custom_lease')

def request_for(snapshot,tool,arguments):
    if not isinstance(snapshot,ExecutionSnapshot):raise native.ExecutorError('invalid_custom_snapshot')
    return worker.request({'snapshot_sha256':snapshot.sha256,'document_bytes':len(snapshot.document),
        'source_bytes':sum(len(f.content)for f in snapshot.files),'source_files':len(snapshot.files),
        'tool':tool,'arguments_sha256':worker.digest(arguments)})

class CustomClient:
    def __init__(self,socket_path=native.DEFAULT_SOCKET):self.socket_path=str(socket_path)
    async def _connect(self,operation,scope,**values):
        reader,writer=await asyncio.wait_for(asyncio.open_unix_connection(self.socket_path,limit=native.CONTROL_BYTES),5)
        try:
            raw=worker.canonical({'operation':operation,'scope':asdict(scope),**values})+b'\n'
            if len(raw)>native.CONTROL_BYTES:raise native.ExecutorError('custom_request_limit')
            writer.write(raw);await asyncio.wait_for(writer.drain(),5)
            response=json.loads(await asyncio.wait_for(reader.readline(),90))
            if response.get('error'):raise native.ExecutorError(response['error'])
            return response,reader,writer
        except BaseException:writer.close();await writer.wait_closed();raise
    async def _request(self,operation,scope,**values):
        response,_,writer=await self._connect(operation,scope,**values)
        writer.close();await writer.wait_closed();return response
    async def capabilities(self,scope):return await self._request('custom_capabilities',scope)
    async def prepare(self,scope,request_id,snapshot,tool,arguments):
        response=await self._request('custom_prepare',scope,request_id=request_id,document=request_for(snapshot,tool,arguments))
        return CustomLease(**response)
    async def recover(self,scope,request_id,*,retire=False):
        response=await self._request('custom_recover',scope,request_id=request_id,retire=retire)
        return CustomLease(**response['lease'])if response['lease']is not None else None
    async def status(self,scope,lease):return await self._request('custom_status',scope,lease=asdict(lease))
    async def stop(self,scope,lease):return await self._request('custom_stop',scope,lease=asdict(lease))
    async def release(self,scope,lease):return await self._request('custom_release',scope,lease=asdict(lease))
    async def start(self,scope,lease,gateway_token):return await self._request('custom_start',scope,lease=asdict(lease),capability=gateway_token)
    async def upload(self,scope,lease,snapshot,tool,arguments):
        request_for(snapshot,tool,arguments)
        _,reader,writer=await self._connect('custom_upload',scope,lease=asdict(lease))
        try:
            async with asyncio.timeout(30):
                await native._write_frame(writer,5,worker.canonical({'snapshot':snapshot.metadata,'arguments':arguments}))
                for item in snapshot.files:
                    for offset in range(0,len(item.content),65536):await native._write_frame(writer,6,item.content[offset:offset+65536])
                await native._write_frame(writer,7,b'')
                response=json.loads(await reader.readline())
                if response.get('error'):raise native.ExecutorError(response['error'])
                if response.get('snapshot_sha256')!=snapshot.sha256:raise native.ExecutorError('custom_snapshot_mismatch')
                return response
        finally:writer.close();await writer.wait_closed()
    async def result(self,scope,lease,snapshot,tool,arguments):
        request=request_for(snapshot,tool,arguments)
        _,reader,writer=await self._connect('custom_read',scope,lease=asdict(lease))
        try:
            async with asyncio.timeout(30):
                kind,raw=await native._read_package_frame(reader,8192)
                if kind!=5:raise native.ExecutorError('invalid_custom_output')
                info=json.loads(raw);expected={'schema_version','snapshot_sha256','tool','arguments_sha256','result_sha256','result_bytes','profile','image_digest','worker_sha256'}
                if(not isinstance(info,dict)or set(info)!=expected or info['schema_version']!=1
                    or any(info[k]!=request[k]for k in ('snapshot_sha256','tool','arguments_sha256'))
                    or any(info[k]!=v for k,v in snapshot.metadata['runtime'].items())
                    or type(info['result_bytes'])is not int or not 0<info['result_bytes']<=worker.RESULT_BYTES):raise native.ExecutorError('custom_output_mismatch')
                output=bytearray();remaining=info['result_bytes']
                while remaining:
                    kind,raw=await native._read_package_frame(reader,min(remaining,65536))
                    if kind!=6 or not raw:raise native.ExecutorError('invalid_custom_output')
                    output.extend(raw);remaining-=len(raw)
                kind,raw=await native._read_package_frame(reader,0)
                if kind!=7 or raw or hashlib.sha256(output).hexdigest()!=info['result_sha256']:raise native.ExecutorError('custom_output_mismatch')
                value=json.loads(output)
                if not isinstance(value,dict)or worker.canonical(value)!=output:raise native.ExecutorError('invalid_custom_output')
                return {**info,'broker_run_id':'custom-'+lease.id,'result':value,'cleanup_confirmed':True}
        finally:writer.close();await writer.wait_closed()
