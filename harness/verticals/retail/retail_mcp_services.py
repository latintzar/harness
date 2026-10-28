"""Local, read-only retail services without an agent runtime or scheduler."""
from contextlib import contextmanager

from host import Admission
from qdrant_retrieval import QdrantRetrievalIndex


def local_services(state, *, principal_id=None, dataset=None, derivatives_enabled=False, index_factory=None):
    from agent_bridge import CodexVideoAgent
    from background_agents import AgentStore
    from retail_business_scope import legacy_scope_current
    from execution_policy import RETRIEVAL_VIEW
    from imported_media import ImportedMedia
    from monitoring import MonitoringStore
    from media import EvidenceMedia
    from media_exports import ExportStore
    from public_mcp import HTTPMCPServices
    from retail_agent_inputs import RetailInputProvider
    from retail_host import RetailWorkspace
    from source_visibility import ReadyIndex, _report
    from tool_families import build_registry
    import pack

    raw = (index_factory or QdrantRetrievalIndex)(state / 'cosmos')
    raw.imported_media = ImportedMedia(state)
    if derivatives_enabled:
        from source_derivatives import SourceDerivatives
        from source_media import MediaClient
        from workspace_identity import workspace_scope
        raw.source_derivatives = SourceDerivatives(state, company_id=workspace_scope(state),
            media_client=MediaClient(), enabled=True)
    media = EvidenceMedia(state)
    if derivatives_enabled:
        media.derivatives = raw.source_derivatives
    index = ReadyIndex(raw, state)
    agent = CodexVideoAgent(state, index, lambda: index.status().get('videos', []), tool_only=True)
    from retail_business import RetailBusinessStore
    agent.business_store = RetailBusinessStore(state, index)
    agent.monitoring_store = MonitoringStore(state)
    agent.export_store = ExportStore(state, index, media) if principal_id else None
    agent.automation_store = AgentStore(state, input_provider=RetailInputProvider(
        monitoring=agent.monitoring_store, index=index), agent_classes=pack.active().agent_classes()) if principal_id else None

    class LocalRetailWorkspace(RetailWorkspace):
        @staticmethod
        def local_result(name, value):
            return {'videos': value} if name == 'list_cameras' else value

        def local_authority(self, actor, collection):
            return bool(principal_id and actor == principal_id and collection == dataset
                        and legacy_scope_current(state, dataset))

        def collection_names(self, principal=None):
            return index.status()['datasets']

        def local_read_names(self):
            return None if principal_id else frozenset({'list_cameras', 'get_harness'})

        def read_admission(self, actor, collection):
            if actor != (principal_id or 'local-operator') or collection != (dataset or 'company'):
                raise ValueError('Use the host-selected principal and collection.')
            if not legacy_scope_current(state, dataset):
                raise ValueError('Use the authenticated workspace connection for current location assignments.')
            try:
                view = index.snapshot(principal_id, refresh=True)
            except (ValueError, OSError):
                view = index.snapshot(principal_id)
            if dataset:
                positions = [position for position, row in enumerate(view.clips) if row['dataset'] == dataset]
                view.clips = [view.clips[position] for position in positions]
                if hasattr(view, 'embeddings'):
                    view.embeddings = view.embeddings[positions]
                view.report = _report(view.clips, view.report)
            view.business_scope_current = lambda: legacy_scope_current(state, dataset)

            @contextmanager
            def bind():
                token = RETRIEVAL_VIEW.set(view)
                try:
                    with index.bind(view):
                        yield
                finally:
                    RETRIEVAL_VIEW.reset(token)

            return Admission(view.business_scope_current, bind, resource=view)

    agent.vertical = LocalRetailWorkspace(None, {'agent': agent, 'index': index, 'state': state})
    families = pack.active().import_tool_families()
    if principal_id is None:
        from retail_tools import BRIDGE
        families = (BRIDGE,)
    agent.tool_registry = build_registry(agent, families=families)
    return HTTPMCPServices(state, agent=agent)
