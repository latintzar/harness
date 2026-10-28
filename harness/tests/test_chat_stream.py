"""A browser stream is a subscriber; the host owns the running investigation."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
from chat_stream import ChatStream


def test_disconnect_preserves_work_and_discards_detached_updates():
    async def scenario():
        released=asyncio.Event();saved=[]
        async def turn():
            yield {'type':'progress','text':'Started'}
            await released.wait()
            for n in range(300):
                saved.append(n)
                yield {'type':'delta','text':str(n)}
        stream=ChatStream(turn(),capacity=2)
        subscriber=stream.subscribe()
        assert (await anext(subscriber))['text']=='Started'
        await subscriber.aclose()
        released.set()
        await asyncio.wait_for(stream.task,1)
        assert len(saved)==300
        assert stream.queue.qsize()<=2
    asyncio.run(scenario())


def test_slow_connected_reader_receives_all_events_and_stop_cancels_work():
    async def scenario():
        async def turn():
            for n in range(50):
                yield {'type':'delta','text':str(n)}
        stream=ChatStream(turn(),capacity=2)
        received=[]
        async for event in stream.subscribe():
            received.append(event['text'])
            await asyncio.sleep(0)
        assert received==list(map(str,range(50)))
        await stream.task
        exited=asyncio.Event()
        async def waiting():
            try:
                yield {'type':'progress','text':'Started'}
                await asyncio.Event().wait()
            finally:exited.set()
        stream=ChatStream(waiting());subscriber=stream.subscribe()
        await anext(subscriber)
        stream.task.cancel()
        await asyncio.gather(stream.task,return_exceptions=True)
        assert exited.is_set()
        await subscriber.aclose()
    asyncio.run(scenario())
