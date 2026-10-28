"""Private, environment-bound messaging commissioning with local files only."""
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from messaging_config import MessagingConfigurationError, provider_for
from messaging_provider import ProviderError
from workspace_identity import workspace_scope


def save(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def commissioned(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    state = tmp_path / 'workspace'
    scope = workspace_scope(state)
    credentials_path = private / 'credentials.json'
    credentials = {'schema_version': 1, 'environment_id': 'video-preview',
        'company_scope': scope, 'waba_id': '12345', 'phone_number_id': '67890',
        'access_token': 'synthetic-access-token', 'app_secret': 'synthetic-app-secret',
        'verify_token': 'synthetic-verify-token'}
    config = {'schema_version': 1, 'environment_id': 'video-preview', 'company_scope': scope,
        'credentials_path': str(credentials_path), 'waba_id': '12345', 'phone_number_id': '67890',
        'sender_phone': '+34600000000', 'api_version': 'v23.0',
        'commissioned': False, 'webhook_ready': False,
        'otp_template': {'name': 'rocoto_security_code', 'language': 'en_US',
                         'category': 'AUTHENTICATION', 'status': 'REJECTED'},
        'hello_template': {'name': 'rocoto_video_hello', 'language': 'en_US',
                           'category': 'UTILITY', 'status': 'PENDING',
                           'body': 'Hello from Rocoto. Reply here.'}}
    path = private / 'messaging.json'
    save(path, config)
    save(credentials_path, credentials)
    monkeypatch.setenv('ROCOTO_VIDEO_MESSAGING_CONFIG', str(path))
    return state, path, config, credentials_path, credentials


def test_unconfigured_and_foreign_environment_leave_workspace_untouched(tmp_path, monkeypatch):
    monkeypatch.delenv('ROCOTO_VIDEO_MESSAGING_CONFIG', raising=False)
    absent = tmp_path / 'unused'
    assert provider_for(absent, 'elsewhere') is None
    assert not absent.exists()
    _, _, _, credentials_path, _ = commissioned(tmp_path, monkeypatch)
    credentials_path.unlink()
    assert provider_for(absent, 'elsewhere') is None
    assert not absent.exists()


def test_actual_template_status_wins_over_incomplete_callback(tmp_path, monkeypatch):
    state, _, _, _, _ = commissioned(tmp_path, monkeypatch)
    provider = provider_for(state, 'video-preview')
    assert provider.config.company_id == workspace_scope(state)
    assert provider.config.otp_template.status == 'REJECTED'
    assert provider.config.hello_template.status == 'PENDING'
    assert provider.config.hello_template.body == 'Hello from Rocoto. Reply here.'
    assert provider.readiness()['reason'] == 'auth_template_required'
    assert provider.readiness()['hello_ready'] is False
    assert provider.transport is None


def test_matching_environment_requires_exact_company(tmp_path, monkeypatch):
    commissioned(tmp_path, monkeypatch)
    with pytest.raises(MessagingConfigurationError, match='private messaging configuration'):
        provider_for(tmp_path / 'other-company', 'video-preview')


def test_credentials_rotate_fresh_without_exposing_values(tmp_path, monkeypatch):
    state, _, _, credentials_path, credentials = commissioned(tmp_path, monkeypatch)
    provider = provider_for(state, 'video-preview')
    first = provider.credentials()
    assert first.access_token == 'synthetic-access-token'
    replacement = dict(credentials, access_token='synthetic-rotated-token')
    save(credentials_path, replacement)
    second = provider.credentials()
    assert second.access_token == 'synthetic-rotated-token'
    assert first is not second
    for secret in (first.access_token, first.app_secret, first.verify_token, second.access_token):
        assert secret not in repr(first) + repr(second) + repr(provider) + json.dumps(provider.readiness())


@pytest.mark.parametrize('field,value', [('environment_id', 'another-preview'), ('company_scope', '0' * 64),
    ('waba_id', '99999'), ('phone_number_id', '99999'), ('schema_version', 2)])
def test_rotated_credentials_stay_bound_to_original_account(tmp_path, monkeypatch, field, value):
    state, _, _, credentials_path, credentials = commissioned(tmp_path, monkeypatch)
    provider = provider_for(state, 'video-preview')
    credentials[field] = value
    save(credentials_path, credentials)
    with pytest.raises(ProviderError):
        provider.credentials()


@pytest.mark.parametrize('change', ['file-mode', 'parent-mode', 'file-symlink', 'parent-symlink', 'hardlink', 'duplicate-key', 'oversized'])
def test_private_configuration_checks_fail_closed(tmp_path, monkeypatch, change):
    state, path, _, _, _ = commissioned(tmp_path, monkeypatch)
    if change == 'file-mode':
        path.chmod(0o644)
    elif change == 'parent-mode':
        path.parent.chmod(0o755)
    elif change == 'file-symlink':
        target = path.with_name('target.json')
        path.rename(target)
        path.symlink_to(target)
    elif change == 'parent-symlink':
        alias = tmp_path / 'alias'
        alias.symlink_to(path.parent, target_is_directory=True)
        monkeypatch.setenv('ROCOTO_VIDEO_MESSAGING_CONFIG', str(alias / path.name))
    elif change == 'hardlink':
        os.link(path, path.with_name('alias.json'))
    elif change == 'duplicate-key':
        path.write_text('{"schema_version":1,"schema_version":2}')
    else:
        path.write_text('x' * (64 * 1024))
    with pytest.raises(MessagingConfigurationError) as error:
        provider_for(state, 'video-preview')
    assert str(path) not in str(error.value)
    assert 'synthetic' not in str(error.value)


@pytest.mark.parametrize('change', ['relative-config', 'relative-credentials', 'unknown-field', 'missing-status',
    'invalid-status', 'invalid-boolean', 'wrong-category', 'missing-hello', 'invalid-version'])
def test_explicit_schema_never_infers_template_approval(tmp_path, monkeypatch, change):
    state, path, config, _, _ = commissioned(tmp_path, monkeypatch)
    if change == 'relative-config':
        monkeypatch.chdir(path.parent)
        monkeypatch.setenv('ROCOTO_VIDEO_MESSAGING_CONFIG', path.name)
    elif change == 'relative-credentials':
        config['credentials_path'] = 'credentials.json'
    elif change == 'unknown-field':
        config['access_token'] = 'synthetic-should-stay-private'
    elif change == 'missing-status':
        del config['otp_template']['status']
    elif change == 'invalid-status':
        config['otp_template']['status'] = 'approved'
    elif change == 'invalid-boolean':
        config['commissioned'] = 'true'
    elif change == 'wrong-category':
        config['otp_template']['category'] = 'UTILITY'
    elif change == 'missing-hello':
        del config['hello_template']
    elif change == 'invalid-version':
        config['schema_version'] = True
    save(path, config)
    with pytest.raises(MessagingConfigurationError):
        provider_for(state, 'video-preview')


def test_absent_template_and_pending_hello_remain_blocked(tmp_path, monkeypatch):
    state, path, config, _, _ = commissioned(tmp_path, monkeypatch)
    config['otp_template'] = None
    save(path, config)
    assert provider_for(state, 'video-preview').readiness()['reason'] == 'auth_template_required'
    config['otp_template'] = {'name': 'rocoto_security_code', 'language': 'en_US',
        'category': 'AUTHENTICATION', 'status': 'APPROVED'}
    config.update(commissioned=True, webhook_ready=True)
    save(path, config)
    readiness = provider_for(state, 'video-preview').readiness()
    assert readiness['ready'] is True
    assert readiness['hello_ready'] is False
    assert readiness['hello_reason'] == 'hello_template_required'


def test_credentials_private_checks_apply_on_every_read(tmp_path, monkeypatch):
    state, _, _, credentials_path, _ = commissioned(tmp_path, monkeypatch)
    provider = provider_for(state, 'video-preview')
    assert provider.credentials().access_token
    credentials_path.chmod(0o644)
    with pytest.raises(ProviderError):
        provider.credentials()


@pytest.mark.parametrize('value', [None, {}, 'template', [None], [False], [0]])
def test_utility_templates_require_bounded_valid_template_records(tmp_path, monkeypatch, value):
    state, path, config, _, _ = commissioned(tmp_path, monkeypatch)
    config['utility_templates'] = value
    save(path, config)
    with pytest.raises(MessagingConfigurationError):
        provider_for(state, 'video-preview')


def spend_document(tmp_path, monkeypatch):
    private = tmp_path / 'private-spend'
    private.mkdir(mode=0o700)
    state = tmp_path / 'workspace'
    value = {'schema_version': 1, 'environment_id': 'video-preview', 'company_scope': workspace_scope(state),
        'company_daily_messages': 200, 'agent_daily_messages': 50, 'stopped': False}
    path = private / 'phone-spend.json'
    save(path, value)
    monkeypatch.setenv('ROCOTO_VIDEO_PHONE_SPEND_CONFIG', str(path))
    return state, path, value


def test_the_phone_spend_record_has_no_default_and_leaves_other_workspaces_untouched(tmp_path, monkeypatch):
    from messaging_config import spend_for
    from phone_spend import SpendHeld, record
    monkeypatch.delenv('ROCOTO_VIDEO_PHONE_SPEND_CONFIG', raising=False)
    absent = tmp_path / 'unused'
    reader = spend_for(absent, 'elsewhere')
    with pytest.raises(MessagingConfigurationError, match='Review the private messaging configuration'):
        reader()
    with pytest.raises(SpendHeld) as held:
        record(reader)
    assert held.value.reason == 'spend_unreadable'
    spend_document(tmp_path, monkeypatch)
    with pytest.raises(MessagingConfigurationError):
        reader()  # another environment's record admits nothing here
    assert not absent.exists()


def test_the_phone_spend_record_is_read_afresh_and_bound_to_its_environment_and_company(tmp_path, monkeypatch):
    from messaging_config import spend_for
    state, path, value = spend_document(tmp_path, monkeypatch)
    reader = spend_for(state, 'video-preview')
    assert reader() == {'company_daily_messages': 200, 'agent_daily_messages': 50, 'stopped': False}
    # The host's Stop and a changed cap apply to the next read, with no restart.
    save(path, {**value, 'stopped': True, 'agent_daily_messages': 5})
    assert reader() == {'company_daily_messages': 200, 'agent_daily_messages': 5, 'stopped': True}
    with pytest.raises(MessagingConfigurationError):
        spend_for(tmp_path / 'another-company', 'video-preview')()
    changed = [{key: item for key, item in value.items() if key != 'agent_daily_messages'},   # an absent cap
        {**value, 'schema_version': 2}, {**value, 'schema_version': True}, {**value, 'monthly_messages': 5000},
        {**value, 'company_scope': '0' * 64}, [value], 'open']
    for document in changed:
        save(path, document)
        with pytest.raises(MessagingConfigurationError) as caught:
            reader()
        assert str(path) not in str(caught.value) and '200' not in str(caught.value)
    save(path, value)
    path.chmod(0o644)
    with pytest.raises(MessagingConfigurationError):
        reader()  # the record stays an owner-only document
    monkeypatch.setenv('ROCOTO_VIDEO_PHONE_SPEND_CONFIG', 'relative/phone-spend.json')
    with pytest.raises(MessagingConfigurationError):
        reader()
