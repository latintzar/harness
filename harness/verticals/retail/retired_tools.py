"""Retired archive workflow entrypoints, shared by every advertised MCP boundary.

Implementations and saved evidence remain available to internal readers. These
names cannot start the retired retrieval/inspection workflow from an agent.
"""
RETIRED_TOOLS = frozenset({
    'plan_investigation', 'retrieve_investigation', 'search_video', 'inspect_clip',
    'cosmos_inspect', 'list_intervals', 'related_clips', 'discover_patterns', 'focus_clip',
})
RETIREMENT_MESSAGE = 'Use list_tracker_events and get_tracker_event for saved observations and source evidence.'


def require_active_tool(name):
    if name in RETIRED_TOOLS:
        raise ValueError(RETIREMENT_MESSAGE)


def active_specs(specs):
    return [spec for spec in specs if spec['name'] not in RETIRED_TOOLS]


ARCHIVE_AGENT_REASON = 'archive_camera_footage_workflow_retired'


def is_retired_archive_agent(definition):
    """Saved camera_footage scheduler definitions use the retired archive workflow.

    Organization and Cameras setup chats are outside this scheduler. Their host
    source acquisition and tracker_events subscriptions retain their normal paths.
    """
    return definition.get('input_kind', 'camera_footage') == 'camera_footage'
