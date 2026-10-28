"""Gallery integration keeps streaming captions tied to the live person and source scope."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from evidence_captions import CaptionUnavailable
from presentation_captions import enrich


def fixture():
    run = {'cancel': asyncio.Event(), 'generation': 'turn-a'}
    record = {'owner_principal_id': 'organization', 'dataset': 'cafe',
              'messages': [{'role': 'user', 'content': 'How are drinks handed over?'}]}
    member = [True]
    view_current = [True]
    agent = SimpleNamespace(children={'chat-a': record}, active={'chat-a': run},
        state=Path('/tmp/unused-caption-integration'),
        auth=SimpleNamespace(environment={'ROCOTO_OPENAI_API_KEY': 'fixture-private-key'}),
        _authority_principal=lambda chat: 'alice',
        messaging_member=lambda principal: {'role': 'operator'} if principal == 'alice' and member[0] else None)
    index = SimpleNamespace(business_scope_current=lambda: view_current[0])
    presentation = {'layout': 'gallery', 'items': [{'id': 'clip-a', 'caption': 'Original relevant caption'}]}
    return agent, run, index, presentation, member, view_current


def test_shared_chat_uses_live_actor_and_streams_final_provenance():
    agent, run, index, presentation, _, _ = fixture()
    events = []
    class Service:
        def __init__(self, state, *, api_key, authorized):
            assert api_key == 'fixture-private-key'
            self.authorized = authorized
        async def caption(self, scope, **kwargs):
            assert self.authorized(scope), 'Shared owner is data identity, not the current human'
            assert scope.owner == 'organization'
            assert kwargs['question'] == 'How are drinks handed over?'
            await kwargs['emit']({'type': 'evidence_caption_delta', 'caption': 'A cup appears'})
            events.append(run['tool_events'].get_nowait())
            return {'caption': 'A cup appears to change hands.', 'caption_kind': 'contextual_cosmos_summary',
                    'observation_ref': 'saved-observation'}
    with patch('presentation_captions.EvidenceCaptions', Service):
        result = asyncio.run(enrich(agent, 'chat-a', run, 'cafe', index, presentation))
    assert events[0]['presentation']['items'][0]['caption_status'] == 'streaming'
    assert result['items'][0]['caption_provenance']['observation_ref'] == 'saved-observation'
    assert run['tool_events'].get_nowait()['presentation']['items'][0]['caption_status'] == 'complete'
    assert events[0]['presentation']['items'][0]['caption'] == 'A cup appears'


def test_unavailable_caption_preserves_relevant_original():
    agent, run, index, presentation, _, _ = fixture()
    class Unavailable:
        def __init__(self, *args, **kwargs):
            raise CaptionUnavailable('Funding is being prepared.')
    with patch('presentation_captions.EvidenceCaptions', Unavailable):
        result = asyncio.run(enrich(agent, 'chat-a', run, 'cafe', index, presentation))
    assert result['items'][0]['caption'] == 'Original relevant caption'


def test_revoked_scope_suppresses_late_caption_events():
    agent, run, index, presentation, _, current = fixture()
    class Service:
        async def caption(self, scope, **kwargs):
            current[0] = False
            await kwargs['emit']({'type': 'evidence_caption_delta', 'caption': 'Late private result'})
            raise CaptionUnavailable('Access changed.')
    agent.evidence_caption_factory = Service
    async def exercise():
        try:
            await enrich(agent, 'chat-a', run, 'cafe', index, presentation)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('Scope revocation must end the caption operation')
        assert run['tool_events'].empty()
    asyncio.run(exercise())
