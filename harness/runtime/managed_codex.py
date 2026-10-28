"""Agents API v1 transport, verified against official documentation on 2026-10-11.

This is a server-side protocol adapter, deliberately independent of agent_bridge.
It neither provisions executors nor stores credentials nor authorizes tenant access.
The caller binds every session to its owner and persists submission keys *before*
sending. Requests never retry implicitly. HTTP 202 means accepted, not completed.

Official contracts:
https://developers.openai.com/api/docs/guides/agents-api/sessions
https://developers.openai.com/api/docs/guides/agents-api/sessions/events
https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted
https://developers.openai.com/api/reference/resources/beta/subresources/agents/streaming-events

Live streams have no replay cursor. After disconnect, open and buffer a stream,
read the session and paginate saved items, then reconcile by item ID. Ignore buffered
updates to already-terminal saved items; output_text.done replaces partial text.
History cursors are exclusive resource IDs, not SSE event IDs. Recovery returns raw
snapshots and buffered events because rendering and durable reconciliation belong to
the caller. Subagent history is separate from root items. Unknown events survive.

Raw responses/events can contain customer content and credential-bearing tool config.
Keep them server-side; the UI requires an explicit public projection. Error strings
and reprs intentionally exclude requests, provider bodies and event payloads.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import json
import re
from typing import Any

import httpx

API_ROOT = 'https://api.openai.com/v1/'
API_VERSION = 'agents=v1'
PROTOCOL_VERIFIED_ON = '2026-10-11'
_RESOURCE_ID = re.compile(r'[A-Za-z0-9_-]{1,512}\Z')
_REQUEST_ID = re.compile(r'req_[A-Za-z0-9_-]{1,160}\Z')
_TERMINAL_TURNS = {'completed', 'failed', 'cancelled'}
_TEXT_EVENTS = {
    'agent.session.turn.output_text.delta': 'text_delta',
    'agent.session.turn.output_text.done': 'text_done',
    'agent.output.command_execution_output.delta': 'command_output',
    'agent.session.turn.item.added': 'item_added',
    'agent.session.turn.item.done': 'item_done',
    'agent.session.turn.content_part.added': 'content_part_added',
    'agent.session.turn.content_part.done': 'content_part_done',
    'agent.session.turn.reasoning_summary_part.added': 'reasoning_summary_part_added',
    'agent.session.turn.reasoning_summary_part.done': 'reasoning_summary_part_done',
    'agent.session.turn.reasoning_summary_text.delta': 'reasoning_summary_delta',
    'agent.session.turn.reasoning_summary_text.done': 'reasoning_summary_done',
    'agent.session.requires_action': 'required_action',
    'error': 'error',
}


class ManagedCodexError(RuntimeError):
    """Stable local failure information; never embeds upstream text or credentials."""

    def __init__(self, code: str, *, status_code: int | None = None,
                 request_id: str | None = None):
        self.code, self.status_code = code, status_code
        self.request_id = request_id if request_id and _REQUEST_ID.fullmatch(request_id) else None
        message = 'Managed Codex ' + code.replace('_', ' ') + '.'
        if status_code is not None:
            message += f' HTTP {status_code}.'
        if self.request_id:
            message += f' Request {self.request_id}.'
        super().__init__(message)


def _identity(value: str) -> str:
    if not isinstance(value, str) or not _RESOURCE_ID.fullmatch(value):
        raise ValueError('A valid resource ID is required.')
    return value


def _submission(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value):
        raise ValueError('A persisted submission key is required.')
    return value


def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ManagedEvent:
    """Lossless native event plus a conservative, versioned host classification."""

    raw: dict[str, Any] = field(repr=False)
    sse_id: str | None = field(default=None, repr=False)
    sse_event: str | None = field(default=None, repr=False)

    @property
    def type(self) -> str:
        return self.raw.get('type', '')

    @property
    def event_id(self) -> str | None:
        return self.raw.get('event_id')

    @property
    def session_id(self) -> str | None:
        return (self.raw.get('session_id') or _obj(self.raw.get('session')).get('id')
                or _obj(self.raw.get('subagent')).get('session_id'))

    @property
    def turn_id(self) -> str | None:
        return (self.raw.get('turn_id') or _obj(self.raw.get('turn')).get('id')
                or _obj(self.raw.get('item')).get('turn_id'))

    @property
    def item_id(self) -> str | None:
        return self.raw.get('item_id') or _obj(self.raw.get('item')).get('id')

    @property
    def subagent_id(self) -> str | None:
        # Text/item events may require the caller's turn_id -> subagent_id map.
        # Missing attribution is not evidence that the event belongs to the root.
        return (_obj(self.raw.get('subagent')).get('id')
                or _obj(self.raw.get('turn')).get('subagent_id'))

    @property
    def root_turn_outcome(self) -> str | None:
        turn = _obj(self.raw.get('turn'))
        outcome = self.type.removeprefix('agent.session.turn.')
        if (outcome in _TERMINAL_TURNS and 'subagent_id' in turn
                and turn['subagent_id'] is None):
            return outcome
        return None

    @property
    def category(self) -> str:
        if self.type in _TEXT_EVENTS:
            return _TEXT_EVENTS[self.type]
        for prefix, category in (
            ('agent.session.subagent.', 'subagent'),
            ('agent.session.environment.', 'environment'),
        ):
            if self.type.startswith(prefix):
                return category
        if self.type in {'agent.session.turn.' + state for state in
                         ('created', 'queued', 'in_progress', 'waiting', 'completed', 'failed', 'cancelled')}:
            return 'turn'
        if self.type in {'agent.session.' + state for state in
                         ('created', 'idle', 'in_progress', 'failed')}:
            return 'session'
        return 'unknown'


@dataclass
class RecoveredSession:
    """Apply snapshots before consuming events; raw data stays inside the host."""

    session: dict = field(repr=False)
    items: list[dict] = field(repr=False)
    turns: list[dict] = field(repr=False)
    events: AsyncIterator[ManagedEvent] = field(repr=False)


class ManagedCodexClient:
    """Explicitly-owned async httpx client for the documented managed protocol.

    Only api.openai.com receives the project key. Tests inject a MockTransport.
    Event streaming is opened as a context manager so subscription precedes input.
    Closing a stream/client does not cancel upstream work: use interrupt explicitly.
    """

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 30.0, max_event_bytes: int = 8 * 1024 * 1024, spending_mode=None):
        if not isinstance(api_key, str) or not api_key or any(c.isspace() for c in api_key):
            raise ValueError('An API credential is required.')
        if timeout <= 0 or max_event_bytes < 1:
            raise ValueError('Positive transport bounds are required.')
        from managed_spend import spending_mode as resolve_spending_mode
        self.spending_mode = resolve_spending_mode(spending_mode)
        self.max_event_bytes = max_event_bytes
        self._client = httpx.AsyncClient(
            base_url=API_ROOT, transport=transport, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(timeout), headers={
                'Authorization': 'Bearer ' + api_key,
                'OpenAI-Beta': API_VERSION,
                'Accept': 'application/json',
            })

    async def __aenter__(self) -> ManagedCodexClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            raise ManagedCodexError('http_error', status_code=response.status_code,
                                    request_id=response.headers.get('x-request-id'))

    async def _request(self, method: str, path: str, *, body: dict | None = None,
                       params: dict | None = None, submission_key: str | None = None) -> dict:
        headers = {'Idempotency-Key': _submission(submission_key)} if submission_key is not None else {}
        if params is not None:
            params = {name: value for name, value in params.items() if value is not None}
        try:
            response = await self._client.request(method, path, json=body, params=params, headers=headers)
        except httpx.HTTPError:
            raise ManagedCodexError('transport_error') from None
        self._check(response)
        if not response.content:
            return {}
        try:
            value = response.json()
        except (ValueError, UnicodeError):
            raise ManagedCodexError('invalid_response') from None
        if not isinstance(value, dict):
            raise ManagedCodexError('invalid_response')
        return value

    @staticmethod
    def _session_path(session_id: str) -> str:
        return 'agents/sessions/' + _identity(session_id)

    @staticmethod
    def _page_params(after: str | None, limit: int, order: str) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100 or order not in {'asc', 'desc'}:
            raise ValueError('Use a page size from 1 to 100 and asc or desc order.')
        return {'limit': limit, 'order': order, **({'after': _identity(after)} if after is not None else {})}

    async def list_artifacts(self, session_id: str, *, after: str | None = None,
                             limit: int = 100, order: str = 'asc') -> dict:
        return await self._request('GET', self._session_path(session_id) + '/artifacts',
                                   params=self._page_params(after, limit, order))

    async def get_artifact(self, session_id: str, artifact_id: str) -> dict:
        return await self._request('GET', self._session_path(session_id) + '/artifacts/' + _identity(artifact_id))

    async def artifact_content(self, session_id: str, artifact_id: str, *, max_bytes: int) -> AsyncIterator[bytes]:
        """Read bounded immutable bytes from the fixed provider origin, without redirects."""
        if type(max_bytes) is not int or not 0 <= max_bytes <= 200 * 1024 * 1024:
            raise ValueError('Use a bounded artifact byte allowance.')
        path = self._session_path(session_id) + '/artifacts/' + _identity(artifact_id) + '/content'
        try:
            async with self._client.stream('GET', path, headers={
                    'Accept': 'application/octet-stream', 'Accept-Encoding': 'identity'}) as response:
                self._check(response)
                if response.headers.get('content-encoding', 'identity') != 'identity':
                    raise ManagedCodexError('artifact_encoding')
                length = response.headers.get('content-length')
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise ManagedCodexError('artifact_size')
                total = 0
                async for chunk in response.aiter_bytes(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ManagedCodexError('artifact_size')
                    yield chunk
                if length is not None and total != int(length):
                    raise ManagedCodexError('artifact_size')
        except httpx.HTTPError:
            raise ManagedCodexError('transport_error') from None

    async def create_session(self, config: Mapping[str, Any], *, submission_key: str) -> dict:
        """Create a bounded session using OpenAI-managed execution.

        ``none`` uses managed function tools; ``openai_hosted`` adds OpenAI's
        managed sandbox. Rocoto's product never starts a self-hosted agent core.
        The caller reserves funds before creating the cumulative session limit.
        """
        body = dict(config)
        if _obj(body.get('environment')).get('type') not in {'none', 'openai_hosted'}:
            raise ValueError('Select OpenAI-managed function tools or an OpenAI-hosted sandbox.')
        if self.spending_mode == 'monitored':
            body.pop('spend_control', None)
        else:
            self._spend_control(body.get('spend_control'))
        if body.get('stream'):
            raise ValueError('Use open_events to subscribe to an existing session.')
        body['stream'] = False
        return await self._request('POST', 'agents/sessions', body=body, submission_key=submission_key)

    @staticmethod
    def _spend_control(value):
        limit = value.get('limit') if isinstance(value, dict) else None
        if type(limit) is not int or not 1 <= limit <= 4503599627370495:
            raise ValueError('Bind a positive session spending limit in whole USD cents.')

    async def update_session(self, session_id: str, config: Mapping[str, Any], *, submission_key: str) -> dict:
        """Update subsequent turns while preserving a finite session allowance."""
        body = dict(config)
        if set(body) - {'agent', 'metadata', 'spend_control'}:
            raise ValueError('Update documented agent settings, metadata or session allowance.')
        if self.spending_mode == 'monitored':
            body.pop('spend_control', None)
        elif 'spend_control' in body:
            self._spend_control(body['spend_control'])
        return await self._request('POST', self._session_path(session_id), body=body,
                                   submission_key=submission_key)

    async def delete_session(self, session_id: str, *, submission_key: str) -> dict:
        """Delete a caller-owned session and request its hosted sandbox cleanup."""
        return await self._request('DELETE', self._session_path(session_id), submission_key=submission_key)

    async def get_session(self, session_id: str) -> dict:
        return await self._request('GET', self._session_path(session_id))

    async def list_sessions(self, *, after: str | None = None, limit: int = 20,
                            order: str = 'desc') -> dict:
        return await self._request('GET', 'agents/sessions', params=self._page_params(after, limit, order))

    async def get_turn(self, session_id: str, turn_id: str) -> dict:
        return await self._request('GET', self._session_path(session_id) + '/turns/' + _identity(turn_id))

    async def list_turns(self, session_id: str, *, after: str | None = None,
                         limit: int = 100, order: str = 'asc') -> dict:
        return await self._request('GET', self._session_path(session_id) + '/turns',
                                   params=self._page_params(after, limit, order))

    async def list_subagent_turns(self, session_id: str, subagent_id: str, *,
                                  after: str | None = None, limit: int = 20,
                                  order: str = 'desc') -> dict:
        """Inspect child turns separately; an active child may be idle."""
        return await self._request('GET', self._session_path(session_id) +
                                   '/subagents/' + _identity(subagent_id) + '/turns',
                                   params=self._page_params(after, limit, order))

    async def list_items(self, session_id: str, *, subagent_id: str | None = None,
                         after: str | None = None, limit: int = 100, order: str = 'asc') -> dict:
        path = self._session_path(session_id)
        if subagent_id is not None:
            path += '/subagents/' + _identity(subagent_id)
        return await self._request('GET', path + '/items', params=self._page_params(after, limit, order))

    async def list_subagents(self, session_id: str, *, after: str | None = None,
                             limit: int = 100, order: str = 'asc') -> dict:
        return await self._request('GET', self._session_path(session_id) + '/subagents',
                                   params=self._page_params(after, limit, order))

    async def submit_events(self, session_id: str, events: list[dict], *, submission_key: str) -> dict:
        """Send native message/cancel/tool-result/approval schemas unchanged."""
        if not events or not all(isinstance(event, dict) for event in events):
            raise ValueError('Input events are required.')
        return await self._request('POST', self._session_path(session_id) + '/events',
                                   body={'events': events}, submission_key=submission_key)

    async def send_message(self, session_id: str, text: str, *, submission_key: str,
                           images: tuple[str, ...] = ()) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise ValueError('A message is required.')
        content = [{'type': 'input_text', 'text': text}]
        content.extend({'type': 'input_image', 'image_url': url} for url in images)
        return await self.submit_events(session_id, [{
            'type': 'agent.session.input.message',
            'input': [{'role': 'user', 'content': content}],
        }], submission_key=submission_key)

    async def steer(self, session_id: str, text: str, *, submission_key: str) -> dict:
        """Native message input steers an active turn, or starts an idle turn."""
        return await self.send_message(session_id, text, submission_key=submission_key)

    async def interrupt(self, session_id: str, *, submission_key: str) -> dict:
        """Request cancellation; await native terminal events to confirm its result."""
        return await self.submit_events(session_id, [{'type': 'agent.session.input.cancel'}],
                                        submission_key=submission_key)

    @asynccontextmanager
    async def open_events(self, session_id: str) -> AsyncIterator[AsyncIterator[ManagedEvent]]:
        """Subscribe before sending. EOF is a disconnect, not a successful turn."""
        try:
            async with self._client.stream(
                'GET', self._session_path(session_id) + '/events', params={'stream': 'true'},
                headers={'Accept': 'text/event-stream'},
                timeout=httpx.Timeout(self._client.timeout.connect, read=None),
            ) as response:
                self._check(response)
                if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/event-stream':
                    raise ManagedCodexError('invalid_event_stream')
                yield self._decode_events(response)
        except httpx.HTTPError:
            raise ManagedCodexError('stream_disconnected') from None

    async def _decode_events(self, response: httpx.Response) -> AsyncIterator[ManagedEvent]:
        data: list[str] = []
        event_name = event_id = None
        size = 0
        try:
            async for line in response.aiter_lines():
                size += len(line.encode('utf-8')) + 1
                if size > self.max_event_bytes:
                    raise ManagedCodexError('event_too_large')
                if line == '':
                    if data:
                        payload = '\n'.join(data)
                        if payload.strip() == '[DONE]':
                            return
                        try:
                            raw = json.loads(payload)
                        except (ValueError, UnicodeError):
                            raise ManagedCodexError('invalid_event') from None
                        if not isinstance(raw, dict) or not isinstance(raw.get('type'), str):
                            raise ManagedCodexError('invalid_event')
                        yield ManagedEvent(raw, sse_id=event_id, sse_event=event_name)
                    data, event_name, event_id, size = [], None, None, 0
                elif not line.startswith(':'):
                    key, _, value = line.partition(':')
                    if value.startswith(' '):
                        value = value[1:]
                    if key == 'data':
                        data.append(value)
                    elif key == 'event':
                        event_name = value
                    elif key == 'id' and '\x00' not in value:
                        event_id = value
            if data:
                # An unfinished frame cannot be applied safely after disconnect.
                raise ManagedCodexError('incomplete_event')
        except httpx.HTTPError:
            raise ManagedCodexError('stream_disconnected') from None

    async def _collect(self, fetch, *, max_pages: int) -> list[dict]:
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError('A positive page limit is required.')
        result, seen, after = [], set(), None
        for _ in range(max_pages):
            page = await fetch(after=after, order='asc', limit=100)
            if not isinstance(page.get('data'), list) or not all(isinstance(v, dict) for v in page['data']):
                raise ManagedCodexError('invalid_history_page')
            result.extend(page['data'])
            if page.get('has_more') is False:
                return result
            cursor = page.get('last_id')
            if not page['data'] or not isinstance(cursor, str) or cursor in seen:
                raise ManagedCodexError('invalid_pagination_cursor')
            seen.add(cursor)
            after = cursor
        raise ManagedCodexError('pagination_limit_reached')

    async def collect_items(self, session_id: str, *, subagent_id: str | None = None,
                            max_pages: int = 1000) -> list[dict]:
        async def fetch(**params):
            return await self.list_items(session_id, subagent_id=subagent_id, **params)
        return await self._collect(fetch, max_pages=max_pages)

    @asynccontextmanager
    async def recover_session(self, session_id: str, *, max_pages: int = 1000,
                              max_buffered_events: int = 2048) -> AsyncIterator[RecoveredSession]:
        """Open+buffer SSE, then retrieve canonical state, without resubmitting work.

        Always rebuild root history: a previously incomplete item can change before
        a stored pagination cursor. The consumer rebuilds each child using collect_items
        with its subagent ID. Buffer overflow fails explicitly for another recovery.
        """
        if type(max_buffered_events) is not int or max_buffered_events < 1:
            raise ValueError('A positive event buffer limit is required.')
        queue: asyncio.Queue[ManagedEvent] = asyncio.Queue(max_buffered_events)
        failure: list[ManagedCodexError] = []
        finished = asyncio.Event()
        async with self.open_events(session_id) as stream:
            async def pump():
                try:
                    async for event in stream:
                        try:
                            queue.put_nowait(event)
                        except asyncio.QueueFull:
                            raise ManagedCodexError('recovery_buffer_overflow') from None
                except ManagedCodexError as error:
                    failure.append(error)
                finally:
                    finished.set()

            async def buffered_events():
                while True:
                    if failure:
                        raise failure[0]
                    if not queue.empty():
                        yield queue.get_nowait()
                    elif finished.is_set():
                        return
                    else:
                        pending = asyncio.create_task(queue.get())
                        ended = asyncio.create_task(finished.wait())
                        try:
                            await asyncio.wait({pending, ended}, return_when=asyncio.FIRST_COMPLETED)
                            if pending.done():
                                yield pending.result()
                        finally:
                            pending.cancel()
                            ended.cancel()
                            await asyncio.gather(pending, ended, return_exceptions=True)

            task = asyncio.create_task(pump())
            try:
                session = await self.get_session(session_id)
                items = await self.collect_items(session_id, max_pages=max_pages)
                async def fetch_turns(**params):
                    return await self.list_turns(session_id, **params)
                turns = await self._collect(fetch_turns, max_pages=max_pages)
                if failure:
                    raise failure[0]
                yield RecoveredSession(session, items, turns, buffered_events())
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
