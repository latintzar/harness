"""Host-only fixed profile and output claims; never parses Office or PDF data."""
from dataclasses import dataclass
import re

IMAGE='sha256:7022ac2e9a7a1b9b44a36a2fb28f1c6f8bb59d333ba636c29cb44a53c71d8269'
PROFILE='document-preview-v1'
RECIPE='rocoto-libreoffice-poppler-pages-v1'
PURPOSE='document_preview_v1'
MAX_ORIGINAL=16*1024**2
MAX_OUTPUT=128*1024**2
MAX_PAGE=8*1024**2
MAX_TEXT=100*1024
MAX_PAGES=40
WALL_SECONDS=150

@dataclass(frozen=True)
class DocumentRequest:
    purpose:str
    file_id:str
    format:str

    def __post_init__(self):
        if self.purpose!=PURPOSE or not isinstance(self.file_id,str) or not re.fullmatch(r'file-[a-f0-9]{32}',self.file_id) or self.format not in {'docx','pdf','xlsx'}:
            raise ValueError('invalid_document_request')

    @classmethod
    def parse(cls,value):
        if not isinstance(value,dict) or set(value)!={'purpose','file_id','format'}:raise ValueError('invalid_document_request')
        return cls(**value)

def validate_manifest(manifest,files,*,request,original_sha256,original_bytes):
    def require(condition):
        if not condition:raise ValueError('invalid_document_preview')
    require(isinstance(manifest,dict) and manifest.get('schema_version')==1
        and manifest.get('profile')==PROFILE and manifest.get('recipe')==RECIPE
        and manifest.get('file_id')==request.file_id and manifest.get('format')==request.format
        and manifest.get('original_sha256')==original_sha256 and manifest.get('original_size_bytes')==original_bytes)
    require(set(manifest)<= {'schema_version','profile','recipe','file_id','format','original_sha256',
        'original_size_bytes','page_count','pages','text','renderer'}
        and isinstance(manifest.get('renderer',''),str) and len(manifest.get('renderer',''))<=128)
    pages=manifest.get('pages');count=manifest.get('page_count');text=manifest.get('text')
    require(type(count) is int and 1<=count<=MAX_PAGES and isinstance(pages,list) and len(pages)==count
        and isinstance(text,dict) and isinstance(files,list) and len(files)==count+2)
    require(all(isinstance(item,dict) and isinstance(item.get('path'),str) and type(item.get('size_bytes')) is int
        and 0<=item['size_bytes']<=MAX_PAGE for item in files))
    mapping={item.get('path'):item for item in files}
    require(len(mapping)==len(files) and 'manifest.json' in mapping)
    for i,page in enumerate(pages,1):
        require(isinstance(page,dict) and set(page)=={'page','path','width','height','size_bytes','sha256'}
            and page.get('page')==i and page.get('path')==f'page-{i:04d}.png'
            and all(type(page.get(k)) is int and 1<=page[k]<=1600 for k in ('width','height')))
    require(set(text)=={'path','size_bytes','sha256','truncated'} and text.get('path')=='text.txt' and type(text.get('truncated')) is bool)
    for row in [*pages,text]:
        file=mapping.get(row['path']);maximum=MAX_TEXT if row is text else MAX_PAGE
        require(isinstance(file,dict) and type(row.get('size_bytes')) is int and 0<=row['size_bytes']<=maximum
            and (row is text or row['size_bytes']>0) and file.get('size_bytes')==row['size_bytes']
            and isinstance(row.get('sha256'),str) and re.fullmatch(r'[a-f0-9]{64}',row['sha256'])
            and file.get('sha256')==row['sha256'])
    require(sum(f['size_bytes'] for f in files)<=MAX_OUTPUT)
    return manifest
