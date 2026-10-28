"""Durable host-issued MCP read grants. Tokens are returned once and stored hashed."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import uuid


class GrantError(ValueError):pass


@dataclass(frozen=True)
class ReadGrant:
    id:str
    company_id:str
    owner_principal_id:str
    dataset:str
    label:str
    created_at:float
    expires_at:float
    revoked_at:float|None=None
    permission:str='video:read'

    def public(self):return dict(vars(self))


class MCPGrantStore:
    MAX_LIFETIME=30*86400
    MAX_ACTIVE_PER_OWNER=16
    MAX_RECORDS=10000

    def __init__(self,state,*,clock=time.time):
        self.clock=clock;state=Path(state).absolute();self.root=state/'mcp-access'
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():raise GrantError('Use private MCP access storage.')
        self.root.chmod(0o700);self.path=self.root/'grants.sqlite3'
        fd=os.open(self.path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():raise GrantError('Use private MCP access storage.')
            os.fchmod(fd,0o600)
        finally:os.close(fd)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY,digest TEXT UNIQUE NOT NULL,
                company_id TEXT NOT NULL,owner_principal_id TEXT NOT NULL,dataset TEXT NOT NULL,label TEXT NOT NULL,
                created_at REAL NOT NULL,expires_at REAL NOT NULL,revoked_at REAL)''')

    @contextmanager
    def _db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA max_page_count=16384');yield db;db.commit()
        finally:db.close()

    @staticmethod
    def _scope(company_id,owner_principal_id,dataset):
        if (not isinstance(company_id,str) or not re.fullmatch(r'[a-f0-9]{64}',company_id)
                or not isinstance(owner_principal_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}',owner_principal_id)
                or not isinstance(dataset,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',dataset)):
            raise GrantError('Choose the authenticated account and collection.')

    @staticmethod
    def _row(row):return ReadGrant(**{key:row[key] for key in ReadGrant.__dataclass_fields__ if key in row.keys()})

    def issue(self,*,company_id,owner_principal_id,dataset,label='MCP connection',ttl_seconds=7*86400):
        """Host-only: authenticate and authorize the requested scope before calling."""
        self._scope(company_id,owner_principal_id,dataset)
        if not isinstance(label,str) or not label.strip() or len(label)>80 or any(ord(c)<32 for c in label):raise GrantError('Name the MCP connection in up to 80 characters.')
        if type(ttl_seconds) not in {int,float} or not math.isfinite(ttl_seconds) or not 60<=ttl_seconds<=self.MAX_LIFETIME:raise GrantError('Choose an expiry within 30 days.')
        now=self.clock();token='rmcp_'+secrets.token_urlsafe(48);key='mcp-key-'+uuid.uuid4().hex
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM grants').fetchone()[0]>=self.MAX_RECORDS:raise GrantError('Review retained MCP access keys before creating another.')
            count=db.execute('SELECT COUNT(*) FROM grants WHERE company_id=? AND owner_principal_id=? AND revoked_at IS NULL AND expires_at>?',(company_id,owner_principal_id,now)).fetchone()[0]
            if count>=self.MAX_ACTIVE_PER_OWNER:raise GrantError('Revoke an existing MCP key before creating another.')
            db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,NULL)',(key,hashlib.sha256(token.encode()).hexdigest(),company_id,owner_principal_id,dataset,label.strip(),now,now+ttl_seconds))
        return self.get(key,company_id=company_id,owner_principal_id=owner_principal_id),token

    def list(self,*,company_id,owner_principal_id):
        self._scope(company_id,owner_principal_id,'company')
        with self._db() as db:return [self._row(row) for row in db.execute('SELECT * FROM grants WHERE company_id=? AND owner_principal_id=? ORDER BY created_at DESC',(company_id,owner_principal_id))]

    def get(self,key,*,company_id,owner_principal_id):
        self._scope(company_id,owner_principal_id,'company')
        with self._db() as db:row=db.execute('SELECT * FROM grants WHERE id=? AND company_id=? AND owner_principal_id=?',(key,company_id,owner_principal_id)).fetchone()
        if row is None:raise GrantError('Choose an MCP key belonging to this account.')
        return self._row(row)

    def revoke(self,key,*,company_id,owner_principal_id):
        self._scope(company_id,owner_principal_id,'company')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM grants WHERE id=? AND company_id=? AND owner_principal_id=?',(key,company_id,owner_principal_id)).fetchone()
            if row is None:raise GrantError('Choose an MCP key belonging to this account.')
            db.execute('UPDATE grants SET revoked_at=COALESCE(revoked_at,?) WHERE id=?',(self.clock(),key))
        return self.get(key,company_id=company_id,owner_principal_id=owner_principal_id)

    def authenticate(self,token):
        if not isinstance(token,str) or not re.fullmatch(r'rmcp_[A-Za-z0-9_-]{64}',token):return None
        with self._db() as db:row=db.execute('SELECT * FROM grants WHERE digest=? AND revoked_at IS NULL AND expires_at>?',(hashlib.sha256(token.encode()).hexdigest(),self.clock())).fetchone()
        return self._row(row) if row else None

    def active(self,key):
        with self._db() as db:row=db.execute('SELECT * FROM grants WHERE id=? AND revoked_at IS NULL AND expires_at>?',(key,self.clock())).fetchone()
        return self._row(row) if row else None
