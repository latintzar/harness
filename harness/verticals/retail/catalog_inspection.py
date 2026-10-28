"""Automatic, bounded image evidence and catalog linking after product events.

The stream worker calls this service. It has no conversation, tools or wakeup
loop. Source pixels, object geometry and catalog identity are separate evidence:
a model-suggested match remains a candidate until a unique identifier or review
confirms it. The host supplies current authorization and local media resolution.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from PIL import Image, ImageOps


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def normalized(value):
    return ' '.join(re.findall(r'[\w]+', str(value).casefold()))


def geometry(value):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError('Use a normalized object rectangle.')
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in value):
        raise ValueError('Keep the object rectangle inside the image.')
    if value[0] >= value[2] or value[1] >= value[3]:
        raise ValueError('Use a rectangle with visible area.')
    return list(value)


def crop(image, box):
    if box is None:
        return image.copy()
    w, h = image.size
    return image.crop((max(0, math.floor(box[0]*w)), max(0, math.floor(box[1]*h)),
        min(w, math.ceil(box[2]*w)), min(h, math.ceil(box[3]*h))))


def extract_frame(path, timestamp):
    """Decode source pixels once on CPU. Host resolves path; model never does."""
    if not Path(path).is_file() or Path(path).is_symlink():
        raise ValueError('Use the registered local source recording.')
    result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(timestamp), '-i', str(path),
        '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=True)
    if len(result.stdout) > 40*1024*1024:
        raise ValueError('Use a source frame within the image limit.')
    image = Image.open(io.BytesIO(result.stdout))
    if image.width * image.height > 40_000_000:
        raise ValueError('Use a source frame within the pixel limit.')
    return ImageOps.exif_transpose(image).convert('RGB')


def read_text(image):
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    try:
        result = subprocess.run(['tesseract', 'stdin', 'stdout', '--psm', '11'],
            input=stream.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=12, check=True)
        return result.stdout.decode('utf-8', errors='replace')[:6000]
    except (FileNotFoundError, subprocess.SubprocessError):
        return ''


def link_catalog(text, candidates, *, object_grounded=False, proposed_id=None):
    """Only a unique, visibly read catalog identifier confirms automatically."""
    words = set(normalized(text).split())
    exact = []
    for product in candidates:
        if product.get('status') != 'confirmed':
            continue
        identifiers = [product.get('sku'), product.get('gtin'), product.get('attributes', {}).get('barcode')]
        identifiers += product.get('attributes', {}).get('identifiers', [])
        # Short identifiers are frequently shelf prices or unrelated numbers.
        if any(isinstance(v, str) and len(v) >= 5 and normalized(v) in words for v in identifiers):
            exact.append(product['id'])
    if object_grounded and len(set(exact)) == 1:
        return {'status': 'confirmed', 'product_id': exact[0], 'basis': 'unique_visible_identifier'}
    ids = {p['id'] for p in candidates}
    if proposed_id in ids:
        return {'status': 'candidate', 'product_id': proposed_id, 'basis': 'visual_catalog_candidate'}
    return {'status': 'unresolved', 'product_id': None, 'basis': 'object_evidence'}


class CatalogInspection:
    def __init__(self, state, *, authorized, source_resolver, catalog, publish_match,
                 inspect_image=None, frame_reader=extract_frame, ocr=read_text, clock=time.time,
                 object_verified=lambda owner,event,payload: False):
        self.root = Path(state) / 'catalog-inspection'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / 'inspection.sqlite'
        self.object_verified = object_verified
        self.authorized, self.source_resolver = authorized, source_resolver
        self.catalog, self.publish_match = catalog, publish_match
        self.inspect_image, self.frame_reader, self.ocr, self.clock = inspect_image, frame_reader, ocr, clock
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS inspections (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created REAL NOT NULL)''')
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def require(self, owner, event):
        if not self.authorized(owner, event):
            raise ValueError('Use item evidence from your current camera access.')

    def process(self, owner, event, payload, job_id):
        self.require(owner, event)
        key = hashlib.sha256(canonical([owner, job_id]).encode()).hexdigest()
        fingerprint = hashlib.sha256(canonical([event, payload]).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM inspections WHERE id=?', (key,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('Preserve the original inspection evidence on retry.')
                if prior['status'] == 'complete':
                    result = json.loads(prior['result'])
                    self.require(owner, event)
                    # Projection is idempotent; replay repairs a crash after evidence save.
                    self.publish_match(owner, event['id'], result)
                    return result
                raise ValueError('Review the existing inspection receipt before retrying.')
            db.execute('INSERT INTO inspections VALUES(?,?,?,?,?,NULL,?)',
                (key, owner, event['id'], fingerprint, 'processing', self.clock()))
        try:
            source = self.source_resolver(owner, payload['source_id'])
            if (not source or source.get('sha256') != payload.get('source_sha256')
                    or not source.get('path')):
                raise ValueError('Resolve the registered source bytes before inspection.')
            with open(source['path'], 'rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != source['sha256']:
                raise ValueError('Use the registered, unchanged recording bytes.')
            timestamp = payload['source_time_s']
            if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                    or not source.get('start', 0) <= timestamp <= source['end']):
                raise ValueError('Choose an instant inside the registered recording.')
            image = self.frame_reader(source['path'], timestamp)
            box = geometry(payload.get('object_bbox'))
            view = crop(image, box)
            text = self.ocr(view)
            candidates = self.catalog(owner, event)
            if not isinstance(candidates, list) or len(candidates) > 500:
                raise ValueError('Use a bounded location catalog for matching.')
            # Geometry alone does not prove that the crop isolates the touched item.
            # Only host-reviewed localization can upgrade OCR to a confirmed SKU.
            verified = box is not None and self.object_verified(owner, event, payload) is True
            match = link_catalog(text, candidates, object_grounded=verified)
            detail = None
            if match['status'] != 'confirmed' and self.inspect_image:
                self.require(owner, event)
                # A commissioned adapter performs one metered call, with this stable key.
                # It receives pixels + data only; remote text never controls tools or paths.
                detail = self.inspect_image(owner=owner, image=view,
                    description=payload.get('object_description') or event['description'],
                    text=text, candidates=candidates, request_id=key)
                self.require(owner, event)
                if not isinstance(detail, dict):
                    raise ValueError('Use a structured image inspection result.')
                if len(canonical(detail)) > 16000:
                    raise ValueError('Keep image inspection within its evidence limit.')
                local_box = geometry(detail.get('object_bbox'))
                if box is None and local_box is not None:
                    box = local_box
                    # Full-scene model text can belong to nearby shelf stock.
                    # Confirm only identifiers independently read in this crop.
                    text = self.ocr(crop(image, box))
                match = link_catalog(text, candidates,
                    object_grounded=verified, proposed_id=detail.get('product_id'))
            preview = crop(image, box)
            owner_root = self.root / hashlib.sha256(owner.encode()).hexdigest()
            owner_root.mkdir(mode=0o700, exist_ok=True)
            path = owner_root / (key + '.jpg')
            preview.save(path, format='JPEG', quality=94)
            path.chmod(0o600)
            result = {**match, 'inspection_id': key,
                'evidence': {'source_id': payload['source_id'], 'source_sha256': source['sha256'],
                    'source_time_s': timestamp, 'object_bbox': box,
                    'image_kind': 'object_crop' if box is not None else 'source_frame',
                    'width': preview.width, 'height': preview.height,
                    'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'image_url': '/api/catalog/inspections/' + key + '/image',
                    'ocr_text': text,
                    'inspection': detail,
                    'processing': 'cpu_ocr_and_image_model' if detail else 'cpu_ocr'},
                'created_at': self.clock()}
            self.require(owner, event)
            with self.connect() as db:
                db.execute("UPDATE inspections SET status='complete',result=? WHERE id=?", (canonical(result), key))
            self.publish_match(owner, event['id'], result)
            return result
        except Exception:
            with self.connect() as db:
                db.execute("UPDATE inspections SET status='review_required' WHERE id=? AND status<>'complete'", (key,))
            raise

    def image(self, owner, key, event_lookup):
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('Choose saved item evidence.')
        with self.connect() as db:
            row = db.execute("SELECT * FROM inspections WHERE id=? AND owner=? AND status='complete'", (key, owner)).fetchone()
        if not row:
            raise ValueError('Choose item evidence from your session.')
        self.require(owner, event_lookup(owner, row['event_id']))
        path = self.root / hashlib.sha256(owner.encode()).hexdigest() / (key + '.jpg')
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Choose saved item pixels.')
        return path
