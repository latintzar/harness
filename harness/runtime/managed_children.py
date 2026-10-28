"""Project provider-attested managed subagents into scoped Rocoto conversations."""
from __future__ import annotations

import uuid

from managed_sessions import identity


class ManagedChildren:
    def __init__(self, agent, chat_id, run, session, client, admission):
        self.agent, self.chat_id, self.run = agent, chat_id, run
        self.session_id = identity(session['id'])
        self.root_agent = identity(session['agent']['id'])
        self.client, self.admission = client, admission
        self.bound = {self.root_agent: chat_id}
        record = agent.children[chat_id]
        self.scope = (record['owner_principal_id'], record['dataset'])
        self.records = {}

    def require_current(self):
        record = self.agent.children.get(self.chat_id, {})
        if (self.agent.active.get(self.chat_id) is not self.run or self.run['cancel'].is_set()
                or not self.admission.current()
                or (record.get('owner_principal_id'), record.get('dataset')) != self.scope):
            raise ValueError('Continue delegation through its current parent workspace.')

    async def refresh(self):
        """Only a bounded, fully paginated provider response establishes lineage."""
        self.require_current()
        result, after, cursors = {}, None, set()
        for _ in range(4):
            page = await self.client.list_subagents(self.session_id, after=after, limit=100, order='asc')
            if not isinstance(page.get('data'), list):
                raise ValueError('Read the complete provider subagent registry.')
            for value in page['data']:
                if (not isinstance(value, dict) or value.get('session_id') != self.session_id
                        or not value.get('parent_agent_id')):
                    raise ValueError('Read children belonging to the bound provider session.')
                child_id = identity(value.get('id'))
                if child_id in result or len(result) >= 128:
                    raise ValueError('Keep a bounded, unique provider child registry.')
                result[child_id] = value
            if page.get('has_more') is False:
                self.require_current()
                self.records = result
                return
            after = identity(page.get('last_id'))
            if after in cursors:
                raise ValueError('Read each provider child page once.')
            cursors.add(after)
        raise ValueError('Keep delegation within the configured child allowance.')

    def ancestry(self, child_id):
        chain, seen = [], set()
        current = identity(child_id)
        while current != self.root_agent:
            if current in seen or len(seen) >= 16 or current not in self.records:
                raise ValueError('Establish every child ancestor through the provider session.')
            seen.add(current)
            record = self.records[current]
            chain.append(record)
            current = identity(record['parent_agent_id'])
        return list(reversed(chain))

    async def admit(self, child_id):
        self.require_current()
        if child_id not in self.records:
            await self.refresh()
        chain = self.ancestry(child_id)
        # Validate the complete ancestry before writing any local conversation.
        for value in chain:
            self.require_current()
            native_id = value['id']
            parent_id = self.bound[value['parent_agent_id']]
            existing = self.bound.get(native_id)
            if existing is None:
                existing = next((key for key, record in self.agent.children.items()
                    if record.get('managed_session_id') == self.session_id
                    and record.get('managed_subagent_id') == native_id
                    and record.get('managed_root_id') == self.chat_id
                    and record.get('parent_id') == parent_id
                    and (record.get('owner_principal_id'), record.get('dataset')) == self.scope), None)
            chat = existing or 'managed-child-' + uuid.uuid4().hex
            title = value.get('name') or 'Investigation'
            record = self.agent.children.get(chat) or {
                'id': chat, 'parent_id': parent_id, 'managed_root_id': self.chat_id,
                'managed_session_id': self.session_id, 'managed_subagent_id': native_id,
                'owner_principal_id': self.scope[0], 'dataset': self.scope[1],
                'kind': 'managed_child', 'messages': [],
                'title': title[:120] if isinstance(title, str) else 'Investigation'}
            self.agent.children[chat] = record
            self.agent._save_child(record)
            self.bound[native_id] = chat
            self.run['child_ids'].add(chat)
        return self.bound[child_id]

    def public_event(self, chat_id):
        record = self.agent.children[chat_id]
        return {'type': 'subchat', 'chat_id': chat_id, 'parent_id': record['parent_id'],
                'title': record['title'], 'status': record.get('status', 'running'),
                'url': '/?chat=' + chat_id}

    async def recover_history(self):
        """A subagent's separate canonical history repairs missed live events."""
        from managed_sessions import TERMINAL
        await self.refresh()
        result = []
        for child_id in self.records:
            chat_id = await self.admit(child_id)
            after, seen, turns = None, set(), {}
            for _ in range(10):
                page = await self.client.list_subagent_turns(self.session_id, child_id,
                    after=after, limit=100, order='asc')
                self.require_current()
                if not isinstance(page.get('data'), list):
                    raise ValueError('Read the complete child turn history.')
                for turn in page['data']:
                    if turn.get('subagent_id') != child_id:
                        raise ValueError('Keep child turn attribution inside its provider ancestry.')
                    turns[identity(turn.get('id'))] = turn
                if page.get('has_more') is False:
                    break
                after = identity(page.get('last_id'))
                if after in seen:
                    raise ValueError('Read each child turn page once.')
                seen.add(after)
            else:
                raise ValueError('Read child turns within the history allowance.')
            items = await self.client.collect_items(self.session_id, subagent_id=child_id, max_pages=10)
            self.require_current()
            record = self.agent.children[chat_id]
            for item in items:
                turn = turns.get(item.get('turn_id'))
                if (turn is None or item.get('type') != 'message' or item.get('role') != 'assistant'
                        or item.get('status') != 'completed'):
                    continue
                text = ''.join(part.get('text', '') for part in item.get('content', [])
                               if part.get('type') == 'output_text')
                if len(text.encode()) > 4_000_000:
                    raise ValueError('Read a bounded child message.')
                answer = next((value for value in record['messages']
                               if value.get('managed_turn_id') == turn['id']), None)
                if answer is None:
                    answer = {'role': 'assistant', 'content': '', 'tools': [], 'results': [],
                              'managed_turn_id': turn['id']}
                    record['messages'].append(answer)
                self.agent._record_event(answer, {'type': 'delta', 'text': text,
                                                  'managed_item_id': identity(item.get('id'))})
            if turns:
                latest = list(turns.values())[-1]
                record.update(status=latest['status'], running=latest['status'] not in TERMINAL)
            self.agent._save_child(record)
            result.append(self.public_event(chat_id))
        return result
