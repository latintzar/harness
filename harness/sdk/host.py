"""Composition and admission contracts for one selected vertical on the harness."""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Callable


class Admission:
    """A host-issued snapshot whose current authority is checked at every use."""
    def __init__(self, current, bind=nullcontext, model_context=None, *, resource=None, history_scope=None):
        self._current = current
        self._bind = bind
        self.model_context = model_context
        self._resource = resource
        self.history_scope = history_scope

    def current(self):
        try:
            return self._current() is True
        except Exception:
            return False

    @contextmanager
    def bind(self):
        if not self.current():
            raise ValueError('Refresh your current workspace access.')
        with self._bind():
            yield self


class Collections:
    """Current collection names supplied by the owning workspace's providers."""
    def __init__(self):
        self._providers = []
        self._sealed = False

    def register(self, provider):
        if self._sealed:
            raise RuntimeError('Workspace collection providers are already sealed.')
        self._providers.append(provider)

    def seal(self):
        self._sealed = True

    def list(self, principal=None):
        result = {'company'}
        for provider in self._providers:
            result.update(provider(principal))
        return sorted(result)

    def contains(self, collection, principal=None):
        return collection in self.list(principal)


@dataclass
class HostContext:
    state: object
    public_origin: str
    access: object
    workspaces: dict
    workspace: Callable
    member: Callable
    members: Callable
    app: object = None
    options: dict = field(default_factory=dict)


class BareWorkspace:
    """Explicit common behavior for a workspace with no vertical extension."""
    def __init__(self, selected):
        self.selected = selected

    def read_admission(self, actor, collection):
        selected=self.selected
        member=selected['business_scope'].member(actor)
        if not member or not selected['collections'].contains(collection,actor):
            raise ValueError('Open a collection in your current workspace.')
        role=member.get('role')
        def current():
            live=selected['business_scope'].member(actor)
            return bool(live and live.get('role')==role and selected['collections'].contains(collection,actor))
        return Admission(current)

    def tool_context(self, context):
        return context

    def result_fields(self, name, content, success):
        return {}

    def status_fields(self):
        return {}

    def budget_fields(self):
        return {}

    def member_cards(self, actor, collection, rows):
        return [{**row,"cover":None} for row in rows]

    def business_fields(self, actor, collection, value):
        return value

    def participant_delivery_current(self, chat_id):
        return True

    def provider_usage(self):
        return {}

    def available_credit(self):
        return '0'

    def agent_capabilities(self):
        return {}

    def validate_activation(self, definition, once):
        pass

    def usage_fields(self):
        return {}

    def collection_names(self, principal=None):
        return self.selected["collections"].list(principal)

    def catalogue(self):
        return {'templates': []}

    def draft_agent(self, template_id, collection, principal):
        raise ValueError('Choose an available agent recipe.')

    def authorize_file(self, request, file_id, principal):
        if file_id.startswith('export-'):
            raise ValueError('Choose a file available in this workspace.')

    async def file_download(self, record, principal):
        raise ValueError('Choose a file available in this workspace.')

    async def tick(self):
        pass

    async def close(self):
        pass

    async def connector_details(self, saved, connector_id, owner, collection):
        return {}

    async def connection_fields(self, value, principal, request):
        return value

    def source_options(self, principal, collection):
        return []

    @asynccontextmanager
    async def request_scope(self, request):
        yield None


@asynccontextmanager
async def bare_lifespan(app, host):
    yield
