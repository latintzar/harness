"""Host-reviewed approval hints for original provider tool schemas."""

def provider_prompt_policy(tool_effects):
    """Host effect classification tunes provider prompts without rewriting schemas.

    The transport still enforces connector access and consumes approved intents.
    Unknown effects use an explicit prompt, preserving original provider discovery.
    """
    if not isinstance(tool_effects,dict) or len(tool_effects)>2048:
        raise ValueError('Use a bounded provider catalogue.')
    tools={}
    for name,effect in tool_effects.items():
        if not isinstance(name,str) or not 1<=len(name)<=256 or effect not in {'read','write','unknown'}:
            raise ValueError('Use the host-reviewed provider effects.')
        if effect!='read':tools[name]={'approval_mode':'prompt'}
    return {'default_tools_approval_mode':'writes','tools':tools}
