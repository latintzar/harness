"""Canonical company conversations, without adopting any person's history.

The host supplies the environment, membership authority and selected agent store.
Only canonical records created here acquire shared visibility. Enrollment chats
remain private to the member, even when setup starts from a company conversation.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time

import pack
from workspace_identity import workspace_scope


def __getattr__(name):
    # PURPOSES is {purpose: title} of the template this process serves (pack.py). A purpose is
    # part of each canonical conversation's id, so a shipped purpose keeps its spelling.
    if name == 'PURPOSES':
        return pack.active().purposes()
    raise AttributeError(name)


class OrganizationChats:
    def __init__(self, state, *, environment_id, member, records=lambda: {}):
        if not isinstance(environment_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', environment_id):
            raise ValueError('Choose the current company environment.')
        self.company_id = workspace_scope(state)
        # Fixed conversation ids carry the template's purposes, so a directory keeps its template (pack.bind).
        pack.bind(state)
        self.environment_id = environment_id
        self.owner = 'organization-' + self._hash(self.company_id, environment_id)
        self.member = member
        self.records = records
        self._lock = threading.Lock()

    @staticmethod
    def _hash(*parts):
        return hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:32]

    @staticmethod
    def _collection(collection):
        if not isinstance(collection, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', collection):
            raise ValueError('Choose a business collection.')
        return collection

    def identity(self, collection, purpose):
        self._collection(collection)
        template = pack.active()
        if purpose not in template.purposes():
            raise ValueError(template.choose_purpose())
        return 'orgchat-' + self._hash(self.owner, collection, purpose)

    def canonical(self, record):
        if not isinstance(record, dict):
            return False
        try:
            return (record.get('kind') == 'organization_chat' and not record.get('parent_id')
                and record.get('environment_id') == self.environment_id
                and record.get('owner_principal_id') == self.owner
                and record.get('id') == self.identity(record.get('dataset'), record.get('organization_purpose')))
        except (ValueError, TypeError):
            return False

    def owns(self, record):
        if self.canonical(record): return True
        # Descendants keep their host-authored lineage to the canonical root.
        seen = set()
        original = record
        root_fields = {'native_child': 'native_root_id', 'managed_child': 'managed_root_id',
                       'managed_job_child': 'managed_root_id'}
        if not isinstance(original, dict) or original.get('kind') not in root_fields:
            return False
        root_id = original.get(root_fields[original['kind']])
        for _ in range(16):
            if (not isinstance(record, dict) or record.get('kind') not in root_fields
                    or record.get('owner_principal_id') != self.owner
                    or not isinstance(record.get('parent_id'), str) or record.get('id') in seen
                    or record.get(root_fields[record['kind']]) != root_id):
                return False
            seen.add(record.get('id'))
            parent = self.records().get(record['parent_id'])
            if not parent or parent.get('dataset') != record.get('dataset'): return False
            if self.canonical(parent):
                return root_id == parent['id']
            record = parent
        return False

    def can_read(self, record, session):
        if not self.owns(record) or not session or session.get('environment', {}).get('id') != self.environment_id:
            return False
        if not session.get('authenticated') or session.get('role') not in {'owner', 'operator'}:
            return False
        viewed = session.get('viewing_as')
        if viewed:
            if session.get('role') != 'owner' or viewed.get('environment', {}).get('id') != self.environment_id:
                return False
            principal = viewed.get('principal', {}).get('id')
        else:
            if not session.get('can_query'):
                return False
            principal = session.get('principal', {}).get('id')
        # Both the observer and observed member must still belong to this company.
        allowed=bool(self.member(session.get('principal', {}).get('id')) and self.member(principal))
        guard=getattr(self,'history_authorized',None)
        return allowed and (guard is None or guard(record,session))

    def ensure(self, agent, collection, purpose):
        key = self.identity(collection, purpose)
        with self._lock:
            prior = agent.children.get(key)
            if prior is not None:
                if not self.canonical(prior):
                    raise ValueError('Preserve the existing conversation owner.')
                return prior
            record = {'id': key, 'title': pack.active().purposes()[purpose], 'kind': 'organization_chat',
                'organization_purpose': purpose, 'environment_id': self.environment_id,
                'owner_principal_id': self.owner, 'dataset': collection, 'parent_id': None,
                'messages': [], 'running': False, 'created_at': time.time()}
            agent._save_child(record)
            agent.children[key] = record
            return record

    def setup_identity(self, member, collection):
        self._collection(collection)
        if not isinstance(member, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', member):
            raise ValueError('Choose a current company member.')
        return 'messaging-' + self._hash(self.owner, member, collection)

    def member_setup(self, agent, member, collection):
        key = self.setup_identity(member, collection)
        if not self.member(member):
            raise ValueError('Choose a current company member.')
        with self._lock:
            prior = agent.children.get(key)
            if prior is not None:
                if (prior.get('kind') != 'messaging_setup' or prior.get('owner_principal_id') != member
                        or prior.get('dataset') != collection or prior.get('environment_id') != self.environment_id):
                    raise ValueError('Preserve the existing member conversation.')
                return prior
            record = {'id': key, 'title': 'Messaging setup', 'kind': 'messaging_setup',
                'environment_id': self.environment_id, 'owner_principal_id': member,
                'dataset': collection, 'parent_id': None, 'messages': [], 'running': False,
                'created_at': time.time()}
            agent._save_child(record)
            agent.children[key] = record
            return record
