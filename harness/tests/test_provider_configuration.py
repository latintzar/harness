"""Host-selected OAuth configuration stays bound to one persisted company."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from provider_configuration import registered_clients_for, commissioned_writes_for
from workspace_identity import workspace_scope


def test_unconfigured_host_keeps_registered_providers_pending(tmp_path, monkeypatch):
    monkeypatch.delenv('ROCOTO_REGISTERED_OAUTH_DIRECTORY', raising=False)
    assert registered_clients_for(tmp_path, public_origin='https://rocoto.example') is None


def test_global_write_setting_only_applies_to_registered_company():
    selected = {'notion', 'google-drive', 'google-docs'}
    assert commissioned_writes_for(selected, None) == {'notion'}
    configured = SimpleNamespace(get=lambda provider: object() if provider == 'google-drive' else None)
    assert commissioned_writes_for(selected, configured) == {'notion', 'google-drive'}
    # Invalid host provider names retain the broker's explicit validation error.
    assert commissioned_writes_for({'unknown-provider'}, None) == {'unknown-provider'}


def test_company_selects_only_its_own_host_file(tmp_path, monkeypatch):
    directory = tmp_path / 'clients'
    directory.mkdir(mode=0o700)
    monkeypatch.setenv('ROCOTO_REGISTERED_OAUTH_DIRECTORY', str(directory))
    company = tmp_path / 'company'
    other = tmp_path / 'other'
    selected = directory / (workspace_scope(company) + '.json')
    selected.write_text('{}')
    selected.chmod(0o600)
    calls = []
    expected = object()

    def load(path, **kwargs):
        calls.append((path, kwargs))
        return expected

    monkeypatch.setitem(sys.modules, 'registered_oauth', SimpleNamespace(
        RegisteredOAuthClients=SimpleNamespace(load=load)))
    assert registered_clients_for(other, public_origin='https://rocoto.example') is None
    assert registered_clients_for(company, public_origin='https://rocoto.example') is expected
    assert calls == [(selected, {'company_scope': workspace_scope(company),
                                'public_origin': 'https://rocoto.example'})]


def test_host_directory_must_be_private_absolute_and_real(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_REGISTERED_OAUTH_DIRECTORY', 'relative-clients')
    with pytest.raises(ValueError, match='absolute'):
        registered_clients_for(tmp_path / 'company', public_origin='https://rocoto.example')
    directory = tmp_path / 'clients'
    directory.mkdir(mode=0o700)
    directory.chmod(0o755)
    monkeypatch.setenv('ROCOTO_REGISTERED_OAUTH_DIRECTORY', str(directory))
    with pytest.raises(ValueError, match='private'):
        registered_clients_for(tmp_path / 'company', public_origin='https://rocoto.example')
    directory.chmod(0o700)
    linked = tmp_path / 'linked'
    linked.symlink_to(directory, target_is_directory=True)
    monkeypatch.setenv('ROCOTO_REGISTERED_OAUTH_DIRECTORY', str(linked))
    with pytest.raises(ValueError, match='private'):
        registered_clients_for(tmp_path / 'company', public_origin='https://rocoto.example')


def test_company_file_cannot_redirect_to_another_file(tmp_path, monkeypatch):
    directory = tmp_path / 'clients'
    directory.mkdir(mode=0o700)
    monkeypatch.setenv('ROCOTO_REGISTERED_OAUTH_DIRECTORY', str(directory))
    company = tmp_path / 'company'
    other_file = tmp_path / 'other.json'
    other_file.write_text('{}')
    (directory / (workspace_scope(company) + '.json')).symlink_to(other_file)
    with pytest.raises(ValueError, match='regular'):
        registered_clients_for(company, public_origin='https://rocoto.example')


def test_hidden_setup_writes_valid_private_clients_and_advances_revision(tmp_path):
    import importlib.util
    from registered_oauth import RegisteredOAuthClients
    script = Path(__file__).parents[1] / 'deploy/connections/configure_google.py'
    spec = importlib.util.spec_from_file_location('configure_google', script)
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    state = tmp_path / 'company'
    config = tmp_path / 'clients'
    identifier = 'fixture-client.apps.googleusercontent.com'
    secret = 'synthetic-client-value'
    result = setup.save_registration(state, config, identifier, secret,
                                     public_origin='https://rocoto.example')
    clients = RegisteredOAuthClients.load(result, company_scope=workspace_scope(state),
                                          public_origin='https://rocoto.example')
    assert {client.provider for client in clients.clients} == {'google-drive', 'google-docs'}
    assert {client.revision for client in clients.clients} == {1}
    assert result.stat().st_mode & 0o077 == 0
    assert identifier not in repr(clients) and secret not in repr(clients)
    original = result.read_bytes()
    with pytest.raises(ValueError):
        setup.save_registration(state, config, 'invalid', secret,
                                public_origin='https://rocoto.example')
    assert result.read_bytes() == original
    setup.save_registration(state, config, identifier, secret,
                            public_origin='https://rocoto.example')
    updated = RegisteredOAuthClients.load(result, company_scope=workspace_scope(state),
                                          public_origin='https://rocoto.example')
    assert {client.revision for client in updated.clients} == {2}
    from mcp_connections import MCPConnections
    import json
    MCPConnections(state, public_origin='https://rocoto.example', registered_clients=updated)
    removal = json.loads(result.read_text())
    removal['clients'] = {}
    result.write_text(json.dumps(removal))
    empty = RegisteredOAuthClients.load(result, company_scope=workspace_scope(state),
                                        public_origin='https://rocoto.example')
    MCPConnections(state, public_origin='https://rocoto.example', registered_clients=empty)
    setup.save_registration(state, config, identifier, secret,
                            public_origin='https://rocoto.example')
    restored = RegisteredOAuthClients.load(result, company_scope=workspace_scope(state),
                                           public_origin='https://rocoto.example')
    assert min(client.revision for client in restored.clients) > 2
    broker = MCPConnections(state, public_origin='https://rocoto.example', registered_clients=restored)
    assert broker.provider_available('google-drive')


def test_remote_provider_configuration_uses_private_company_selection(tmp_path, monkeypatch):
    from provider_configuration import remote_providers_for
    monkeypatch.delenv('ROCOTO_REMOTE_MCP_DIRECTORY', raising=False)
    company = tmp_path / 'company'
    assert remote_providers_for(company, public_origin='https://rocoto.example') is None
    directory = tmp_path / 'providers'
    directory.mkdir(mode=0o700)
    monkeypatch.setenv('ROCOTO_REMOTE_MCP_DIRECTORY', str(directory))
    other = tmp_path / 'other'
    selected = directory / (workspace_scope(company) + '.json')
    selected.write_text('{}')
    selected.chmod(0o600)
    calls = []
    expected = object()
    def load(path, **kwargs):
        calls.append((path, kwargs))
        return expected
    monkeypatch.setitem(sys.modules, 'catalogue_providers', SimpleNamespace(
        RemoteProviderCommissioning=SimpleNamespace(load=load)))
    assert remote_providers_for(other, public_origin='https://rocoto.example') is None
    assert remote_providers_for(company, public_origin='https://rocoto.example') is expected
    assert calls == [(selected, {'company_scope': workspace_scope(company),
                                'public_origin': 'https://rocoto.example'})]
    selected.unlink()
    assert remote_providers_for(company, public_origin='https://rocoto.example') is None


def test_remote_provider_host_config_cannot_follow_links_or_public_directory(tmp_path, monkeypatch):
    from provider_configuration import remote_providers_for
    company = tmp_path / 'company'
    monkeypatch.setenv('ROCOTO_REMOTE_MCP_DIRECTORY', 'relative-providers')
    with pytest.raises(ValueError, match='absolute'):
        remote_providers_for(company, public_origin='https://rocoto.example')
    directory = tmp_path / 'providers'
    directory.mkdir(mode=0o700)
    monkeypatch.setenv('ROCOTO_REMOTE_MCP_DIRECTORY', str(directory))
    selected = directory / (workspace_scope(company) + '.json')
    selected.symlink_to(tmp_path / 'other')
    with pytest.raises(ValueError, match='regular'):
        remote_providers_for(company, public_origin='https://rocoto.example')
    selected.unlink()
    directory.chmod(0o755)
    with pytest.raises(ValueError, match='private'):
        remote_providers_for(company, public_origin='https://rocoto.example')


def test_remote_write_setting_only_applies_to_company_commissioning(tmp_path):
    from test_catalogue_providers import configuration, descriptor
    requested = {'canva', 'notion', 'google-docs', 'unknown-provider'}
    assert commissioned_writes_for(requested, None) == {'notion', 'unknown-provider'}
    selected = configuration(tmp_path, descriptor('canva'))
    assert commissioned_writes_for(requested, None, selected) == {'notion', 'canva', 'unknown-provider'}
    selected = configuration(tmp_path, descriptor('canva', auth='oauth_static'))
    assert commissioned_writes_for(requested, None, selected) == {'notion', 'unknown-provider'}
    clients = SimpleNamespace(get=lambda provider: object() if provider == 'canva' else None)
    assert commissioned_writes_for(requested, clients, selected) == {'notion', 'canva', 'unknown-provider'}
    readonly = configuration(tmp_path, descriptor('canva', scopes_by_mode={'read': ['read']}))
    assert commissioned_writes_for(requested, clients, readonly) == {'notion', 'unknown-provider'}
