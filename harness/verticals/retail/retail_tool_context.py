"""Retail location and source authority supplied to registered tool families."""
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ReadContext:
    """What a family's read receives, the same on every surface."""
    store:object            # the scoped MonitoringStore; `store.state` is the company's state directory
    owner:str               # whose records are read: the acting member, or the principal of an MCP grant
    dataset:str             # the business collection the host fixed for this conversation or grant
    agents:object=None      # the AgentStore, on the surfaces that have one

    @property
    def state(self):return Path(self.store.state)

    def allowed(self,location_id,camera_ids=()):
        """Whether the reader's present access covers a location and, with `camera_ids`, those
        cameras of it. A tracker's definition belongs to its location; its output is the record of
        the cameras it read, so an output is read with its own cameras.

        The server's store carries its own guard. A read over MCP arrives with a fresh store, so the
        scoped view that came with it decides: its current business scope, checked again here.
        """
        cameras=sorted(camera_ids)
        if not self.store.profile_authorized(self.owner,{'dataset':self.dataset,'location_id':location_id,'camera_ids':cameras}):return False
        view=getattr(self.store,'scope_view',None)
        if view is None:return True  # A host's own call with its own store.
        current=getattr(view,'business_scope_current',None)
        if current is not None and not current():return False
        for scope in getattr(view,'business_scopes',None) or []:
            if scope['collection']==self.dataset:
                return not scope['configured'] or scope['can_query'] and (scope['includes_unassigned']
                    or location_id in scope['location_ids'] and set(cameras)<=set(scope.get('camera_ids') or []))
        from retail_business_scope import legacy_scope_current
        return legacy_scope_current(self.state,self.dataset)

    def manages(self,location_id):
        """Whether the reader manages a location in the scope this read arrived with."""
        view=getattr(self.store,'scope_view',None)
        return any(scope['collection']==self.dataset and scope.get('can_manage') and location_id in scope['location_ids']
                   for scope in getattr(view,'business_scopes',None) or [])

    def source(self,source_id):
        """Whether the reader's present access covers a recording."""
        try:return self.store.source_lookup(self.owner,source_id) is not None
        except ValueError:return False

    def require(self,location_id):
        if not isinstance(location_id,str) or not self.allowed(location_id):raise ValueError('Choose a location in your current access.')
        return location_id

@dataclass
class WriteContext(ReadContext):
    """What a family's write receives: the hosted parent conversation and its active run."""
    agent:object=None
    chat_id:str=None
    record:dict=None        # the conversation's saved record
    run:dict=None           # the active run of this turn

    @property
    def organization(self):
        """Whether this is the company's Organization conversation."""
        chats=getattr(self.agent,'organization_chats',None)
        return bool(chats is not None and chats.canonical(self.record) and self.record.get('organization_purpose')=='organization')

    def company(self):
        """The acting member's handle on the company's stores, under the host's current scope."""
        host=getattr(self.agent,'business_scope_host',None)
        if host is None or 'location_intelligence' not in host:raise ValueError('Use the current location workspace.')
        import retail_records as records
        return records.Company(Path(host['state']),self.owner,self.dataset,host['business_scope'],host['monitoring'],host['location_intelligence'])


