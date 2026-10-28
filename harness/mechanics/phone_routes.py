"""The phone block of one Tracking Agent over HTTP (CHANNELS.md, "HTTP interface").

Session, CSRF, observation and company checks are the host middleware's, as
for the messaging routes. A current participant reads the block, and the
company owner reads it too. The creator manages participants and the agent's
delivery; each participant owns their own switch and channel. Every POST
answers with the fresh block.
"""
import json
import sqlite3

from fastapi import HTTPException, Request

from chat_permissions import workspace_writable
from phone_chat import FIELDS, NOT_FOUND, PhoneChatError

MAX_BODY_BYTES = 8192
PREPARING = 'Phone alerts are being prepared. Refresh in a moment.'


def register_phone_routes(app, *, member, members):
    """`member(selected, principal)` answers a current owner or operator; `members(selected)` lists them."""

    def reader(request, agent_id):
        """The caller as a current company member, and this Tracking Agent's chat within their scope."""
        selected, session = request.state.workspace, request.state.session
        principal = selected['chats'].principal(session)
        current = member(selected, principal) if principal else None
        if not current or not member(selected, session['principal']['id']):
            raise HTTPException(404, NOT_FOUND)
        try:
            saved = selected['phone']._saved(agent_id)
            record = selected['agent'].children.get(saved['chat_id']) or selected['scheduler'].ensure_conversation(saved)
        except (PhoneChatError, ValueError, KeyError):
            raise HTTPException(404, NOT_FOUND) from None
        # The creator reads their own conversation. Another participant, or the company
        # owner, reads it while their own location assignments cover its scope.
        import scope_host
        def covered():
            if record.get('owner_principal_id') == principal:
                return selected['chats'].can_read(record, session)
            return scope_host.history_allowed(selected, record, principal) is True
        if not covered():
            raise HTTPException(404, NOT_FOUND)
        request.state.scope_guards.append(covered)
        return selected, principal, current['role'] == 'owner'

    def block(request, selected, agent_id, principal, owner, admitted=False):
        try:
            return selected['phone_delivery'].block(agent_id, principal, company_owner=owner, admitted=admitted,
                writable=workspace_writable(request.state.session), members=members(selected))
        except PhoneChatError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, PREPARING) from None

    async def body(request, shape, required):
        """A JSON object of the named fields, each of its declared type, in at most 8192 bytes."""
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise HTTPException(413, FIELDS)
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) - set(shape) or required - set(value)
                    or any(type(item) is not shape[key] for key, item in value.items())
                    or any(isinstance(item, str) and (not 1 <= len(item) <= 128 or any(ord(c) < 32 for c in item))
                        for item in value.values())):
                raise ValueError
            return value
        except (ValueError, TypeError):
            raise HTTPException(400, FIELDS) from None

    async def change(request, agent_id, shape, required, operation):
        """Authorize the caller, apply one change as that caller and answer with the fresh block."""
        selected, principal, owner = reader(request, agent_id)
        # Only a current participant or the company owner reaches the change at all.
        block(request, selected, agent_id, principal, owner)
        fields = await body(request, shape, required)
        try:
            operation(selected['phone'], principal, fields)
        except PhoneChatError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, PREPARING) from None
        # The caller was admitted above: a participant who just removed themself reads the result once.
        return block(request, selected, agent_id, principal, owner, admitted=True)

    @app.get('/api/agents/{agent_id}/phone')
    async def phone_block(agent_id: str, request: Request):
        selected, principal, owner = reader(request, agent_id)
        return block(request, selected, agent_id, principal, owner)

    @app.post('/api/agents/{agent_id}/phone/participants')
    async def phone_add(agent_id: str, request: Request):
        return await change(request, agent_id, {'member_id': str, 'expected_generation': int},
            {'member_id', 'expected_generation'}, lambda phone, by, fields: phone.add_participant(
                agent_id, fields['member_id'], by=by, expected_generation=fields['expected_generation']))

    @app.post('/api/agents/{agent_id}/phone/participants/{member_id}/remove')
    async def phone_remove(agent_id: str, member_id: str, request: Request):
        return await change(request, agent_id, {'expected_generation': int}, {'expected_generation'},
            lambda phone, by, fields: phone.remove_participant(
                agent_id, member_id, by=by, expected_generation=fields['expected_generation']))

    @app.post('/api/agents/{agent_id}/phone/participants/{member_id}')
    async def phone_participant(agent_id: str, member_id: str, request: Request):
        return await change(request, agent_id, {'enabled': bool, 'channel': str, 'expected_generation': int},
            {'expected_generation'}, lambda phone, by, fields: phone.update_binding(agent_id, member_id, by=by,
                enabled=fields.get('enabled'), channel=fields.get('channel'),
                expected_generation=fields['expected_generation']))

    @app.post('/api/agents/{agent_id}/phone/delivery')
    async def phone_delivery_state(agent_id: str, request: Request):
        return await change(request, agent_id, {'state': str, 'expected_generation': int},
            {'state', 'expected_generation'}, lambda phone, by, fields: phone.set_delivery(
                agent_id, fields['state'], by=by, expected_generation=fields['expected_generation']))

    @app.post('/api/agents/{agent_id}/phone/contact-request')
    async def phone_contact_request(agent_id: str, request: Request):
        return await change(request, agent_id, {'member_id': str}, {'member_id'},
            lambda phone, by, fields: phone.request_contact(agent_id, fields['member_id'], by=by))
