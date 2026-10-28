"""Explicit retail tool services for transport tests, without a running host."""
from contextlib import contextmanager

from host import Admission


def retail_agent(agent):
    from monitoring import MonitoringStore
    from retail_host import RetailWorkspace
    from source_visibility import ReadyIndex, _report
    from execution_policy import RETRIEVAL_VIEW

    if not isinstance(agent.retrieval, ReadyIndex):
        agent.retrieval = ReadyIndex(agent.retrieval, agent.state)
    if getattr(agent, 'monitoring_store', None) is None:
        agent.monitoring_store = MonitoringStore(agent.state)

    class ReaderWorkspace(RetailWorkspace):
        def read_admission(self, actor, collection):
            index = agent.retrieval
            try:
                view = index.snapshot(actor, refresh=True)
            except (ValueError, OSError):
                view = index.snapshot(actor)
            positions = [position for position, row in enumerate(view.clips) if row['dataset'] == collection]
            view.clips = [view.clips[position] for position in positions]
            if hasattr(view, 'embeddings'):
                view.embeddings = view.embeddings[positions]
            view.report = _report(view.clips, view.report)
            current = getattr(view, 'business_scope_current', lambda: True)

            @contextmanager
            def bind():
                token = RETRIEVAL_VIEW.set(view)
                try:
                    with index.bind(view):
                        yield
                finally:
                    RETRIEVAL_VIEW.reset(token)

            return Admission(current, bind, resource=view)

        def collection_names(self, principal=None):
            return sorted({row['dataset'] for row in agent.retrieval.clips} | {'company'})

    agent.vertical = ReaderWorkspace(None, {'agent': agent, 'index': agent.retrieval, 'state': agent.state})
    return agent


def retail_services(state, index):
    from agent_bridge import CodexVideoAgent
    from public_mcp import HTTPMCPServices
    return HTTPMCPServices(state, agent=retail_agent(CodexVideoAgent(state, index, tool_only=True)))
