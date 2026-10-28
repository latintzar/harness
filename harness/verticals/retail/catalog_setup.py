"""Location catalog onboarding with private, bounded shelf-photo handoff.

The host owns company/location authority. A phone capability permits image uploads
only; it cannot list catalogs, read photos or create products. Tokens are hashed at
rest and carried in the URL fragment so access logs never contain them.
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
from PIL import Image, ImageOps, UnidentifiedImageError
from location_intelligence import ProductDefinition

MAX_BYTES=10*1024*1024
MAX_PHOTOS=8
TTL_SECONDS=900


def token_hash(token):
    if not isinstance(token,str) or len(token)!=64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Open the current shelf-photo link.')
    return hashlib.sha256(token.encode()).hexdigest()


def qr_svg(value):
    from reportlab.graphics.barcode import qrencoder
    qr=qrencoder.QRCode(None,qrencoder.QRErrorCorrectLevel.M);qr.addData(value);qr.make();size=qr.getModuleCount()
    commands=[]
    for y in range(size):
        for x in range(size):
            if qr.isDark(y,x):commands.append(f'M{x+4} {y+4}h1v1h-1z')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size+8} {size+8}" role="img" aria-label="Open shelf-photo upload"><path fill="white" d="M0 0h{size+8}v{size+8}H0z"/><path fill="black" d="'+''.join(commands)+'"/></svg>'



class CatalogSetup:
    def __init__(self,state,locations,environment_id,*,clock=time.time):
        self.root=Path(state)/'catalog-setup';self.path=self.root/'setup.sqlite'
        self.locations,self.environment_id,self.clock=locations,environment_id,clock
        self.photo_sink=None
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS handoffs(hash TEXT PRIMARY KEY,owner TEXT NOT NULL,collection TEXT NOT NULL,location TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0,closed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY,owner TEXT NOT NULL,collection TEXT NOT NULL,location TEXT NOT NULL,sha256 TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,owner TEXT NOT NULL,collection TEXT NOT NULL,location TEXT NOT NULL,body TEXT NOT NULL);''')
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row;return db

    def overview(self,owner,collection,location_id):
        self.locations.authorized(owner,collection,location_id)
        with self.connect() as db:
            photos=[json.loads(row[0]) for row in db.execute('SELECT body FROM photos WHERE owner=? AND collection=? AND location=?',(owner,collection,location_id))]
        return {'collection':collection,'location_id':location_id,'methods':[
            {'id':'catalog_import','name':'Import your catalog','accept':'.csv,.json','fields':['sku','name','barcode']},
            {'id':'shelf_photos','name':'Photograph your shelves','max_images':MAX_PHOTOS,'max_bytes':MAX_BYTES},
            {'id':'camera_observations','name':'Build from camera observations','identity_status':'provisional'}],
            'product_count':len(self.locations.products(owner,collection,location_id)),'photos':photos,
            'catalog_url':'/api/control-room/catalog?collection='+collection+'&location_id='+location_id}

    def import_products(self,owner,collection,location_id,rows,*,source_name,source_kind='catalog_file'):
        self.locations.authorized(owner,collection,location_id,write=True)
        if source_kind not in {'catalog_file','pos_export'} or not isinstance(source_name,str) or not source_name.strip() or len(source_name)>160:
            raise ValueError('Name the catalog file or POS export.')
        if not isinstance(rows,list) or not 1<=len(rows)<=500:raise ValueError('Import up to 500 catalog rows per file.')
        payload=json.dumps(rows,sort_keys=True,allow_nan=False)
        if len(payload.encode())>2*1024*1024:raise ValueError('Choose a catalog file up to 2 MB.')
        digest=hashlib.sha256(payload.encode()).hexdigest();receipt=hashlib.sha256(json.dumps([owner,collection,location_id,digest,source_kind]).encode()).hexdigest()
        with self.connect() as db:
            previous=db.execute('SELECT body FROM imports WHERE id=? AND owner=?',(receipt,owner)).fetchone()
        prior=json.loads(previous[0]) if previous else {}
        imported_at=prior.get('imported_at') or datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()
        prepared=[];seen=set()
        for row in rows:
            if not isinstance(row,dict) or set(row)-{'sku','name','label','barcode','description','price','currency'}:raise ValueError('Use the documented catalog columns.')
            sku=str(row.get('sku') or '').strip();label=str(row.get('name') or row.get('label') or '').strip()
            if not sku or not label or sku in seen:raise ValueError('Give each product a distinct SKU and name.')
            seen.add(sku)
            key='catalog-'+hashlib.sha256(json.dumps([collection,location_id,sku]).encode()).hexdigest()[:32]
            attributes={k:row[k] for k in ('barcode','description') if row.get(k) is not None}
            price=None
            if row.get('price') not in (None,''):
                price={'amount':row['price'],'currency':row.get('currency'),'basis':'catalog_sourced',
                    'source':source_name,'observed_at':imported_at}
            prepared.append(ProductDefinition(id=key,collection=collection,location_id=location_id,label=label,status='confirmed',sku=sku,price=price,attributes=attributes,
                provenance={'kind':source_kind,'source_name':source_name,'import_id':receipt,'sha256':digest,'authority':'user_supplied_catalog'}).model_dump(mode='json'))
        # Validate every row before applying. Retries reuse deterministic SKU identities.
        products=[self.locations.save_product(owner,row) for row in prepared]
        value={'id':receipt,'imported_at':imported_at,'source_name':source_name,'source_kind':source_kind,'sha256':digest,'product_ids':[p['id'] for p in products],'count':len(products)}
        with self.connect() as db:db.execute('INSERT OR IGNORE INTO imports VALUES(?,?,?,?,?)',(receipt,owner,collection,location_id,json.dumps(value)))
        return value

    def create_handoff(self,owner,collection,location_id,*,origin):
        self.locations.authorized(owner,collection,location_id,write=True)
        if not origin.startswith(('https://','http://127.0.0.1:','http://localhost:')) or '#' in origin or '?' in origin:
            raise ValueError('Use the current application origin for the upload link.')
        token=secrets.token_hex(32);expires=self.clock()+TTL_SECONDS
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active=db.execute('SELECT count(*) FROM handoffs WHERE owner=? AND expires>? AND closed=0',(owner,self.clock())).fetchone()[0]
            if active>=3:raise ValueError('Finish a current photo handoff or wait for its expiry.')
            db.execute('INSERT INTO handoffs VALUES(?,?,?,?,?,0,0)',(token_hash(token),owner,collection,location_id,expires))
        url=origin.rstrip('/')+'/pages/retail/catalog-upload/'+self.environment_id+'#'+token
        return {'upload_url':url,'qr_svg':qr_svg(url),'expires_at':expires,'max_images':MAX_PHOTOS,'max_bytes':MAX_BYTES}

    def _handoff(self,db,token):
        row=db.execute('SELECT * FROM handoffs WHERE hash=?',(token_hash(token),)).fetchone()
        if row is None or row['closed'] or row['expires']<=self.clock() or row['used']>=MAX_PHOTOS:
            raise ValueError('Open a fresh shelf-photo link from the location.')
        self.locations.authorized(row['owner'],row['collection'],row['location'],write=True)
        return row

    def upload(self,token,data,*,extraction_consent=False):
        if not data or len(data)>MAX_BYTES:raise ValueError('Choose a shelf photo up to 10 MB.')
        with self.connect() as db:self._handoff(db,token)
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.width*source.height>20_000_000:raise ValueError('Choose a shelf photo up to 20 megapixels.')
                source.load();clean=ImageOps.exif_transpose(source).convert('RGB');clean.info.clear()
                output=io.BytesIO();clean.save(output,format='PNG')
        except (UnidentifiedImageError,OSError,Image.DecompressionBombError) as exc:
            raise ValueError('Choose a valid PNG, JPEG or WebP shelf photo.') from exc
        pixels=output.getvalue();key=uuid.uuid4().hex;path=self.root/(key+'.png')
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE');row=self._handoff(db,token)
                path.write_bytes(pixels);path.chmod(0o600)
                value={'id':key,'location_id':row['location'],'collection':row['collection'],'sha256':hashlib.sha256(pixels).hexdigest(),
                    'width':clean.width,'height':clean.height,'image_url':'/api/catalog/photos/'+key+'/image','received_at':self.clock(),
                    'provenance':{'kind':'shelf_photo_upload','pixel_transform':'exif_orientation_and_rgb_png','original_sha256':hashlib.sha256(data).hexdigest()},'status':'awaiting_catalog_extraction','extraction_consent':{'granted':extraction_consent is True,'purpose':'provisional_catalog_extraction','granted_at':self.clock() if extraction_consent is True else None}}
                db.execute('INSERT INTO photos VALUES(?,?,?,?,?,?,?)',(key,row['owner'],row['collection'],row['location'],value['sha256'],json.dumps(value),'pending'))
                db.execute('UPDATE handoffs SET used=used+1 WHERE hash=?',(token_hash(token),))
        except BaseException:
            path.unlink(missing_ok=True);raise
        if self.photo_sink:self.photo_sink(row['owner'],value)
        return {'received':True,'remaining':MAX_PHOTOS-row['used']-1}

    def photo(self,owner,key):
        if not isinstance(key,str) or len(key)!=32 or any(c not in '0123456789abcdef' for c in key):raise ValueError('Choose a saved shelf photo.')
        with self.connect() as db:row=db.execute('SELECT * FROM photos WHERE owner=? AND id=?',(owner,key)).fetchone()
        if row is None:raise ValueError('Choose a shelf photo in your location.')
        self.locations.authorized(owner,row['collection'],row['location'])
        path=self.root/(key+'.png')
        if path.is_symlink() or not path.is_file():raise ValueError('Choose the saved shelf-photo pixels.')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:raise ValueError('Use the original uploaded photo.')
        return json.loads(row['body']),path

    def list_pending_photos(self,owner):
        with self.connect() as db:ids=[r[0] for r in db.execute("SELECT id FROM photos WHERE owner=? AND status='pending'",(owner,))]
        values=[]
        for key in ids:
            try:value,path=self.photo(owner,key)
            except ValueError:continue
            values.append({**value,'path':str(path)})
        return values

    def mark_photo_status(self,owner,key,status,detail=None):
        if status not in {'awaiting_catalog_extraction','awaiting_commissioning','awaiting_funding','awaiting_upload_consent','extracting','review_required'}:
            raise ValueError('Use the current photo processing state.')
        value,_=self.photo(owner,key)
        value.update(status=status)
        if detail:value['processing_note']=str(detail)[:400]
        with self.connect() as db:db.execute("UPDATE photos SET body=? WHERE owner=? AND id=? AND status='pending'",(json.dumps(value),owner,key))
        return value

    def complete_photo(self,owner,key,product_ids):
        value,_=self.photo(owner,key)
        allowed={p['id'] for p in self.locations.products(owner,value['collection'],value['location_id'])}
        if not isinstance(product_ids,list) or not set(product_ids)<=allowed:raise ValueError('Use catalog products from this location.')
        value.update(status='catalog_extracted',product_ids=product_ids)
        with self.connect() as db:db.execute("UPDATE photos SET status='complete',body=? WHERE owner=? AND id=?",(json.dumps(value),owner,key))
        return value
