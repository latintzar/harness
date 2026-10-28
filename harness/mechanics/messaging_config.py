"""Private host commissioning for one explicit workspace and company.

ROCOTO_MESSAGING_CONFIG names an owner-only JSON document. Its separately
stored credentials are read afresh for rotation; neither file is a user setting.
Template statuses are provider facts, and construction makes no provider calls.
ROCOTO_PHONE_SPEND_CONFIG names the spend record of participant phone
chat, held beside the channel accounts and read afresh ahead of every send.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from messaging_provider import MetaConfiguration, MetaCredentials, MetaWhatsApp, ProviderError, Template
from registered_oauth import read_private_configuration
from workspace_identity import workspace_scope

MAX_CONFIG_BYTES = 32 * 1024
_CONFIG_FIELDS = {'schema_version', 'environment_id', 'company_scope', 'credentials_path',
    'waba_id', 'phone_number_id', 'sender_phone', 'api_version', 'commissioned',
    'webhook_ready', 'otp_template', 'hello_template'}
_BINDING_FIELDS = {'schema_version', 'environment_id', 'company_scope', 'waba_id', 'phone_number_id'}
_SECRET_FIELDS = {'access_token', 'app_secret', 'verify_token'}
_SPEND_FIELDS = ('company_daily_messages', 'agent_daily_messages', 'stopped')


class MessagingConfigurationError(ProviderError):
    """Sanitized configuration error; contains neither paths nor private values."""


def _fail():
    raise MessagingConfigurationError('Review the private messaging configuration.') from None


def _absolute_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
        _fail()
    path = Path(value)
    if not path.is_absolute():
        _fail()
    return path


def _read(path):
    try:
        return read_private_configuration(path, max_bytes=MAX_CONFIG_BYTES)
    except (ValueError, OSError, TypeError):
        _fail()


def _template(value, category):
    if value is None:
        return None
    required = {'name', 'language', 'category', 'status'}
    if (not isinstance(value, dict) or set(value) - required - {'body'}
            or not required <= set(value) or value['category'] != category):
        _fail()
    try:
        return Template(**value)
    except (ValueError, TypeError):
        _fail()


def provider_for(state, environment_id):
    """Build the provider for the selected workspace, or leave other environments alone.

    The host config declares schema_version=1, exact environment_id/company_scope,
    credentials_path, all MetaConfiguration fields except derived company_id, and
    explicit nullable otp_template/hello_template records. The credential JSON has
    the same schema/environment/company/account binding plus three private fields:
    access_token, app_secret, verify_token. Both documents use the shared bounded
    regular-file validator (private parent and file, owner-held, symlink-free).
    """
    location = os.environ.get('ROCOTO_MESSAGING_CONFIG', os.environ.get('ROCOTO_VIDEO_MESSAGING_CONFIG'))
    if not location:
        return None
    raw = _read(_absolute_path(location))
    if (not isinstance(raw, dict) or set(raw) - {'utility_templates'} != _CONFIG_FIELDS
            or type(raw['schema_version']) is not int or raw['schema_version'] != 1
            or not isinstance(raw['environment_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', raw['environment_id'])
            or not isinstance(raw['company_scope'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', raw['company_scope'])):
        _fail()
    if raw['environment_id'] != environment_id:
        return None
    if 'utility_templates' in raw and (not isinstance(raw['utility_templates'], list) or len(raw['utility_templates']) > 32):
        _fail()
    try:
        company = workspace_scope(state)
        if raw['company_scope'] != company:
            _fail()
        credentials_path = _absolute_path(raw['credentials_path'])
        configuration = MetaConfiguration(company_id=company, waba_id=raw['waba_id'],
            phone_number_id=raw['phone_number_id'], sender_phone=raw['sender_phone'],
            api_version=raw['api_version'], commissioned=raw['commissioned'],
            webhook_ready=raw['webhook_ready'],
            otp_template=_template(raw['otp_template'], 'AUTHENTICATION'),
            hello_template=_template(raw['hello_template'], 'UTILITY'),
            utility_templates=tuple(_template(v, 'UTILITY') for v in raw.get('utility_templates', [])))
    except (ValueError, TypeError, OSError):
        _fail()
    binding = {key: raw[key] for key in _BINDING_FIELDS}

    def credentials():
        value = _read(credentials_path)
        if (not isinstance(value, dict) or set(value) != _BINDING_FIELDS | _SECRET_FIELDS
                or type(value['schema_version']) is not int
                or any(value[key] != expected for key, expected in binding.items())):
            _fail()
        try:
            return MetaCredentials(**{key: value[key] for key in _SECRET_FIELDS})
        except (ValueError, TypeError):
            _fail()

    return MetaWhatsApp(configuration, credentials)


def spend_for(state, environment_id):
    """The reader of the host's spend record for participant phone chat.

    The owner-only JSON document declares schema_version=1, the exact
    environment_id and company_scope, company_daily_messages,
    agent_daily_messages and stopped: the owner's recorded authorization under
    docs/COST-SAFETY.md. There is no built-in default. The reader runs ahead of
    every phone send, so the host's Stop and a changed cap apply to the next
    one. A missing, unreadable or malformed document, or one bound to another
    environment or company, raises, and the store then admits zero sends.
    """
    def read():
        location = os.environ.get('ROCOTO_PHONE_SPEND_CONFIG', os.environ.get('ROCOTO_VIDEO_PHONE_SPEND_CONFIG'))
        if not location:
            _fail()
        raw = _read(_absolute_path(location))
        try:
            bound = (isinstance(raw, dict) and set(raw) == {'schema_version', 'environment_id', 'company_scope', *_SPEND_FIELDS}
                and type(raw['schema_version']) is int and raw['schema_version'] == 1
                and raw['environment_id'] == environment_id and raw['company_scope'] == workspace_scope(state))
        except (ValueError, TypeError, OSError):
            bound = False
        if not bound:
            _fail()
        return {key: raw[key] for key in _SPEND_FIELDS}
    return read
