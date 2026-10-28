"""Host-reviewed authentication for original, pinned remote MCP packages.

A package endpoint supplies provenance, not authorization. A host descriptor
supplies exact auth destinations and permission policy; owner credentials and
consent are kept separately by MCPConnections. This module performs no network I/O.
"""
from __future__ import annotations

import layout
from dataclasses import dataclass
from copy import deepcopy
from functools import lru_cache
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

CATALOGUE_REVISION='5fd93af4cd0c623e020d0cc7e9ce178b4ac1f70f'
MAX_CONFIG_BYTES=128*1024

class ProviderConfigurationError(ValueError):pass

def _fail():
    raise ProviderConfigurationError('Review the company’s commissioned provider configuration.') from None

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def _url(value, *, origin=False):
    try:
        if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 or ord(c)>126 for c in value):_fail()
        p=urlsplit(value)
        if (p.scheme!='https' or not p.hostname or p.hostname!=p.hostname.lower() or p.username or p.password
                or p.port not in (None,443) or p.query or p.fragment or '\\'in value
                or '%'in p.netloc or p.hostname.endswith('.') or '.'not in p.hostname):_fail()
        try:address=ipaddress.ip_address(p.hostname)
        except ValueError:address=None
        if address is not None or p.hostname.endswith(('.localhost','.local','.internal')):_fail()
        if origin and (p.path or p.netloc!=p.hostname):_fail()
        return value
    except (TypeError,ValueError):_fail()

@lru_cache(maxsize=1)
def _entries():
    raw=json.loads((layout.RUNTIME / 'plugin_catalogue.json').read_bytes())
    if raw['revision']!=CATALOGUE_REVISION:_fail()
    result={}
    for plugin in raw['plugins']:
        servers=[s for s in plugin.get('mcp_servers',[])if s.get('transport')in {'http','streamable-http'}]
        if plugin.get('supported_api')is not True or not servers:continue
        if len(servers)!=1:_fail()
        server=servers[0]
        binding={'catalogue_revision':raw['revision'],'plugin_id':plugin['id'],'version':plugin['version'],
                 'source':plugin['catalogue_entry']['source'],'source_url':plugin['source_url'],
                 'resolved_source':plugin.get('resolved_source'),
                 'server_name':server['name'],'endpoint':_url(server['url'])}
        result[plugin['id']]={**binding,'name':plugin['name'],'catalogue_binding':_digest(binding),
            'reference':server['source_url'],'declared_auth':server.get('auth_kind','unknown')}
    if len(result)!=20:_fail()
    return result

def remote_entries():
    return deepcopy(_entries())

def pending_policies():
    return {name:{'name':entry['name'],'endpoint':entry['endpoint'],'auth':'setup_required',
        'scope':None,'hosts':frozenset({urlsplit(entry['endpoint']).hostname}),'tools':frozenset(),
        'description':'Connect the account used by this original plugin.','reference':entry['reference'],
        'available':False,'scopes_by_mode':{'read':()},'read_scope_authority':False,'token_auth_method':'none',
        'upstream_revision':CATALOGUE_REVISION,'policy_revision':'catalogue:'+entry['catalogue_binding'],
        'catalogue_binding':entry['catalogue_binding'],'setup_reason':'provider_commissioning_required'}
        for name,entry in _entries().items()}

@dataclass(frozen=True)
class RemoteProvider:
    plugin_id:str
    revision:int
    catalogue_binding:str
    auth:str
    issuer:str|None
    authorization_endpoint:str|None
    token_endpoint:str|None
    registration_endpoint:str|None
    token_auth_method:str
    scopes_by_mode:tuple[tuple[str,tuple[str,...]],...]
    read_tools:tuple[str,...]
    read_scope_authority:bool
    reference:str

    def __post_init__(self):
        entry=_entries().get(self.plugin_id)
        if (entry is None or self.catalogue_binding!=entry['catalogue_binding'] or type(self.revision)is not int
                or not 1<=self.revision<=2**31-1 or self.auth not in {'oauth_dcr','oauth_static','bearer','anonymous'}):_fail()
        _url(self.reference)
        if type(self.read_scope_authority)is not bool or not isinstance(self.read_tools,tuple) or len(self.read_tools)>512:_fail()
        if (len(set(self.read_tools))!=len(self.read_tools)
                or any(not isinstance(t,str)or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}',t)for t in self.read_tools)):_fail()
        if not isinstance(self.scopes_by_mode,tuple)or len(self.scopes_by_mode)not in (1,2):_fail()
        modes={}
        for item in self.scopes_by_mode:
            if not isinstance(item,tuple)or len(item)!=2:_fail()
            mode,scopes=item
            if mode not in {'read','read_write'}or mode in modes or not isinstance(scopes,tuple)or len(scopes)>64:_fail()
            if len(set(scopes))!=len(scopes)or any(not isinstance(s,str)or not 1<=len(s)<=512 or not all(0x21<=ord(c)<=0x7e and c not in '\\"'for c in s)for s in scopes):_fail()
            modes[mode]=scopes
        if 'read'not in modes or 'read_write'in modes and not set(modes['read'])<=set(modes['read_write']):_fail()
        if self.auth.startswith('oauth_'):
            _url(self.issuer);_url(self.authorization_endpoint);_url(self.token_endpoint)
            if self.token_auth_method not in {'none','client_secret_post','client_secret_basic'}:_fail()
            if self.auth=='oauth_dcr':_url(self.registration_endpoint)
            elif self.registration_endpoint is not None:_fail()
            destinations=[self.authorization_endpoint,self.token_endpoint,self.registration_endpoint]
            if len([v for v in destinations if v])!=len(set(v for v in destinations if v)):_fail()
            if self.read_scope_authority and not modes['read']:_fail()
        elif (any(v is not None for v in (self.issuer,self.authorization_endpoint,self.token_endpoint,self.registration_endpoint))
              or self.token_auth_method!='none' or self.read_scope_authority or any(modes.values())):_fail()
        if self.auth=='anonymous'and 'read_write'in modes:_fail()

    @classmethod
    def from_dict(cls,value):
        try:
            if not isinstance(value,dict)or set(value)!=set(cls.__dataclass_fields__):_fail()
            if not isinstance(value['scopes_by_mode'],dict)or not isinstance(value['read_tools'],list):_fail()
            data={**value,'scopes_by_mode':tuple((m,tuple(v))for m,v in sorted(value['scopes_by_mode'].items())if isinstance(v,list)),
                  'read_tools':tuple(value['read_tools'])}
            if len(data['scopes_by_mode'])!=len(value['scopes_by_mode']):_fail()
            return cls(**data)
        except (TypeError,ValueError,KeyError):_fail()

    @property
    def fingerprint(self):
        from dataclasses import asdict
        return _digest(asdict(self))

    def policy(self):
        entry=_entries()[self.plugin_id]
        urls=[entry['endpoint'],self.issuer,self.authorization_endpoint,self.token_endpoint,self.registration_endpoint]
        return {**pending_policies()[self.plugin_id],
            'auth':'oauth'if self.auth.startswith('oauth_')else self.auth,'available':True,
            'registration':'static'if self.auth=='oauth_static'else'dynamic'if self.auth=='oauth_dcr'else None,
            'issuer':self.issuer,'authorization_endpoint':self.authorization_endpoint,'token_endpoint':self.token_endpoint,
            'registration_endpoint':self.registration_endpoint,'token_auth_method':self.token_auth_method,
            'hosts':frozenset(urlsplit(u).hostname for u in urls if u),'tools':frozenset(self.read_tools),
            'scopes_by_mode':dict(self.scopes_by_mode),'read_scope_authority':self.read_scope_authority,
            'reference':self.reference,'setup_reason':None,'remote_descriptor':True,
            'descriptor_revision':self.revision,'descriptor_sha256':self.fingerprint,
            'plugin_id':self.plugin_id,'server_name':entry['server_name'],'package_version':entry['version'],
            'policy_revision':'remote-v1:'+str(self.revision)+':'+self.fingerprint}

@dataclass(frozen=True)
class RemoteProviderCommissioning:
    company_scope:str
    redirect_uri:str
    providers:tuple[RemoteProvider,...]

    def __post_init__(self):
        from registered_oauth import _callback,CALLBACK_PATH
        if (not isinstance(self.company_scope,str)or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',self.company_scope)
                or not isinstance(self.redirect_uri,str)or not self.redirect_uri.endswith(CALLBACK_PATH)
                or _callback(self.redirect_uri[:-len(CALLBACK_PATH)])!=self.redirect_uri
                or not isinstance(self.providers,tuple)or len(self.providers)>20
                or any(not isinstance(p,RemoteProvider)for p in self.providers)
                or len({p.plugin_id for p in self.providers})!=len(self.providers)):_fail()

    @classmethod
    def load(cls,path,*,company_scope,public_origin):
        from registered_oauth import read_private_configuration,_callback
        try:
            raw=read_private_configuration(path,max_bytes=MAX_CONFIG_BYTES)
            if (not isinstance(raw,dict)or set(raw)!={'schema_version','company_scope','redirect_uri','providers'}
                    or type(raw['schema_version'])is not int or raw['schema_version']!=1
                    or raw['company_scope']!=company_scope or raw['redirect_uri']!=_callback(public_origin)
                    or not isinstance(raw['providers'],list)):_fail()
            return cls(company_scope,raw['redirect_uri'],tuple(RemoteProvider.from_dict(v)for v in raw['providers']))
        except (OSError,ValueError,TypeError,KeyError):_fail()
