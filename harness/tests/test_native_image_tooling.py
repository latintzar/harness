"""Image assembly and executable tooling contract; no Docker/provider calls."""
import hashlib
import importlib.util
from pathlib import Path
import shutil

import pytest

DEPLOY = Path(__file__).parents[1] / 'deploy/native-executor'


def module(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f'{name}.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def inputs(tmp_path, monkeypatch, build):
    result = {}
    for name, constant in [('codex', 'CODEX_SHA256'), ('code_mode_host', 'CODE_MODE_SHA256'), ('ripgrep', 'RIPGREP_SHA256')]:
        path = tmp_path / name
        path.write_bytes(name.encode())
        monkeypatch.setattr(build, constant, hashlib.sha256(path.read_bytes()).hexdigest())
        result[name] = path
    return result


def test_context_contains_pinned_binary_notices_and_all_source_hashes(tmp_path, monkeypatch):
    build = module('build_image')
    source = inputs(tmp_path, monkeypatch, build)
    context = tmp_path / 'context'; context.mkdir()
    hashes = build.prepare_context(context, **source)
    assert (context / 'rg').read_bytes() == b'ripgrep'
    assert hashes['rg'] == build.RIPGREP_SHA256
    assert 'tooling_probe.py' in hashes
    for name in ('COPYING', 'LICENSE-MIT', 'UNLICENSE', 'PCRE2-LICENCE.md', 'provenance.json'):
        assert f'licenses/ripgrep/{name}' in hashes
    assert hashes == {str(p.relative_to(context)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in context.rglob('*') if p.is_file()}


@pytest.mark.parametrize('name', ['codex', 'code_mode_host', 'ripgrep'])
def test_context_rejects_changed_binary(tmp_path, monkeypatch, name):
    build = module('build_image'); source = inputs(tmp_path, monkeypatch, build)
    source[name].write_bytes(b'changed')
    context = tmp_path / 'context'; context.mkdir()
    with pytest.raises(ValueError, match='binary'):
        build.prepare_context(context, **source)


def test_context_checks_copied_bytes_before_any_build(tmp_path, monkeypatch):
    build = module('build_image'); source = inputs(tmp_path, monkeypatch, build)
    original = build.shutil.copyfile
    def changed(source, target):
        result = original(source, target)
        if Path(target).name == 'rg': Path(target).write_bytes(b'swapped during copy')
        return result
    monkeypatch.setattr(build.shutil, 'copyfile', changed)
    context = tmp_path / 'context'; context.mkdir()
    with pytest.raises(ValueError, match='binary'):
        build.prepare_context(context, **source)


def test_tooling_probe_rejects_missing_or_changed_executable(tmp_path):
    probe = module('tooling_probe')
    with pytest.raises((OSError, ValueError)):
        probe.run(tmp_path / 'missing')
    binary = tmp_path / 'rg'; binary.write_text('unreviewed executable')
    with pytest.raises(ValueError, match='hash'):
        probe.run(binary)


def test_actual_bundled_ripgrep_file_content_pcre_and_exit_semantics():
    probe = module('tooling_probe')
    executable = shutil.which('rg')
    if not executable or hashlib.sha256(Path(executable).read_bytes()).hexdigest() != probe.RIPGREP_SHA256:
        pytest.skip('Actual pinned rg is verified by the separate image proof.')
    result = probe.run(executable)
    assert result['status'] == 'passed'
    assert result['file_listing'] == ['nested/other.txt', 'report with spaces.txt']
    assert result['content_search'] == ['report with spaces.txt:2:needle value']
    assert result['pcre2_search'] == 'needle'
    assert result['no_match_exit'] == 1
