"""Optional built-ins are selected by a vertical and commissioned by its own host."""
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

import pack
from workspace_identity import workspace_scope


def test_vertical_selection_is_explicit_and_defaults_closed(tmp_path):
    retail = pack.load('retail')
    bare = pack.load('bare')
    assert retail.builtin_capabilities() == {'whatsapp': True, 'viber': False, 'email': False, 'voice': False}
    assert all(bare.builtin_capabilities().values())
    from capability_contract import selection
    assert not any(selection(None).values())
    for invalid in ({'email': 'yes'}, {'unknown': True}, ['email']):
        with pytest.raises(ValueError):
            selection(invalid)


def test_disabled_capability_calls_neither_factory_nor_poller(tmp_path):
    from builtin_capabilities import BuiltInCapabilities
    calls = []
    def factory(state, environment):
        calls.append((state, environment))
        return SimpleNamespace(company_id=workspace_scope(state), channel='email', account_id='account',
            readiness=lambda: {'ready': True, 'configured': True}, poll=lambda: None)
    disabled = BuiltInCapabilities(tmp_path, 'company', {'email': False}, factories={'email': factory})
    assert disabled.provider('email') is None and disabled.pollers() == ()
    assert disabled.status()['email']['selected'] is False and not calls
    enabled = BuiltInCapabilities(tmp_path, 'company', {'email': True}, factories={'email': factory})
    status = enabled.status()['email']
    assert status['installed'] and status['provider_ready'] and not status['spending_ready']
    assert not status['ready'] and len(calls) == 1
    assert len(enabled.pollers()) == 1 and len(calls) == 1


def test_foreign_provider_and_factory_errors_have_safe_status(tmp_path):
    from builtin_capabilities import BuiltInCapabilities
    foreign = SimpleNamespace(company_id='another-company', channel='email', account_id='account')
    with pytest.raises(ValueError):
        BuiltInCapabilities(tmp_path, 'company', {'email': True}, factories={'email': lambda *args: foreign}).provider('email')
    def unavailable(*args):
        raise ValueError('private-credential-must-stay-private')
    selected = BuiltInCapabilities(tmp_path, 'company', {'email': True}, factories={'email': unavailable})
    status = selected.status()['email']
    assert not status['ready'] and not status['installed']
    assert 'private-credential' not in json.dumps(status)


def test_ready_requires_installed_host_transport_as_well_as_account_and_funds(tmp_path):
    from builtin_capabilities import BuiltInCapabilities
    def factory(state, environment):
        return SimpleNamespace(company_id=workspace_scope(state), channel='email', account_id='account',
            readiness=lambda: {'ready': True, 'configured': True})
    selected = BuiltInCapabilities(tmp_path, 'company', {'email': True},
        factories={'email': factory}, spending={'email': lambda: True})
    status = selected.status()['email']
    assert status['provider_ready'] and status['spending_ready']
    assert not status['host_ready'] and not status['ready']
    selected.host_ready['email'] = True
    assert selected.status()['email']['ready']
