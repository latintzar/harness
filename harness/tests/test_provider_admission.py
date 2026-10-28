"""Cross-process authority fencing and cancellation; synthetic credentials only."""
import asyncio
from contextlib import asynccontextmanager
import json
import multiprocessing
from pathlib import Path
import sys
import time

import pytest
from mcp.shared.auth import OAuthToken
sys.path.insert(0,str(Path(__file__).parents[1]))
from mcp_connections import MCPConnections,ConnectionError,_Storage


async def connected(state,mode='read_write'):
    broker=MCPConnections(state,public_origin='https://rocoto.example')
    generation=broker._begin('notion','alice',mode)
    await _Storage(broker,'alice','notion',generation).set_tokens(OAuthToken(access_token='host-only-private-token',scope='default',expires_in=300))
    broker._update('notion','alice',generation,status='connected')
    return broker,generation


def test_fixed_credential_requires_same_task_fence_and_validity(tmp_path):
    async def scenario():
        broker,generation=await connected(tmp_path)
        with pytest.raises(ConnectionError):broker.dispatch_credentials('notion',principal_id='alice',expected_generation=generation)
        async with broker.admission('notion',principal_id='alice',expected_generation=generation):
            value=broker.dispatch_credentials('notion',principal_id='alice',expected_generation=generation)
            assert value.access_token=='host-only-private-token'and 'private-token'not in repr(value)
            assert value.oauth_scopes==('default',)and value.access_mode=='read_write'
            async def child():
                with pytest.raises(ConnectionError):broker.dispatch_credentials('notion',principal_id='alice',expected_generation=generation)
            await asyncio.create_task(child())
            with broker._db()as db:db.execute('UPDATE connections_v2 SET expires_at=?',(time.time()+40,))
            with pytest.raises(ConnectionError):broker.dispatch_credentials('notion',principal_id='alice',expected_generation=generation)
        await broker.close()
    asyncio.run(scenario())


def test_disconnect_closes_admission_then_drains_accepted_fixed_token_work(tmp_path):
    async def scenario():
        broker,generation=await connected(tmp_path);entered=asyncio.Event();release=asyncio.Event();completed=[]
        async def effect():
            async with broker.admission('notion',principal_id='alice',expected_generation=generation):
                credential=broker.dispatch_credentials('notion',principal_id='alice',expected_generation=generation)
                entered.set();await release.wait();completed.append(credential.connection_generation)
        running=asyncio.create_task(effect());await entered.wait()
        disconnect=asyncio.create_task(broker.disconnect('notion',principal_id='alice'))
        for _ in range(20):
            if broker._record('notion','alice')['status']=='disconnecting':break
            await asyncio.sleep(.01)
        assert broker._record('notion','alice')['generation']==generation+1
        assert not disconnect.done()and not running.done()
        async def denied():
            with pytest.raises(ConnectionError):
                async with broker.admission('notion',principal_id='alice',expected_generation=generation):raise AssertionError('Stale dispatch')
        queued=asyncio.create_task(denied())
        disconnect.cancel();await asyncio.sleep(.01);disconnect.cancel();await asyncio.sleep(.01)
        assert not disconnect.done()
        release.set();await running;await queued
        with pytest.raises(asyncio.CancelledError):await disconnect
        assert completed==[generation]and broker._record('notion','alice')['status']=='disconnected'
        await broker.close()
    asyncio.run(scenario())


def _cross_process_holder(state,generation,entered,release):
    async def scenario():
        broker=MCPConnections(state,public_origin='https://rocoto.example')
        async with broker.admission('notion',principal_id='alice',expected_generation=generation):
            entered.set();await asyncio.to_thread(release.wait,10)
        await broker.close()
    asyncio.run(scenario())


def test_cross_process_fence_and_cancelled_waiter_cannot_release_another_job(tmp_path):
    async def scenario():
        broker,generation=await connected(tmp_path)
        context=multiprocessing.get_context('spawn');entered=context.Event();release=context.Event()
        process=context.Process(target=_cross_process_holder,args=(tmp_path,generation,entered,release));process.start()
        try:
            assert await asyncio.to_thread(entered.wait,5)
            admitted=[]
            async def attempt():
                async with broker.admission('notion',principal_id='alice',expected_generation=generation):admitted.append(True)
            waiter=asyncio.create_task(attempt());await asyncio.sleep(.05);assert not waiter.done()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):await waiter
            successor=asyncio.create_task(attempt());await asyncio.sleep(.05)
            assert not successor.done()and not admitted
            release.set();await asyncio.wait_for(successor,5)
            assert admitted==[True]
        finally:
            release.set();await asyncio.to_thread(process.join,5)
            if process.is_alive():process.kill();process.join()
        assert process.exitcode==0
        await broker.close()
    asyncio.run(scenario())


def test_public_reconnect_waits_for_admitted_work_and_bumps_generation(tmp_path,monkeypatch):
    from mcp import types
    async def scenario():
        broker,generation=await connected(tmp_path,'read')
        class Session:
            async def initialize(self):pass
            async def list_tools(self,cursor=None):return types.ListToolsResult(tools=[types.Tool(name='notion-fetch',inputSchema={'type':'object'})])
        @asynccontextmanager
        async def session(*args,**kwargs):yield Session()
        monkeypatch.setattr(broker,'_session',session)
        async with broker.admission('notion',principal_id='alice',expected_generation=generation):
            task=asyncio.create_task(broker.start('notion',principal_id='alice',session_binding='browser',access_mode='read_write'))
            await asyncio.sleep(.03);assert not task.done()
        assert (await task)['status']=='connected'
        row=broker._record('notion','alice');assert row['generation']==generation+1 and row['access_mode']=='read_write'
        async with broker.admission('notion',principal_id='alice',expected_generation=row['generation']):
            with pytest.raises(ConnectionError):broker.dispatch_credentials('notion',principal_id='alice',expected_generation=row['generation'])
        await broker.close()
    asyncio.run(scenario())


def test_older_disconnect_completion_does_not_open_newer_disconnect_admission(tmp_path,monkeypatch):
    async def scenario():
        broker,generation=await connected(tmp_path);first_gate=asyncio.Event();second_gate=asyncio.Event();count=0
        async def cancel_flows(*args):
            nonlocal count
            count+=1
            await(first_gate if count==1 else second_gate).wait()
        monkeypatch.setattr(broker,'_cancel_flows',cancel_flows)
        first=asyncio.create_task(broker.disconnect('notion',principal_id='alice'))
        while count<1:await asyncio.sleep(.001)
        second=asyncio.create_task(broker.disconnect('notion',principal_id='alice'))
        while count<2:await asyncio.sleep(.001)
        first_gate.set();await first
        assert broker._record('notion','alice')['status']=='disconnecting'
        assert broker._record('notion','alice')['generation']==generation+2
        second_gate.set();await second
        assert broker._record('notion','alice')['status']=='disconnected'
        await broker.close()
    asyncio.run(scenario())


def test_dispatch_admission_uses_shared_read_capacity(tmp_path):
    async def scenario():
        broker,generation=await connected(tmp_path)
        for _ in range(8):await broker._slots.acquire()
        entered=[]
        async def effect():
            async with broker.dispatch_admission('notion',principal_id='alice',expected_generation=generation):
                entered.append(broker.dispatch_credentials('notion',principal_id='alice',expected_generation=generation).provider)
        task=asyncio.create_task(effect());await asyncio.sleep(.03);assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert not entered
        for _ in range(8):broker._slots.release()
        await effect();assert entered==['notion']
        await broker.close()
    asyncio.run(scenario())
