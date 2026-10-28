"""Migration reads saved public history; active controls use the managed runtime."""
import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest


def test_migration_transport_reads_exact_saved_history_and_closes_process():
    connection_type = importlib.import_module('deploy.migration.app_server').AppServerConnection
    async def scenario():
        class Process:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stdin = self
                self.returncode = None
                self.requests = []
            def write(self, payload):
                value = json.loads(payload)
                self.requests.append(value)
                self.stdout.feed_data((json.dumps({'id': value['id'], 'result': {
                    'thread': {'id': value['params']['threadId'], 'turns': []}}}) + '\n').encode())
            async def drain(self):
                pass
            def terminate(self):
                self.returncode = 0
            async def wait(self):
                return self.returncode
        process = Process()
        connection = connection_type(process)
        assert await connection.rpc('thread/read', {'threadId': 'saved', 'includeTurns': True}) == {
            'thread': {'id': 'saved', 'turns': []}}
        await connection.close()
        assert process.returncode == 0 and not connection.pending
        assert [request['method'] for request in process.requests] == ['thread/read']
    asyncio.run(scenario())


def test_migration_history_keeps_public_messages_and_original_owner(tmp_path):
    history = importlib.import_module('deploy.migration.history')
    source = {'id': 'old-thread', 'cwd': str(tmp_path), 'status': {'type': 'idle'}, 'turns': [
        {'status': 'completed', 'items': [
            {'type': 'userMessage', 'id': 'message', 'content': [{'type': 'text', 'text': 'Original question'}]},
            {'type': 'agentMessage', 'text': 'Original answer'},
            {'type': 'reasoning', 'text': 'Private reasoning'}]}]}
    saved = history.history_export(source, state=tmp_path, chat_id='chat', owner_principal_id='owner')
    assert saved['owner_principal_id'] == 'owner' and saved['source_thread_id'] == 'old-thread'
    assert [item['role'] for item in saved['items']] == ['user', 'assistant']
    assert 'Private reasoning' not in json.dumps(saved)


def test_bridge_refuses_a_local_core_control_record(tmp_path):
    from agent_bridge import HarnessAgent
    async def scenario():
        agent = HarnessAgent(tmp_path)
        agent.children['chat'] = {'id': 'chat', 'owner_principal_id': 'owner', 'messages': []}
        called = []
        async def rpc(*args, **kwargs):
            called.append(args)
            return {}
        ready = asyncio.Event(); ready.set()
        agent.active['chat'] = {'connection': SimpleNamespace(rpc=rpc), 'ready': ready,
            'thread_id': 'old-thread', 'turn_id': 'old-turn', 'cancel': asyncio.Event()}
        with pytest.raises(ValueError, match='managed'):
            await agent.control('chat', 'stop')
        assert called == []
    asyncio.run(scenario())


def test_current_registry_exposes_managed_delegation_only():
    from agent_bridge import tool_specs
    names = {spec['name'] for spec in tool_specs()}
    assert 'delegate_job' in names
    assert not {'delegate_investigations', 'wait_investigations'} & names


def test_export_uses_migration_config_and_reads_only_registered_public_history(tmp_path, monkeypatch):
    from deploy import migrate_state
    source = tmp_path / 'old'
    source.mkdir()
    (source / 'chats').mkdir()
    (source / 'agent-chats.json').write_text(json.dumps({'chat': 'old-thread'}))
    (source / 'chats/chat.json').write_text(json.dumps({'id': 'chat', 'owner_principal_id': 'owner'}))
    calls = []
    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdin = self
            self.returncode = None
        def write(self, payload):
            request = json.loads(payload)
            calls.append(request)
            if 'id' not in request:
                return
            method = request['method']
            assert method in {'initialize', 'thread/read'}
            result = {} if method == 'initialize' else {'thread': {
                'id': 'old-thread', 'cwd': str(source), 'status': {'type': 'idle'},
                'turns': [{'status': 'completed', 'items': [
                    {'type': 'agentMessage', 'text': 'Original public answer'}]}]}}
            self.stdout.feed_data((json.dumps({'id': request['id'], 'result': result}) + '\n').encode())
        async def drain(self):
            pass
        def terminate(self):
            self.returncode = 0
        async def wait(self):
            return self.returncode
    async def launch(*args, **kwargs):
        assert args[:4] == ('fixture-codex', 'app-server', '--listen', 'stdio://')
        assert 'web_search="disabled"' in args
        assert not any('model_catalog' in str(value) for value in args)
        assert kwargs['cwd'] == str(source)
        return Process()
    monkeypatch.setattr(migrate_state.asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(migrate_state, 'inventory', lambda state: {'workspaces': ['.']})
    result = asyncio.run(migrate_state.export_native(source, tmp_path / 'export', codex_bin='fixture-codex'))
    assert result['exported_threads'] == 1 and result['model_calls'] == 0
    assert [request['method'] for request in calls] == ['initialize', 'initialized', 'thread/read']
    assert calls[-1]['params'] == {'threadId': 'old-thread', 'includeTurns': True}
    assert not (source / 'native-policy').exists()
