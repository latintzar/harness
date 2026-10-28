"""Explicit retail declarations for isolated store tests outside host composition."""
def retail_agent_store(state, **options):
    from background_agents import AgentStore
    from pack import load
    return AgentStore(state, agent_classes=load('retail').agent_classes(), **options)


def root_agent(state):
    from agent_bridge import HarnessAgent
    agent = HarnessAgent(state)
    agent.children['chat'] = {'id': 'chat', 'owner_principal_id': 'alice',
        'dataset': 'cafe', 'title': 'Review service', 'messages': [
            {'role': 'user', 'content': 'Review service'},
            {'role': 'assistant', 'content': '', 'results': [], 'tools': []}]}
    agent._save_child(agent.children['chat'])
    agent.capability_snapshot = lambda *args: None
    agent.agent_setup_context = lambda *args: None
    return agent
