"""Pinned upstream plugin reference data, independent from account connections.

Refresh explicitly during development. Serving the catalogue performs no network access,
installation, credential loading or executable plugin/skill evaluation.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import io
import tarfile
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPOSITORY = 'openai/plugins'
SNAPSHOT = Path(__file__).with_name('plugin_catalogue.json')
EXTERNAL_PINS = Path(__file__).with_name('plugin_external_pins.json')
_REVISION = re.compile(r'[0-9a-f]{40}\Z')
_NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}\Z')
_MANIFEST_FIELDS = ('name', 'version', 'description', 'author', 'homepage', 'repository',
                    'license', 'keywords', 'interface', 'skills')
_CREDENTIAL_NAME = re.compile(r'(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|BEARER_TOKEN|CLIENT_SECRET|PASSWORD|SECRET_ACCESS_KEY)', re.I)
_DOCUMENTATION_URL = re.compile(r'https://[^\s<>\[\]`"\)]+')


def _documentation_candidate(value):
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    labels = host.lower().split('.')
    if (not _https(value) or parsed.query or parsed.fragment or len(labels) < 2
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label) for label in labels)
            or any(label in {'example', 'localhost', 'invalid', 'test', 'yourdomain', 'your-domain'} for label in labels)
            or any(char in value for char in ('{', '}', '$', '…'))):
        return False
    # An MCP endpoint or asset URL is a transport reference, not a prose setup guide.
    return bool(any(label in {'docs', 'doc', 'developer', 'developers', 'help', 'support'} for label in labels)
                or re.search(r'(?:^|/)(?:docs?|documentation|guides?|help|support|quickstart|setup|installation)(?:/|$|\.md$)', parsed.path, re.I))


def _source_url(path, revision, repository=REPOSITORY):
    return f'https://github.com/{repository}/blob/{revision}/{path}'


def _instruction_evidence(source, path, revision, repository=REPOSITORY):
    """Retain links and authentication mentions, never an executable instruction body."""
    documentation = []
    for candidate in sorted(set(_DOCUMENTATION_URL.findall(source))):
        candidate = candidate.rstrip('.,;:')
        if _documentation_candidate(candidate):
            documentation.append({'url': candidate, 'source_url': _source_url(path, revision, repository)})
    mentions = []
    if re.search(r'\boauth\b', source, re.I):
        mentions.append('oauth')
    if re.search(r'\bapi[ _-]?keys?\b', source, re.I):
        mentions.append('api_key')
    return {'authentication_mentions': mentions, 'documentation': documentation}


def _mcp_auth(details):
    """Describe declared transport setup, without storing credential values."""
    oauth = details.get('oauth') or {}
    environment_names = set(_strings(details.get('env_vars')))
    if isinstance(details.get('env'), dict):
        environment_names.update(details['env'])
    if isinstance(details.get('bearer_token_env_var'), str):
        environment_names.add(details['bearer_token_env_var'])
    secret_names = sorted(name for name in environment_names if _CREDENTIAL_NAME.search(name))
    has_headers = bool(details.get('http_headers') or details.get('env_http_headers'))
    if oauth or details.get('oauth_resource') or details.get('scopes'):
        kind, evidence = 'oauth', 'mcp_configuration'
    elif secret_names:
        kind, evidence = 'api_key', 'credential_environment_names'
    elif details.get('bearer_token_env_var') or has_headers:
        kind, evidence = 'unknown', 'provider_headers'
    elif details.get('url'):
        kind, evidence = 'unknown', 'provider_discovery_required'
    else:
        kind, evidence = 'none', 'no_transport_auth_declared'
    oauth_fields = [key for key in ('client_id', 'client_secret')
                    if isinstance(oauth, dict) and oauth.get(key)]
    placeholders = [key for key in oauth_fields if re.search(
        r'<[^>]+>|\$\{[^}]+\}', str(oauth[key]))]
    return {'auth_kind': kind, 'auth_evidence': evidence,
            'credential_environment_names': secret_names,
            'oauth_client_fields': oauth_fields,
            'oauth_client_configuration': 'placeholders' if placeholders else (
                'publisher_declared' if oauth_fields else 'discovery' if kind == 'oauth' else None),
            'client_registration_required': True if placeholders else None}


def _connection_setup(row, root, paths, revision, fetch, repository=REPOSITORY):
    status = row['metadata_status']
    setup = {'status': status, 'auth_kinds': [], 'app_managed_connections': bool(row['apps']),
             'package_instructions_present': False, 'instructions': [],
             'documentation': [], 'instruction_authentication_mentions': [], 'setup_steps': []}
    if status != 'available':
        setup.update(auth_kinds=['unknown'], setup_steps=['inspect_published_package'])
        return setup
    # Link the actual package's prose; copy no README, skill body or credential content.
    instruction_paths = sorted(path for path in paths if path.startswith(root + '/' if root else '')
        and PurePosixPath(path).suffix.lower() == '.md'
        and not any(part in {'tests', 'test', 'fixtures', 'evals', 'evaluations', '.git'} for part in PurePosixPath(path).parts)
        and (PurePosixPath(path).name.lower() in {'readme.md', 'install.md', 'setup.md', 'authentication.md'}
             or re.search(r'(?:^|/|[-_])(oauth|auth|setup|install)(?:/|[-_.])', path[len(root) + 1:] if root else path, re.I)))
    for path in instruction_paths:
        setup['instructions'].append({'path': path, 'source_url': _source_url(path, revision, repository)})
        # Root README is the package's setup entry point. Nested guides remain links.
        if path != _child(root, 'README.md'):
            continue
        source = fetch(path).decode('utf-8')
        evidence = _instruction_evidence(source, path, revision, repository)
        setup['documentation'].extend(evidence['documentation'])
        for kind in evidence['authentication_mentions']:
            setup['instruction_authentication_mentions'].append({'kind': kind, 'source_url': _source_url(path, revision, repository)})
    for skill in row['skills']:
        evidence = skill.get('setup_evidence') or {}
        setup['documentation'].extend(evidence.get('documentation', []))
        for kind in evidence.get('authentication_mentions', []):
            setup['instruction_authentication_mentions'].append({'kind': kind, 'source_url': skill['source_url']})
    # Different skills can point at the same setup guide. Keep the first provenance link.
    setup['documentation'] = list({entry['url']: entry for entry in reversed(setup['documentation'])}.values())[::-1]
    setup['package_instructions_present'] = bool(instruction_paths or row['skills'])
    kinds = {server['auth_kind'] for server in row['mcp_servers']}
    if row['apps']:
        kinds.add('app_managed')
    if not kinds:
        # Skills may use authenticated CLIs/APIs. Their mention evidence is kept separate.
        kinds.add('unknown' if setup['instruction_authentication_mentions'] else 'none')
    setup['auth_kinds'] = sorted(kinds)
    steps = ['load_package']
    if row['apps']:
        steps.append('select_app_or_public_mcp_connection')
    if any(server['client_registration_required'] for server in row['mcp_servers']):
        steps.append('configure_oauth_client')
    if 'oauth' in kinds:
        steps.append('authorize_customer_account')
    if 'api_key' in kinds:
        steps.append('supply_customer_credential')
    if 'unknown' in kinds:
        steps.append('discover_provider_authentication')
    if row['mcp_servers'] or row['apps']:
        steps.append('verify_tools_with_customer_account')
    setup['setup_steps'] = steps
    return setup


def _https(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
        return value
    return None


def _child(root, value):
    """Resolve a published relative path inside its own plugin package."""
    if not isinstance(value, str) or '\\' in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts:
        return None
    return str(PurePosixPath(root) / path)


def _strings(value):
    if isinstance(value, str):
        return [value]
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _skill(path, revision, fetch, repository=REPOSITORY):
    row = {'name': PurePosixPath(path).parent.name, 'description': '', 'path': path,
           'source_url': f'https://github.com/{repository}/blob/{revision}/{path}'}
    try:
        # YAML is used only by the explicit refresh command, never when serving a snapshot.
        import yaml
        source = fetch(path).decode('utf-8')
        row['setup_evidence'] = _instruction_evidence(source, path, revision, repository)
        if source.startswith('---\n'):
            frontmatter = source.split('\n---', 1)[0][4:]
            header = yaml.safe_load(frontmatter)
            if isinstance(header, dict):
                for key in ('name', 'description'):
                    if isinstance(header.get(key), str):
                        row[key] = header[key]
        row['metadata_status'] = 'available'
    except (ValueError, OSError, KeyError, ImportError, UnicodeError):
        row['metadata_status'] = 'unavailable'
    return row


def _populate_entry(row, root, paths, revision, fetch, repository=REPOSITORY):
    identifier = row['id']
    try:
        manifest = json.loads(fetch(_child(root, '.codex-plugin/plugin.json')))
    except (ValueError, OSError, KeyError, UnicodeError):
        row['connection_setup'] = _connection_setup(row, root, paths, revision, fetch, repository)
        return row
    interface = manifest.get('interface') or {}
    author = manifest.get('author') or {}
    row.update(name=interface.get('displayName') or manifest.get('name') or identifier,
               description=interface.get('shortDescription') or manifest.get('description') or '',
               long_description=interface.get('longDescription') or manifest.get('description') or '',
               developer=interface.get('developerName') or (author.get('name') if isinstance(author, dict) else author),
               category=interface.get('category') or row['category'],
               brand_color=interface.get('brandColor'), prompts=_strings(interface.get('defaultPrompt')),
               capabilities=_strings(interface.get('capabilities')),
               website_url=_https(interface.get('websiteURL') or manifest.get('homepage')),
               privacy_policy_url=_https(interface.get('privacyPolicyURL')),
               terms_url=_https(interface.get('termsOfServiceURL')),
               license=manifest.get('license'), version=manifest.get('version'),
               metadata_status='available',
               manifest={key: deepcopy(manifest[key]) for key in _MANIFEST_FIELDS if key in manifest})
    # A future inline MCP/app config can carry credentials. Preserve file references only.
    for field in ('apps', 'mcpServers'):
        if isinstance(manifest.get(field), str):
            row['manifest'][field] = manifest[field]
    # Original logo assets stay at their immutable upstream location.
    for logo in (interface.get('logo'), interface.get('composerIcon')):
        image_path = _child(root, logo)
        if image_path in paths:
            row['logo_url'] = f'https://raw.githubusercontent.com/{repository}/{revision}/{image_path}'
            break
        if _https(logo):
            row['logo_url'] = _https(logo)
            break
    skill_roots = [_child(root, value) for value in _strings(manifest.get('skills', './skills'))]
    skill_paths = sorted(path for path in paths if path.endswith('/SKILL.md') and any(
        prefix and (path == prefix or path.startswith(prefix.rstrip('/') + '/')) for prefix in skill_roots))
    row['skills'] = [_skill(path, revision, fetch, repository) for path in skill_paths]
    for filename, field in (('.app.json', 'apps'), ('.mcp.json', 'mcpServers')):
        path = _child(root, filename)
        if path not in paths:
            continue
        try:
            config = json.loads(fetch(path)).get(field, {})
        except (ValueError, OSError, KeyError, UnicodeError):
            continue
        for name, details in sorted(config.items()):
            if not isinstance(details, dict):
                continue
            if field == 'apps':
                row['apps'].append({'name': name, 'id': details.get('id')})
            else:
                oauth = details.get('oauth')
                auth = 'oauth' if oauth or details.get('oauth_resource') or details.get('scopes') else (
                    'environment' if details.get('env') or details.get('env_vars') or details.get('bearer_token_env_var') or details.get('env_http_headers') else 'provider')
                row['mcp_servers'].append({'name': name, 'url': _https(details.get('url')),
                    'transport': details.get('type') or ('http' if details.get('url') else 'stdio'),
                    'authentication': auth, 'scopes': _strings(details.get('scopes')),
                    'oauth_client_required': bool(isinstance(oauth, dict) and (oauth.get('client_id') or oauth.get('client_secret'))),
                    'source_url': _source_url(path, revision, repository), **_mcp_auth(details)})
    row['connection_setup'] = _connection_setup(row, root, paths, revision, fetch, repository)
    return row


def build_catalogue(revision, tree, fetch):
    """Build deterministically from a fixed Git tree and a public-file reader.

    `fetch(path)` returns bytes from openai/plugins at precisely `revision`. Raw MCP
    auth configuration and skill bodies are discarded; only reference metadata survives.
    """
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ValueError('Select a full immutable upstream Git revision')
    paths = {entry['path'] for entry in tree if entry.get('type') == 'blob'}
    normal = json.loads(fetch('.agents/plugins/marketplace.json'))
    api = json.loads(fetch('.agents/plugins/api_marketplace.json'))
    api_names = {row['name'] for row in api['plugins']}
    default_names = {row['name'] for row in normal['plugins']}
    entries = {row['name']: row for row in normal['plugins']}
    for row in api['plugins']:
        entries.setdefault(row['name'], row)
    base = f'https://github.com/{REPOSITORY}/tree/{revision}'

    def read_entry(entry):
        identifier = entry['name']
        if not isinstance(identifier, str) or not _NAME.fullmatch(identifier):
            raise ValueError('Upstream plugin identifier requires review')
        source = entry.get('source', {})
        root = _child('', source.get('path', f'plugins/{identifier}'))
        external = source.get('source') in {'url', 'git-subdir'}
        source_url = _https(source.get('url')) if external else f'{base}/{root}' if root else base
        interface = entry.get('interface', {})
        row = {'id': identifier, 'name': interface.get('displayName') or identifier,
               'description': '', 'long_description': '', 'developer': None,
               'category': entry.get('category', ''), 'logo_url': None, 'brand_color': None,
               'prompts': [], 'capabilities': [], 'website_url': None,
               'privacy_policy_url': None, 'terms_url': None, 'license': None,
               'version': None, 'supported_api': identifier in api_names,
               'in_default_marketplace': identifier in default_names, 'source_url': source_url,
               'metadata_status': 'external_source' if external else 'unavailable',
               'skills': [], 'mcp_servers': [], 'apps': [], 'manifest': {},
               'catalogue_entry': deepcopy(entry)}
        if external or not root:
            row['connection_setup'] = _connection_setup(row, root, paths, revision, fetch)
            return row
        return _populate_entry(row, root, paths, revision, fetch)

    with ThreadPoolExecutor(max_workers=6) as pool:
        plugins = list(pool.map(read_entry, entries.values()))
    return {'schema': 1, 'repository': REPOSITORY, 'revision': revision, 'source_url': base,
            'default_marketplace_count': len(default_names), 'api_marketplace_count': len(api_names),
            'plugins': plugins}


def materialize_external(snapshot, pins, fetch_archive):
    """Resolve reviewed public package pins during refresh, never during serving.

    Catalogue source declarations remain verbatim. The separate resolved_source is
    accepted only when it adds an immutable SHA to that exact declared origin.
    Public archives are inspected as data; package scripts and skill bodies never run.
    """
    from native_install_source import source_spec
    result = deepcopy(snapshot)
    rows = {row['id']: row for row in result['plugins']}
    seen = set()
    for pin in pins:
        identifier = pin.get('id')
        if identifier in seen or identifier not in rows:
            raise ValueError('External pin must identify one published package')
        seen.add(identifier)
        row = rows[identifier]
        original = row['catalogue_entry']['source']
        if original.get('source') not in {'url', 'git-subdir'}:
            raise ValueError('External pins apply to published external sources')
        row['resolved_source'] = {**original, 'sha': pin.get('revision')}
        spec = source_spec(row, result)
        if spec.repository != pin.get('repository') or spec.subtree != pin.get('subtree'):
            raise ValueError('External pin differs from the published repository or package')
        if (type(pin.get('archive_bytes')) is not int or not 0 < pin['archive_bytes'] <= 32 * 1024**2
                or not re.fullmatch(r'[a-f0-9]{64}', pin.get('archive_sha256', ''))):
            raise ValueError('External pin needs bounded immutable archive evidence')
        content = fetch_archive(deepcopy(pin))
        if (not isinstance(content, bytes) or len(content) != pin['archive_bytes']
                or hashlib.sha256(content).hexdigest() != pin['archive_sha256']):
            raise ValueError('External archive differs from reviewed bytes')
        with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as archive:
            members = {}; total = 0; count = 0
            for member in archive:
                count += 1; total += member.size
                if count > 100_000 or total > 512 * 1024**2 or member.size < 0:
                    raise ValueError('External archive exceeds inspection bounds')
                name = member.name.rstrip('/') if member.isdir() else member.name
                parts = name.split('/')
                if (len(parts) > 24 or any(part in {'', '.', '..'} or '\\' in part or '\0' in part for part in parts)
                        or parts[0] != spec.archive_root or name in members
                        or not (member.isfile() or member.isdir())):
                    raise ValueError('External archive needs ordinary unambiguous contained paths')
                members[name] = member
            paths = {name[len(spec.archive_root) + 1:] for name, member in members.items() if member.isfile()}
            def fetch(path, limit=2_000_000):
                member = members[spec.archive_root + '/' + path]
                if not member.isfile() or member.size > limit:
                    raise ValueError('External metadata exceeds inspection bounds')
                data = archive.extractfile(member).read(limit + 1)
                if len(data) != member.size:
                    raise ValueError('External metadata is incomplete')
                return data
            manifest_path = _child(spec.subtree, '.codex-plugin/plugin.json')
            manifest = json.loads(fetch(manifest_path))
            if manifest.get('name') != identifier or not isinstance(manifest.get('version'), str):
                raise ValueError('External manifest identifies a different package')
            # Clear any previous metadata so a refresh cannot retain removed skills or servers.
            row.update(skills=[], apps=[], mcp_servers=[], metadata_status='external_source')
            row['source_url'] = f'https://github.com/{spec.repository}/tree/{spec.revision}' + (
                '/' + spec.subtree if spec.subtree else '')
            _populate_entry(row, spec.subtree, paths, spec.revision, fetch, spec.repository)
            if row['metadata_status'] != 'available' or any(s['metadata_status'] != 'available' for s in row['skills']):
                raise ValueError('External package requires complete original metadata')
            license_path = pin.get('license_path')
            if _child('', license_path) != license_path or not license_path:
                raise ValueError('External package needs a contained original license notice')
            license_bytes = fetch(license_path, 64 * 1024)
            # Preserve the original notice beside metadata, including when the package
            # publisher keeps LICENSE above its declared subdirectory (Qodo).
            row['source_provenance'] = {
                'repository': spec.repository, 'revision': spec.revision, 'subtree': spec.subtree,
                'archive_url': spec.url, 'archive_sha256': pin['archive_sha256'],
                'archive_bytes': pin['archive_bytes'],
                'manifest_sha256': hashlib.sha256(fetch(manifest_path)).hexdigest(),
                'license_notice': {'path': license_path,
                    'source_url': _source_url(license_path, spec.revision, spec.repository),
                    'sha256': hashlib.sha256(license_bytes).hexdigest(),
                    'text': license_bytes.decode('utf-8')},
            }
    return result


@lru_cache(maxsize=1)
def _snapshot():
    data = json.loads(SNAPSHOT.read_text())
    if data.get('schema') != 1 or data.get('repository') != REPOSITORY or not _REVISION.fullmatch(data.get('revision', '')):
        raise ValueError('Published plugin catalogue needs a valid upstream snapshot')
    return data


def catalogue():
    """Return public reference data; connections and installation grants are separate."""
    return deepcopy(_snapshot())


def read_plugin(identifier):
    for entry in _snapshot()['plugins']:
        if entry['id'] == identifier:
            return deepcopy(entry)
    raise KeyError(identifier)


def refresh(revision, output=SNAPSHOT, external_pins=EXTERNAL_PINS):
    if not _REVISION.fullmatch(revision):
        raise ValueError('Select a full immutable upstream Git revision')

    def get(url, limit=2_000_000):
        request = Request(url, headers={'User-Agent': 'Rocoto-plugin-catalogue/1.0'})
        with urlopen(request, timeout=25) as response:
            content = response.read(limit + 1)
        if len(content) > limit:
            raise ValueError('Upstream metadata exceeds the reviewed size limit')
        return content

    tree = json.loads(get(f'https://api.github.com/repos/{REPOSITORY}/git/trees/{revision}?recursive=1', 8_000_000))
    if tree.get('truncated') or tree.get('sha') != revision:
        raise ValueError('Upstream tree must be complete and match the selected revision')
    result = build_catalogue(revision, tree['tree'], lambda path: get(
        f'https://raw.githubusercontent.com/{REPOSITORY}/{revision}/{path}'))
    pins = json.loads(Path(external_pins).read_text())
    result = materialize_external(result, pins['packages'], lambda pin: get(
        f"https://codeload.github.com/{pin['repository']}/tar.gz/{pin['revision']}", 32 * 1024**2))
    # A failed upstream fetch must never quietly replace a complete release snapshot.
    failures = [row['id'] for row in result['plugins'] if row['metadata_status'] == 'unavailable'
                or any(skill['metadata_status'] != 'available' for skill in row['skills'])]
    if failures:
        raise ValueError('Refresh needs complete upstream metadata: ' + ', '.join(failures))
    content = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    output = Path(output)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                prefix=f'.{output.name}.', suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.chmod(output.stat().st_mode & 0o777 if output.exists() else 0o644)
        temporary.replace(output)
        _snapshot.cache_clear()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Saved {len(result['plugins'])} plugins at {revision}; sha256={hashlib.sha256(content.encode()).hexdigest()}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', type=Path, default=SNAPSHOT)
    parser.add_argument('--external-pins', type=Path, default=EXTERNAL_PINS)
    args = parser.parse_args()
    refresh(args.revision, args.output, args.external_pins)
