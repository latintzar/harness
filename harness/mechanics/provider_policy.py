"""Host-commissioned MCP authority; upstream packages remain unchanged.

Tool annotations are provider data. They do not grant execution permission.
"""
from __future__ import annotations
import hashlib
import json

POLICY_REVISION='2026-10-05-v1'
UPSTREAM_REVISION='5fd93af4cd0c623e020d0cc7e9ce178b4ac1f70f'
# Reviewed handler behavior at getsentry/toolkit df857f8ee0cf54ed9bf1d44a282ed8d081d3592f.
# Sentry authorizes its MCP via separately consented grantedSkills: wrapper OAuth
# scopes and provider readOnlyHint fields cannot establish read-only authority.
# execute_sentry_tool can invoke changes and deliberately remains approval-only.
SENTRY_READ_TOOLS=frozenset({
    'whoami','find_organizations','find_projects','find_teams','find_releases',
    'get_issue_details','get_issue_tag_values','get_trace_details','get_event_attachment',
    'search_issues','search_issue_events','search_events','search_errors','search_logs',
    'search_traces','search_metrics','search_profiles','search_replays',
    'get_sentry_resource','get_doc','search_docs','search_sentry_tools',
})
SENTRY_SCOPES=('org:read','project:write','team:write','event:write','alerts:write')
CATALOGUE={
    'openai-docs': {
        'name':'OpenAI Docs','endpoint':'https://developers.openai.com/mcp',
        'auth':'anonymous','scope':None,'hosts':frozenset({'developers.openai.com'}),
        'tools':frozenset({'search_openai_docs','list_openai_docs','fetch_openai_doc','list_api_endpoints','get_openapi_spec'}),
        'description':'Search and read public OpenAI documentation.',
        'reference':'https://developers.openai.com/learn/docs-mcp',
        'available':True,'scopes_by_mode':{'read':()},'read_scope_authority':False,
        'token_auth_method':'none','upstream_revision':None,
    },
    'notion': {
        'name':'Notion','endpoint':'https://mcp.notion.com/mcp','auth':'oauth','scope':'default',
        'hosts':frozenset({'mcp.notion.com'}),
        'tools':frozenset({'notion-get-tool-access','notion-search','notion-fetch'}),
        'description':'Work with the Notion content you authorize.',
        'reference':'https://developers.notion.com/guides/mcp/build-mcp-client',
        'available':True,'scopes_by_mode':{'read':('default',),'read_write':('default',)},
        'read_scope_authority':False,'token_auth_method':'none','upstream_revision':UPSTREAM_REVISION,
    },
    'linear': {
        'name':'Linear','endpoint':'https://mcp.linear.app/mcp','auth':'oauth','scope':'read',
        'hosts':frozenset({'mcp.linear.app'}),'tools':frozenset(),
        'description':'Work with the Linear workspace you authorize.',
        'reference':'https://linear.app/docs/mcp',
        'available':True,'scopes_by_mode':{'read':('read',),'read_write':('read','write')},
        'read_scope_authority':True,'token_auth_method':'none','upstream_revision':UPSTREAM_REVISION,
    },
    'sentry': {
        'name':'Sentry','endpoint':'https://mcp.sentry.dev/mcp','auth':'oauth','scope':'org:read',
        'hosts':frozenset({'mcp.sentry.dev'}),'tools':SENTRY_READ_TOOLS,
        'description':'Work with the Sentry projects, issues and events you authorize.',
        'reference':'https://mcp.sentry.dev/','token_endpoint':'https://mcp.sentry.dev/oauth/token',
        'available':True,'scopes_by_mode':{'read':('org:read',),'read_write':SENTRY_SCOPES},
        'read_scope_authority':False,'token_auth_method':'none','upstream_revision':UPSTREAM_REVISION,
        'policy_revision':'2026-10-05-sentry-v1',
    },
    'figma': {
        'name':'Figma','endpoint':'https://mcp.figma.com/mcp','auth':'oauth','scope':'mcp:connect',
        'hosts':frozenset({'mcp.figma.com','api.figma.com','www.figma.com'}),'tools':frozenset(),
        'description':'Connect Figma after provider setup is complete.',
        'reference':'https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/',
        'available':False,'scopes_by_mode':{'read':('mcp:connect',),'read_write':('mcp:connect',)},
        'read_scope_authority':False,'token_auth_method':'client_secret_basic','upstream_revision':UPSTREAM_REVISION,
    },
}
# Google publishes separate first-party MCP services. These policies do not
# change the original OpenAI package's supported_api catalogue field.
_GOOGLE_SCOPE='https://www.googleapis.com/auth/'
for _id, _label, _host, _reads, _read_scopes, _write_scopes in (
    ('google-drive', 'Google Drive', 'drivemcp.googleapis.com',
     {'download_file_content', 'get_file_metadata', 'get_file_permissions',
      'list_recent_files', 'read_file_content', 'search_files'},
     ('drive.readonly',), ('drive.readonly', 'drive.file')),
    ('google-docs', 'Google Docs', 'docsmcp.googleapis.com', {'read_doc'},
     ('documents.readonly',), ('documents',)),
):
    CATALOGUE[_id]={
        'name':_label, 'endpoint':'https://'+_host+'/mcp/v1', 'auth':'oauth', 'scope':None,
        'hosts':frozenset({_host, 'accounts.google.com', 'oauth2.googleapis.com'}),
        'tools':frozenset(_reads), 'description':'Work with the '+_label+' content you authorize.',
        'reference':'https://developers.google.com/workspace/'+('drive' if _id=='google-drive' else 'docs')+'/api/guides/configure-mcp-server',
        'available':False, 'registration':'static', 'native_direct':True,
        'developer':'Google', 'category':'Google Workspace',
        'scopes_by_mode':{'read':tuple(_GOOGLE_SCOPE+x for x in _read_scopes),
                          'read_write':tuple(_GOOGLE_SCOPE+x for x in _write_scopes)},
        'read_scope_authority':False, 'token_auth_method':'client_secret_post',
        'token_endpoint':'https://oauth2.googleapis.com/token',
        'authorization_endpoint':'https://accounts.google.com/o/oauth2/v2/auth',
        'issuer':'https://accounts.google.com', 'upstream_revision':None,
        'policy_revision':'2026-10-05-google-v1',
    }

for _config in CATALOGUE.values():_config.setdefault('policy_revision',POLICY_REVISION)


def selected_scopes(config,mode):
    if not isinstance(mode,str) or mode not in config['scopes_by_mode']:raise ValueError('Choose an available connection access mode.')
    return tuple(config['scopes_by_mode'][mode])


def scopes(value):
    if value is None:return ()
    if not isinstance(value,str) or len(value)>16384:raise ValueError('Use the provider’s granted scopes.')
    result=tuple(sorted(set(value.split())))
    if len(result)>64 or any(len(s)>512 or not all(0x21<=ord(c)<=0x7e and c not in '\\"' for c in s) for s in result):
        raise ValueError('Use the provider’s granted scopes.')
    return result


def provider_read_only(config,row):
    granted=tuple(json.loads(row['oauth_scopes']))
    return bool(config['read_scope_authority'] and granted and set(granted)<=set(config['scopes_by_mode']['read']))


def read_authorized(config,row,name):
    return name in config['tools'] or provider_read_only(config,row)


def visible_tools(config,row,inventory):
    if row['access_mode']=='read_write':return inventory
    return [tool for tool in inventory if read_authorized(config,row,tool['name'])]


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def effective_policies(remote_providers=None):
    """Host configuration projection shared by broker and deployment filtering."""
    from copy import deepcopy
    from catalogue_providers import RemoteProviderCommissioning,pending_policies
    if remote_providers is not None and not isinstance(remote_providers,RemoteProviderCommissioning):
        raise ValueError('Use the company’s commissioned provider configuration.')
    result=pending_policies();result.update(deepcopy(CATALOGUE))
    for descriptor in remote_providers.providers if remote_providers else ():
        result[descriptor.plugin_id]=descriptor.policy()
    return result
