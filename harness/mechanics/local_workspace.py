"""Company-only tools for a host operator using the local MCP entrypoint."""
from host import Admission, BareWorkspace, Collections


def local_services(state, *, principal_id=None, dataset=None, derivatives_enabled=False):
    from agent_bridge import CodexVideoAgent, workspace_registry
    from public_mcp import HTTPMCPServices
    if derivatives_enabled or dataset not in (None, 'company'):
        raise ValueError('Choose a collection supplied by this workspace.')
    agent = CodexVideoAgent(state, tool_only=True)
    selected = {'collections': Collections()}

    class LocalWorkspace(BareWorkspace):
        def local_authority(self, actor, collection):
            return bool(principal_id and actor == principal_id and collection == 'company')

        def read_admission(self, actor, collection):
            return Admission(lambda: actor == (principal_id or 'local-operator') and collection == 'company')

    agent.vertical = LocalWorkspace(selected)
    agent.tool_registry = workspace_registry(agent)
    return HTTPMCPServices(state, agent=agent)
