"""Fresh root-receipt checks for the separately commissioned document renderer."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import document_preview_contract as contract

RECEIPT=Path('/opt/rocoto-source-media/document-preview-readiness.json')
SOURCE_PINS={
    'source_media.py':'bfd8c5c1637b70d5f3219b99382a3a021008c5966f5b66994771c11759b07870',
    'native_executor.py':'09dcb88d13d8fdff83f2bfbaf6d521bd6b6106471554b7d067321f34e39bea08',
    'native_package_mount.py':'c4b9e19bab2ffe9585bb15cd5bf097e20802d992fbdb62ab329374b7aa371323',
    'document_preview_contract.py':'dcbcf27b3955ea46af85b50dc113f415079b8bf68db30f2ee222e1eaf7fb8e6b',
    'broker_service.py':'8e4c99179d35a097906f7490264196c4b2847ad52588983ba31e7e3f1ae05010',
}
SHA=re.compile(r'^[a-f0-9]{64}$')

def root_bytes(path,*,uid=0):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        if not (stat.S_ISREG(before.st_mode) and before.st_uid==uid and before.st_nlink==1
                and not before.st_mode&0o022 and 0<before.st_size<=1024*1024):
            raise ValueError('invalid_document_receipt')
        with os.fdopen(fd,'rb',closefd=False) as stream:data=stream.read(1024*1024+1)
        after=os.fstat(fd);current=Path(path).lstat()
        identity=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
        if identity(before)!=identity(after) or identity(after)!=identity(current) or len(data)!=before.st_size:
            raise ValueError('document_receipt_changed')
        return data
    finally:os.close(fd)

class DocumentPreviewReadiness:
    def __init__(self,*,enabled=False,receipt_sha256='',receipt_path=RECEIPT,reader=None):
        self.enabled=enabled is True;self.receipt_sha256=receipt_sha256
        self.path=Path(receipt_path);self.reader=reader or root_bytes
        self.worker_sha256=hashlib.sha256(Path(__file__).with_name('document_preview_worker.py').read_bytes()).hexdigest()

    def ready(self):
        if not self.enabled or not isinstance(self.receipt_sha256,str) or not SHA.fullmatch(self.receipt_sha256):return False
        try:
            raw=self.reader(self.path)
            if hashlib.sha256(raw).hexdigest()!=self.receipt_sha256:return False
            value=json.loads(raw)
            if not (isinstance(value,dict) and type(value.get('schema_version')) is int and value['schema_version']==1
                    and value.get('purpose')==contract.PURPOSE and value.get('profile')==contract.PROFILE
                    and value.get('recipe')==contract.RECIPE and value.get('image_digest')==contract.IMAGE
                    and value.get('worker_sha256')==self.worker_sha256 and value.get('preview_uid')==os.geteuid()
                    and value.get('preview_gid')==os.getegid() and value.get('provider_calls')==0):return False
            for field in ('installed_proof_sha256','private_runtime_proof_sha256'):
                if not isinstance(value.get(field),str) or not SHA.fullmatch(value[field]):return False
            sources=value.get('sources')
            if not isinstance(sources,list) or len(sources)!=len(SOURCE_PINS):return False
            if any(not isinstance(row,dict) or set(row)!={'path','sha256'} for row in sources):return False
            if {row['path']:row['sha256'] for row in sources}!=SOURCE_PINS:return False
            # Receipt and source bytes are rechecked for every request; no positive cache.
            for name,checksum in SOURCE_PINS.items():
                if hashlib.sha256(self.reader(self.path.parent/name)).hexdigest()!=checksum:return False
            return self.enabled and hashlib.sha256(self.reader(self.path)).hexdigest()==self.receipt_sha256
        except (OSError,ValueError,TypeError,KeyError):return False
