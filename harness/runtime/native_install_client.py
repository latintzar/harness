"""Scoped native package materializer using the fixed privileged installer.

Network fetches are bounded public source reads. Provider/account credentials are
never forwarded. Call acquire_for_principal only from an authenticated host scope.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import native_executor as native
import native_package_mount as packages
from native_install_source import fetch_archive,source_spec
from native_packages import PackageError,_scan_tree
from workspace_identity import workspace_scope

async def drain(task):
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:continue
    return task.result()

class InstallerClient:
    def __init__(self,socket_path=native.DEFAULT_SOCKET):self.socket_path=str(socket_path)
    async def _connect(self,operation,scope,**values):
        reader,writer=await asyncio.open_unix_connection(self.socket_path,limit=native.CONTROL_BYTES)
        try:
            data=json.dumps({'operation':operation,'scope':asdict(scope),**values}).encode()+b'\n'
            if len(data)>native.CONTROL_BYTES:raise native.ExecutorError('install_request_limit')
            writer.write(data);await writer.drain()
            response=json.loads(await asyncio.wait_for(reader.readline(),90))
            if response.get('error'):raise native.ExecutorError(response['error'])
            return response,reader,writer
        except BaseException:
            writer.close();await writer.wait_closed();raise
    async def _request(self,operation,scope,**values):
        response,_,writer=await self._connect(operation,scope,**values)
        writer.close();await writer.wait_closed();return response
    async def capabilities(self,scope):return await self._request('plugin_capabilities',scope)
    async def prepare(self,scope,request_id,document):
        return await self._request('plugin_prepare',scope,request_id=request_id,document=document)
    async def request(self,operation,scope,lease):
        return await self._request(operation,scope,**{'lease':lease})
    async def upload(self,scope,lease,path,document):
        _,reader,writer=await self._connect('plugin_upload',scope,**{'lease':lease})
        try:
            async with asyncio.timeout(60):
                with Path(path).open('rb') as stream:
                    while data:=stream.read(65536):await native._write_frame(writer,6,data)
                await native._write_frame(writer,7,b'')
                response=json.loads(await reader.readline())
                if response.get('error'):raise native.ExecutorError(response['error'])
                return response
        finally:writer.close();await writer.wait_closed()
    async def read(self,scope,lease,destination,document):
        _,reader,writer=await self._connect('plugin_read',scope,**{'lease':lease})
        try:
            async with asyncio.timeout(60):
                kind,raw=await native._read_package_frame(reader,packages.METADATA_BYTES)
                if kind!=5:raise native.ExecutorError('invalid_installer_output')
                result=json.loads(raw)
                if result.get('source')!=document['source'] or result.get('archive_sha256')!=document['archive_sha256']:raise native.ExecutorError('installer_result_mismatch')
                package={'id':document['source']['plugin_id'],'version':result.get('version'),'tree_sha256':result.get('tree_sha256'),'files':result.get('files'),'directories':result.get('directories')}
                owner=hashlib.sha256(scope.owner_id.encode()).hexdigest();metadata={'company':scope.company_id,'owner':owner,'generation':1,'packages':[package]}
                metadata['selection_sha256']=packages.digest({'company':scope.company_id,'owner':owner,'generation':1,'packages':[(package['id'],package['version'],package['tree_sha256'])]})
                packages.validate_metadata(metadata,scope.company_id,scope.owner_id)
                destination=Path(destination);destination.mkdir(mode=0o700)
                for directory in result['directories']:(destination/directory).mkdir(mode=0o700)
                for item in result['files']:
                    remaining=item['size_bytes'];hasher=hashlib.sha256()
                    with (destination/item['path']).open('xb') as stream:
                        while remaining:
                            kind,data=await native._read_package_frame(reader,min(65536,remaining))
                            if kind!=6 or not data:raise native.ExecutorError('invalid_installer_output')
                            stream.write(data);hasher.update(data);remaining-=len(data)
                        if hasher.hexdigest()!=item['sha256']:raise native.ExecutorError('installer_file_mismatch')
                        stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o700 if item['executable'] else 0o600)
                kind,data=await native._read_package_frame(reader,0)
                if kind!=7 or data:raise native.ExecutorError('invalid_installer_output')
                # NativePackageStore subsequently verifies held descriptors again.
                return result
        finally:writer.close();await writer.wait_closed()

class BrokerPluginInstaller:
    def __init__(self,state,*,client=None,fetch=fetch_archive):
        self.state=Path(state).absolute();self.company=workspace_scope(self.state)
        self.client=client or InstallerClient();self.fetch=fetch

    @asynccontextmanager
    async def acquire_for_principal(self,entry,catalogue,*,principal_id):
        spec=source_spec(entry,catalogue);attempt=uuid.uuid4().hex
        scope=native.NativeScope(self.company,principal_id,'plugin-install:'+attempt)
        lease=None;document=None
        self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.plugin-install-',dir=self.state) as temporary:
            root=Path(temporary)
            try:
                # Store/API admission already holds a finite per-company mutation
                # slot. Public download is one bounded attempt; no auto retry.
                received=await self.fetch(spec,root/'source.tar.gz')
                document={'source':spec.document(),'archive_sha256':received['sha256'],'archive_bytes':received['size_bytes']}
                preparing=asyncio.create_task(self.client.prepare(scope,attempt,document))
                try:lease=await asyncio.shield(preparing)
                except BaseException:
                    if not preparing.done():
                        try:lease=await drain(preparing)
                        except Exception:pass
                    if lease is None:
                        # Same request ID recovers a lost reservation response;
                        # prepare is idempotent and never starts a worker.
                        lease=await drain(asyncio.create_task(self.client.prepare(scope,attempt,document)))
                    raise
                await self.client.upload(scope,lease,root/'source.tar.gz',document)
                async with asyncio.timeout(180):
                    while True:
                        status=await self.client.request('plugin_status',scope,lease)
                        if status['status']=='succeeded':break
                        if status['status'] in {'failed','stopped','released','uncertain'}:raise PackageError('The native installer needs a fresh attempt.')
                        await asyncio.sleep(.1)
                result=await self.client.read(scope,lease,root/'package',document)
                # Return only original package bytes + ordinary native install
                # metadata; sandbox source/home/CLI output remain private.
                yield root/'package',{'runtime':'codex-cli','pluginId':entry['id']+'@rocoto-pinned-api','version':result['version'],'installed':True,'enabled':True}
            finally:
                if lease is not None:
                    async def cleanup():
                        status=await self.client.request('plugin_stop',scope,lease)
                        if status.get('cleanup_confirmed') is not True:raise PackageError('Native installation cleanup is finishing.')
                        await self.client.request('plugin_release',scope,lease)
                    await drain(asyncio.create_task(cleanup()))
