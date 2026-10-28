"""Hosted uploads retain immutable package bytes and account selection authority."""
import base64
import io
import json
import zipfile

import pytest

from test_native_packages import package, snapshot, make
from hosted_capabilities import HostedCapabilities


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def public_manifest(package):
    path = package / '.codex-plugin/plugin.json'
    manifest = json.loads(path.read_text())
    manifest['description'] = 'Read public documentation.'
    path.write_text(json.dumps(manifest))


@pytest.mark.anyio
async def test_exact_deterministic_package_and_revocable_selection(tmp_path, package, snapshot):
    public_manifest(package)
    store, _ = make(tmp_path, package, snapshot)
    await store.install('notion', principal_id='alice')
    builder = HostedCapabilities(tmp_path / 'state', store,
        public_read_servers={'notion': 'https://mcp.notion.com/mcp'})
    first = await builder.prepare('alice', None)
    second = await builder.prepare('alice', None)
    assert first.identity == second.identity and first.environment == second.environment
    uploaded = first.environment['plugins'][0]
    assert uploaded['name'] == 'notion' and uploaded['description'] == 'Read public documentation.'
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(uploaded['source']['data']))) as archive:
        for file in package.rglob('*'):
            if file.is_file():
                assert archive.read('notion/' + file.relative_to(package).as_posix()) == file.read_bytes()
        assert archive.getinfo('notion/run.sh').external_attr >> 16 & 0o111
    frozen = builder.freeze({'environment': {'type': 'openai_hosted', **first.environment}})
    assert 'data' not in json.dumps(frozen)
    restored = await builder.restore('alice', first.identity, frozen)
    assert restored.environment == first.environment and restored.current()
    await store.uninstall('notion', principal_id='alice')
    assert not first.current()
    with pytest.raises(ValueError, match='selection'):
        await builder.restore('alice', first.identity, frozen)


@pytest.mark.anyio
async def test_authenticated_or_unknown_mcp_is_explicitly_unready(tmp_path, package, snapshot):
    public_manifest(package)
    store, _ = make(tmp_path, package, snapshot)
    await store.install('notion', principal_id='alice')
    builder = HostedCapabilities(tmp_path / 'state', store)
    ordinary = await builder.prepare('alice', None)
    assert ordinary.environment == {} and ordinary.current()
    assert ordinary.identity['selection']['packages'][0]['id'] == 'notion'
    assert ordinary.identity['readiness'][0]['ready'] is False
    with pytest.raises(ValueError, match='host relay'):
        await builder.prepare('alice', {'native_skills': [{'plugin_id': 'notion'}]})
    assert store.list(principal_id='alice')['plugins'][0]['installed']


@pytest.mark.anyio
async def test_skill_body_is_exact_and_metadata_cannot_break_frontmatter(tmp_path):
    skill = {'id': 'company-skill', 'revision': 2, 'name': 'Name\n---\nunsafe: yes',
             'description': 'A quoted "description"', 'instructions': 'Use the exact workflow.\n\n# Detail\n'}
    builder = HostedCapabilities(tmp_path)
    selection = await builder.prepare('alice', {'skills': [skill]})
    upload = selection.environment['skills'][0]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(upload['source']['data']))) as archive:
        body = archive.read(upload['name'] + '/SKILL.md').decode()
    assert body.endswith(skill['instructions'])
    assert body.split('\n---\n', 1)[1] == '\n' + skill['instructions']
    assert json.loads(body.splitlines()[2].split(': ', 1)[1]) == skill['description']


@pytest.mark.anyio
async def test_cache_is_private_hashed_and_changed_bytes_fail_recovery(tmp_path):
    builder = HostedCapabilities(tmp_path)
    selected = await builder.prepare('alice', {'skills': [{'id': 'skill', 'revision': 1,
        'name': 'Routine', 'description': 'Routine', 'instructions': 'Follow this.'}]})
    frozen = builder.freeze({'environment': {'type': 'openai_hosted', **selected.environment}})
    artifact = next((tmp_path / 'hosted-capabilities').glob('*.zip'))
    assert artifact.stat().st_mode & 0o777 == 0o600
    artifact.write_bytes(b'changed')
    with pytest.raises(ValueError, match='archive'):
        await builder.restore('alice', selected.identity, frozen)


@pytest.mark.anyio
async def test_pending_removal_withdraws_current_and_blocks_new_generation(tmp_path, package, snapshot):
    from plugin_routes import PendingRemovals
    public_manifest(package)
    store, _ = make(tmp_path, package, snapshot)
    await store.install('notion', principal_id='alice')
    builder = HostedCapabilities(tmp_path / 'state', store)
    selected = await builder.prepare('alice', None)
    PendingRemovals(store).begin('alice', 'notion')
    assert not selected.current()
    await store.uninstall('notion', principal_id='alice')
    with pytest.raises(ValueError, match='pending package removal'):
        await builder.prepare('alice', None)


@pytest.mark.anyio
async def test_original_empty_directories_and_archive_quota(tmp_path, package, snapshot, monkeypatch):
    import hosted_capabilities
    public_manifest(package)
    (package / 'empty').mkdir()
    store, _ = make(tmp_path, package, snapshot)
    await store.install('notion', principal_id='alice')
    builder = HostedCapabilities(tmp_path / 'state', store,
        public_read_servers={'notion': 'https://mcp.notion.com/mcp'})
    selected = await builder.prepare('alice', None)
    upload = selected.environment['plugins'][0]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(upload['source']['data']))) as archive:
        assert archive.getinfo('notion/empty/').is_dir()
    monkeypatch.setattr(hosted_capabilities, 'MAX_CACHE_BYTES', 1)
    with pytest.raises(ValueError, match='retained hosted archives'):
        builder.freeze({'environment': selected.environment})
    assert not list(builder.cache.glob('*.zip'))


@pytest.mark.anyio
async def test_native_skill_resolves_exact_installed_metadata_before_upload(tmp_path, package, snapshot):
    from native_skills import NativePluginSkills
    public_manifest(package)
    store, _ = make(tmp_path, package, snapshot)
    await store.install('notion', principal_id='alice')
    resolver = NativePluginSkills(store)
    public = resolver.list('alice')[0]
    selected = {'native_skills': resolver.resolve([public['id']], 'alice')}
    builder = HostedCapabilities(tmp_path / 'state', store,
        public_read_servers={'notion': 'https://mcp.notion.com/mcp'})
    hosted = await builder.prepare('alice', selected)
    assert hosted.commands(selected) == ['notion:notes']
    selected['native_skills'][0]['name'] = 'forged'
    with pytest.raises(ValueError, match='skill selection'):
        await builder.prepare('alice', selected)
