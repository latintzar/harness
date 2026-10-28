"""Retail tool specifications and dispatch, installed by the selected vertical."""
import asyncio
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from inspection_previews import InspectionPreviews, PreviewError, tool_previews
from retired_tools import active_specs, require_active_tool

FRAME_OUTPUT_RENDER_JS = 'for (const line of r.split("\\n")) { if (line.startsWith("data:image/")) image(line); else if (line.trim()) text(line); }'


def bridge_specs(write=True):
    values = [
        {"type": "function", "name": "list_cameras", "description": "List available source videos and their provenance.",
                 "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"type": "function", "name": "search_video", "description": "Find source intervals using visual embeddings. Results are candidates to inspect.",
                 "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 8}, "dataset": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
        {"type": "function", "name": "inspect_clip", "description": "View representative source snapshots, their timestamps and source metadata from a retrieved clip. Use cosmos_inspect for temporal video understanding. Use the clip's evidence ID. Set include_observations=true only when comparing with saved model observations; the default supports an independent reading of source evidence. In native code mode the result is a STRING, with metadata JSON on the first line and image data URLs on subsequent lines. Render it exactly as: const r = await tools.inspect_clip({clip_id: YOUR_CLIP_ID}); " + FRAME_OUTPUT_RENDER_JS + " This supplies actual images to vision while keeping base64 out of text output.",
                 "inputSchema": {"type": "object", "properties": {"clip_id": {"type": "string"}, "include_observations": {"type": "boolean", "default": False, "description": "Include unaltered saved model observations for an explicit history comparison."}}, "required": ["clip_id"], "additionalProperties": False}},
        {"type":"function","name":"get_business_context","description":"Read the selected collection's actual source provenance, index coverage and confirmed camera context before planning an investigation. Interpret these facts for this question; label assumptions.",
                 "inputSchema":{"type":"object","properties":{"dataset":{"type":"string"}},"additionalProperties":False}},
        {"type":"function","name":"plan_investigation","description":"Persist your business-specific investigation plan. You generate observable categories, focused visual queries, counterexamples and verification questions. Read get_business_context first. A revised plan is a new immutable record.",
                 "inputSchema":{"type":"object","properties":{
                     "question":{"type":"string","maxLength":8000},"business_context":{"type":"string","maxLength":6000},
                     "assumptions":{"type":"array","maxItems":20,"items":{"type":"string"}},
                     "coverage_goal":{"type":"string","enum":["examples","overview","all_occurrences"]},
                     "direct_clip_ids":{"type":"array","maxItems":6,"items":{"type":"string"}},
                     "categories":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"object","properties":{
                         "id":{"type":"string"},"label":{"type":"string"},
                         "observable_criteria":{"type":"array","minItems":1,"items":{"type":"string"}},
                         "queries":{"type":"array","minItems":1,"maxItems":4,"items":{"type":"string"}},
                         "verification_question":{"type":"string"},
                         "counterexamples":{"type":"array","items":{"type":"string"}}},
                         "required":["id","label","observable_criteria","queries","verification_question","counterexamples"],"additionalProperties":False}}},
                     "required":["question","business_context","assumptions","coverage_goal","categories"],"additionalProperties":False}},
        {"type":"function","name":"retrieve_investigation","description":"Execute all queries from your saved plan over the selected index. Return deduplicated candidate clips, category/query provenance and adjacent intervals. Counts describe candidates; detailed interpretation follows with Cosmos.",
                 "inputSchema":{"type":"object","properties":{"plan_id":{"type":"string"},"limit_per_query":{"type":"integer","minimum":1,"maximum":8}},"required":["plan_id"],"additionalProperties":False}},
        {"type":"function","name":"list_camera_context","description":"Read camera views, confirmed room/layout context, draft notes and actual stored observations in the selected collection. Camera identity and context carry source/time provenance.",
                 "inputSchema":{"type":"object","properties":{"camera_id":{"type":"string"},"dataset":{"type":"string"}},"additionalProperties":False}},
        {"type":"function","name":"cosmos_inspect","description":"Ask the actual Cosmos3 video reasoner a fresh question about up to six clips, or compare two views. Uses bounded free-credit GPU compute; exact prior requests are cached.",
                 "inputSchema":{"type":"object","properties":{"clip_ids":{"type":"array","minItems":1,"maxItems":6,"items":{"type":"string"}},"question":{"type":"string","minLength":1,"maxLength":2000},"compare_views":{"type":"boolean"},"category_ids":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"string"}}},"required":["clip_ids","question","category_ids"],"additionalProperties":False}},
        {"type":"function","name":"list_intervals","description":"Page through the entire selected collection, independent of similarity ranking, for coverage audits and broad investigations.",
                 "inputSchema":{"type":"object","properties":{"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":60}},"additionalProperties":False}},
        {"type":"function","name":"related_clips","description":"Get adjacent times in a source video and stored Cosmos-proposed relations, with uncertainty and evidence.",
                 "inputSchema":{"type":"object","properties":{"clip_id":{"type":"string"}},"required":["clip_id"],"additionalProperties":False}},
        {"type":"function","name":"discover_patterns","description":"Score and group the entire collection with Cosmos vectors on CPU by default. Set candidate_limit only for an explicit ranked subset. Inspect representative clips to interpret and label groups; drill down by group_id/interval_id. Counts are candidates.",
                 "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"groups":{"type":"integer","minimum":1,"maximum":8},"candidate_limit":{"type":"integer","minimum":1,"maximum":1000000},"score_threshold":{"type":"number","minimum":-1,"maximum":1},"group_id":{"type":"string"},"interval_id":{"type":"string"},"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":50}},"required":["query"],"additionalProperties":False}},
        {"type":"function","name":"focus_clip","description":"Track a subject you selected from a viewed source frame. Supply normalized x,y,width,height and the anchor time relative to clip start. Returns measured moving boxes for focus playback; this does not establish identity.",
                 "inputSchema":{"type":"object","properties":{"clip_id":{"type":"string"},"bbox":{"type":"array","minItems":4,"maxItems":4,"items":{"type":"number","minimum":0,"maximum":1}},"anchor_seconds":{"type":"number","minimum":0}},"required":["clip_id","bbox","anchor_seconds"],"additionalProperties":False}},
        {"type":"function", "name":"prepare_export", "description":"Prepare a requested editing handoff from one to twenty ordered source clips. Returns a private ZIP download with source-resolution normal-speed cuts, evidence manifest, report and rational edit-plan timing. Suggested speeds/framing are metadata for the editor.",
                     "inputSchema":{"type":"object","properties":{"document":{"type":"object","properties":{
                         "title":{"type":"string","minLength":1,"maxLength":160},"summary":{"type":"string","maxLength":20000},
                         "output_fps":{"type":["string","null"]},"items":{"type":"array","minItems":1,"maxItems":20,"items":{
                             "type":"object","properties":{"clip_id":{"type":"string"},"speed_multiplier":{"type":["number","string"]},
                             "framing":{"type":"string","maxLength":500},"reason":{"type":"string","maxLength":1000}},
                             "required":["clip_id"],"additionalProperties":False}}},"required":["title","items"],"additionalProperties":False}},
                         "required":["document"],"additionalProperties":False}},
        {"type":"function", "name":"get_export", "description":"Read the current status, source manifest and private download link of an editing package from this person's selected business collection.",
                     "inputSchema":{"type":"object","properties":{"payload_id":{"type":"string"}},"required":["payload_id"],"additionalProperties":False}},
        {"type":"function","name":"propose_camera_context","description":"Save a camera-context draft when the user asks. Anchor it to a clip and source time; room/layout notes stay inactive until an operator confirms them. An optional observation_ref must identify an actual stored model observation returned by list_camera_context.",
                     "inputSchema":{"type":"object","properties":{"camera_id":{"type":"string"},"dataset":{"type":"string"},"expected_revision":{"type":"integer","minimum":1},"anchor_clip_id":{"type":"string"},"valid_from_seconds":{"type":"number","minimum":0},"room":{"type":"string","maxLength":160},"layout_notes":{"type":"string","maxLength":6000},"change_type":{"type":"string","enum":["context_update","camera_moved"]},"observation_ref":{"type":"string","maxLength":64},"zones":{"type":"array","maxItems":20,"items":{"type":"object","properties":{"name":{"type":"string","maxLength":80},"description":{"type":"string","maxLength":1000},"bbox":{"type":"array","minItems":4,"maxItems":4,"items":{"type":"number","minimum":0,"maximum":1}}},"required":["name"],"additionalProperties":False}}},"required":["camera_id","expected_revision","anchor_clip_id","valid_from_seconds"],"additionalProperties":False}},
    ]
    from evidence_presentation import SPEC
    from custom_tool_contracts import tool_specs
    values.append(copy.deepcopy(SPEC))
    values.extend(spec for spec in tool_specs() if spec['name'] in {'get_storage_connection', 'propose_storage_connection'})
    return [spec for spec in active_specs(values) if write or spec['name'] in BRIDGE_READS]


def result_fields(name, content, success):
    previews = tool_previews(name, content, success)
    return {'frame_previews': previews} if previews else {}


async def bridge_call(context, name, args):
    self = context.agent
    scope, chat_id = context.collection, context.chat_id
    run, results = context.run, None
    if chat_id is None:
        return await external_read(context, name, args)
    from execution_policy import RETRIEVAL_VIEW
    query_index = RETRIEVAL_VIEW.get() or self.retrieval
    require_active_tool(name)
    planned = None
    if run is not None and name in {"search_video","discover_patterns","cosmos_inspect"}:
        if not run.get("plan_id"):
            raise ValueError("Read get_business_context and save plan_investigation before investigating footage.")
        planned = self._plans().get(run["plan_id"],scope,chat_id)
        if name != "cosmos_inspect" or planned.get("mode") != "direct":
            if not run.get("retrieval_receipt"):
                raise ValueError("Execute retrieve_investigation before refining candidates or asking Cosmos to verify them.")
        if name == "cosmos_inspect":
            category_ids = args.get("category_ids",[])
            allowed = {category["id"] for category in planned["categories"]}
            if (not isinstance(category_ids,list) or not 1<=len(category_ids)<=8
                    or any(not isinstance(key,str) for key in category_ids)
                    or len(set(category_ids))!=len(category_ids) or not set(category_ids)<=allowed):
                raise ValueError("Select category_ids from the current investigation plan for this verification question.")
    if name == "present_evidence":
        from evidence_presentation import build_presentation
        record = self.children.get(chat_id, {})
        if (record.get("parent_id") or not record.get("owner_principal_id") or not scope
                or record.get("dataset") != scope or run is None or run.get("cancel", asyncio.Event()).is_set()):
            raise ValueError("Present footage in its active parent chat and business collection.")
        data = build_presentation(args, index=query_index, collection=scope, public_clip=self._public_clip)
        run.setdefault("tool_events", asyncio.Queue()).put_nowait({"type": "evidence_presentation", "presentation": copy.deepcopy(data)})
        from presentation_captions import enrich
        data = await enrich(self, chat_id, run, scope, query_index, data)
    elif name in {"prepare_export", "get_export"}:
        record = self.children.get(chat_id, {})
        if record.get("parent_id") or not record.get("owner_principal_id") or not scope:
            raise ValueError("Prepare editing packages from the owning parent chat in one business collection.")
        if self.export_store is None:
            from media_exports import ExportStore
            self.export_store = ExportStore(self.state, self.retrieval)
        if name == "prepare_export":
            # Keep the shared store/locks, with this run's immutable source view.
            export_store = copy.copy(self.export_store)
            export_store.index = query_index
            data = await export_store.prepare(args["document"], dataset=scope,
                owner_principal_id=record["owner_principal_id"])
            files = self._files()
            files.exports = self.export_store
            artifact = await asyncio.to_thread(files.link_export, data["id"], owner_principal_id=record["owner_principal_id"],
                                               chat_id=chat_id, dataset=scope)
            data = {**data, "file": artifact}
        else:
            data = await asyncio.to_thread(self.export_store.get, args["payload_id"], dataset=scope,
                owner_principal_id=record["owner_principal_id"])
    elif name == "get_business_context":
        collection = scope or args.get("dataset")
        if not collection or scope and args.get("dataset") not in (None,scope):
            raise ValueError("Read context within the selected video collection.")
        data = await asyncio.to_thread(self._plans().context,collection)
        if run is not None:
            run["business_context_read"] = True
    elif name == "plan_investigation":
        if not scope:
            raise ValueError("Choose a business collection before planning the investigation.")
        if run is not None and not run.get("business_context_read"):
            raise ValueError("Call get_business_context before interpreting this business and planning its searches.")
        data = await asyncio.to_thread(self._plans().save,scope,chat_id or "mcp",args)
        if run is not None:
            run["plan_id"] = data["id"]
            run.pop("retrieval_receipt",None)
    elif name == "retrieve_investigation":
        if not scope:
            raise ValueError("Choose the investigation's business collection.")
        plan_id = args.get("plan_id")
        if run is not None and plan_id != run.get("plan_id"):
            raise ValueError("Retrieve the current turn's saved investigation plan.")
        data = await asyncio.to_thread(self._plans().retrieve,plan_id,scope,chat_id or "mcp",
                                      index=query_index,limit_per_query=args.get("limit_per_query",6))
        results = data["results"]
        if run is not None:
            run["retrieval_receipt"] = data["receipt_id"]
    elif name in {"list_camera_context", "propose_camera_context"}:
        if scope and args.get("dataset") not in (None,scope):
            raise ValueError("Use camera context within the selected video collection.")
        collection = scope or args.get("dataset")
        if not collection:
            raise ValueError("Choose a video collection for camera context.")
        if self.camera_store is None:
            from camera_context import CameraStore
            self.camera_store = CameraStore(self.state,self.retrieval)
        if name == "list_camera_context":
            if args.get("camera_id"):
                data = await asyncio.to_thread(self.camera_store.get,args["camera_id"],collection)
            else:
                data = await asyncio.to_thread(self.camera_store.list,collection)
        else:
            if chat_id and self._is_child(chat_id):
                raise ValueError("Return camera-context findings to the parent investigation for review.")
            body = {k:v for k,v in args.items() if k not in {"camera_id","dataset"}}
            data = await asyncio.to_thread(self.camera_store.propose,args["camera_id"],body,
                                           origin="agent_proposal",collection=collection)
    elif name == "discover_patterns":
        from discovery import discover_patterns
        options = {key:value for key,value in args.items() if key in {"groups","candidate_limit","score_threshold","group_id","interval_id","offset","limit"}}
        data = await asyncio.to_thread(discover_patterns,query_index,str(args.get("query","")),scope,**options)
        seen, results = set(), []
        for group in data["groups"]:
            for clip in group["representatives"]:
                if clip["id"] not in seen:
                    results.append(clip)
                    seen.add(clip["id"])
    elif name == "focus_clip":
        selected = query_index.get_clip(args["clip_id"])
        if selected is None or scope and selected["dataset"] != scope:
            raise ValueError("Choose a clip from the selected business.")
        if self.focus_tracker is None:
            from focus import FocusTracker
            self.focus_tracker = FocusTracker(self.state)
        data = await asyncio.to_thread(self.focus_tracker.build,selected,args["bbox"],float(args.get("anchor_seconds",0)))
        sidecar = data.pop("sidecar_path")
        focus_id = Path(sidecar).stem
        data["focus_id"] = focus_id
        path = self.state / "focus" / (selected["id"] + ".json")
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(data))
        temporary.chmod(0o600)
        temporary.replace(path)
        results = [{**self._public_clip(selected),"focus_available":True,"focus_id":focus_id}]
    elif name == "cosmos_inspect":
        ids = args.get("clip_ids", [])
        if not isinstance(ids,list) or not 1 <= len(ids) <= 6:
            raise ValueError("Choose one to six source intervals.")
        selected = [query_index.get_clip(i) for i in ids]
        if any(c is None or scope and c["dataset"] != scope for c in selected):
            raise ValueError("Choose clips from the selected business.")
        if planned is not None and planned.get("direct_clip_ids") and not set(ids)<=set(planned["direct_clip_ids"]):
            raise ValueError("Inspect the clips selected by this direct plan, or save a revised plan.")
        question = args.get("question","")
        if not isinstance(question,str) or not question.strip() or len(question)>2000:
            raise ValueError("Use a focused verification question of up to 2000 characters.")
        from cosmos_inspect import inspect_live, inspection_complete
        data = await inspect_live(ids,question,index=query_index,
                                  state=self.state,compare_views=bool(args.get("compare_views",False)))
        data = {key:data[key] for key in ("model","model_revision","requests","timing","run_id","cached") if key in data}
        if not inspection_complete(data):
            data.update(status="failed",error={"code":"inspection_incomplete",
                "message":"Cosmos returned an incomplete inspection answer. The attempt is retained."})
            return [{"type":"inputText","text":json.dumps(data)}], None, False
        if planned is not None:
            receipt = await asyncio.to_thread(self._plans().record_inspection,planned["id"],scope,chat_id,
                args["category_ids"],question,ids,data)
            data["investigation"] = {"plan_id":planned["id"],"category_ids":args["category_ids"],
                                     "inspection_receipt":receipt}
        results = [self._public_clip(c) for c in selected]
    elif name == "list_intervals":
        clips = sorted((c for c in query_index.clips if scope is None or c["dataset"]==scope),key=lambda c:(c["video_id"],c["start"]))
        offset, limit = max(0,int(args.get("offset",0))), min(60,max(1,int(args.get("limit",30))))
        page = clips[offset:offset+limit]
        data = {"intervals":[self._public_clip(c) for c in page],"total_intervals":len(clips),
                "indexed_seconds":sum(c["end"]-c["start"] for c in clips),
                "next_offset":offset+len(page) if offset+len(page)<len(clips) else None,
                "coverage":"All indexed windows in this run's selected scope, independent of ranking. Review is a separate step."}
    elif name == "related_clips":
        selected = query_index.get_clip(args["clip_id"])
        if selected is None or scope and selected["dataset"] != scope:
            raise ValueError("Choose a clip from the selected business.")
        same = sorted([c for c in query_index.clips if c["video_id"]==selected["video_id"]],key=lambda c:c["start"])
        position = next(i for i,c in enumerate(same) if c["id"]==selected["id"])
        neighbors = same[max(0,position-1):position] + same[position+1:position+2]
        path = self.state/"relations.json"
        relations = json.loads(path.read_text()) if path.exists() else []
        known = {c["id"] for c in query_index.clips if scope is None or c["dataset"]==scope}
        relations = [r for r in relations if selected["id"] in (r.get("from_clip_id"),r.get("to_clip_id")) and r.get("from_clip_id") in known and r.get("to_clip_id") in known]
        data = {"temporal_neighbors":[self._public_clip(c) for c in neighbors],"cosmos_relations":relations}
    elif name == "list_cameras":
        data = [item for item in self.corpus() if scope is None or item["dataset"] == scope]
        if query_index is not self.retrieval:
            allowed={clip['video_id'] for clip in query_index.clips}
            data=[item for item in data if item.get('video_id',item.get('id')) in allowed]
    elif name == "search_video":
        query = str(args.get("query", ""))[:2000]
        if scope and args.get("dataset") not in (None, scope):
            raise ValueError("Search within the selected video collection.")
        results = await asyncio.to_thread(query_index.search, query, min(8, max(1, int(args.get("limit", 6)))), scope or args.get("dataset"))
        data = {"candidates": results, "coverage": "Ranked visual similarity candidates; inspect before answering."}
    elif name == "inspect_clip":
        owner = self.children.get(chat_id, {}).get('owner_principal_id')
        saved_scope = self.children.get(chat_id, {}).get('dataset')
        generation = run.get('generation') if run else None
        task = asyncio.current_task()
        def preview_current():
            saved = self.children.get(chat_id, {})
            session = run.get('isolated_session') if run else None
            return bool(owner and run and self.active.get(chat_id) is run
                and run.get('generation') == generation and not task.cancelling()
                and not run.get('cancel', asyncio.Event()).is_set()
                and saved.get('owner_principal_id') == owner
                and saved.get('dataset') == saved_scope
                and saved_scope in (None, scope)
                and not getattr(session, '_closing', False) and not getattr(session, 'stopped', False))
        selected = query_index.get_clip(args["clip_id"])
        if selected is None or (scope and selected["dataset"] != scope):
            raise ValueError("Choose a clip from the selected video collection.")
        from imported_media import imported_record
        derivatives = getattr(query_index, 'source_derivatives', None)
        if imported_record(selected) and derivatives is not None and derivatives.enabled:
            from media import EvidenceMedia
            evidence = EvidenceMedia(self.state)
            evidence.derivatives = derivatives
            inspected = await evidence.inspect(selected,
                owner_principal_id=self.children.get(chat_id, {}).get('owner_principal_id'))
        else:
            inspected = await asyncio.to_thread(query_index.inspect_clip, args["clip_id"])
        # The retrieval adapter returns evidence plus cached image files.
        record = inspected.get("clip", inspected.get("record", inspected))
        paths = inspected.get("frames", [])[:4]
        public = self._inspection_metadata(record, paths, args.get("include_observations", False))
        # Only the host receipt supplies browser-addressable snapshots.
        public.pop('frame_previews', None)
        public.pop('frame_previews_status', None)
        content, samples = [], []
        for frame in paths[:4]:
            image_bytes = frame.get("image_bytes") if isinstance(frame, dict) else None
            if image_bytes is None:
                filename = frame.get("path") if isinstance(frame, dict) else frame
                if not filename:
                    raise ValueError("Choose an available source preview.")
                image_bytes = Path(filename).read_bytes()
            if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= 8 * 1024**2:
                raise ValueError("Choose an available source preview.")
            content.append({"type": "inputImage", "imageUrl": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()})
            samples.append({**({key:frame[key] for key in ('time', 'basis', 'width', 'height') if key in frame}
                if isinstance(frame, dict) else {}), 'image_bytes': image_bytes})
        if not content:
            raise ValueError("Choose an available source preview with image frames.")
        if preview_current():
            from custom_coordinator import offload
            try:
                public['frame_previews'] = await offload(InspectionPreviews(self.state).save,
                    owner_principal_id=owner, chat_id=chat_id, dataset=selected['dataset'],
                    clip_id=selected['id'], frames=samples, guard=preview_current)
            except PreviewError:
                public['frame_previews_status'] = 'unavailable'
        content.insert(0, {'type': 'inputText', 'text': json.dumps(public)})
        return content, None, True
    elif name in {'propose_storage_connection','get_storage_connection'}:
        principal = context.data_owner
        store = self._custom_connectors()
        saved = await asyncio.to_thread(store.get, args['connector_id'], owner=principal)
        if saved['collection'] != scope:
            raise ValueError('Use a custom connector from this business.')
        from custom_coordinator import offload
        if saved['kind']!='sftp':raise ValueError('Choose a stored-recording connector.')
        control=await offload(self._storage_runtime)
        if name=='get_storage_connection':
            if set(args)!={'connector_id'}:raise ValueError('Read the selected folder connection.')
            data=await offload(control.describe,args['connector_id'],owner=principal,collection=scope)
        else:
            if set(args)!={'connector_id','target','reason','expected_revision'}:raise ValueError('Provide the current folder proposal.')
            try:
                data=await offload(control.authority.propose,args['connector_id'],args['target'],args['reason'],
                    expected_revision=args['expected_revision'],owner=principal,collection=scope)
            except ImportError:raise ValueError('The host is preparing its fixed SFTP reader. Keep the folder details and private login fields in this setup.')from None
            data['review_url']=f"/?customize=1&tab=connections&item={args['connector_id']}"
    else:
        raise ValueError('Choose an available retail tool.')
    return [{'type': 'inputText', 'text': json.dumps(data)}], results, True


def plans(self):
    if self.investigation_plans is None:
        from investigation_plans import InvestigationPlans
        self.investigation_plans = InvestigationPlans(self.state, self.retrieval)
    return self.investigation_plans


def public_clip(record):
    result = {k:v for k,v in record.items() if k not in {"path","thumbnail_path","prepared_path"}}
    result.update(source_url=f"/?clip={record['id']}",video_url=f"/clips/{record['id']}.mp4",thumbnail_url=f"/thumbnails/{record['id']}.jpg")
    return result


def inspection_metadata(self, record, frames, include_observations=False):
    if type(include_observations) is not bool:
        raise ValueError("Choose true or false for include_observations.")
    public = self._public_clip({key: value for key, value in record.items()
                                if key not in {"frames", "cosmos_observations"}})
    public["evidence_type"] = "snapshot_preview"
    public["temporal_inspection_tool"] = "cosmos_inspect"
    public["sampled_frame_times"] = [frame.get("time") for frame in frames if isinstance(frame, dict)]
    frame_basis = [{key: frame.get(key) for key in ("basis", "width", "height")}
                   for frame in frames if isinstance(frame, dict) and frame.get("basis")]
    if frame_basis:
        public["frame_basis"] = frame_basis
    if include_observations:
        path = self.state / "descriptions" / (record["id"] + ".json")
        public["cosmos_observations"] = json.loads(path.read_text()) if path.exists() else []
    return public


def clip_attachments(self, ids=(), dataset=None):
    if not isinstance(ids, (list, tuple)) or len(ids) > 12 or any(not isinstance(key,str) for key in ids) or len(set(ids)) != len(ids):
        raise ValueError("Choose up to twelve distinct Gallery clips.")
    result = []
    for key in ids:
        clip = self.retrieval.get_clip(key)
        if clip is None or dataset and clip["dataset"] != dataset:
            raise ValueError("Choose Gallery clips from this business.")
        result.append(self._public_clip(clip))
    return result


def context(admitted):
    from monitoring_tools import scoped
    from retail_tool_context import WriteContext
    agent = admitted.agent
    view = getattr(admitted.admission, '_resource', None) or getattr(agent, 'retrieval', None)
    store = getattr(agent, 'monitoring_store', None)
    if store is None or view is None:
        raise ValueError('Use the installed retail workspace services.')
    selected = WriteContext(scoped(store, view, host=getattr(agent, 'business_scope_host', None),
        chat_id=admitted.chat_id), admitted.actor, admitted.collection, admitted.agents,
        agent, admitted.chat_id, admitted.record, admitted.run)
    selected.collection = admitted.collection
    selected.actor = admitted.actor
    selected.data_owner = admitted.data_owner
    selected.admission = admitted.admission
    selected.extension = admitted.extension
    return selected


def catalog_call(context, name, arguments):
    from catalog_setup_tools import call
    return call(context.agent, context.chat_id, context.dataset, name, arguments, context.run)


BRIDGE_READS = frozenset({'list_cameras', 'get_business_context', 'list_camera_context',
                          'get_export', 'get_storage_connection'})
BRIDGE_WRITES = frozenset({'present_evidence', 'prepare_export', 'propose_camera_context',
                           'propose_storage_connection'})
BRIDGE = SimpleNamespace(READ_NAMES=BRIDGE_READS, WRITE_NAMES=BRIDGE_WRITES,
    ASYNC_NAMES=BRIDGE_WRITES, specs=bridge_specs, aread=bridge_call, acall=bridge_call,
    SURFACE_READS={'native': frozenset({'list_cameras', 'get_business_context', 'list_camera_context'}),
        'public': frozenset({'list_cameras', 'get_business_context', 'list_camera_context'}),
        'local': frozenset({'list_cameras', 'get_business_context', 'list_camera_context', 'get_export'})},
    name='retail', module='retail_tools')

from catalog_setup_tools import specs as catalog_specs
CATALOG = SimpleNamespace(READ_NAMES=frozenset({'get_catalog_setup', 'get_product_match'}),
    WRITE_NAMES=frozenset({'configure_location', 'prepare_catalog_photo_upload', 'import_catalog_products'}),
    specs=catalog_specs, read=catalog_call, call=catalog_call,
    SURFACE_READS={'native': frozenset({'get_product_match'}), 'public': frozenset(), 'local': frozenset()},
    name='catalog', module='catalog_setup_tools')

from retail_agent_inputs import RetailAgentDefinition
BRIDGE.definition_model = RetailAgentDefinition


async def connector_details(agent, saved, connector_id, owner, collection):
    if saved['kind'] != 'sftp':
        return {}
    from custom_coordinator import offload
    control = await offload(agent._storage_runtime)
    return {'storage_setup': await offload(control.describe, connector_id, owner=owner, collection=collection)}


async def external_read(context, name, arguments):
    from tool_families import _offload
    agent = context.agent
    view = context.store.scope_view
    if name == 'list_cameras':
        return view.report['videos']
    if name == 'get_export':
        return await _offload(lambda: agent.export_store.get(arguments['payload_id'],
            dataset=context.collection, owner_principal_id=context.actor))
    if name == 'get_business_context':
        return await _offload(plans(agent).context, context.collection)
    if name == 'list_camera_context':
        from camera_context import CameraStore
        cameras = agent.camera_store or CameraStore(agent.state, agent.retrieval)
        if arguments.get('camera_id'):
            return await _offload(cameras.get, arguments['camera_id'], context.collection)
        return await _offload(cameras.list, context.collection)
    raise ValueError('Choose an available retail read tool.')


async def local_call(context, name, arguments):
    from tool_families import _offload
    agent = context.agent
    if name == 'prepare_export':
        return await agent.export_store.prepare(arguments['document'], dataset=context.collection,
            owner_principal_id=context.actor)
    if name == 'propose_camera_context':
        from camera_context import CameraStore
        store = agent.camera_store or CameraStore(agent.state, agent.retrieval)
        body = {key: value for key, value in arguments.items() if key not in {'camera_id', 'dataset'}}
        return await _offload(lambda: store.propose(arguments['camera_id'], body,
            origin='agent_proposal', collection=context.collection))
    raise ValueError('Choose an available local retail action.')


BRIDGE.LOCAL_WRITES = frozenset({'prepare_export', 'propose_camera_context'})
BRIDGE.local_call = local_call
