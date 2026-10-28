"""Context captions use real money-ledger mechanics and mocked Responses streams."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from company_usage import CompanyUsage
from evidence_captions import EvidenceCaptions, CaptionScope, CaptionUnavailable, MODEL, MAX_OUTPUT


def fixture(tmp_path, *, funded=True, response_changes=None, caption='A person appears to pass a cup across the counter.', hook=None, model=MODEL):
    state = tmp_path / 'workspace'; state.mkdir(parents=True, exist_ok=True)
    pool = CompanyUsage.openai_pool(state)
    if funded:
        with pool.connect() as db:
            pool._fund(db, 'fixture', 1_000_000, 'prepaid_customer_funds', 'fixture-sha')
        pool.set_monthly_limit(0, '1')
    clip = {'id':'clip-a', 'dataset':'cafe', 'start':2, 'end':8}
    class Index:
        def get_clip(self, key):
            return clip if key == 'clip-a' else None
    observation = {'clip_id':'clip-a', 'model':'nvidia/Cosmos3-Super', 'model_revision':'fixture',
        'text':'A person appears to pass a cup across the counter; the handoff is partly occluded.',
        'prompt':'Describe the interaction at the counter.', 'run_id':'cosmos-fixture',
        'verification':'model_generated_unverified', 'created_at':'2026-10-06T10:00:00Z',
        'source':{'id':'clip-a', 'dataset':'cafe'}, 'source_url':'/?clip=clip-a'}
    directory = state/'descriptions'; directory.mkdir(exist_ok=True)
    source = directory/'clip-a.json'; source.write_text(json.dumps([observation]))
    original = source.read_bytes()
    current = [True]; permitted = [True]; calls = []; events = []
    async def upstream(request):
        assert pool.summary()['reserved_usd'] != '0.000000'
        calls.append(json.loads(request.content))
        if hook:
            result = hook(request, current, permitted)
            if result is not None:
                return result
        response = {'id':'resp_caption' + str(len(calls)), 'model':model, 'status':'completed', 'service_tier':'default',
            'usage':{'input_tokens':200, 'input_tokens_details':{'cached_tokens':20}, 'output_tokens':40},
            'output':[{'type':'message', 'content':[{'type':'output_text','text':caption}]}]}
        response.update(response_changes or {})
        stream = [ {'type':'response.output_text.delta', 'delta':caption[:20]},
            {'type':'response.output_text.delta', 'delta':caption[20:]},
            {'type':'response.completed', 'response':response} ]
        return httpx.Response(200, headers={'content-type':'text/event-stream'},
            content=''.join('data: '+json.dumps(event)+'\n\n' for event in stream).encode())
    service = EvidenceCaptions(state, api_key='fixture-private-key', model=model, pool=pool,
        transport=httpx.MockTransport(upstream), authorized=lambda scope:permitted[0],
        clock=lambda:datetime(2026,10,6,tzinfo=timezone.utc).timestamp())
    scope = CaptionScope('peter', 'chat-a', 'cafe', 'turn-a')
    kwargs = {'index':Index(), 'clip_id':'clip-a', 'question':'How are drinks handed over?',
        'current':lambda:current[0], 'emit':events.append}
    return service, scope, kwargs, calls, events, pool, source, original, current, permitted


def test_caption_streams_with_provenance_and_measured_payment(tmp_path):
    service, scope, kw, calls, events, pool, source, original, _, _ = fixture(tmp_path)
    result = asyncio.run(service.caption(scope, **kw))
    assert result['caption_kind'] == 'contextual_cosmos_summary'
    assert result['source_run_id'] == 'cosmos-fixture' and result['verification'] == 'model_generated_unverified'
    assert result['caption'] == 'A person appears to pass a cup across the counter.'
    assert len(result['observation_ref']) == len(result['usage_receipt_sha256']) == 64
    assert source.read_bytes() == original
    assert [event['type'] for event in events] == ['evidence_caption_delta', 'evidence_caption_delta', 'evidence_caption']
    assert events[-1]['status'] == 'complete'
    body = calls[0]
    assert body['store'] is False and body['tools'] == [] and body['max_output_tokens'] == MAX_OUTPUT
    assert json.loads(body['input'])['question'] == kw['question']
    assert pool.summary()['reserved_usd'] == '0.000000'
    assert pool.summary()['provider_actual_cost_usd'] == '0.000026'
    receipt = next(service.directory.glob('*.usage.json'))
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert 'fixture-private-key' not in receipt.read_text()


def test_exact_cache_reuse_is_free_and_scope_question_observation_changes_miss(tmp_path):
    service, scope, kw, calls, _, pool, source, _, _, _ = fixture(tmp_path)
    first = asyncio.run(service.caption(scope, **kw))
    second = asyncio.run(service.caption(scope, **kw))
    assert second['cached'] and first['caption'] == second['caption'] and len(calls) == 1
    asyncio.run(service.caption(scope, **{**kw,'question':'Is the cup visible?'}))
    asyncio.run(service.caption(CaptionScope('someone-else','chat-b','cafe','turn-b'), **kw))
    raw = json.loads(source.read_text()); raw[0]['text'] += ' The cup appears white.'; source.write_text(json.dumps(raw))
    asyncio.run(service.caption(scope, **kw))
    assert len(calls) == 4
    assert pool.summary()['reserved_usd'] == '0.000000'


def test_cache_read_requires_current_authority(tmp_path):
    service, scope, kw, calls, _, _, _, _, _, permitted = fixture(tmp_path)
    asyncio.run(service.caption(scope, **kw)); permitted[0] = False
    with pytest.raises(CaptionUnavailable, match='active source conversation'):
        asyncio.run(service.caption(scope, **kw))
    assert len(calls) == 1


def test_unfunded_request_and_wrong_collection_never_reach_provider(tmp_path):
    service, scope, kw, calls, _, _, _, _, _, _ = fixture(tmp_path, funded=False)
    with pytest.raises(CaptionUnavailable):
        asyncio.run(service.caption(scope, **kw))
    with pytest.raises(CaptionUnavailable):
        asyncio.run(service.caption(CaptionScope('peter','chat-a','warehouse','turn-a'), **kw))
    assert calls == []


def test_reference_resolves_only_saved_observation(tmp_path):
    service, scope, kw, calls, _, _, _, _, _, _ = fixture(tmp_path)
    with pytest.raises(CaptionUnavailable):
        asyncio.run(service.caption(scope, **kw, observation_ref='forged-reference'))
    assert calls == []


@pytest.mark.parametrize('change', [{'benchmark_only':True}, {'model_structure_valid':False}, {'clip_id':'foreign'}, {'source':{'id':'clip-a','dataset':'warehouse'}}])
def test_benchmark_invalid_and_misbound_observations_are_excluded(tmp_path, change):
    service, scope, kw, calls, _, _, source, _, _, _ = fixture(tmp_path)
    value = json.loads(source.read_text())[0]; value.update(change); source.write_text(json.dumps([value]))
    with pytest.raises(CaptionUnavailable):
        asyncio.run(service.caption(scope, **kw))
    assert calls == []


def test_network_failure_retains_hold_and_does_not_retry(tmp_path):
    def hook(*args):
        raise httpx.ReadError('synthetic interruption')
    service, scope, kw, calls, _, pool, _, _, _, _ = fixture(tmp_path, hook=hook)
    for _ in range(2):
        with pytest.raises(CaptionUnavailable):
            asyncio.run(service.caption(scope, **kw))
    assert len(calls) == 1
    assert pool.summary()['reserved_usd'] != '0.000000'
    assert pool.summary()['provider_actual_cost_usd'] == '0.000000'


def test_stop_after_dispatch_suppresses_publication_and_retains_hold(tmp_path):
    def hook(request, current, permitted):
        current[0] = False
    service, scope, kw, calls, events, pool, _, _, _, _ = fixture(tmp_path, hook=hook)
    with pytest.raises(CaptionUnavailable):
        asyncio.run(service.caption(scope, **kw))
    assert len(calls) == 1 and events == []
    assert pool.summary()['reserved_usd'] != '0.000000'


@pytest.mark.parametrize('caption', ['A person passes a cup.', 'A person appears to visit https://example.com', 'A person appears ' + 'x'*240])
def test_uncertainty_plaintext_and_length_are_enforced(tmp_path, caption):
    service, scope, kw, _, events, _, _, _, _, _ = fixture(tmp_path, caption=caption)
    with pytest.raises(CaptionUnavailable):
        asyncio.run(service.caption(scope, **kw))
    assert not any(event['type']=='evidence_caption' for event in events)


def test_unknown_provider_usage_remains_reserved(tmp_path):
    service, scope, kw, _, events, pool, _, _, _, _ = fixture(tmp_path,
        response_changes={'usage':{'input_tokens':200,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':10},'output_tokens':40}})
    with pytest.raises(CaptionUnavailable):
        asyncio.run(service.caption(scope, **kw))
    assert pool.summary()['reserved_usd'] != '0.000000'
    assert not any(event['type']=='evidence_caption' for event in events)


def test_nonreasoning_model_is_explicit_and_shutdown_blocks_new_calls(tmp_path):
    service, scope, kw, calls, _, _, _, _, _, _ = fixture(tmp_path, model='gpt-4.1-nano-2025-04-14')
    asyncio.run(service.caption(scope, **kw))
    assert 'reasoning' not in calls[0]
    service.clock = lambda:datetime(2026,10,23,tzinfo=timezone.utc).timestamp()
    with pytest.raises(CaptionUnavailable, match='supported caption model'):
        asyncio.run(service.caption(scope, **{**kw,'question':'Another question'}))
    assert len(calls) == 1
