"""Immutable public retrieval views over Ready, principal-owned source imports.

The raw index remains a host publisher. These views keep its concrete Qdrant
collection/fingerprint, restrict IDs and aligned vectors, and never publish or
refresh. Create a fresh view at each request/turn boundary. Media resolution still
checks sealed bytes independently when the selected record is opened.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import copy
import json
from pathlib import Path
import re
import sqlite3
import threading

import numpy as np

from imported_media import PROFILE, RECIPE, IMAGE, imported_record
from source_intake import SourceStore

ASSET=re.compile(r'asset-[a-f0-9]{32}\Z')
SHA=re.compile(r'[a-f0-9]{64}\Z')


class VisibilityError(ValueError):
    """Stable host configuration errors without principal or path disclosure."""


def _imported(row):
    return imported_record(row) or bool(ASSET.fullmatch(str(row.get('video_id','')))) or 'asset_id' in row


def _receipt(value):
    if not isinstance(value,str) or len(value.encode())>64*1024:return None
    try:result=json.loads(value)
    except (ValueError,RecursionError):return None
    return result if isinstance(result,dict) else None


def _authority(store,principal,asset_ids):
    """One source-store read snapshot; no index lock or media decoder is held."""
    result={}
    with store._read() as db:
        db.execute('BEGIN')
        for start in range(0,len(asset_ids),128):
            batch=asset_ids[start:start+128]
            marks=','.join('?' for _ in batch)
            rows=db.execute(f'''SELECT a.*, s.owner AS source_owner, s.collection,
                s.name AS source_name, o.source_id AS object_source,
                p.result AS prepare_result, u.result AS publish_result
                FROM assets a JOIN sources s ON a.source_id=s.id
                JOIN source_objects o ON a.object_id=o.id
                JOIN jobs p ON p.asset_id=a.id AND p.stage='prepare' AND p.status='succeeded'
                JOIN jobs u ON u.asset_id=a.id AND u.stage='publish' AND u.status='succeeded'
                WHERE a.status='ready' AND a.owner=? AND a.id IN ({marks})''',(principal,*batch)).fetchall()
            for row in rows:
                value=dict(row)
                if value['source_owner']!=principal or value['object_source']!=value['source_id']:continue
                prepared=_receipt(value.pop('prepare_result'));published=_receipt(value.pop('publish_result'))
                if (not prepared or not published or prepared.get('profile')!=PROFILE
                        or prepared.get('recipe')!=RECIPE or prepared.get('image_digest')!=IMAGE
                        or prepared.get('original_sha256')!=value['sha256']
                        or not SHA.fullmatch(str(prepared.get('manifest_sha256','')))
                        or not isinstance(published.get('generation_id'),str) or not published['generation_id']
                        or not isinstance(published.get('qdrant_collection'),str) or not published['qdrant_collection']
                        or not SHA.fullmatch(str(published.get('qdrant_fingerprint','')))):
                    continue
                value['prepared']=prepared;result[value['id']]=value
    return result


def _allowed(row,authority,store):
    asset=authority.get(row.get('video_id'))
    if asset is None or row.get('storage_kind')!='imported_source':return False
    prepared=asset['prepared']
    expected={'asset_id':asset['id'],'source_id':asset['source_id'],'source_object_id':asset['object_id'],
        'original_sha256':asset['sha256'],'dataset':asset['collection'],'camera':asset['source_name'],
        'path':str(store.blobs/asset['sha256']),'preparation_profile':PROFILE,'preparation_recipe':RECIPE,
        'preparation_image_digest':IMAGE,'prepared_manifest_sha256':prepared['manifest_sha256']}
    if any(row.get(key)!=value for key,value in expected.items()):return False
    return bool(re.fullmatch(re.escape(asset['id'])+r'-clip-[0-9]{6}',str(row.get('id',''))))


def _report(rows,original):
    # Rebuild public source counts rather than carrying private videos, per-job
    # timing or prior unfiltered summaries through the raw generation report.
    report={key:original[key] for key in ('model','model_revision','embedding_dimensions',
        'text_query_device','query_provider_spend_usd') if key in original}
    videos={}
    for row in rows:
        videos.setdefault(row['video_id'],{'video_id':row['video_id'],'dataset':row['dataset'],
            'camera':row.get('camera',''),'duration_seconds':row.get('duration',row['end'])})
    report.update(clip_count=len(rows),video_count=len(videos),videos=list(videos.values()),
        footage_seconds=round(sum(row['end']-row['start'] for row in rows),6),
        archive_seconds=round(sum(value['duration_seconds'] for value in videos.values()),6))
    return report


def _read_only(*args,**kwargs):
    raise VisibilityError('ready_view_is_read_only')


def ready_index_view(index,store:SourceStore,*,principal_id):
    """Capture one principal's coherent read-only view without changing the index.

    Host authentication supplies principal_id (including an observer's selected
    principal). Both source store and index must belong to that selected company.
    Legacy company/demo rows remain available; imported rows require actual Ready
    and successful source publication authority. A store read failure hides all
    imported rows while retaining the existing legacy view.
    """
    if ((principal_id is not None and (not isinstance(principal_id,str) or not principal_id))
            or (store is not None and not isinstance(store,SourceStore))):
        raise VisibilityError('host_source_scope_required')
    # Release the in-memory reader lock before opening SQLite. Publication owns
    # SourceStore -> Cosmos -> Qdrant, so this cannot introduce an inverse lock.
    with getattr(index,'_lock',nullcontext()):
        view=copy.copy(index)
        rows=copy.deepcopy(index.clips)
        matrix=np.asarray(index.embeddings) if hasattr(index,'embeddings') else None
        if matrix is not None:
            if matrix.ndim!=2 or matrix.shape[0]!=len(rows):raise VisibilityError('invalid_source_snapshot')
            matrix=matrix.copy()
        report=copy.deepcopy(getattr(index,'report',{}))
        encoder=getattr(index,'_encode_text',None)
    asset_ids=sorted({row.get('video_id') for row in rows if _imported(row)
        and isinstance(row.get('video_id'),str) and ASSET.fullmatch(row['video_id'])})
    unavailable=False
    try:authority=_authority(store,principal_id,asset_ids) if asset_ids and principal_id is not None and store is not None else {}
    except (sqlite3.Error,OSError,ValueError):authority={};unavailable=True
    positions=[position for position,row in enumerate(rows) if not _imported(row) or _allowed(row,authority,store)]
    view.clips=[rows[position] for position in positions]
    if matrix is not None:
        view.embeddings=matrix[positions];view.embeddings.flags.writeable=False
    view.report=_report(view.clips,report)
    # These are in-memory selection copies, not a new persisted Qdrant generation.
    # Its inherited search sends has_id for exactly these selected clip IDs.
    if encoder is not None:view._encode_text=encoder
    view.refresh=lambda:False
    view._reload=_read_only
    view._publish_loaded=_read_only
    view.build=_read_only
    view.source_visibility='ready_principal_snapshot'
    view.source_visibility_unavailable=unavailable
    return view


class ReadyIndex:
    """Shared services resolve reads through one host-bound request/turn snapshot."""
    def __init__(self,index,company_state):
        self._raw=index
        self._company_state=Path(company_state).absolute()
        self._view=ContextVar('rocoto_ready_source_view_'+str(id(self)),default=None)
        self._store_cache=None
        self._store_lock=threading.Lock()

    def _store(self):
        # Ordinary demo browsing does not create a new intake database.
        if not (self._company_state/'source-intake/intake.sqlite3').exists():return None
        with self._store_lock:
            if self._store_cache is None:self._store_cache=SourceStore(self._company_state)
            return self._store_cache

    def snapshot(self,principal_id,*,source=None,refresh=False,apply_scope=True):
        if refresh:self.refresh()
        source=self._raw if source is None else source
        if isinstance(source,ReadyIndex):source=source.current()
        if (hasattr(source,'state') and hasattr(self._raw,'state')
                and Path(source.state).absolute()!=Path(self._raw.state).absolute()):
            raise VisibilityError('source_snapshot_company_mismatch')
        result=ready_index_view(source,self._store() if principal_id is not None else None,principal_id=principal_id)
        result.source_visibility_company=str(self._company_state)
        result.source_visibility_principal=principal_id
        scope_filter=self.__dict__.get('scope_filter')
        if apply_scope and scope_filter is not None:result=scope_filter(result,principal_id)
        return result

    @contextmanager
    def bind(self,view,*,check_exit=True):
        if getattr(view,'source_visibility_company',None)!=str(self._company_state):
            raise VisibilityError('source_snapshot_company_mismatch')
        guard=getattr(view,'business_scope_current',None)
        if guard is not None and not guard():raise VisibilityError('business_scope_changed')
        token=self._view.set(view)
        try:
            yield view
            if check_exit and guard is not None and not guard():raise VisibilityError('business_scope_changed')
        finally:self._view.reset(token)

    def current(self):
        bound=self._view.get()
        return bound if bound is not None else self.snapshot(None)

    def refresh(self):
        refresh=getattr(self._raw,'refresh',None)
        return refresh() if refresh is not None else False

    def __copy__(self):
        return copy.copy(self.current())

    def __getattr__(self,name):
        # Called only after ordinary host facade attributes have been resolved.
        return getattr(self.current(),name)


@contextmanager
def chat_source_view(index,principal_id):
    """Bind saved chat ownership and preserve any narrower native/scheduler view."""
    if not isinstance(index,ReadyIndex):
        yield
        return
    from execution_policy import RETRIEVAL_VIEW
    source=RETRIEVAL_VIEW.get()
    if source is None:source=index._view.get()
    if (source is not None and getattr(source,'source_visibility_company',None)==str(index._company_state)
            and getattr(source,'source_visibility_principal',object())==principal_id):
        view=source
    else:view=index.snapshot(principal_id,source=source)
    token=RETRIEVAL_VIEW.set(view)
    try:
        with index.bind(view):yield view
    finally:RETRIEVAL_VIEW.reset(token)
