"""Retail cameras: services, lifecycle and HTTP contribution to the shared harness."""
from __future__ import annotations
from retail_settings import value as setting
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import threading
import time

from host import BareWorkspace
from source_visibility import ReadyIndex
from qdrant_retrieval import QdrantRetrievalIndex
from media import EvidenceMedia
from media_exports import ExportStore
from library import SourceLibrary
from camera_context import CameraStore
from imported_media import ImportedMedia
from demo_guidance import demo_guidance
from phone_delivery import window_lane

def public_corpus(index):
    videos = {}
    for clip in index.clips:
        key = clip["video_id"]
        if key not in videos:
            videos[key] = {"video_id": key, "dataset": clip["dataset"], "camera": clip["camera"],
                           "duration": 0, "source_reference": clip.get("source_reference"),
                           "provenance": clip.get("camera_semantics")}
        videos[key]["duration"] = max(videos[key]["duration"], clip["end"])
    return list(videos.values())


class RetailWorkspace(BareWorkspace):
    def __init__(self, host, selected):
        super().__init__(selected)
        self.host = host
        self.source_lock = threading.Lock()
        self.storage_lock = threading.Lock()
        self.link_lock = threading.Lock()

    def participant_delivery_current(self, chat_id):
        lane=getattr(self.selected['agent'],'phone_run_lane',None)
        try:live=callable(lane) and lane(chat_id)=='live'
        except Exception:live=False
        if not live:
            raise ValueError('This run reads replayed observations. Phone alerts follow live observations.')
        return True

    def read_admission(self, actor, collection):
        from host import Admission
        from scope_host import scope_current
        common=super().read_admission(actor,collection)
        selected=self.selected
        try:source=selected['index'].snapshot(actor,refresh=True,apply_scope=False)
        except (ValueError,OSError):source=selected['index'].snapshot(actor,apply_scope=False)
        policy=selected['scope_policy']
        view=policy.filter_view(source,actor,collection)
        return Admission(lambda:common.current() and scope_current(view),lambda:policy.bind(view),resource=view)

    def tool_context(self, context):
        from retail_tools import context as retail_context
        return retail_context(context)

    def result_fields(self, name, content, success):
        from retail_tools import result_fields
        return result_fields(name, content, success)

    def status_fields(self):
        selected=self.selected
        index=selected['index']
        return {'retrieval':index.status(),'corpus':public_corpus(index),
            'demo_guidance':demo_guidance(selected['environment_id'],index.clips),
            'index_update':{'status':'needs_attention' if selected['scheduler'].input_error else 'ready'}}

    def budget_fields(self):
        from query_budget import QueryBudget
        authority=self.selected['state']/'compute/query-approval.json'
        if authority.exists():
            try:return {'query_pool':QueryBudget(authority).status()}
            except (ValueError,OSError):return {'status':'GPU budget verification needs attention.'}
        return {}

    def member_cards(self, actor, collection, rows):
        return self.selected["profile_covers"].members(actor,collection,rows)

    def business_fields(self, actor, collection, value):
        return self.selected["profile_covers"].business(actor,collection,value)

    def provider_usage(self):
        return {'modal':self.selected['usage'].summary()}

    def available_credit(self):
        from query_budget import QueryBudget
        try:return QueryBudget(self.selected['state']/'compute/query-approval.json').status()['available_usd']
        except (ValueError,OSError):return '0'

    def agent_capabilities(self):
        return {'fresh_inspection_ceiling_usd':str(self.selected['agents'].inputs.resource_ceiling()),
            'ongoing_input':'Committed source observations','external_delivery':'in_development'}

    def validate_activation(self, definition, once):
        from decimal import Decimal
        from query_budget import QueryBudget
        amount=self.selected['agents'].inputs.resource_allowance(definition)
        if amount:
            allowance=QueryBudget(self.selected['state']/'compute/query-approval.json').status()
            runs=1 if once else definition['max_runs']-definition['runs_started']
            if amount*runs>Decimal(allowance['available_usd']):
                raise ValueError('Choose fewer runs to fit the available free-credit allowance.')

    def usage_fields(self):
        from cosmos_retrieval import generation_paths
        selected=self.selected
        paths={Path(clip['path']) for clip in selected['index'].clips if clip.get('path')}
        matrix=generation_paths(selected['state']/'cosmos')['embeddings.npy']
        return {'local_search':{'additional_provider_cost_usd':'0'},
            'storage':{'source_bytes':sum(path.stat().st_size for path in paths if path.is_file()),
                'index_bytes':matrix.stat().st_size if matrix.is_file() else 0,
                'billing_status':'existing_development_storage','metered_storage_cost_usd':None}}

    def authorize_file(self, request, file_id, principal):
        return self.host.options["file_authorization"](request,file_id,principal)

    async def file_download(self, record, principal):
        return await asyncio.to_thread(self.selected["exports"].download_path,record["id"],owner_principal_id=principal)

    def plans(self):
        from retail_tools import plans
        return plans(self.selected['agent'])

    def public_clip(self, record):
        from retail_tools import public_clip
        return public_clip(record)

    def inspection_metadata(self, record, frames, include_observations=False):
        from retail_tools import inspection_metadata
        return inspection_metadata(self.selected['agent'],record,frames,include_observations)

    def clip_attachments(self, ids=(), dataset=None):
        from retail_tools import clip_attachments
        return clip_attachments(self.selected['agent'],ids,dataset)

    async def connector_details(self, saved, connector_id, owner, collection):
        from retail_tools import connector_details
        return await connector_details(self.selected["agent"],saved,connector_id,owner,collection)

    async def connection_fields(self, value, principal, request):
        if value['kind']=='sftp':
            from custom_coordinator import offload
            from chat_permissions import workspace_writable
            control=await offload(self.storage_runtime)
            value['storage_setup']=await offload(control.describe,value['id'],owner=principal,collection=value['collection'])
            if not workspace_writable(request.state.session):
                value['storage_setup'].update(can_run=False,can_pause=False)
        return value

    def catalogue(self):
        from agent_catalogue import available
        return available()

    def draft_agent(self, template_id, collection, principal):
        from agent_catalogue import draft
        return draft(template_id,collection,self.selected['monitoring'].list_profiles(principal,collection))

    def source_options(self, principal, collection):
        return self.selected['scope_policy'].source_options(principal,collection)

    def request_scope(self, request):
        return self.selected['scope_policy'].request_scope(request)

    async def tick(self):
        from stream_runtime import stream_tick
        await stream_tick(self.selected)

    async def close(self):
        if self.selected.get('link_intake'):
            await self.selected['link_intake'].close()

    def session_fields(self):
        clips=self.selected['index'].clips
        guidance=demo_guidance('demo',clips)
        return {'category':'demo','collections':[{'id':name,'name':guidance.get(name,{}).get('name',name.replace('_',' ').title())}
            for name in sorted({row['dataset'] for row in clips})]}

    def source_host(self):
        selected = self.selected
        from source_service import SourceHost
        with self.source_lock:
            if 'source_host' not in selected:
                selected['source_host'] = SourceHost(selected['state'])
            return selected['source_host']


    def source_store(self):
        selected = self.selected
        from source_intake import SourceStore
        with self.source_lock:
            if 'source_store' not in selected:
                selected['source_store'] = SourceStore(selected['state'])
            return selected['source_store']


    def storage_runtime(self):
        selected = self.selected
        # Lazy construction keeps the existing app startup independent of the
        # optional SSH worker dependency and avoids starting any acquisition.
        from storage_connections import StorageConnections
        from storage_runtime import StorageRuntime
        with self.storage_lock:
            if 'storage_runtime' not in selected:
                selected['storage_runtime'] = StorageRuntime(
                    StorageConnections(selected['custom_connectors'],self.source_store()),
                    source_host=self.source_host(),access_state=self.host.access.state,environment=selected['environment_id'])
            return selected['storage_runtime']


    def link_intake(self):
        selected = self.selected
        from link_intake import LinkIntake, member_current
        with self.link_lock:
            if 'link_intake' not in selected:
                selected['link_intake'] = LinkIntake(selected['state'],
                    store=self.source_store(), host=self.source_host(), enabled=True,
                    authorized=lambda owner, collection: member_current(
                        selected['state'], self.host.access.state, selected['environment_id'], owner))
            return selected['link_intake']


    def capabilities(self, collection, *, principal=None, chat_id=None, read_only=False, child=False, actor=None, custom=None):
        selected=self.selected
        status=selected['index'].status()
        clips=[row for row in selected['index'].clips if row.get('dataset')==collection]
        storage=[]
        if principal:
            sftp = [row for row in selected['custom_connectors'].list(owner=principal, collection=collection) if row['kind']=='sftp']
            if sftp and self.storage_runtime:
                control = self.storage_runtime()
                for row in sftp:
                    detail = control.describe(row['id'],owner=principal,collection=collection)
                    if read_only or child:detail.update(can_run=False,can_pause=False)
                    storage.append({'connector_id':row['id'],**detail})
                    item=next(value for value in custom if value['id']==row['id'])
                    item.update(runtime_available=detail['runtime_available'],execution='fixed_sftp_acquisition',
                        active_status=detail['acquisition']['status'])
        intake=selected['intake_projection'].snapshot(principal,collection,read_only=read_only or child)
        return {'recurring_agents':{'configurable':not read_only and not child,
                'ready':selected['agents'].inputs.recurring_ready(collection,actor or principal),
                'notice':None if selected['agents'].inputs.recurring_ready(collection,actor or principal) else
                    'Configure your agent here. Scheduled and ongoing runs start on committed observations.'},
            'demo_context':demo_guidance(selected['environment_id'],selected['index'].clips).get(collection),
            'footage':{'kind':'imported','search_ready':bool(status.get('ready') and clips),'clip_count':len(clips)},
            'live_camera_feeds':{'ready':False,'status':'setup_available'},'storage_connections':storage,
            'intake':intake['intake'],'imports':intake['imports'],'imports_next_offset':intake['next_offset']}

def install_workspace(host, selected):
    extension=RetailWorkspace(host,selected)
    selected['vertical']=extension
    from retail_business import install_business
    install_business(selected)
    environment_id=selected['environment_id']
    access=host.access
    index=host.options.get('index') if environment_id=='demo' else None
    index=index or host.options.get('index_factory',QdrantRetrievalIndex)(selected['state']/'cosmos')
    primary=host.workspaces.get('demo')
    share=getattr(index,'use_text_encoder',None)
    if primary and callable(share):share(primary['index']._raw)
    selected['index']=index
    selected['media']=EvidenceMedia(selected['state'])
    selected["imported_media"] = ImportedMedia(selected["state"])
    selected["index"].imported_media = selected["imported_media"]
    if setting('ROCOTO_RETAIL_SOURCE_DERIVATIVES_ENABLED') == '1':
        from source_derivatives import SourceDerivatives
        from source_media import MediaClient
        from workspace_identity import workspace_scope
        selected['derivatives'] = SourceDerivatives(selected['state'],
            company_id=workspace_scope(selected['state']), media_client=MediaClient(), enabled=True)
        selected['index'].source_derivatives = selected['derivatives']
        selected['media'].derivatives = selected['derivatives']
    selected["index"] = ReadyIndex(selected["index"], selected["state"])
    selected["agent"].retrieval = selected["index"]
    selected["agent"].corpus = lambda: public_corpus(selected["index"])
    selected["library"] = SourceLibrary(selected["state"], selected["index"])
    selected["cameras"] = CameraStore(selected["state"], selected["index"])
    selected["agent"].camera_store = selected["cameras"]
    selected["exports"] = ExportStore(selected["state"], selected["index"], selected["media"])
    selected["exports"].recover()
    selected["agent"].export_store = selected["exports"]
    selected["files"].exports = selected["exports"]
    selected["agent"].file_store = selected["files"]
    selected['agent'].storage_runtime_factory = lambda selected=selected: extension.storage_runtime()
    from intake_projection import IntakeProjection
    from workspace_identity import workspace_scope
    selected['intake_projection'] = IntakeProjection(selected['state'],
        company_id=workspace_scope(selected['state']),
        collections=lambda selected=selected: selected['index'].status().get('datasets',[]))
    from monitoring import MonitoringStore, resolve_monitoring_source, monitoring_profile_authorized
    def monitoring_source(owner,key):
        return resolve_monitoring_source(selected,owner,key)
    selected['monitoring']=MonitoringStore(selected['state'],source_lookup=monitoring_source)
    selected['intake_projection'].monitoring_collections=selected['monitoring'].registered_datasets
    selected['monitoring'].profile_authorized=lambda owner,record:monitoring_profile_authorized(selected,owner,record)
    selected['agent'].monitoring_store=selected['monitoring']

    from company_usage import CompanyUsage
    from retail_usage import RetailQueryProvider
    selected["usage"] = CompanyUsage(selected["state"],provider=RetailQueryProvider())
    authority = selected["state"] / "compute/query-approval.json"
    if selected["state"] == host.state and authority.exists():
        try:
            selected["usage"].import_demo_allowance(authority)
        except (ValueError,OSError):
            selected["usage_status"] = "Refresh the provider-credit authorization to enable fresh inspections."
    from retail_scope import install_scope, RetailScope
    install_scope(selected,host.member,host.members)
    selected['scope_policy']=RetailScope(selected)
    def display_owner_agent(owner, collection, *, create=False):
        chats=selected['organization_chats']
        if owner!=chats.owner:
            return None
        if create:
            return chats.ensure(selected['agent'],collection,'organization')['id']
        record=selected['agent'].children.get(chats.identity(collection,'organization'))
        return record['id'] if chats.canonical(record) else None
    selected['business_scope'].display_owner_agent=display_owner_agent
    from location_intelligence import LocationIntelligence
    from stream_ingestion import StreamIngestion
    selected['location_intelligence']=LocationIntelligence(selected['state'],selected['business_scope'],selected['monitoring'])
    selected['stream_ingestion']=StreamIngestion(selected['state'],selected['monitoring'])
    from stream_runtime import setup_catalog_inspection
    setup_catalog_inspection(selected)
    from catalog_setup import CatalogSetup
    selected['catalog_setup']=CatalogSetup(selected['state'],selected['location_intelligence'],environment_id)
    from profile_cover_host import install_profile_covers
    install_profile_covers(selected,access)

    def collections(principal):
        from source_intake import IntakeActor
        result=set(selected['index'].status().get('datasets',()))
        if principal:
            result.update(selected['monitoring'].registered_datasets(principal))
            result.update(selected['intake_projection'].collections(IntakeActor(principal)))
        return result
    selected['collections'].register(collections)
    for collection in selected['collections'].list(None):
        display_owner_agent(selected['organization_chats'].owner,collection,create=True)
    from storage_connections import initialize_schema, public_values
    selected['custom_connectors'].register_extension('retail_storage',initialize=initialize_schema,public_values=public_values)
    from retail_agent_inputs import RetailInputProvider
    from tracker_runner import script_wake_authorizer
    store=selected['agents']
    store.install_inputs(RetailInputProvider(monitoring=selected['monitoring'],index=selected['index']),
        wake_authorizer=script_wake_authorizer(selected))
    from retail_script_signals import install as install_script_signals
    install_script_signals(selected)
    selected['capabilities'].contribution=extension.capabilities
    def run_lane(run, depth=0):
        """The lane of one run: the observation that woke it and the observations it was handed."""
        wake = run.get('wake') or {}
        observations = list(run.get('observations') or [])[:256]
        if wake.get('provider') == 'monitoring':
            observations.append(selected['monitoring'].get_event(run['owner_principal_id'], wake['event_id']))
        for observation in observations:
            lane = observation_lane(observation, depth)
            if lane != 'live':
                return lane
        return 'live'
    def observation_lane(event, depth=0):
        """'live', 'replay' or 'unknown' for one committed observation, decided once and kept.

        A stream window names its own lane. An observation a person or a model turn
        committed outside any window takes the lane of a run that was reading
        replayed observations at that moment, so a replay loop reaches zero phones
        through the turns it wakes as well.
        """
        def decide():
            lane = window_lane(selected['stream_ingestion'], event)
            if lane != 'windowless':
                return lane
            if depth >= 3:
                return 'unknown'
            at = event['committed_at']
            for definition in store.list(owner_principal_id=event['owner_principal_id']):
                for run in definition.get('runs', []):
                    if run['created_at'] <= at and (run.get('completed_at') is None or at <= run['completed_at']):
                        lane = run_lane(run, depth + 1)
                        if lane != 'live':
                            return lane
            return 'live'
        return selected['phone_delivery'].lane(event['id'], decide)
    def chat_run_lane(chat_id):
        """The lane of the host run active in this conversation; a person's own turn is live."""
        record = selected['agent'].children.get(chat_id) or {}
        if not record.get('active_run_id'):
            return 'live'
        try:
            return run_lane(store.run_report(record['agent_id'], record['active_run_id']))
        except Exception:
            return 'unknown'
    selected['agent'].phone_run_lane = chat_run_lane
    def covers(definition, event):
        """This saved definition tracks the observation: its business and its profiles."""
        return (definition.get('input_kind')=='tracker_events' and definition['dataset']==event['dataset']
            and (not definition.get('profile_ids') or event['profile_id'] in definition['profile_ids'])
            and (not definition.get('event_types') or event['type'] in definition['event_types']))
    def tracker_alert(definition, event):
        """Host alert delivery: the committed event is this Tracking Agent's processed alert, revision 1.

        It follows the agent's own phone policy, whatever its mode and run
        activation. The compiled-data replay lane and an unreadable lane write
        zero deliveries.
        """
        return selected['phone_delivery'].fan_out(definition['id'], alert_id=event['id'], alert_revision=1,
            summary=event['description'], lane=observation_lane(event), committed_at=event['committed_at'])
    def emit_monitoring_event(event):
        import sqlite3
        for definition in store.list(owner_principal_id=event['owner_principal_id']):
            if not covers(definition, event):continue
            if definition['status']=='active' and definition['mode']=='ongoing':
                try:
                    store.wakes.enqueue(definition['id'],owner=event['owner_principal_id'],expected_revision=definition['revision'],
                        provider='monitoring',connection_id=event['profile_id'],event_id=event['id'],
                        text='Review the newly committed observation '+event['id']+' and its source evidence.')
                except ValueError:
                    # Changed/expired activation leaves the committed event for ordinary
                    # watermark-based processing after an authorized resume.
                    pass
            if definition.get('agent_kind') != 'tracker':
                continue
            try:
                tracker_alert(definition, event)
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
                continue  # the catch-up pass reads this alert again; the commit and the wake are unaffected
    selected['monitoring'].event_sink=emit_monitoring_event
    def phone_catch_up():
        """Read again each covering observation of the last 900 seconds that holds no fan-out receipt.

        The event commits first and its fan-out follows, so a store that was
        busy at that moment leaves an alert unread. Fan-out is idempotent and
        bounded by the alert's commit time, so this pass adds nothing twice.
        """
        import sqlite3
        from contextlib import closing
        from monitoring import is_reference_event
        from phone_delivery import DELIVERY_SECONDS
        with closing(selected['monitoring'].connect()) as db:
            recent = [json.loads(row[0]) for row in db.execute(
                'SELECT body FROM monitoring_events WHERE created_at>? ORDER BY created_at,rowid LIMIT 500',
                (time.time() - DELIVERY_SECONDS,))]
        recent = [event for event in recent if not is_reference_event(event)]
        if not recent:
            return 0
        read = 0
        trackers = [definition for definition in store.list() if definition.get('agent_kind') == 'tracker']
        for definition in trackers:
            mine = [event for event in recent if event['owner_principal_id'] == definition.get('owner_principal_id')
                and covers(definition, event)]
            waiting = selected['phone_delivery'].unread(definition['id'], [event['id'] for event in mine]) if mine else ()
            for event in mine:
                if event['id'] not in waiting:
                    continue
                try:
                    tracker_alert(definition, event)
                    read += 1
                except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
                    continue
        return read
    selected['phone_catch_up'] = phone_catch_up
    return extension


@asynccontextmanager
async def lifespan(app, host):
    from realtime_read_routes import maintained_lifespan
    from tracker_runner import tracker_start, tracker_stop, tracker_tick
    async def tick():
        while True:
            try:await tracker_tick(app.state)
            except Exception:pass
            await asyncio.sleep(1)
    async with maintained_lifespan(app):
        await tracker_start(app.state)
        task=asyncio.create_task(tick())
        try:yield
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            await tracker_stop(app.state)


def register_routes(app, host, http):
    from retail_business import register_routes as register_business_routes
    register_business_routes(app)
    from retail_scope_routes import register_host_routes
    register_host_routes(app)
    from control_room_static import is_static_viewer_request
    from retail_urls import legacy_path
    public_assets={'/control-room.js','/assets/demo-companies/sij-acroni.png',
        '/tracker-canvas.js','/tracker-canvas.css','/camera-streams.js','/camera-streams.css','/retail-shell.js','/retail-ui.js','/retail-ui.css',
        '/catalog-upload.js','/catalog-upload.css','/business-scope.js','/business-scope.css',
        '/static/harness/business-scope.js','/static/harness/business-scope.css'}
    def public_request(request):
        path=legacy_path(request.url.path,request.method)
        return (path in public_assets or path.startswith('/assets/camera-providers/')
            or request.method in {'GET','HEAD'} and path.startswith('/catalog-upload/')
            or request.method=='POST' and path.startswith('/api/catalog/mobile-upload/'))
    def request_body_limit(request):
        path=legacy_path(request.url.path,request.method)
        if request.method=='POST' and path.startswith(('/api/catalog/mobile-upload/','/api/catalog/import-file')):
            return (10 if '/mobile-upload/' in path else 2)*1024*1024+65536
        return None
    host.options['page_assets']={
        'scripts':['/static/retail/'+name for name in ('camera-streams.js','tracker-canvas.js','control-room.js','retail-shell.js','retail-scope.js','retail-ui.js')],
        'styles':['/static/retail/'+name for name in ('camera-streams.css','tracker-canvas.css','retail-scope.css','retail-ui.css')]}
    def query_request(request):
        path=legacy_path(request.url.path,request.method)
        return (path.startswith(('/api/exports','/api/inspection-previews/','/api/connections/custom/'))
            or path=='/api/search' or request.method=='POST' and (path.startswith('/api/cameras/')
                or path in {'/api/business/settings', '/api/business/connections'}))
    def scope_change(request):
        path=legacy_path(request.url.path,request.method)
        return (request.method=='POST' and path.startswith('/api/business/scope/')
            or request.method=='PUT' and path.startswith('/api/chats/') and path.endswith('/scope'))
    host.options.update(public_request=public_request,request_body_limit=request_body_limit,
        static_request=is_static_viewer_request,query_request=query_request,scope_change=scope_change)

    from tracker_runner import runner_state
    app.state.tracker_runner=runner_state(host.state,host.access.state)
    from retail_http import register_routes as register_retail_http
    register_retail_http(app,host,http)


def local_services(*args, **kwargs):
    from retail_mcp_services import local_services as factory
    return factory(*args, **kwargs)


def read_workspace(host, selected):
    """Install bounded retail reads without scheduler, messaging or runtime recovery."""
    from retail_business import install_business
    install_business(selected)
    from retail_scope import RetailScope
    from retail_business_scope import RetailBusinessScope
    authority=selected['business_scope']
    selected['business_scope']=RetailBusinessScope(selected['state'],company_id=authority.company_id,
        member=authority.member,members=authority.members)
    from monitoring import MonitoringStore
    from retail_agent_inputs import RetailInputProvider
    from investigation_plans import InvestigationPlans
    raw=host.options.get('index_factory',QdrantRetrievalIndex)(selected['state']/'cosmos')
    primary=host.workspaces.get('demo')
    share=getattr(raw,'use_text_encoder',None)
    if primary and callable(share):share(primary['index']._raw)
    raw.imported_media=ImportedMedia(selected['state'])
    selected['index']=ReadyIndex(raw,selected['state'])
    selected['scope_policy']=RetailScope(selected)
    selected['index'].scope_filter=lambda view,principal:selected['scope_policy'].filter_view(view,principal)
    selected['collections'].register(lambda principal:selected['index'].status().get('datasets',()))
    selected['monitoring']=MonitoringStore(selected['state'])
    selected['agents'].install_inputs(RetailInputProvider(monitoring=selected['monitoring'],index=selected['index']))
    agent=selected['agent']
    agent.retrieval=selected['index']
    agent.corpus=lambda:public_corpus(selected['index'])
    agent.monitoring_store=selected['monitoring']
    agent.camera_store=CameraStore(selected['state'],selected['index'])
    agent._plans_store=InvestigationPlans(selected['state'],selected['index'])
    return RetailWorkspace(host,selected)
