"""Managed Agents protocol projected through Rocoto's admitted workspace tools.

OpenAI owns turn scheduling, orchestration and history. Rocoto admits current
required actions, durably records their outcomes, and projects public chat events.
"""
from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar

from managed_sessions import ACTIVE_TURNS, TERMINAL, identity
from scope_host import bind_admission


_MANAGED_CALL = ContextVar('managed_function_call', default=None)


def current_managed_call():
    """Host-only identity of the ledger-admitted function being executed now."""
    value = _MANAGED_CALL.get()
    return dict(value) if value is not None else None


def hosted_connection_pending(session, action):
    """OpenAI connects its hosted environment; the host only waits for readiness."""
    if not isinstance(action, dict) or action.get('type') != 'environment_connection':
        return False
    environment = session.get('environment')
    if (not isinstance(environment, dict) or environment.get('type') != 'openai_hosted'
            or not isinstance(environment.get('id'), str) or not environment['id']
            or action.get('environment_id') != environment['id']):
        raise ValueError('Wait only for the bound OpenAI-hosted environment.')
    return True


class ManagedTurn:
    """One host generation attached to a workspace-bound provider session."""

    def __init__(self, *, client, ledger, binding, run, admission, registry, execute,
                 result_fields=None, current=None, request=None, children=None):
        self.client, self.ledger, self.binding = client, ledger, binding
        self.run, self.admission, self.registry = run, admission, registry
        self.execute = execute
        self.result_fields = result_fields or (lambda name, content, success: {})
        self.current = current or (lambda: True)
        self.session_id = identity(binding['session_id'])
        self.chat_id = binding['chat_id']
        self.turns = {}
        self.finished_items = set()
        self.terminal = asyncio.Event()
        self.outcome = None
        self.root_turn_id = None
        self.session = None
        self.calls = 0
        self.request = request
        self.children = children
        self.ignored_turns = set(request["baseline"]) if request else set()

    def require_current(self):
        if (self.run['cancel'].is_set() or not self.current()
                or not self.admission.current()):
            raise ValueError('Continue through the current workspace admission.')

    def accept_session(self, session):
        if not isinstance(session, dict) or session.get('id') != self.session_id:
            raise ValueError('Use the provider session bound to this workspace.')
        root = session.get('agent', {}).get('id')
        if not root or self.session and root != self.session.get('agent', {}).get('id'):
            raise ValueError('Keep the original provider agent identity.')
        self.session = session
        return session

    async def attributed_turn(self, turn_id):
        identity(turn_id)
        turn = await self.client.get_turn(self.session_id, turn_id)
        if (not isinstance(turn, dict) or turn.get('id') != turn_id
                or turn.get('session_id', self.session_id) != self.session_id
                or 'subagent_id' not in turn):
            raise ValueError('Resolve the turn through its bound provider session.')
        if turn['subagent_id'] is None:
            self.ledger.turn(self.binding['id'], turn, self.chat_id)
        self.turns[turn_id] = turn
        return turn

    async def required_actions(self):
        """History items and client arguments never authorize effect execution."""
        self.require_current()
        session = self.accept_session(await self.client.get_session(self.session_id))
        actions = session.get('required_actions', [])
        if not isinstance(actions, list) or len(actions) > 100:
            raise ValueError('Read bounded required actions from the current session.')
        self.run['managed_environment_pending'] = False
        for action in actions:
            self.require_current()
            if hosted_connection_pending(session, action):
                self.run['managed_environment_pending'] = True
                continue
            if not isinstance(action, dict) or action.get('type') != 'function_call':
                raise ValueError('Review this provider action through its supported host interface.')
            turn = await self.attributed_turn(action.get('turn_id'))
            self.require_current()
            if turn['id'] in self.ignored_turns or turn.get('status') in TERMINAL:
                continue
            if turn.get('status') not in ACTIVE_TURNS:
                raise ValueError('Run functions from an active provider turn.')
            self.root_turn_id = turn['id'] if turn['subagent_id'] is None else self.root_turn_id
            name = action.get('name')
            # Stock managed subagents use MCP, not function tools. Future protocol
            # changes must retain explicit child authority before adding this path.
            allowed = (turn['subagent_id'] is None and
                       name in self.registry.reads | self.registry.writes)
            claim = self.ledger.claim(self.binding['id'], action,
                generation=self.run['generation'], actor_chat=self.chat_id,
                write=name not in self.registry.reads, recoverable=name == 'delegate_job')
            if claim['decision'] == 'pending':
                continue
            if claim['decision'] in {'replay', 'review'}:
                receipt = claim['receipt']
            else:
                self.calls += 1
                yield {'type': 'tool_start', 'id': action['call_id'], 'name': name,
                       'arguments': action['arguments']}
                self.require_current()
                content, results, success = [], None, False
                if allowed and self.calls <= 100:
                    try:
                        call_token = _MANAGED_CALL.set({'binding_id': self.binding['id'],
                            'request_id': self.request['id'] if self.request else action['turn_id'],
                            'turn_id': action['turn_id'], 'call_id': action['call_id']})
                        try:
                            with bind_admission(self.admission):
                                content, results, success = await self.execute(name, action['arguments'])
                        finally:
                            _MANAGED_CALL.reset(call_token)
                    except (ValueError, KeyError, OSError, RuntimeError, TimeoutError):
                        content = [{'type': 'inputText', 'text':
                                    'Review this operation with the current workspace tools.'}]
                else:
                    content = [{'type': 'inputText', 'text':
                                'Use a declared coordinator function for this operation.'}]
                output = '\n'.join(part.get('text', '') for part in content
                                   if part.get('type') == 'inputText')
                receipt = {'success': bool(success), 'output': output}
                if len(json.dumps(receipt).encode()) > 4_000_000:
                    receipt = {'success': False, 'output':
                               'Read a smaller part of this result using the workspace tools.'}
                # Persist even if Stop/revocation arrived while an admitted effect
                # was draining. Only delivery and further effects are withdrawn.
                self.ledger.completed(self.binding['id'], action['call_id'],
                                      self.run['generation'], receipt)
                self.require_current()
                if results:
                    yield {'type': 'results', 'results': results}
                fields = self.result_fields(name, content, success)
                yield {'type': 'tool_end', 'id': action['call_id'], 'name': name,
                       'success': receipt['success'], 'output': receipt['output'][:12000],
                       'output_truncated': len(receipt['output']) > 12000, **fields}
            self.require_current()
            if name == 'create_file' and receipt['success']:
                artifact = json.loads(receipt['output'])
                if (isinstance(artifact, dict) and artifact.get('id')
                        and artifact.get('chat_id') == self.chat_id
                        and artifact.get('dataset') == self.binding['collection']):
                    yield {'type': 'artifact', 'artifact': artifact}
            result = {'type': 'agent.session.input.tool_result', 'turn_id': action['turn_id'],
                      'call_id': action['call_id'], 'success': receipt['success']}
            result['output' if receipt['success'] else 'error'] = receipt['output']
            delivery = None
            if name in {'send_message', 'send_alert'} and receipt['success']:
                delivery = json.loads(receipt['output'])
                if not isinstance(delivery, dict) or type(delivery.get('waiting_for_reply')) is not bool:
                    raise ValueError('Read the saved channel delivery receipt.')
                if self.request is not None:
                    self.ledger.save_delivery_boundary(self.binding['id'], self.request['id'], action['call_id'], delivery)
            await self.client.submit_events(self.session_id, [result],
                                             submission_key=claim['submission_key'])
            if delivery is not None:
                self.require_current()
                yield {'type': 'channel_delivery', 'delivery': delivery}
                return

    async def event(self, event):
        """Whitelist public fields; provider errors/configuration remain private."""
        if event.session_id != self.session_id:
            raise ValueError('Project events from the bound managed session.')
        raw = event.raw
        if event.turn_id in self.ignored_turns:
            return []
        if event.category == 'subagent' and self.children is not None:
            chat = await self.children.admit(event.subagent_id)
            return [self.children.public_event(chat)]
        if event.category == 'turn':
            turn = raw.get('turn', {})
            if 'subagent_id' not in turn:
                turn = await self.attributed_turn(event.turn_id)
            if turn['subagent_id'] is not None:
                if self.children is None or self.run['cancel'].is_set():
                    return []
                chat = await self.children.admit(turn['subagent_id'])
                self.ledger.turn(self.binding['id'], turn, chat)
                self.turns[turn['id']] = turn
                record = self.children.agent.children[chat]
                record.update(running=turn['status'] not in TERMINAL, status=turn['status'])
                self.children.agent._save_child(record)
                return [self.children.public_event(chat)]
            self.ledger.turn(self.binding['id'], turn, self.chat_id)
            self.turns[turn['id']] = turn
            if self.root_turn_id is not None and turn['id'] != self.root_turn_id:
                return []
            self.root_turn_id = turn['id']
            if self.request is not None:
                self.ledger.request_state(self.request['id'], 'submitted', turn_id=turn['id'])
            self.run['turn_id'] = turn['id']
            if 'ready' in self.run:
                self.run['ready'].set()
            if turn['status'] in TERMINAL:
                self.outcome = turn['status']
                self.terminal.set()
                return [{'type': 'done', 'thread_id': self.session_id,
                         'status': 'interrupted' if self.outcome == 'cancelled' else self.outcome}]
            return []
        self.require_current()
        if event.category == 'session':
            self.accept_session(raw.get('session'))
            return []
        if event.category in {'text_delta', 'text_done'}:
            turn = self.turns.get(event.turn_id) or await self.attributed_turn(event.turn_id)
            self.require_current()
            if event.item_id in self.finished_items:
                return []
            # Completed text is canonical after reconnect. Buffering until done
            # avoids appending a stale delta to a recovered complete message.
            if event.category == 'text_done':
                text = raw.get('text')
                if not isinstance(text, str) or len(text.encode()) > 4_000_000:
                    raise ValueError('Read a bounded completed text item.')
                self.finished_items.add(identity(event.item_id))
                output = {'type': 'delta', 'text': text, 'managed_item_id': event.item_id}
                if turn['subagent_id'] is not None:
                    return await self.child_output(turn, output)
                return [output]
        return []

    async def snapshot(self, session, items, turns, *, turn_ids=None):
        """Reconcile canonical public history before consuming buffered events."""
        self.require_current()
        self.accept_session(session)
        selected = set(turn_ids) if turn_ids is not None else None
        result = []
        for turn in turns:
            if turn.get('subagent_id', 'missing') is None:
                self.ledger.turn(self.binding['id'], turn, self.chat_id)
                self.turns[turn['id']] = turn
        for item in items:
            turn_id = item.get('turn_id')
            if selected is not None and turn_id not in selected:
                continue
            turn = self.turns.get(turn_id)
            if (not turn or turn['subagent_id'] is not None or item.get('type') != 'message'
                    or item.get('role') != 'assistant' or item.get('status') != 'completed'
                    or item.get('id') in self.finished_items):
                continue
            text = ''.join(part.get('text', '') for part in item.get('content', [])
                           if part.get('type') == 'output_text')
            if len(text.encode()) > 4_000_000:
                raise ValueError('Read a bounded completed text item.')
            self.finished_items.add(identity(item.get('id')))
            result.append({'type': 'delta', 'text': text, 'managed_item_id': item['id']})
        return result

    async def child_output(self, turn, event):
        if self.children is None:
            return []
        chat = await self.children.admit(turn['subagent_id'])
        self.require_current()
        record = self.children.agent.children[chat]
        answer = next((item for item in record['messages']
                       if item.get('managed_turn_id') == turn['id']), None)
        if answer is None:
            answer = {'role': 'assistant', 'content': '', 'tools': [], 'results': [],
                      'managed_turn_id': turn['id']}
            record['messages'].append(answer)
        self.children.agent._record_event(answer, event)
        self.children.agent._save_child(record)
        return [self.children.public_event(chat)]
