"""Original-source adapter manifests and proposals exposed to the native harness."""

ADAPTER_INSTRUCTIONS = """For a public HTTPS read API, prepare execution.profile=https-python-read-v1.
The entrypoint is an existing Python file in the package and runs with Python 3.12 standard library
inside a separate network-isolated worker. It receives one JSON object on stdin:
{tool,arguments,gateway:{socket,token}}. Send HTTP POST /request through an AF_UNIX connection to
gateway.socket with Authorization: Bearer <gateway.token> and JSON {operation,query}. Operation
IDs match the owner's approved GET/HEAD paths; query contains schema-valid scalar/array values.
The response has status,content_type,body_base64. Check its status and decode its body. Write
one schema-valid JSON object to stdout; diagnostics go to stderr. Actual provider credentials
remain in the host gateway. The worker has 35 seconds and a 512 KiB result ceiling. Source uses
only the standard library; all files are immutable during execution.
Declare every tool's name, concise description, bounded input_schema/output_schema and operation
IDs. Include observational tests covering every tool and its declared operations. Schemas are
expanded JSON Schema with explicit additionalProperties:false for objects; external references
and executable regex patterns are unsupported. Save the package, propose_custom_connector_target
with the exact public HTTPS origin, auth field references and GET/HEAD paths/query schemas, and
direct the owner to Review access in the connector details. Once approved and credentials are
ready, test_custom_connector supplies real host verification. The owner activates the tested
version; read_custom_connector then invokes its active tools. A failed repair keeps the prior
working version available while its owner grant remains current.
For public SFTP recordings, save a setup package with private login fields and use the fixed
propose_storage_connection/get_storage_connection flow. The owner approves the exact folder/key
and starts acquisition in its controls. Private-network SQL, SFTP and local CLIs use their
separately commissioned customer runner; keep their setup/source ready.
"""


def object_schema(properties, required=None):
    return {'type':'object', 'properties':properties,
        'required':list(properties) if required is None else required, 'additionalProperties':False}


def execution_schema():
    return object_schema({
        'profile':{'type':'string', 'enum':['https-python-read-v1']},
        'entrypoint':{'type':'string', 'maxLength':160},
        'tools':{'type':'array', 'minItems':1, 'maxItems':16, 'items':object_schema({
            'name':{'type':'string', 'maxLength':64},
            'description':{'type':'string', 'minLength':1, 'maxLength':1000},
            'input_schema':{'type':'object'}, 'output_schema':{'type':'object'},
            'operations':{'type':'array', 'minItems':1, 'maxItems':16, 'items':{'type':'string'}}})},
        'tests':{'type':'array', 'minItems':1, 'maxItems':32, 'items':object_schema({
            'tool':{'type':'string'}, 'arguments':{'type':'object'}})}})


def target_schema():
    return object_schema({
        'origin':{'type':'string', 'maxLength':2048},
        'authentication':{'type':'object', 'description':'Exactly {kind:none}, {kind:bearer,credential:FIELD}, {kind:header,name:HEADER,credential:FIELD}, or {kind:basic,username_field:FIELD,password_field:FIELD}. References name saved secret fields.'},
        'operations':{'type':'array', 'minItems':1, 'maxItems':32, 'items':object_schema({
            'id':{'type':'string', 'maxLength':64}, 'method':{'type':'string', 'enum':['GET','HEAD']},
            'path':{'type':'string', 'maxLength':1024}, 'query_schema':{'type':'object'}})}})


def tool_specs():
    connector = {'connector_id':{'type':'string', 'maxLength':160}}
    return [
        {'type':'function','name':'get_storage_connection',
         'description':'Read this owner’s actual stored-recording folder approval, acquisition worker readiness, funding and transfer progress. Imported byte receipts and search readiness remain distinct.',
         'inputSchema':object_schema(connector)},
        {'type':'function','name':'propose_storage_connection',
         'description':'Save an immutable public SFTP folder proposal for owner review. Use the administrator-supplied exact OpenSSH host key and declared private login field references. Approval and starting one-time or watched-folder acquisition are separate owner controls.',
         'inputSchema':object_schema({**connector,'expected_revision':{'type':'integer','minimum':1},
             'reason':{'type':'string','minLength':1,'maxLength':1000},'target':object_schema({
                 'host':{'type':'string','maxLength':253},'port':{'type':'integer','minimum':1,'maximum':65535},
                 'root':{'type':'string','maxLength':1024},'host_key':{'type':'string','maxLength':8192},
                 'mode':{'type':'string','enum':['one_time','watch']},
                 'stable_seconds':{'type':'integer','minimum':30,'maximum':86400},
                 'poll_seconds':{'type':'integer','minimum':30,'maximum':86400},
                 'authentication':{'type':'object','description':'Declared field references: username_field plus password_field OR private_key_field and optional passphrase_field.'}})})},
        {'type':'function', 'name':'propose_custom_connector_target',
         'description':'Prepare an exact public HTTPS read-access proposal for the connector owner to review in Customize. This saves the proposal; only the owner can approve its destination, methods and paths. Save the execution manifest first. Credentials are field references.',
         'inputSchema':object_schema({**connector, 'expected_revision':{'type':'integer','minimum':1},
             'target':target_schema(), 'reason':{'type':'string','minLength':1,'maxLength':1000}})},
        {'type':'function', 'name':'test_custom_connector',
         'description':'Run the current connector version’s declared read tests in the commissioned isolated worker after its owner approves the target and supplies required credentials. Returns host verification and cleanup evidence. Activation remains the owner’s separate action.',
         'inputSchema':object_schema({**connector, 'expected_revision':{'type':'integer','minimum':1}})},
        {'type':'function', 'name':'read_custom_connector',
         'description':'Run one declared read tool from an owner-activated custom connector. Use get_custom_connector for its actual active tool schemas. Target, version and credentials are selected by the host.',
         'inputSchema':object_schema({**connector, 'tool':{'type':'string','maxLength':64}, 'arguments':{'type':'object'}})},
    ]
