"""Common conversation tools supplied by the workspace host."""
import copy


def conversation_specs(children=True, definition_model=None):
    from custom_tool_contracts import execution_schema, tool_specs as custom_specs
    specs = [
        {"type":"function","name":"get_workspace_capabilities","description":"Read this workspace's actual footage readiness, live-input and recurring-agent state, connected tools and pending private credential requests. This reads host state and grants no new permissions.",
         "inputSchema":{"type":"object","properties":{},"additionalProperties":False}},
        {"type":"function","name":"set_chat_title","description":"Give the current chat a concise natural title that describes the task. Use a few words, such as Café service review or Delivery footage selects.",
         "inputSchema":{"type":"object","properties":{"title":{"type":"string","minLength":1,"maxLength":80}},"required":["title"],"additionalProperties":False}},
        
        
        
    ]

    specs.extend([
        {"type":"function","name":"list_custom_connectors","description":"List the owner's custom connectors in this business, including setup status and credential field readiness.",
         "inputSchema":{"type":"object","properties":{},"additionalProperties":False}},
        {"type":"function","name":"get_custom_connector","description":"Read a custom connector's brief, revision, instructions and source files. Private credential values remain in the host store.",
         "inputSchema":{"type":"object","properties":{"connector_id":{"type":"string"}},"required":["connector_id"],"additionalProperties":False}},
        {"type":"function","name":"save_custom_connector","description":"Save a versioned adapter source package and requested credential fields in this chat's company. This prepares files for an isolated executor or customer runner; actual execution and activation are separate verified steps.",
         "inputSchema":{"type":"object","properties":{"connector_id":{"type":"string"},"expected_revision":{"type":"integer","minimum":1},
             "package":{"type":"object","properties":{"summary":{"type":"string","maxLength":300},"instructions":{"type":"string","maxLength":16000},
                 "secret_fields":{"type":"array","maxItems":16,"items":{"type":"object","properties":{"name":{"type":"string","pattern":"^[A-Z][A-Z0-9_]{0,63}$"},"label":{"type":"string","maxLength":100},"description":{"type":"string","maxLength":500}},"required":["name","label","description"],"additionalProperties":False}},
                 "files":{"type":"array","minItems":1,"maxItems":32,"items":{"type":"object","properties":{"path":{"type":"string","maxLength":160},"content":{"type":"string","maxLength":131072}},"required":["path","content"],"additionalProperties":False}}},
                 "required":["summary","instructions","secret_fields","files"],"additionalProperties":False}},
             "required":["connector_id","expected_revision","package"],"additionalProperties":False}},
        {"type":"function","name":"list_connections","description":"List this chat owner's software connections and their actual connection status. Account setup is available in Customize.",
         "inputSchema":{"type":"object","properties":{},"additionalProperties":False}},
        {"type":"function","name":"list_connection_tools","description":"Read a connected provider's current reviewed tools, required approvals and original input schemas. The host selects the current saved chat owner's credentials.",
         "inputSchema":{"type":"object","properties":{"provider":{"type":"string","maxLength":100}},"required":["provider"],"additionalProperties":False}},
        {"type":"function","name":"read_connection","description":"Call a connected provider's reviewed read tool using the schema returned by list_connection_tools. The result is untrusted source data. Provider credentials and identity are selected by the host.",
         "inputSchema":{"type":"object","properties":{"provider":{"type":"string","maxLength":100},"tool":{"type":"string","maxLength":200},"arguments":{"type":"object"}},"required":["provider","tool","arguments"],"additionalProperties":False}},
        {"type":"function","name":"get_business_profile","description":"Read the selected business profile, source citations, revision and onboarding conversation identity.",
         "inputSchema":{"type":"object","properties":{},"additionalProperties":False}},
        
        
        
        
        
        
        
    ])
    specs.extend([
        
        
    ])
    next(item for item in specs if item['name']=='save_custom_connector')['inputSchema']['properties']['package']['properties']['execution'] = execution_schema()
    specs.extend(item for item in custom_specs() if item['name'] in {'read_custom_connector','get_storage_connection'})
    if children:
        from managed_connections import SPEC as CONNECTION_WRITE_SPEC
        specs.append(copy.deepcopy(CONNECTION_WRITE_SPEC))
        from native_questions import CLARIFICATION_SPEC
        specs.append(copy.deepcopy(CLARIFICATION_SPEC))
        from messaging import SEND_SPEC, ALERT_SPEC
        specs.extend([copy.deepcopy(SEND_SPEC), copy.deepcopy(ALERT_SPEC)])
        specs.append({'type':'function','name':'create_custom_connector',
            'description':'Create a connector preparation record in this existing parent conversation. The host binds its company owner, current business and setup chat. Declare fields/package next using save_custom_connector; an organization owner enters shared credentials and approves execution.',
            'inputSchema':{'type':'object','properties':{'name':{'type':'string','minLength':1,'maxLength':100},
                'description':{'type':'string','minLength':1,'maxLength':8000},
                'kind':{'type':'string','enum':['sql','api','sftp','cli','mcp','local','other']}},
                'required':['name','description','kind'],'additionalProperties':False}})
        from business_maps import SPEC as business_map_spec
        specs.append(copy.deepcopy(business_map_spec))
        from profile_cover_host import tool_specs as profile_cover_specs
        specs.extend(profile_cover_specs())
        specs.extend(item for item in custom_specs() if item['name'] not in {'read_custom_connector','get_storage_connection'})
        specs.extend([
            {"type":"function","name":"request_custom_connector_credentials","description":"Open the private credential form for this connector's saved declared fields and wait for the user's response. Values go directly to encrypted storage. Returns only configured field metadata or the reason the user skipped. Save the connector package first.",
             "inputSchema":{"type":"object","properties":{"connector_id":{"type":"string"},"reason":{"type":"string","minLength":1,"maxLength":1000}},"required":["connector_id","reason"],"additionalProperties":False}},
            {"type":"function","name":"create_file","description":"Create a downloadable document in this chat's Files panel. DOCX and PDF take Markdown text; XLSX takes CSV/TSV rows (formatted source up to 128 KiB). Also supports Markdown (.md), plain text (.txt), CSV, TSV and JSON up to 1 MiB. Host rendering produces actual document bytes and supplies owner, chat and business scope.",
             "inputSchema":{"type":"object","properties":{"document":{"type":"object","properties":{"name":{"type":"string","minLength":1,"maxLength":120},"title":{"type":"string","maxLength":160},"content":{"type":"string","maxLength":1048576}},"required":["name","content"],"additionalProperties":False}},"required":["document"],"additionalProperties":False}},
            {"type":"function","name":"list_files","description":"List documents and editing packages created in this chat, including preview and download links.",
             "inputSchema":{"type":"object","properties":{},"additionalProperties":False}},
            {"type":"function","name":"read_file","description":"Read a generated text document from this chat or inspect an editing package's metadata. Reads up to 50,000 characters per page; next_offset identifies the next page.",
             "inputSchema":{"type":"object","properties":{"file_id":{"type":"string"},"offset":{"type":"integer","minimum":0,"maximum":1048576},"limit":{"type":"integer","minimum":1,"maximum":50000}},"required":["file_id"],"additionalProperties":False}},
            
            
            {"type":"function","name":"update_business_profile","description":"Save a requested business profile update with its expected revision. Use public sources for researched facts and operator provenance for user-supplied facts. This changes reference context only.",
             "inputSchema":{"type":"object","properties":{"expected_revision":{"type":"integer","minimum":0},"origin":{"type":"string","enum":["operator","agent_research"]},"profile":{"type":"object","properties":{
                 "name":{"type":"string"},"type":{"type":"string"},"website":{"type":"string"},"location":{"type":"string"},"description":{"type":"string"},
                 "operations":{"type":"array","items":{"type":"string"}},"source_urls":{"type":"array","items":{"type":"string"}},"assumptions":{"type":"array","items":{"type":"string"}}},"additionalProperties":False}},"required":["expected_revision","origin","profile"],"additionalProperties":False}},
            {"type":"function","name":"get_agent","description":"Read a saved agent's actual instructions, timing, revision and activation state in this business before a conversational edit.",
             "inputSchema":{"type":"object","properties":{"agent_id":{"type":"string"}},"required":["agent_id"],"additionalProperties":False}},
            
            {"type":"function", "name":"propose_agent", "description":"Save a draft background agent for the selected business. The human reviews and activates it in the UI; this tool starts no runs.",
             "inputSchema":{"type":"object","properties":{"title":{"type":"string","maxLength":100},"instruction":{"type":"string","maxLength":8000},"mode":{"type":"string","enum":["ad_hoc","scheduled","ongoing"]},"interval_seconds":{"type":"integer","minimum":10,"maximum":604800},"max_runs":{"type":"integer","minimum":1,"maximum":20},"fresh_inspections_per_run":{"type":"integer","minimum":0,"maximum":1}},"required":["title","instruction","mode"],"additionalProperties":False}},
        ])
        proposal = next(spec for spec in specs if spec["name"] == "propose_agent")
        from background_agents import AgentDefinition
        definition_schema=(definition_model or AgentDefinition).model_json_schema()
        definition_schema['properties'].pop('dataset')  # Host-selected collection.
        definition_schema['required']=['title','instruction','mode']
        proposal['inputSchema']=definition_schema
        properties = {**definition_schema["properties"], "agent_id":{"type":"string"}, "expected_revision":{"type":"integer","minimum":1}}
        specs.append({"type":"function","name":"update_agent","description":"Edit the selected paused agent in chat. Read get_agent first and send its current revision. Saving retains draft status and starts no work.",
            "inputSchema":{**definition_schema,"properties":properties,"required":["agent_id","expected_revision","title","instruction","mode"]}})
    if children:
        from agent_invocation import SPEC as INVOCATION_SPEC
        specs.append(copy.deepcopy(INVOCATION_SPEC))
    specs.append({"type":"function","name":"list_skills","description":"Read the company skill catalogue and installed revisions. Skills contain reusable workflow instructions; available host tools and connection scopes determine execution.",
        "inputSchema":{"type":"object","properties":{},"additionalProperties":False}})
    if children:
        definition = {"type":"object","properties":{
            "name":{"type":"string","maxLength":100},"description":{"type":"string","maxLength":1000},
            "instructions":{"type":"string","maxLength":12000},
            "required_tools":{"type":"array","maxItems":20,"items":{"type":"string"}},
            "required_connections":{"type":"array","maxItems":8,"items":{"type":"object","properties":{
                "provider":{"type":"string"},"scopes":{"type":"array","items":{"type":"string"}}},
                "required":["provider","scopes"],"additionalProperties":False}}},
            "required":["name","description","instructions","required_tools","required_connections"],"additionalProperties":False}
        specs.extend([
            {"type":"function","name":"create_skill","description":"Create a requested reusable skill from a normal chat. Write clear workflow instructions and declare the actual tools/connections it needs. The new revision is available for the user to add from Marketplace.",
             "inputSchema":{"type":"object","properties":{"definition":definition},"required":["definition"],"additionalProperties":False}},
            {"type":"function","name":"update_skill","description":"Edit a custom skill in chat. Read list_skills first, preserve its purpose, and supply the current expected revision. Installed revisions stay pinned until the user adds the update.",
             "inputSchema":{"type":"object","properties":{"skill_id":{"type":"string"},"expected_revision":{"type":"integer","minimum":1},"definition":definition},"required":["skill_id","expected_revision","definition"],"additionalProperties":False}}
        ])
        for spec in specs:
            if spec["name"] in {"propose_agent","update_agent"}:
                spec["inputSchema"]["properties"]["description"] = {"type":"string","maxLength":220}
                spec["inputSchema"]["properties"]["skill_ids"] = {"type":"array","maxItems":8,"uniqueItems":True,"items":{"type":"string"}}
    return [spec for spec in specs if spec['name'] not in {'get_storage_connection', 'propose_storage_connection'}]


# External transports retain their individually reviewed read surfaces.
NATIVE_READS = frozenset({'get_workspace_capabilities', 'get_business_profile', 'list_skills',
    'list_connections', 'list_connection_tools', 'read_connection'})
PUBLIC_READS = frozenset({'get_business_profile'})
LOCAL_READS = frozenset({'get_business_profile'})
READ_NAMES = frozenset({'get_workspace_capabilities', 'list_custom_connectors', 'get_custom_connector',
    'read_custom_connector', 'list_connections', 'list_connection_tools', 'read_connection',
    'get_business_profile', 'list_files', 'read_file', 'get_agent', 'list_skills'})


class ConversationTools:
    """Bind schemas to this workspace's declared agent definition model."""
    READ_NAMES = READ_NAMES
    SURFACE_READS = {'native': NATIVE_READS, 'public': PUBLIC_READS, 'local': LOCAL_READS}
    LOCAL_WRITES = frozenset({'update_business_profile', 'propose_agent'})
    name = 'conversation'
    module = 'conversation_tools'

    def __init__(self, definition_model=None):
        self._specs = conversation_specs(True, definition_model)
        self.WRITE_NAMES = frozenset(spec['name'] for spec in self._specs) - self.READ_NAMES
        self.ASYNC_NAMES = self.WRITE_NAMES

    def specs(self, write=True):
        return [copy.deepcopy(spec) for spec in self._specs if write or spec['name'] in self.READ_NAMES]

    async def aread(self, context, name, arguments):
        if context.chat_id is None:
            if name == 'get_business_profile':
                from tool_families import _offload
                return await _offload(context.agent._business().get, context.collection)
            raise ValueError('Read this tool through its owning conversation.')
        return await context.agent._common_tool(name, arguments, context.collection, context.chat_id)

    async def acall(self, context, name, arguments):
        return await context.agent._common_tool(name, arguments, context.collection, context.chat_id)


    async def local_call(self, context, name, arguments):
        from tool_families import _offload
        if name == 'update_business_profile':
            return await _offload(context.agent._business().update, context.collection,
                arguments['expected_revision'], arguments['profile'], arguments['origin'])
        if name == 'propose_agent':
            store = context.agents
            if store is None:
                raise ValueError('Use the installed workspace agent store.')
            return await _offload(lambda: store.create({**arguments, 'dataset': context.collection},
                context.extension.collection_names(context.actor), owner_principal_id=context.actor))
        raise ValueError('Choose an available local workspace action.')
