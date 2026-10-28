"""Control races at the actual native JSON-RPC write boundary; no model calls."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent
from deploy.migration.app_server import AppServerConnection


class Protocol:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.sent = []
        self.written = asyncio.Event()
        self.reply = asyncio.Event()
        self.reply.set()

    def write(self, data):
        self.sent.append(json.loads(data))
        self.written.set()

    async def drain(self):
        await self.reply.wait()
        request = self.sent[-1]
        result = {'turn': {'id': 'next-turn'}} if request['method'] == 'turn/start' else {}
        self.stdout.feed_data((json.dumps({'id': request['id'], 'result': result}) + '\n').encode())


class Connection(AppServerConnection):
    def __init__(self, process):
        super().__init__(process)
        self.write_entered = asyncio.Event()

    async def write(self, value, *, guard=None):
        self.write_entered.set()
        await super().write(value, guard=guard)


@asynccontextmanager
async def controlled_run(tmp_path, *, native_active=True):
    agent = object.__new__(CodexVideoAgent)
    agent.state, agent.scopes = tmp_path, {}
    agent.children = {'chat': {'id': 'chat', 'messages': []}}
    agent._input = lambda message, *args: [{'type': 'text', 'text': message}]
    agent._save_child = lambda child: None
    agent._cancel_credential_waits = lambda *args: None
    agent.refresh_native_questions = lambda *args: None
    agent.native_questions = SimpleNamespace(retire_generation=lambda *args, **kwargs: [])
    stopping, stopped = asyncio.Event(), asyncio.Event()
    session = SimpleNamespace(_closing=False, stopped=False)

    async def stop():
        session._closing = True
        stopping.set()
        await stopped.wait()
        session.stopped = True

    session.stop = stop
    protocol = Protocol()
    connection = Connection(protocol)
    ready = asyncio.Event()
    ready.set()
    run = {'isolated': True, 'generation': 'generation', 'status': 'running',
           'cancel': asyncio.Event(), 'tool_events': asyncio.Queue(), 'isolated_session': session,
           'ready': ready, 'turn_id': 'turn', 'thread_id': 'thread', 'connection': connection,
           'native_turn_active': native_active}
    agent.active = {'chat': run}
    tasks = []

    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    try:
        yield agent, run, protocol, connection, stopping, stopped, start
    finally:
        stopped.set()
        protocol.reply.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        connection.reader.cancel()
        await asyncio.gather(connection.reader, return_exceptions=True)


@pytest.mark.parametrize('native_active', [True, False])
def test_steer_rejects_run_while_stop_is_draining(tmp_path, native_active):
    async def scenario():
        async with controlled_run(tmp_path, native_active=native_active) as (agent, run, protocol, _, stopping, stopped, start):
            stop = start(agent.control('chat', 'stop'))
            await asyncio.wait_for(stopping.wait(), 1)
            with pytest.raises((ValueError, RuntimeError)):
                await agent.control('chat', 'steer', 'Synthetic follow-up')
            assert protocol.sent == []
            assert run['status'] == 'stopping' and agent.children['chat']['messages'] == []
            assert not stop.done()
            stopped.set()
            assert (await stop)['result']['stopped']
    asyncio.run(scenario())


def test_steer_waiting_for_startup_is_fenced_by_stop(tmp_path):
    async def scenario():
        async with controlled_run(tmp_path) as (agent, run, protocol, _, stopping, stopped, start):
            run['ready'].clear()
            steer = start(agent.control('chat', 'steer', 'Synthetic follow-up'))
            await asyncio.sleep(0)
            stop = start(agent.control('chat', 'stop'))
            await asyncio.wait_for(stopping.wait(), 1)
            run['ready'].set()
            with pytest.raises((ValueError, RuntimeError)):
                await asyncio.wait_for(steer, 1)
            assert protocol.sent == [] and agent.children['chat']['messages'] == []
            stopped.set()
            await stop
    asyncio.run(scenario())


@pytest.mark.parametrize('native_active', [True, False])
@pytest.mark.parametrize('invalidation', ['stop', 'session', 'generation', 'replacement'])
def test_steer_rechecks_authority_under_native_write_lock(tmp_path, native_active, invalidation):
    async def scenario():
        async with controlled_run(tmp_path, native_active=native_active) as (agent, run, protocol, connection, stopping, stopped, start):
            await connection.write_lock.acquire()
            steer = start(agent.control('chat', 'steer', 'Synthetic follow-up'))
            await asyncio.wait_for(connection.write_entered.wait(), 1)
            stop = None
            if invalidation == 'stop':
                stop = start(agent.control('chat', 'stop'))
                await asyncio.wait_for(stopping.wait(), 1)
            elif invalidation == 'session':
                run['isolated_session']._closing = True
            elif invalidation == 'generation':
                run['generation'] = 'replacement-generation'
            else:
                agent.active['chat'] = dict(run)
            connection.write_lock.release()
            with pytest.raises((ValueError, RuntimeError)):
                await asyncio.wait_for(steer, 1)
            assert protocol.sent == [] and agent.children['chat']['messages'] == []
            assert connection.pending == {}
            if stop:
                stopped.set()
                await stop
    asyncio.run(scenario())


@pytest.mark.parametrize('native_active', [True, False])
def test_steer_reply_after_stop_does_not_restart_or_persist_input(tmp_path, native_active):
    async def scenario():
        async with controlled_run(tmp_path, native_active=native_active) as (agent, run, protocol, _, stopping, stopped, start):
            protocol.reply.clear()
            steer = start(agent.control('chat', 'steer', 'Synthetic follow-up'))
            await asyncio.wait_for(protocol.written.wait(), 1)
            stop = start(agent.control('chat', 'stop'))
            await asyncio.wait_for(stopping.wait(), 1)
            protocol.reply.set()
            with pytest.raises((ValueError, RuntimeError)):
                await asyncio.wait_for(steer, 1)
            assert len(protocol.sent) == 1
            assert run['status'] == 'stopping' and run['turn_id'] == 'turn'
            assert agent.children['chat']['messages'] == []
            stopped.set()
            await stop
    asyncio.run(scenario())


@pytest.mark.parametrize('native_active', [True, False])
def test_current_steer_and_waiting_child_restart_remain_usable(tmp_path, native_active):
    async def scenario():
        async with controlled_run(tmp_path, native_active=native_active) as (agent, run, protocol, _, _, _, _):
            assert (await agent.control('chat', 'steer', 'Synthetic follow-up'))['ok']
            assert [row['method'] for row in protocol.sent] == ['turn/steer' if native_active else 'turn/start']
            assert agent.children['chat']['messages'][-1]['content'] == 'Synthetic follow-up'
            assert run['turn_id'] == ('turn' if native_active else 'next-turn')
    asyncio.run(scenario())


def test_steer_keeps_native_skill_generation_guard(tmp_path, monkeypatch):
    import native_skills

    async def scenario():
        async with controlled_run(tmp_path) as (agent, run, protocol, connection, _, _, start):
            skill_current = True

            async def inputs(*args):
                run['native_skill_guard'] = lambda: skill_current
                return [{'type': 'skill', 'name': 'synthetic:notes', 'path': '/workspace/skills/notes/SKILL.md'}]

            monkeypatch.setattr(native_skills, 'native_skill_inputs', inputs)
            await connection.write_lock.acquire()
            steer = start(agent.control('chat', 'steer', 'Synthetic follow-up'))
            await asyncio.wait_for(connection.write_entered.wait(), 1)
            skill_current = False
            connection.write_lock.release()
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(steer, 1)
            assert protocol.sent == [] and agent.children['chat']['messages'] == []
    asyncio.run(scenario())
