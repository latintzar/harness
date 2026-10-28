"""Bounded synthetic Agents API proof, explicitly funded from an existing pool.

Default execution prints the plan. --execute requires an existing commissioned
workspace, its exact pool scope, a new private proof directory, and the Rocoto
credential in ROCOTO_OPENAI_API_KEY. The operator supplies approval separately.
The default uses the provider's finite cap. Explicit monitored mode requires a
private overshoot approval and uses token estimates plus a 300-second deadline.
The existing money hold remains reserved until a final provider receipt.
--resume-rejected accepts only the approved definite spend-control rejection;
its exclusive claim and new attempt retain the original evidence and money hold.

This proves the stock protocol, shell artifact, child history and cancellation.
The deployed scoped MCP/child-read acceptance is a separate product proof.
Official file contract: https://developers.openai.com/api/docs/guides/agents-api/environments/files
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import sqlite3
import stat
import time
from types import SimpleNamespace
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import layout
if __name__ == '__main__':
    layout.install('bare')

from company_usage import CompanyUsage
from host import Admission
from managed_codex import ManagedCodexClient, ManagedCodexError
from managed_runtime import pages, quiescent
from managed_sessions import ManagedSessions, TERMINAL, digest, identity
from managed_spend import SpendMonitor, spending_mode
from managed_turn import ManagedTurn, hosted_connection_pending
from managed_usage import ManagedUsage

MODEL = 'gpt-6-astra'
OUTPUT_PATH = '/workspace/outputs/rocoto-proof.json'
OVERSHOOT = ('Token estimates trigger cancellation at the target. Reporting and cancellation delays, '
             'hosted environments and other provider costs can exceed the target; final billing remains unknown.')


def private_json(path, value):
    temporary = path.with_suffix('.pending')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def configuration(limit, nonce, *, spending_mode='enforced'):
    if spending_mode not in {'enforced', 'monitored'}:
        raise ValueError('Select enforced or monitored spending.')
    result = {
        'agent': {'model': MODEL, 'reasoning': {'effort': 'low'},
                  'multi_agent': {'enabled': True, 'max_concurrent_subagents': 1},
                  'instructions': 'Run only the synthetic proof requested. Keep answers short.',
                  'tools': [
                      {'type': 'function', 'name': name, 'description': description,
                       'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
                      for name, description in (
                          ('proof_receipt', 'Record the synthetic proof receipt exactly once.'),
                          ('cancellation_checkpoint', 'Pause at the requested cancellation checkpoint.'))]},
        'environment': {'type': 'openai_hosted', 'container_size': 'small',
                        'network': {'access': 'disabled'}},
        'spend_control': {'limit': limit},
        'metadata': {'rocoto_proof': nonce},
        'input': ('First call proof_receipt once. Then use a stock subagent to compute 2+3 from these '
                  'numbers and wait for its answer. Use the shell to write a JSON file at '
                  + OUTPUT_PATH + ' containing exactly ' + json.dumps({'nonce': nonce, 'answer': 5})
                  + '. Read the file back, then report completion.')}
    if spending_mode == 'monitored':
        result.pop('spend_control')
    return result


def private_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        value = os.fstat(stream.fileno())
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid()
                or value.st_nlink != 1 or value.st_mode & 0o077 or value.st_size > 2 * 1024 * 1024):
            raise ValueError('Use the original private proof and approval files.')
        return stream.read(2 * 1024 * 1024 + 1)


def exclusive_bytes(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def approved_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise ValueError('Keep an explicit timestamp with its time zone.') from None


def read_approval(args):
    source = getattr(args, 'approval_record', None)
    if source is None:
        raise ValueError('Monitored spending requires the saved explicit overshoot approval.')
    raw = private_bytes(source)
    value = json.loads(raw)
    expected = {'schema_version': 1, 'kind': 'managed_protocol_monitored_approval',
                'approved_by': 'peter', 'actor': args.actor, 'pool_scope': args.pool_scope,
                'spending_mode': 'monitored', 'target_cents': args.limit_cents, 'overshoot_approved': True}
    if (not isinstance(value, dict) or any(value.get(k) != v for k, v in expected.items())
            or value.get('overshoot_approved') is not True
            or type(value.get('max_active_seconds')) is not int
            or not args.timeout_seconds <= value['max_active_seconds'] <= 300
            or not all(isinstance(value.get(k), str) and value[k].strip()
                       for k in ('authorization_reference', 'user_instruction'))
            or approved_time(value.get('approved_at')) > datetime.now(timezone.utc)):
        raise ValueError('Use approval for this actor, pool, target and bounded monitored attempt.')
    return value, raw


def rejected_evidence(args, approval, usage):
    """Only this definite admission rejection can reuse an unspent original hold."""
    old = args.proof_state.resolve()
    raw = private_bytes(old / 'receipt.json')
    diagnostic_raw = private_bytes(old / 'diagnostic.json')
    receipt, diagnostic = json.loads(raw), json.loads(diagnostic_raw)
    if (approval.get('receipt_sha256') != hashlib.sha256(raw).hexdigest()
            or approval.get('diagnostic_sha256') != hashlib.sha256(diagnostic_raw).hexdigest()
            or receipt.get('kind') != 'managed_protocol_proof' or receipt.get('schema_version') != 1
            or receipt.get('model') != MODEL or receipt.get('approved_limit_cents') != args.limit_cents
            or receipt.get('pool_scope') != args.pool_scope or receipt.get('passed') is not False
            or receipt.get('session_id') is not None or receipt.get('checks') != {}
            or receipt.get('failure') != {'type': 'ManagedCodexError', 'code': 'http_error', 'status_code': 400}
            or receipt.get('cleanup') != {'deleted': False, 'ambiguous_creation_key': receipt.get('creation_key')}
            or approval.get('creation_key') != receipt.get('creation_key')
            or approved_time(approval['approved_at']) < approved_time(receipt.get('finished_at'))
            or diagnostic.get('status') != 400 or not diagnostic.get('request_id')
            or diagnostic.get('error') != {'code': 'invalid_request_error', 'type': 'invalid_request_error',
                'param': 'spend_control', 'message': 'Session budget configuration is not enabled'}):
        raise ValueError('Resume only the approved definite spend-control admission rejection.')
    database = old / 'managed-sessions.sqlite3'
    private_bytes(database)
    # Read-only mode deliberately avoids ManagedSessions migrations and backup writes.
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        required = {'sessions', 'submissions', 'calls', 'requests', 'turns'}
        optional = {'artifacts', 'session_retirements', 'delivery_boundaries'}
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # The original rejected proof predates artifact and generation receipts.
        # Every recognized execution table must be empty when it is present.
        if not required <= tables or tables - required - optional:
            raise ValueError('Use a recognized original managed proof ledger schema.')
        sessions = [dict(row) for row in db.execute('SELECT * FROM sessions')]
        submissions = [dict(row) for row in db.execute('SELECT * FROM submissions')]
        config = configuration(args.limit_cents, receipt['nonce'])
        expected = {'id': receipt['binding_id'], 'company_id': args.pool_scope,
                    'chat_id': 'proof-' + receipt['nonce'], 'owner': args.actor,
                    'collection': 'synthetic', 'configuration': digest(config),
                    'session_id': None, 'creation_key': receipt['creation_key']}
        if (len(sessions) != 1 or any(sessions[0].get(k) != v for k, v in expected.items())
                or submissions != [{'binding_id': receipt['binding_id'], 'logical_id': 'initial',
                    'kind': 'create', 'payload_hash': digest(config), 'submission_key': receipt['request_id']}]
                or any(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                       for table in tables - {'sessions', 'submissions'})):
            raise ValueError('Verify the original unused synthetic creation ledger.')
    validate_hold(usage, receipt, args)
    return receipt


def validate_hold(usage, receipt, args, *, recover=False):
    with usage.pool.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        record = usage._request(db, receipt['request_id'])
        expected = {'binding_id': receipt['binding_id'], 'actor': args.actor, 'model': MODEL,
                    'limit_cents': args.limit_cents, 'session_id': None, 'closed': 0,
                    'consumed_cents': None, 'terminal': None}
        if (record is None or any(record.get(k) != v for k, v in expected.items())
                or record.get('status') not in {'admitted', 'uncertain'} or not record.get('reservation_id')
                or db.execute('SELECT COUNT(*) FROM managed_allowances WHERE binding_id=?',
                              (receipt['binding_id'],)).fetchone()[0] != 1):
            raise ValueError('Keep the original unspent and unbound managed allowance.')
        held = usage.pool._reservation(db, record['reservation_id'])
        if held['status'] != 'running' or held['held'] < args.limit_cents * 10000:
            raise ValueError('Keep the complete original monetary hold reserved.')
        if recover:
            usage._reserve(db, binding_id=receipt['binding_id'], request_id=receipt['request_id'],
                           actor=args.actor, model=MODEL, limit_cents=args.limit_cents, recovering=True)


def prepare_attempt(args, pool, usage):
    mode = spending_mode(getattr(args, 'spending_mode', 'enforced'))
    if (type(args.limit_cents) is not int or not 1 <= args.limit_cents <= 100
            or type(args.timeout_seconds) is not int
            or not 30 <= args.timeout_seconds <= (300 if mode == 'monitored' else 600)):
        raise ValueError('Use the approved target and bounded proof deadline.')
    resume = getattr(args, 'resume_rejected', False)
    if resume and mode != 'monitored':
        raise ValueError('The reviewed rejected-proof transition requires monitored spending.')
    approval, approval_raw = read_approval(args) if mode == 'monitored' else (None, None)
    prior = rejected_evidence(args, approval, usage) if resume else None
    nonce = uuid.uuid4().hex
    proof = args.proof_state.resolve()
    deadline_at = time.time() + args.timeout_seconds
    claim = None
    if resume:
        claim = proof / 'monitored-resume.json'
        attempt = proof / 'attempts' / ('monitored-' + nonce)
        exclusive_bytes(claim, json.dumps({'schema_version': 1, 'attempt': str(attempt),
            'approval_sha256': hashlib.sha256(approval_raw).hexdigest(),
            'original_creation_key': prior['creation_key'], 'deadline_at': deadline_at}, sort_keys=True).encode())
        (proof / 'attempts').mkdir(mode=0o700, exist_ok=True)
        if (proof / 'attempts').is_symlink():
            raise ValueError('Use the original private attempt directory.')
        proof = attempt
    proof.mkdir(mode=0o700, parents=False, exist_ok=False)
    receipt = {'schema_version': 1, 'kind': 'managed_protocol_proof', 'nonce': nonce,
               'started_at': datetime.now(timezone.utc).isoformat(), 'approved_limit_cents': args.limit_cents,
               'model': MODEL, 'spending_mode': mode, 'remote_mcp_verified': False, 'checks': {}, 'cleanup': None}
    monitor = None
    if mode == 'monitored':
        exclusive_bytes(proof / 'approval.json', approval_raw)
        monitor = SpendMonitor(args.limit_cents, deadline_at, model=MODEL)
        receipt.update(monitor=monitor.receipt(), overshoot_notice=OVERSHOOT,
                       approval_sha256=hashlib.sha256(approval_raw).hexdigest())
    ledger = ManagedSessions(proof)
    config = configuration(args.limit_cents, nonce, spending_mode=mode)
    binding = ledger.binding(company_id=pool.company_scope, chat_id='proof-' + nonce,
                             owner=args.actor, collection='synthetic', configuration=config)
    request_id = ledger.submission(binding['id'], 'initial', 'create', config)
    funding_binding = prior['binding_id'] if prior else binding['id']
    funding_request = prior['request_id'] if prior else request_id
    receipt.update(binding_id=binding['id'], request_id=request_id, pool_scope=pool.company_scope,
                   creation_key=binding['creation_key'], funding_binding_id=funding_binding,
                   funding_request_id=funding_request)
    if prior:
        receipt['resumed_rejection'] = {'receipt_sha256': approval['receipt_sha256'],
            'diagnostic_sha256': approval['diagnostic_sha256'], 'creation_key': prior['creation_key'],
            'claim': str(claim)}
    private_json(proof / 'receipt.json', receipt)
    if prior:
        validate_hold(usage, prior, args, recover=True)
    else:
        usage.reserve(binding_id=funding_binding, request_id=funding_request, actor=args.actor,
                      model=MODEL, limit_cents=args.limit_cents)
    return {'proof': proof, 'receipt': receipt, 'ledger': ledger, 'binding': binding,
            'request_id': request_id, 'funding_binding_id': funding_binding,
            'funding_request_id': funding_request, 'config': config, 'monitor': monitor,
            'deadline_at': deadline_at}


def protocol_shape(session):
    """Small state projection: credentials, instructions and arguments stay private."""
    states = {'idle', 'in_progress', 'requires_action', 'failed', 'provisioning', 'connected'}
    kinds = {'function_call', 'environment_connection', 'credential_request', 'computer_use_approval_request'}
    def known(value, choices, fallback='unknown'):
        return value if isinstance(value, str) and value in choices else fallback
    environment = session.get('environment')
    environment = environment if isinstance(environment, dict) else {}
    actions = session.get('required_actions')
    actions = actions if isinstance(actions, list) else []
    return {'session_status': known(session.get('status'), states),
            'environment_type': known(environment.get('type'), {'none', 'openai_hosted', 'self_hosted'}),
            'environment_status': known(environment.get('status'), states),
            'environment_error_present': bool(environment.get('error')),
            'session_error_present': bool(session.get('error')),
            'action_count': len(actions),
            'action_types': [known(value.get('type'), kinds, 'other')
                             for value in actions[:10] if isinstance(value, dict)],
            'action_names': [known(value.get('name'), {'proof_receipt', 'cancellation_checkpoint'}, 'other')
                for value in actions[:10] if isinstance(value, dict)]}


def failure_details(error, stage):
    """Retain source literals and location, never dynamic upstream error contents."""
    value = {'stage': stage}
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    if trace is not None:
        code = trace.tb_frame.f_code
        value.update(source=Path(code.co_filename).name, line=trace.tb_lineno)
        message = str(error)
        if isinstance(error, (ValueError, TimeoutError)) and message in code.co_consts:
            value['message'] = message[:280]
    request_id = getattr(error, 'request_id', None)
    if isinstance(request_id, str) and re.fullmatch(r'req_[A-Za-z0-9_-]{1,120}', request_id):
        value['provider_request_id'] = request_id
    return value


class ProofClient(ManagedCodexClient):
    def _check(self, response):
        request_id = response.headers.get('x-request-id', '')
        self.last_request_id = request_id if re.fullmatch(r'req_[A-Za-z0-9_-]{1,120}', request_id) else None
        super()._check(response)

    async def artifacts(self, session_id):
        async def fetch(**params):
            return await self._request('GET', self._session_path(session_id) + '/artifacts', params=params)
        return await pages(fetch, limit=4)

    async def artifact_bytes(self, session_id, artifact_id):
        path = self._session_path(session_id) + '/artifacts/' + identity(artifact_id) + '/content'
        async with self._client.stream('GET', path) as response:
            self._check(response)
            result = bytearray()
            async for chunk in response.aiter_bytes():
                result.extend(chunk)
                if len(result) > 65536:
                    raise ValueError('Synthetic artifact exceeds the proof allowance.')
            return bytes(result)


async def wait_for_action(client, session_id, name, deadline, *, prior_turns=(), diagnostics=None):
    while time.monotonic() < deadline:
        session = await client.get_session(session_id)
        if diagnostics is not None:
            diagnostics(session)
        actions = session.get('required_actions', [])
        if not isinstance(actions, list) or len(actions) > 100 or not all(isinstance(action, dict) for action in actions):
            raise ValueError('Read bounded required actions from the current session.')
        actions = [action for action in actions if not hosted_connection_pending(session, action)]
        if actions:
            if len(actions) != 1 or actions[0].get('name') != name:
                raise ValueError('Provider requested a different synthetic operation.')
            return session, actions[0]
        turns = await pages(lambda **params: client.list_turns(session_id, **params), limit=4)
        current = [turn for turn in turns if turn['id'] not in prior_turns]
        if current and current[-1]['status'] in TERMINAL:
            raise ValueError('Provider completed before the required proof checkpoint.')
        await asyncio.sleep(0.5)
    raise TimeoutError('Provider checkpoint exceeded the proof deadline.')


async def wait_for_terminal(client, session_id, deadline):
    while time.monotonic() < deadline:
        stopped, snapshot = await quiescent(client, session_id)
        if stopped:
            turns = await pages(lambda **params: client.list_turns(session_id, **params), limit=4)
            if not turns:
                raise ValueError('Expected a canonical provider turn.')
            return snapshot, turns
        await asyncio.sleep(0.5)
    raise TimeoutError('Provider terminal state exceeded the proof deadline.')


async def cleanup(client, session_id, key):
    """Stop known work and request sandbox deletion within a fixed 90 seconds."""
    outcome = {'session_id': session_id, 'deleted': False, 'quiescent': False, 'attempts': 0}
    async with asyncio.timeout(90):
        for attempt in range(12):
            outcome['attempts'] = attempt + 1
            try:
                await client.interrupt(session_id, submission_key=key + '-cancel')
                outcome['quiescent'], _ = await quiescent(client, session_id)
                deleted = await client.delete_session(session_id, submission_key=key + '-delete')
                if deleted.get('id') != session_id or deleted.get('deleted') is not True:
                    raise ValueError('Provider deletion did not confirm the bound session.')
                outcome['deleted'] = True
                return outcome
            except ManagedCodexError as error:
                if error.status_code == 404:
                    try:
                        await client.get_session(session_id)
                    except ManagedCodexError as absent:
                        if absent.status_code == 404:
                            outcome['deleted'] = True
                            return outcome
                    raise
                if error.status_code != 409 or attempt == 11:
                    raise
                await asyncio.sleep(min(2 ** attempt, 8))
    return outcome


async def run(args):
    credential = os.environ.get('ROCOTO_OPENAI_API_KEY', '')
    if not credential or any(character.isspace() for character in credential):
        raise ValueError('Load the owning Rocoto credential through its private input channel.')
    state = args.workspace_state.resolve()
    pool_path = state / 'billing' / 'openai-api'
    if not (pool_path / 'company-usage.sqlite').is_file() or pool_path.resolve() != pool_path:
        raise ValueError('Select the existing commissioned OpenAI API pool.')
    pool = CompanyUsage.openai_pool(state)
    if pool.company_scope != args.pool_scope:
        raise ValueError('Use the approved original pool scope.')
    usage = ManagedUsage(pool)
    attempt = prepare_attempt(args, pool, usage)
    proof, receipt, ledger = attempt['proof'], attempt['receipt'], attempt['ledger']
    binding, request_id, config = attempt['binding'], attempt['request_id'], attempt['config']
    funding_binding, funding_request = attempt['funding_binding_id'], attempt['funding_request_id']
    monitor, nonce = attempt['monitor'], receipt['nonce']
    receipt_path = proof / 'receipt.json'
    session_id = None
    snapshot = None
    terminal = None
    cancel_request = None
    remaining = max(0, attempt['deadline_at'] - time.time())
    deadline = time.monotonic() + remaining
    failure = None
    watch = None
    foreground = asyncio.current_task()
    stop_reason = None
    stage = 'create_session'
    async with ProofClient(credential, timeout=15, spending_mode=receipt['spending_mode']) as client:
        def checkpoint(value, session=None):
            nonlocal stage
            stage = value
            receipt['stage'] = stage
            if session is not None:
                receipt['protocol'] = protocol_shape(session)
            request_id = getattr(client, 'last_request_id', None)
            if request_id is not None:
                receipt['provider_request_id'] = request_id
            private_json(receipt_path, receipt)

        def persist_monitor(value):
            receipt['monitor'] = value
            private_json(receipt_path, receipt)

        async def stop(reason):
            nonlocal stop_reason
            stop_reason = reason
            # The foreground finally block interrupts and reconciles root plus
            # children before sandbox deletion; cancelling first stops new input.
            foreground.cancel()

        try:
            async with asyncio.timeout(max(0, attempt['deadline_at'] - time.time())):
                if time.time() >= attempt['deadline_at']:
                    raise TimeoutError('Use the original active proof deadline.')
                # One retry has the same immutable body/key: an ambiguous response
                # cannot create a second independent spending allowance.
                for attempt in range(2):
                    try:
                        session = await client.create_session(config, submission_key=binding['creation_key'])
                        break
                    except ManagedCodexError as error:
                        if error.code != 'transport_error' or attempt:
                            raise
                checkpoint('bind_created_session', session)
                session_id = identity(session['id'])
                receipt['session_id'] = session_id
                private_json(receipt_path, receipt)
                ledger.created(binding['id'], session_id)
                usage.created(funding_binding, session_id)
                if monitor is not None:
                    persist_monitor(monitor.observe(session))
                    watch = asyncio.create_task(monitor.watch(client, session_id, stop, persist_monitor))
                    await asyncio.sleep(0)
                binding = ledger.get(binding['id'])
                checkpoint('wait_for_receipt', session)
                session, action = await wait_for_action(client, session_id, 'proof_receipt', deadline,
                    diagnostics=lambda value: checkpoint('wait_for_receipt', value))
                # Close a real stream after a canonical snapshot; reconnect reads
                # required_actions again. History never authorizes the effect.
                checkpoint('recover_receipt_session', session)
                async with client.recover_session(session_id, max_pages=4) as recovered:
                    if recovered.session['id'] != session_id:
                        raise ValueError('Recover the bound provider session.')
                effect_path = proof / 'synthetic-effect.json'
                async def execute(name, arguments):
                    if name != 'proof_receipt' or arguments or effect_path.exists():
                        raise ValueError('Execute the synthetic effect once.')
                    private_json(effect_path, {'nonce': nonce, 'executions': 1})
                    return [{'type': 'inputText', 'text': 'Synthetic receipt saved.'}], None, True
                checkpoint('execute_receipt', session)
                engine = ManagedTurn(client=client, ledger=ledger, binding=binding,
                    run={'cancel': asyncio.Event(), 'generation': 'proof-first'},
                    admission=Admission(lambda: True),
                    registry=SimpleNamespace(reads=frozenset(), writes=frozenset({'proof_receipt'})),
                    execute=execute)
                engine.accept_session(session)
                async for _ in engine.required_actions():
                    pass
                # Replay only the saved completed receipt with the original call
                # identity and submission key; the durable local effect stays once.
                checkpoint('replay_receipt')
                replay = ManagedSessions(proof).claim(binding['id'], action,
                    generation='proof-restarted', actor_chat=binding['chat_id'], write=True)
                if replay['decision'] != 'replay':
                    raise ValueError('Recover the durable completed function receipt.')
                result = {'type': 'agent.session.input.tool_result', 'turn_id': action['turn_id'],
                          'call_id': action['call_id'], 'success': True,
                          'output': replay['receipt']['output']}
                await client.submit_events(session_id, [result], submission_key=replay['submission_key'])
                receipt['checks']['function_replay'] = {'call_id': action['call_id'], 'effect_executions': 1}
                checkpoint('wait_for_artifact_turn')
                snapshot, turns = await wait_for_terminal(client, session_id, deadline)
                if turns[-1]['status'] != 'completed':
                    raise ValueError('The synthetic artifact turn must complete successfully.')
                terminal = 'completed'
                usage.finish(funding_request, snapshot, terminal)
                checkpoint('verify_artifact', snapshot)
                artifacts = await client.artifacts(session_id)
                artifact = next((value for value in artifacts if value.get('path') == OUTPUT_PATH
                                 and value.get('turn_id') == turns[-1]['id']), None)
                if artifact is None:
                    raise ValueError('Read the published artifact for the completed root turn.')
                data = await client.artifact_bytes(session_id, artifact['id'])
                if json.loads(data) != {'nonce': nonce, 'answer': 5}:
                    raise ValueError('Verify the provider artifact bytes against synthetic input.')
                receipt['checks']['artifact'] = {'id': artifact['id'], 'turn_id': artifact['turn_id'],
                    'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                children = await pages(lambda **params: client.list_subagents(session_id, **params), limit=4)
                verified_children = []
                for child in children:
                    if child.get('session_id') != session_id or child.get('parent_agent_id') != session['agent']['id']:
                        raise ValueError('Verify stock child ancestry within the proof session.')
                    items = await client.collect_items(session_id, subagent_id=child['id'], max_pages=4)
                    outputs = [item for item in items if item.get('type') == 'message'
                               and item.get('role') == 'assistant' and item.get('status') == 'completed']
                    if not outputs:
                        raise ValueError('Read a completed child answer in stock provider history.')
                    verified_children.append({'id': child['id'], 'assistant_items': len(outputs)})
                if not verified_children:
                    raise ValueError('Verify at least one actual stock child.')
                receipt['checks']['stock_children'] = verified_children
                private_json(receipt_path, receipt)
                checkpoint('verify_cancellation', snapshot)
                cancel_text = 'Call cancellation_checkpoint now and wait for its result.'
                cancel_request = ledger.submission(binding['id'], 'cancel-turn', 'message', {'text': cancel_text})
                usage.reserve(binding_id=funding_binding, request_id=cancel_request, actor=args.actor,
                    model=MODEL, limit_cents=args.limit_cents, session_id=session_id)
                await client.send_message(session_id, cancel_text, submission_key=cancel_request)
                _, action = await wait_for_action(client, session_id, 'cancellation_checkpoint', deadline,
                                                  prior_turns={turn['id'] for turn in turns})
                cancel_key = ledger.submission(binding['id'], 'cancel', 'cancel', {})
                await client.interrupt(session_id, submission_key=cancel_key)
                snapshot, turns = await wait_for_terminal(client, session_id, deadline)
                if turns[-1]['id'] != action['turn_id'] or turns[-1]['status'] != 'cancelled':
                    raise ValueError('Confirm the targeted provider turn was cancelled.')
                usage.finish(cancel_request, snapshot, 'cancelled')
                receipt['checks']['cancellation'] = {'turn_id': action['turn_id'], 'status': 'cancelled',
                                                      'root_and_children_quiescent': True}
        except asyncio.CancelledError as error:
            failure = error
            if stop_reason is None:
                receipt['failure'] = {'type': 'CancelledError', 'reason': 'operator_cancelled'}
                raise
            receipt['failure'] = {'type': 'MonitoredStop', 'reason': stop_reason}
        except (ManagedCodexError, ValueError, OSError, TimeoutError) as error:
            failure = error
            receipt['failure'] = {'type': type(error).__name__,
                'code': getattr(error, 'code', None), 'status_code': getattr(error, 'status_code', None)}
            receipt['failure_detail'] = failure_details(error, stage)
        finally:
            if watch is not None:
                watch.cancel()
                try:
                    await watch
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    failure = failure or error
            if monitor is not None:
                if snapshot is not None:
                    try:
                        monitor.observe(snapshot)
                    except ValueError as error:
                        failure = failure or error
                receipt['monitor'] = monitor.receipt()
            if session_id is not None:
                try:
                    receipt['cleanup'] = await cleanup(client, session_id, binding['creation_key'])
                except (ManagedCodexError, ValueError, OSError, TimeoutError) as error:
                    receipt['cleanup'] = {'session_id': session_id, 'deleted': False,
                        'error_type': type(error).__name__, 'status_code': getattr(error, 'status_code', None)}
                    failure = failure or error
            else:
                receipt['cleanup'] = {'deleted': False, 'ambiguous_creation_key': binding['creation_key']}
            usage.finish(funding_request, snapshot, terminal)
            if cancel_request is not None:
                usage.finish(cancel_request, snapshot, None)
            receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
            receipt['passed'] = failure is None and receipt['cleanup']['deleted']
            receipt['accounting'] = 'Reserved; final provider receipt required for settlement.'
            private_json(receipt_path, receipt)
    print(json.dumps({'passed': receipt['passed'], 'receipt': str(receipt_path),
                      'cleanup': receipt['cleanup'], 'approved_limit_cents': args.limit_cents,
                      'spending_mode': receipt['spending_mode'], 'monitor': receipt.get('monitor')}))
    return 0 if receipt['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--spending-mode', choices=('enforced', 'monitored'), default='enforced')
    parser.add_argument('--approval-record', type=Path)
    parser.add_argument('--resume-rejected', action='store_true')
    parser.add_argument('--workspace-state', type=Path)
    parser.add_argument('--pool-scope')
    parser.add_argument('--actor')
    parser.add_argument('--proof-state', type=Path)
    parser.add_argument('--limit-cents', type=int, default=100)
    parser.add_argument('--timeout-seconds', type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.limit_cents <= 100 or not 30 <= args.timeout_seconds <= 600:
        parser.error('Use 1–100 USD cents and a 30–600 second proof deadline.')
    if args.spending_mode == 'monitored' and args.timeout_seconds > 300:
        parser.error('Monitored execution keeps one original deadline of at most 300 seconds.')
    if args.resume_rejected and (args.spending_mode != 'monitored' or args.proof_state is None):
        parser.error('Resume needs monitored mode and the exact rejected proof directory.')
    if not args.execute:
        print(json.dumps({'execute': False, 'model': MODEL, 'spending_mode': args.spending_mode,
                          'target_cents': args.limit_cents, 'provider_cap': args.spending_mode == 'enforced',
                          'active_deadline_seconds': args.timeout_seconds,
                          'approval_required': args.spending_mode == 'monitored',
                          'resume_claim': str(args.proof_state.resolve() / 'monitored-resume.json')
                              if args.resume_rejected else None,
                          'overshoot_notice': OVERSHOOT if args.spending_mode == 'monitored' else None,
                          'hosted_container': 'small', 'network': 'disabled', 'sessions': 1,
                          'remote_mcp_verified': False, 'cleanup_deadline_seconds': 90}))
        return 0
    if not all((args.workspace_state, args.pool_scope, args.actor, args.proof_state)):
        parser.error('Execution needs the existing workspace, exact pool scope, actor and new proof directory.')
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
