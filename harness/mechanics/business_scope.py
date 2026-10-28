"""Current workspace membership and actor authority shared by every vertical.

Membership comes from the host's AccessStore. Constructing or reading this
object creates no state; business resource graphs belong to their vertical.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re

class ScopeError(ValueError): pass

def need(value, code='invalid_business_scope'):
    if not value: raise ScopeError(code)

def identifier(value):
    need(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}',value))
    return value

def label(value, maximum=200, empty=False):
    need(isinstance(value,str) and len(value)<=maximum and (empty or value.strip())
         and all(ord(c)>=32 for c in value))
    return value.strip()

def ids(value, maximum=500):
    need(isinstance(value,list) and len(value)<=maximum and all(isinstance(item,str) for item in value)
         and len(set(value))==len(value))
    return sorted(identifier(item) for item in value)

@dataclass(frozen=True)
class ScopeActor:
    company_id: str
    principal_id: str
    observer: bool = False
    def __post_init__(self):
        identifier(self.company_id); identifier(self.principal_id)
        need(type(self.observer)is bool)

class WorkspaceAuthority:
    def __init__(self,state,*,company_id,member,members=lambda:[]):
        self.state=Path(state).absolute()
        self.company_id=identifier(company_id)
        self.member=member
        self.members=members

    def authority(self,actor,*,write=False):
        need(isinstance(actor,ScopeActor) and actor.company_id==self.company_id,'scope_access_required')
        try: member=self.member(actor.principal_id)
        except Exception: member=None
        need(isinstance(member,dict) and member.get('role') in {'owner','operator','browse'},'scope_access_required')
        need(not write or not actor.observer and member['role'] in {'owner','operator'},'scope_read_only')
        return dict(member)


# Existing host integrations may use this name for shared membership authority.
BusinessScope = WorkspaceAuthority
