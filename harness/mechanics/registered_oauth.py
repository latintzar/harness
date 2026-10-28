"""Private host registrations for reviewed providers, never customer token import.

Load during host startup, off the request loop. Rotation uses a strictly larger
revision and a stopped old runtime; the connection store also fences stale peers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from mcp.shared.auth import OAuthClientInformationFull

CALLBACK_PATH = '/api/connections/oauth/callback'
GOOGLE_ISSUER = 'https://accounts.google.com'
GOOGLE_AUTHORIZATION = GOOGLE_ISSUER + '/o/oauth2/v2/auth'
GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token'
GOOGLE_PROVIDERS = frozenset({'google-drive', 'google-docs'})
from catalogue_providers import remote_entries
PROVIDERS = GOOGLE_PROVIDERS | frozenset(remote_entries())
MAX_CONFIG_BYTES = 32 * 1024


class RegistrationError(ValueError):
    pass


def _fail():
    raise RegistrationError('Review the private registered OAuth client configuration.') from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: _fail()
        result[key] = value
    return result


def _callback(origin):
    try:
        url = urlsplit(origin)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.port not in (None, 443) or url.path not in ('', '/') or url.query or url.fragment):
            _fail()
    except (TypeError, ValueError): _fail()
    return origin.rstrip('/') + CALLBACK_PATH


def read_private_configuration(path, *, max_bytes=MAX_CONFIG_BYTES):
    try:
        path = Path(path).absolute()
        if path.resolve(strict=True) != path: _fail()
        parent = path.parent.stat()
        if (parent.st_uid not in (0, os.geteuid()) or parent.st_mode & 0o077
                or not stat.S_ISDIR(parent.st_mode)): _fail()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, os.geteuid())
                    or before.st_mode & 0o077 or before.st_nlink != 1
                    or not 1 <= before.st_size <= max_bytes): _fail()
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(max_bytes + 1)
            after = os.fstat(fd)
            current = path.lstat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if (identity(before) != identity(after) or identity(after) != identity(current)
                    or len(data) != before.st_size): _fail()
        finally: os.close(fd)
        raw = json.loads(data, object_pairs_hook=_object)
        return raw
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        _fail()


@dataclass(frozen=True)
class RegisteredClient:
    provider: str
    revision: int
    redirect_uri: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    issuer: str = GOOGLE_ISSUER
    token_auth_method: str = 'client_secret_post'

    def __post_init__(self):
        from catalogue_providers import _url
        if (self.provider not in PROVIDERS or type(self.revision) is not int or not 1 <= self.revision <= 2**31-1
                or not isinstance(self.client_id, str)
                or not 1<=len(self.client_id)<=512 or not all(0x21<=ord(c)<=0x7e for c in self.client_id)
                or not isinstance(self.client_secret, str) or len(self.client_secret)>4096
                or self.token_auth_method not in {'none','client_secret_post','client_secret_basic'}
                or (self.token_auth_method=='none')!= (self.client_secret=='')
                or not all(0x21 <= ord(c) <= 0x7e for c in self.client_secret)):
            _fail()
        try:_url(self.issuer)
        except ValueError:_fail()
        if self.provider in GOOGLE_PROVIDERS and (
                not re.fullmatch(r'[A-Za-z0-9_-]{1,256}\.apps\.googleusercontent\.com',self.client_id)
                or self.issuer!=GOOGLE_ISSUER or self.token_auth_method!='client_secret_post'):_fail()
        if not isinstance(self.redirect_uri, str) or not self.redirect_uri.endswith(CALLBACK_PATH): _fail()
        if _callback(self.redirect_uri[:-len(CALLBACK_PATH)]) != self.redirect_uri: _fail()

    @property
    def fingerprint(self):
        bound = [self.provider, self.revision, self.redirect_uri, self.client_id, self.client_secret]
        if self.provider not in GOOGLE_PROVIDERS:bound.extend([self.issuer,self.token_auth_method])
        return hashlib.sha256(json.dumps(bound, separators=(',', ':')).encode()).hexdigest()

    @property
    def policy_fingerprint(self):
        # Public approval identity includes the revision and public registration
        # contract, never a digest usable to guess a weak client secret.
        bound = [self.provider, self.revision, self.redirect_uri, self.client_id]
        if self.provider not in GOOGLE_PROVIDERS:bound.extend([self.issuer,self.token_auth_method])
        return hashlib.sha256(json.dumps(bound, separators=(',', ':')).encode()).hexdigest()

    def client_info(self, requested_scopes):
        return OAuthClientInformationFull(
            client_name='Rocoto', client_id=self.client_id, client_secret=self.client_secret or None,
            redirect_uris=[self.redirect_uri], token_endpoint_auth_method=self.token_auth_method,
            grant_types=['authorization_code', 'refresh_token'], response_types=['code'],
            scope=' '.join(requested_scopes), issuer=self.issuer)


@dataclass(frozen=True)
class RegisteredOAuthClients:
    company_scope: str
    redirect_uri: str
    clients: tuple[RegisteredClient, ...] = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.company_scope, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', self.company_scope)
                or not isinstance(self.clients, tuple) or len(self.clients) > len(PROVIDERS)
                or any(not isinstance(c, RegisteredClient) or c.redirect_uri != self.redirect_uri for c in self.clients)
                or len({c.provider for c in self.clients}) != len(self.clients)):
            _fail()
        if not isinstance(self.redirect_uri, str) or not self.redirect_uri.endswith(CALLBACK_PATH): _fail()
        if _callback(self.redirect_uri[:-len(CALLBACK_PATH)]) != self.redirect_uri: _fail()

    def get(self, provider):
        return next((c for c in self.clients if c.provider == provider), None)

    @classmethod
    def load(cls, path, *, company_scope, public_origin):
        """Read one bounded private file; token/grant fields are rejected."""
        try:
            raw = read_private_configuration(path)
            callback = _callback(public_origin)
            if (not isinstance(raw, dict) or set(raw) != {'schema_version', 'company_scope', 'redirect_uri', 'clients'}
                    or type(raw['schema_version']) is not int or raw['schema_version'] != 1
                    or raw['company_scope'] != company_scope or raw['redirect_uri'] != callback
                    or not isinstance(raw['clients'], dict) or set(raw['clients']) - PROVIDERS): _fail()
            clients = []
            for provider, item in raw['clients'].items():
                if not isinstance(item, dict) or set(item) != ({'revision','client_id','client_secret'} if provider in GOOGLE_PROVIDERS else {'revision','client_id','client_secret','issuer','token_auth_method'}): _fail()
                clients.append(RegisteredClient(provider, redirect_uri=callback, **item))
            return cls(company_scope, callback, tuple(clients))
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            _fail()
