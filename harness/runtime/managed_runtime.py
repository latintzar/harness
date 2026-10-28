"""Funded managed turns, session recovery and controls for the shared harness.

The policy owns credentials, spending reservations and scoped remote MCP leases.
This adapter owns provider protocol ordering; ordinary chat files own the public
conversation, including the pending input identity needed after a process restart.
"""
from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import time
import uuid

from managed_codex import ManagedCodexError, ManagedEvent
from managed_sessions import ManagedSessions, TERMINAL
from managed_turn import ManagedTurn
from managed_children import ManagedChildren
from scope_host import current_admission
from workspace_identity import workspace_scope


async def resolved(value):
    return await value if inspect.isawaitable(value) else value


def function_tools(registry):
    """Delegation belongs to OpenAI's stock coordinator tools."""
    return [{'type': 'function', 'name': spec['name'],
             'description': spec.get('description', ''), 'parameters': spec['inputSchema']}
            for spec in registry.specs() if spec['name'] not in
            {'delegate_investigations', 'wait_investigations'}]


def portable_content(content, state):
    result, total = [], 0
    root = (Path(state) / 'attachments').resolve()
    for part in content:
        if part.get('type') == 'text':
            result.append({'type': 'input_text', 'text': part['text']})
        elif part.get('type') == 'localImage':
            path = Path(part.get('path', '')).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('Choose an uploaded image in this workspace.')
            with path.open('rb') as stream:
                data = stream.read(4 * 1024 * 1024 + 1)
            total += len(data)
            if (not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 4 * 1024 * 1024
                    or total > 8 * 1024 * 1024):
                raise ValueError('Choose PNG attachments within the image allowance.')
            result.append({'type': 'input_image', 'image_url':
                           'data:image/png;base64,' + base64.b64encode(data).decode()})
        else:
            raise ValueError('Use supported managed input content.')
    return result


async def pages(fetch, *, limit=100):
    values, after, seen = [], None, set()
    for _ in range(limit):
        page = await fetch(after=after, limit=100, order='asc')
        rows = page.get('data')
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError('Read complete provider state before continuing.')
        values.extend(rows)
        if page.get('has_more') is False:
            return values
        after = page.get('last_id')
        if not isinstance(after, str) or not after or after in seen:
            raise ValueError('Read each provider state page once.')
        seen.add(after)
    raise ValueError('Read provider state within the history allowance.')


async def quiescent(client, session_id):
    """An available subagent may be idle; its saved turns decide quiescence."""
    session = await client.get_session(session_id)
    if session.get('id') != session_id:
        raise ValueError('Read the bound provider session.')
    if session.get('status') not in {'idle', 'failed'} or session.get('required_actions'):
        return False, session
    turns = await pages(lambda **params: client.list_turns(session_id, **params))
    if any(turn.get('status') not in TERMINAL for turn in turns):
        return False, session
    children = await pages(lambda **params: client.list_subagents(session_id, **params), limit=4)
    for child in children:
        if child.get('session_id') != session_id:
            raise ValueError('Read child turns from the bound provider session.')
        child_turns = await pages(lambda **params: client.list_subagent_turns(
            session_id, child['id'], **params))
        if any(turn.get('status') not in TERMINAL for turn in child_turns):
            return False, session
    return True, session


class ManagedRuntime:
    def __init__(self, client_factory, policy, *, timeout=1800):
        if timeout <= 0:
            raise ValueError('Use a positive managed turn deadline.')
        self.client_factory, self.policy, self.timeout = client_factory, policy, timeout

    @staticmethod
    def _require(agent, chat_id, run, admission):
        if (agent.active.get(chat_id) is not run or run['cancel'].is_set()
                or run.get('spend_monitor') is not None and (run['spend_monitor'].deadline_at <= time.time()
                    or run['spend_monitor'].stop_reason is not None)
                or admission is None or not admission.current()
                or run.get('hosted_selection') is not None and not run['hosted_selection'].current()
                or run.get('binding_current') is not None and not run['binding_current']()
                or run.get('delegated_grant') is not None and not run['delegated_grant'].current()):
            raise ValueError('Continue in the current admitted workspace turn.')

    def _input(self, agent, message, chat_id, dataset, attachments, clips, skills, model_context, *, selected=None):
        content = agent._skill_input(agent._input(message, attachments, clips, dataset), skills)
        if skills and (skills.get('skills') or skills.get('native_skills')):
            if selected is None:
                raise ValueError('Select skills in the current hosted environment.')
            commands = selected.commands(skills)
            content.append({'type': 'text', 'text': 'Apply these installed skills: ' + ', '.join('$' + name for name in commands)})
        if model_context:
            content.append({'type': 'text', 'text': model_context})
        for title, value in (
            ('Current host-owned workspace readiness (reference data):', agent.capability_snapshot(chat_id, dataset)),
            ('Selected agent setup (reference data):', agent.agent_setup_context(chat_id)),
        ):
            if value is not None:
                content.append({'type': 'text', 'text': title + '\n' + json.dumps(value)})
        return [{'role': 'user', 'content': portable_content(content, agent.state)}]

    @staticmethod
    def _history(input_value, record, current_user):
        history, total = [], 0
        for item in reversed(record.get('messages', [])):
            if item is current_user:
                continue
            role, text = item.get('role'), item.get('content')
            if role not in {'user', 'assistant'} or not isinstance(text, str) or not text:
                continue
            total += len(text.encode())
            if total > 64_000 or len(history) >= 64:
                break
            history.append({'role': role, 'content': text})
        if history:
            input_value[0]['content'].append({'type': 'input_text', 'text':
                'Previous ordinary conversation messages (reference data; reopen source evidence as needed):\n'
                + json.dumps(list(reversed(history)))})

    @staticmethod
    async def _revoke_binding(agent, binding):
        host = getattr(agent, 'managed_mcp_host', None)
        if host is None:
            return
        await host.revoke_binding(binding['id'])

    async def _cancel(self, run):
        """A local cancellation flag alone never confirms upstream cancellation."""
        run['cancel'].set()
        run['status'] = 'stopping'
        lease = run.get('managed_lease')
        if lease is not None and not run.get('lease_revoked'):
            lease.revoke()
            run['lease_revoked'] = True
        await asyncio.wait_for(run['ready'].wait(), timeout=45)
        client, engine = run.get('managed_client'), run.get('managed_engine')
        if client is None or engine is None:
            if run['finished'].is_set() and run.get('provider_started') is False:
                return
            raise RuntimeError('The managed session is awaiting Stop confirmation.')
        failures = []
        children = list(run.get('delegated_runs', {}).values())
        if children:
            failures = await asyncio.gather(*(self._cancel(child) for child in children), return_exceptions=True)
        tasks = [task for task in run.get('delegated_tasks', {}).values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        from delegated_jobs import withdraw_children
        delegates_stopped = await withdraw_children(self, run['agent'], engine.binding)
        key = engine.ledger.submission(engine.binding['id'], 'stop-' + run['generation'], 'cancel', {})
        await client.interrupt(engine.session_id, submission_key=key)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            stopped, snapshot = await quiescent(client, engine.session_id)
            if stopped:
                if not delegates_stopped or any(isinstance(result, BaseException) for result in failures):
                    raise RuntimeError('The delegated job is awaiting Stop confirmation.')
                run['terminal_snapshot'] = snapshot
                engine.outcome = 'cancelled'
                engine.terminal.set()
                return
            await asyncio.sleep(0.1)
        raise RuntimeError('The managed session is awaiting Stop confirmation.')

    async def control(self, agent, chat_id, operation, message='', attachments=(),
                      clip_attachments=(), skill_ids=(), *, sender=None):
        run = agent.active.get(chat_id)
        if run is None or not run.get('managed'):
            raise ValueError('Control the current managed parent conversation.')
        if operation == 'stop':
            await self._cancel(run)
            return {'ok': True, 'result': {'stopped': True}}
        # Every steering message is paid input and goes through a fresh policy
        # reservation. The original captured admission remains authoritative.
        admission = run['admission']
        self._require(agent, chat_id, run, admission)
        admitted = getattr(agent, '_turn_senders', {}).get(chat_id, {})
        if sender is not None and sender.get('principal_id') != admitted.get('principal_id'):
            raise ValueError('Steer through the current admitted conversation member.')
        skills = agent.resolve_skills(skill_ids, chat_id) if skill_ids else None
        input_value = self._input(agent, message, chat_id, agent.children[chat_id]['dataset'],
                                  attachments, clip_attachments, skills, '', selected=run.get('hosted_selection'))
        engine, client = run['managed_engine'], run['managed_client']
        logical_id = 'steer-' + uuid.uuid4().hex
        key = engine.ledger.submission(engine.binding['id'], logical_id, 'steer', input_value)
        record = {'role': 'user', 'content': message, 'steer': True,
                  'managed_request_id': logical_id, 'managed_submission': 'pending',
                  **({'sender_principal_id': sender['principal_id'], 'channel': copy.deepcopy(sender)} if sender else {})}
        agent.children[chat_id]['messages'].append(record)
        agent._save_child(agent.children[chat_id])
        lease = await run['lifetime'].enter_async_context(self.policy.turn(
            agent, chat_id, run['tools'], run['instructions'], session_id=engine.session_id,
            request_id=key, binding_id=engine.binding['id'], admission=admission, run=run, steering=True))
        run.setdefault('steering_leases', []).append(lease)
        self._require(agent, chat_id, run, admission)
        await self._update(client, engine, lease.configuration, key)
        self._require(agent, chat_id, run, admission)
        content = input_value[0]['content']
        text = '\n'.join(part['text'] for part in content if part['type'] == 'input_text')
        images = tuple(part['image_url'] for part in content if part['type'] == 'input_image')
        await client.send_message(engine.session_id, text, images=images, submission_key=key)
        self._require(agent, chat_id, run, admission)
        record['managed_submission'] = 'accepted'
        agent._save_child(agent.children[chat_id])
        return {'ok': True, 'result': {'queued': True}}

    @staticmethod
    async def _update(client, engine, configuration, key):
        agent = configuration.get('agent', {})
        settings = {name: value for name, value in agent.items() if name in {'model', 'service_tier'}}
        reasoning = agent.get('reasoning')
        # The update schema accepts effort; summary belongs to creation only.
        if isinstance(reasoning, dict) and 'effort' in reasoning:
            settings['reasoning'] = {'effort': reasoning['effort']}
        update = {'agent': settings}
        if 'spend_control' in configuration:
            update['spend_control'] = configuration['spend_control']
        await client.update_session(engine.session_id, update, submission_key=key + '-allowance')

    async def _observe(self, client, engine, *, created, input_value, submission_key, baseline):
        sent = created or engine.request is not None and engine.request['status'] == 'submitted'
        for attempt in range(4):
            if engine.request is not None:
                delivery = engine.ledger.delivery_boundary(engine.binding['id'], engine.request['id'])
                if delivery is not None:
                    engine.require_current()
                    yield {'type': 'channel_delivery', 'delivery': delivery}
                    return
            try:
                async with client.recover_session(engine.session_id) as recovery:
                    engine.accept_session(recovery.session)
                    roots_now = [turn for turn in recovery.turns if turn['id'] not in baseline
                                 and turn.get('subagent_id', 'missing') is None]
                    if sent and not created and not roots_now and recovery.session.get('status') == 'idle':
                        # Retrying the original input uses its exact persisted idempotency key.
                        sent = False
                    if not sent:
                        if recovery.session.get('status') not in {'idle', 'failed'}:
                            raise ValueError('Recover the preceding managed turn before sending another message.')
                        baseline.update(turn['id'] for turn in recovery.turns)
                        engine.ignored_turns = set(baseline)
                        engine.require_current()
                        content = input_value[0]['content']
                        text = '\n'.join(part['text'] for part in content if part['type'] == 'input_text')
                        images = tuple(part['image_url'] for part in content if part['type'] == 'input_image')
                        engine.ledger.request_state(engine.request['id'], 'submitted', baseline=sorted(baseline))
                        sent = True
                        await client.send_message(engine.session_id, text, images=images, submission_key=submission_key)
                    selected = [turn for turn in recovery.turns if turn['id'] not in baseline]
                    for event in await engine.snapshot(recovery.session, recovery.items, recovery.turns,
                                                      turn_ids={turn['id'] for turn in selected}):
                        yield event
                    if engine.children is not None:
                        for event in await engine.children.recover_history():
                            yield event
                    roots = [turn for turn in selected if turn.get('subagent_id', 'missing') is None]
                    if roots:
                        latest = roots[-1]
                        engine.root_turn_id = latest['id']
                        if latest['status'] in TERMINAL:
                            for event in await engine.event(ManagedEvent({'type': 'agent.session.turn.' + latest['status'],
                                    'session_id': engine.session_id, 'turn': latest})):
                                yield event
                            return
                    async for event in engine.required_actions():
                        yield event
                    stream = recovery.events.__aiter__()
                    pending = asyncio.create_task(anext(stream, None))
                    terminal = asyncio.create_task(engine.terminal.wait())
                    try:
                        while not engine.terminal.is_set():
                            done, _ = await asyncio.wait({pending, terminal}, timeout=1,
                                                        return_when=asyncio.FIRST_COMPLETED)
                            if terminal in done:
                                break
                            if not done:
                                engine.require_current()
                                if engine.run.get('managed_environment_pending'):
                                    async for value in engine.required_actions():
                                        yield value
                                continue
                            event = pending.result()
                            if event is None:
                                break
                            if event.category == 'required_action':
                                async for value in engine.required_actions():
                                    yield value
                            else:
                                for value in await engine.event(event):
                                    yield value
                            if not engine.terminal.is_set():
                                pending = asyncio.create_task(anext(stream, None))
                        if engine.terminal.is_set():
                            return
                    finally:
                        pending.cancel()
                        terminal.cancel()
                        await asyncio.gather(pending, terminal, return_exceptions=True)
            except ManagedCodexError as error:
                if error.code not in {'stream_disconnected', 'transport_error', 'incomplete_event'}:
                    raise
            if attempt == 3:
                raise ManagedCodexError('recovery_attempts_exhausted')

    async def _project(self, observer, run):
        """Publish host prompts while a provider function awaits their answer."""
        stream = observer.__aiter__()
        next_event = asyncio.create_task(anext(stream, None))
        notice = asyncio.create_task(run['tool_events'].get())
        terminal = asyncio.create_task(run['managed_engine'].terminal.wait())
        try:
            while True:
                waiting = {next_event, notice, terminal}
                monitor = run.get('monitor_task')
                if monitor is not None:
                    waiting.add(monitor)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if monitor is not None and monitor in done:
                    monitor.result()
                    return
                if notice in done:
                    yield notice.result()
                    notice = asyncio.create_task(run['tool_events'].get())
                if next_event in done:
                    try:
                        event = next_event.result()
                    except asyncio.CancelledError:
                        engine = run.get('managed_engine')
                        if run['cancel'].is_set() and engine is not None:
                            await asyncio.wait_for(engine.terminal.wait(), timeout=45)
                            return
                        raise
                    except ValueError:
                        engine = run.get('managed_engine')
                        if run['cancel'].is_set() and engine is not None and engine.terminal.is_set():
                            return
                        raise
                    if event is None:
                        while not run['tool_events'].empty():
                            yield run['tool_events'].get_nowait()
                        return
                    yield event
                    next_event = asyncio.create_task(anext(stream, None))
                if terminal in done:
                    return
        finally:
            next_event.cancel()
            notice.cancel()
            terminal.cancel()
            await asyncio.gather(next_event, notice, terminal, return_exceptions=True)
            await stream.aclose()

    @staticmethod
    async def _stop_monitor(run):
        task = run.pop('monitor_task', None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _harvest_files(self, agent, client, chat_id, *, current, answer=None, binding_id=None):
        from managed_files import ManagedFiles
        if getattr(agent, 'file_store', None) is None:
            return []
        ledger = ManagedSessions(agent.state)
        record = agent.children[chat_id]
        pending, events = False, []
        publisher = ManagedFiles(agent, ledger, client, current=current)
        for binding in ledger.generations(workspace_scope(agent.state), chat_id):
            config = binding['configuration_body'] or {}
            if binding['session_id'] is None or config.get('environment', {}).get('type') != 'openai_hosted':
                continue
            try:
                async with asyncio.timeout(30):
                    artifacts = await publisher.harvest(binding)
                for event in artifacts:
                    if not current():
                        raise ValueError('Display files through current workspace access.')
                    target = next((message for message in record.get('messages', [])
                        if message.get('role') == 'assistant'
                        and message.get('managed_session_id') == event['managed_session_id']
                        and message.get('managed_turn_id') == event['managed_turn_id']), None)
                    if target is None and binding['id'] == binding_id:
                        target = answer
                    public = {'type': 'artifact', 'artifact': event['artifact']}
                    if target is not None:
                        agent._record_event(target, public)
                    events.append(public)
            except (ManagedCodexError, RuntimeError, ValueError, OSError, TimeoutError):
                pending = True
        if pending:
            record['managed_artifact_recovery_required'] = True
        else:
            record.pop('managed_artifact_recovery_required', None)
        agent._save_child(record)
        return events

    async def recover_files(self, agent, chat_id):
        """Read retained generations without sending input or reserving new funds."""
        if getattr(agent, 'file_store', None) is None or chat_id in agent.active:
            return
        record = agent.children.get(chat_id, {})
        if record.get('parent_id') or not record.get('owner_principal_id'):
            return
        admission = current_admission()
        if admission is None:
            from scope_host import agent_scope
            user = next((message for message in reversed(record.get('messages', []))
                         if message.get('role') == 'user'), {})
            sender = user.get('channel') or {'principal_id': record['owner_principal_id'], 'channel': 'web'}
            async with agent_scope(agent, chat_id, record['dataset'], sender=sender, admit=True):
                async for event in self.recover_files(agent, chat_id):
                    yield event
            return
        if not admission.current():
            raise ValueError('Recover files through current workspace access.')
        async with self.client_factory() as client:
            for event in await self._harvest_files(agent, client, chat_id, current=admission.current):
                if not admission.current():
                    raise ValueError('Display files through current workspace access.')
                yield event

    async def run(self, agent, message, chat_id, dataset, attachments=(), clip_attachments=(),
                  resolved_skills=None, *, model_context='', instructions=None, recovering=False, recovery_answer=None,
                  delegated_grant=None):
        from agent_bridge import system_instructions, workspace_registry
        from intake_instructions import for_chat
        from delegated_jobs import DelegatedGrant, role_plan
        admission = current_admission()
        record = agent.children.get(chat_id)
        delegated = (isinstance(delegated_grant, DelegatedGrant) and delegated_grant.agent is agent
            and delegated_grant.job['chat_id'] == chat_id and delegated_grant.current())
        if delegated_grant is not None and not delegated:
            raise ValueError('Use the host-issued current declared job grant.')
        if (not record or record.get('dataset') != dataset or record.get('parent_id') and not delegated
                or chat_id in agent.active or admission is None or not admission.current()):
            raise ValueError('Start a managed turn in an admitted parent conversation.')
        registry = workspace_registry(agent)
        tools = function_tools(registry)
        if delegated:
            if not set(delegated_grant.role['tools']) <= registry.reads:
                raise ValueError('Run declared jobs with available read tools.')
            tools = [tool for tool in tools if tool['name'] in delegated_grant.role['tools']]
            instructions = delegated_grant.role['instructions']
        else:
            instructions = instructions if instructions is not None else ('' if recovering else for_chat(agent, chat_id, system_instructions()))
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        run = {'agent': agent, 'managed': True, 'ready': asyncio.Event(), 'finished': asyncio.Event(),
               'cancel': asyncio.Event(), 'status': 'starting', 'generation': uuid.uuid4().hex,
               'admission': admission, 'deadline': started + self.timeout, 'tool_events': asyncio.Queue(),
               'child_ids': set(), 'tools': tools, 'instructions': instructions, 'provider_started': False}
        if delegated:
            run['delegated_grant'] = delegated_grant
            run['deadline'] = min(run['deadline'], delegated_grant.parent_run['deadline'],
                                  started + max(0, delegated_grant.job['expires_at'] - time.time()))
            delegated_grant.parent_run.setdefault('delegated_runs', {})[chat_id] = run
        else:
            run['role_plan'] = role_plan(agent, chat_id)
        agent.active[chat_id] = run
        ledger = ManagedSessions(agent.state)
        lease = engine = request = binding = None
        snapshot = None
        terminal_status = None
        lifetime = AsyncExitStack()
        run['lifetime'] = lifetime
        try:
            from hosted_capabilities import HostedCapabilities
            company = workspace_scope(agent.state)
            owner = record['owner_principal_id']
            builder = HostedCapabilities(agent.state, getattr(agent, 'plugin_package_store', None))
            pending_binding = ledger.pending_binding(company, chat_id)
            if recovering:
                if pending_binding is None:
                    return
                binding = pending_binding
                request = ledger.pending(binding['id'])
                run['provider_started'] = request['status'] == 'submitted'
                if (binding['owner'], binding['collection']) != (owner, dataset):
                    raise ValueError('Recover through the original workspace owner and collection.')
                if not delegated and (binding.get('capabilities') or {}).get('history_scope') != admission.history_scope:
                    raise ValueError('Recover through the original admitted history scope.')
                frozen = binding['configuration_body']
                if frozen is None:
                    frozen = await resolved(self.policy.base_configuration(agent, chat_id, tools, instructions))
                    if ledger.fingerprint(frozen) != binding['configuration']:
                        raise ValueError('Recover the original saved managed configuration.')
                if binding['capabilities'] is not None:
                    run['hosted_selection'] = await builder.restore(owner, binding['capabilities'], frozen)
                base = builder.configuration(frozen)
                input_value = request['input']
            else:
                user = next((item for item in reversed(record['messages']) if item.get('role') == 'user'), None)
                if user is None or user.get('content') != message:
                    raise ValueError('Save the user message before starting a managed turn.')
                logical_id = user.setdefault('managed_request_id', 'message-' + uuid.uuid4().hex)
                agent._save_child(record)
                original = ledger.request_binding(company, chat_id, logical_id)
                if original is not None:
                    prior = ledger.request_record(original['id'], logical_id)
                    if prior['status'] in TERMINAL:
                        yield {'type': 'done', 'status': prior['status']}
                        return
                if pending_binding is not None:
                    raise ValueError('Recover the original pending managed request before new input.')
                if delegated:
                    base = delegated_grant.configuration(tools)
                    if getattr(self.policy, 'spending_mode', None) == 'monitored':
                        base['metadata'] = {'rocoto_spending_mode': 'monitored'}
                    selected, capability_identity = None, None
                else:
                    base = await resolved(self.policy.base_configuration(agent, chat_id, tools, instructions))
                    selected = await builder.prepare(owner, resolved_skills)
                    run['hosted_selection'] = selected
                    capability_identity = (copy.deepcopy(selected.identity)
                        if builder.store is not None or resolved_skills or admission.history_scope is not None else None)
                    if admission.history_scope is not None:
                        capability_identity['history_scope'] = admission.history_scope
                if selected is not None and selected.environment:
                    if base.get('environment', {}).get('type') != 'openai_hosted':
                        raise ValueError('Use an OpenAI-hosted environment for installed skills and plugins.')
                    base['environment'].update(selected.environment)
                frozen = builder.freeze(base)
                previous = ledger.for_chat(company, chat_id)
                values = dict(company_id=company, chat_id=chat_id, owner=owner, collection=dataset,
                              configuration=frozen, capabilities=capability_identity)
                changed = previous is not None and (ledger.retired(previous['id']) or previous['configuration'] != ledger.fingerprint(frozen, capability_identity))
                if changed:
                    if (previous['owner'], previous['collection']) != (owner, dataset):
                        raise ValueError('Continue through the original workspace owner and collection.')
                    client = await lifetime.enter_async_context(self.client_factory())
                    run['managed_client'] = client
                    stopped = True
                    if previous['session_id'] is not None:
                        stopped, _ = await quiescent(client, previous['session_id'])
                    if not stopped:
                        raise ValueError('Wait for the previous root and children before changing capabilities.')
                    self._require(agent, chat_id, run, admission)
                    await self._revoke_binding(agent, previous)
                    ledger.retire(previous['id'], 'configuration_changed')
                    binding = ledger.rollover(**values, previous_id=previous['id'], quiesced=stopped)
                else:
                    binding = ledger.binding(**values)
                input_value = ([{'role': 'user', 'content': [{'type': 'input_text', 'text': message}]}]
                    if delegated else self._input(agent, message, chat_id, dataset, attachments, clip_attachments,
                                                  resolved_skills, model_context, selected=selected))
                # Saved local messages predate per-message resource provenance.
                # Once any generation carried a scope, only the original remote
                # session can retain that history; a new generation starts fresh.
                scoped_history = changed and any((item.get('capabilities') or {}).get('history_scope') is not None
                    for item in ledger.generations(company, chat_id))
                if changed and not scoped_history:
                    self._history(input_value, record, user)
                request = ledger.request(binding['id'], logical_id, input_value)
            run['binding_current'] = lambda: not ledger.retired(binding['id'])
            run['base_configuration'] = copy.deepcopy(base)
            # Recovery keeps original model instructions and environment; current
            # host registry admission still decides each attempted tool effect.
            instructions = base['agent']['instructions']
            run['instructions'] = instructions
            key = ledger.submission(binding['id'], request['logical_id'], 'message', input_value)
            self._require(agent, chat_id, run, admission)
            client = run.get('managed_client')
            if client is None:
                client = await lifetime.enter_async_context(self.client_factory())
                run['managed_client'] = client
            policy_mode = getattr(self.policy, 'spending_mode', None)
            if policy_mode is not None and getattr(client, 'spending_mode', policy_mode) != policy_mode:
                raise ValueError('Use the same explicit managed spending mode for policy and transport.')
            run['provider_quiesced'] = True
            if binding['session_id'] is not None:
                run['provider_quiesced'], _ = await quiescent(client, binding['session_id'])
                if not recovering and not run['provider_quiesced']:
                    raise ValueError('Recover the preceding managed turn before sending another input.')
            lease = await lifetime.enter_async_context(self.policy.turn(
                agent, chat_id, tools, instructions, session_id=binding['session_id'],
                request_id=key, binding_id=binding['id'], admission=admission, run=run,
                recovering=recovering))
            run['managed_lease'] = lease
            self._require(agent, chat_id, run, admission)
            config = copy.deepcopy(lease.configuration)
            if config.get('agent', {}).get('multi_agent', {}).get('enabled') and lease.mcp is None:
                raise ValueError('Bind scoped managed MCP before enabling subagents.')
            created = binding['session_id'] is None
            if created:
                config['input'] = input_value
                ledger.submission(binding['id'], 'create', 'create', config)
                ledger.request_state(request['id'], 'submitted')
                run['provider_started'] = True
                session = await client.create_session(config, submission_key=binding['creation_key'])
                ledger.created(binding['id'], session['id'])
                binding = ledger.get(binding['id'])
            else:
                session = await client.get_session(binding['session_id'])
            original_functions = {tool['name'] for tool in base['agent']['tools'] if tool.get('type') == 'function'}
            available_functions = original_functions & {tool['name'] for tool in tools}
            async def execute(name, arguments):
                if name not in available_functions:
                    raise ValueError('Use an available managed coordinator function.')
                return await agent._scoped_tool(name, arguments, dataset, chat_id)
            children = None if delegated else ManagedChildren(agent, chat_id, run, session, client, admission)
            engine = ManagedTurn(client=client, ledger=ledger, binding=binding, run=run,
                admission=admission, registry=registry, execute=execute,
                result_fields=agent.tool_result_fields, request=request, children=children,
                current=lambda: agent.active.get(chat_id) is run and run['binding_current']()
                    and (run.get('spend_monitor') is None or (run['spend_monitor'].deadline_at > time.time()
                         and run['spend_monitor'].stop_reason is None))
                    and (not delegated or delegated_grant.current())
                    and (run.get('hosted_selection') is None or run['hosted_selection'].current()))
            engine.accept_session(session)
            engine.root_turn_id = request.get('turn_id')
            # Public item identities live atomically beside their rendered text.
            answer = recovery_answer if recovery_answer is not None else next(
                (item for item in reversed(record['messages']) if item.get('role') == 'assistant'), {})
            engine.finished_items.update(answer.get('managed_items', []))
            run['managed_engine'] = engine
            run['thread_id'] = engine.session_id
            run['ready'].set()
            if hasattr(lease, 'created'):
                await resolved(lease.created(engine.session_id))
            elif lease.mcp is not None:
                await resolved(lease.mcp.created(engine.session_id))
            monitor = getattr(lease, 'monitor', None)
            if monitor is not None:
                async def monitored_stop(reason):
                    run['spending_stop_reason'] = reason
                    await self._cancel(run)
                run['monitor_task'] = asyncio.create_task(monitor.watch(
                    client, engine.session_id, monitored_stop, lease.persist_monitor))
            if not created and not recovering:
                await self._update(client, engine, config, key)
                run['provider_started'] = True
            run['status'] = 'running'
            engine.require_current()
            yield {'type': 'timing', 'started_at': started_at}
            yield {'type': 'thread', 'thread_id': engine.session_id}
            async with asyncio.timeout(max(0, run['deadline'] - time.monotonic())):
                observer = self._observe(client, engine, created=created, input_value=input_value,
                                         submission_key=key, baseline=set(request['baseline']))
                async for event in self._project(observer, run):
                    if event.get('type') == 'channel_delivery':
                        engine.require_current()
                        # A sent message ends this provider turn. Keep the cost
                        # hold and active slot until its root and children stop.
                        await self._cancel(run)
                        if agent.active.get(chat_id) is run and admission.current():
                            run['delivery_boundary'] = event['delivery']
                            yield event
                        continue
                    if event.get('type') != 'done':
                        if run['cancel'].is_set():
                            if event.get('type') in {'approval_resolved', 'user_input_resolved'}:
                                run.setdefault('settled_notices', []).append(event)
                            continue
                        engine.require_current()
                        yield event
                settled, snapshot = await quiescent(client, engine.session_id)
                if not settled:
                    await self._cancel(run)
                    snapshot = run.get('terminal_snapshot')
                terminal_status = engine.outcome
            if not delegated:
                from delegated_jobs import withdraw_children
                if not await withdraw_children(self, agent, binding):
                    terminal_status = None
                    raise RuntimeError('The delegated job is awaiting terminal confirmation.')
            ledger.request_state(request['id'], terminal_status)
            await self._stop_monitor(run)
            if not run.get('lease_revoked'):
                lease.revoke()
                run['lease_revoked'] = True
            await lease.finish(snapshot, terminal_status=terminal_status)
            for steering_lease in run.get('steering_leases', []):
                await steering_lease.finish(snapshot, terminal_status=terminal_status)
            run['policy_finished'] = True
            if not delegated and not run['cancel'].is_set():
                answer['managed_session_id'] = engine.session_id
                answer['managed_turn_id'] = engine.root_turn_id
                agent._save_child(record)
                def files_current():
                    self._require(agent, chat_id, run, admission)
                    return True
                for event in await self._harvest_files(agent, client, chat_id, current=files_current,
                                                      answer=answer, binding_id=binding['id']):
                    engine.require_current()
                    yield event
            await lifetime.aclose()
            notices = run.pop('settled_notices', [])
            while not run['tool_events'].empty():
                notices.append(run['tool_events'].get_nowait())
            for event in notices:
                if (event.get('type') in {'approval_resolved', 'user_input_resolved'}
                        and agent.active.get(chat_id) is run and admission.current()):
                    yield event
            public_status = 'interrupted' if terminal_status == 'cancelled' else terminal_status
            if run.get('delivery_boundary') and admission.current():
                public_status = ('waiting_for_reply' if run['delivery_boundary']['waiting_for_reply']
                                 else 'delivery_needs_attention')
            yield {'type': 'done', 'thread_id': engine.session_id,
                   'status': public_status,
                   'started_at': started_at,
                   'completed_at': datetime.now(timezone.utc).isoformat(),
                   'elapsed_ms': round((time.monotonic() - started) * 1000)}
        finally:
            async def cleanup():
                nonlocal snapshot, terminal_status
                await self._stop_monitor(run)
                if lease is not None and not run.get('lease_revoked'):
                    disconnected = terminal_status is None and not run['cancel'].is_set() and admission.current()
                    lease.revoke(disconnected=disconnected)
                    run['lease_revoked'] = True
                if engine is not None and terminal_status is None:
                    try:
                        await self._cancel(run)
                        snapshot = run.get('terminal_snapshot')
                        terminal_status = engine.outcome
                        ledger.request_state(engine.request['id'], terminal_status)
                    except (ManagedCodexError, RuntimeError, ValueError, TimeoutError):
                        record['managed_recovery_required'] = True
                        agent._save_child(record)
                if engine is None and request is not None and binding is not None and binding['session_id'] is not None:
                    client = run.get('managed_client')
                    try:
                        if client is None:
                            client = await lifetime.enter_async_context(self.client_factory())
                        key = ledger.submission(binding['id'], 'withdraw-' + request['id'], 'cancel', {})
                        await client.interrupt(binding['session_id'], submission_key=key)
                        async with asyncio.timeout(30):
                            while True:
                                stopped, snapshot = await quiescent(client, binding['session_id'])
                                if stopped:
                                    from delegated_jobs import withdraw_children
                                    if not await withdraw_children(self, agent, binding):
                                        raise RuntimeError('The delegated job is awaiting Stop confirmation.')
                                    terminal_status = 'cancelled'
                                    ledger.request_state(request['id'], terminal_status)
                                    break
                                await asyncio.sleep(0.1)
                    except (ManagedCodexError, RuntimeError, ValueError, TimeoutError):
                        record['managed_recovery_required'] = True
                        agent._save_child(record)
                elif engine is None and request is not None and not run['provider_started']:
                    ledger.request_state(request['id'], 'failed')
                if lease is not None and not run.get('policy_finished'):
                    await lease.finish(snapshot, terminal_status=terminal_status)
                    for steering_lease in run.get('steering_leases', []):
                        await steering_lease.finish(snapshot, terminal_status=terminal_status)
                await lifetime.aclose()
            cleanup_task = asyncio.create_task(cleanup())
            try:
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        continue
                cleanup_task.result()
            finally:
                run['ready'].set()
                run['finished'].set()
                if agent.active.get(chat_id) is run:
                    agent.active.pop(chat_id)
                if delegated:
                    delegated_grant.parent_run.get('delegated_runs', {}).pop(chat_id, None)

    async def recover(self, agent, chat_id):
        """Recover current access, or withdraw already-funded work after access ends."""
        ledger = ManagedSessions(agent.state)
        binding = ledger.pending_binding(workspace_scope(agent.state), chat_id)
        request = ledger.pending(binding['id']) if binding else None
        if request is None or chat_id in agent.active:
            return
        try:
            async for event in self._recover_admitted(agent, chat_id):
                yield event
        except (ManagedCodexError, RuntimeError, ValueError, OSError, TimeoutError):
            pending = ledger.pending(binding['id'])
            if binding['session_id'] is not None and pending is not None and pending['id'] == request['id']:
                try:
                    async with self.client_factory() as client:
                        key = ledger.submission(binding['id'], 'withdraw-' + request['id'], 'cancel', {})
                        await client.interrupt(binding['session_id'], submission_key=key)
                        async with asyncio.timeout(30):
                            while True:
                                stopped, _ = await quiescent(client, binding['session_id'])
                                if stopped:
                                    from delegated_jobs import withdraw_children
                                    if not await withdraw_children(self, agent, binding):
                                        raise RuntimeError('The delegated job is awaiting Stop confirmation.')
                                    ledger.request_state(request['id'], 'cancelled')
                                    break
                                await asyncio.sleep(0.1)
                except (ManagedCodexError, RuntimeError, ValueError, OSError, TimeoutError):
                    record = agent.children.get(chat_id)
                    if record is not None:
                        record['managed_recovery_required'] = True
                        agent._save_child(record)
            raise

    async def _recover_admitted(self, agent, chat_id):
        """Resume the exact pending outbox into its original saved assistant row."""
        from scope_host import agent_scope
        ledger = ManagedSessions(agent.state)
        binding = ledger.pending_binding(workspace_scope(agent.state), chat_id)
        request = ledger.pending(binding['id']) if binding else None
        if request is None or chat_id in agent.active:
            return
        record = agent.children.get(chat_id, {})
        messages = record.get('messages', [])
        index = next((number for number, item in enumerate(messages)
                      if item.get('managed_request_id') == request['logical_id']), None)
        if index is None:
            raise ValueError('Recover through the original saved conversation request.')
        user = messages[index]
        answer = next((item for item in messages[index + 1:] if item.get('role') == 'assistant'), None)
        if answer is None:
            raise ValueError('Recover into the original saved assistant message.')
        sender = user.get('channel') or {'principal_id': record.get('owner_principal_id'), 'channel': 'web'}
        if not hasattr(agent, '_turn_senders'):
            agent._turn_senders = {}
        previous = agent._turn_senders.get(chat_id)
        async with agent_scope(agent, chat_id, record.get('dataset'), sender=sender, admit=True):
            agent._turn_senders[chat_id] = copy.deepcopy(sender)
            record['running'] = True
            agent._save_child(record)
            try:
                async for event in self.run(agent, user.get('content', ''), chat_id, record.get('dataset'),
                                             recovering=True, recovery_answer=answer):
                    agent._record_event(answer, event)
                    agent._save_child(record)
                    yield event
                record.pop('managed_recovery_required', None)
            finally:
                if previous is None:
                    agent._turn_senders.pop(chat_id, None)
                else:
                    agent._turn_senders[chat_id] = previous
                record['running'] = False
                agent._save_child(record)

    async def recover_request(self, agent, chat_id, logical_id):
        """Resolve one saved scheduler input without creating another provider turn."""
        ledger = ManagedSessions(agent.state)
        binding = ledger.request_binding(workspace_scope(agent.state), chat_id, logical_id)
        request = ledger.request_record(binding['id'], logical_id) if binding else None
        if request is None:
            return {'status': 'missing', 'turn_id': None}
        if request['status'] not in TERMINAL:
            active = agent.active.get(chat_id)
            if active is not None:
                if not active.get('managed'):
                    return {'status': 'uncertain', 'turn_id': request['turn_id']}
                await active['finished'].wait()
            else:
                try:
                    async for _ in self.recover(agent, chat_id):
                        pass
                except (ManagedCodexError, RuntimeError, ValueError, OSError, TimeoutError):
                    pass
            request = ledger.request_record(binding['id'], logical_id)
        status = request['status'] if request['status'] in TERMINAL else 'uncertain'
        return {'status': status, 'turn_id': request['turn_id']}

    async def revoke_plugin(self, agent, principal_id, plugin_id):
        """Retire every original session carrying a removed package, including idle ones.

        The caller's durable removal journal fences new admissions until uninstall
        commits. A missing create response remains uncertain and blocks cleanup.
        """
        ledger = ManagedSessions(agent.state)
        affected = []
        for binding in ledger.for_owner(workspace_scope(agent.state), principal_id):
            packages = ((binding.get('capabilities') or {}).get('selection') or {}).get('packages', [])
            if any(package['id'] == plugin_id for package in packages):
                affected.append(binding)
        complete = True
        for binding in affected:
            ledger.retire(binding['id'], 'plugin_removed')
        for binding in affected:
            await self._revoke_binding(agent, binding)
            run = agent.active.get(binding['chat_id'])
            engine = run.get('managed_engine') if run else None
            if engine is not None and engine.binding['id'] == binding['id']:
                try:
                    await self._cancel(run)
                    await asyncio.wait_for(run['finished'].wait(), timeout=45)
                except (ManagedCodexError, RuntimeError, ValueError, TimeoutError):
                    complete = False
                    continue
            pending = ledger.pending(binding['id'])
            if binding['session_id'] is None:
                if pending is not None and pending['status'] == 'submitted':
                    complete = False
                elif pending is not None:
                    ledger.request_state(pending['id'], 'cancelled')
                continue
            try:
                async with self.client_factory() as client:
                    key = ledger.submission(binding['id'], 'remove-plugin-' + plugin_id, 'cancel', {})
                    await client.interrupt(binding['session_id'], submission_key=key)
                    async with asyncio.timeout(30):
                        while True:
                            stopped, _ = await quiescent(client, binding['session_id'])
                            if stopped:
                                if pending is not None:
                                    ledger.request_state(pending['id'], 'cancelled')
                                break
                            await asyncio.sleep(0.1)
            except (ManagedCodexError, RuntimeError, ValueError, OSError, TimeoutError):
                complete = False
        return complete

    async def recover_pending(self, agent):
        """Workspace startup can resume admitted requests without new input/funding."""
        for chat_id in list(agent.children):
            try:
                async for _ in self.recover(agent, chat_id):
                    pass
                async for _ in self.recover_files(agent, chat_id):
                    pass
            except (ManagedCodexError, RuntimeError, ValueError, OSError, TimeoutError):
                record = agent.children.get(chat_id)
                if record is not None:
                    record['managed_recovery_required'] = True
                    agent._save_child(record)


def configured_runtime(agent):
    """Use the owning workspace's protected OpenAI credential and funded policy."""
    from managed_codex import ManagedCodexClient
    from managed_policy import ManagedPolicy
    policy = ManagedPolicy()
    def client():
        authentication = getattr(agent, 'auth', None)
        environment = getattr(authentication, 'environment', {})
        return ManagedCodexClient(environment.get('ROCOTO_OPENAI_API_KEY', ''), spending_mode=policy.spending_mode)
    return ManagedRuntime(client, policy)
