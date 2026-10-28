"""Host-owned connection and execution readiness shared by all verticals."""
from __future__ import annotations

class WorkspaceCapabilities:
    def __init__(self, *, connections=None, custom_connectors=None, custom_execution=None, inputs=None, contribution=None, builtins=None):
        self.connections=connections
        self.custom_connectors=custom_connectors
        self.custom_execution=custom_execution
        self.inputs=inputs
        self.contribution=contribution
        self.builtins=builtins

    @staticmethod
    def live_input_ready(collection):
        return False

    def execution(self, definition):
        return self.inputs().execution(definition)

    def snapshot(self, collection, *, principal=None, chat_id=None, read_only=False, child=False, actor=None):
        recurring=self.inputs().recurring_ready(collection, actor or principal)
        connections = []
        custom = []
        pending = []
        storage = []
        if principal and self.connections is not None:
            connections = [{k: row[k] for k in ('id', 'name', 'status', 'read_only', 'available_tools')}
                           for row in self.connections.catalogue(principal)['connections']]
        if principal and self.custom_connectors is not None:
            custom = [{'id': row['id'], 'name': row['name'], 'status': row['status'],
                       'revision': row['revision'], 'credentials_ready': all(f['configured'] for f in row['secret_fields']),
                       'active_status':row.get('active_status','unconfigured'),
                       'active_revision':row.get('active_revision'),
                       'runtime_available':bool(self.custom_execution and self.custom_execution.runtime),
                       'execution': 'active_read_tools' if row.get('active_status')=='active' and self.custom_execution and self.custom_execution.runtime
                            else 'prepared_package' if row['status'] == 'prepared' else 'setup_draft'}
                      for row in self.custom_connectors.list(owner=principal, collection=collection)]
            if chat_id and not child:
                pending = [{'id': row['id'], 'connector_id': row['connector_id'], 'status': row['status']}
                           for row in self.custom_connectors.credential_requests(owner=principal, chat_id=chat_id)
                           if row['status'] == 'pending']
        result={'schema_version':1,'collection':collection,
            'recurring_agents':{'configurable':not read_only and not child,'ready':recurring,
                'notice':None if recurring else 'Connect the input required by this agent.'},
            'permissions':{'read_only':read_only,'parent_controlled':child,
                'configure_connections':not read_only and not child,'submit_credentials':not read_only and not child},
            'connections':connections,'custom_connectors':custom,'pending_credential_requests':pending}
        if self.builtins is not None:
            result['builtins'] = self.builtins.status()
        if self.contribution:
            result.update(self.contribution(collection, principal=principal, chat_id=chat_id,
                read_only=read_only, child=child, actor=actor, custom=custom))
        return result
