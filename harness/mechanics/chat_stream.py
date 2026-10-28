"""Bounded live subscriber for a host-owned, persistently recorded native turn."""
import asyncio
import contextlib


class ChatStream:
    def __init__(self, events, *, capacity=128):
        self.queue=asyncio.Queue(maxsize=capacity)
        self.detached=asyncio.Event()
        self.finished=asyncio.Event()
        self.task=asyncio.create_task(self._run(events))

    async def _publish(self,event):
        while not self.detached.is_set():
            try:
                await asyncio.wait_for(self.queue.put(event),timeout=.2)
                return
            except TimeoutError:
                continue

    async def _run(self,events):
        try:
            async for event in events:
                await self._publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._publish({'type':'error','message':'Open the saved chat to review its progress and continue.'})
        finally:
            await events.aclose()
            self.finished.set()

    async def subscribe(self):
        try:
            while not self.finished.is_set() or not self.queue.empty():
                try:
                    event=await asyncio.wait_for(self.queue.get(),timeout=.2)
                except TimeoutError:
                    continue
                yield event
        finally:
            self.detached.set()
            while not self.queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):self.queue.get_nowait()
