"""Retain stock hosted artifacts in the owning conversation's existing Files store.

Only provider-attested completed turns in a durably bound session supply files.
Transport bytes are bounded, hashed and committed locally before receipt saving;
recovery reuses the immutable publication key across process/session generations.
"""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
import re
import tempfile

from chat_files import MAX_NATIVE_FILE_BYTES
from managed_sessions import digest
from workspace_identity import workspace_scope


def resource_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', value):
        raise ValueError('Use a provider artifact identity.')
    return value


def metadata(value):
    if not isinstance(value, dict) or value.get('object') != 'agent.session.artifact':
        raise ValueError('Read immutable provider artifact metadata.')
    result = {key: resource_id(value.get(key)) for key in ('id', 'session_id', 'environment_id', 'turn_id')}
    path = value.get('path')
    if (not isinstance(path, str) or not path.startswith('/workspace/outputs/') or len(path) > 2048
            or '\\' in path or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in {'', '.', '..'} for part in path.split('/')[1:])
            or len(PurePosixPath(path).parts) > 20):
        raise ValueError('Publish a bounded file beneath /workspace/outputs.')
    if type(value.get('size_bytes')) is not int or not 0 <= value['size_bytes'] <= MAX_NATIVE_FILE_BYTES:
        raise ValueError('Retain outputs up to 16 MiB each.')
    if type(value.get('created_at')) is not int or value['created_at'] < 0:
        raise ValueError('Read the immutable artifact timestamp.')
    result.update(path=path, size_bytes=value['size_bytes'], created_at=value['created_at'], object=value['object'])
    return result


class ManagedFiles:
    def __init__(self, agent, ledger, client, *, current):
        self.agent, self.ledger, self.client, self.current = agent, ledger, client, current

    def require_current(self, binding):
        record = self.agent.children.get(binding['chat_id'], {})
        actual = self.ledger.get(binding['id'])
        fields = ('id', 'company_id', 'chat_id', 'owner', 'collection', 'session_id')
        if (not self.current() or binding['company_id'] != workspace_scope(self.agent.state)
                or any(actual[key] != binding[key] for key in fields)
                or record.get('parent_id')
                or (record.get('owner_principal_id'), record.get('dataset')) != (binding['owner'], binding['collection'])):
            raise ValueError('Read files through their original admitted workspace and conversation.')

    async def completed_turn(self, binding, session, artifact):
        turn = await self.client.get_turn(binding['session_id'], artifact['turn_id'])
        self.require_current(binding)
        if (turn.get('id') != artifact['turn_id'] or turn.get('status') != 'completed'
                or turn.get('session_id', binding['session_id']) != binding['session_id']
                or 'subagent_id' not in turn):
            raise ValueError('Publish files from an attributed completed session turn.')
        child = turn['subagent_id']
        if child is None:
            return
        # Child files share the session filesystem, but every ancestor must still
        # be attested inside this exact session before a parent may retain them.
        records, after, seen = {}, None, set()
        for _ in range(4):
            page = await self.client.list_subagents(binding['session_id'], after=after, limit=100, order='asc')
            self.require_current(binding)
            if not isinstance(page.get('data'), list):
                raise ValueError('Read complete provider artifact ancestry.')
            for value in page['data']:
                if value.get('session_id') != binding['session_id'] or len(records) >= 128:
                    raise ValueError('Keep artifact children inside their provider session.')
                key = resource_id(value.get('id'))
                if key in records:
                    raise ValueError('Read unique artifact child identities.')
                records[key] = resource_id(value.get('parent_agent_id'))
            if page.get('has_more') is False:
                break
            after = resource_id(page.get('last_id'))
            if after in seen:
                raise ValueError('Read each ancestry page once.')
            seen.add(after)
        else:
            raise ValueError('Read artifact ancestry within its page allowance.')
        seen = set()
        root = resource_id(session.get('agent', {}).get('id'))
        while child != root:
            if child in seen or child not in records or len(seen) >= 16:
                raise ValueError('Keep every artifact child beneath the original session root.')
            seen.add(child)
            child = records[child]

    async def harvest(self, binding):
        self.require_current(binding)
        session_id = resource_id(binding['session_id'])
        session = await self.client.get_session(session_id)
        self.require_current(binding)
        if session.get('id') != session_id or session.get('environment', {}).get('type') != 'openai_hosted':
            raise ValueError('Read outputs from the bound OpenAI-hosted session.')
        environment = resource_id(session['environment'].get('id'))
        results, after, cursors, seen = [], None, set(), set()
        total = 0
        for _ in range(10):
            page = await self.client.list_artifacts(session_id, after=after, limit=100, order='asc')
            self.require_current(binding)
            if not isinstance(page.get('data'), list) or len(page['data']) > 100:
                raise ValueError('Read a bounded artifact page.')
            for value in page['data']:
                item = metadata(value)
                if item['session_id'] != session_id or item['environment_id'] != environment or item['id'] in seen:
                    raise ValueError('Keep unique artifacts inside their bound session environment.')
                seen.add(item['id'])
                receipt = self.ledger.artifact(binding['id'], item['id'], item)
                scope = dict(owner_principal_id=binding['owner'], chat_id=binding['chat_id'], dataset=binding['collection'])
                if receipt is not None:
                    record = self.agent.file_store.get(receipt, **scope)
                else:
                    await self.completed_turn(binding, session, item)
                    actual = await self.client.get_artifact(session_id, item['id'])
                    self.require_current(binding)
                    if metadata(actual) != item:
                        raise ValueError('Read the same immutable artifact metadata before downloading.')
                    total += item['size_bytes']
                    if total > 64 * 1024 * 1024:
                        raise ValueError('Continue file recovery within the next 64 MiB allowance.')
                    with tempfile.TemporaryFile(mode='w+b') as buffer:
                        checksum, size = hashlib.sha256(), 0
                        async for chunk in self.client.artifact_content(session_id, item['id'], max_bytes=item['size_bytes']):
                            self.require_current(binding)
                            size += len(chunk)
                            checksum.update(chunk)
                            buffer.write(chunk)
                        self.require_current(binding)
                        if size != item['size_bytes']:
                            raise ValueError('Retain the exact immutable artifact byte count.')
                        buffer.seek(0)
                        key = digest({'binding': binding['id'], 'artifact': item})
                        record = self.agent.file_store.import_native(buffer, name=PurePosixPath(item['path']).name,
                            size_bytes=size, sha256=checksum.hexdigest(), publication_key=key,
                            provenance={'runtime': 'openai_managed', 'binding': binding['id'], 'artifact': item}, **scope)
                    self.ledger.artifact(binding['id'], item['id'], item, file_id=record['id'])
                results.append({'type': 'artifact', 'artifact': record, 'managed_turn_id': item['turn_id'],
                                'managed_session_id': session_id})
            if page.get('has_more') is False:
                return results
            after = resource_id(page.get('last_id'))
            if after in cursors:
                raise ValueError('Read each artifact page once.')
            cursors.add(after)
        raise ValueError('Recover artifacts within the session page allowance.')
