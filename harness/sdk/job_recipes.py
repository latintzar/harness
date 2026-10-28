"""Vertical job instructions and bounded role declarations for managed delegation."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Key = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')]
Text = Annotated[str, Field(min_length=1, max_length=8000)]
Tool = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,79}$')]


class Declared(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SubAgentRole(Declared):
    id: Key
    title: Annotated[str, Field(min_length=1, max_length=80)]
    job: Annotated[str, Field(min_length=1, max_length=400)]
    instructions: Text
    launched_by: Annotated[list[Key], Field(min_length=1, max_length=12)]
    tools: Annotated[list[Tool], Field(max_length=32)]
    model: Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9.-]{0,79}$')]
    effort: Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] = 'low'
    limit_cents: Annotated[int, Field(ge=1, le=40)] = 20
    timeout_seconds: Annotated[int, Field(ge=10, le=600)] = 180
    per_turn: Annotated[int, Field(ge=1, le=2)] = 1


class RecipeStep(Declared):
    id: Key
    title: Annotated[str, Field(min_length=1, max_length=100)]
    instructions: Text
    agent: Key = 'parent'
    tools: Annotated[list[Tool], Field(max_length=32)] = Field(default_factory=list)
    after: Annotated[list[Key], Field(max_length=20)] = Field(default_factory=list)
    done_when: Annotated[str, Field(min_length=1, max_length=1000)]


class Recipe(Declared):
    id: Key
    revision: Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    title: Annotated[str, Field(min_length=1, max_length=100)]
    job: Annotated[str, Field(min_length=1, max_length=400)]
    instructions: Text
    launched_by: Annotated[list[Key], Field(min_length=1, max_length=12)]
    steps: Annotated[list[RecipeStep], Field(min_length=1, max_length=20)]
    done_when: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode='after')
    def ordered_steps(self):
        seen = set()
        for step in self.steps:
            if step.id in seen or len(step.after) != len(set(step.after)) or not set(step.after) <= seen:
                raise ValueError('Give each step a unique name and earlier prerequisites.')
            seen.add(step.id)
        return self


def _rows(reader, name, value, model, maximum):
    if isinstance(value, str):
        _, value = reader.data(name, value)
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f'{name} is a list of up to {maximum} declarations.')
    result = []
    for raw in value:
        item = model.model_validate(raw).model_dump()
        path = reader.text(f'{name}.{item["id"]}.instructions', item['instructions'])
        item['instructions'] = reader.files[path].decode('utf-8')
        if len(item['instructions']) > 8000:
            raise ValueError('Keep each job or role instruction within 8000 characters.')
        result.append(item)
    if len({item['id'] for item in result}) != len(result):
        raise ValueError(f'Give each {name} declaration a unique id.')
    return tuple(result)


def load_jobs(reader, recipes, roles, classes, chats):
    """Read exact vertical instruction bytes and validate cross-references once."""
    from pack import PackError
    try:
        roles = _rows(reader, 'sub_agents', roles, SubAgentRole, 12)
        recipes = _rows(reader, 'recipes', recipes, Recipe, 32)
        launchers = {'chat'} | {row['kind'] for row in classes} | {row['purpose'] for row in chats}
        by_role = {row['id']: row for row in roles}
        for item in (*roles, *recipes):
            if len(item['launched_by']) != len(set(item['launched_by'])) or not set(item['launched_by']) <= launchers:
                raise ValueError('Declare existing conversation purposes or agent classes as launchers.')
        if any(sum(role['limit_cents'] * role['per_turn'] for role in roles if launcher in role['launched_by']) > 80
               for launcher in launchers):
            raise ValueError('Keep declared sub-agent allowances within 80 cents per parent turn.')
        for role in roles:
            if len(role['tools']) != len(set(role['tools'])):
                raise ValueError('Give a sub-agent each permitted tool once.')
        for recipe in recipes:
            for step in recipe['steps']:
                if step['agent'] != 'parent':
                    role = by_role.get(step['agent'])
                    if role is None or not set(recipe['launched_by']) <= set(role['launched_by']):
                        raise ValueError('Assign each recipe step to a role its conversation may launch.')
                    if not set(step['tools']) <= set(role['tools']):
                        raise ValueError('Keep each step within its declared sub-agent tools.')
        return recipes, roles
    except (ValidationError, ValueError) as error:
        raise PackError(str(error)) from None


def launcher_for(agent, chat_id):
    """Only host-confirmed fixed purposes select an Organization recipe."""
    record = agent.children.get(chat_id, {})
    organization = getattr(agent, 'organization_chats', None)
    if organization is not None and organization.canonical(record):
        return record['organization_purpose']
    saved = getattr(agent, 'automation_store', None)
    if record.get('agent_id') and saved is not None:
        return saved.get(record['agent_id'])['agent_kind']
    return 'chat'


def available(vertical, launcher):
    return [row for row in vertical.recipes() if launcher in row['launched_by']]


def instructions_for(agent, chat_id, vertical):
    jobs = available(vertical, launcher_for(agent, chat_id))
    if not jobs:
        return ''
    lines = ['Job recipes for this conversation:',
        'Read the complete recipe with get_recipe. Start its durable record with begin_recipe, then record completed steps with their evidence. Resume that record after interruptions.']
    lines.extend(f'{row["id"]}: {row["job"]} Finished when: {row["done_when"]}' for row in jobs)
    return '\n'.join(lines)
