"""Bounded public HTTPS media acquisition; URLs stay private to the host.

No cookies, provider credentials, proxy environment, HTML execution, or automatic
HTTP retry. Each redirect is a new DNS-pinned, verified-TLS connection.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from email.message import Message
import ipaddress
import re
import ssl
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin, unquote
import httpcore
from custom_http import public_addresses, validated_addresses, _PinnedBackend

MAX_URL=8192
MAX_BYTES=256*1024**2
CHUNK=1024*1024
EXTENSIONS={'.mp4','.m4v','.mov','.webm','.mkv','.avi','.mpeg','.mpg','.mts','.m2ts','.ts'}
ACTIONS={
 'browser_download_required':'Open the link in your browser, download the video, then choose Upload files.',
 'access_required':'Choose a shared download link that permits access, or download the video and choose Upload files.',
 'video_file_required':'Choose a link to one video file, or download the video and choose Upload files.',
 'size_required':'Download the video and choose Upload files so its size can be checked.',
 'remote_changed':'The shared file changed. Add its current download link as a new import.',
 'file_size_limit':'Choose a video up to 256 MiB.',
 'download_interrupted':'Retry the download to continue this import.',
 'source_service_unavailable':'Your workspace owner can enable video imports.',
 'embedding_allowance_required':'Your workspace owner can add an upload allowance.',
 'embedding_allowance_reserved':'The current upload allowance is reserved.',
 'acquisition_stopped':'The import has stopped.',
 'download_capacity':'Two shared-link downloads are running. Retry when one finishes.',
}

class LinkError(ValueError):pass

def require(value,code='invalid_link'):
 if not value:raise LinkError(code)


def validate_url(value):
 from public_urls import validate_url as validate
 try:return validate(value)
 except ValueError:raise LinkError('invalid_link')from None


def classify(value):
 url=validate_url(value);parsed=urlsplit(url);host=parsed.hostname
 provider=('dropbox'if host in {'dropbox.com','www.dropbox.com'}or host.endswith('.dropboxusercontent.com')else
  'google_drive'if host in {'drive.google.com','drive.usercontent.google.com'}or host.endswith('.googleusercontent.com')else
  'wetransfer'if host=='we.tl'or host=='wetransfer.com'or host.endswith('.wetransfer.com')else'https')
 if provider=='dropbox'and host in {'dropbox.com','www.dropbox.com'}:
  if not re.match(r'^/(s/[^/]+/|scl/fi/[^/]+/)',parsed.path):return provider,url,'video_file_required'
  query=[(k,v)for k,v in parse_qsl(parsed.query,keep_blank_values=True,max_num_fields=64)if k not in {'dl','raw'}]
  url=urlunsplit(('https',host,parsed.path,urlencode(query+[('dl','1')]),''))
 if provider=='google_drive'and host=='drive.google.com'and not parsed.path.startswith('/uc'):
  return provider,url,'browser_download_required'
 if provider=='wetransfer'and(host=='we.tl'or parsed.path.startswith('/downloads/')):
  return provider,url,'browser_download_required'
 return provider,url,None


def media_magic(data):
 # A cheap rejection boundary only. Codec parsing remains in the media sandbox.
 return(len(data)>=12 and(data[4:8]==b'ftyp'or data[:4]==b'\x1aE\xdf\xa3'or
  (data[:4]==b'RIFF'and data[8:12]==b'AVI ')or data[:4]in{b'\x00\x00\x01\xba',b'\x00\x00\x01\xb3'}or
  (data[0]==0x47 and len(data)>188 and data[188]==0x47)))


def metadata(headers,url,max_bytes):
 require(len(headers)<=64 and sum(len(k)+len(v)+4 for k,v in headers)<=16384,'video_file_required')
 values={}
 for key,value in headers:
  key=key.lower();require(key not in values or key not in {b'content-length',b'content-type',b'content-encoding',b'etag'},'video_file_required');values[key]=value
 require(values.get(b'content-encoding',b'identity').lower()in{b'identity',b''},'video_file_required')
 length=values.get(b'content-length',b'');require(re.fullmatch(rb'[0-9]{1,10}',length),'size_required');size=int(length)
 require(0<size<=max_bytes,'file_size_limit')
 kind=values.get(b'content-type',b'application/octet-stream').decode('latin1').split(';',1)[0].lower().strip()
 require(kind.startswith('video/')or kind in{'application/octet-stream','application/mp4','application/x-matroska'},'video_file_required')
 name='Shared video.mp4';disposition=values.get(b'content-disposition')
 if disposition:
  message=Message();message['content-disposition']=disposition.decode('latin1');name=message.get_filename()or name
 require(isinstance(name,str),'video_file_required')
 name=name.rsplit('/',1)[-1].rsplit('\\',1)[-1]
 # Keep opaque URL tokens out of public filenames; use a fixed fallback.
 if not any(name.lower().endswith(suffix)for suffix in EXTENSIONS):name='Shared video.mp4'
 require(0<len(name.encode())<=255 and not any(ord(c)<32 or ord(c)==127 for c in name),'video_file_required')
 etag=values.get(b'etag',b'').decode('latin1');require(len(etag)<=512,'video_file_required')
 return {'filename':name,'size_bytes':size,'content_type':kind,'etag':etag}

@dataclass
class Download:
 metadata:dict
 response:object=field(repr=False)
 current:object=field(repr=False)
 async def chunks(self):
  pending=bytearray();total=0
  async for raw in self.response.aiter_stream():
   require(self.current(),'acquisition_stopped');total+=len(raw)
   require(total<=self.metadata['size_bytes'],'remote_changed')
   # httpcore's socket reads are bounded; retain one application chunk only.
   view=memoryview(raw)
   while view:
    take=min(CHUNK-len(pending),len(view));pending.extend(view[:take]);view=view[take:]
    if len(pending)==CHUNK:yield bytes(pending);pending.clear()
  require(total==self.metadata['size_bytes'],'download_interrupted')
  if pending:yield bytes(pending)

class PublicDownload:
 def __init__(self,*,resolver=public_addresses,network_backend=None,ssl_context=None):
  self.resolver,self.backend=resolver,network_backend;self.ssl=ssl_context or ssl.create_default_context()
  require(self.ssl.verify_mode==ssl.CERT_REQUIRED and self.ssl.check_hostname,'verified_tls_required')
 @asynccontextmanager
 async def open(self,url,*,current,max_bytes=MAX_BYTES):
  url=validate_url(url)
  for hop in range(6):
   require(current(),'acquisition_stopped');host=urlsplit(url).hostname
   async with asyncio.timeout(15):addresses=validated_addresses(await self.resolver(host))
   require(current(),'acquisition_stopped')
   backend=_PinnedBackend(host,addresses,current,self.backend)
   async with httpcore.AsyncConnectionPool(ssl_context=self.ssl,network_backend=backend,max_connections=1,max_keepalive_connections=0,http2=False,retries=0)as pool:
    async with pool.stream('GET',url,headers=[(b'host',host.encode()),(b'accept-encoding',b'identity')],
     extensions={'timeout':{'connect':5,'read':15,'write':5,'pool':1}})as response:
     if response.status in{301,302,303,307,308}:
      require(hop<5,'browser_download_required')
      locations=[v for k,v in response.headers if k.lower()==b'location'];require(len(locations)==1,'browser_download_required')
      url=validate_url(urljoin(url,locations[0].decode('ascii')))
      if urlsplit(url).hostname in{'accounts.google.com','login.live.com'}:raise LinkError('access_required')
      continue
     if response.status in{401,403,404,410}:raise LinkError('access_required')
     require(response.status==200,'download_interrupted')
     yield Download(metadata(response.headers,url,min(MAX_BYTES,max_bytes)),response,current)
     return
  raise LinkError('browser_download_required')
