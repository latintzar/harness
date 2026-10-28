"""Pinned public source acquisition for the offline native installer.

Only host-owned catalogue records enter source_spec; HTTP routes supply an ID.
Download paths are chosen by the host and never exposed through public metadata.
"""
from __future__ import annotations
from dataclasses import asdict,dataclass
import asyncio
import hashlib
import os
from pathlib import Path,PurePosixPath
import re
from urllib.parse import urlsplit

import httpx

ARCHIVE_BYTES=32*1024*1024
FETCH_SECONDS=90

class InstallSourceError(ValueError):pass

@dataclass(frozen=True)
class InstallSource:
    plugin_id:str
    repository:str
    revision:str
    subtree:str
    catalogue_revision:str

    @property
    def url(self):return f'https://codeload.github.com/{self.repository}/tar.gz/{self.revision}'

    @property
    def archive_root(self):return self.repository.split('/')[1]+'-'+self.revision

    def document(self):return asdict(self)

def source_spec(entry,catalogue):
    if not isinstance(entry,dict) or entry.get('supported_api') is not True:raise InstallSourceError('plugin_setup_required')
    identifier=entry.get('id');revision=catalogue.get('revision')
    if not isinstance(identifier,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',identifier):raise InstallSourceError('invalid_plugin_id')
    if not isinstance(revision,str) or not re.fullmatch(r'[a-f0-9]{40}',revision):raise InstallSourceError('pin_catalogue_revision')
    try:source=entry['catalogue_entry']['source']
    except (KeyError,TypeError):raise InstallSourceError('plugin_setup_required') from None
    resolved=entry.get('resolved_source')
    if resolved is not None:
        if (not isinstance(resolved,dict) or source.get('source') not in {'url','git-subdir'}
                or set(resolved)!=(set(source)|{'sha'})
                or any(resolved.get(key)!=value for key,value in source.items())):
            raise InstallSourceError('resolved_source_differs_from_catalogue')
        source=resolved
    kind=source.get('source')
    if kind=='local':repository='openai/plugins';commit=revision;subtree=source.get('path','').removeprefix('./')
    elif kind in {'url','git-subdir'}:
        url=urlsplit(source.get('url',''))
        if (url.scheme!='https' or url.netloc!='github.com' or url.query or url.fragment or url.username or url.password
                or not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?',url.path)):
            raise InstallSourceError('public_pinned_source_required')
        repository=url.path[1:].removesuffix('.git');commit=source.get('sha');subtree=source.get('path','') if kind=='git-subdir' else ''
    else:raise InstallSourceError('plugin_setup_required')
    if not isinstance(commit,str) or not re.fullmatch(r'[a-f0-9]{40}',commit):raise InstallSourceError('pin_plugin_revision')
    if (not isinstance(subtree,str) or len(subtree)>512 or (subtree and (PurePosixPath(subtree).is_absolute()
            or any(x in {'','..','.'} or '\\' in x or '\0' in x for x in subtree.split('/'))))):
        raise InstallSourceError('contained_plugin_source_required')
    if kind=='local' and not subtree:raise InstallSourceError('contained_plugin_source_required')
    return InstallSource(identifier,repository,commit,subtree,revision)

async def fetch_archive(spec:InstallSource,destination:Path,*,transport=None):
    """One finite public HTTPS attempt; caller retries a fresh request explicitly."""
    if not isinstance(spec,InstallSource):raise InstallSourceError('invalid_source_spec')
    # Revalidate even manually constructed frozen dataclasses, before network.
    source_spec({'id':spec.plugin_id,'supported_api':True,'catalogue_entry':{'source':{
        'source':'git-subdir','url':'https://github.com/'+spec.repository+'.git','sha':spec.revision,'path':spec.subtree}}}, {'revision':spec.catalogue_revision})
    target=Path(destination)
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        count=0;hasher=hashlib.sha256()
        with os.fdopen(fd,'wb') as stream:
            async with asyncio.timeout(FETCH_SECONDS),httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False,
                    timeout=httpx.Timeout(30,connect=10),headers={'Accept':'application/gzip','User-Agent':'Rocoto-native-installer/1'}) as client:
                async with client.stream('GET',spec.url) as response:
                    if response.status_code!=200:raise InstallSourceError('public_archive_unavailable')
                    length=response.headers.get('content-length')
                    if length is not None and (not length.isdigit() or int(length)>ARCHIVE_BYTES):raise InstallSourceError('archive_size_limit')
                    if response.headers.get('content-encoding','identity')!='identity':raise InstallSourceError('archive_encoding_unsupported')
                    async for chunk in response.aiter_raw(65536):
                        count+=len(chunk)
                        if count>ARCHIVE_BYTES:raise InstallSourceError('archive_size_limit')
                        stream.write(chunk);hasher.update(chunk)
            if count==0 or (length is not None and count!=int(length)):raise InstallSourceError('archive_incomplete')
            stream.flush();os.fsync(stream.fileno())
        return {'sha256':hasher.hexdigest(),'size_bytes':count,'source':spec.document()}
    except BaseException:
        target.unlink(missing_ok=True)
        raise
