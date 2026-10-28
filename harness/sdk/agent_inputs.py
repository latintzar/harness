"""Input provider contract for the shared saved-agent store and scheduler.

Providers operate inside the store's existing transaction and execution grant.
They select input and supply context; the store owns identity, revisions, queue,
run accounting and recovery. Installation is complete before scheduler recovery.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    instruction: str = Field(min_length=1, max_length=8000)
    description: str = Field(default="", max_length=220)
    dataset: str = Field(min_length=1, max_length=100)
    agent_kind: str = Field(default="action", pattern=r"^[a-z][a-z0-9_]{0,79}$")
    input_kind: str = Field(default="none", pattern=r"^[a-z][a-z0-9_]{0,79}$")
    mode: Literal["ad_hoc", "scheduled", "ongoing"] = "ad_hoc"
    interval_seconds: int = Field(default=3600, ge=10, le=604800)
    max_runs: int = Field(default=1, ge=1, le=20)
    signal_chain: list[dict] = Field(default_factory=lambda:[{'kind':'deliver'}], min_length=1, max_length=8)
    output: Literal["report"] = "report"
    skill_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_schedule(self):
        if not self.title.strip() or not self.instruction.strip():
            raise ValueError("Give the agent a title and an instruction.")
        if self.mode == "scheduled" and self.interval_seconds < 60:
            raise ValueError("Scheduled agents run at intervals of at least one minute.")
        if self.mode == "ad_hoc":
            self.max_runs = 1
        if (len(set(self.skill_ids)) != len(self.skill_ids) or any(
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', key) for key in self.skill_ids)):
            raise ValueError("Choose distinct registered skill identifiers.")
        return self


@dataclass
class InputBatch:
    sources: dict = field(default_factory=dict)
    records: list = field(default_factory=list)
    screening: dict | None = None
    snapshot: object = None


@dataclass
class InputSelection:
    sources: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    allow_empty: bool = False


class AgentInputProvider:
    """Subclass for a declared vertical input; default implementation is no-source work."""
    definition_model = AgentDefinition
    inherited_fields = ('agent_kind', 'input_kind', 'signal_chain')

    def attach(self, store):
        self.store = store

    def normalize(self, record, table):
        if table == 'agents':
            for name in ('description', 'skill_ids', 'agent_kind', 'input_kind', 'signal_chain'):
                item = self.definition_model.model_fields[name]
                record.setdefault(name, item.get_default(call_default_factory=True))
        record.setdefault('resource_ceiling_usd', '0')
        if table == 'runs':
            record.setdefault('resource_uses', 0)
        return record

    def collection_allowed(self, spec, datasets, owner):
        return spec['dataset'] in datasets

    def validate_definition(self, spec):
        if spec['input_kind'] != 'none':
            raise ValueError("Install the declared input provider for this agent.")

    def definition_context(self, agent, chat_id, arguments):
        return None

    def prepare_record(self, record, context=None):
        pass

    def describe(self, record, db):
        pass

    def runnable(self, record):
        self.validate_definition(record)
        return True

    def validate_activation(self, record):
        self.runnable(record)

    def resource_ceiling(self):
        return Decimal('0')

    def resource_allowance(self, definition):
        return 0

    def validate_resource_budget(self, definition, *, runs=1):
        if self.resource_allowance(definition):
            raise ValueError('Install the resource budget required by this input provider.')

    def resource_admitted(self, run):
        pass

    def validate_wake(self, wake, definition):
        return True

    def recovered(self, db, run):
        pass

    def claimed(self, db, record, run):
        pass

    def finished(self, db, run, status):
        pass

    async def refresh(self, definitions):
        pass

    def ready(self, definition, wake, agent, input_optional):
        return self.runnable(definition)

    async def prepare(self, definition, wake, agent):
        return InputBatch()

    def select(self, db, record, sources, source_records, screening, now, wake):
        self.validate_definition(record)
        if sources or source_records or screening:
            raise ValueError("Use the declared provider to admit source inputs.")
        return InputSelection(allow_empty=True)

    def execution(self, definition):
        ready = definition.get('input_kind', 'none') == 'none'
        return {'configurable': True, 'ready': ready, 'required_input': 'none',
                'notice': None if ready else 'Install this agent’s declared input provider.'}

    def recurring_ready(self, collection, principal=None):
        return True

    @contextmanager
    def context(self, agent, run, snapshot=None):
        yield run['definition']['instruction'] + "\n\nProduce a report for the selected workspace: " + run['definition']['dataset'] + "."


class NoInputProvider(AgentInputProvider):
    """Explicit no-source input for ordinary workspace jobs."""
