"""Provider-free checks of the paid proof's limits and cleanup guarantees."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from managed_codex import ManagedCodexError

spec = importlib.util.spec_from_file_location('managed_protocol_proof',
    Path(__file__).resolve().parents[1] / 'deploy' / 'quality' / 'prove_managed_protocol.py')
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_proof_uses_one_hosted_environment_with_only_synthetic_functions():
    config = proof.configuration(35, 'fixture')
    assert config['spend_control'] == {'limit': 35}
    assert config['environment'] == {'type': 'openai_hosted', 'container_size': 'small',
                                     'network': {'access': 'disabled'}}
    assert {tool['name'] for tool in config['agent']['tools']} == {'proof_receipt', 'cancellation_checkpoint'}
    assert '/workspace/outputs/rocoto-proof.json' in config['input']


@pytest.mark.anyio
@pytest.mark.parametrize("conflicts", [1, 8])
async def test_cleanup_retries_conflict_with_same_cancel_and_delete_keys(monkeypatch, conflicts):
    calls = []
    async def interrupt(session_id, *, submission_key):
        calls.append(('cancel', submission_key))
    async def delete(session_id, *, submission_key):
        calls.append(('delete', submission_key))
        if sum(kind == 'delete' for kind, _ in calls) <= conflicts:
            raise ManagedCodexError('http_error', status_code=409)
        return {'id': session_id, 'deleted': True}
    async def quiet(client, session_id):
        return True, {'id': session_id}
    async def sleep(seconds):
        assert seconds <= 8
    monkeypatch.setattr(proof, 'quiescent', quiet)
    monkeypatch.setattr(proof.asyncio, 'sleep', sleep)
    value = await proof.cleanup(SimpleNamespace(interrupt=interrupt, delete_session=delete), 'session-1', 'proof-key')
    assert value == {'session_id': 'session-1', 'deleted': True, 'quiescent': True, 'attempts': conflicts + 1}
    assert calls == [('cancel', 'proof-key-cancel'), ('delete', 'proof-key-delete')] * (conflicts + 1)


@pytest.mark.anyio
async def test_cleanup_does_not_claim_deleted_from_unrelated_404(monkeypatch):
    async def interrupt(session_id, **kwargs):
        raise ManagedCodexError('http_error', status_code=404)
    async def get(session_id):
        return {'id': session_id}
    with pytest.raises(ManagedCodexError):
        await proof.cleanup(SimpleNamespace(interrupt=interrupt, get_session=get), 'session-1', 'proof-key')


@pytest.mark.anyio
async def test_new_checkpoint_ignores_completed_preceding_turn(monkeypatch):
    attempts = 0
    async def get(session_id):
        nonlocal attempts
        attempts += 1
        return {'required_actions': [] if attempts == 1 else [{'name': 'cancellation_checkpoint'}]}
    async def turns(session_id, **kwargs):
        return {'data': [{'id': 'old-turn', 'status': 'completed'}], 'has_more': False}
    async def sleep(seconds):
        pass
    monkeypatch.setattr(proof.asyncio, 'sleep', sleep)
    _, action = await proof.wait_for_action(SimpleNamespace(get_session=get, list_turns=turns),
        'session-1', 'cancellation_checkpoint', time.monotonic() + 5, prior_turns={'old-turn'})
    assert action['name'] == 'cancellation_checkpoint' and attempts == 2


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['enforced', 'monitored', 'resumed'])
async def test_full_proof_uses_real_transport_and_existing_ledger(tmp_path, monkeypatch, mode):
    import json
    import httpx
    from test_managed_usage import funded
    original_files = None
    if mode == 'resumed':
        args, pool, _, prior, _ = rejected_proof(tmp_path)
        original_files = {p.name: p.read_bytes() for p in args.proof_state.iterdir() if p.is_file()}
    else:
        pool = funded(tmp_path / 'workspace', 100)
        args = SimpleNamespace(workspace_state=tmp_path / 'workspace', pool_scope=pool.company_scope,
            actor='peter', proof_state=tmp_path / 'proof', limit_cents=100, timeout_seconds=30,
            spending_mode=mode, approval_record=None)
        if mode == 'monitored':
            args.approval_record = tmp_path / 'approval.json'
            proof.private_json(args.approval_record, fresh_approval(args))
    def attempt_path():
        if mode == 'resumed':
            return Path(json.loads((args.proof_state / 'monitored-resume.json').read_text())['attempt'])
        return args.proof_state
    monkeypatch.setenv('ROCOTO_OPENAI_API_KEY', 'fixture-credential')
    session = {'id': 'session-1', 'agent': {'id': 'root-agent'}, 'status': 'in_progress',
               'spend_control': {'limit': 100, 'consumed': 2}, 'required_actions': []}
    if mode != 'enforced':
        session.pop('spend_control')
        session['usage'] = {'input_tokens': 100, 'output_tokens': 200}
    turn = {'id': 'turn-1', 'session_id': 'session-1', 'subagent_id': None, 'status': 'in_progress'}
    turns = [turn]
    nonce = None
    created = 0
    submits = []
    deletes = 0
    def page(rows):
        return httpx.Response(200, json={'data': rows, 'has_more': False})
    def handler(request):
        nonlocal nonce, created, deletes
        path = request.url.path
        assert request.url.host == 'api.openai.com'
        assert request.headers['authorization'] == 'Bearer fixture-credential'
        if request.method == 'POST' and path == '/v1/agents/sessions':
            assert pool.summary()['reserved_usd'] == '1.000000'
            created += 1
            config = json.loads(request.content)
            nonce = config['metadata']['rocoto_proof']
            if mode == 'enforced':
                assert config['spend_control'] == {'limit': 100}
            else:
                assert 'spend_control' not in config
            if mode == 'resumed':
                assert request.headers['idempotency-key'] != prior['creation_key']
            session['required_actions'] = [{'type': 'function_call', 'call_id': 'call-1',
                'turn_id': 'turn-1', 'name': 'proof_receipt', 'arguments': {}}]
            return httpx.Response(200, json=session)
        if request.method == 'POST' and path.endswith('/events'):
            event = json.loads(request.content)['events'][0]
            if event['type'] == 'agent.session.input.tool_result':
                effect = json.loads((attempt_path() / 'synthetic-effect.json').read_text())
                assert effect == {'nonce': nonce, 'executions': 1}
                submits.append((event, request.headers['idempotency-key']))
                turn['status'] = 'completed'
                session.update(status='idle', required_actions=[])
            elif event['type'] == 'agent.session.input.message':
                turns.append({'id': 'turn-2', 'session_id': 'session-1', 'subagent_id': None, 'status': 'in_progress'})
                session.update(status='in_progress', required_actions=[{'type': 'function_call',
                    'call_id': 'call-2', 'turn_id': 'turn-2', 'name': 'cancellation_checkpoint', 'arguments': {}}])
            else:
                assert event['type'] == 'agent.session.input.cancel'
                turns[-1]['status'] = 'cancelled'
                session.update(status='idle', required_actions=[])
            return httpx.Response(200, json={})
        if request.method == 'DELETE':
            deletes += 1
            return httpx.Response(200, json={'id': 'session-1', 'deleted': True})
        if path.endswith('/events'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=b'')
        if path.endswith('/artifacts/artifact-1/content'):
            return httpx.Response(200, json={'nonce': nonce, 'answer': 5})
        if path.endswith('/artifacts'):
            return page([{'id': 'artifact-1', 'turn_id': 'turn-1', 'path': proof.OUTPUT_PATH}])
        if path.endswith('/subagents/child-1/turns'):
            return page([{'id': 'child-turn', 'subagent_id': 'child-1', 'status': 'completed'}])
        if path.endswith('/subagents/child-1/items'):
            return page([{'id': 'child-item', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                          'content': [{'type': 'output_text', 'text': '5'}]}])
        if path.endswith('/subagents'):
            return page([{'id': 'child-1', 'session_id': 'session-1', 'parent_agent_id': 'root-agent'}])
        if path.endswith('/turns/turn-1'):
            return httpx.Response(200, json=turn)
        if path.endswith('/turns'):
            return page(turns)
        if path.endswith('/items'):
            return page([])
        assert path == '/v1/agents/sessions/session-1', path
        return httpx.Response(200, json=session)
    original = proof.ProofClient
    monkeypatch.setattr(proof, 'ProofClient', lambda key, **kwargs: original(
        key, transport=httpx.MockTransport(handler), **kwargs))
    assert await proof.run(args) == 0
    receipt = json.loads((attempt_path() / 'receipt.json').read_text())
    assert receipt['passed'] and receipt['cleanup']['deleted'] and not receipt['remote_mcp_verified']
    assert receipt['checks']['stock_children'] == [{'id': 'child-1', 'assistant_items': 1}]
    assert len(submits) == 2 and submits[0] == submits[1]
    assert created == 1 and deletes == 1
    assert pool.summary()['reserved_usd'] == '1.000000'
    assert pool.summary()['provider_actual_cost_usd'] == '0.000000'
    if mode != 'enforced':
        assert receipt['spending_mode'] == 'monitored'
        assert receipt['monitor']['observed_cents'] == pytest.approx(1.125)
        assert receipt['monitor']['billed_cents'] is None
        assert receipt['monitor']['other_provider_costs'] == 'unknown'
        assert receipt['overshoot_notice'] == proof.OVERSHOOT
    if mode == 'resumed':
        assert original_files == {name: (args.proof_state / name).read_bytes() for name in original_files}
        assert receipt['funding_binding_id'] == prior['binding_id']
        saved = proof.ManagedUsage(pool).request(prior['request_id'])
        assert saved['session_id'] == 'session-1' and saved['consumed_cents'] is None


def test_monitored_configuration_omits_provider_budget():
    config = proof.configuration(100, 'fixture', spending_mode='monitored')
    assert 'spend_control' not in config
    assert config['environment']['network']['access'] == 'disabled'


def rejected_proof(tmp_path):
    import hashlib
    import json
    from datetime import datetime, timezone
    from test_managed_usage import funded
    pool = funded(tmp_path / 'workspace', 100)
    usage = proof.ManagedUsage(pool)
    old = tmp_path / 'rejected'
    old.mkdir(mode=0o700)
    ledger = proof.ManagedSessions(old)
    config = proof.configuration(100, 'old-nonce')
    binding = ledger.binding(company_id=pool.company_scope, chat_id='proof-old-nonce',
                             owner='peter', collection='synthetic', configuration=config)
    request = ledger.submission(binding['id'], 'initial', 'create', config)
    usage.reserve(binding_id=binding['id'], request_id=request, actor='peter',
                  model=proof.MODEL, limit_cents=100)
    usage.finish(request, None, None)
    receipt = {'schema_version': 1, 'kind': 'managed_protocol_proof', 'nonce': 'old-nonce',
               'model': proof.MODEL, 'approved_limit_cents': 100, 'passed': False, 'checks': {},
               'binding_id': binding['id'], 'request_id': request, 'pool_scope': pool.company_scope,
               'creation_key': binding['creation_key'],
               'failure': {'type': 'ManagedCodexError', 'code': 'http_error', 'status_code': 400},
               'cleanup': {'deleted': False, 'ambiguous_creation_key': binding['creation_key']},
               'finished_at': '2026-10-11T22:12:38.404162+00:00'}
    diagnostic = {'status': 400, 'request_id': 'req_fixture', 'error': {
        'code': 'invalid_request_error', 'type': 'invalid_request_error', 'param': 'spend_control',
        'message': 'Session budget configuration is not enabled'}}
    proof.private_json(old / 'receipt.json', receipt)
    proof.private_json(old / 'diagnostic.json', diagnostic)
    approval = {'schema_version': 1, 'kind': 'managed_protocol_monitored_approval',
                'approved_by': 'peter', 'actor': 'peter', 'pool_scope': pool.company_scope,
                'spending_mode': 'monitored', 'target_cents': 100, 'overshoot_approved': True,
                'max_active_seconds': 300, 'authorization_reference': 'fixture approval',
                'user_instruction': 'I approve monitored spending that can exceed one dollar.',
                'approved_at': datetime.now(timezone.utc).isoformat(),
                'receipt_sha256': hashlib.sha256((old / 'receipt.json').read_bytes()).hexdigest(),
                'diagnostic_sha256': hashlib.sha256((old / 'diagnostic.json').read_bytes()).hexdigest(),
                'creation_key': binding['creation_key']}
    approval_path = tmp_path / 'approval.json'
    proof.private_json(approval_path, approval)
    args = SimpleNamespace(workspace_state=tmp_path / 'workspace', pool_scope=pool.company_scope,
        actor='peter', proof_state=old, limit_cents=100, timeout_seconds=30,
        spending_mode='monitored', resume_rejected=True, approval_record=approval_path)
    return args, pool, usage, receipt, approval


@pytest.mark.parametrize('schema', ['current', 'original'])
def test_resume_preserves_original_bytes_and_reuses_exact_hold_once(tmp_path, schema):
    args, pool, usage, old, approval = rejected_proof(tmp_path)
    if schema == 'original':
        import sqlite3
        with sqlite3.connect(args.proof_state / 'managed-sessions.sqlite3') as db:
            for table in ('artifacts', 'session_retirements', 'delivery_boundaries'):
                db.execute('DROP TABLE ' + table)
            assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
                'sessions', 'submissions', 'calls', 'requests', 'turns'}
    before = {p.name: p.read_bytes() for p in args.proof_state.iterdir() if p.is_file()}
    attempt = proof.prepare_attempt(args, pool, usage)
    assert attempt['funding_binding_id'] == old['binding_id']
    assert attempt['funding_request_id'] == old['request_id']
    assert attempt['binding']['id'] != old['binding_id']
    assert attempt['binding']['creation_key'] != old['creation_key']
    assert attempt['request_id'] != old['request_id']
    assert pool.summary()['reserved_usd'] == '1.000000'
    assert before == {name: (args.proof_state / name).read_bytes() for name in before}
    assert (attempt['proof'] / 'approval.json').read_bytes() == args.approval_record.read_bytes()
    assert (args.proof_state / 'monitored-resume.json').is_file()
    with pytest.raises((ValueError, FileExistsError)):
        proof.prepare_attempt(args, pool, usage)
    assert pool.summary()['reserved_usd'] == '1.000000'


@pytest.mark.parametrize('change', ['hash', 'session', 'ledger', 'diagnostic', 'hold', 'approval', 'timeout'])
def test_resume_rejects_ambiguous_or_changed_evidence(tmp_path, change):
    import hashlib
    import json
    args, pool, usage, old, approval = rejected_proof(tmp_path)
    if change == 'hash':
        old['nonce'] = 'changed'
        proof.private_json(args.proof_state / 'receipt.json', old)
    elif change == 'session':
        usage.created(old['binding_id'], 'existing-session')
    elif change == 'ledger':
        import sqlite3
        with sqlite3.connect(args.proof_state / 'managed-sessions.sqlite3') as db:
            db.execute("UPDATE sessions SET session_id='existing-session'")
    elif change == 'diagnostic':
        path = args.proof_state / 'diagnostic.json'
        value = json.loads(path.read_text())
        value['error']['param'] = 'unknown'
        proof.private_json(path, value)
        approval['diagnostic_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        proof.private_json(args.approval_record, approval)
    elif change == 'hold':
        with pool.connect() as db:
            db.execute('UPDATE managed_allowances SET consumed_cents=1 WHERE request_id=?', (old['request_id'],))
    elif change == 'approval':
        approval['overshoot_approved'] = False
        proof.private_json(args.approval_record, approval)
    else:
        args.timeout_seconds = 301
    with pytest.raises(ValueError):
        proof.prepare_attempt(args, pool, usage)
    assert not (args.proof_state / 'monitored-resume.json').exists()
    assert pool.summary()['reserved_usd'] == '1.000000'


def fresh_approval(args):
    from datetime import datetime, timezone
    return {'schema_version': 1, 'kind': 'managed_protocol_monitored_approval',
            'approved_by': 'peter', 'actor': args.actor, 'pool_scope': args.pool_scope,
            'spending_mode': 'monitored', 'target_cents': args.limit_cents, 'overshoot_approved': True,
            'max_active_seconds': 300, 'authorization_reference': 'fixture approval',
            'user_instruction': 'I approve monitored spending that can exceed one dollar.',
            'approved_at': datetime.now(timezone.utc).isoformat()}


@pytest.mark.anyio
@pytest.mark.parametrize('reason', ['target_reached', 'deadline'])
async def test_monitored_watchdog_stops_provider_and_retains_unknown_money(tmp_path, monkeypatch, reason):
    import asyncio
    import json
    from test_managed_usage import funded
    pool = funded(tmp_path / 'workspace', 100)
    args = SimpleNamespace(workspace_state=tmp_path / 'workspace', pool_scope=pool.company_scope,
        actor='peter', proof_state=tmp_path / 'proof', limit_cents=100, timeout_seconds=30,
        spending_mode='monitored', approval_record=tmp_path / 'approval.json')
    proof.private_json(args.approval_record, fresh_approval(args))
    monkeypatch.setenv('ROCOTO_OPENAI_API_KEY', 'fixture-credential')
    calls = []
    session = {'id': 'session-1', 'required_actions': []}
    if reason == 'target_reached':
        session['usage'] = {'input_tokens': 0, 'output_tokens': 20000}
    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs['spending_mode'] == 'monitored'
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def create_session(self, config, **kwargs):
            assert 'spend_control' not in config
            calls.append('create')
            return session
        async def get_session(self, session_id):
            return session
        async def interrupt(self, session_id, **kwargs):
            calls.append('interrupt')
        async def delete_session(self, session_id, **kwargs):
            calls.append('delete')
            return {'id': session_id, 'deleted': True}
    async def quiet(*args):
        assert 'interrupt' in calls
        calls.append('root_and_children_checked')
        return True, session
    async def action(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(proof, 'ProofClient', Client)
    monkeypatch.setattr(proof, 'quiescent', quiet)
    monkeypatch.setattr(proof, 'wait_for_action', action)
    if reason == 'deadline':
        monitor = proof.SpendMonitor
        class ShortDeadline(monitor):
            def __init__(self, target, deadline_at, **kwargs):
                super().__init__(target, time.time() + 0.02, **kwargs)
        monkeypatch.setattr(proof, 'SpendMonitor', ShortDeadline)
    assert await proof.run(args) == 1
    receipt = json.loads((args.proof_state / 'receipt.json').read_text())
    assert receipt['failure'] == {'type': 'MonitoredStop', 'reason': reason}
    assert receipt['monitor']['stop_reason'] == reason
    if reason == 'deadline':
        assert receipt['monitor']['usage_status'] == 'unknown'
        assert receipt['monitor']['observed_cents'] == 0
    assert receipt['monitor']['billed_cents'] is None
    assert receipt['cleanup']['deleted']
    assert calls == ['create', 'interrupt', 'root_and_children_checked', 'delete']
    assert pool.summary()['reserved_usd'] == '1.000000'
    assert pool.summary()['provider_actual_cost_usd'] == '0.000000'


@pytest.mark.parametrize('change', ['unknown_table', 'missing_required', 'saved_artifact'])
def test_resume_validates_known_execution_tables(tmp_path, change):
    import sqlite3
    args, pool, usage, old, _ = rejected_proof(tmp_path)
    with sqlite3.connect(args.proof_state / 'managed-sessions.sqlite3') as db:
        if change == 'unknown_table':
            db.execute('CREATE TABLE remote_execution (id TEXT)')
        elif change == 'missing_required':
            db.execute('DROP TABLE turns')
        else:
            db.execute('INSERT INTO artifacts VALUES (?,?,?,?)',
                       (old['binding_id'], 'artifact-1', 'metadata', 'file-1'))
    before = (args.proof_state / 'managed-sessions.sqlite3').read_bytes()
    with pytest.raises(ValueError):
        proof.prepare_attempt(args, pool, usage)
    assert not (args.proof_state / 'monitored-resume.json').exists()
    assert (args.proof_state / 'managed-sessions.sqlite3').read_bytes() == before
    assert pool.summary()['reserved_usd'] == '1.000000'


@pytest.mark.anyio
async def test_hosted_provisioning_waits_and_records_safe_protocol_shape(monkeypatch):
    readings = [
        {'id': 'session-1', 'status': 'requires_action',
         'environment': {'id': 'env-1', 'type': 'openai_hosted'},
         'required_actions': [{'type': 'environment_connection', 'environment_id': 'env-1'}]},
        {'id': 'session-1', 'status': 'requires_action',
         'environment': {'id': 'env-1', 'type': 'openai_hosted'},
         'required_actions': [{'type': 'function_call', 'name': 'proof_receipt'}]},
    ]
    projected = []
    async def get(session_id):
        return readings.pop(0)
    async def turns(session_id, **kwargs):
        return {'data': [], 'has_more': False}
    async def sleep(seconds):
        pass
    monkeypatch.setattr(proof.asyncio, 'sleep', sleep)
    _, result = await proof.wait_for_action(SimpleNamespace(get_session=get, list_turns=turns),
        'session-1', 'proof_receipt', time.monotonic() + 2,
        diagnostics=lambda value: projected.append(proof.protocol_shape(value)))
    assert result['name'] == 'proof_receipt'
    assert projected[0]['action_types'] == ['environment_connection']
    assert projected[0]['environment_type'] == 'openai_hosted'


def test_failure_details_keep_local_literal_message_and_reject_provider_text():
    def local():
        raise ValueError('Provider requested a different synthetic operation.')
    try:
        local()
    except ValueError as error:
        value = proof.failure_details(error, 'wait_for_receipt')
    assert value['stage'] == 'wait_for_receipt'
    assert value['message'] == 'Provider requested a different synthetic operation.'
    secret = 'fixture-provider-secret'
    try:
        raise ValueError(secret + ': dynamic response')
    except ValueError as error:
        assert secret not in str(proof.failure_details(error, 'wait_for_receipt'))
