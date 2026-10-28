"""Explicit per-person chat ownership and durable pin/archive preferences.

The HTTP host supplies the selected tenant's state and authenticated session.
Unowned legacy chats remain unassigned; owner observation permits reading only.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time


def workspace_writable(session):
    return bool(session and session.get('authenticated') and session.get('can_query')
        and session.get('role') in {'owner','operator'} and not session.get('viewing_as'))


class ChatPermissions:
    def __init__(self,state, *, organization=None):
        self.organization=organization
        self.state=Path(state);self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=self.state/'chat-permissions.sqlite'
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS chat_metadata (
                principal_id TEXT NOT NULL, chat_id TEXT NOT NULL, revision INTEGER NOT NULL,
                pinned INTEGER NOT NULL, archived INTEGER NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(principal_id,chat_id))''')
            db.executescript('''CREATE TABLE IF NOT EXISTS ownership_assignments (
                chat_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, record_sha256 TEXT NOT NULL,
                authorization_note TEXT NOT NULL, assigned_at REAL NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_ownership_update BEFORE UPDATE ON ownership_assignments
                    BEGIN SELECT RAISE(ABORT,'Ownership assignment audit is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_ownership_delete BEFORE DELETE ON ownership_assignments
                    BEGIN SELECT RAISE(ABORT,'Ownership assignment audit is immutable'); END;
                CREATE TABLE IF NOT EXISTS attachment_owners (
                    attachment_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                    origin TEXT NOT NULL, authorization_note TEXT, registered_at REAL NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_attachment_owner_update BEFORE UPDATE ON attachment_owners
                    BEGIN SELECT RAISE(ABORT,'Attachment ownership is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_attachment_owner_delete BEFORE DELETE ON attachment_owners
                    BEGIN SELECT RAISE(ABORT,'Attachment ownership is immutable'); END;''')
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        return db

    @staticmethod
    def principal(session):
        if not session or not session.get('authenticated') or session.get('role') not in {'owner','operator'}:
            return None
        viewing=session.get('viewing_as')
        if viewing:
            if session['role']!='owner' or viewing.get('environment',{}).get('id')!=session.get('environment',{}).get('id'):
                return None
            return viewing.get('principal',{}).get('id')
        return session.get('principal',{}).get('id') if session.get('can_query') else None

    def _owned(self,record,session):
        principal=self.principal(session)
        allowed=bool(principal and isinstance(record,dict) and (record.get('owner_principal_id')==principal
            or self.organization is not None and self.organization.can_read(record,session)))
        guard=getattr(self,'history_authorized',None)
        return allowed and (guard is None or guard(record,session))

    def participant(self,record,session):
        """The selected person reads this Tracking Agent's own conversation as one of its participants.

        Participation covers that one top-level conversation. Its runs, child
        chats and the agent's definition stay with their owner. The host supplies
        `participant_authorized(record, principal)` from the participant records;
        with none, or with a failing read, the conversation is its owner's alone.
        The location-assignment guard applies to the participant themself.
        """
        principal=self.principal(session);check=getattr(self,'participant_authorized',None)
        if (not principal or check is None or not isinstance(record,dict) or record.get('owner_principal_id')==principal
                or record.get('kind')!='agent_conversation' or record.get('parent_id') or not isinstance(record.get('id'),str)):
            return False
        try:admitted=check(record,principal) is True
        except Exception:admitted=False
        guard=getattr(self,'history_authorized',None)
        return bool(admitted and (guard is None or guard(record,session)))

    def can_read(self,record,session):
        return self._owned(record,session) or self.participant(record,session)

    def can_write(self,record,session):
        """Owner authority: the definition, its controls and every host interaction of the conversation."""
        return workspace_writable(session) and self._owned(record,session)

    def can_converse(self,record,session):
        """Write a message in the conversation: its owner, or a participant of a Tracking Agent's own chat."""
        return workspace_writable(session) and self.can_read(record,session)

    def _attachment_digest(self,attachment_id):
        if not isinstance(attachment_id,str) or not re.fullmatch(r'[a-f0-9]{32}',attachment_id):
            raise ValueError('Choose an uploaded image from this workspace.')
        path=self.state/'attachments'/(attachment_id+'.png')
        # The validated 10 MB upload can expand when normalized to RGB PNG.
        if path.is_symlink() or not path.is_file() or path.stat().st_size>64*1024*1024:
            raise ValueError('Choose an available uploaded image.')
        with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

    def _register_attachment(self,db,attachment_id,principal,origin,authorization_note=None):
        digest=self._attachment_digest(attachment_id)
        prior=db.execute('SELECT * FROM attachment_owners WHERE attachment_id=?',(attachment_id,)).fetchone()
        if prior:
            if prior['principal_id']!=principal or prior['sha256']!=digest:
                raise ValueError('Preserve the uploaded image’s original owner and source.')
            return
        db.execute('INSERT INTO attachment_owners VALUES(?,?,?,?,?,?)',
            (attachment_id,principal,digest,origin,authorization_note,time.time()))

    def register_attachment(self,attachment_id,session):
        if not workspace_writable(session) or not self.principal(session):
            raise ValueError('Upload images from your own workspace.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._register_attachment(db,attachment_id,self.principal(session),'principal_upload')

    def can_read_attachment(self,attachment_id,session):
        principal=self.principal(session)
        if not principal or not isinstance(attachment_id,str) or not re.fullmatch(r'[a-f0-9]{32}',attachment_id):return False
        with self.connect() as db:
            row=db.execute('SELECT principal_id FROM attachment_owners WHERE attachment_id=?',(attachment_id,)).fetchone()
        return bool(row and row['principal_id']==principal)

    def validate_attachments(self,attachment_ids,session,*,write=False):
        if (not isinstance(attachment_ids,(list,tuple)) or len(attachment_ids)>8
                or any(not isinstance(key,str) for key in attachment_ids)):
            raise ValueError('Choose up to eight images from your workspace.')
        if write and not workspace_writable(session):raise ValueError('Use images from your own writable workspace.')
        if any(not self.can_read_attachment(key,session) for key in attachment_ids):
            raise ValueError('Choose uploaded images belonging to the selected person.')

    def _metadata(self,db,record,session):
        principal=self.principal(session)
        row=db.execute('SELECT * FROM chat_metadata WHERE principal_id=? AND chat_id=?',(principal,record['id'])).fetchone()
        participant=self.participant(record,session)
        return {'pinned':bool(row['pinned']) if row else False,'archived':bool(row['archived']) if row else False,
            'metadata_revision':row['revision'] if row else 0,
            'read_only':not (workspace_writable(session) and (participant or self._owned(record,session))),
            'participant':participant,
            'metadata_updated_at':row['updated_at'] if row else None}

    def get(self,record,session):
        if not self.can_read(record,session):raise ValueError('Choose a chat belonging to the selected person.')
        with self.connect() as db:return {**copy.deepcopy(record),**self._metadata(db,record,session)}

    def visible_records(self,records,session,*,include_archived=False):
        candidates=records.values() if isinstance(records,dict) else records
        with self.connect() as db:
            visible=[{**copy.deepcopy(record),**self._metadata(db,record,session)} for record in candidates if self.can_read(record,session)]
        return [record for record in visible if include_archived or not record['archived']]

    def update(self,record,session,*,pinned=None,archived=None,expected_revision=None):
        if not self.can_converse(record,session):raise ValueError('Edit chat preferences from the chat owner’s workspace.')
        if not isinstance(record.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',record['id']):
            raise ValueError('Choose a saved chat identifier.')
        if (pinned is None and archived is None) or any(value is not None and type(value) is not bool for value in [pinned,archived]):
            raise ValueError('Set the requested pin or archive preference.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');current=self._metadata(db,record,session)
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision!=current['metadata_revision']):
                raise ValueError('Refresh the chat preference revision before saving.')
            db.execute('INSERT OR REPLACE INTO chat_metadata VALUES(?,?,?,?,?,?)',
                (self.principal(session),record['id'],current['metadata_revision']+1,
                 int(current['pinned'] if pinned is None else pinned),int(current['archived'] if archived is None else archived),time.time()))
            return {**copy.deepcopy(record),**self._metadata(db,record,session)}

    def assign_founder_history(self,records,owner_session,*,chat_ids,authorization_note):
        """Host-only, explicitly authorized migration of enumerated Demo history.

        Returns copies for the host's normal persistence function. This helper
        has no HTTP/tool route and never changes session roles or report content.
        """
        if (not workspace_writable(owner_session) or owner_session.get('role')!='owner'
                or owner_session.get('environment',{}).get('id')!='demo'):
            raise ValueError('Use the founder’s authenticated owner session in Demo.')
        if (not isinstance(chat_ids,list) or not 1<=len(chat_ids)<=1000
                or any(not isinstance(key,str) for key in chat_ids) or len(set(chat_ids))!=len(chat_ids)
                or not isinstance(authorization_note,str) or not authorization_note.strip() or len(authorization_note)>2000):
            raise ValueError('Enumerate the authorized Demo chat IDs and preserve the founder authorization.')
        available=records if isinstance(records,dict) else {record['id']:record for record in records}
        principal=self.principal(owner_session);assigned=[]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key in chat_ids:
                record=available.get(key)
                if not isinstance(record,dict) or record.get('id')!=key or record.get('environment_id','demo')!='demo':
                    raise ValueError('Select existing Demo chat records for the founder migration.')
                prior=db.execute('SELECT * FROM ownership_assignments WHERE chat_id=?',(key,)).fetchone()
                if prior and prior['principal_id']!=principal:
                    raise ValueError('Preserve the existing audited chat owner.')
                if record.get('owner_principal_id') is not None:
                    if prior and record['owner_principal_id']==principal:
                        assigned.append(copy.deepcopy(record));continue
                    raise ValueError('Preserve each chat’s existing explicit owner.')
                checksum=hashlib.sha256(json.dumps(record,sort_keys=True,allow_nan=False).encode()).hexdigest()
                if prior is None:
                    db.execute('INSERT INTO ownership_assignments VALUES(?,?,?,?,?)',
                        (key,principal,checksum,authorization_note,time.time()))
                    prior=db.execute('SELECT * FROM ownership_assignments WHERE chat_id=?',(key,)).fetchone()
                elif prior['record_sha256']!=checksum:
                    raise ValueError('Review the changed legacy chat before assigning its owner.')
                updated=copy.deepcopy(record)
                updated.update(owner_principal_id=principal,owner_assignment_provenance={
                    'kind':'explicit_founder_demo_history','prior_record_sha256':prior['record_sha256'],
                    'assigned_at':prior['assigned_at'],'authorization_note':prior['authorization_note']})
                assigned.append(updated)
        return assigned

    def assign_founder_attachments(self,records,owner_session,*,chat_ids,authorization_note):
        """Host-only migration of images actually referenced by assigned Demo chats."""
        if (not workspace_writable(owner_session) or owner_session.get('role')!='owner'
                or owner_session.get('environment',{}).get('id')!='demo'):
            raise ValueError('Use the founder’s authenticated Demo owner session.')
        if (not isinstance(chat_ids,list) or not 1<=len(chat_ids)<=1000 or any(not isinstance(key,str) for key in chat_ids)
                or len(set(chat_ids))!=len(chat_ids) or not isinstance(authorization_note,str)
                or not authorization_note.strip() or len(authorization_note)>2000):
            raise ValueError('Enumerate the authorized founder chats and preserve the authorization.')
        available=records if isinstance(records,dict) else {record['id']:record for record in records}
        referenced=set()
        for key in chat_ids:
            record=available.get(key)
            if (not self.can_write(record,owner_session) or record.get('id')!=key
                    or record.get('environment_id','demo')!='demo'):
                raise ValueError('Select already-assigned founder Demo chat records.')
            for message in record.get('messages',[]):
                for attachment in message.get('attachments',[]):
                    identity=attachment.get('id') if isinstance(attachment,dict) else attachment
                    if not isinstance(identity,str) or not re.fullmatch(r'[a-f0-9]{32}',identity):
                        raise ValueError('Review each legacy image reference before assignment.')
                    referenced.add(identity)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for attachment_id in sorted(referenced):
                self._register_attachment(db,attachment_id,self.principal(owner_session),
                    'explicit_founder_demo_history',authorization_note)
        return sorted(referenced)
