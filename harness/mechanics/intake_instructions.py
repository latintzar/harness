"""Host-selected standing instructions for canonical company conversations.

Instructions explain registered capabilities. Canonical conversation identity,
permissions and readiness remain host owned on every resumed native turn.

The text belongs to the template this process serves (pack.py): a shared prefix and one file
per fixed conversation under packs/<id>/prompts/. This module selects it for a turn.
"""
import pack

# Names earlier code imports, bound to the active template's files on each read.
_FIXED_CHAT_TEXT = {'CAMERA': 'cameras', 'ORGANIZATION': 'organization'}


def __getattr__(name):
    if name == 'COMMON':
        return pack.active().prompt('fixed_chat_common')
    if name in _FIXED_CHAT_TEXT and _FIXED_CHAT_TEXT[name] in pack.active().purposes():
        return pack.active().fixed_chat_text(_FIXED_CHAT_TEXT[name])
    raise AttributeError(name)


def for_chat(agent, chat_id, base):
    """Attach only host-authenticated purpose instructions, including on resume."""
    from job_recipes import instructions_for
    jobs = instructions_for(agent, chat_id, pack.active())
    if jobs:
        base += '\n' + jobs
    organization = getattr(agent, 'organization_chats', None)
    record = agent.children.get(chat_id, {})
    if organization is None or not organization.canonical(record):
        return base
    extra = pack.active().fixed_chat_prompt(record.get('organization_purpose'))
    return base + '\n' + extra if extra else base
