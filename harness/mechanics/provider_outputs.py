"""Standard provider resources become scoped output metadata, never URL fetches."""
from __future__ import annotations
import asyncio
import hashlib
import ipaddress
import json
import re
import sqlite3
import unicodedata
from urllib.parse import urlsplit,unquote

MAX_RESOURCES=16
MAX_TEXT_BYTES=100*1024
MAX_ITEMS=128
MAX_RESULT_BYTES=4*1024*1024


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

def label(value,fallback='',limit=160):
    if not isinstance(value,str):return fallback
    value=' '.join(''.join(c for c in value if not unicodedata.category(c).startswith('C')).split())
    return value[:limit] or fallback


def external_url(value):
    """Only navigable public HTTPS links; no host/network resolution is performed."""
    if not isinstance(value,str) or not 1<=len(value)<=4096 or value!=value.strip() or '\\' in value:return None
    if any(ord(c)<33 or ord(c)==127 for c in value):return None
    try:
        parsed=urlsplit(value);host=parsed.hostname
        if parsed.scheme!='https' or not host or parsed.username or parsed.password or parsed.port not in (None,443):return None
        host=host.rstrip('.').lower()
        try:ipaddress.ip_address(host);return None
        except ValueError:pass
        if '.' not in host or host.rsplit('.',1)[-1].isdigit() or host.endswith(('.localhost','.local','.internal','.test','.invalid')):return None
        if not re.fullmatch(r'[a-z0-9.-]+',host) or any(not p or p.startswith('-') or p.endswith('-') for p in host.split('.')):return None
        return value
    except (ValueError,UnicodeError):return None


def _google_created_file(result):
    """Drive create_file's documented File result, not arbitrary JSON URLs.

    https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/create_file
    The snippet is not the full document. Only the returned viewUrl is published.
    """
    value=result.get('structuredContent')
    if value is None:
        content=result.get('content',[])
        if len(content)!=1 or not isinstance(content[0],dict) or content[0].get('type')!='text':return None
        try:value=json.loads(content[0].get('text',''))
        except (ValueError,TypeError,RecursionError):return None
    if not isinstance(value,dict):return None
    identifier=value.get('id');url=external_url(value.get('viewUrl'))
    if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',identifier) or not url:return None
    parsed=urlsplit(url)
    patterns={'docs.google.com':r'/(?:document|spreadsheets|presentation)/d/'+re.escape(identifier)+r'(?:/(?:edit|view|preview))?/?',
              'drive.google.com':r'/(?:file/d/'+re.escape(identifier)+r'(?:/view)?|drive/folders/'+re.escape(identifier)+r')/?'}
    if parsed.hostname not in patterns or not re.fullmatch(patterns[parsed.hostname],parsed.path):return None
    return {'type':'resource_link','uri':url,'name':label(value.get('title'),'Google Drive file'),
        'mimeType':value.get('mimeType','application/octet-stream'),'description':label(value.get('description'),limit=1000)}


def resources(result,*,provider=None,tool=None):
    """Standard MCP blocks plus the reviewed, host-bound Drive create_file shape."""
    if not isinstance(result,dict) or result.get('isError') is True:return []
    try:
        if len(canonical(result).encode())>MAX_RESULT_BYTES:return []
    except (ValueError,TypeError,RecursionError,UnicodeError):return []
    content=result.get('content')
    if not isinstance(content,list):return []
    if provider=='google-drive' and tool=='create_file':
        created=_google_created_file(result)
        if created is not None:content=[*content,created]
    found=[];seen=set()
    for block in content[:100]:
        if not isinstance(block,dict):continue
        kind=block.get('type')
        if kind=='resource_link':resource=block
        elif kind=='resource' and isinstance(block.get('resource'),dict):resource=block['resource']
        else:continue
        uri=resource.get('uri')
        if not isinstance(uri,str) or not 1<=len(uri)<=4096 or any(ord(c)<32 for c in uri):continue
        url=external_url(uri);text=resource.get('text') if kind=='resource' else None
        if not isinstance(text,str):text=None
        if text is not None:
            try:
                if len(text.encode())>MAX_TEXT_BYTES or '\x00' in text:text=None
            except UnicodeError:text=None
        if not url and text is None:continue
        mime=resource.get('mimeType','text/plain' if text is not None else 'application/octet-stream')
        if not isinstance(mime,str) or not re.fullmatch(r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+',mime):mime='text/plain' if text is not None else 'application/octet-stream'
        title=label(resource.get('title')) or label(resource.get('name'))
        if not title and url:title=label(unquote(urlsplit(url).path.rstrip('/').rsplit('/',1)[-1]))
        title=title or 'Provider resource'
        entry={'title':title,'name':title,'source_url':url,'media_type':mime,'content':text,
               'description':label(resource.get('description'),limit=1000),'resource_uri_sha256':hashlib.sha256(uri.encode()).hexdigest()}
        # Text snapshots always render literally, including HTML/SVG and Markdown
        # with remote images. The original resource is opened explicitly by URL.
        identity=digest([uri,text])
        if identity in seen:continue
        seen.add(identity);found.append(entry)
        if len(found)==MAX_RESOURCES:break
    return found


class ProviderOutputs:
    """One verified native tree; host relay provenance is pinned at tool start."""
    def __init__(self,files,relay,*,owner,root_chat_id,dataset,company_id,current):
        self.files,self.relay=files,relay
        self.owner,self.root_chat_id,self.dataset,self.company_id=owner,root_chat_id,dataset,company_id
        self.current=current;self.calls={}

    async def event(self,method,params,*,actor_chat_id):
        if method not in {'item/started','item/completed'} or not self.current():return []
        item=params.get('item') or {}
        if not isinstance(item,dict) or item.get('type')!='mcpToolCall':return []
        server,tool,identity,thread=(item.get('server'),item.get('tool'),item.get('id'),params.get('threadId'))
        if any(not isinstance(v,str) or not 1<=len(v)<=256 for v in (server,tool,identity,thread)):return []
        key=(thread,identity)
        if method=='item/started':
            if key in self.calls or len(self.calls)>=MAX_ITEMS:return []
            try:provenance=await self.relay.provenance(server)
            except Exception:return []
            if not self.current():return []
            self.calls[key]={'actor':actor_chat_id,'server':server,'tool':tool,'provenance':provenance}
            return []
        admitted=self.calls.pop(key,None)
        if (not admitted or admitted['actor']!=actor_chat_id or admitted['server']!=server
                or admitted['tool']!=tool or item.get('status')!='completed' or item.get('error')):return []
        selected=resources(item.get('result'),provider=admitted['provenance']['provider'],tool=tool)
        if not selected:return []
        try:current=await self.relay.provenance(server)
        except Exception:return []
        if current!=admitted['provenance'] or not self.current():return []
        provenance={**current,'company_id':self.company_id,'owner':self.owner,'root_chat_id':self.root_chat_id,
                    'actor_chat_id':actor_chat_id,'native_thread_id':thread,'native_item_id':identity,'tool':tool,
                    'result_sha256':digest(item['result'])}
        # Both original packages and direct services derive display identity from
        # the host policy. Provider result fields never choose the provenance.
        provider_name=label(self.relay.display_name(server),label(current['provider'],'Provider'))
        from native_provider_relay import _read
        output=[];created=[];task=asyncio.current_task()
        authority={'owner_principal_id':self.owner,'chat_id':actor_chat_id,'dataset':self.dataset}
        try:
            async with self.relay.artifact_admission(server,current) as admitted_current:
                def guard():
                    return not task.cancelling() and self.current() and admitted_current()
                for resource in selected:
                    if not guard():raise ValueError('The resource request has finished.')
                    key=digest([provenance,resource['resource_uri_sha256'],resource['content']])
                    record=await _read(self.files.import_provider_resource,resource,provider=current['provider'],
                        provider_name=provider_name,publication_key=key,provenance=provenance,
                        **authority,guard=guard,created=created)
                    if not guard():raise ValueError('The resource request has finished.')
                    output.append({'type':'artifact','artifact':record})
            if not self.current() or task.cancelling():raise ValueError('The resource request has finished.')
        except BaseException as error:
            # A later disconnect must not remove historical results. Only rows
            # newly inserted by this exact admission have a rollback receipt.
            if created:await _read(self.files.discard_provider_publications,created,**authority)
            if isinstance(error,(ValueError,sqlite3.Error,OSError)):return []
            raise
        return output
