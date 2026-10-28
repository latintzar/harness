"""Manufacturer research uses the native run ledger; these proofs make zero provider calls."""
import asyncio
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'verticals/manufacturer'))
from research_queue import ResearchStore, ManufacturerResearchInputs
from background_agents import AgentStore


def order(key='o-1'):
    return {'id': key, 'number': key, 'customer': {'company': 'Example Works',
        'website': 'https://example.com', 'email': 'buyer@example.com'}}


def findings():
    return {'status': 'enriched', 'summary': 'Example Works makes metal components.',
        'email_snippet': 'Example Works manufactures metal components.',
        'sources': [{'url': 'https://example.com/about', 'title': 'About Example Works'}],
        'facts': [{'label': 'Business', 'text': 'Manufactures metal components.',
            'source_urls': ['https://example.com/about']}], 'review_notes': ''}


def test_company_scope_claim_and_idempotent_completion(tmp_path):
    store = ResearchStore(tmp_path, 'company-a')
    store.enqueue('company-a', [order(), order()], now=100)
    with pytest.raises(ValueError):
        store.get('company-b', 'o-1')
    claimed = store.claim_next('company-a', 'chat-a', 'call-a', now=100)
    assert claimed['status'] == 'researching'
    assert store.claim_next('company-a', 'chat-b', 'call-b', now=101) is None
    assert store.claim_next('company-a', 'chat-a', 'call-a', now=102)['lease_token'] == claimed['lease_token']
    with pytest.raises(ValueError):
        store.complete('company-a', 'o-1', 'chat-b', claimed['lease_token'], findings(), now=103)
    done = store.complete('company-a', 'o-1', 'chat-a', claimed['lease_token'], findings(), now=103)
    assert done['status'] == 'enriched'
    assert 'lease_token' not in store.get('company-a', 'o-1')
    assert store.complete('company-a', 'o-1', 'chat-a', claimed['lease_token'], findings(), now=104) == done
    assert store.claim_next('company-a', 'chat-a', 'call-a', now=105)['status'] == 'enriched'


def test_retry_after_restart_and_stale_completion_fenced(tmp_path):
    store = ResearchStore(tmp_path, 'company-a')
    store.enqueue('company-a', [order()], now=100)
    first = store.claim_next('company-a', 'chat-a', 'call-a', now=100)
    restarted = ResearchStore(tmp_path, 'company-a')
    assert restarted.claim_next('company-a', 'chat-b', 'call-b', now=101) is None
    second = restarted.claim_next('company-a', 'chat-b', 'call-b', now=first['lease_until'] + 61)
    assert second['attempts'] == 2
    with pytest.raises(ValueError):
        restarted.complete('company-a', 'o-1', 'chat-a', first['lease_token'], findings(), now=second['updated_at'])
    result = restarted.fail('company-a', 'o-1', 'chat-b', second['lease_token'], 'Source needs another attempt.', now=second['updated_at'])
    assert result['status'] == 'queued'
    third = restarted.claim_next('company-a', 'chat-c', 'call-c', now=result['next_attempt_at'])
    failed = restarted.fail('company-a', 'o-1', 'chat-c', third['lease_token'], 'Source needs review.', now=third['updated_at'])
    assert failed['status'] == 'failed'


def test_evidence_requires_public_sources_and_fact_citations(tmp_path):
    store = ResearchStore(tmp_path, 'company-a')
    store.enqueue('company-a', [order()], now=100)
    job = store.claim_next('company-a', 'chat-a', 'call-a', now=100)
    for source in ['http://127.0.0.1/info', 'https://admin:secret@example.com', 'file:///etc/passwd']:
        body = findings()
        body['sources'][0]['url'] = source
        with pytest.raises(ValueError):
            store.complete('company-a', 'o-1', 'chat-a', job['lease_token'], body, now=101)
    body = findings()
    body['facts'][0]['source_urls'] = ['https://another.example/about']
    with pytest.raises(ValueError):
        store.complete('company-a', 'o-1', 'chat-a', job['lease_token'], body, now=101)


def test_native_provider_skips_empty_backlog_and_claims_in_run_transaction(tmp_path):
    class Source:
        rows = []
        def status(self):
            return {'status': 'connected'}
        def orders(self, limit=100, offset=0):
            return self.rows[offset:offset + limit]
    source = Source()
    research = ResearchStore(tmp_path, 'company-a')
    provider = ManufacturerResearchInputs(research, source)
    agents = AgentStore(tmp_path, input_provider=provider)
    definition = {'title': 'Customer research', 'instruction': 'Research one customer and save evidence.',
        'dataset': 'company', 'agent_kind': 'action', 'input_kind': 'order_research',
        'mode': 'scheduled', 'interval_seconds': 60, 'max_runs': 3}
    saved = agents.create(definition, ['company'], owner_principal_id='person-a')
    assert provider.execution(saved)['ready'] is True
    now = time.time()
    agents.activate(saved['id'], now + 3600, now=now)
    asyncio.run(provider.refresh(agents.list()))
    assert not provider.ready(saved, None, None, False)
    assert agents.claim(saved['id'], {}, now=now) is None
    assert agents.get(saved['id'])['runs_started'] == 0
    source.rows = [order()]
    asyncio.run(provider.refresh(agents.list()))
    assert provider.ready(saved, None, None, False)
    batch = asyncio.run(provider.prepare(saved, None, None))
    run = agents.claim(saved['id'], batch.sources, source_records=batch.records, now=now + 61)
    assert run['research_order_id'] == 'o-1'
    assert research.get('company-a', 'o-1')['status'] == 'researching'
    with provider.context(None, run) as prompt:
        assert 'lease_token' in prompt
        assert 'o-1' in prompt
    agents.finish(run['id'], 'completed', now=now + 62)
    assert research.get('company-a', 'o-1')['status'] == 'queued'


def test_new_orders_are_scanned_without_activating_or_spending(tmp_path):
    class Source:
        def orders(self, limit=100, offset=0):
            return [order()][offset:offset + limit]
    store = ResearchStore(tmp_path, 'company-a')
    provider = ManufacturerResearchInputs(store, Source())
    native = AgentStore(tmp_path, input_provider=provider)
    asyncio.run(provider.refresh([]))
    assert store.get('company-a', 'o-1')['status'] == 'queued'
    assert native.list() == []
    assert native.path == store.path


def test_one_company_lease_under_concurrent_claims(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = ResearchStore(tmp_path, 'company-a')
    store.enqueue('company-a', [order('o-1'), order('o-2')], now=100)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda owner: store.claim_next('company-a', owner, owner, now=100), ['chat-a', 'chat-b']))
    assert sum(result is not None for result in results) == 1
    assert store.list('company-a')['counts'] == {'queued': 1, 'researching': 1}


def test_native_claim_rollback_leaves_order_queued(tmp_path):
    class Source:
        def orders(self, limit=100, offset=0):
            return [order()][offset:offset + limit]
    class InterruptedInputs(ManufacturerResearchInputs):
        def claimed(self, db, record, run):
            super().claimed(db, record, run)
            raise RuntimeError('Transaction interrupted')
    research = ResearchStore(tmp_path, 'company-a')
    provider = InterruptedInputs(research, Source())
    native = AgentStore(tmp_path, input_provider=provider)
    saved = native.create({'title': 'Research', 'instruction': 'Research the reserved company.',
        'dataset': 'company', 'input_kind': 'order_research'}, ['company'], owner_principal_id='person-a')
    now = time.time()
    native.activate(saved['id'], now + 3600, now=now)
    asyncio.run(provider.refresh([]))
    with pytest.raises(RuntimeError, match='Transaction interrupted'):
        native.claim(saved['id'], {}, now=now + 1)
    assert research.get('company-a', 'o-1')['status'] == 'queued'
    assert native.get(saved['id'])['runs_started'] == 0


def test_native_recovery_keeps_original_lease_and_completed_result(tmp_path):
    class Source:
        def orders(self, limit=100, offset=0):
            return [order()][offset:offset + limit]
    research = ResearchStore(tmp_path, 'company-a')
    provider = ManufacturerResearchInputs(research, Source())
    native = AgentStore(tmp_path, input_provider=provider, authority=lambda record, actor: True)
    saved = native.create({'title': 'Research', 'instruction': 'Research the reserved company.',
        'dataset': 'company', 'input_kind': 'order_research', 'mode': 'scheduled',
        'interval_seconds': 60, 'max_runs': 2}, ['company'], owner_principal_id='person-a')
    now = time.time()
    native.activate(saved['id'], now=now, standing=True, actor_principal_id='person-a')
    asyncio.run(provider.refresh([]))
    run = native.claim(saved['id'], {}, now=now + 1)
    native.mark_conversation_run(run['id'], 0)
    native.recover()
    restarted = ResearchStore(tmp_path, 'company-a')
    assert restarted.claim_next('company-a', 'other-chat', 'other-call', now=now + 4000) is None
    assert restarted.get('company-a', 'o-1')['status'] == 'researching'
    with restarted.transaction() as db:
        reserved = restarted._read(db, 'o-1')
    result = restarted.complete('company-a', 'o-1', run['chat_id'], reserved['lease_token'], findings(), now=now + 2)
    native.finish(run['id'], 'completed', now=now + 3)
    assert restarted.get('company-a', 'o-1') == result
    asyncio.run(provider.refresh([]))
    assert restarted.pending(now=now + 4000) is None


def test_tools_validate_scope_and_public_surface(tmp_path):
    from types import SimpleNamespace
    import research_tools
    from host import Admission
    from tool_families import ToolContext, ToolRegistry
    store = ResearchStore(tmp_path, 'company-a')
    store.enqueue('company-a', [order()])
    extension = SimpleNamespace(research=store, selected={'state': tmp_path,
        'business_scope': SimpleNamespace(company_id='company-a')})
    registry = ToolRegistry([research_tools])
    assert registry.read_names('public') == set()
    allowed = {'state': tmp_path, 'company_id': 'company-a', 'actor': 'person-a',
        'data_owner': 'person-a', 'collection': 'company', 'extension': extension}
    current = ToolContext(**allowed, admission=Admission(lambda: True))
    value = asyncio.run(registry.invoke(current, 'get_order_research', {'order_id': 'o-1'}))
    assert value['research']['status'] == 'queued'
    revoked = ToolContext(**allowed, admission=Admission(lambda: False))
    with pytest.raises(ValueError):
        asyncio.run(registry.invoke(revoked, 'get_order_research', {'order_id': 'o-1'}))
    wrong = ToolContext(**{**allowed, 'company_id': 'company-b'}, admission=Admission(lambda: True))
    with pytest.raises(ValueError):
        asyncio.run(registry.invoke(wrong, 'get_order_research', {'order_id': 'o-1'}))



def test_undispatched_native_run_returns_order_without_spending_attempt(tmp_path):
    class Source:
        def orders(self, limit=100, offset=0):
            return [order()][offset:offset + limit]
    research = ResearchStore(tmp_path, 'company-a')
    provider = ManufacturerResearchInputs(research, Source())
    native = AgentStore(tmp_path, input_provider=provider, authority=lambda record, actor: True)
    saved = native.create({'title': 'Research', 'instruction': 'Research the reserved company.',
        'dataset': 'company', 'input_kind': 'order_research', 'mode': 'scheduled',
        'interval_seconds': 60, 'max_runs': 2}, ['company'], owner_principal_id='person-a')
    now = time.time()
    native.activate(saved['id'], now=now, standing=True, actor_principal_id='person-a')
    asyncio.run(provider.refresh([]))
    run = native.claim(saved['id'], {}, now=now + 1)
    assert research.get('company-a', 'o-1')['attempts'] == 1
    native.defer_unstarted_run(run['id'])
    returned = research.get('company-a', 'o-1')
    assert returned['status'] == 'queued'
    assert returned['attempts'] == 0
    assert native.get(saved['id'])['runs_started'] == 0
