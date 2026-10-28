"""Protocol fixtures for the documented Agents API; these tests never call a provider."""
import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from managed_codex import ManagedCodexClient, ManagedCodexError, ManagedEvent


def run(coro):
    return asyncio.run(coro)


def test_sessions_use_versioned_native_contract_and_saved_submission_keys():
    async def case():
        requests = []
        async def handle(request):
            requests.append(request)
            if request.method == 'POST' and request.url.path.endswith('/events'):
                return httpx.Response(202)
            return httpx.Response(200, json={'id':'sess_demo', 'environment':{'id':'env_demo', 'type':'openai_hosted'}})
        async with ManagedCodexClient('test-secret', transport=httpx.MockTransport(handle)) as client:
            config={'agent':{'model':'fixture-model','instructions':'Help the person','multi_agent':{'enabled':True}},'environment':{'type':'openai_hosted','container_size':'small','network':{'access':'disabled'}},'spend_control':{'limit':100}}
            result=await client.create_session(config, submission_key='create-once')
            assert result['id']=='sess_demo'
            await client.send_message('sess_demo','Continue',submission_key='message-once')
            await client.steer('sess_demo','Focus on Tuesday',submission_key='steer-once')
            await client.interrupt('sess_demo',submission_key='stop-once')
        assert json.loads(requests[0].content)=={**config,'stream':False}
        assert requests[0].headers['OpenAI-Beta']=='agents=v1'
        assert requests[0].headers['Authorization']=='Bearer test-secret'
        assert requests[1].headers['Idempotency-Key']=='message-once'
        assert json.loads(requests[1].content)=={'events':[{'type':'agent.session.input.message','input':[{'role':'user','content':[{'type':'input_text','text':'Continue'}]}]}]}
        assert json.loads(requests[2].content)['events'][0]['type']=='agent.session.input.message'
        assert json.loads(requests[3].content)=={'events':[{'type':'agent.session.input.cancel'}]}
        assert all(request.url.host=='api.openai.com' for request in requests)
    run(case())


def test_errors_never_expose_keys_provider_bodies_or_request_urls_and_do_not_retry():
    async def case():
        calls=[]
        async def handle(request):
            calls.append(request)
            return httpx.Response(401,json={'error':{'message':'test-secret Authorization: Bearer other-secret','code':'test-secret'}},headers={'x-request-id':'req_safe'})
        async with ManagedCodexClient('test-secret',transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ManagedCodexError) as info:
                await client.send_message('sess_demo','Private user text',submission_key='once')
            error=info.value
            assert error.status_code==401 and error.request_id=='req_safe'
            assert error.__cause__ is None
            assert 'secret' not in str(error) and 'Private' not in repr(error)
            assert 'https' not in str(error)
        assert len(calls)==1
    run(case())


@pytest.mark.parametrize('identity',['../other','a/b','a?key=secret','https://elsewhere',''])
def test_resource_ids_reject_path_and_query_injection(identity):
    async def case():
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(lambda r: pytest.fail('network called'))) as client:
            with pytest.raises(ValueError):
                await client.get_session(identity)
    run(case())


def test_sse_preserves_unknown_types_and_multiline_data_without_resume_headers():
    async def case():
        calls=[]
        wire=': heartbeat\r\n\r\nid: wire-1\r\nevent: future.event\r\ndata: {"type":"future.event",\r\ndata: "event_id":"evt_1","session_id":"sess_a","opaque":{"next":true}}\r\n\r\ndata: [DONE]\r\n\r\n'
        async def handle(request):
            calls.append(request)
            return httpx.Response(200,text=wire,headers={'content-type':'text/event-stream'})
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(handle)) as client:
            async with client.open_events('sess_a') as events:
                found=[event async for event in events]
        assert len(found)==1 and found[0].event_id=='evt_1'
        assert found[0].sse_id=='wire-1' and found[0].raw['opaque']=={'next':True}
        assert found[0].category=='unknown'
        assert dict(calls[0].url.params)=={'stream':'true'}
        assert 'Last-Event-ID' not in calls[0].headers
    run(case())


def test_event_mapping_preserves_ids_text_replacements_and_child_completion():
    delta=ManagedEvent({'type':'agent.session.turn.output_text.delta','event_id':'e1','item_id':'m1','session_id':'s1','turn_id':'t1','output_index':0,'content_index':2,'delta':'Hello'})
    assert delta.category=='text_delta' and delta.item_id=='m1' and delta.turn_id=='t1'
    done=ManagedEvent({'type':'agent.session.turn.output_text.done','event_id':'e2','item_id':'m1','text':'Hello corrected'})
    assert done.category=='text_done' and done.raw['text']=='Hello corrected'
    summary=ManagedEvent({'type':'agent.session.turn.reasoning_summary_text.delta','delta':'Checking the data'})
    assert summary.category=='reasoning_summary_delta'
    child=ManagedEvent({'type':'agent.session.turn.completed','turn':{'id':'t2','subagent_id':'child1'},'usage':{'total_tokens':42}})
    assert child.subagent_id=='child1' and child.root_turn_outcome is None
    root=ManagedEvent({'type':'agent.session.turn.cancelled','turn':{'id':'t1','subagent_id':None}})
    assert root.root_turn_outcome=='cancelled'
    assert ManagedEvent({'type':'agent.session.turn.completed'}).root_turn_outcome is None
    assert ManagedEvent({'type':'agent.session.idle','session':{'id':'s1'}}).root_turn_outcome is None
    assert ManagedEvent({'type':'agent.session.environment.failed'}).category=='environment'
    assert ManagedEvent({'type':'agent.session.subagent.created','subagent':{'id':'c1','session_id':'s1'}}).session_id=='s1'


def test_recovery_opens_stream_before_snapshot_and_paginates_all_saved_work():
    async def case():
        calls=[]
        async def handle(request):
            calls.append(request)
            path=request.url.path
            if path.endswith('/events'):
                return httpx.Response(200,text='data: {"type":"agent.session.turn.output_text.done","event_id":"e1","item_id":"m2","text":"Recovered"}\n\n',headers={'content-type':'text/event-stream'})
            if path.endswith('/items'):
                after=request.url.params.get('after')
                return httpx.Response(200,json={'data':[{'id':'m2' if after else 'm1','status':'completed'}], 'has_more':not bool(after),'last_id':'m2' if after else 'm1'})
            if path.endswith('/turns'):
                return httpx.Response(200,json={'data':[{'id':'t1','subagent_id':None}],'has_more':False,'last_id':'t1'})
            return httpx.Response(200,json={'id':'sess_a','status':'requires_action','required_actions':[{'type':'environment_connection'}]})
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(handle)) as client:
            async with client.recover_session('sess_a') as recovered:
                assert [item['id'] for item in recovered.items]==['m1','m2']
                assert recovered.turns[0]['id']=='t1'
                assert recovered.session['required_actions'][0]['type']=='environment_connection'
                assert [e.event_id async for e in recovered.events]==['e1']
        assert calls[0].url.path.endswith('/events')
        assert any(r.url.params.get('after')=='m1' for r in calls)
        assert all(r.method=='GET' for r in calls)
    run(case())


def test_subagent_history_remains_separate_and_pagination_rejects_loop():
    async def guard():
        requests=[]
        async def handle(request):
            requests.append(request)
            return httpx.Response(200,json={'data':[{'id':'same'}],'has_more':True,'last_id':'same'})
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(handle)) as client:
            page=await client.list_items('sess_a',subagent_id='child_a',after='previous',limit=10)
            assert page['data'][0]['id']=='same'
            assert requests[0].url.path=='/v1/agents/sessions/sess_a/subagents/child_a/items'
            assert requests[0].url.params.get('after')=='previous'
            with pytest.raises(ManagedCodexError,match='pagination'):
                await client.collect_items('sess_a',max_pages=3)
    run(guard())


@pytest.mark.parametrize('wire',[ 'data: broken-json\n\n', 'data: []\n\n', 'data: {"event_id":"e"}\n\n'])
def test_malformed_sse_is_actionable_and_secret_safe(wire):
    async def case():
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(lambda r:httpx.Response(200,text=wire,headers={'content-type':'text/event-stream'}))) as client:
            with pytest.raises(ManagedCodexError,match='event'):
                async with client.open_events('sess_a') as events:
                    async for _ in events: pass
    run(case())


def test_subscription_precedes_submission_and_closing_stream_does_not_cancel():
    async def case():
        calls=[]
        async def handle(request):
            calls.append(request)
            if request.method=='GET':
                return httpx.Response(200,text=': connected\n\n',headers={'content-type':'text/event-stream'})
            return httpx.Response(202)
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(handle)) as client:
            async with client.open_events('sess_a'):
                await client.send_message('sess_a','Do the work',submission_key='saved-key')
        assert [r.method for r in calls]==['GET','POST']
        assert json.loads(calls[1].content)['events'][0]['type']=='agent.session.input.message'
    run(case())


def test_all_read_endpoints_and_page_bounds():
    async def case():
        calls=[]
        async def handle(request):
            calls.append(request)
            return httpx.Response(200,json={'data':[],'has_more':False,'last_id':None})
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(handle)) as client:
            await client.list_sessions(after='sess_previous',limit=3)
            await client.get_turn('sess_a','turn_a')
            await client.list_turns('sess_a')
            await client.list_subagents('sess_a')
            with pytest.raises(ValueError): await client.list_sessions(limit=101)
            with pytest.raises(ValueError): await client.list_items('sess_a',after='../bad')
        assert [r.url.path for r in calls]==['/v1/agents/sessions','/v1/agents/sessions/sess_a/turns/turn_a','/v1/agents/sessions/sess_a/turns','/v1/agents/sessions/sess_a/subagents']
        assert dict(calls[0].url.params)=={'after':'sess_previous','limit':'3','order':'desc'}
    run(case())


def test_recovery_reports_overflow_instead_of_silently_losing_events():
    async def case():
        async def handle(request):
            if request.url.path.endswith('/events'):
                data=''.join('data: '+json.dumps({'type':'future','event_id':str(i)})+'\n\n' for i in range(3))
                return httpx.Response(200,text=data,headers={'content-type':'text/event-stream'})
            await asyncio.sleep(0)
            if request.url.path.endswith(('/items','/turns')):
                return httpx.Response(200,json={'data':[],'has_more':False})
            return httpx.Response(200,json={'id':'sess_a'})
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ManagedCodexError,match='buffer overflow'):
                async with client.recover_session('sess_a',max_buffered_events=1): pass
    run(case())


def test_disconnect_is_secret_safe_and_does_not_resend_task():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"agent.session.turn.output_text.delta","delta":"x"}\n\n'
            raise httpx.ReadError('API KEY private-secret')
    async def case():
        calls=[]
        async def handle(request):
            calls.append(request)
            return httpx.Response(200,stream=BrokenStream(),headers={'content-type':'text/event-stream'})
        async with ManagedCodexClient('fixture',transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ManagedCodexError) as info:
                async with client.open_events('sess_a') as events:
                    async for event in events: assert event.raw['delta']=='x'
            assert info.value.code=='stream_disconnected'
            assert 'secret' not in str(info.value) and info.value.__cause__ is None
        assert len(calls)==1 and calls[0].method=='GET'
    run(case())


def test_event_bound_and_incomplete_frame_fail_explicitly():
    async def check(wire,code,limit):
        async with ManagedCodexClient('fixture',max_event_bytes=limit,transport=httpx.MockTransport(lambda r:httpx.Response(200,text=wire,headers={'content-type':'text/event-stream'}))) as client:
            with pytest.raises(ManagedCodexError) as info:
                async with client.open_events('sess_a') as events:
                    async for _ in events: pass
            assert info.value.code==code
    run(check('data: '+json.dumps({'type':'future','text':'x'*100})+'\n\n','event_too_large',32))
    run(check('data: {"type":"future"}\n','incomplete_event',1024))


def test_error_and_event_representations_do_not_contain_raw_content():
    event=ManagedEvent({'type':'future','event_id':'e1','secret':'customer-secret'},sse_id='sensitive-value')
    assert 'secret' not in repr(event) and 'sensitive' not in repr(event)
    error=ManagedCodexError('http_error',status_code=403,request_id='Authorization: Bearer secret')
    assert error.request_id is None and 'secret' not in str(error)


def test_product_sessions_require_managed_execution_and_a_finite_provider_limit():
    async def case():
        calls=[]
        async def handle(request):
            calls.append(request)
            return httpx.Response(200,json={'id':'sess_a'})
        async with ManagedCodexClient('test-secret',transport=httpx.MockTransport(handle)) as client:
            valid={'agent':{'model':'fixture-model','instructions':'Help.'},
                   'environment':{'type':'none'},'spend_control':{'limit':100}}
            invalid=[{**valid,'environment':{'type':'self_hosted'}},
                     {key:value for key,value in valid.items() if key!='spend_control'},
                     *[{**valid,'spend_control':value} for value in [None,{}, {'limit':None},
                         {'limit':True},{'limit':0},{'limit':-1},{'limit':1.5},{'limit':2**53}]]]
            for config in invalid:
                with pytest.raises(ValueError):
                    await client.create_session(config,submission_key='invalid')
            assert calls==[]
            await client.create_session(valid,submission_key='managed-functions')
            await client.create_session({**valid,'environment':{'type':'openai_hosted',
                'container_size':'small','network':{'access':'disabled'}}},submission_key='managed-sandbox')
            assert len(calls)==2
    run(case())


def test_session_updates_preserve_limits_and_cleanup_uses_owned_resource_path():
    async def case():
        calls=[]
        async def handle(request):
            calls.append(request)
            return httpx.Response(200,json={'id':'sess_a','deleted':request.method=='DELETE'})
        async with ManagedCodexClient('test-secret',transport=httpx.MockTransport(handle)) as client:
            for control in [None,{}, {'limit':None},{'limit':0},{'limit':True}]:
                with pytest.raises(ValueError):
                    await client.update_session('sess_a',{'spend_control':control},submission_key='invalid')
            assert calls==[]
            await client.update_session('sess_a',{'agent':{'model':'fixture-model'}},submission_key='model')
            await client.update_session('sess_a',{'spend_control':{'limit':25}},submission_key='limit')
            deleted=await client.delete_session('sess_a',submission_key='cleanup')
            assert deleted['deleted'] is True
            assert [r.method for r in calls]==['POST','POST','DELETE']
            assert all(r.url.path=='/v1/agents/sessions/sess_a' for r in calls)
            assert calls[-1].headers['Idempotency-Key']=='cleanup'
    run(case())


def test_child_turn_history_uses_the_bound_session_and_exclusive_resource_cursor():
    async def case():
        calls=[]
        async def handle(request):
            calls.append(request)
            return httpx.Response(200,json={'data':[], 'has_more':False})
        async with ManagedCodexClient('test-secret',transport=httpx.MockTransport(handle)) as client:
            await client.list_subagent_turns('sess_a','subagent_b',after='turn_c',limit=7,order='asc')
            with pytest.raises(ValueError):
                await client.list_subagent_turns('sess_a','../foreign')
            assert len(calls)==1
            assert calls[0].url.path=='/v1/agents/sessions/sess_a/subagents/subagent_b/turns'
            assert dict(calls[0].url.params)=={'after':'turn_c','limit':'7','order':'asc'}
    run(case())


def test_monitored_transport_omits_control_on_create_and_update_explicitly():
    async def case():
        bodies = []
        async def handle(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={'id': 'session', 'usage': None})
        config = {'agent': {'model': 'gpt-6-astra'}, 'environment': {'type': 'none'},
                  'spend_control': {'limit': 100}}
        async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handle),
                                      spending_mode='monitored') as client:
            await client.create_session(config, submission_key='create')
            await client.update_session('session', {'spend_control': {'limit': 100}}, submission_key='update')
        assert all('spend_control' not in body for body in bodies)
        assert config['spend_control'] == {'limit': 100}
    run(case())


def test_optional_query_cursor_is_omitted_and_present_values_are_preserved():
    async def case():
        queries = []
        async def handle(request):
            queries.append(dict(request.url.params))
            return httpx.Response(200, json={'data': [], 'has_more': False})
        async with ManagedCodexClient('fixture', transport=httpx.MockTransport(handle)) as client:
            await client._request('GET', 'agents/sessions/session/artifacts',
                                  params={'after': None, 'limit': 100, 'order': 'asc'})
            await client._request('GET', 'agents/sessions/session/artifacts',
                                  params={'after': 'artifact-next', 'limit': 100, 'order': 'asc',
                                          'zero': 0, 'disabled': False, 'empty': ''})
        assert queries[0] == {'limit': '100', 'order': 'asc'}
        assert queries[1] == {'after': 'artifact-next', 'limit': '100', 'order': 'asc',
                              'zero': '0', 'disabled': 'false', 'empty': ''}
    run(case())
