"""Validated workspace tools and host-owned authority shared by every transport.

A registry is immutable after construction. Verticals contribute declared families;
transport code only selects a surface and supplies an admitted workspace context.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from jsonschema import Draft202012Validator


SURFACES = frozenset({'hosted', 'native', 'public', 'local'})
REGISTRATION_ERROR = ('Give each tool of a tool family one name: a read or a write, '
                      'specified once, beside the names in use.')


@dataclass(frozen=True)
class ToolContext:
    """Identity comes from the host; arguments carry only the requested operation."""
    state: Path
    company_id: str
    actor: str
    data_owner: str
    collection: str
    chat_id: str | None = None
    record: dict | None = None
    run: dict | None = None
    agents: object | None = None
    agent: object | None = None
    admission: object | None = None
    extension: object | None = None
    local_authority: object | None = None

    @property
    def owner(self):
        return self.actor

    @property
    def dataset(self):
        return self.collection

    def require_current(self):
        if not self.actor or not self.collection or self.admission is None or not self.admission.current():
            raise ValueError('Continue in the current authorized workspace.')
        if self.record is not None and (self.record.get('dataset') != self.collection
                or self.record.get('owner_principal_id') != self.data_owner):
            raise ValueError('Continue in the owning conversation and collection.')

    def require_parent(self):
        self.require_current()
        record = self.record or {}
        if (not self.agent or not self.chat_id or self.run is None
                or self.agent.active.get(self.chat_id) is not self.run
                or record.get('parent_id') or record.get('native_root_id')
                or not isinstance(self.run.get('cancel'), asyncio.Event)
                or self.run['cancel'].is_set()):
            raise ValueError('Use the active parent conversation for this change.')


async def _offload(function, *arguments):
    """Retain admission until synchronous work finishes, including after cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *arguments))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()
        raise


def validate_families(families, *, reserved_reads=(), reserved_writes=()):
    """Validate the original family protocol without importing a vertical."""
    reads, writes = set(reserved_reads), set(reserved_writes)
    for family in families:
        own_reads, own_writes = set(family.READ_NAMES), set(family.WRITE_NAMES)
        read_specs, all_specs = family.specs(False), family.specs(True)
        waited = set(getattr(family, 'ASYNC_NAMES', ()))
        if (own_reads & own_writes or (own_reads | own_writes) & (reads | writes)
                or sorted(spec['name'] for spec in read_specs) != sorted(own_reads)
                or sorted(spec['name'] for spec in all_specs) != sorted(own_reads | own_writes)
                or not waited <= own_writes
                or waited and not callable(getattr(family, 'acall', None))
                or own_reads and not any(callable(getattr(family, method, None)) for method in ('read', 'aread'))
                or own_writes - waited and not callable(getattr(family, 'call', None))):
            raise ValueError(REGISTRATION_ERROR)
        for spec in all_specs:
            Draft202012Validator.check_schema(spec['inputSchema'])
        local_writes = getattr(family, 'LOCAL_WRITES', frozenset())
        if not set(local_writes) <= own_writes or local_writes and not callable(getattr(family, 'local_call', None)):
            raise ValueError('Declare local actions with their explicit host authority handler.')
        for surface, visible in getattr(family, 'SURFACE_READS', {}).items():
            if surface not in SURFACES - {'hosted'} or not set(visible) <= own_reads:
                raise ValueError('Declare each transport surface using reviewed read tools.')
        reads.update(own_reads)
        writes.update(own_writes)
    return frozenset(reads), frozenset(writes)


class ToolRegistry:
    """One listing and dispatch table with explicit transport visibility."""

    def __init__(self, families):
        self.families = tuple(families)
        self.reads, self.writes = validate_families(self.families)
        definitions, owners, visible = {}, {}, {surface: set() for surface in SURFACES}
        for family in self.families:
            for spec in family.specs(True):
                name = spec['name']
                definitions[name] = copy.deepcopy(spec)
                owners[name] = family
            visible['hosted'].update(family.READ_NAMES | family.WRITE_NAMES)
            visible['local'].update(getattr(family, 'LOCAL_WRITES', ()))
            for surface in SURFACES - {'hosted'}:
                visible[surface].update(getattr(family, 'SURFACE_READS', {}).get(surface, family.READ_NAMES))
        self._specs = MappingProxyType(definitions)
        self._owners = MappingProxyType(owners)
        self._visible = MappingProxyType({key: frozenset(value) for key, value in visible.items()})
        self._validators = MappingProxyType({name: Draft202012Validator(spec['inputSchema'])
                                            for name, spec in definitions.items()})

    def names(self, surface='hosted'):
        return self._visible[surface]

    def read_names(self, surface='hosted'):
        return self._visible[surface] & self.reads

    def specs(self, write=True, *, surface='hosted'):
        names = self._visible[surface]
        if not write or surface in {'native', 'public'}:
            names = names & self.reads
        return [copy.deepcopy(spec) for name, spec in self._specs.items() if name in names]

    def family_of(self, name):
        return self._owners.get(name)

    def declared(self, name, arguments):
        validator = self._validators.get(name)
        if validator is None or not isinstance(arguments, dict) or not validator.is_valid(arguments):
            raise ValueError('Use the declared fields of this tool.')
        return arguments

    async def invoke(self, context, name, arguments, *, surface='hosted'):
        if name not in self._visible[surface] or surface in {'native', 'public'} and name not in self.reads:
            raise ValueError('Choose an available workspace tool.')
        self.declared(name, arguments)
        context.require_current()
        if arguments.get('dataset', context.collection) != context.collection:
            raise ValueError('Use the collection selected by the host.')
        family = self._owners[name]
        if name in self.writes:
            if surface == 'local':
                if context.local_authority is None or context.local_authority() is not True:
                    raise ValueError('Use the host-authorized local workspace for this change.')
            else:
                context.require_parent()
        adapter = getattr(family, 'context', None)
        selected = adapter(context) if adapter else context
        if surface == 'local' and name in self.writes:
            result = await family.local_call(selected, name, arguments)
        elif name in getattr(family, 'ASYNC_NAMES', ()):
            result = await family.acall(selected, name, arguments)
        elif name in self.reads and callable(getattr(family, 'aread', None)):
            result = await family.aread(selected, name, arguments)
        else:
            function = family.read if name in self.reads else family.call
            result = await _offload(function, selected, name, arguments)
        context.require_current()
        return result


def build_registry(agent=None, *, families=(), definition_model=None):
    """Construct the common product plus exactly the selected vertical families."""
    from conversation_tools import ConversationTools
    import harness_tools
    from wake_tools import FAMILY as wakes
    from recipe_runs import FAMILY as recipes
    from delegated_jobs import FAMILY as delegated
    if definition_model is None and agent is not None:
        definition_model = getattr(getattr(agent, 'automation_store', None), 'definition_model', None)
    if definition_model is None:
        models = {getattr(family, 'definition_model', None) for family in families} - {None}
        if len(models) > 1:
            raise ValueError('Declare one agent definition model for this workspace.')
        definition_model = next(iter(models), None)
    return ToolRegistry((ConversationTools(definition_model), harness_tools, wakes, recipes, delegated,
                         *(VerticalFamily(family) for family in families)))


class VerticalFamily:
    """Apply only the selected extension's context adapter to its own tools."""
    def __init__(self, family):
        self.family = family

    def __getattr__(self, name):
        return getattr(self.family, name)

    @staticmethod
    def context(context):
        return context.extension.tool_context(context) if context.extension is not None else context

