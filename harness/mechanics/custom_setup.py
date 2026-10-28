"""Public target proposals; only the authenticated owner's action grants access."""
from __future__ import annotations

import json
import secrets
import time

from custom_execution import authority, digest, encoded, require, validate_target


class CustomSetup:
    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store

    def propose(self, key, target, reason, *, expected_revision, owner, collection):
        require(isinstance(reason, str) and 1 <= len(reason.strip()) <= 1000,
            'Explain the requested connection access.')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            record, package = self.manager._owned(db, key, owner, collection)
            require(type(expected_revision) is int and record['revision'] == expected_revision,
                'Read the current connector source before proposing access.')
            require(package and package.get('execution'), 'Prepare the executable read tools first.')
            value = validate_target(target, {f['name'] for f in record['secret_fields']})
            operations = {name for tool in package['execution']['tools'] for name in tool['operations']}
            require(operations == {op['id'] for op in value['operations']},
                'Request exactly the read operations used by this connector.')
            row = authority(db, key)
            document = {'revision':expected_revision, 'target_generation':row['target_generation'],
                'target':value, 'target_sha256':digest(value), 'reason':reason.strip()}
            self.store._public_text(db, key, document)
            serialized = encoded(document).decode()
            existing = db.execute('SELECT id FROM custom_target_proposals WHERE connector_id=? AND document=?',
                (key, serialized)).fetchone()
            if existing:
                return {'id':existing[0], **document, 'status':'proposed'}
            count = db.execute('SELECT count(*) FROM custom_target_proposals WHERE connector_id=?', (key,)).fetchone()[0]
            total = db.execute('SELECT coalesce(sum(length(CAST(document AS BLOB))),0) FROM custom_target_proposals').fetchone()[0]
            require(count < 32 and total + len(serialized.encode()) <= 16 * 1024 * 1024,
                'Review retained connection access proposals.')
            identifier = 'target-' + secrets.token_hex(16)
            db.execute('INSERT INTO custom_target_proposals VALUES(?,?,?,?,?,?)',
                (identifier, key, owner, collection, serialized, time.time()))
            return {'id':identifier, **document, 'status':'proposed'}

    def approve(self, key, proposal_id, *, owner, collection):
        # The proposal is immutable. Its version and generation are rechecked
        # together inside grant_target's write transaction, closing the read gap.
        with self.store.db() as db:
            self.manager._owned(db, key, owner, collection)
            row = db.execute('SELECT document FROM custom_target_proposals WHERE id=? AND connector_id=? AND owner=? AND collection=?',
                (proposal_id, key, owner, collection)).fetchone()
            require(row is not None, 'Review an access proposal from this connector.')
            value = json.loads(row[0])
        return self.manager.grant_target(key, value['target'], owner=owner, collection=collection,
            expected_generation=value['target_generation'], expected_revision=value['revision'])

    def describe(self, key, *, owner, collection):
        with self.store.db() as db:
            record, package = self.manager._owned(db, key, owner, collection)
            row = authority(db, key)
            grant = db.execute('SELECT document FROM custom_execution_grants WHERE connector_id=? AND generation=?',
                (key, row['target_generation'])).fetchone()
            current_target = json.loads(grant[0]) if grant else None
            latest = db.execute('SELECT id,document FROM custom_target_proposals WHERE connector_id=? AND owner=? AND collection=? ORDER BY created DESC,id DESC LIMIT 1',
                (key, owner, collection)).fetchone()
            proposal = None
            if latest:
                value = json.loads(latest[1])
                proposal = {'id':latest[0], **value,
                    'current':value['revision'] == record['revision'] and value['target_generation'] == row['target_generation']}
            result = {'target':current_target, 'proposal':proposal,
                'tools':(package or {}).get('execution', {}).get('tools', []),
                'active_tools':json.loads(row['active_snapshot'])['manifest']['tools']
                    if row['active_status']=='active' and row['active_snapshot'] else []}
            last = db.execute('SELECT id FROM custom_execution_tests WHERE connector_id=? AND owner=? AND collection=? ORDER BY created DESC,id DESC LIMIT 1',
                (key, owner, collection)).fetchone()
        result['last_test'] = self.manager.test_result(last[0], owner=owner, collection=collection) if last else None
        return {**self.manager.status(key, owner=owner, collection=collection), **result}
