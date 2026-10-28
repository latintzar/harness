"""The page asks the harness what it serves: GET /api/harness.

The web app draws its sidebar from the answer: the fixed chats of the active template, one divider
for each agent class with its labels and starter prompt, and the pages the template ships
(`Pack.shell()`, schema rocoto.harness-shell.v1). Each fixed chat also carries the id of its
conversation in this company once the conversation exists and the signed-in member may read it.

The route sits behind the session check every /api/ route passes (the middleware in server.py),
and the handler asks for the session itself as well: a request that carries a session gets the
shell, and every other request gets the access-code answer. The answer holds names, labels and ids
of the reader's own conversations; file locations and prompt text stay on the host.

server.py registers it through `register_harness_routes(app)`.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import pack

PRIVATE = {'Cache-Control': 'private, no-store'}


def shell(selected=None, session=None, collection='company', guards=None):
    """The active template's shell with the reader's fixed conversation ids for one collection.

    `selected` is the company's workspace and `session` the verified session. A fixed chat gets
    `chat_id` once its conversation exists and this session reads it; until then the page opens it
    through POST /api/organization/chats, which creates it. Each id handed out adds its read check
    to `guards`, so access that changes during the request closes the answer.
    """
    answer = pack.active().shell()
    chats = (selected or {}).get('organization_chats')
    agent = (selected or {}).get('agent')
    permissions = (selected or {}).get('chats')
    for chat in answer['fixed_chats']:
        chat['chat_id'] = None
        if chats is None or agent is None or permissions is None:
            continue
        record = agent.children.get(chats.identity(collection, chat['purpose']))
        if record is not None and permissions.can_read(record, session):
            chat['chat_id'] = record['id']
            if guards is not None:
                guards.append(lambda row=record: permissions.can_read(row, session))
    return answer


def register_harness_routes(app):
    @app.get('/api/harness')
    async def harness(request: Request, collection: str = 'company'):
        session = getattr(request.state, 'session', None)
        if not session:
            raise HTTPException(401, 'Enter your company access code.', headers=PRIVATE)
        try:
            answer = shell(getattr(request.state, 'workspace', None), session, collection,
                           getattr(request.state, 'scope_guards', None))
        except ValueError as error:
            raise HTTPException(400, str(error), headers=PRIVATE) from None
        return JSONResponse(answer, headers=PRIVATE)
