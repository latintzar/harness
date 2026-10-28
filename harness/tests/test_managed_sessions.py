"""Durable managed bindings and effect claims; no provider requests."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from managed_sessions import ManagedSessions


def binding(store):
    return store.binding(company_id='company', chat_id='chat', owner='owner',
                         collection='company', configuration={'agent': {'model': 'fixture'}})


def action(name='save_value'):
    return {'type': 'function_call', 'turn_id': 'turn-1', 'call_id': 'call-1',
            'name': name, 'arguments': {'value': 1}}


def test_submission_keys_survive_restart_and_bind_exact_input(tmp_path):
    store = ManagedSessions(tmp_path)
    selected = binding(store)
    key = store.submission(selected['id'], 'message-1', 'message', {'text': 'Hello'})
    reopened = ManagedSessions(tmp_path)
    assert binding(reopened)['id'] == selected['id']
    assert reopened.submission(selected['id'], 'message-1', 'message', {'text': 'Hello'}) == key
    with pytest.raises(ValueError):
        reopened.submission(selected['id'], 'message-1', 'message', {'text': 'Changed'})
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert b'Hello' not in store.path.read_bytes()


def test_owner_collection_and_configuration_never_rebind_a_live_session(tmp_path):
    store = ManagedSessions(tmp_path)
    selected = binding(store)
    store.created(selected['id'], 'session-1')
    for changes in ({'owner': 'other'}, {'collection': 'other'}, {'configuration': {'changed': True}}):
        with pytest.raises(ValueError):
            store.binding(**({'company_id': 'company', 'chat_id': 'chat', 'owner': 'owner',
                'collection': 'company', 'configuration': {'agent': {'model': 'fixture'}}} | changes))
    assert store.get(selected['id'])['session_id'] == 'session-1'


def test_completed_effect_replays_its_exact_receipt_after_restart(tmp_path):
    store = ManagedSessions(tmp_path)
    selected = binding(store)
    call = store.claim(selected['id'], action(), generation='one', actor_chat='chat', write=True)
    assert call['decision'] == 'execute'
    receipt = {'success': True, 'output': 'Saved revision 2'}
    store.completed(selected['id'], 'call-1', 'one', receipt)
    recovered = ManagedSessions(tmp_path).claim(selected['id'], action(), generation='two', actor_chat='chat', write=True)
    assert recovered['decision'] == 'replay' and recovered['receipt'] == receipt
    assert recovered['submission_key'] == call['submission_key']
    with pytest.raises(ValueError):
        store.claim(selected['id'], {**action(), 'arguments': {'value': 2}},
                    generation='two', actor_chat='chat', write=True)


def test_uncertain_mutation_is_reviewed_and_read_can_restart(tmp_path):
    store = ManagedSessions(tmp_path)
    selected = binding(store)
    first = store.claim(selected['id'], action(), generation='one', actor_chat='chat', write=True)
    assert first['decision'] == 'execute'
    assert store.claim(selected['id'], action(), generation='one', actor_chat='chat', write=True)['decision'] == 'pending'
    recovered = store.claim(selected['id'], action(), generation='two', actor_chat='chat', write=True)
    assert recovered['decision'] == 'review' and recovered['receipt']['success'] is False
    with pytest.raises(ValueError):
        store.completed(selected['id'], 'call-1', 'one', {'success': True, 'output': 'Late'})
    read = {**action('read_value'), 'call_id': 'read-1'}
    assert store.claim(selected['id'], read, generation='one', actor_chat='chat', write=False)['decision'] == 'execute'
    assert store.claim(selected['id'], read, generation='two', actor_chat='chat', write=False)['decision'] == 'execute'


def test_duplicate_concurrent_claims_execute_once(tmp_path):
    selected = binding(ManagedSessions(tmp_path))
    def claim(_):
        return ManagedSessions(tmp_path).claim(selected['id'], action(), generation='one', actor_chat='chat', write=True)['decision']
    with ThreadPoolExecutor(max_workers=4) as pool:
        choices = list(pool.map(claim, range(4)))
    assert choices.count('execute') == 1 and choices.count('pending') == 3


def test_history_function_item_is_not_a_pending_action(tmp_path):
    store = ManagedSessions(tmp_path)
    selected = binding(store)
    with pytest.raises(ValueError):
        store.claim(selected['id'], {**action(), 'type': 'message'}, generation='one', actor_chat='chat', write=True)


def test_turn_identity_and_terminal_state_are_monotonic(tmp_path):
    store = ManagedSessions(tmp_path)
    selected = binding(store)
    store.turn(selected['id'], {'id': 'turn-1', 'subagent_id': None, 'status': 'in_progress'}, 'chat')
    store.turn(selected['id'], {'id': 'turn-1', 'subagent_id': None, 'status': 'completed'}, 'chat')
    store.turn(selected['id'], {'id': 'turn-1', 'subagent_id': None, 'status': 'in_progress'}, 'chat')
    assert store.turns(selected['id'])[0]['status'] == 'completed'
    with pytest.raises(ValueError):
        store.turn(selected['id'], {'id': 'turn-1', 'subagent_id': 'other', 'status': 'completed'}, 'child')


def test_pending_input_survives_restart_and_blocks_another_input(tmp_path):
    store = ManagedSessions(tmp_path)
    selected = binding(store)
    first = store.request(selected['id'], 'message-1', [{'role': 'user', 'content': 'Hello'}])
    assert store.pending(selected['id'])['id'] == first['id']
    with pytest.raises(ValueError):
        store.request(selected['id'], 'message-2', [{'role': 'user', 'content': 'Later'}])
    store.request_state(first['id'], 'submitted', baseline=['old-turn'])
    recovered = ManagedSessions(tmp_path).pending(selected['id'])
    assert recovered['input'] == [{'role': 'user', 'content': 'Hello'}]
    assert recovered['baseline'] == ['old-turn'] and recovered['status'] == 'submitted'
    store.request_state(first['id'], 'completed')
    assert store.pending(selected['id']) is None
    assert store.request(selected['id'], 'message-2', [])['status'] == 'prepared'


def test_receipt_store_rejects_symlinks_and_shared_permissions(tmp_path):
    outside = tmp_path / 'outside'
    outside.write_text('keep')
    (tmp_path / 'managed-sessions.sqlite3').symlink_to(outside)
    with pytest.raises(OSError):
        ManagedSessions(tmp_path)
    assert outside.read_text() == 'keep'
    (tmp_path / 'managed-sessions.sqlite3').unlink()
    store = ManagedSessions(tmp_path)
    store.path.chmod(0o644)
    with pytest.raises(ValueError):
        ManagedSessions(tmp_path)


def test_rollover_preserves_original_requests_and_requires_quiescence(tmp_path):
    store = ManagedSessions(tmp_path)
    first = binding(store)
    store.created(first['id'], 'session-1')
    request = store.request(first['id'], 'message-1', [])
    values = dict(company_id='company', chat_id='chat', owner='owner', collection='company',
                  configuration={'agent': {'model': 'new'}}, previous_id=first['id'])
    with pytest.raises(ValueError):
        store.rollover(**values, quiesced=True)
    store.request_state(request['id'], 'completed')
    with pytest.raises(ValueError):
        store.rollover(**values, quiesced=False)
    second = store.rollover(**values, quiesced=True)
    assert second['generation'] == 2 and second['id'] != first['id']
    assert store.for_chat('company', 'chat')['id'] == second['id']
    assert store.request_binding('company', 'chat', 'message-1')['id'] == first['id']
    assert store.get(first['id'])['session_id'] == 'session-1'
    assert ManagedSessions(tmp_path).get(first['id'])['configuration_body'] == {'agent': {'model': 'fixture'}}
    with pytest.raises(ValueError):
        store.request(first['id'], 'stale-new-input', [])


def test_capability_identity_is_part_of_binding_even_with_identical_zip_bytes(tmp_path):
    store = ManagedSessions(tmp_path)
    values = dict(company_id='company', chat_id='chat', owner='owner', collection='company', configuration={})
    store.binding(**values, capabilities={'generation': 1})
    with pytest.raises(ValueError):
        store.binding(**values, capabilities={'generation': 2})


def test_legacy_schema_migration_preserves_foreign_keys_and_uncertain_work(tmp_path):
    store = ManagedSessions(tmp_path)
    first = binding(store)
    request = store.request(first['id'], 'message-1', [])
    # Recreate the original parent-table schema while retaining its referencing rows.
    with sqlite3.connect(store.path) as db:
        db.executescript('''
            PRAGMA foreign_keys=OFF;
            CREATE TABLE legacy_sessions (
                id TEXT PRIMARY KEY, company_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                owner TEXT NOT NULL, collection TEXT NOT NULL, configuration TEXT NOT NULL,
                session_id TEXT UNIQUE, creation_key TEXT NOT NULL, UNIQUE(company_id, chat_id));
            INSERT INTO legacy_sessions SELECT id,company_id,chat_id,owner,collection,configuration,session_id,creation_key FROM sessions;
            DROP TABLE sessions;
            ALTER TABLE legacy_sessions RENAME TO sessions;
        ''')
    restored = ManagedSessions(tmp_path)
    assert restored.pending(first['id'])['id'] == request['id']
    assert restored.get(first['id'])['configuration_body'] is None
    with restored.transaction() as db:
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
