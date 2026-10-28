"""Parent-only publication of explicit, broker-sealed native output files.

Scope, actor identity and session are host objects; the model supplies only path
and optional title. This module never opens a runtime path on the host.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import hashlib
import json
import re
import tempfile
import unicodedata

from chat_files import MAX_NATIVE_FILE_BYTES
from native_executor import NativeScope, NativeLease, ExecutorError
import sqlite3


class PublicationError(ValueError):
    """Stable publication error, without broker sockets or runtime paths."""


def output_path(value):
    try:
        if not isinstance(value,str) or not 1<=len(value.encode('utf-8'))<=1024:
            raise ValueError()
    except (ValueError,UnicodeError):
        raise PublicationError('invalid_output_path') from None
    parts=value.split('/')
    if (len(parts)>16 or any(not part or part.startswith('.') or len(part)>120
            or any(char in '\\<>:"|?*' or unicodedata.category(char).startswith('C') for char in part)
            or part!=part.strip() or part.endswith('.') for part in parts)):
        raise PublicationError('invalid_output_path')
    return value


PUBLISH_FILE_TOOL={'type':'function','name':'publish_file',
    'description':'Share a finished file from /workspace/outputs in this chat. First create the file with native tools, then provide its relative path, such as report.docx or charts/chart.png. Preserves original bytes and returns the downloadable artifact. Up to 16 MiB per file.',
    'inputSchema':{'type':'object','properties':{'path':{'type':'string','minLength':1,'maxLength':1024},
        'title':{'type':'string','minLength':1,'maxLength':160}},'required':['path'],'additionalProperties':False}}


class NativeFilePublisher:
    def __init__(self,files,*,company_id,owner_principal_id,root_chat_id,dataset,timeout_seconds=90):
        files._scope(owner_principal_id,root_chat_id,dataset)
        if not root_chat_id or not dataset:raise PublicationError('invalid_publication_scope')
        self.files,self.scope=files,NativeScope(company_id,owner_principal_id,root_chat_id)
        self.dataset,self.timeout_seconds=dataset,timeout_seconds
        self._lock=asyncio.Lock()

    def _active(self,session,actor_chat_id):
        storage_scope=getattr(session,'storage_scope',session.scope)
        if (actor_chat_id!=self.scope.root_chat_id or session.scope!=self.scope
            or not isinstance(storage_scope,NativeScope) or storage_scope.company_id!=self.scope.company_id
            or storage_scope.owner_id!=self.scope.owner_id
            or not isinstance(session.lease,NativeLease) or session.lease.scope_key!=storage_scope.key):
            raise PublicationError('publication_scope_mismatch')
        if session.stopped or session._closing:raise PublicationError('publication_stopped')
        if storage_scope!=self.scope and not re.fullmatch(re.escape(self.scope.root_chat_id)+r':scope:[a-f0-9]{64}',storage_scope.root_chat_id):
            raise PublicationError('publication_scope_mismatch')
        current=getattr(session,'authority_current',None)
        if current is not None and not current():raise PublicationError('publication_scope_changed')
        task=asyncio.current_task()
        if task is not None and task.cancelling():raise asyncio.CancelledError()

    async def publish(self,session,*,actor_chat_id,path,title=None):
        try:
            return await self._publish(session,actor_chat_id=actor_chat_id,path=path,title=title)
        except TimeoutError:
            raise PublicationError('publication_timeout') from None
        except (OSError,ExecutorError,sqlite3.Error):
            raise PublicationError('publication_unavailable') from None

    async def _publish(self,session,*,actor_chat_id,path,title=None):
        """Return metadata for the standard artifact event. Parent dispatch only.

        Use one publisher per active IsolatedSession. Its lock prevents concurrent
        snapshots deleting each other's delivery buffers. Stop revokes the session
        before broker termination; the final no-await transaction fences commit.
        """
        path=output_path(path)
        name=path.rsplit('/',1)[-1]
        self.files.native_name(name,title)  # Validate before touching the broker.
        self._active(session,actor_chat_id)
        async with self._lock, asyncio.timeout(self.timeout_seconds):
            self._active(session,actor_chat_id)
            snapshot=await session.client.snapshot(getattr(session,'storage_scope',session.scope),session.lease,paths=[path])
            rows=snapshot.get('files') if isinstance(snapshot,dict) else None
            identity=snapshot.get('snapshot_id') if isinstance(snapshot,dict) else None
            if (not isinstance(identity,str) or not re.fullmatch(r'[a-f0-9]{48}',identity)
                or type(snapshot.get('generation')) is not int or snapshot['generation']!=session.lease.generation or not isinstance(rows,list) or len(rows)!=1):
                raise PublicationError('invalid_output_snapshot')
            row=rows[0]
            if (not isinstance(row,dict) or row.get('path')!=path or type(row.get('index')) is not int or row['index']!=0
                or type(row.get('size')) is not int or not 0<=row['size']<=MAX_NATIVE_FILE_BYTES
                or type(snapshot.get('total_bytes')) is not int or snapshot['total_bytes']!=row['size']
                or not isinstance(row.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',row['sha256'])):
                raise PublicationError('invalid_output_manifest')
            self._active(session,actor_chat_id)
            digest=hashlib.sha256();size=0
            with tempfile.SpooledTemporaryFile(max_size=1024*1024,mode='w+b') as buffer:
                async with aclosing(session.client.read_output(getattr(session,'storage_scope',session.scope),session.lease,identity,0)) as chunks:
                    async for chunk in chunks:
                        self._active(session,actor_chat_id)
                        if not isinstance(chunk,bytes):raise PublicationError('invalid_output_bytes')
                        size+=len(chunk)
                        if size>row['size'] or size>MAX_NATIVE_FILE_BYTES:raise PublicationError('output_size_mismatch')
                        digest.update(chunk);buffer.write(chunk)
                if size!=row['size'] or digest.hexdigest()!=row['sha256']:
                    raise PublicationError('output_integrity_mismatch')
                self._active(session,actor_chat_id)
                # Content identity survives fresh snapshots and runtime restarts.
                key=hashlib.sha256(json.dumps([self.scope.company_id,self.scope.owner_id,
                    self.scope.root_chat_id,self.dataset,path,row['sha256']],separators=(',',':')).encode()).hexdigest()
                provenance={'kind':'native_output','generation':session.lease.generation,
                    'snapshot_id':identity,'path':path,'sha256':row['sha256']}
                buffer.seek(0)
                # Deliberately synchronous: no cancellation/Stop task can interleave
                # after the final active check and before the atomic local commit.
                return self.files.import_native(buffer,name=name,title=title,size_bytes=size,
                    sha256=row['sha256'],publication_key=key,provenance=provenance,
                    owner_principal_id=self.scope.owner_id,chat_id=self.scope.root_chat_id,dataset=self.dataset)
