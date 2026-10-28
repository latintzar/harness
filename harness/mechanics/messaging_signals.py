"""Verified provider messages enter the workspace's existing signal ledger.

The inbox retains provider verification; agent_wakes owns scheduling, authority,
limits and dispatch. The ordinary saved user message is the admission receipt.
"""
from __future__ import annotations

import asyncio
import hashlib
import json


def reserve_send(store, actor, row):
    """One stable outgoing effect unit in the same workspace-wide signal ledger."""
    row = dict(row)
    if not row.get('id') or not row.get('channel'):
        return False
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        return store.signals.reserve(db, 'send', 'message:' + row['id'], target=actor.chat_id)


class MessagingSignals:
    def __init__(self, messaging, store, agent, *, member, answered=None):
        self.messaging, self.store, self.agent = messaging, store, agent
        self.member, self.answered = member, answered

    def current(self, signal):
        event_id = signal.get('payload', {}).get('messaging_reply_id')
        return bool(event_id and self.messaging.signal_reply_current(event_id, signal))

    def _admitted(self, reply):
        record = self.agent.children.get(reply['chat_id']) or {}
        return any(item.get('channel', {}).get('admission_key') == reply['admission_key']
                   for item in record.get('messages', []))

    def _queued(self, reply, owner, connection):
        with self.store.connect() as db:
            row = db.execute('SELECT body FROM agent_wakes WHERE agent_id=? AND owner=? AND provider=? '
                'AND connection_id=? AND event_id=?', (reply['chat_id'], owner, reply['channel'],
                connection, reply['provider_event_id'])).fetchone()
            return json.loads(row['body']) if row else None

    async def tick(self):
        for reply in await asyncio.to_thread(self.messaging.unsettled_replies, limit=8):
            if self._admitted(reply):
                await asyncio.to_thread(self.messaging.finish_reply, reply['id'], reply['claim_id'], admitted=True)
        for reply in await asyncio.to_thread(self.messaging.pending_replies, limit=8):
            record = self.agent.children.get(reply['chat_id'])
            if (not record or record.get('dataset') != reply['collection']
                    or not record.get('owner_principal_id')):
                continue
            if self._admitted(reply):
                await self._finish(reply, True)
                continue
            member = self.member(reply['member_id'])
            if not member:
                continue
            sender = {'principal_id':reply['member_id'], 'display_name':member['display_name'],
                'channel':reply['channel'], 'admission_key':reply['admission_key'],
                'provider_event_id':reply['provider_event_id']}
            connection = 'message-' + hashlib.sha256(
                json.dumps([reply['channel'], reply['account']]).encode()).hexdigest()
            queued = await asyncio.to_thread(self._queued, reply, record['owner_principal_id'], connection)
            if queued is not None:
                if queued['status'] not in {'pending', 'claimed'}:
                    await self._finish(reply, False)
                continue
            payload = {'text':reply['text'], 'messaging_reply_id':reply['id']}
            if self.answered is not None:
                answered = await asyncio.to_thread(self.answered, reply)
                if answered:
                    payload['model_context'] = ('Host-supplied context (reference data): '
                        'this message answers the phone alert ' + json.dumps(answered, ensure_ascii=False))
            try:
                signal = await asyncio.to_thread(self.store.wakes.enqueue_chat,
                    chat_id=reply['chat_id'], collection=reply['collection'], owner=record['owner_principal_id'],
                    actor=reply['member_id'], provider=reply['channel'], connection_id=connection,
                    event_id=reply['provider_event_id'], text=reply['text'], sender=sender, payload=payload)
            except ValueError:
                # Authority or capacity can change between verification and enqueue.
                # No claim was taken; a later tick can retry the same provider event.
                continue
            if signal['status'] not in {'pending', 'claimed'}:
                await self._finish(reply, False)

    async def _finish(self, reply, admitted):
        claim = await asyncio.to_thread(self.messaging.claim_reply, reply['id'])
        if claim is not None:
            await asyncio.to_thread(self.messaging.finish_reply, reply['id'], claim['claim_id'], admitted=admitted)
