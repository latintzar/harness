"""Failed video answers stay failed across receipts, native tools and accounting."""
import asyncio
import contextlib
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent, tool_specs
import cosmos_inspect
from company_usage import CompanyUsage
from compute_guard import OWNER
from query_budget import QueryBudget
from test_company_usage import approval
from test_investigation_plans import Index, document
from test_isolated_turn import tree_fixture


QUESTION = 'Describe the visible sequence.'


@pytest.mark.parametrize('length,accepted',[(0,False),(1,True),(2000,True),(2001,False)])
def test_advertised_inspection_question_limit_matches_dispatcher(tmp_path,monkeypatch,length,accepted):
    from jsonschema import Draft202012Validator
    agent=CodexVideoAgent(tmp_path/'state',Index(tmp_path),lambda:[])
    plan_for(agent,['clip-0'])
    args=arguments(['clip-0']);args['question']='x'*length
    schema=next(item['inputSchema'] for item in tool_specs() if item['name']=='cosmos_inspect')
    assert Draft202012Validator(schema).is_valid(args) is accepted
    raw=result([{**answer(),'question':args['question']}])
    provider=AsyncMock(return_value=raw);monkeypatch.setattr(cosmos_inspect,'inspect_live',provider)
    _,_,success=asyncio.run(agent._tool('cosmos_inspect',args,'cafe','chat'))
    assert success is accepted
    assert provider.await_count==int(accepted)


def answer(*, parsed=True, text='A person carries a cup.', clip='clip-0'):
    return {'clip_ids': [clip], 'question': QUESTION, 'parsed': parsed,
            'final_text': text, 'parse_error': 'Inspection requires a final answer' if not parsed else None}


def result(requests):
    return {'model': cosmos_inspect.MODEL, 'model_revision': cosmos_inspect.REVISION,
            'requests': requests, 'run_id': 'inspection-fixture', 'timing': {'worker_seconds': 1}}


def plan_for(agent, clips):
    body = document()
    body['direct_clip_ids'] = clips
    plan = agent._plans().save('cafe', 'chat', body)
    agent.active.setdefault('chat', {})['plan_id'] = plan['id']
    return plan


def arguments(clips):
    return {'clip_ids': clips, 'question': QUESTION, 'category_ids': ['service']}


@pytest.mark.parametrize('requests', [
    [answer(parsed=False, text='Unparsed original answer')],
    [answer(text='')], [answer(text=' \n ')], [],
    [answer(), answer(parsed=False, text='', clip='clip-1')],
])
def test_failed_or_partial_answer_keeps_attempt_and_creates_no_inspection_receipt(tmp_path, monkeypatch, requests):
    agent = CodexVideoAgent(tmp_path / 'state', Index(tmp_path), lambda: [])
    clips = ['clip-0', 'clip-1'] if len(requests) == 2 else ['clip-0']
    plan_for(agent, clips)
    raw = result(requests)
    original = copy.deepcopy(raw)
    provider = AsyncMock(return_value=raw)
    monkeypatch.setattr(cosmos_inspect, 'inspect_live', provider)
    content, evidence, success = asyncio.run(agent._tool('cosmos_inspect', arguments(clips), 'cafe', 'chat'))
    assert success is False
    public = json.loads(content[0]['text'])
    assert public['status'] == 'failed'
    assert public['error']['code'] == 'inspection_incomplete'
    assert public['requests'] == original['requests']
    assert public['run_id'] == original['run_id']
    assert 'investigation' not in public and evidence is None
    assert raw == original
    with agent._plans().connect() as db:
        assert db.execute('SELECT COUNT(*) FROM inspection_receipts').fetchone()[0] == 0
    provider.assert_awaited_once()


@pytest.mark.parametrize('record', [answer(parsed=False), answer(text=''), answer(text=' \n ')])
def test_receipt_store_also_rejects_incomplete_answers(tmp_path, record):
    agent = CodexVideoAgent(tmp_path / 'state', Index(tmp_path), lambda: [])
    plan = plan_for(agent, ['clip-0'])
    with pytest.raises(ValueError, match='complete'):
        agent._plans().record_inspection(plan['id'], 'cafe', 'chat', ['service'], QUESTION,
                                         ['clip-0'], result([record]))


def test_historical_complete_cached_answer_remains_usable_without_sampling_metadata(tmp_path, monkeypatch):
    agent = CodexVideoAgent(tmp_path / 'state', Index(tmp_path), lambda: [])
    plan_for(agent, ['clip-0'])
    raw = dict(result([answer()]), cached=True)
    monkeypatch.setattr(cosmos_inspect, 'inspect_live', AsyncMock(return_value=raw))
    content, _, success = asyncio.run(agent._tool('cosmos_inspect', arguments(['clip-0']), 'cafe', 'chat'))
    assert success is True
    public = json.loads(content[0]['text'])
    saved = agent._plans().get_inspection(public['investigation']['inspection_receipt'], 'cafe', 'chat')
    assert saved['result']['requests'][0]['final_text'] == raw['requests'][0]['final_text']
    assert saved['result']['cached'] is True


def test_native_reply_and_saved_tool_trail_report_failure(tmp_path, monkeypatch):
    async def scenario():
        agent, run, connection, tree = tree_fixture(tmp_path)
        agent.retrieval = Index(tmp_path)
        plan_for(agent, ['clip-0'])
        raw = result([answer(parsed=False, text='')])
        monkeypatch.setattr(cosmos_inspect, 'inspect_live', AsyncMock(return_value=raw))
        await tree.tool({'id': 0, 'params': {'tool': 'cosmos_inspect', 'arguments': arguments(['clip-0'])}}, 'chat')
        assert connection.writes[-1]['id'] == 0
        assert connection.writes[-1]['result']['success'] is False
        events = list(run['tool_events']._queue)
        end = next(event for event in events if event['type'] == 'tool_end')
        assert end['success'] is False
        assert json.loads(end['output'])['requests'] == raw['requests']
        saved_answer = agent.children['chat']['messages'][-1]
        for event in events:
            agent._record_event(saved_answer, event)
        agent._save_child(agent.children['chat'])
        assert saved_answer['tools'][-1]['status'] == 'failed'
        assert 'inspection-fixture' in json.dumps(saved_answer)
    asyncio.run(scenario())


@pytest.mark.parametrize('record,cacheable', [
    (answer(parsed=False, text='Raw unparsed answer'), False),
    (answer(text=''), False), (answer(), True),
])
def test_attempt_is_preserved_and_only_complete_answer_is_cached(tmp_path, monkeypatch, record, cacheable):
    index = Index(tmp_path)
    state = tmp_path / 'state'
    clips = [{**index.get_clip('clip-0'), 'sha256': 'source-fixture'}]
    requests = [{'clip_ids': ['clip-0'], 'question': QUESTION}]
    raw = result([record])
    monkeypatch.setattr(cosmos_inspect, '_prepare_source_clip',
                        lambda clip, path, **kwargs: path.write_bytes(b'opaque-fixture'))
    monkeypatch.setattr(cosmos_inspect, 'load_manifest', lambda *args, **kwargs: (clips, requests))
    monkeypatch.setattr('camera_context.get_context_for_clip', lambda *args: None)
    monkeypatch.setattr(cosmos_inspect, '_modal_runtime', lambda *args: (Path('/fixture-python'), {}))
    monkeypatch.setattr('execution_policy.admit_fresh_inspection', lambda *args: None)
    async def start(*command, **kwargs):
        output = Path(command[command.index('--output') + 1])
        async def wait():
            output.write_text(json.dumps(raw))
            return 0
        return SimpleNamespace(returncode=0, wait=wait)
    process = AsyncMock(side_effect=start)
    monkeypatch.setattr(cosmos_inspect.asyncio, 'create_subprocess_exec', process)
    async def run():
        received = await cosmos_inspect._inspect_sources(['clip-0'], requests, index=index,
            state=state, query_approval_path=state / 'fixture-approval.json')
        saved = state / 'reasoner-runs' / received['run_id'] / 'result.json'
        assert json.loads(saved.read_text()) == raw
        assert received['requests'] == raw['requests']
        assert bool(list((state / 'reasoner-cache').glob('*.json'))) is cacheable
        if cacheable:
            # Existing valid cache entries remain usable with their original metadata.
            cached = await cosmos_inspect._inspect_sources(['clip-0'], requests, index=index,
                state=state, query_approval_path=state / 'fixture-approval.json')
            assert cached['cached'] is True and cached['requests'] == raw['requests']
            assert cached['query_provider_spend_usd'] == 0
        process.assert_awaited_once()
    asyncio.run(run())


def test_failed_model_answer_preserves_private_raw_output_and_pending_metering(tmp_path, monkeypatch):
    path = approval(tmp_path)
    budget = QueryBudget(path)
    store = CompanyUsage(tmp_path / 'state')
    store.import_demo_allowance(path)
    raw = result([answer(parsed=False, text='')])
    workspace = SimpleNamespace(name='peter-17535', hydrate=lambda: None,
        members=SimpleNamespace(list=lambda: [SimpleNamespace(email=OWNER, role='owner')]))
    monkeypatch.setitem(sys.modules, 'modal', SimpleNamespace(
        Workspace=SimpleNamespace(from_context=lambda: workspace), enable_output=contextlib.nullcontext))
    app = SimpleNamespace(app_id='ap-outcomefixture', run=lambda **kwargs: contextlib.nullcontext())
    call = SimpleNamespace(object_id='fc-outcomefixture', get=lambda **kwargs: copy.deepcopy(raw))
    spawn = lambda *args: call
    monkeypatch.setattr(cosmos_inspect, 'create_worker', lambda *args: (app, SimpleNamespace(spawn=spawn)))
    monkeypatch.setattr(budget, 'reconcile_modal', lambda *args: {'status': 'awaiting_provider_evidence'})
    output = store.state / 'result.json'
    received = cosmos_inspect.run_inspection([], [], None, output, query_budget=budget, company_usage=store)
    saved = json.loads(output.read_text())
    assert saved['requests'] == raw['requests'] == received['requests']
    assert saved['budget']['company_reservation_id']
    assert store.summary()['reserved_usd'] == '2.500000'
    assert store.summary()['provider_actual_cost_usd'] == '0.000000'
    assert store.summary()['retail_charged_usd'] == '0.000000'


@pytest.mark.parametrize('frames', [[], [{}]])
def test_empty_preview_is_a_tool_error(tmp_path, frames):
    index = Index(tmp_path)
    index.inspect_clip = lambda clip: {'clip': index.get_clip(clip), 'frames': frames}
    agent = CodexVideoAgent(tmp_path / 'state', index, lambda: [])
    content, _, success = asyncio.run(agent._tool('inspect_clip', {'clip_id': 'clip-0'}, 'cafe'))
    assert success is False
    assert 'preview' in content[0]['text'].lower()


def test_empty_image_file_is_a_tool_error(tmp_path):
    image = tmp_path / 'empty.jpg'
    image.write_bytes(b'')
    index = Index(tmp_path)
    index.inspect_clip = lambda clip: {'clip': index.get_clip(clip),
        'frames': [{'path': str(image), 'time': 1}]}
    agent = CodexVideoAgent(tmp_path / 'state', index, lambda: [])
    content, _, success = asyncio.run(agent._tool('inspect_clip', {'clip_id': 'clip-0'}, 'cafe'))
    assert success is False
    assert 'preview' in content[0]['text'].lower()


def test_preview_describes_only_returned_snapshots(tmp_path):
    index = Index(tmp_path)
    index.inspect_clip = lambda clip: {'clip': index.get_clip(clip),
        'frames': [{'time': stamp, 'image_bytes': b'fixture-jpeg'} for stamp in (.8, 4, 7.2)]}
    agent = CodexVideoAgent(tmp_path / 'state', index, lambda: [])
    content, _, success = asyncio.run(agent._tool('inspect_clip', {'clip_id': 'clip-0'}, 'cafe'))
    assert success is True and len(content) == 4
    metadata = json.loads(content[0]['text'])
    assert metadata['evidence_type'] == 'snapshot_preview'
    assert metadata['sampled_frame_times'] == [.8, 4, 7.2]
    assert 'cosmos_inspect' in metadata['temporal_inspection_tool']
