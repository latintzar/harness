"""Host-owned, principal-scoped remote MCP reads and OAuth consent.

One registered broker owns each company workspace. Provider content is untrusted
source data. Credentials and OAuth callbacks never become agent tool arguments.
"""
from __future__ import annotations

import asyncio
import contextvars
import fcntl
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import time
import stat
from urllib.parse import parse_qs, urlsplit, urlencode, urlunsplit

import httpx
import jsonschema
from referencing import Registry
from referencing.exceptions import NoSuchResource
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from workspace_identity import workspace_scope
from registered_oauth import RegisteredOAuthClients
from catalogue_providers import RemoteProviderCommissioning
from provider_policy import (CATALOGUE, selected_scopes, scopes,
    provider_read_only, read_authorized, visible_tools, digest, effective_policies)
from provider_credentials import (Vault, CredentialError, initialize, locked, directory, _format, TABLE, configure_database, _bounded_database)

CALLBACK_PATH = '/api/connections/oauth/callback'
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_ARGUMENT_BYTES = 32 * 1024
MAX_INVENTORY_BYTES = 2 * 1024 * 1024
MAX_TOOLS = 512


class ConnectionError(ValueError):
    """Safe, public broker error; provider exceptions are intentionally excluded."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _identity(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}',value):
        raise ConnectionError('Use the authenticated workspace identity.')
    return value


def _binding(value):
    if not isinstance(value,str) or not 1 <= len(value) <= 1024:
        raise ConnectionError('Continue from the session that opened this connection.')
    return _hash(value)


def _provider(value):
    try: return CATALOGUE[value]
    except (KeyError,TypeError): raise ConnectionError('Choose a reviewed connection.') from None


def validate_provider_url(url, hosts):
    try:
        parsed=urlsplit(str(url))
        if (parsed.scheme!='https' or parsed.hostname not in hosts or parsed.username
                or parsed.password or parsed.port not in (None,443) or parsed.fragment
                or len(str(url))>16384):
            raise ValueError
    except (TypeError,ValueError):
        raise ConnectionError('The provider endpoint requires a reviewed HTTPS address.') from None
    return parsed


class _PrivateSDKLog(logging.Filter):
    def filter(self, record):
        # SDK exception strings can include OAuth query/body values. Retain only level.
        record.msg='Remote MCP protocol event';record.args=();record.exc_info=None;record.exc_text=None
        return True


for _logger_name in ('mcp.client.auth.oauth2','mcp.client.streamable_http'):
    _logger=logging.getLogger(_logger_name)
    if not any(isinstance(f,_PrivateSDKLog) for f in _logger.filters):
        _logger.addFilter(_PrivateSDKLog())


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream): self.stream=stream;self.total=0
    async def __aiter__(self):
        async for chunk in self.stream:
            self.total+=len(chunk)
            if self.total>MAX_RESULT_BYTES:
                raise ConnectionError('Request a smaller provider result.')
            yield chunk
    async def aclose(self): await self.stream.aclose()


class _GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, hosts):
        self.hosts=hosts
        self.inner=httpx.AsyncHTTPTransport(retries=0,limits=httpx.Limits(max_keepalive_connections=0))
    async def handle_async_request(self, request):
        parsed=validate_provider_url(str(request.url),self.hosts)
        try:
            addresses=await asyncio.wait_for(asyncio.to_thread(
                socket.getaddrinfo,parsed.hostname,443,type=socket.SOCK_STREAM),5)
            def public(value):
                ip=ipaddress.ip_address(value);mapped=getattr(ip,'ipv4_mapped',None)
                return all(p.is_global and not(p.is_private or p.is_loopback or p.is_link_local or p.is_multicast or p.is_reserved or p.is_unspecified) for p in (ip,mapped)if p is not None)
            if not addresses or len(addresses)>64 or any(not public(a[4][0])for a in addresses):
                raise ConnectionError('The provider requires a public network address.')
        except (OSError,TimeoutError):
            raise ConnectionError('Try the provider connection again.') from None
        # Pin the validated numeric address. Keep the reviewed Host and TLS SNI;
        # disable pooled reuse so two auth origins sharing an IP cannot share TLS.
        pinned=httpx.Request(request.method,request.url.copy_with(host=addresses[0][4][0]),
            headers=request.headers,stream=request.stream,extensions={**request.extensions,'sni_hostname':parsed.hostname})
        response=await self.inner.handle_async_request(pinned)
        if 300<=response.status_code<400:
            await response.aclose()
            raise ConnectionError('The provider redirect requires endpoint review.')
        response.stream=_LimitedStream(response.stream)
        return response
    async def aclose(self): await self.inner.aclose()


class _Storage:
    def __init__(self, broker, principal, provider, generation):
        self.broker=broker;self.principal=principal;self.provider=provider;self.generation=generation
        self.expires_at=None
    async def get_tokens(self):
        row=self.broker._check(self.provider,self.principal,self.generation)
        self.expires_at=row['expires_at']
        return OAuthToken.model_validate_json(row['tokens']) if row['tokens'] else None
    async def set_tokens(self,tokens):
        self.expires_at=time.time()+tokens.expires_in if tokens.expires_in is not None else None
        row=self.broker._check(self.provider,self.principal,self.generation)
        expected=tuple(json.loads(row['requested_scopes']))
        granted=scopes(tokens.scope) if tokens.scope is not None else tuple(json.loads(row['oauth_scopes'])) or expected
        previous=tuple(json.loads(row['oauth_scopes']))
        if not set(granted)<=set(expected) or previous and set(previous)!=set(granted):
            self.broker._invalidate(self.provider,self.principal,self.generation)
            raise ConnectionError('Review the provider scopes and reconnect this account.')
        previous_tokens=OAuthToken.model_validate_json(row['tokens']) if row['tokens'] else None
        tokens=tokens.model_copy(update={'scope':' '.join(granted) or None,
            'refresh_token':tokens.refresh_token or (previous_tokens.refresh_token if previous_tokens else None)})
        self.broker._update(self.provider,self.principal,self.generation,
            tokens=tokens.model_dump_json(),expires_at=self.expires_at,oauth_scopes=json.dumps(granted))
    async def get_client_info(self):
        row=self.broker._check(self.provider,self.principal,self.generation)
        return OAuthClientInformationFull.model_validate_json(row['client_info']) if row['client_info'] else None
    async def set_client_info(self,client_info):
        row=self.broker._check(self.provider,self.principal,self.generation)
        config=self.broker._provider(self.provider)
        if (client_info.token_endpoint_auth_method!=config['token_auth_method']
                or client_info.redirect_uris and list(map(str,client_info.redirect_uris))!=[self.broker.public_origin+CALLBACK_PATH]
                or not set(scopes(client_info.scope))<=set(json.loads(row['requested_scopes']))):
            raise ConnectionError('Review the registered provider client contract.')
        if config.get('registration')=='static':
            expected=self.broker._client(self.provider).client_info(json.loads(row['requested_scopes']))
            if client_info.model_dump()!=expected.model_dump():
                raise ConnectionError('Use the host’s registered provider client.')
        if client_info.issuer:validate_provider_url(str(client_info.issuer),config['hosts'])
        self.broker._update(self.provider,self.principal,self.generation,client_info=client_info.model_dump_json())


class _OAuth(OAuthClientProvider):
    def __init__(self,*args,requested_scopes=(),token_auth_method='none',allow_authorization=False,refresh_token_endpoint=None,registered_policy=None,current=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.requested_scopes=tuple(requested_scopes);self.token_auth_method=token_auth_method
        self.allow_authorization=allow_authorization
        self.refresh_token_endpoint=refresh_token_endpoint
        self.registered_policy=registered_policy;self.current=current

    async def _initialize(self):
        await super()._initialize()
        self.context.token_expiry_time=self.context.storage.expires_at

    async def _refresh_token(self):
        request=await super()._refresh_token()
        # SDK 1.30 refreshes stored expired tokens before rediscovering metadata.
        # Its /token fallback differs from some commissioned providers. Use the
        # host-reviewed endpoint only for that empty-context case; discovery,
        # client authentication, scopes and refresh storage remain SDK-owned.
        if self.context.oauth_metadata is None and self.refresh_token_endpoint:
            return httpx.Request(request.method,self.refresh_token_endpoint,
                headers={k:v for k,v in request.headers.items() if k.lower()!='host'},content=request.content,extensions=request.extensions)
        return request

    def _clamp(self):
        # SDK 1.30 chooses all advertised/challenged scopes. Host consent is the
        # ceiling, including during a subsequent insufficient_scope challenge.
        self.context.client_metadata.scope=' '.join(self.requested_scopes) or None
        self.context.client_metadata.token_endpoint_auth_method=self.token_auth_method
        metadata=self.context.oauth_metadata
        if metadata and self.registered_policy:
            config=self.registered_policy
            for field in ('issuer','authorization_endpoint','token_endpoint'):
                actual=str(getattr(metadata,field,None))
                if field=='issuer' and urlsplit(actual).path=='/':actual=actual.rstrip('/')
                expected=config[field]
                if field=='issuer' and urlsplit(expected).path=='/':expected=expected.rstrip('/')
                if actual!=expected:
                    raise ConnectionError('Review the commissioned provider authorization metadata.')
            if config.get('registration')=='dynamic' and str(metadata.registration_endpoint)!=config['registration_endpoint']:
                raise ConnectionError('Review the commissioned provider registration endpoint.')
        if metadata and metadata.token_endpoint_auth_methods_supported is not None:
            if self.token_auth_method not in metadata.token_endpoint_auth_methods_supported:
                raise ConnectionError('Review the provider client authentication method.')

    async def _perform_authorization_code_grant(self):
        if not self.allow_authorization:raise ConnectionError('Reconnect this provider in settings.')
        self._clamp()
        return await super()._perform_authorization_code_grant()

    async def _auth_flow(self,request):
        # Preserve SDK state/PKCE/issuer handling while clamping its registration
        # request before HTTP sends it; authorization is clamped separately above.
        flow=super()._auth_flow(request)
        try:
            current=await anext(flow)
            while True:
                if self.current:self.current()
                if self.registered_policy:
                    config=self.registered_policy
                    parsed=validate_provider_url(str(current.url),config['hosts'])
                    # A secret-bearing request can target only the fixed token
                    # endpoint or MCP resource. Google never falls through to DCR.
                    target=str(current.url)
                    resource=httpx.URL(target)==httpx.URL(config['endpoint'])
                    token=(target==config['token_endpoint'] and current.method=='POST'
                           and current.headers.get('content-type','').startswith('application/x-www-form-urlencoded')
                           and parse_qs(current.content.decode()).get('grant_type') in
                               (['authorization_code'],['refresh_token']))
                    metadata=(current.method=='GET' and re.search(r'/\.well-known/(?:oauth-protected-resource|oauth-authorization-server|openid-configuration)(?:/[^?#]*)?$',parsed.path)
                              and not parsed.query and not current.headers.get('authorization'))
                    registration=(config.get('registration')=='dynamic' and target==config.get('registration_endpoint')
                                  and current.method=='POST' and not current.headers.get('authorization')
                                  and current.headers.get('content-type','').startswith('application/json'))
                    if not (resource or token or metadata or registration):
                        raise ConnectionError('Review the registered provider endpoint contract.')
                if current.method=='POST' and current.headers.get('content-type','').startswith('application/json'):
                    try:data=json.loads(current.content)
                    except (ValueError,UnicodeError):data={}
                    if isinstance(data,dict) and 'redirect_uris'in data and 'client_name'in data:
                        if not self.allow_authorization:raise ConnectionError('Reconnect this provider in settings.')
                        self._clamp();data['scope']=' '.join(self.requested_scopes) or None
                        data['token_endpoint_auth_method']=self.token_auth_method
                        headers={k:v for k,v in current.headers.items()if k.lower()!='content-length'}
                        current=httpx.Request(current.method,current.url,headers=headers,json=data,extensions=current.extensions)
                response=yield current
                if (not self.allow_authorization and response.status_code in (401,403)
                        and str(current.url)==self.context.server_url):
                    raise ConnectionError('Reconnect this provider in settings.')
                current=await flow.asend(response)
        except StopAsyncIteration:return
        finally:await flow.aclose()


@dataclass
class _Flow:
    id: str
    provider: str
    principal: str
    binding: str
    generation: int
    expires_at: float
    callback: asyncio.Future
    ready: asyncio.Event=field(default_factory=asyncio.Event)
    task: asyncio.Task|None=None
    authorization_url: str|None=None
    state_hash: str|None=None
    consumed: bool=False


@dataclass(frozen=True)
class DispatchCredentials:
    """Host memory only; never serialize this object into a model/event payload."""
    provider:str
    endpoint:str
    connection_generation:int
    access_mode:str
    oauth_scopes:tuple[str,...]
    policy_revision:str
    inventory_sha256:str|None
    expires_at:float|None
    access_token:str|None=field(repr=False)


@dataclass(frozen=True)
class BearerCredential:
    token:str=field(repr=False)
    expires_at:float|None=None

    def __post_init__(self):
        if (not isinstance(self.token,str)or not 1<=len(self.token)<=16384
                or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+',self.token)
                or self.expires_at is not None and (type(self.expires_at)not in (int,float)
                    or not time.time()+45<self.expires_at<time.time()+366*86400)):
            raise ConnectionError('Use a valid bounded provider bearer credential.')


class _Bearer(httpx.Auth):
    def __init__(self,token,endpoint,current):self.token=token;self.endpoint=endpoint;self.current=current
    def auth_flow(self,request):
        self.current()
        if request.url!=httpx.URL(self.endpoint):raise ConnectionError('Use the commissioned provider resource endpoint.')
        request.headers['Authorization']='Bearer '+self.token
        yield request


class MCPConnections:
    def __init__(self,state,*,public_origin,flow_timeout=300,write_providers=frozenset(),registered_clients=None,remote_providers=None):
        self.state=Path(state).resolve();self.company_scope=workspace_scope(self.state)
        if (registered_clients is not None and
                (not isinstance(registered_clients,RegisteredOAuthClients)
                 or registered_clients.company_scope!=self.company_scope
                 or registered_clients.redirect_uri!=public_origin.rstrip('/')+CALLBACK_PATH)):
            raise ConnectionError('Use this company’s registered OAuth client configuration.')
        if (remote_providers is not None and (not isinstance(remote_providers,RemoteProviderCommissioning)
                or remote_providers.company_scope!=self.company_scope
                or remote_providers.redirect_uri!=public_origin.rstrip('/')+CALLBACK_PATH)):
            raise ConnectionError('Use this company’s commissioned provider configuration.')
        self._remote_providers=remote_providers
        self._policies=effective_policies(remote_providers)
        self._registered_clients=registered_clients
        for client in registered_clients.clients if registered_clients else ():
            config=self._provider(client.provider)
            if (config.get('registration')!='static' or config.get('issuer')!=client.issuer
                    or config['token_auth_method']!=client.token_auth_method):
                raise ConnectionError('Match the registered client to its commissioned provider policy.')
        # Deployment authority, never an OAuth/model/browser supplied option.
        if (not isinstance(write_providers,(set,frozenset)) or any(
                not isinstance(provider,str) or provider not in self._policies or
                not self._configured(provider) or self._policies[provider]['auth']not in {'oauth','bearer'} or
                'read_write'not in self._policies[provider]['scopes_by_mode'] for provider in write_providers)):
            raise ConnectionError('Use the host-commissioned provider write policy.')
        self._write_providers=frozenset(write_providers)
        self.state=Path(state).resolve();self.company_scope=workspace_scope(self.state)
        parsed=urlsplit(public_origin)
        if public_origin and (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.port not in (None,443)):
            raise ConnectionError('Configure the public HTTPS callback origin.')
        self.public_origin=public_origin.rstrip('/')
        self.flow_timeout=min(600,max(.01,float(flow_timeout)))
        try:
            self.directory=directory(self.state);self.database=self.directory/'connections.sqlite'
            self._vault=initialize(self.directory,self.company_scope)
        except CredentialError as exc:raise ConnectionError(str(exc)) from None
        self._flows={};self._locks={};self._active=set();self._active_bindings={};self._revoked_reads=set();self._closed=False
        self._admission=contextvars.ContextVar('provider_admission_'+str(id(self)),default=None)
        self._slots=asyncio.Semaphore(8)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS flows (
                  id TEXT PRIMARY KEY,principal TEXT NOT NULL,provider TEXT NOT NULL,binding TEXT NOT NULL,
                  status TEXT NOT NULL,expires_at REAL NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS registered_clients (
                  provider TEXT PRIMARY KEY,revision INTEGER NOT NULL,fingerprint TEXT NOT NULL)''')
            if registered_clients is not None:
                selected={client.provider for client in registered_clients.clients}
                for previous in db.execute('SELECT * FROM registered_clients').fetchall():
                    if previous['provider'] not in selected and previous['fingerprint']:
                        # Explicit configuration omission is a durable revocation;
                        # an unconfigured read-only helper (None) does not mutate it.
                        db.execute("UPDATE registered_clients SET fingerprint='' WHERE provider=?",(previous['provider'],))
                        db.execute('''UPDATE connections_v2 SET generation=generation+1,status='reconnect_required',
                          tokens=NULL,client_info=NULL,expires_at=NULL,tools='[]',oauth_scopes='[]',inventory_sha256=NULL,
                          updated_at=? WHERE provider=?''',(_now(),previous['provider']))
            for client in registered_clients.clients if registered_clients else ():
                previous=db.execute('SELECT * FROM registered_clients WHERE provider=?',(client.provider,)).fetchone()
                if previous and (client.revision<previous['revision'] or
                        client.revision==previous['revision'] and client.fingerprint!=previous['fingerprint']):
                    raise ConnectionError('Advance the registered client revision before changing its configuration.')
                if not previous or client.revision!=previous['revision']:
                    db.execute('''UPDATE connections_v2 SET generation=generation+1,status='reconnect_required',
                      tokens=NULL,client_info=NULL,expires_at=NULL,tools='[]',oauth_scopes='[]',inventory_sha256=NULL,
                      updated_at=? WHERE provider=?''',(_now(),client.provider))
                    db.execute('INSERT OR REPLACE INTO registered_clients VALUES(?,?,?)',
                               (client.provider,client.revision,client.fingerprint))
            db.execute('CREATE TABLE IF NOT EXISTS remote_policies (provider TEXT PRIMARY KEY, revision INTEGER NOT NULL, fingerprint TEXT NOT NULL)')
            if remote_providers is not None:
                selected={p.plugin_id:p for p in remote_providers.providers}
                for previous in db.execute('SELECT * FROM remote_policies').fetchall():
                    if previous['provider']not in selected and previous['fingerprint']:
                        db.execute("UPDATE remote_policies SET fingerprint='' WHERE provider=?",(previous['provider'],))
                        self._revoke_configuration(db,previous['provider'])
                for provider,descriptor in selected.items():
                    previous=db.execute('SELECT * FROM remote_policies WHERE provider=?',(provider,)).fetchone()
                    if previous and (descriptor.revision<previous['revision'] or
                            descriptor.revision==previous['revision'] and descriptor.fingerprint!=previous['fingerprint']):
                        raise ConnectionError('Advance the provider descriptor revision before changing its configuration.')
                    if not previous or descriptor.revision!=previous['revision']:
                        self._revoke_configuration(db,provider)
                        db.execute('INSERT OR REPLACE INTO remote_policies VALUES(?,?,?)',
                                   (provider,descriptor.revision,descriptor.fingerprint))
        # A single registered runtime calls recover on startup. Construction does not
        # interrupt another live broker (fixtures and read-only callers may reopen).

    @staticmethod
    def _revoke_configuration(db,provider):
        db.execute("""UPDATE connections_v2 SET generation=generation+1,status='reconnect_required',
          tokens=NULL,client_info=NULL,expires_at=NULL,tools='[]',oauth_scopes='[]',inventory_sha256=NULL,
          updated_at=? WHERE provider=?""",(_now(),provider))

    def _provider(self,provider):
        try:return self._policies[provider]
        except (KeyError,TypeError):raise ConnectionError('Choose a reviewed connection.') from None

    def provider_policy(self,provider):
        """Host-only defensive copy; request data cannot modify broker authority."""
        from copy import deepcopy
        return deepcopy(self._provider(provider))

    def provider_policies(self):
        return {name:self.provider_policy(name)for name in self._policies}

    def _client(self,provider):
        return self._registered_clients.get(provider) if self._registered_clients else None

    def _configured(self,provider):
        config=self._provider(provider)
        return bool(self._client(provider)) if config.get('registration')=='static' else config['available']

    def _configuration_current(self,db,provider):
        config=self._provider(provider)
        previous=db.execute('SELECT revision,fingerprint FROM remote_policies WHERE provider=?',(provider,)).fetchone()
        if previous and (not config.get('remote_descriptor') or previous['revision']!=config['descriptor_revision']
                         or not hmac.compare_digest(previous['fingerprint'],config['descriptor_sha256'])):return False
        if config.get('remote_descriptor') and previous is None:return False
        if config.get('registration')!='static':return config['available']
        client=self._client(provider)
        if client is None:return False
        row=db.execute('SELECT revision,fingerprint FROM registered_clients WHERE provider=?',(provider,)).fetchone()
        return bool(row and row['revision']==client.revision and hmac.compare_digest(row['fingerprint'],client.fingerprint))

    def provider_available(self,provider):
        """Host-only configured authority, distinct from customer consent."""
        if not self._configured(provider):return False
        with self._db()as db:return self._configuration_current(db,provider)

    def provider_policy_revision(self,provider):
        config=self._provider(provider)
        if config.get('registration')!='static':return config['policy_revision']
        client=self._client(provider)
        if client is None:return config['policy_revision']
        return config['policy_revision']+':'+str(client.revision)+':'+client.policy_fingerprint

    @contextmanager
    def _db(self):
        try:
            with locked(self.directory):
                _bounded_database(self.database)
                db=sqlite3.connect(self.database,timeout=5);db.row_factory=sqlite3.Row
                try:
                    configure_database(db)
                    _format(db,self.company_scope)
                    db.execute('PRAGMA secure_delete=ON');db.execute('BEGIN IMMEDIATE')
                    yield db;db.commit()
                finally:db.close()
        except CredentialError as exc:raise ConnectionError(str(exc)) from None
        except sqlite3.Error:raise ConnectionError('Review the bounded provider credential store.') from None

    @property
    def write_providers(self):
        return self._write_providers

    def recover(self):
        if self._flows: raise ConnectionError('Close the active login flows before recovery.')
        with self._db() as db:
            db.execute("UPDATE flows SET status='interrupted',updated_at=? WHERE status IN ('connecting','awaiting_authorization','completing')",(_now(),))
            db.execute("UPDATE connections_v2 SET status='reconnect_required',tokens=NULL,client_info=NULL,expires_at=NULL,generation=generation+1,updated_at=? WHERE status='connecting'",(_now(),))

    def _record(self,provider,principal):
        self._provider(provider);_identity(principal)
        with self._db() as db:
            row=db.execute('SELECT * FROM connections_v2 WHERE provider=? AND principal=?',(provider,principal)).fetchone()
            if not row:return None
            result=dict(row)
            try:
                for field in ('tokens','client_info'):
                    result[field]=self._vault.open(result[field],principal=principal,provider=provider,generation=result['generation'],field=field)
            except CredentialError as exc:raise ConnectionError(str(exc)) from None
            return result

    def _check(self,provider,principal,generation,*,connected=False):
        row=self._record(provider,principal)
        if (not row or row['generation']!=generation or row['policy_revision']!=self.provider_policy_revision(provider)
                or not self.provider_available(provider) or (connected and row['status']!='connected')):
            raise ConnectionError('Open the current connection before reading its tools.')
        return row

    def _update(self,provider,principal,generation,**changes):
        assert set(changes)<={'status','tokens','client_info','expires_at','tools','oauth_scopes','inventory_sha256'}
        for field in ('tokens','client_info'):
            if field in changes:
                try:changes[field]=self._vault.seal(changes[field],principal=principal,provider=provider,generation=generation,field=field)
                except CredentialError as exc:raise ConnectionError(str(exc)) from None
        with self._db() as db:
            fields=','.join(f'{k}=?' for k in changes)
            cursor=db.execute(f'UPDATE connections_v2 SET {fields},updated_at=? WHERE principal=? AND provider=? AND generation=?',
                (*changes.values(),_now(),principal,provider,generation))
            if cursor.rowcount!=1: raise ConnectionError('Continue with the current connection.')

    def _begin(self,provider,principal,access_mode='read'):
        config=self._provider(provider);_identity(principal)
        try:requested=selected_scopes(config,access_mode)
        except ValueError as exc:raise ConnectionError(str(exc)) from None
        with self._db() as db:
            if not self._configuration_current(db,provider):
                raise ConnectionError('Complete this provider’s registered client setup before connecting.')
            db.execute('''INSERT INTO connections_v2(principal,provider,generation,status,updated_at,access_mode,requested_scopes,oauth_scopes,policy_revision)
              VALUES(?,?,1,'connecting',?,?,?,'[]',?) ON CONFLICT(principal,provider) DO UPDATE SET
              generation=generation+1,status='connecting',tokens=NULL,client_info=NULL,expires_at=NULL,tools='[]',
              updated_at=excluded.updated_at,access_mode=excluded.access_mode,requested_scopes=excluded.requested_scopes,
              oauth_scopes='[]',policy_revision=excluded.policy_revision,inventory_sha256=NULL''',
              (principal,provider,_now(),access_mode,json.dumps(requested),self.provider_policy_revision(provider)))
            generation=db.execute('SELECT generation FROM connections_v2 WHERE principal=? AND provider=?',(principal,provider)).fetchone()[0]
            client=self._client(provider)
            if client:
                sealed=self._vault.seal(client.client_info(requested).model_dump_json(),principal=principal,
                                        provider=provider,generation=generation,field='client_info')
                db.execute('UPDATE connections_v2 SET client_info=? WHERE principal=? AND provider=?',(sealed,principal,provider))
            return generation

    def _invalidate(self,provider,principal,generation):
        with self._db()as db:
            db.execute('''UPDATE connections_v2 SET generation=generation+1,status='reconnect_required',
              tokens=NULL,client_info=NULL,expires_at=NULL,oauth_scopes='[]',tools='[]',inventory_sha256=NULL,updated_at=?
              WHERE principal=? AND provider=? AND generation=?''',(_now(),principal,provider,generation))

    def _flow_state(self,flow,status):
        with self._db() as db:
            db.execute('UPDATE flows SET status=?,updated_at=? WHERE id=?',(status,_now(),flow.id))
        flow.ready.set()

    def catalogue(self,principal_id):
        _identity(principal_id)
        connections=[]
        for provider,config in self._policies.items():
            row=self._record(provider,principal_id)
            available=self.provider_available(provider)
            current=row is not None and row['policy_revision']==self.provider_policy_revision(provider)
            connections.append({'id':provider,'provider':provider,'name':config['name'],
                'description':config['description'],'authentication':config['auth'],
                'status':(row['status'] if current else 'reconnect_required' if row else 'disconnected') if available else 'setup_required',
                'available':available,'read_only':row is None or row['access_mode']=='read' or provider_read_only(config,row),
                'access_mode':row['access_mode'] if row else 'read','access_modes':list(config['scopes_by_mode']),
                'oauth_scopes':json.loads(row['oauth_scopes']) if row else [],
                'requested_scopes':json.loads(row['requested_scopes']) if row else list(selected_scopes(config,'read')),
                'scope_summary':('Read the content you authorize.' if row is None or row['access_mode']=='read' else 'View provider actions and request approval for changes.'),
                'write_available':available and provider in self._write_providers,'policy_revision':self.provider_policy_revision(provider),
                **({'registration':'static','setup_reason':None if available else 'registered_client_required',
                    'native_direct':config.get('native_direct',False),'developer':config.get('developer',config['name']),'category':config.get('category','Apps')}
                   if config.get('registration')=='static' else {}),
                **({'catalogue_binding':config['catalogue_binding'],'setup_reason':None if available else ('registered_client_required'if config.get('registration')=='static' else 'provider_commissioning_required')}if 'catalogue_binding'in config else {}),
                'reviewed_tools':sorted(config['tools']),'available_tools':json.loads(row['tools']) if row else [],
                'reference':config['reference'],
                'updated_at':row['updated_at'] if row else None})
        return {'company_scope':self.company_scope,'connections':connections}

    def read_generation(self,provider,*,principal_id):
        """Host-only connection identity, excluding OAuth material."""
        self._provider(provider);_identity(principal_id)
        with self._db() as db:
            current=self._configuration_current(db,provider)
            row=db.execute('SELECT generation,status,policy_revision FROM connections_v2 WHERE provider=? AND principal=?',
                           (provider,principal_id)).fetchone()
        if not current or not row or row['status']!='connected' or row['policy_revision']!=self.provider_policy_revision(provider):
            raise ConnectionError('Connect this provider in settings first.')
        return row['generation']

    def capabilities(self,principal_id):
        """Host-only capability summary from the last successful tool discovery."""
        result=[]
        for connection in self.catalogue(principal_id)['connections']:
            names=connection['available_tools']
            if connection['status']!='connected' or not names:continue
            scopes=[]
            if connection['provider']=='openai-docs':scopes=['read:docs']
            elif {'notion-search','notion-fetch'}.intersection(names):scopes=['read:pages']
            if scopes:result.append({'provider':connection['provider'],'scopes':scopes,'tools':names})
        return result

    async def start(self,provider,*,principal_id,session_binding,access_mode='read'):
        if self._provider(provider)['auth']=='bearer':
            raise ConnectionError('Enter the account credential through its private setup form.')
        return await self._start(provider,principal_id=principal_id,session_binding=session_binding,access_mode=access_mode)

    async def connect_bearer(self,provider,credential,*,principal_id,session_binding,access_mode='read'):
        # Host-only private handoff; never register as an agent tool or echo input.
        if self._provider(provider)['auth']!='bearer' or not isinstance(credential,BearerCredential):
            raise ConnectionError('Use this provider’s private credential setup.')
        return await self._start(provider,principal_id=principal_id,session_binding=session_binding,
                                 access_mode=access_mode,credential=credential)

    async def _start(self,provider,*,principal_id,session_binding,access_mode='read',credential=None):
        if self._closed: raise ConnectionError('Open the connection after the service restarts.')
        config=self._provider(provider);_identity(principal_id);binding=_binding(session_binding)
        if not self.provider_available(provider):raise ConnectionError('Complete this provider’s setup before connecting.')
        try:selected_scopes(config,access_mode)
        except ValueError as exc:raise ConnectionError(str(exc)) from None
        if config['auth']=='oauth' and not self.public_origin:
            raise ConnectionError('Configure the public HTTPS callback before connecting this account.')
        # Bound retained in-memory login handles. Completed records remain auditable on disk.
        self._flows={k:f for k,f in self._flows.items() if not f.task or not f.task.done()}
        pending=[f for f in self._flows.values() if f.task and not f.task.done()]
        if len(pending)>=16 or sum(f.principal==principal_id for f in pending)>=4:
            raise ConnectionError('Finish the open connection before starting another.')
        await self._cancel_flows(provider,principal_id)
        async with self._scope_fence(provider,principal_id):
            row=self._record(provider,principal_id)
            if row and row['status']=='disconnecting':raise ConnectionError('Finish closing the current connection first.')
            generation=self._begin(provider,principal_id,access_mode)
            if credential is not None:
                self._update(provider,principal_id,generation,
                    tokens=OAuthToken(access_token=credential.token,token_type='Bearer').model_dump_json(),
                    expires_at=credential.expires_at)
        flow=_Flow('mcp-'+secrets.token_hex(16),provider,principal_id,binding,generation,
                   time.time()+self.flow_timeout,asyncio.get_running_loop().create_future())
        self._flows[flow.id]=flow
        with self._db() as db:
            db.execute('INSERT INTO flows VALUES(?,?,?,?,?,?,?,?)',
                (flow.id,principal_id,provider,binding,'connecting',flow.expires_at,_now(),_now()))
        flow.task=asyncio.create_task(self._connect(flow))
        try:await asyncio.wait_for(flow.ready.wait(),10)
        except TimeoutError:pass
        return self.flow_status(flow.id,principal_id=principal_id,session_binding=session_binding)

    async def _connect(self,flow):
        try:
            async with asyncio.timeout(self.flow_timeout):
                async with self._session(flow.provider,flow.principal,flow.generation,flow) as session:
                    await session.initialize()
                    available=await self._listed(session,flow.provider)
                    row=self._check(flow.provider,flow.principal,flow.generation)
                    if not visible_tools(self._provider(flow.provider),row,available):
                        raise ConnectionError('Choose access that includes this provider’s available tools.')
                self._update(flow.provider,flow.principal,flow.generation,status='connected',tools=json.dumps([t['name'] for t in visible_tools(self._provider(flow.provider),row,available)]),inventory_sha256=digest(available))
                self._flow_state(flow,'connected')
        except asyncio.CancelledError:
            self._flow_state(flow,'interrupted')
            raise
        except TimeoutError:
            self._failed(flow,'expired')
        except Exception:
            self._failed(flow,'failed')
        finally:
            flow.authorization_url=None;flow.state_hash=None
            if not flow.callback.done():flow.callback.cancel()
            flow.ready.set()

    def _failed(self,flow,status):
        try:self._update(flow.provider,flow.principal,flow.generation,status='reconnect_required',tokens=None,client_info=None,expires_at=None)
        except ConnectionError:pass
        self._flow_state(flow,status)

    async def _redirect(self,flow,url):
        config=self._provider(flow.provider);validate_provider_url(url,config['hosts'])
        parts=urlsplit(url)
        if config.get('registration')=='static' or config.get('remote_descriptor'):
            if urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))!=config['authorization_endpoint']:
                raise ConnectionError('Review the registered provider authorization endpoint.')
            params=parse_qs(parts.query)
            info=json.loads(self._check(flow.provider,flow.principal,flow.generation)['client_info'])
            if (params.get('client_id')!=[info['client_id']] or params.get('redirect_uri')!=[self.public_origin+CALLBACK_PATH]
                    or 'client_secret'in params or 'access_token'in params or 'refresh_token'in params):
                raise ConnectionError('Review the registered provider authorization request.')
            if config.get('native_direct') and config.get('issuer')=='https://accounts.google.com':
                params.update(access_type=['offline'],prompt=['consent'],include_granted_scopes=['false'])
            url=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(params,doseq=True),''))
        params=parse_qs(urlsplit(url).query)
        if (len(params.get('state',[]))!=1 or not params['state'][0]
                or params.get('code_challenge_method')!=['S256']):
            raise ConnectionError('The provider requires a protected authorization flow.')
        self._check(flow.provider,flow.principal,flow.generation)
        flow.state_hash=_hash(params['state'][0]);flow.authorization_url=url
        self._flow_state(flow,'awaiting_authorization')

    def flow_status(self,flow_id,*,principal_id,session_binding):
        _identity(principal_id);binding=_binding(session_binding)
        with self._db() as db:
            row=db.execute('SELECT * FROM flows WHERE id=? AND principal=? AND binding=?',(flow_id,principal_id,binding)).fetchone()
        if not row:raise ConnectionError('Continue from the session that opened this connection.')
        result={'flow_id':row['id'],'provider':row['provider'],'status':row['status'],'expires_at':row['expires_at']}
        flow=self._flows.get(flow_id)
        if flow and flow.authorization_url and row['status']=='awaiting_authorization':
            result['authorization_url']=flow.authorization_url
        return result

    async def oauth_callback(self,*,state,code=None,principal_id,session_binding,error=None):
        binding=_binding(session_binding);_identity(principal_id)
        if not isinstance(state,str) or not 1<=len(state)<=512:
            raise ConnectionError('Restart the connection from its settings.')
        flow=next((f for f in self._flows.values() if f.state_hash and hmac.compare_digest(f.state_hash,_hash(state))),None)
        if (not flow or flow.principal!=principal_id or not hmac.compare_digest(flow.binding,binding)
                or flow.consumed or flow.expires_at<=time.time() or flow.callback.done()
                or not flow.task or flow.task.done()):
            raise ConnectionError('Restart the connection from its settings.')
        self._check(flow.provider,flow.principal,flow.generation)
        if error:
            flow.consumed=True;flow.task.cancel()
            await asyncio.gather(flow.task,return_exceptions=True)
            self._failed(flow,'declined')
        else:
            if not isinstance(code,str) or not 1<=len(code)<=8192:
                raise ConnectionError('Complete the provider authorization step.')
            flow.consumed=True;flow.authorization_url=None
            self._flow_state(flow,'completing');flow.callback.set_result((code,state))
            await asyncio.wait({flow.task},timeout=10)
        return self.flow_status(flow.id,principal_id=principal_id,session_binding=session_binding)

    @asynccontextmanager
    async def _session(self,provider,principal,generation,flow=None):
        config=self._provider(provider);auth=None
        row=self._check(provider,principal,generation)
        requested=tuple(json.loads(row['requested_scopes']))
        if config['auth']=='oauth':
            if config.get('token_endpoint'):validate_provider_url(config['token_endpoint'],config['hosts'])
            storage=_Storage(self,principal,provider,generation)
            async def redirect(url):
                if flow is None:raise ConnectionError('Reconnect this provider in settings.')
                await self._redirect(flow,url)
            async def callback():
                if flow is None:raise ConnectionError('Reconnect this provider in settings.')
                return await flow.callback
            auth=_OAuth(config['endpoint'],client_metadata=OAuthClientMetadata(
                client_name='Rocoto',redirect_uris=[self.public_origin+CALLBACK_PATH],
                token_endpoint_auth_method=config['token_auth_method'],grant_types=['authorization_code','refresh_token'],
                response_types=['code'],scope=' '.join(requested) or None),storage=storage,
                redirect_handler=redirect,callback_handler=callback,timeout=self.flow_timeout,
                requested_scopes=requested,token_auth_method=config['token_auth_method'],allow_authorization=flow is not None,
                refresh_token_endpoint=config.get('token_endpoint'),
                registered_policy=config if config.get('registration')=='static' or config.get('remote_descriptor') else None,
                current=lambda:self._check(provider,principal,generation))
        if config['auth']=='bearer':
            if (not row['tokens'] or row['expires_at']is not None and row['expires_at']<=time.time()+45):
                raise ConnectionError('Renew this provider’s private credential.')
            auth=_Bearer(OAuthToken.model_validate_json(row['tokens']).access_token,config['endpoint'],
                         lambda:self._check(provider,principal,generation))
        async with httpx.AsyncClient(auth=auth,transport=_GuardedTransport(config['hosts']),
                timeout=httpx.Timeout(30),follow_redirects=False,trust_env=False) as client:
            async with streamable_http_client(config['endpoint'],http_client=client) as (read,write,_):
                async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=30)) as session:
                    yield session

    async def _listed(self,session,provider):
        result=[];cursor=None;seen=set();names=set();total=0
        for _ in range(8):
            page=await session.list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name in names or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}',tool.name):
                    raise ConnectionError('Review the provider tool identity.')
                names.add(tool.name)
                data=tool.model_dump(mode='json',by_alias=True,exclude_none=True)
                size=len(json.dumps(data,allow_nan=False).encode());total+=size
                if size>65536 or total>MAX_INVENTORY_BYTES or len(names)>MAX_TOOLS:
                    raise ConnectionError('Review the bounded provider tool catalogue.')
                def review(value,depth=0):
                    if depth>24:raise ConnectionError('Review the provider tool schema before connecting.')
                    if isinstance(value,dict):
                        for keyword in ('$ref','$dynamicRef','$recursiveRef'):
                            if keyword in value and not str(value[keyword]).startswith('#'):
                                raise ConnectionError('Review the provider tool schema before connecting.')
                        for child in value.values():review(child,depth+1)
                    elif isinstance(value,list):
                        for child in value:review(child,depth+1)
                review(data.get('inputSchema',{}));review(data.get('outputSchema',{}))
                result.append(data)
            cursor=page.nextCursor
            if not cursor:break
            if cursor in seen:raise ConnectionError('Try the provider connection again.')
            seen.add(cursor)
        else:raise ConnectionError('Review the provider tool catalogue before connecting.')
        return result

    @asynccontextmanager
    async def _scope_fence(self,provider,principal):
        self._provider(provider);_identity(principal)
        key=(provider,principal);lock=self._locks.setdefault(key,asyncio.Lock())
        async with asyncio.timeout(50):
            async with lock:
                path=self.directory/('.admission-'+_hash(provider+'\0'+principal)+'.lock')
                fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
                try:
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode)or info.st_uid!=os.geteuid()or info.st_mode&0o077:
                        raise ConnectionError('Use the private provider admission fence.')
                    # Nonblocking flock permits cross-process exclusion without
                    # a worker thread/host loop blocked behind a network request.
                    while True:
                        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                        except BlockingIOError:await asyncio.sleep(.02)
                    yield
                finally:os.close(fd)

    @asynccontextmanager
    async def admission(self,provider,*,principal_id,expected_generation):
        """Host-only dispatch fence; no OAuth, effects, retry, or approval claim.

        Disconnect first persists a generation fence, then drains this lease.
        A caller which already claimed an effect owns its bounded receipt/cleanup.
        """
        if self._closed:raise ConnectionError('Open the connection after the service restarts.')
        self._check(provider,principal_id,expected_generation,connected=True)
        async with self._scope_fence(provider,principal_id):
            row=self._check(provider,principal_id,expected_generation,connected=True)
            if not self.provider_available(provider):raise ConnectionError('Complete this provider’s setup before connecting.')
            token=self._admission.set((asyncio.current_task(),provider,principal_id,expected_generation))
            try:yield row
            finally:self._admission.reset(token)

    @asynccontextmanager
    async def dispatch_admission(self,provider,*,principal_id,expected_generation):
        """Shared provider capacity plus the current-account dispatch fence."""
        async with asyncio.timeout(50):
            async with self._slots,self.admission(provider,principal_id=principal_id,expected_generation=expected_generation)as row:
                yield row

    def dispatch_credentials(self,provider,*,principal_id,expected_generation,minimum_validity_seconds=45):
        """Fixed credential snapshot only inside admission; never starts OAuth."""
        if self._admission.get()!=(asyncio.current_task(),provider,principal_id,expected_generation):
            raise ConnectionError('Admit the current provider action before reading its credential.')
        if not isinstance(minimum_validity_seconds,(int,float)) or isinstance(minimum_validity_seconds,bool) or not 0<=minimum_validity_seconds<=120:
            raise ConnectionError('Use a bounded provider dispatch duration.')
        row=self._check(provider,principal_id,expected_generation,connected=True);config=self._provider(provider)
        token=OAuthToken.model_validate_json(row['tokens'])if row['tokens']else None
        if config['auth']in {'oauth','bearer'} and (token is None or row['expires_at'] is not None and row['expires_at']<=time.time()+minimum_validity_seconds):
            raise ConnectionError('Refresh provider access before requesting approval.')
        return DispatchCredentials(provider,config['endpoint'],expected_generation,row['access_mode'],
            tuple(json.loads(row['oauth_scopes'])),row['policy_revision'],row['inventory_sha256'],row['expires_at'],
            token.access_token if token else None)

    @asynccontextmanager
    async def _operation(self,provider,principal,*,expected_generation=None):
        _identity(principal);self._provider(provider)
        row=self._record(provider,principal)
        if not row or row['status']!='connected':raise ConnectionError('Connect this provider in settings first.')
        generation=row['generation']
        if expected_generation is not None and generation!=expected_generation:
            raise ConnectionError('Open the current connection before reading its tools.')
        task=asyncio.current_task();self._active.add(task);self._active_bindings[task]=(provider,principal)
        try:
            async with asyncio.timeout(45):
                async with self._slots,self.admission(provider,principal_id=principal,expected_generation=generation):
                    async with self._session(provider,principal,generation) as session:
                        await session.initialize()
                        yield session,generation
                        self._check(provider,principal,generation,connected=True)
        except asyncio.CancelledError:
            if task in self._revoked_reads:raise ConnectionError('Open the current provider connection before continuing.') from None
            raise
        except ConnectionError:raise
        except Exception as exc:
            def public_error(error):
                if isinstance(error,ConnectionError):return error
                if isinstance(error,BaseExceptionGroup):
                    for child in error.exceptions:
                        found=public_error(child)
                        if found:return found
                return None
            raise public_error(exc) or ConnectionError('Reconnect this provider or request a smaller result.') from None
        finally:self._active.discard(task);self._active_bindings.pop(task,None);self._revoked_reads.discard(task)

    async def tools(self,provider,*,principal_id,expected_generation=None):
        async with self._operation(provider,principal_id,expected_generation=expected_generation) as (session,generation):
            inventory=await self._listed(session,provider)
            row=self._check(provider,principal_id,generation,connected=True);config=self._provider(provider)
            available=visible_tools(config,row,inventory)
            self._update(provider,principal_id,generation,tools=json.dumps([t['name']for t in available]),inventory_sha256=digest(inventory))
            available=self._redact_credentials(available,provider,principal_id,generation)
            result={'provider':provider,'read_only':row['access_mode']=='read' or provider_read_only(config,row),
                'access_mode':row['access_mode'],'oauth_scopes':json.loads(row['oauth_scopes']),
                'policy_revision':row['policy_revision'],'inventory_sha256':digest(inventory),
                'tools':available,'tool_permissions':{t['name']:'read' if read_authorized(config,row,t['name'])else'approval_required'for t in available},
                'authority':'source_data'}
        return result

    def _write_enabled(self,provider,config,row):
        required=set(config['scopes_by_mode'].get('read_write',()))
        return bool(provider in self._write_providers and row['status']=='connected' and
            row['access_mode']=='read_write' and (required or config.get('remote_descriptor')) and
            set(json.loads(row['requested_scopes']))==required and
            set(json.loads(row['oauth_scopes']))==required)

    async def execution_binding(self,provider,tool,*,principal_id,expected_generation=None):
        async with self._operation(provider,principal_id,expected_generation=expected_generation)as(session,generation):
            inventory=await self._listed(session,provider)
            row=self._check(provider,principal_id,generation,connected=True);config=self._provider(provider)
            schema=next((t for t in visible_tools(config,row,inventory)if t['name']==tool),None)
            if schema is None:raise ConnectionError('Choose a tool from this connection’s current inventory.')
            self._update(provider,principal_id,generation,inventory_sha256=digest(inventory))
            schema=self._redact_credentials(schema,provider,principal_id,generation)
            result={'provider':provider,'tool':tool,'endpoint':config['endpoint'],'connection_generation':generation,
                'schema_sha256':digest(schema),'tool_contract':schema,'access_mode':row['access_mode'],'oauth_scopes':tuple(json.loads(row['oauth_scopes'])),
                'policy_revision':row['policy_revision'],'write_enabled':self._write_enabled(provider,config,row),'read_authorized':read_authorized(config,row,tool)}
        return result

    def _redact_credentials(self,value,provider,principal,generation):
        row=self._check(provider,principal,generation,connected=True)
        secrets_to_remove=[]
        for column,keys in [('tokens',('access_token','refresh_token')),('client_info',('client_secret',))]:
            data=json.loads(row[column]) if row[column] else {}
            secrets_to_remove.extend(data[k] for k in keys if isinstance(data.get(k),str) and data[k])
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False)
        if len(raw.encode())>MAX_RESULT_BYTES:raise ConnectionError('Request a smaller provider result.')
        for secret in secrets_to_remove:
            raw=raw.replace(json.dumps(secret,ensure_ascii=False)[1:-1],'[private credential]')
        return json.loads(raw)

    async def call(self,provider,tool,arguments,*,principal_id,expected_generation=None):
        config=self._provider(provider)
        if not isinstance(arguments,dict) or len(json.dumps(arguments,allow_nan=False).encode())>MAX_ARGUMENT_BYTES:
            raise ConnectionError('Use a bounded object of tool arguments.')
        async with self._operation(provider,principal_id,expected_generation=expected_generation) as (session,generation):
            schemas=await self._listed(session,provider)
            schema=next((t for t in schemas if t['name']==tool),None)
            if schema is None:raise ConnectionError('Choose a tool from this connection’s current inventory.')
            row=self._check(provider,principal_id,generation,connected=True)
            if not read_authorized(config,row,tool):
                raise ConnectionError('Native approval is required for this provider action.')
            def deny_schema_io(uri):
                raise NoSuchResource(ref=uri)
            try:
                validator=jsonschema.validators.validator_for(schema['inputSchema'])
                validator.check_schema(schema['inputSchema'])
                validator(schema['inputSchema'],registry=Registry(retrieve=deny_schema_io)).validate(arguments)
            except (jsonschema.ValidationError,jsonschema.SchemaError):
                raise ConnectionError('Match the provider’s current tool input schema.') from None
            result=await session.call_tool(tool,arguments)
            data=self._redact_credentials(result.model_dump(mode='json',by_alias=True,exclude_none=True),provider,principal_id,generation)
            response={'provider':provider,'tool':tool,'read_only':True,'authority':'untrusted_source_data',
                      'retrieved_at':_now(),'result':data}
        return response

    async def _cancel_flows(self,provider,principal):
        tasks=[f.task for f in self._flows.values() if f.provider==provider and f.principal==principal and f.task and not f.task.done()]
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

    async def disconnect(self,provider,*,principal_id):
        self._provider(provider);_identity(principal_id)
        # Immediately close admission before waiting for any accepted effect.
        # Existing fixed-token write transports are not in _active: they retain
        # their receipt/uncertainty and drain independently under admission.
        with self._db() as db:
            db.execute('''UPDATE connections_v2 SET generation=generation+1,status='disconnecting',tokens=NULL,
                client_info=NULL,expires_at=NULL,oauth_scopes='[]',tools='[]',inventory_sha256=NULL,updated_at=?
                WHERE principal=? AND provider=?''',(_now(),principal_id,provider))
            revoking=db.execute('SELECT generation FROM connections_v2 WHERE principal=? AND provider=?',(principal_id,provider)).fetchone()
            revoking_generation=revoking[0] if revoking else None
        async def finish():
            await self._cancel_flows(provider,principal_id)
            tasks=[task for task,scope in self._active_bindings.items()if scope==(provider,principal_id)and task is not asyncio.current_task()]
            for task in tasks:self._revoked_reads.add(task);task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            async with self._scope_fence(provider,principal_id):
                with self._db()as db:
                    db.execute("UPDATE connections_v2 SET status='disconnected',updated_at=? WHERE principal=? AND provider=? AND generation=? AND status='disconnecting'",(_now(),principal_id,provider,revoking_generation))
        task=asyncio.create_task(finish())
        try:await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:await asyncio.shield(task)
                except asyncio.CancelledError:continue
                except Exception:break
            if not task.cancelled():task.exception()
            raise
        return {'provider':provider,'status':'disconnected','remote_grant':'Manage the provider grant in its account settings.'}

    async def close(self):
        self._closed=True
        tasks={f.task for f in self._flows.values() if f.task and not f.task.done()}
        tasks.update(t for t in self._active if t and t is not asyncio.current_task())
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self._flows.clear()
