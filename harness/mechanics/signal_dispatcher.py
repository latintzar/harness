"""One dispatcher routes saved signals to an agent job or an ordinary conversation."""
from __future__ import annotations

import asyncio
import json
import time


class SignalDispatcher:
    def __init__(self,store,agent,scheduler,slots,sources):
        self.store=store;self.agent=agent;self.scheduler=scheduler;self.slots=slots;self.sources=sources
        self.task=None;self.closing=False;self.next_recovery=0

    async def tick(self):
        if self.closing:return
        await asyncio.to_thread(self.sources.tick)
        if self.task is not None and not self.task.done():return
        if time.time()>=self.next_recovery:
            self.next_recovery=time.time()+30
            await self.recover_pending()
        if self.slots.locked():return
        with self.store.connect() as db:
            signals=[json.loads(row['body']) for row in db.execute(
                "SELECT body FROM agent_wakes WHERE status='pending' ORDER BY created_at,rowid LIMIT 1000")]
        for signal in signals:
            if signal.get('target_kind')!='chat':continue
            chat_id=signal['chat_id'];child=self.agent.children.get(chat_id)
            pending=self.agent.child_tasks.get(chat_id)
            if not child or chat_id in self.agent.active or pending is not None and not pending.done():continue
            if not self.store.signals._current(signal):
                with self.store.connect() as db:
                    self.store.wakes._retire(db,signal,'cancelled','execution_grant_ended',time.time())
                continue
            signal=await self.store.signals.process(signal)
            if signal is None:continue
            await self.slots.acquire()
            try:
                claim=self.store.wakes.claim_chat(signal['id'],owner=signal['owner_principal_id'])
                if claim is None:continue
                self.task=asyncio.create_task(self._execute(claim))
                self.agent.child_tasks[chat_id]=self.task
                self.task.add_done_callback(self._finished)
                return
            finally:
                if self.task is None or self.task.done():self.slots.release()
        await self.scheduler.tick()

    def _finished(self,task):
        self.slots.release()
        if not task.cancelled():task.exception()

    async def _execute(self,signal):
        child=self.agent.children[signal['chat_id']]
        start=len(child['messages']);status='uncertain';admitted=False
        try:
            if signal['chat_id'] in self.agent.active or not self.store.signals._current(signal):
                status='deferred';return
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');latest=self.store.wakes._get(db,signal['id'])
                latest['message_start']=start;self.store.wakes._write(db,latest)
            child['active_signal_id']=signal['id'];self.agent._save_child(child)
            async with asyncio.timeout(max(.01,min(1800,signal['expires_at']-time.time()))):
                async for event in self.agent.turn(signal.get('processed_text',signal['text']),signal['chat_id'],signal['collection'],
                        sender=signal['sender'],model_context=signal.get('payload',{}).get('model_context','')+'\nHost-authenticated signal origin (reference data): '+json.dumps({
                            'provider':signal['provider'],'event_id':signal['event_id'],'signal_id':signal['id']})):
                    if event.get('type')=='done':
                        status='completed' if event.get('status') in {'completed','waiting_for_reply'} else 'cancelled'
        except asyncio.CancelledError:
            status='uncertain' if self.closing else 'cancelled';raise
        except Exception:
            status='uncertain'
        finally:
            admitted=any(message.get('channel',{}).get('admission_key')==signal['sender'].get('admission_key')
                and message.get('role')=='user' for message in child['messages'][start:])
            if not admitted and len(child['messages'])>start:
                admitted=child['messages'][start].get('role')=='user'
            if status=='deferred' and admitted:status='uncertain'
            self.store.wakes.finish_chat(signal['id'],signal['run_id'],owner=signal['owner_principal_id'],
                status=status,admitted=admitted)
            if status!='uncertain' or not admitted:child.pop('active_signal_id',None)
            self.agent._save_child(child)
            if self.agent.child_tasks.get(signal['chat_id']) is asyncio.current_task():
                self.agent.child_tasks.pop(signal['chat_id'],None)

    async def recover_pending(self):
        runtime=getattr(self.agent,'managed_runtime',None)
        if not hasattr(runtime,'recover_request'):return
        with self.store.connect() as db:
            signals=[json.loads(row['body']) for row in db.execute(
                "SELECT body FROM agent_wakes WHERE status IN ('claimed','uncertain')")]
        for signal in signals:
            if signal.get('target_kind')!='chat':continue
            child=self.agent.children.get(signal['chat_id'])
            if not child:continue
            if signal['chat_id'] in self.agent.active:continue
            messages=child.get('messages',[]);start=signal.get('message_start')
            original=messages[start] if type(start) is int and 0<=start<len(messages) else {}
            logical_id=original.get('managed_request_id') if original.get('role')=='user' else None
            if not logical_id:
                if 'message_start' not in signal:
                    with self.store.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        self.store.signals.release_unstarted(db,signal['id'])
                        signal.update(status='pending',run_id=None)
                        self.store.wakes._write(db,signal)
                else:
                    with self.store.connect() as db:
                        self.store.wakes._retire(db,signal,'uncertain','reconciliation_required',time.time())
                    child['active_signal_id']=signal['id'];self.agent._save_child(child)
                continue
            # A withdrawn grant enters cancellation-only provider recovery; its
            # admitted work keeps the workspace slot until quiescence is proven.
            signal['status']='claimed'
            with self.store.connect() as db:self.store.wakes._write(db,signal)
            child['active_signal_id']=signal['id'];self.agent._save_child(child)
            status='uncertain'
            try:
                result=await runtime.recover_request(self.agent,signal['chat_id'],logical_id)
                status=result['status'] if result['status'] in {'completed','failed','cancelled'} else (
                    'cancelled' if result['status']=='missing' else 'uncertain')
                self.store.wakes.finish_chat(signal['id'],signal['run_id'],owner=signal['owner_principal_id'],status=status)
            finally:
                if status!='uncertain':child.pop('active_signal_id',None)
                self.agent._save_child(child)

    async def close(self):
        self.closing=True
        if self.task is not None and not self.task.done():
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
