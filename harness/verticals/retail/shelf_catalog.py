"""Durable, bounded shelf-photo extraction and source-derived provisional products."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from PIL import Image
from pydantic import BaseModel,ConfigDict,Field,model_validator
from catalog_inspection import geometry,crop
from catalog_image_model import ImageFundingUnavailable


class ShelfProduct(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    label:str=Field(min_length=1,max_length=200)
    description:str=Field(max_length=1000)
    bbox:list[float]=Field(min_length=4,max_length=4)
    visible_text:str=Field(max_length=1500)
    barcode_candidates:list[str]=Field(max_length=4)
    brand:str|None=Field(default=None,max_length=160)
    size:str|None=Field(default=None,max_length=160)
    uncertainty:str=Field(max_length=800)
    @model_validator(mode='after')
    def valid_box(self):
        geometry(self.bbox)
        if any(len(s)>80 for s in self.barcode_candidates):raise ValueError('Keep candidate identifiers bounded.')
        return self


class ShelfResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    products:list[ShelfProduct]=Field(max_length=24)
    notes:str=Field(max_length=2000)
    model:str
    usage_receipt_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')


class ShelfCatalog:
    def __init__(self,state,setup,locations,*,provider=None,enabled=False,funding_ready=lambda:False,clock=time.time):
        self.root=Path(state)/'shelf-catalog';self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=self.root/'jobs.sqlite';self.setup,self.locations=setup,locations
        self.provider,self.enabled,self.funding_ready,self.clock=provider,enabled,funding_ready,clock
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,photo_id TEXT NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,lease REAL NOT NULL DEFAULT 0,result TEXT,products TEXT NOT NULL DEFAULT \'[]\',error TEXT)')
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row;return db

    def _status(self,owner,photo,status,detail=None):
        self.setup.mark_photo_status(owner,photo['id'],status,detail)
        return {'photo_id':photo['id'],'status':status}

    def process(self,owner,photo_id):
        photo,path=self.setup.photo(owner,photo_id)
        self.locations.authorized(owner,photo['collection'],photo['location_id'],write=True)
        fingerprint=hashlib.sha256(json.dumps([owner,photo_id,photo['sha256'],'shelf-v1']).encode()).hexdigest();key=fingerprint
        with self.connect() as db:prior=db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
        if prior and prior['status']=='complete':
            ids=json.loads(prior['products']);self.setup.complete_photo(owner,photo_id,ids)
            return {'photo_id':photo_id,'status':'catalog_extracted','product_ids':ids}
        if prior and prior['status']=='review_required':return {'photo_id':photo_id,'status':'review_required'}
        if not photo.get('extraction_consent',{}).get('granted'):
            return self._status(owner,photo,'awaiting_upload_consent')
        if not prior or not prior['result']:
            if not self.enabled or self.provider is None:return self._status(owner,photo,'awaiting_commissioning')
            if not self.funding_ready():return self._status(owner,photo,'awaiting_funding')
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE');current=db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
                if current and current['status']=='review_required':return {'photo_id':photo_id,'status':'review_required'}
                if current and current['status']=='processing':
                    if current['lease']>self.clock():return {'photo_id':photo_id,'status':'extracting'}
                    db.execute("UPDATE jobs SET status='review_required',error='Reconcile the interrupted provider receipt before another call.' WHERE id=?",(key,))
                    return self._status(owner,photo,'review_required','Review the interrupted image request receipt.')
                if current and current['result']:
                    prior=current
                else:
                    db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?, ?,NULL,'[]',NULL)",(key,owner,photo_id,fingerprint,'processing',self.clock()+120))
                    prior=None
            if prior is None:
                self._status(owner,photo,'extracting')
                try:
                    with Image.open(path) as original:
                        original.load();image=original.convert('RGB')
                    scale=min(1,math.sqrt(16_000_000/(image.width*image.height)))
                    if scale<1:image=image.resize((max(1,int(image.width*scale)),max(1,int(image.height*scale))),Image.Resampling.LANCZOS)
                    # Current consent/authority is checked immediately before the one provider call.
                    current,_=self.setup.photo(owner,photo_id)
                    self.locations.authorized(owner,current['collection'],current['location_id'],write=True)
                    if not current.get('extraction_consent',{}).get('granted'):raise ValueError('Use the current photo processing consent.')
                    result=self.provider.extract_shelf(owner=owner,image=image,request_id=key)
                    result=ShelfResult.model_validate(result).model_dump()
                    if len(json.dumps(result).encode())>256*1024:raise ValueError('Keep the extraction result within its bound.')
                    self.setup.photo(owner,photo_id)
                    with self.connect() as db:db.execute("UPDATE jobs SET result=?,status='projecting' WHERE id=?",(json.dumps(result),key))
                except ImageFundingUnavailable:
                    with self.connect() as db:db.execute("UPDATE jobs SET status='pending',lease=0,error='Fund the bounded image request.' WHERE id=?",(key,))
                    return self._status(owner,photo,'awaiting_funding')
                except Exception:
                    with self.connect() as db:db.execute("UPDATE jobs SET status='review_required',error='Review the retained provider or image receipt.' WHERE id=?",(key,))
                    self._status(owner,photo,'review_required','Review the retained image request receipt.')
                    raise
            else:result=json.loads(prior['result'])
        else:result=json.loads(prior['result'])
        try:
            ids=[];owner_root=self.root/hashlib.sha256(owner.encode()).hexdigest();owner_root.mkdir(mode=0o700,exist_ok=True)
            existing={p['id']:p for p in self.locations.products(owner,photo['collection'],photo['location_id'])}
            with Image.open(path) as original:
                original.load()
                for i,item in enumerate(result['products']):
                    product_id='shelf-'+key[:28]+'-'+str(i);ids.append(product_id)
                    target=owner_root/(key+'-'+str(i)+'.png')
                    pixels=crop(original,item['bbox']).convert('RGB');pixels.info.clear();pixels.save(target,format='PNG');target.chmod(0o600)
                    provenance={'kind':'shelf_photo_extraction','photo_id':photo_id,'source_sha256':photo['sha256'],'bbox':item['bbox'],'inspection_id':key,
                        'model':result['model'],'usage_receipt_sha256':result['usage_receipt_sha256']}
                    if product_id not in existing:
                        self.locations.save_product(owner,{'id':product_id,'collection':photo['collection'],'location_id':photo['location_id'],'label':item['label'],'status':'provisional','sku':None,
                            'image':{'url':'/api/catalog/shelf-crops/'+key+'/'+str(i)+'/image','kind':'shelf_photo_crop','provenance':provenance},
                            'attributes':{k:v for k,v in item.items() if k not in {'label','bbox'}},'provenance':provenance})
            self.locations.authorized(owner,photo['collection'],photo['location_id'],write=True)
            with self.connect() as db:db.execute("UPDATE jobs SET status='complete',products=?,error=NULL WHERE id=?",(json.dumps(ids),key))
            self.setup.complete_photo(owner,photo_id,ids)
            return {'photo_id':photo_id,'status':'catalog_extracted','product_ids':ids}
        except Exception:
            # Saved model output permits projection repair without another paid call.
            self._status(owner,photo,'review_required','Finish projecting the saved product evidence.')
            raise

    def drain(self,limit=1):
        with self.setup.connect() as db:
            rows=db.execute("SELECT owner,id FROM photos WHERE status='pending' AND json_extract(body,'$.extraction_consent.granted')=1 ORDER BY rowid LIMIT 100").fetchall()
        result=[]
        for row in rows:
            with self.connect() as db:
                job=db.execute('SELECT status FROM jobs WHERE owner=? AND photo_id=?',(row['owner'],row['id'])).fetchone()
            if job and job['status']=='review_required':continue
            try:result.append(self.process(row['owner'],row['id']))
            except (ValueError,OSError,sqlite3.Error):continue
            if len(result)>=max(1,min(limit,4)):break
        return result

    def image(self,owner,key,index):
        if not re.fullmatch(r'[a-f0-9]{64}',key) or type(index)is not int or not 0<=index<24:raise ValueError('Choose an extracted shelf product.')
        with self.connect() as db:row=db.execute("SELECT * FROM jobs WHERE owner=? AND id=? AND status='complete'",(owner,key)).fetchone()
        if not row:raise ValueError('Choose a saved product image.')
        photo,_=self.setup.photo(owner,row['photo_id'])
        result=json.loads(row['result'])
        if index>=len(result['products']):raise ValueError('Choose an extracted product.')
        path=self.root/hashlib.sha256(owner.encode()).hexdigest()/(key+'-'+str(index)+'.png')
        if path.is_symlink() or not path.is_file():raise ValueError('Choose the source-derived product crop.')
        return path
