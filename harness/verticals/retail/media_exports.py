"""Principal-private, bounded editing packages from already authorized indexed clips.

Cuts retain available source resolution and frame timing. Suggested speed/framing
are edit-plan metadata, not transformations or claims of an editor import format.
The host supplies the authenticated principal, selected collection and scoped index.
"""
from __future__ import annotations

from imported_media import ImportedMedia, VerifiedFile, imported_record

import asyncio
from contextlib import suppress
import copy
import csv
from datetime import datetime, timezone
import fcntl
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid
import zipfile

from media import EvidenceMedia


MAX_ITEMS = 20
MAX_SOURCE_SECONDS = 600
MAX_OUTPUT_BYTES = 1024**3
MAX_RETAINED_BYTES = 2 * 1024**3
MAX_RETAINED_EXPORTS = 20
MAX_HASH_SOURCE_BYTES = 8 * 1024**3
PREPARE_TIMEOUT_SECONDS = 300
ID_PATTERN = re.compile(r"export-[0-9a-f]{32}")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _rational(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def _fraction(value, *, minimum=Fraction(1, 1000), maximum=Fraction(10000)):
    if isinstance(value, bool):
        raise ValueError("Choose a finite positive speed or frame rate.")
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError, TypeError):
        raise ValueError("Choose a finite positive speed or frame rate.") from None
    if not minimum <= result <= maximum or result.denominator > 1_000_000:
        raise ValueError("Choose a supported speed or frame rate.")
    return result


def _text(value, limit, *, empty=True):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(c) < 32 and c not in '\n\t' for c in value)):
        raise ValueError("Use bounded plain text for the export title, summary and editing notes.")
    return value.strip()


def _safe_file(path, root):
    """Only regular, nonsymlink files inside this host-selected company state."""
    path = Path(os.path.abspath(path))
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise ValueError("Choose a preserved source in this company.") from None
    candidate = root
    try:
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("Choose a preserved regular source file.")
        info = candidate.stat()
    except OSError:
        raise ValueError("The preserved source is unavailable.") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Choose a preserved regular source file.")
    return candidate


def _fingerprint(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _public_reference(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            return None
        # Source citations never export query credentials or private transport URLs.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    except ValueError:
        return None


def _public_note(value, root, source, limit=4000):
    if not isinstance(value, str):
        return None
    value = value[:limit]
    for private in (str(root), str(source)):
        value = value.replace(private, '[private source]')
    if re.search(r'(?:file|rtsp|s3)://|/(?:home|root|var|tmp|etc)/|(?:password|token|secret)\s*[=:]', value, re.I):
        return None
    return value


class _Stopped(Exception):
    pass


def _check(stop):
    if stop.is_set():
        raise _Stopped()


def _transfer(stop, source, destination=None, limit=None):
    """Hash/copy in bounded chunks on a worker thread, with cooperative cancellation."""
    digest, size = hashlib.sha256(), 0
    with (source.open() if isinstance(source, VerifiedFile) else source.open('rb')) as reader:
        before = os.fstat(reader.fileno())
        writer = destination.open('xb') if destination else None
        try:
            if writer:
                os.fchmod(writer.fileno(), 0o600)
            while chunk := reader.read(1024 * 1024):
                _check(stop)
                size += len(chunk)
                if limit is not None and size > limit:
                    raise ValueError("This package exceeds its storage allowance.")
                digest.update(chunk)
                if writer:
                    writer.write(chunk)
            if isinstance(source, VerifiedFile):
                after = os.fstat(reader.fileno())
                identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if size != source.size or digest.hexdigest() != source.sha256 or identity(before) != identity(after):
                    raise ValueError('The verified source changed during export preparation.')
            if writer:
                writer.flush()
                os.fsync(writer.fileno())
        finally:
            if writer:
                writer.close()
    return {'sha256': digest.hexdigest(), 'bytes': size}


async def _joined(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        with suppress(Exception):
            task.result()


async def _thread(operation, *args):
    stop = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(operation, stop, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        # Join before cleanup, so a cancelled thread cannot recreate removed files.
        await _joined(task)
        raise


async def _blocking(operation, *args):
    """Join short state mutations before propagating cancellation to cleanup."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _joined(task)
        raise


async def _probe(path):
    process = await asyncio.create_subprocess_exec('ffprobe','-v','error','-show_streams',
        '-show_format','-of','json',str(path),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        if process.returncode or len(output) > 1024 * 1024:
            raise ValueError("The prepared clip's media details could not be verified.")
        data = json.loads(output)
        video = next(s for s in data['streams'] if s.get('codec_type') == 'video')
        duration = float(video.get('duration', data['format']['duration']))
        if not math.isfinite(duration) or not 0 < duration <= 121:
            raise ValueError("Choose a bounded media interval.")
        return {'mime_type':'video/mp4','video_codec':video.get('codec_name'),
            'width':video['width'],'height':video['height'],
            'average_frame_rate':video.get('avg_frame_rate'),
            'nominal_frame_rate':video.get('r_frame_rate'),
            'duration_seconds':duration,'frame_count':int(video['nb_frames']) if video.get('nb_frames','').isdigit() else None,
            'start_time_seconds':float(video.get('start_time',0)),
            'audio_codecs':[s.get('codec_name') for s in data['streams'] if s.get('codec_type')=='audio']}
    except (KeyError, StopIteration, TypeError, json.JSONDecodeError):
        raise ValueError("The prepared clip's media details could not be verified.") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class ExportStore:
    def __init__(self, state, index, media=None):
        self.state, self.index = Path(state).resolve(), index
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = self.state/'payloads'
        if self.directory.is_symlink():
            raise ValueError("The package storage location needs operator review.")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.state/'payloads.sqlite'
        if self.path.is_symlink():
            raise ValueError("The package catalogue needs operator review.")
        self.media = media or EvidenceMedia(self.state)
        self.imported_media = ImportedMedia(self.state)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS exports (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, dataset TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS export_events (
                id INTEGER PRIMARY KEY, export_id TEXT NOT NULL, at TEXT NOT NULL,
                status TEXT NOT NULL, body TEXT NOT NULL);''')
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    @staticmethod
    def _owner(owner):
        if not isinstance(owner,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',owner):
            raise ValueError("Choose an authenticated package owner.")
        return owner

    @staticmethod
    def _key(key):
        if not isinstance(key,str) or not ID_PATTERN.fullmatch(key):
            raise ValueError("Choose a saved package.")
        return key

    def _lock(self):
        fd = os.open(self.directory/'.prepare.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ValueError("A package is already being prepared for this company.") from None
        return fd

    def _save(self, record):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE exports SET body=? WHERE id=?',(json.dumps(record),record['id']))
            db.execute('INSERT INTO export_events(export_id,at,status,body) VALUES(?,?,?,?)',
                (record['id'],_now(),record['status'],json.dumps({'error':record.get('error')})))

    def _admit(self, document, dataset, owner, duration):
        fd = self._lock()
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                retained = [json.loads(row[0]) for row in db.execute('SELECT body FROM exports')]
                retained = [r for r in retained if r['status'] in ('queued','preparing','ready')]
                used = sum(r.get('bytes',0) for r in retained)
                allowance = min(MAX_OUTPUT_BYTES, MAX_RETAINED_BYTES-used)
                if len(retained)>=MAX_RETAINED_EXPORTS or allowance<1024*1024:
                    raise ValueError("The company's package storage allowance is full. Ask the operator to archive earlier exports.")
                if shutil.disk_usage(self.directory).free < 2*allowance + 128*1024**2:
                    raise ValueError("The package needs more available storage. Ask the operator to review it.")
                record = {'schema_version':1,'id':'export-'+uuid.uuid4().hex,'owner_principal_id':owner,
                    'dataset':dataset,'title':document['title'],'summary':document['summary'],
                    'status':'queued','created_at':_now(),'updated_at':_now(),'item_count':len(document['items']),
                    'source_duration_seconds':float(duration),'selection':document,'error':None}
                db.execute('INSERT INTO exports VALUES(?,?,?,?)',(record['id'],owner,dataset,json.dumps(record)))
                db.execute('INSERT INTO export_events(export_id,at,status,body) VALUES(?,?,?,?)',
                    (record['id'],_now(),'queued','{}'))
            return record, fd, allowance
        except BaseException:
            os.close(fd)
            raise

    def _imported_source(self, record, owner):
        resolved = self.imported_media.resolve(record)
        asset, source, _ = self.imported_media._authority(record['video_id'])
        if asset['owner'] != owner or source['owner'] != owner:
            raise ValueError('Choose a source from this person’s workspace.')
        return resolved

    def _verified_receipt(self, identity, source_record, owner):
        from source_derivatives import SourceDerivatives
        from workspace_identity import workspace_scope
        reader = SourceDerivatives(self.state, company_id=workspace_scope(self.state))
        return reader.read_receipt(identity, source_record, owner_principal_id=owner, purpose='source_cut_v1')

    def _selection(self, document, dataset, owner):
        self._owner(owner)
        if (not isinstance(document,dict) or set(document)-{'title','summary','items','output_fps'}
                or not isinstance(dataset,str) or not dataset):
            raise ValueError("Choose a titled package and one business collection.")
        title = _text(document.get('title'),160,empty=False)
        summary = _text(document.get('summary',''),20000)
        items = document.get('items')
        if not isinstance(items,list) or not 1<=len(items)<=MAX_ITEMS:
            raise ValueError("Select one to twenty ordered evidence clips.")
        fps = document.get('output_fps')
        if fps is not None:
            fps = _fraction(fps,minimum=Fraction(1),maximum=Fraction(240))
        records, normalized, duration, unique = [], [], Fraction(0), {}
        for item in items:
            if not isinstance(item,dict) or set(item)-{'clip_id','speed_multiplier','framing','reason'}:
                raise ValueError("Choose clip identifiers and editing notes from the selected collection.")
            clip_id = item.get('clip_id')
            if not isinstance(clip_id,str) or len(clip_id)>240:
                raise ValueError("Choose an indexed evidence clip.")
            record = self.index.get_clip(clip_id)
            if not record or record.get('dataset')!=dataset:
                raise ValueError("Choose clips from this business collection.")
            record = copy.deepcopy(record)
            if imported_record(record):
                source = self._imported_source(record, owner).original
                source_key, source_size = 'imported:' + source.sha256, source.size
            else:
                source = _safe_file(record['path'],self.state)
                source_key, source_size = str(source), source.stat().st_size
            start,end = Fraction(str(record['start'])),Fraction(str(record['end']))
            if start<0 or not 0<end-start<=120:
                raise ValueError("Choose bounded evidence clips of at most two minutes each.")
            duration += end-start
            unique[source_key] = source_size
            if duration>MAX_SOURCE_SECONDS or sum(unique.values())>MAX_HASH_SOURCE_BYTES:
                raise ValueError("Select up to ten source minutes from at most eight GiB of preserved files.")
            speed = _fraction(item.get('speed_multiplier',1))
            normalized.append({'clip_id':clip_id,'speed_multiplier':str(speed),
                'framing':_text(item.get('framing','Preserve source'),500),
                'reason':_text(item.get('reason',''),1000)})
            if not isinstance(source, VerifiedFile):
                record['path']=str(source)
            records.append(record)
        texts = [title, summary, *[v for item in normalized for k,v in item.items() if k in ('framing','reason')]]
        if any(_public_note(value,self.state,Path('/unavailable'),20000)!=value for value in texts):
            raise ValueError("Use public source identifiers and editing notes in the downloadable report.")
        return {'title':title,'summary':summary,'items':normalized,
                'output_fps':str(fps) if fps else None},records,duration

    @staticmethod
    def _public(record):
        result = {k:v for k,v in record.items() if k not in ('owner_principal_id','selection')}
        if record['status']=='ready':
            result.update(download_url=f"/api/exports/{record['id']}/download",
                          source_url=f"/api/exports/{record['id']}")
        return result

    def get(self, payload_id, *, owner_principal_id, dataset=None):
        self._key(payload_id);self._owner(owner_principal_id)
        with self.connect() as db:
            row=db.execute('SELECT body FROM exports WHERE id=? AND owner=?',(payload_id,owner_principal_id)).fetchone()
        if row is None:
            raise ValueError("Choose a package from this person's workspace.")
        record=json.loads(row[0])
        if dataset is not None and record['dataset']!=dataset:
            raise ValueError("Choose a package from this business collection.")
        return self._public(record)

    def list(self, *, owner_principal_id, dataset=None):
        self._owner(owner_principal_id)
        with self.connect() as db:
            rows=db.execute('SELECT body FROM exports WHERE owner=? ORDER BY rowid DESC',(owner_principal_id,)).fetchall()
        return [self._public(r) for r in (json.loads(row[0]) for row in rows) if dataset is None or r['dataset']==dataset]

    def download_path(self, payload_id, *, owner_principal_id, dataset=None):
        record=self.get(payload_id,owner_principal_id=owner_principal_id,dataset=dataset)
        if record['status']!='ready':
            raise ValueError("This package is still being prepared or needs review.")
        path = _safe_file(self.directory/payload_id/'package.zip',self.state)
        if path.stat().st_size != record['bytes']:
            raise ValueError("This package's stored file needs operator review.")
        return path

    def recover(self):
        """Call once at service startup. A live export's lock blocks recovery."""
        fd=self._lock()
        try:
            with self.connect() as db:
                rows=db.execute('SELECT body FROM exports').fetchall()
            for row in rows:
                record=json.loads(row[0])
                if record['status'] in ('queued','preparing'):
                    self._cleanup(record['id'])
                    record.update(status='interrupted',updated_at=_now(),error='The service restarted during preparation. Create a new package to continue.')
                    self._save(record)
        finally:
            os.close(fd)

    def _cleanup(self,key):
        self._key(key)
        for path in (self.directory/key,self.directory/('.'+key+'.work')):
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

    async def prepare(self, document, *, dataset, owner_principal_id):
        normalized,records,duration = await asyncio.to_thread(self._selection,document,dataset,owner_principal_id)
        # Admission is short and shielded so cancellation cannot strand its held file lock.
        admission=asyncio.create_task(asyncio.to_thread(self._admit,normalized,dataset,owner_principal_id,duration))
        try:
            record,fd,allowance=await asyncio.shield(admission)
        except asyncio.CancelledError:
            record,fd,_=await admission
            record.update(status='interrupted',updated_at=_now(),error='Package preparation was stopped.')
            await _blocking(self._save,record)
            os.close(fd)
            raise
        work=self.directory/('.'+record['id']+'.work')
        try:
            async with asyncio.timeout(PREPARE_TIMEOUT_SECONDS):
                record.update(status='preparing',updated_at=_now())
                await _blocking(self._save,record)
                work.mkdir(mode=0o700)
                files=work/'files';files.mkdir(mode=0o700)
                (files/'clips').mkdir(mode=0o700)
                imported_checks = []
                manifest=await self._build(record,records,files,allowance,imported_checks)
                await _thread(self._documents_and_zip,files,work/'package.zip',manifest,allowance)
                archive=await _thread(_transfer,work/'package.zip',None,allowance)
                for identity, source_record in imported_checks:
                    await asyncio.to_thread(self._verified_receipt,identity,source_record,record['owner_principal_id'])
                await _blocking(shutil.rmtree,files)
                work.rename(self.directory/record['id'])
                record.update(status='ready',updated_at=_now(),manifest=manifest,**archive)
                await _blocking(self._save,record)
        except asyncio.CancelledError:
            await _blocking(self._cleanup,record['id'])
            record.update(status='interrupted',updated_at=_now(),error='Package preparation was stopped.')
            await _blocking(self._save,record)
            raise
        except Exception:
            await _blocking(self._cleanup,record['id'])
            record.update(status='failed',updated_at=_now(),error='The package could not be prepared within its source, time or storage limits. Review the selection and try again.')
            await _blocking(self._save,record)
        finally:
            os.close(fd)
        return self._public(record)

    async def _build(self,record,records,files,allowance,imported_checks=None):
        selection=record['selection']; source_cache={};source_fingerprints={};items=[];used=0;timeline=Fraction(0)
        for order,(choice,source_record) in enumerate(zip(selection['items'],records),1):
            imported = imported_record(source_record)
            receipt = None
            if imported:
                # The host-bound helper rechecks Ready/publication, company and
                # owner, then returns sealed descriptors from the fixed sandbox.
                receipt = await self.media.source_cut(source_record,
                    owner_principal_id=record['owner_principal_id'])
                resolved = await asyncio.to_thread(self._imported_source,
                    source_record, record['owner_principal_id'])
                source = resolved.original
                if (len(receipt.files) != 1 or len(receipt.manifest['files']) != 1
                        or not isinstance(receipt.files[0], VerifiedFile)
                        or receipt.manifest.get('purpose') != 'source_cut_v1'
                        or receipt.manifest.get('clip_id') != source_record['id']
                        or receipt.manifest.get('original', {}).get('sha256') != source.sha256):
                    raise ValueError('Choose a verified source-resolution cut.')
                if imported_checks is not None:
                    imported_checks.append((receipt.receipt_id, source_record))
                prepared = receipt.files[0]
                details = receipt.manifest['files'][0]
                if prepared.sha256 != details['sha256'] or prepared.size != details['size_bytes']:
                    raise ValueError('The source-cut receipt needs verification.')
                metadata = {key: copy.deepcopy(details[key]) for key in (
                    'mime_type','video_codec','width','height','average_frame_rate','nominal_frame_rate',
                    'duration_seconds','frame_count','start_time_seconds','audio_codecs','audio_start_seconds',
                    'time_base','frame_pts_seconds','source_frames','actual_source_first_frame_seconds',
                    'actual_source_end_seconds','actual_original_first_frame_pts','requested_interval_is_frame_aligned',
                    'rotation_applied','mapping_basis','resampling_rounding_bound_seconds') if key in details}
                metadata.update(derivation_profile=receipt.manifest['profile'],
                    derivation_recipe=receipt.manifest['recipe'],
                    original_time_base=receipt.manifest['source']['time_base'],
                    requested_original_pts_start_seconds=receipt.manifest['requested_original_pts_start_seconds'],
                    requested_original_pts_end_seconds=receipt.manifest['requested_original_pts_end_seconds'])
                source_key = 'imported:' + source.sha256
                source_name, private_source = source.filename, source.path
            else:
                source = _safe_file(source_record['path'],self.state)
                source_key, source_name, private_source = str(source), source.name, source
                before = _fingerprint(source)
                if source_key in source_fingerprints and source_fingerprints[source_key] != before:
                    raise ValueError('A source changed during export preparation.')
                source_fingerprints[source_key] = before
                prepared = await self.media.clip(source_record,source_resolution=True)
                prepared = _safe_file(prepared,self.state)
                metadata = await _probe(prepared)
            if source_key not in source_cache:
                source_cache[source_key] = await _thread(_transfer,source,None,MAX_HASH_SOURCE_BYTES)
            filename=f'clips/{order:02d}.mp4'
            copied=await _thread(_transfer,prepared,files/filename,allowance-used-1024*1024)
            used+=copied['bytes']
            if not imported and before!=_fingerprint(source):
                raise ValueError('A source changed during export preparation.')
            speed=Fraction(choice['speed_multiplier'])
            media_duration=Fraction(str(metadata['duration_seconds']))
            proposed_duration=media_duration/speed
            requested_start,requested_end=Fraction(str(source_record['start'])),Fraction(str(source_record['end']))
            item={'sequence_order':order,'clip_id':source_record['id'],'asset_id':source_record['video_id'],
                'camera':source_record.get('camera'),'filename':filename,
                'source_url':f"/?clip={source_record['id']}",
                'requested_source_in':_rational(requested_start),'requested_source_out':_rational(requested_end),
                'media_in':_rational(Fraction(0)),'media_out':_rational(media_duration),
                'timeline_in':_rational(timeline),'timeline_out':_rational(timeline+proposed_duration),
                'speed_multiplier':_rational(speed),'framing':choice['framing'],'reason':choice['reason'],
                'source':{**source_cache[source_key],'filename':source_name,
                    'reference':_public_reference(source_record.get('source_reference')),
                    'provenance':_public_note(source_record.get('provenance'),self.state,private_source),
                    'camera_semantics':_public_note(source_record.get('camera_semantics'),self.state,private_source),
                    'clock':'source_relative',
                    'capture_start_utc':None if imported else source_record.get('source_capture_start_utc'),
                    'asset_revision':source_record.get('asset_revision')},
                'media':{**metadata,**copied,'speed_multiplier_applied':1,
                    'transformation':('Fixed sandbox source cut; H.264 CRF18 at original pixel dimensions after rotation and even-dimension alignment; source frame timestamps preserved; AAC when audio exists' if imported else 'ffmpeg accurate-seek bounded cut; H.264 CRF18 source resolution with even dimensions; available frame timestamps preserved; AAC when audio exists'),
                    'boundary_basis':('Verified original frame PTS; requested boundaries and actual first/end frame times are recorded separately.' if imported else 'Requested source seek/range; derivative duration and frame timing measured. Source frame mapping is not separately audited.')},
                'audio_treatment':'Included at normal speed when available; suggested timing belongs to the edit plan.'}
            if imported:
                thumbnail = resolved.thumbnail
                if thumbnail.size <= 2*1024**2:
                    (files/'thumbnails').mkdir(mode=0o700,exist_ok=True)
                    target=f'thumbnails/{order:02d}.jpg'
                    thumb=await _thread(_transfer,thumbnail,files/target,min(2*1024**2,allowance-used-1024*1024))
                    used+=thumb['bytes'];item['thumbnail']={'filename':target,**thumb,
                        'basis':'prepared_first_frame','width':source_record['prepared_width'],
                        'height':source_record['prepared_height']}
                # Recheck source ownership after the copies; the saved receipt is
                # reauthorized again immediately before package publication.
                await asyncio.to_thread(self._imported_source,source_record,record['owner_principal_id'])
            else:
                thumbnail=source_record.get('thumbnail_path')
                if thumbnail:
                    try:
                        path=_safe_file(thumbnail,self.state)
                        if path.suffix.lower() in ('.jpg','.jpeg') and path.stat().st_size<=2*1024**2:
                            (files/'thumbnails').mkdir(mode=0o700,exist_ok=True)
                            target=f'thumbnails/{order:02d}.jpg'
                            thumb=await _thread(_transfer,path,files/target,min(2*1024**2,allowance-used-1024*1024))
                            used+=thumb['bytes'];item['thumbnail']={'filename':target,**thumb}
                    except ValueError:
                        pass
            items.append(item);timeline+=proposed_duration
        return {'schema_version':1,'kind':'source_cut_editing_package','id':record['id'],
            'title':record['title'],'summary':record['summary'],'created_at':record['created_at'],
            'dataset':record['dataset'],'items':items,'timeline_duration':_rational(timeline),
            'output_fps':_rational(Fraction(selection['output_fps'])) if selection['output_fps'] else None,
            'output_fps_applied':False,'sequence_rendered':False,
            'notes':'Normal-speed source-resolution cuts; speed, framing and output frame rate are proposed editing settings. CSV is a documented handoff, not an editor-specific project.'}

    @staticmethod
    def _documents_and_zip(stop,files,archive,manifest,allowance):
        def fraction(value):return f"{value['numerator']}/{value['denominator']}"
        def write(name,text):
            path=files/name
            path.write_text(text,encoding='utf-8');path.chmod(0o600)
        write('manifest.json',json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
        write('README.txt','Rocoto editing package\n\n'
            'clips/ contains bounded source-resolution H.264 CRF18 cuts at normal speed. '
            'Available source frame timestamps are preserved, and audio is AAC when present.\n'
            'manifest.json records source and derived-file SHA256 hashes, requested source ranges, '
            'measured media, suggested sequence timing and acquisition notes.\n'
            'edit-decisions.csv is a human-readable edit plan; editor-specific import support is unverified. '
            'Speed, framing and output frame rate are suggestions; the package does not render a sequence.\n'
            'Inspect the source evidence and check source licences and sharing permission before redistribution.\n')
        report=[f"# {manifest['title']}",'',manifest['summary'],'','## Selected clips','']
        rows=[]
        for item in manifest['items']:
            report += [f"{item['sequence_order']}. [{item['camera'] or item['clip_id']}]({item['filename']}) — {item['reason']}",
                f"   Source `{item['asset_id']}`: {fraction(item['requested_source_in'])}–{fraction(item['requested_source_out'])} seconds; suggested speed {fraction(item['speed_multiplier'])}×."]
            rows.append({'sequence_order':item['sequence_order'],'clip_id':item['clip_id'],
                'filename':item['filename'],'asset_id':item['asset_id'],
                **{k:fraction(item[k]) for k in ('requested_source_in','requested_source_out','media_in','media_out','timeline_in','timeline_out','speed_multiplier')},
                'output_fps':fraction(manifest['output_fps']) if manifest['output_fps'] else '',
                'framing':item['framing'],'audio_treatment':item['audio_treatment'],'reason':item['reason']})
        write('report.md','\n'.join(report)+'\n')
        csv_text=io.StringIO(newline='');writer=csv.DictWriter(csv_text,fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            # Keep spreadsheet programs from interpreting user notes as formulas.
            writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()})
        write('edit-decisions.csv',csv_text.getvalue())
        paths=sorted(path for path in files.rglob('*') if path.is_file())
        with archive.open('xb') as output:
            os.fchmod(output.fileno(),0o600)
            with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as package:
                for path in paths:
                    _check(stop)
                    with path.open('rb') as source,package.open(str(path.relative_to(files)),'w') as target:
                        while chunk:=source.read(1024*1024):
                            _check(stop);target.write(chunk)
                            if output.tell()>allowance:
                                raise ValueError("This package exceeds its storage allowance.")
            output.flush();os.fsync(output.fileno())
