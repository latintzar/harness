"""The full upstream directory is reference data, separate from executable grants."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from plugin_catalogue import build_catalogue, catalogue, read_plugin, _instruction_evidence

REVISION = 'a' * 40


def fixture():
    files = {
        '.agents/plugins/marketplace.json': json.dumps({'plugins': [
            {'name': 'mail', 'source': {'source': 'local', 'path': './plugins/mail'}},
            {'name': 'external', 'source': {'source': 'url', 'url': 'https://github.com/vendor/plugin.git'}, 'interface': {'displayName': 'External name'}},
        ]}).encode(),
        '.agents/plugins/api_marketplace.json': json.dumps({'plugins': [
            {'name': 'only-api', 'source': {'source': 'local', 'path': './plugins/only-api'}},
        ]}).encode(),
        'plugins/mail/.codex-plugin/plugin.json': json.dumps({'name': 'mail', 'description': 'Original.', 'license': 'Proprietary', 'skills': './skills', 'interface': {'displayName': 'Mail', 'shortDescription': 'Original short.', 'longDescription': 'Original long.', 'defaultPrompt': ['Exact prompt'], 'logo': './assets/mail.svg'}}).encode(),
        'plugins/only-api/.codex-plugin/plugin.json': json.dumps({'name': 'only-api', 'license': 'MIT'}).encode(),
        'plugins/mail/.mcp.json': json.dumps({'mcpServers': {'mail': {'url': 'https://mail.example/mcp', 'oauth': {'client_secret': 'PRIVATE_VALUE', 'client_id': 'PRIVATE_CLIENT'}}}}).encode(),
        'plugins/mail/.app.json': json.dumps({'apps': {'mail': {'id': 'connector_mail'}}}).encode(),
        'plugins/mail/skills/summarize/SKILL.md': b'---\nname: summarize\ndescription: Exact summary.\n---\nNever copy this proprietary executable workflow.',
    }
    tree = [{'path': key, 'type': 'blob'} for key in files]
    tree.append({'path': 'plugins/mail/assets/mail.svg', 'type': 'blob'})
    return files, tree


def test_preserves_every_marketplace_entry_and_exact_metadata_without_credentials_or_workflows():
    files, tree = fixture()
    result = build_catalogue(REVISION, tree, files.__getitem__)
    rows = {row['id']: row for row in result['plugins']}
    assert set(rows) == {'mail', 'external', 'only-api'}
    row = rows['mail']
    assert row['description'] == 'Original short.' and row['long_description'] == 'Original long.'
    assert row['prompts'] == ['Exact prompt'] and row['license'] == 'Proprietary'
    assert not row['supported_api'] and rows['only-api']['supported_api']
    assert row['skills'][0]['description'] == 'Exact summary.'
    assert row['logo_url'] == f'https://raw.githubusercontent.com/openai/plugins/{REVISION}/plugins/mail/assets/mail.svg'
    assert row['apps'] == [{'name': 'mail', 'id': 'connector_mail'}]
    assert row['mcp_servers'][0]['authentication'] == 'oauth'
    assert rows['external']['metadata_status'] == 'external_source'
    serialized = json.dumps(result)
    assert 'PRIVATE_VALUE' not in serialized and 'PRIVATE_CLIENT' not in serialized
    assert 'Never copy this proprietary executable workflow' not in serialized
    assert result == build_catalogue(REVISION, list(reversed(tree)), files.__getitem__)


def test_metadata_load_failure_is_explicit_and_preserves_entry():
    files, tree = fixture()
    del files['plugins/mail/.codex-plugin/plugin.json']
    row = build_catalogue(REVISION, tree, files.__getitem__)['plugins'][0]
    assert row['id'] == 'mail' and row['metadata_status'] == 'unavailable'
    assert not row['mcp_servers'] and row['source_url'].endswith('/plugins/mail')


def test_connection_setup_preserves_evidence_and_omits_all_credential_values():
    files, tree = fixture()
    files['plugins/mail/.mcp.json'] = json.dumps({'mcpServers': {
        'mail': {'url': 'https://mail.example/mcp', 'oauth': {
            'client_id': '<CLIENT_ID>', 'client_secret': '<CLIENT_SECRET>'}},
        'unknown': {'url': 'https://provider.example/mcp'},
        'token': {'url': 'https://api.example/mcp', 'bearer_token_env_var': 'PROVIDER_API_KEY'},
        'local': {'command': 'node', 'env': {'ENABLED_WORKFLOWS': 'read'}},
    }}).encode()
    files['plugins/mail/README.md'] = b'''# Mail
## Setup
Use [provider setup](https://docs.mailprovider.com/oauth/setup).
Never persist this body or https://docs.mailprovider.com/setup?token=PRIVATE_TOKEN.
'''
    tree.append({'path': 'plugins/mail/README.md', 'type': 'blob'})
    result = build_catalogue(REVISION, tree, files.__getitem__)
    row = result['plugins'][0]
    setup = row['connection_setup']
    servers = {server['name']: server for server in row['mcp_servers']}
    assert servers['mail']['auth_kind'] == 'oauth'
    assert servers['mail']['client_registration_required'] is True
    assert servers['unknown']['auth_kind'] == 'unknown'
    assert servers['local']['auth_kind'] == 'none'
    assert servers['token']['auth_kind'] == 'api_key'
    assert setup['app_managed_connections'] is True
    assert setup['package_instructions_present'] is True
    assert 'configure_oauth_client' in setup['setup_steps']
    assert 'discover_provider_authentication' in setup['setup_steps']
    assert setup['documentation'][0]['url'] == 'https://docs.mailprovider.com/oauth/setup'
    assert setup['instructions'][0]['path'] == 'plugins/mail/README.md'
    serialized = json.dumps(result)
    assert 'PRIVATE_TOKEN' not in serialized and 'Never persist this body' not in serialized
    assert '<CLIENT_SECRET>' not in serialized and '<CLIENT_ID>' not in serialized
    external = next(item for item in result['plugins'] if item['id'] == 'external')
    assert external['connection_setup']['status'] == 'external_source'
    assert external['connection_setup']['auth_kinds'] == ['unknown']


def test_setup_links_exclude_examples_assets_credentials_and_evaluation_instructions():
    source = '\n'.join([
        'https://.../api/mcp/asset/',
        'https://example.com/docs/setup',
        'https://docs.example.com/oauth',
        'https://localhost/docs/setup',
        'https://docs.provider.com/setup?token=PRIVATE_TOKEN',
        'https://docs.provider.com/setup#PRIVATE_TOKEN',
        'https://name:PRIVATE_TOKEN@docs.provider.com/setup',
        'https://provider.com/api/mcp/asset/image.png',
        'https://mcp.provider.com/mcp',
        'https://provider.com/api/docs/oauth',
        'https://docs.provider.com/mcp/setup',
    ])
    evidence = _instruction_evidence(source, 'plugins/mail/README.md', REVISION)
    assert {row['url'] for row in evidence['documentation']} == {
        'https://provider.com/api/docs/oauth', 'https://docs.provider.com/mcp/setup'}
    files, tree = fixture()
    tree.extend([{'path': f'plugins/mail/{directory}/README.md', 'type': 'blob'}
                 for directory in ('evaluations', 'evals', 'tests', 'fixtures')])
    row = build_catalogue(REVISION, tree, files.__getitem__)['plugins'][0]
    assert row['connection_setup']['instructions'] == []


@pytest.mark.parametrize('revision', ['main', '../escape', 'https://example.org', 'a' * 39])
def test_refresh_requires_immutable_revision(revision):
    files, tree = fixture()
    with pytest.raises(ValueError):
        build_catalogue(revision, tree, files.__getitem__)


def test_untrusted_manifest_paths_cannot_leave_plugin_root():
    files, tree = fixture()
    manifest = json.loads(files['plugins/mail/.codex-plugin/plugin.json'])
    manifest['interface']['logo'] = '../../other/private.svg'
    manifest['skills'] = '../other/skills'
    manifest['mcpServers'] = {'private': {'http_headers': {'Authorization': 'PRIVATE_VALUE'}}}
    files['plugins/mail/.codex-plugin/plugin.json'] = json.dumps(manifest).encode()
    row = build_catalogue(REVISION, tree, files.__getitem__)['plugins'][0]
    assert row['logo_url'] is None and row['skills'] == []
    assert 'PRIVATE_VALUE' not in json.dumps(row)


def test_published_snapshot_is_complete_and_read_isolation_prevents_shared_mutation():
    result = catalogue()
    assert len(result['plugins']) == 65
    assert sum(row['supported_api'] for row in result['plugins']) == 50
    gmail = read_plugin('gmail')
    assert gmail['description'] == 'Read and manage Gmail'
    assert gmail['long_description'].startswith('Use Gmail to summarize inbox activity')
    assert not gmail['supported_api']
    assert read_plugin('notion')['supported_api']
    assert read_plugin('crowdstrike-falcon-foundry')['metadata_status'] == 'available'
    gmail['description'] = 'Mutated'
    assert read_plugin('gmail')['description'] == 'Read and manage Gmail'
    with pytest.raises(KeyError):
        read_plugin('made-up-provider')
