"""Bounded private import reads shared by Gallery and native workspace context.

Opening this projection never initializes SourceStore, creates its directories,
claims an allowance or constructs a worker. Host-selected state and saved actor
scope are the only authority; model arguments cannot supply either.
"""
from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import stat

from source_intake import IntakeActor, IntakeError, IntakeLimits, SourceStore

DEFAULT_LIMIT=100
MAX_LIMIT=200
MAX_OFFSET=IntakeLimits().max_assets
CHUNK_BYTES=min(8*1024**2,IntakeLimits().max_chunk_bytes)


def public_import(view):
    upload,source,asset=view['upload'],view['source'],view['asset']
    status=upload['status'];error=upload['error']
    jobs=asset.get('jobs',[]) if asset else []
    if asset:
        status=asset['status']
        failed=[job for job in jobs if job['status'] in {'failed','needs_reconciliation'}]
        if failed:error=failed[-1]['error']
        if any(job['status']=='needs_reconciliation' and job['error']=='cancel_requested' for job in jobs):
            status='cancelling'
    elif status=='sealed':status='received'
    active=next((job for state in ['running','queued'] for job in jobs if job['status']==state),None)
    stage=active['stage'] if active and status not in {'ready','cancelled','cancelling','failed','needs_attention'} else None
    return {'stage':stage,'stage_status':active['status'] if stage else None,
        'id':upload['id'],'source_id':upload['source_id'],'asset_id':upload['asset_id'],
        'collection':source['collection'],'filename':upload['filename'],
        'expected_bytes':upload['expected_bytes'],'committed_bytes':upload['committed_bytes'],
        'status':status,'transfer_status':upload['status'],'sha256':upload['sha256'],
        'created_at':upload['created'],'expires_at':upload['expires'],
        'cleanup_pending':any(job['status']=='needs_reconciliation' for job in jobs),
        'error':(error if isinstance(error,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',error)
                 else 'import_needs_attention') if error else None}


class IntakeProjection:
    def __init__(self,state,*,company_id,collections):
        from source_service import SourceHost
        self.state=Path(state).absolute()
        self.host=SourceHost(self.state,company_id=company_id,read_only=True)
        self.known_collections=collections

    def collections(self,actor):
        allowed={value for value in self.known_collections() if isinstance(value,str)}
        allowed.update(getattr(self,'monitoring_collections',lambda owner:[])(actor.principal_id))
        allowed.update(self.host.collections(actor))
        try:config=self.host.configuration()
        except (OSError,ValueError):config=None
        if not allowed and config and config.principal_id==actor.principal_id:
            allowed.add(config.collection)
        return allowed

    def _rows(self,actor,collection,allowed,limit,offset):
        root=self.state/'source-intake';database=root/'intake.sqlite3'
        if root.absolute()!=root.resolve():raise IntakeError('invalid_storage')
        if not root.exists():return [],None
        info=root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise IntakeError('invalid_storage')
        if not database.exists() and not database.is_symlink():return [],None
        descriptor=SourceStore._open(database,os.O_RDONLY)
        os.close(descriptor)
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=.1,isolation_level=None)) as db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            marks=','.join('?' for _ in allowed)
            rows=db.execute('SELECT u.* FROM uploads u JOIN sources s ON s.id=u.source_id '
                f'WHERE u.owner=? AND s.owner=? AND s.collection IN ({marks}) '
                'AND (? IS NULL OR s.collection=?) ORDER BY u.created DESC,u.id DESC LIMIT ? OFFSET ?',
                (actor.principal_id,actor.principal_id,*sorted(allowed),collection,collection,limit+1,offset)).fetchall()
            # The existing source-store view deliberately discards worker
            # tokens/results, object locators and provider receipt contents.
            values=[public_import(SourceStore._import_view(db,row)) for row in rows[:limit]]
            return values,offset+limit if len(rows)>limit else None

    def snapshot(self,principal,collection=None,*,read_only=False,limit=DEFAULT_LIMIT,offset=0):
        if type(limit) is not int or not 1<=limit<=MAX_LIMIT or type(offset) is not int or not 0<=offset<=MAX_OFFSET:
            raise IntakeError('invalid_query')
        empty={'imports':[],'offset':offset,'next_offset':None,'intake':{
            'available':False,'worker_ready':False,'can_upload':False,'can_manage_imports':False,
            'admission':'setup_required','notice':'','collections':[],
            'default_collection':None,'chunk_bytes_max':CHUNK_BYTES,'status':'ok'}}
        if not principal:return empty
        actor=IntakeActor(principal,read_only=read_only)
        try:
            allowed=self.collections(actor)
            # The host already permits pre-footage business setup in this scope.
            if collection=='company':allowed.add('company')
            if collection is not None and collection not in allowed:raise IntakeError('collection_not_found')
            gate=self.host.readiness(actor,collection=collection)
            try:config=self.host.configuration()
            except (OSError,ValueError):config=None
            default=config.collection if config and config.principal_id==principal and config.collection in allowed else None
            values,next_offset=self._rows(actor,collection,allowed,limit,offset)
            return {'imports':values,'offset':offset,'next_offset':next_offset,'intake':{
                **gate,'collections':sorted(allowed),'default_collection':default,
                'chunk_bytes_max':CHUNK_BYTES,'status':'unavailable' if gate['admission']=='status_unavailable' else 'ok'}}
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError) as error:
            if isinstance(error,IntakeError) and str(error)=='collection_not_found':raise
            empty['intake'].update(status='unavailable',admission='status_unavailable',
                notice='Import status is temporarily unavailable.')
            return empty
