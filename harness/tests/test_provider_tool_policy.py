"""Current provider schema discovery keeps host effect approval hints bounded."""
import pytest

from provider_tool_policy import provider_prompt_policy


def test_provider_effect_hints_keep_original_tool_names_and_read_discovery():
    effects = {'original-search': 'read', 'create_document': 'write', 'new-original-tool': 'unknown'}
    assert provider_prompt_policy(effects) == {
        'default_tools_approval_mode': 'writes', 'tools': {
            'create_document': {'approval_mode': 'prompt'},
            'new-original-tool': {'approval_mode': 'prompt'}}}
    assert effects['original-search'] == 'read'


@pytest.mark.parametrize('value', [None, [], {'': 'read'}, {'x': 'execute'}, {3: 'read'},
    {'x' * 257: 'write'}, {str(index): 'read' for index in range(2049)}])
def test_provider_effect_hints_refuse_unbounded_or_unknown_policy(value):
    with pytest.raises(ValueError):
        provider_prompt_policy(value)
