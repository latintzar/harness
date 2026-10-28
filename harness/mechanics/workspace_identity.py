"""Private, portable company identity shared by stores and provider pools.

First adoption preserves the legacy path-derived scope and onboarding seed.
Copy this file with the complete workspace during a host move. A copy represents
the same company; a new company starts explicitly in a fresh empty directory.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

IDENTITY_FILE='.workspace-identity.json'
_LOCK_FILE='.workspace-identity.lock'
_FIELDS={'schema_version','company_scope','onboarding_seed','origin','created_at'}


def _private_regular(descriptor):
    info=os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise ValueError('Use the private workspace identity owned by this runtime.')


@contextmanager
def _locked(state):
    state=Path(state).resolve()
    state.mkdir(mode=0o700,parents=True,exist_ok=True)
    descriptor=None
    try:
        descriptor=os.open(state/_LOCK_FILE,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        _private_regular(descriptor)
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        yield state
    except OSError:
        raise ValueError('Open the complete private workspace identity before using its records.') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read(path):
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError('Use the original private workspace identity file.') from None
    try:
        _private_regular(descriptor)
        if os.fstat(descriptor).st_size>8192:
            raise ValueError('Restore the original workspace identity.')
        with os.fdopen(descriptor,'r',encoding='utf-8',closefd=False) as stream:
            record=json.load(stream)
        if (not isinstance(record,dict) or set(record)!=_FIELDS
                or type(record['schema_version']) is not int or record['schema_version']!=1
                or not isinstance(record['company_scope'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',record['company_scope'])
                or not isinstance(record['onboarding_seed'],str)
                or not 1<=len(record['onboarding_seed'])<=4096
                or '\0' in record['onboarding_seed']
                or record['origin'] not in {'legacy_path_adoption','new_company'}
                or not isinstance(record['created_at'],str)
                or datetime.fromisoformat(record['created_at']).utcoffset() is None):
            raise ValueError('Restore the original workspace identity.')
        if record['origin']=='legacy_path_adoption':
            expected=hashlib.sha256(record['onboarding_seed'].encode()).hexdigest()
            if expected!=record['company_scope']:
                raise ValueError('Restore the original workspace identity.')
        elif record['onboarding_seed']!='workspace-'+record['company_scope']:
            raise ValueError('Restore the original workspace identity.')
        return record
    except (json.JSONDecodeError,UnicodeError,TypeError,OverflowError):
        raise ValueError('Restore the original workspace identity.') from None
    finally:
        os.close(descriptor)


def _write(state,record):
    descriptor,name=tempfile.mkstemp(prefix='.workspace-identity-',suffix='.tmp',dir=state)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump(record,stream,sort_keys=True,allow_nan=False)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,state/IDENTITY_FILE)
        directory=os.open(state,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def workspace_identity(state):
    """Adopt once at the original location; retain that identity after migration."""
    with _locked(state) as directory:
        record=_read(directory/IDENTITY_FILE)
        if record is None:
            seed=str(directory)
            record={'schema_version':1,'company_scope':hashlib.sha256(seed.encode()).hexdigest(),
                'onboarding_seed':seed,'origin':'legacy_path_adoption',
                'created_at':datetime.now(timezone.utc).isoformat()}
            _write(directory,record)
        return record


def workspace_scope(state):
    return workspace_identity(state)['company_scope']


def onboarding_identity(state,collection):
    if not isinstance(collection,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',collection):
        raise ValueError('Choose the collection belonging to this workspace.')
    seed=workspace_identity(state)['onboarding_seed']
    return hashlib.sha256((seed+'\0'+collection).encode()).hexdigest()[:24]


def initialize_new_workspace(state):
    """Host-only fresh-company creation; copied history must retain its identity."""
    with _locked(state) as directory:
        if any(item.name!=_LOCK_FILE for item in directory.iterdir()):
            raise ValueError('Create a new company in a fresh empty workspace.')
        scope=hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        record={'schema_version':1,'company_scope':scope,'onboarding_seed':'workspace-'+scope,
            'origin':'new_company','created_at':datetime.now(timezone.utc).isoformat()}
        _write(directory,record)
        return record
