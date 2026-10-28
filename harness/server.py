"""One workspace host for the Rocoto harness and its selected vertical."""
from __future__ import annotations

import layout
layout.install()

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import threading
import time
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ConfigDict

from agent_bridge import HarnessAgent
from agent_auth import AgentAuth
from chat_files import ChatFiles
from plugin_catalogue import catalogue as plugin_catalogue, read_plugin
from plugin_routes import register_plugin_routes
from mcp_connections import MCPConnections, ConnectionError as MCPConnectionError
from custom_connectors import CustomConnectors
from custom_execution import CustomExecution
from custom_setup import CustomSetup
from custom_routes import register_custom_routes
from document_previews import DocumentPreviews
from document_preview_routes import register_document_preview_routes
from document_preview_readiness import DocumentPreviewReadiness
from business_context import BusinessStore
from company_usage import CompanyUsage
from skills import SkillStore
from chat_permissions import ChatPermissions, workspace_writable
from access import AccessStore, COOKIE, LEGACY_COOKIE, DAY, page_request
from background_agents import AgentStore, AgentScheduler
from workspace_capabilities import WorkspaceCapabilities
from types import SimpleNamespace
from host import HostContext, Collections, BareWorkspace, bare_lifespan
from pack import active as active_vertical
from transfer_budget import TransferBudget, TransferBudgetMiddleware


HERE = layout.ROOT

def web_asset(relative):
    """Resolve shared shell assets or the active vertical's own web assets."""
    shared = HERE / "web" / relative
    return shared if shared.is_file() else layout.vertical_root() / "web" / relative

DEFAULT_STATE = Path(os.environ.get("ROCOTO_STATE_DIRECTORY", str(Path.home() / ".local/state/rocoto/harness" / layout.selected_vertical())))


class ManagedConnection:
    """Read-only access verification for the commissioned OpenAI-managed core."""

    def __init__(self, state, *, timeout=3, max_age=15, auth=None, agent=None, api_transport=None):
        self.state, self.timeout, self.max_age = Path(state), timeout, max_age
        self.auth = auth or AgentAuth(state)
        self.agent, self.api_transport = agent, api_transport
        self.lock = asyncio.Lock()
        self.checked_monotonic = self.cached = None

    async def status(self):
        from managed_codex import ManagedCodexClient, ManagedCodexError
        async with self.lock:
            if (self.cached is not None and self.checked_monotonic is not None
                    and time.monotonic() - self.checked_monotonic < self.max_age):
                return self.cached.copy()
            policy = self.auth.status()
            authenticated, status = False, 'unavailable'
            message = 'Commission the Rocoto OpenAI Agents API connection.'
            if policy['mode'] == 'openai_api':
                key = self.auth.environment.get('ROCOTO_OPENAI_API_KEY', '')
                if not key or any(char.isspace() for char in key):
                    status, message = 'rocoto_api_key_required', policy['message']
                else:
                    try:
                        async with asyncio.timeout(self.timeout):
                            async with ManagedCodexClient(key, timeout=self.timeout, transport=self.api_transport) as client:
                                page = await client.list_sessions(limit=1)
                                if not isinstance(page.get('data'), list) or type(page.get('has_more')) is not bool:
                                    raise ValueError('Read a valid session access response.')
                        authenticated, status = True, 'authenticated'
                        message = ('OpenAI verified access to the managed Agents API.'
                            if policy['inference_enabled'] else policy['message'])
                    except TimeoutError:
                        status, message = 'timeout', 'The OpenAI connection check timed out. The next status check will retry.'
                    except ManagedCodexError as error:
                        status = 'unauthorized' if error.status_code in {401, 403} else 'unavailable'
                        message = ('Reconnect the Rocoto OpenAI API credential to restore Agents API access.'
                            if status == 'unauthorized' else 'OpenAI Agents API access needs a fresh check.')
                    except (ValueError, OSError):
                        status, message = 'error', 'OpenAI Agents API access needs a fresh check.'
            self.checked_monotonic = time.monotonic()
            self.cached = {'host': socket.gethostname(), 'managed_authenticated': authenticated,
                'checked_at': datetime.now(timezone.utc).isoformat(), 'check_status': status,
                'check_max_age_seconds': self.max_age, 'route': 'OpenAI-managed Agents API',
                'auth_mode': 'openai_api', 'execution': 'openai_hosted',
                'inference_enabled': authenticated and policy['mode'] == 'openai_api' and policy['inference_enabled'],
                'message': message, 'verification_kind': 'managed_session_read_access', 'inference_verified': False}
            return self.cached.copy()


class LoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)

class EnvironmentRequest(BaseModel):
    environment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")



class SkillInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_id: str = Field(min_length=1, max_length=100)
    expected_revision: int | None = Field(default=None, ge=1)


class ActivationRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=6, ge=1, le=24)
    dataset: str | None = None


class AgentSetup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    agent_kind: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]{0,63}$')
    input_kind: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]{0,63}$')
    mode: str = Field(pattern=r'^(ad_hoc|scheduled|ongoing)$')
    agent_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{1,100}$')
    template_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{1,100}$')


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    chat_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    thread_id: str | None = None
    dataset: str | None = None
    attachments: list[str] = Field(default_factory=list, max_length=8)
    clip_attachments: list[str] = Field(default_factory=list, max_length=12)
    skill_ids: list[str] = Field(default_factory=list, max_length=8)
    followup_run_chat_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    agent_setup: AgentSetup | None = None


class SteerRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    attachments: list[str] = Field(default_factory=list, max_length=8)
    clip_attachments: list[str] = Field(default_factory=list, max_length=12)
    skill_ids: list[str] = Field(default_factory=list, max_length=8)


class BusinessScope(BaseModel):
    collection: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


class BusinessProfileRequest(BusinessScope):
    expected_revision: int = Field(ge=0)
    profile: dict


class CustomConnectorRequest(BusinessScope):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="",max_length=100)
    description: str = Field(min_length=1,max_length=8000)
    kind: str = Field(pattern=r"^(sql|api|sftp|cli|mcp|local|other)$")


class UsageSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    monthly_limit_usd: str


class ObserverRequest(BaseModel):
    environment_id: str
    principal_id: str






class OAuthCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str = Field(min_length=1, max_length=2000)
    code: str | None = Field(default=None, max_length=8000)
    error: str | None = Field(default=None, max_length=200)


class ChatMetadataRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    pinned: bool | None = None
    archived: bool | None = None


class FileTitleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)


class FileOpenedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")




def create_app(state=DEFAULT_STATE, index=None, *, messaging_provider_factory=None, phone_spend_factory=None, agent_factory=None, index_factory=None, read_agent_factory=None, managed_runtime_factory=None, builtin_provider_factories=None):
    if managed_runtime_factory is None:
        from managed_runtime import configured_runtime
        managed_runtime_factory=configured_runtime
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    public_origin = os.environ.get("ROCOTO_PUBLIC_ORIGIN", os.environ.get("ROCOTO_VIDEO_PUBLIC_ORIGIN", ""))
    allowed_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
    if public_origin:
        parsed = urlparse(public_origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("Configure the application's exact HTTPS origin.")
        allowed_hosts.add(parsed.hostname.lower())
    demo_flag = os.environ.get("ROCOTO_BROWSE_SESSIONS", os.environ.get("ROCOTO_VIDEO_DEMO_BROWSING"))
    if demo_flag is not None and demo_flag.strip().lower() not in {"0", "1", "false", "true"}:
        raise ValueError("Set ROCOTO_BROWSE_SESSIONS to true or false.")
    demo_browsing = (not public_origin if demo_flag is None
                     else demo_flag.strip().lower() in {"1", "true"})
    vertical = active_vertical()
    vertical_host = vertical.import_host()
    agent_factory = agent_factory or HarnessAgent
    if read_agent_factory is None:
        from agent_bridge import HarnessAgent as ReadAgent
        read_agent_factory = ReadAgent
    slots = asyncio.Semaphore(2)

    cleanup_tasks=set()

    async def ready_workspace(selected):
        lock=selected.setdefault('_ready_lock',asyncio.Lock())
        async with lock:
            if selected.get('_ready'):
                return
            if selected.get('custom_coordinator'):
                await selected['custom_coordinator'].ready()
            async def recover_managed():
                await app.state.serving.wait()
                await selected['signal_dispatcher'].recover_pending()
                await selected['scheduler'].recover_pending()
                await selected['agent'].managed_runtime.recover_pending(selected['agent'])
            selected['managed_recovery']=asyncio.create_task(recover_managed())
            selected['_ready']=True

    async def close_workspace(selected):
        recovery=selected.get('managed_recovery')
        if recovery is not None:
            recovery.cancel()
            await asyncio.gather(recovery,return_exceptions=True)
        dispatcher=selected.get('signal_dispatcher')
        if dispatcher:await dispatcher.close()
        scheduler=selected.get('scheduler')
        if scheduler:await scheduler.close()
        agent=selected.get('agent')
        running=list(getattr(agent,'child_tasks',{}).values())
        for pending in running:pending.cancel()
        await asyncio.gather(*running,return_exceptions=True)
        for name in ('custom_coordinator','vertical','phone_delivery','messaging','connections'):
            resource=selected.get(name)
            if resource is not None and callable(getattr(resource,'close',None)):
                await resource.close()

    def abandon_workspace(selected):
        try:loop=asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(close_workspace(selected))
        else:
            task=loop.create_task(close_workspace(selected))
            cleanup_tasks.add(task)
            task.add_done_callback(cleanup_tasks.discard)

    @asynccontextmanager
    async def lifespan(app):
        async def tick():
            next_workspace_scan=0
            while True:
                if time.time()>=next_workspace_scan:
                    next_workspace_scan=time.time()+30
                    for environment in await asyncio.to_thread(pending_signal_workspaces):
                        try:workspace(environment)
                        except (ValueError,OSError,sqlite3.Error):continue
                for selected in list(workspaces.values()):
                    try:
                        await ready_workspace(selected)
                        await selected['signal_dispatcher'].tick()
                        await phone_tick(selected)
                        await messaging_tick(selected)
                        await selected["vertical"].tick()
                    except Exception:
                        # A failed tick grants no additional work; durable state permits review.
                        continue
                await asyncio.sleep(1)
        async with AsyncExitStack() as resources:
            await resources.enter_async_context(vertical_host.lifespan(app, host) if vertical_host else bare_lifespan(app, host))
            polling = None
            try:
                for selected in workspaces.values():
                    await ready_workspace(selected)
                polling = asyncio.create_task(tick())
                if app.state.mcp_host.transport is not None:
                    await resources.enter_async_context(app.state.mcp_host.transport.lifespan())
                await resources.enter_async_context(app.state.managed_mcp_host.lifespan())
                app.state.serving.set()
                yield
            finally:
                app.state.serving.clear()
                if polling:
                    polling.cancel()
                    with suppress(asyncio.CancelledError):
                        await polling
                await resources.aclose()
                for selected in workspaces.values():
                    await close_workspace(selected)
                await asyncio.gather(*cleanup_tasks,return_exceptions=True)

    app = FastAPI(title="Rocoto", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.serving=asyncio.Event()
    app.state.transfer_budget = TransferBudget(state / "transfer-budget.json")
    # Initialization is explicit and validates an existing ledger. A missing
    # or altered ledger during serving fails closed in the middleware.
    app.state.transfer_budget.initialize()
    app.add_middleware(TransferBudgetMiddleware, budget=app.state.transfer_budget)
    from source_icons import SourceIcons
    app.state.source_icons = SourceIcons()
    rich_vendors = json.loads((HERE / "web/vendor/rich-content-vendors.json").read_text())
    rich_vendor_paths = {item["path"] for package in rich_vendors["packages"] for item in package["files"]}
    access = AccessStore(state)
    app.state.access = access
    operator_name = " ".join(os.environ.get("ROCOTO_OPERATOR_NAME", os.environ.get("ROCOTO_VIDEO_OPERATOR_NAME", "Workspace owner")).split())[:80] or "Workspace owner"
    operator = {"name":operator_name, "mode":"founder_demo"}
    workspaces = {}

    def scope_member(selected, principal):
        from contextlib import closing
        import sqlite3
        try:
            with closing(sqlite3.connect(access.path.as_uri()+'?mode=ro',uri=True,timeout=0)) as db:
                row=db.execute('SELECT p.id,p.display_name,e.role FROM principals p JOIN principal_environments e '
                    'ON e.principal_id=p.id WHERE p.id=? AND e.environment=?',
                    (principal,selected['environment_id'])).fetchone()
                return {'id':row[0],'display_name':row[1],'role':row[2]} if row and row[2] in {'owner','operator','browse'} else None
        except sqlite3.Error:return None

    def scope_members(selected):
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(access.path.as_uri()+'?mode=ro',uri=True,timeout=0)) as db:
            return [dict(id=r[0],display_name=r[1],role=r[2]) for r in db.execute(
                'SELECT p.id,p.display_name,e.role FROM principals p JOIN principal_environments e ON e.principal_id=p.id '
                'WHERE e.environment=? ORDER BY p.display_name,p.id',(selected['environment_id'],))]

    def install_scheduler(selected, environment_id):
        selected["environment_id"] = environment_id
        if Path(getattr(selected['agent'],'state',selected['state'])).absolute()!=Path(selected['state']).absolute():
            raise ValueError('Use the agent runtime of the owning workspace.')
        selected['agent'].state=selected['state']
        selected["auth"] = getattr(selected["agent"], "auth", None) or AgentAuth(selected["state"])
        selected["agent"].auth = selected["auth"]
        selected["agent"].managed_mcp_host=app.state.managed_mcp_host
        from native_packages import NativePackageStore
        selected['native_packages'] = NativePackageStore(selected['state'])
        selected['agent'].plugin_package_store = selected['native_packages']
        selected["connection"] = ManagedConnection(selected["state"],auth=selected["auth"],agent=selected["agent"])
        selected['files'] = ChatFiles(selected['state'])
        selected['agent'].file_store = selected['files']
        selected['document_previews'] = DocumentPreviews(selected['files'])
        selected['document_preview_readiness'] = DocumentPreviewReadiness(
            enabled=os.environ.get('ROCOTO_DOCUMENT_PREVIEWS_ENABLED') == '1',
            receipt_sha256=os.environ.get('ROCOTO_DOCUMENT_PREVIEW_RECEIPT_SHA256', ''))
        from provider_configuration import registered_clients_for, remote_providers_for, commissioned_writes_for
        registered_clients = registered_clients_for(selected['state'], public_origin=public_origin)
        remote_providers = remote_providers_for(selected['state'], public_origin=public_origin)
        write_providers = commissioned_writes_for(
            (value.strip() for value in os.environ.get(
                'ROCOTO_CONNECTION_WRITE_PROVIDERS', os.environ.get('ROCOTO_NATIVE_WRITE_PROVIDERS', '')).split(',') if value.strip()), registered_clients, remote_providers)
        if write_providers and app.state.managed_mcp_host.transport is None:
            raise RuntimeError('Commission the managed workspace endpoint for provider actions.')
        selected["connections"] = MCPConnections(selected["state"], public_origin=public_origin,
            write_providers=write_providers, registered_clients=registered_clients, remote_providers=remote_providers)
        selected["connections"].recover()
        selected["agent"].connection_store = selected["connections"]
        selected["custom_connectors"] = CustomConnectors(selected["state"])
        selected["agent"].custom_connector_store = selected["custom_connectors"]
        selected['custom_execution'] = CustomExecution(selected['custom_connectors'])
        if os.environ.get('ROCOTO_CUSTOM_EXECUTOR_ENABLED') == '1':
            from custom_runtime import CustomRuntime
            selected['custom_coordinator'] = CustomRuntime(selected['custom_connectors'])
            selected['custom_execution'] = selected['custom_coordinator'].manager
            selected['agent'].custom_coordinator = selected['custom_coordinator']
        selected['custom_setup'] = CustomSetup(selected['custom_execution'])
        selected['agent'].custom_execution_manager = selected['custom_execution']
        selected['collections'] = Collections()
        selected['agents'] = AgentStore(selected['state'],agent_classes=vertical.agent_classes())
        selected['agent'].automation_store = selected['agents']
        from builtin_capabilities import BuiltInCapabilities
        factories = dict(builtin_provider_factories or {})
        if messaging_provider_factory is not None:
            factories['whatsapp'] = messaging_provider_factory
        selected['builtins'] = BuiltInCapabilities(selected['state'], environment_id,
            vertical.builtin_capabilities(), factories=factories)
        selected['agent'].builtin_capabilities = selected['builtins']
        selected['phone_enabled'] = any(selected['builtins'].enabled(name) for name in ('whatsapp', 'viber'))
        selected['messaging_enabled'] = any(vertical.builtin_capabilities().values())
        selected['capabilities'] = WorkspaceCapabilities(connections=selected['connections'],
            custom_connectors=selected['custom_connectors'],custom_execution=selected['custom_execution'],
            inputs=lambda:selected['agents'].inputs, builtins=selected['builtins'])
        selected['agent'].workspace_capabilities = selected['capabilities']
        selected['business'] = BusinessStore(selected['state'],collections=selected['collections'])
        selected['agent'].business_store = selected['business']
        from business_maps import BusinessMaps, current_member
        selected['business_maps'] = BusinessMaps(selected['state'])
        selected['agent'].business_map_store = selected['business_maps']
        selected['agent'].business_map_authorized = lambda owner: current_member(access.path, environment_id, owner)
        from organization_chats import OrganizationChats
        from messaging_composition import compose_messaging
        from messaging_signals import reserve_send
        from workspace_identity import workspace_scope
        selected['organization_chats'] = OrganizationChats(selected['state'], environment_id=environment_id,
            member=lambda principal: current_member(access.path, environment_id, principal),
            records=lambda: selected['agent'].children)
        selected['agent'].organization_chats = selected['organization_chats']
        selected["chats"] = ChatPermissions(selected["state"], organization=selected['organization_chats'])
        spend = None
        if selected['phone_enabled']:
            spend_factory = phone_spend_factory
            if spend_factory is None:
                from messaging_config import spend_for
                spend_factory = spend_for
            spend = spend_factory(selected['state'], environment_id)
        def phone_spending_ready():
            from phone_spend import record
            allowance = record(spend)
            return (allowance['stopped'] is False and allowance['company_daily_messages'] > 0
                    and allowance['agent_daily_messages'] > 0)
        for name in ('whatsapp', 'viber'):
            selected['builtins'].spending[name] = phone_spending_ready
        selected['messaging'] = compose_messaging(selected['builtins'],
            authorized=lambda actor: messaging_authorized(selected, actor), spend=spend,
            send_admission=lambda actor, row: reserve_send(selected['agents'], actor, row))
        if selected['messaging_enabled']:
            selected['messaging'].recover()
        selected['agent'].messaging_store = selected['messaging']
        selected['agent'].messaging_member = lambda principal: messaging_member(selected, principal)
        from scope_host import setup_scope
        setup_scope(selected, scope_member, scope_members)
        selected["skills"] = SkillStore(selected["state"])
        selected["agent"].skill_store = selected["skills"]
        selected["usage"] = CompanyUsage(selected["state"])
        selected["api_usage"] = CompanyUsage.agent_pool(selected["state"])
        store = selected['agents']
        # Phone chat is a default capability of every Tracking Agent: its
        # participants, bindings and contact cards live beside the verified numbers.
        from phone_chat import PhoneChat
        from phone_delivery import PhoneDelivery, window_lane
        def phone_entry(chat_id, entry):
            """Write a contact card or an alert into its Tracking Agent chat, replacing its earlier state."""
            agent = selected['agent']
            record = agent.children.get(chat_id)
            if not record or record.get('kind') != 'agent_conversation' or record.get('parent_id'):
                return False
            if entry.get('alert'):
                key = (entry['alert']['id'], entry['alert']['revision'])
                same = lambda item: ((item.get('alert') or {}).get('id'), (item.get('alert') or {}).get('revision')) == key
            else:
                same = lambda item: (item.get('contact_request') or {}).get('id') == entry['contact_request']['id']
            for position, item in enumerate(record['messages']):
                if same(item):
                    record['messages'][position] = {**item, **entry}
                    break
            else:
                pending = getattr(agent, 'child_tasks', {}).get(chat_id)
                if entry.get('alert') and (chat_id in getattr(agent, 'active', {}) or record.get('running')
                        or record.get('active_run_id') or pending is not None and not pending.done()):
                    return False  # an alert joins the conversation once its active turn has drained
                record['messages'].append(entry)
            agent._save_child(record)
            return True
        selected['phone'] = PhoneChat(selected['messaging'], agents=store,
            member=lambda principal: messaging_member(selected, principal), post=phone_entry)
        selected['agent'].phone_chat = selected['phone']
        # A participant opens the Tracking Agent's own conversation in the app too. The
        # location-assignment guard of ChatPermissions applies to the participant themself.
        selected['chats'].participant_authorized = lambda record, principal: (
            record.get('agent_id') is not None and selected['phone'].participant_chat(record['id'], principal))
        # Host alert delivery: a Tracking Agent's processed alert reaches each participant's
        # phone with no model turn. It runs beside the wake and leaves the agent's run as it is.
        selected['phone_delivery'] = PhoneDelivery(selected['messaging'], selected['phone'],
            origin=public_origin, post=phone_entry)
        selected['agent'].phone_delivery = selected['phone_delivery']
        selected['builtins'].host_ready.update(whatsapp=True, viber=True)
        selected['vertical'] = BareWorkspace(selected)
        selected['agent'].vertical = selected['vertical']
        if vertical_host:
            selected['vertical'] = vertical_host.install_workspace(host, selected)
            selected['agent'].vertical = selected['vertical']
        selected['collections'].seal()
        from tool_families import build_registry
        selected['agent'].tool_registry = build_registry(selected['agent'],
            families=vertical.import_tool_families(),definition_model=store.definition_model)
        selected['agent'].managed_runtime=managed_runtime_factory(selected['agent'])
        def current_agent_authority(definition, actor):
            member=messaging_member(selected,actor)
            child=selected['agent'].children.get(definition['chat_id'])
            if (member is None or not child or child.get('owner_principal_id')!=definition.get('owner_principal_id')
                    or child.get('dataset')!=definition['dataset'] or child.get('parent_id')
                    or not selected['collections'].contains(definition['dataset'],actor)):
                return False
            session={'authenticated':True,'can_query':True,'role':member['role'],'principal':member,
                'environment':{'id':selected['environment_id']}}
            return selected['chats'].can_write(child,session)
        store.install_authority(current_agent_authority)
        def current_signal_authority(signal):
            actor=signal.get('actor_principal_id')
            source_id=signal.get('source_id')
            if source_id:
                source=selected['signal_sources'].get(source_id)
                if (source['status'] not in {'active','done'} or source['authority_until']<=time.time()
                        or any(source.get(key)!=signal.get(key) for key in
                            ('owner_principal_id','actor_principal_id','chat_id','collection'))):return False
            reply_id=signal.get('payload',{}).get('messaging_reply_id')
            if reply_id:
                check=getattr(selected['messaging'],'signal_reply_current',None)
                if not callable(check) or not check(reply_id,signal):return False
            member=messaging_member(selected,actor)
            child=selected['agent'].children.get(signal.get('chat_id'))
            if (member is None or not child or child.get('owner_principal_id')!=signal.get('owner_principal_id')
                    or child.get('dataset')!=signal.get('collection') or child.get('parent_id')
                    or not selected['collections'].contains(signal.get('collection'),actor)):
                return False
            session={'authenticated':True,'can_query':True,'role':member['role'],'principal':member,
                'environment':{'id':selected['environment_id']}}
            return selected['chats'].can_converse(child,session)
        store.signal_authority=current_signal_authority
        from signal_sources import SignalSources
        from signal_dispatcher import SignalDispatcher
        selected['signal_sources']=SignalSources(store,public_origin=public_origin,environment=environment_id)
        selected['agent'].signal_sources=selected['signal_sources']
        from signal_endpoints import SignalEndpoints
        selected['signal_endpoints']=SignalEndpoints(store,
            authorized=lambda record:current_signal_authority(record)
                and (messaging_member(selected,record.get('actor_principal_id')) or {}).get('role')=='owner',
            owner_authorized=lambda actor:bool((messaging_member(selected,actor) or {}).get('role')=='owner'))
        selected['agent'].signal_endpoints=selected['signal_endpoints']
        from signal_screening import SignalScreening
        selected['signal_screening']=SignalScreening(store,
            pool=CompanyUsage.openai_pool(selected['state']),
            api_key=lambda:getattr(getattr(selected['agent'],'auth',None),'environment',{}).get('ROCOTO_OPENAI_API_KEY',''))
        selected['scheduler'] = AgentScheduler(store,selected['agent'],slots=slots)
        selected['signal_dispatcher']=SignalDispatcher(store,selected['agent'],selected['scheduler'],slots,selected['signal_sources'])

    def workspace(environment):
        if environment not in workspaces:
            root = state if environment == 'demo' else state / 'environments' / environment
            root.mkdir(mode=0o700,parents=True,exist_ok=True)
            selected = {'state':root,'agent':agent_factory(root)}
            try:
                install_scheduler(selected,environment)
            except BaseException:
                abandon_workspace(selected)
                raise
            workspaces[environment]=selected
        return workspaces[environment]

    host=HostContext(state,public_origin,access,workspaces,workspace,scope_member,scope_members,
        app=app,options={'operator':operator,'index':index,**({'index_factory':index_factory} if index_factory else {})})
    # Finish declaring identity callbacks before constructing the first workspace.
    app.state.workspaces = workspaces

    # External MCP reads reuse a loaded workspace index. A cold company needs
    # only retrieval services, not chat recovery, a scheduler or a Codex process.
    from mcp_host import MCPHost
    from public_mcp import HTTPMCPServices
    from starlette.routing import Route
    mcp_services = {}
    mcp_services_lock = threading.Lock()

    def services_for_environment(environment):
        selected=workspaces.get(environment)
        if selected is not None:
            return HTTPMCPServices(selected['state'],agent=selected['agent'])
        with mcp_services_lock:
            if environment not in mcp_services:
                if len(mcp_services)>=64:
                    raise ValueError('The service is processing its current workspaces.')
                from business_scope import WorkspaceAuthority
                from workspace_identity import workspace_scope
                from tool_families import build_registry
                root=state/'environments'/environment
                reader=read_agent_factory(root,tool_only=True)
                cold={'state':root,'environment_id':environment,'agent':reader,'collections':Collections()}
                cold['business_scope']=WorkspaceAuthority(root,company_id=workspace_scope(root),
                    member=lambda principal,cold=cold:scope_member(cold,principal))
                cold['business']=BusinessStore(root,collections=cold['collections'])
                reader.business_store=cold['business']
                cold['agents']=AgentStore(root,agent_classes=vertical.agent_classes())
                reader.automation_store=cold['agents']
                cold['vertical']=BareWorkspace(cold)
                if vertical_host:
                    factory=getattr(vertical_host,'read_workspace',None)
                    if not callable(factory):
                        raise ValueError('This vertical supplies authenticated read services.')
                    cold['vertical']=factory(host,cold)
                reader.vertical=cold['vertical']
                cold['collections'].seal()
                cold['agents'].seal_inputs()
                reader.tool_registry=build_registry(reader,families=vertical.import_tool_families(),
                    definition_model=cold['agents'].definition_model)
                mcp_services[environment]=HTTPMCPServices(root,agent=reader)
            return mcp_services[environment]

    app.state.mcp_host = MCPHost(state, access, services_for_environment, public_origin=public_origin)
    app.state.mcp_host.register_routes(app)
    from managed_mcp import ManagedMCPHost
    app.state.managed_mcp_host=ManagedMCPHost(state,public_origin=public_origin)
    if app.state.managed_mcp_host.transport is not None:
        app.router.routes.append(Route('/runtime/mcp',app.state.managed_mcp_host.transport,methods=['GET','POST','DELETE']))
    from phone_routes import register_phone_routes
    register_phone_routes(app, member=lambda selected, principal: messaging_member(selected, principal),
        members=lambda selected: messaging_members(selected))
    from harness_routes import register_harness_routes
    register_harness_routes(app)
    if app.state.mcp_host.transport is not None:
        app.router.routes.append(Route('/mcp', app.state.mcp_host.transport, methods=['GET', 'POST', 'DELETE']))

    @app.middleware("http")
    async def local_operator_only(request: Request, call_next):
        # Prevent DNS rebinding and cross-site requests to Peter's authenticated agent.
        hostname = (request.url.hostname or "").lower()
        if hostname not in allowed_hosts:
            return Response("Use the configured Rocoto address.", status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return Response("Open this action from the demo.", status_code=403)
        route_parts = request.url.path.split('/')
        signed_messaging = ((request.url.path.startswith('/hooks/in/') and len(route_parts) == 5
            or request.url.path.startswith('/hooks/') and len(route_parts) == 4 and route_parts[2] in {'whatsapp','viber','email','voice'}) or request.url.path.startswith('/api/hooks/') and len(route_parts) == 5
            or request.url.path.startswith('/api/messaging/webhook/') and (len(route_parts) == 5
                or len(route_parts) == 6 and route_parts[-1] in {'whatsapp', 'viber', 'email', 'voice'}))
        external_mcp = ((request.url.path == '/mcp' and app.state.mcp_host.transport is not None)
            or (request.url.path == '/runtime/mcp' and app.state.managed_mcp_host.transport is not None))
        if not external_mcp and not signed_messaging and request.method in {"POST", "DELETE", "PATCH", "PUT"} and not page_request(request.headers):
            return Response("Use the demo controls.", status_code=403)
        maximum=host.options.get('request_body_limit',lambda request:None)(request)
        if maximum is not None:
            chunks=[];size=0
            async for chunk in request.stream():
                size+=len(chunk)
                if size>maximum:
                    return Response('Choose a file within the upload size limit.',status_code=413)
                chunks.append(chunk)
            request._body=b''.join(chunks)
        session = None if external_mcp else access.session((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE)))
        # A copied local browse cookie must not grant access on an invite-only host.
        if session and session.get("role") == "browse" and not demo_browsing:
            session = None
        request.state.session = session
        path = request.url.path
        public = path.startswith(("/vendor/","/connection-brands/")) or path in {"/", "/healthz", "/app.js", "/business-summary.js", "/business-summary.css", "/settings-shell.js", "/settings-shell.css", "/assets/monet-water-lilies.jpg", "/assets/chat-dreamscape.png", "/assets/search-dreamscape.png", "/style.css", "/theme.css", "/brand/rocoto-mark.svg", "/fonts/fraunces/fraunces-latin-500-700.woff2", "/api/session", "/api/session/demo"}
        public = public or (request.method in {"GET", "HEAD"} and path.startswith(("/static/harness/", "/static/vendor/")))
        public = public or path in {"/tool-trail.js", "/tool-trail.css", "/rich-content.js", "/rich-content.css", "/rich-diagram.html", "/rich-diagram.js"}
        public = public or path in {"/harness-shell.js", "/workspace-tabs.js", "/workspace-extensions.js", "/wake-controls.js", "/wake-controls.css", "/workspace-tabs.css", "/workspace-home.js", "/workspace-home.css"}
        public = public or path in {"/display-charts.js", "/display-charts.css", "/display-canvas.js", "/display-canvas.css"}
        public = public or path in {"/profile-settings.js", "/profile-settings.css", "/plugin-settings.js", "/plugin-settings.css", "/messaging-settings.js", "/messaging-settings.css"}
        public = public or path in {"/workspace-settings.js", "/workspace-settings.css"}
        public = public or path in {"/business-map.js", "/business-map.css"}
        public = public or (request.method in {"GET", "HEAD"} and path in {
            "/api/connections/oauth/callback", "/oauth-callback.js"})
        public = public or host.options.get('public_request',lambda request:False)(request)
        public = public or external_mcp or signed_messaging
        if not public:
            if not session:
                return Response("Enter your company access code.",status_code=401)
            if (session.get("viewing_as") and request.method not in {"GET","HEAD"}
                    and path not in {"/api/observer","/api/session"}):
                return Response("Return to your workspace to make changes.",status_code=403)
            question_action = (path in {"/api/chat", "/api/search", "/api/attachments"}
                               or (path.startswith("/api/agents") and path != "/api/agents/catalogue")
                               or (path.startswith(("/api/business","/api/usage","/api/skills")) and request.method == "POST")
                               or host.options.get("query_request",lambda request:False)(request)
                               or path.startswith("/api/files")
                               or path.startswith("/api/inspection-previews/")
                               or path.startswith(("/api/connections", "/api/messaging", "/api/organization/chats"))
                               or path.startswith("/api/chats/")
                               or path.endswith(("/steer", "/stop")))
            observer_read = bool(session.get("viewing_as") and request.method in {"GET","HEAD"})
            if question_action and not session["can_query"] and not observer_read:
                return Response("Enter your access code to ask questions.",status_code=403)
            request.state.workspace = workspace(session["environment"]["id"])
            await ready_workspace(request.state.workspace)
        selected = getattr(request.state, "workspace", None)
        # Fixed application bytes retain the login/workspace checks above. They
        # contain no tenant data, so reading them need not rescan every recording
        # at admission, after the response and for each streamed file chunk.
        static_viewer_code = host.options.get("static_request",lambda path,method:False)(path,request.method)
        if selected is not None and not static_viewer_code:
            principal = selected["chats"].principal(session)
            from scope_host import scope_current
            request.state.scope_views=[]
            admitted_member=scope_member(selected,principal) if principal else None
            admitted_role=(admitted_member or {}).get('role')
            request.state.scope_guards=[lambda: admitted_role is not None and
                (scope_member(selected,principal) or {}).get('role')==admitted_role] if principal else []
            scope_change=host.options.get('scope_change',lambda request:False)(request)
            async def permitted(*, resources=True):
                return ((not resources or all(await asyncio.gather(
                    *(asyncio.to_thread(scope_current,item) for item in request.state.scope_views))))
                    and all(check() for check in request.state.scope_guards))
            async with AsyncExitStack() as scope_stack:
                try:
                    await scope_stack.enter_async_context(selected['vertical'].request_scope(request))
                except (ValueError, PermissionError):
                    return JSONResponse({'error':'Refresh your current workspace access.'},headers={'Cache-Control':'private, no-store'},status_code=403)
                if not await permitted():
                    return JSONResponse({'error':'Refresh your current workspace access.'},headers={'Cache-Control':'private, no-store'},status_code=403)
                result = await call_next(request)
            if not await permitted(resources=not scope_change):
                return JSONResponse({'error':'Refresh your current workspace access.'},headers={'Cache-Control':'private, no-store'},status_code=403)
            original_body=result.body_iterator
            async def guarded_body():
                async for chunk in original_body:
                    if not await permitted(resources=not scope_change):break
                    yield chunk
            result.body_iterator=guarded_body()
        else:
            result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https://raw.githubusercontent.com; media-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'")
        return result

    def connection_identity(request, *, writable=False):
        if writable and not workspace_writable(request.state.session):
            raise HTTPException(403, "Return to your workspace to connect an account.")
        principal = request.state.workspace["chats"].principal(request.state.session)
        if not principal:
            raise HTTPException(403, "Sign in to view your connections.")
        import hashlib
        binding = hashlib.sha256((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE)).encode()).hexdigest()
        return principal, binding

    def messaging_member(selected, principal):
        import sqlite3
        try:
            with sqlite3.connect(access.path.as_uri() + '?mode=ro', uri=True, timeout=0) as db:
                row = db.execute('SELECT p.id,p.display_name,e.role FROM principals p JOIN principal_environments e '
                    'ON e.principal_id=p.id WHERE p.id=? AND e.environment=?',
                    (principal, selected['environment_id'])).fetchone()
                return {'id':row[0], 'display_name':row[1], 'role':row[2]} if row and row[2] in {'owner','operator'} else None
        except sqlite3.Error:
            return None

    def messaging_members(selected):
        """The company's current owners and operators, the two roles a participant holds."""
        import sqlite3
        with sqlite3.connect(access.path.as_uri() + '?mode=ro', uri=True, timeout=0) as db:
            return [{'id':row[0], 'display_name':row[1], 'role':row[2]} for row in db.execute(
                'SELECT p.id,p.display_name,e.role FROM principals p JOIN principal_environments e '
                'ON e.principal_id=p.id WHERE e.environment=? AND e.role IN (\'owner\',\'operator\') '
                'ORDER BY p.display_name,p.id', (selected['environment_id'],))]

    def messaging_authorized(selected, actor):
        member = messaging_member(selected, actor.member_id)
        requester = messaging_member(selected, actor.actor_id)
        record = selected['agent'].children.get(actor.chat_id, {})
        if not member or not requester or actor.company_id != selected['organization_chats'].company_id:
            return False
        if actor.actor_id != actor.member_id and requester['role'] != 'owner':
            return False
        if record.get('dataset') != actor.collection or record.get('parent_id'):
            return False
        if selected['organization_chats'].owns(record):
            return True
        owned = record.get('owner_principal_id') == actor.member_id
        if record.get('kind') != 'agent_conversation':
            return owned
        # A saved agent's own chat belongs to its creator and, for a Tracking Agent, to each
        # current participant, while that member's own location assignments cover the
        # conversation's scope. The creator is held to the same check as everyone they added.
        if not owned and (actor.actor_id != actor.member_id
                or not selected['phone'].participant_chat(actor.chat_id, actor.member_id)):
            return False
        import scope_host
        return scope_host.history_allowed(selected, record, actor.member_id) is True

    async def messaging_body(request, allowed, required):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, 'Send the messaging setup fields.')
        try:
            body = json.loads(raw)
            if (not isinstance(body, dict) or set(body) - allowed or required - set(body)
                    or any(not isinstance(v,str) or not 1 <= len(v) <= 4096 or any(ord(c)<32 for c in v) for v in body.values())):
                raise ValueError()
            return body
        except (ValueError, TypeError):
            raise HTTPException(400, 'Send the messaging setup fields.') from None

    def organization_identity(request, collection, *, write=False):
        selected, session = request.state.workspace, request.state.session
        principal = selected['chats'].principal(session)
        if (not principal or not messaging_member(selected, principal)
                or not messaging_member(selected, session['principal']['id'])
                or write and not workspace_writable(session)):
            raise HTTPException(403, 'Open your current company workspace.')
        if not selected['collections'].contains(collection,principal):
            raise HTTPException(400, 'Choose a business collection.')
        return selected, principal

    async def messaging_actor(request, member_id, collection, *, write=False):
        from messaging import MessagingActor
        selected, principal = organization_identity(request, collection, write=write)
        member_id = member_id or principal
        requester = messaging_member(selected, principal)
        if not messaging_member(selected, member_id) or member_id != principal and requester['role'] != 'owner':
            raise HTTPException(403, 'Choose your messaging profile or an authorized company member.')
        record = await asyncio.to_thread(selected['organization_chats'].member_setup,
            selected['agent'], member_id, collection)
        return selected, MessagingActor(selected['organization_chats'].company_id, member_id,
            collection, record['id'], principal)

    async def messaging_projection(request, member_id, collection):
        selected, actor = await messaging_actor(request, member_id, collection)
        import sqlite3
        with sqlite3.connect(access.path.as_uri() + '?mode=ro', uri=True, timeout=0) as db:
            rows = db.execute('SELECT p.id,p.display_name,e.role FROM principals p JOIN principal_environments e '
                'ON e.principal_id=p.id WHERE e.environment=? AND e.role IN (\'owner\',\'operator\') '
                'ORDER BY p.display_name,p.id', (selected['environment_id'],)).fetchall()
        data = await asyncio.to_thread(selected['messaging'].describe, actor)
        contacts = []
        from messaging import MessagingActor
        is_owner = messaging_member(selected, actor.actor_id)['role'] == 'owner'
        for member in rows:
            if not is_owner and member[0] != actor.actor_id: continue
            setup = next((record for record in selected['agent'].children.values()
                if record.get('kind') == 'messaging_setup' and record.get('owner_principal_id') == member[0]
                and record.get('environment_id') == selected['environment_id']), None)
            if setup is None: continue
            scoped = MessagingActor(actor.company_id, member[0], setup['dataset'], setup['id'], actor.actor_id)
            summary = await asyncio.to_thread(selected['messaging'].describe, scoped)
            # One row per verified channel address; a member still verifying shows that channel's request.
            contacts.extend({key:contact[key] for key in ('member_id','phone','verified','channel')} for contact in summary['addresses'])
            verified, challenge = {contact['channel'] for contact in summary['addresses']}, summary['challenge']
            if challenge and challenge['channel'] not in verified and (
                    not verified or challenge['status'] in {'pending','sent','uncertain'}):
                contacts.append({'member_id':member[0], 'phone':challenge['phone'], 'verified':False,
                    'channel':challenge['channel']})
        if actor.member_id != actor.actor_id:
            data.update(challenge=None, outbox=[], waiting=False)
        return {**data, 'contacts':contacts, 'collection':collection, 'member_id':actor.member_id,
            'setup_chat_id':actor.chat_id, 'setup_chat_url':'/?chat=' + actor.chat_id,
            'members':await asyncio.to_thread(selected['vertical'].member_cards,actor.actor_id,collection,
                [{'id':r[0], 'name':r[1], 'display_name':r[1], 'role':r[2], 'is_self':r[0] == actor.actor_id} for r in rows]),
            'read_only':not workspace_writable(request.state.session)}

    @app.get('/api/organization/chats')
    async def organization_chats(request: Request, collection: str = 'company'):
        selected, principal = organization_identity(request, collection)
        from organization_chats import PURPOSES
        records = []
        for purpose in PURPOSES:
            record = await asyncio.to_thread(selected['organization_chats'].ensure, selected['agent'], collection, purpose)
            if selected['chats'].can_read(record,request.state.session):
                records.append(selected['chats'].get(record,request.state.session))
                request.state.scope_guards.append(lambda row=record:selected['chats'].can_read(row,request.state.session))
        return {'collection':collection, 'chats':records}

    @app.post('/api/organization/chats')
    async def organization_chat(request: Request):
        body = await messaging_body(request, {'collection','purpose'}, {'collection','purpose'})
        selected, principal = organization_identity(request, body['collection'], write=True)
        try:
            record = await asyncio.to_thread(selected['organization_chats'].ensure,
                selected['agent'], body['collection'], body['purpose'])
            if not selected['chats'].can_read(record,request.state.session):
                raise HTTPException(404,'Choose a conversation within your location assignments.')
            request.state.scope_guards.append(lambda:selected['chats'].can_read(record,request.state.session))
            return {**selected['chats'].get(record, request.state.session), 'url':'/?chat=' + record['id']}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get('/api/messaging')
    async def messaging_status(request: Request, member_id: str | None = None, collection: str = 'company'):
        return await messaging_projection(request, member_id, collection)

    @app.post('/api/messaging/verification')
    async def messaging_verification(request: Request):
        body = await messaging_body(request, {'phone','member_id','collection','request_id','channel'}, {'phone','member_id','collection'})
        # Each address is verified on its own channel. Without one the route selects WhatsApp, the built transport.
        channel = body.get('channel', 'whatsapp')
        selected, actor = await messaging_actor(request, body['member_id'], body['collection'], write=True)
        if channel not in selected['messaging'].channel_names:
            raise HTTPException(400, 'Choose a channel enabled for this workspace.')
        from messaging import MessagingError
        try:
            result = await selected['messaging'].request_code(actor, body['phone'], body.get('request_id'), channel=channel)
            return {**result, 'setup_chat_id':actor.chat_id}
        except MessagingError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post('/api/messaging/verification/confirm')
    async def messaging_confirm(request: Request):
        body = await messaging_body(request, {'challenge_id','code','member_id','collection'}, {'challenge_id','code','member_id','collection'})
        selected, actor = await messaging_actor(request, body['member_id'], body['collection'], write=True)
        from messaging import MessagingError
        try:
            result = await asyncio.to_thread(selected['messaging'].verify_code, actor, body['challenge_id'], body['code'])
            # The verification completed this member's contact cards; show that in their chats.
            import sqlite3
            with suppress(ValueError, OSError, sqlite3.Error):
                selected['phone'].publish(member_id=actor.member_id)
            return {**result, 'setup_chat_id':actor.chat_id}
        except MessagingError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post('/api/messaging/hello')
    async def messaging_hello(request: Request):
        body = await messaging_body(request, {'request_id','member_id','collection','channel'}, {'request_id','member_id','collection'})
        selected, actor = await messaging_actor(request, body['member_id'], body['collection'], write=True)
        from messaging import MessagingError
        try:
            result = await selected['messaging'].send_hello(actor, request_id=body['request_id'], channel=body.get('channel'))
            record = selected['agent'].children[actor.chat_id]
            if not any(item.get('channel_delivery', {}).get('id') == result['id'] for item in record['messages']):
                record['messages'].append({'role':'assistant', 'content':result.get('text') or '',
                    'channel_delivery':result, 'created_at':result['created_at']})
                record['waiting_for_reply'] = result['waiting_for_reply']
                selected['agent']._save_child(record)
            return {**result, 'setup_chat_id':actor.chat_id}
        except MessagingError as exc:
            raise HTTPException(400, str(exc)) from None

    def registered_workspace_root(environment):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', environment):
            raise ValueError('Choose a registered workspace.')
        with access.connect() as db:
            if not db.execute('SELECT 1 FROM environments WHERE id=?', (environment,)).fetchone():
                raise ValueError('Choose a registered workspace.')
        return state if environment == 'demo' else state / 'environments' / environment

    def pending_signal_workspaces():
        """Discover durable work without constructing another runtime for idle companies."""
        with access.connect() as db:environments=[row['id'] for row in db.execute('SELECT id FROM environments')]
        pending=[]
        for environment in environments:
            if environment in workspaces:continue
            root=registered_workspace_root(environment)
            waiting=False
            for filename in ('background-agents.sqlite','messaging.sqlite'):
                path=root/filename
                if not path.is_file() or path.is_symlink():continue
                try:
                    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0) as db:
                        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if filename=='messaging.sqlite':
                            waiting='inbox' in tables and db.execute("SELECT 1 FROM inbox WHERE status IN ('pending','admitting','admission_uncertain') LIMIT 1").fetchone()
                        else:
                            active='agents' in tables and any(json.loads(row[0]).get('status')=='active' for row in db.execute('SELECT body FROM agents'))
                            queued='agent_wakes' in tables and db.execute("SELECT 1 FROM agent_wakes WHERE status IN ('pending','claimed','uncertain') LIMIT 1").fetchone()
                            timers='signal_sources' in tables and db.execute("SELECT 1 FROM signal_sources WHERE status='active' AND kind='timer' LIMIT 1").fetchone()
                            waiting=active or queued or timers
                except (sqlite3.Error,ValueError,OSError):
                    continue  # One unavailable workspace is retried on the next bounded scan.
                if waiting:break
            if waiting:pending.append(environment)
            if len(pending)>=64:break
        return pending

    def cold_signal_sources(environment):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',environment):
            raise ValueError('Choose a registered workspace.')
        with access.connect() as db:
            if not db.execute('SELECT 1 FROM environments WHERE id=?',(environment,)).fetchone():
                raise ValueError('Choose a registered workspace.')
        root=state if environment=='demo' else state/'environments'/environment
        if not (root/'background-agents.sqlite').is_file():raise ValueError('Use a current workspace webhook.')
        from signal_sources import SignalSources
        store=AgentStore(root,agent_classes=vertical.agent_classes())
        def ingress_current(signal):
            principal=signal.get('actor_principal_id')
            if not messaging_member({'environment_id':environment},principal):return False
            key=signal.get('chat_id')
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',key):return False
            path=root/'chats'/(key+'.json')
            if path.is_symlink() or not path.is_file() or path.stat().st_size>32*1024*1024:return False
            record=json.loads(path.read_text())
            return (record.get('id')==key and not record.get('parent_id')
                and record.get('owner_principal_id')==signal.get('owner_principal_id')
                and record.get('dataset')==signal.get('collection'))
        store.signal_authority=ingress_current
        return SignalSources(store,public_origin=public_origin,environment=environment)

    hook_requests={}
    @app.post('/hooks/in/{environment}/{hook_id}')
    @app.post('/api/hooks/{environment}/{hook_id}')
    async def incoming_hook(environment:str,hook_id:str,request:Request):
        if not re.fullmatch(r'hook-[a-f0-9]{32}',hook_id):raise HTTPException(404,'Use the current webhook address.')
        minute=int(time.time()//60);key=(environment,hook_id,minute)
        for prior in list(hook_requests):
            if prior[2]<minute:hook_requests.pop(prior,None)
        if len(hook_requests)>=10000 or hook_requests.get(key,0)>=120:
            raise HTTPException(429,'Use the webhook request allowance.')
        hook_requests[key]=hook_requests.get(key,0)+1
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>65536:raise HTTPException(413,'Send a webhook body within 64 KB.')
        try:
            sources=await asyncio.to_thread(cold_signal_sources,environment)
            signature=request.headers.get('x-hub-signature-256') or request.headers.get('authorization','')
            result=await asyncio.to_thread(sources.receive,hook_id,bytes(raw),signature,
                event_id=request.headers.get('x-event-id'))
            return JSONResponse(result,status_code=202)
        except PermissionError:raise HTTPException(403,'Verify the webhook signature.')from None
        except (ValueError,OSError):raise HTTPException(400,'Use the current bounded webhook event.')from None

    @app.get('/api/wakes')
    async def wake_sources(request:Request,chat_id:str):
        selected=request.state.workspace;record=selected['agent'].children.get(chat_id)
        if not record or not selected['chats'].can_read(record,request.state.session):
            raise HTTPException(404,'Open the owning conversation.')
        actor=selected['chats'].principal(request.state.session)
        return {'sources':selected['signal_sources'].list(actor=actor,chat_id=chat_id),
            'endpoints':selected['signal_endpoints'].list(actor=actor,chat_id=chat_id),
            'daily_limits':selected['agents'].signals.usage()}

    @app.post('/api/wakes/endpoints/{endpoint_id}/secret')
    async def endpoint_secret(endpoint_id:str,request:Request):
        selected=request.state.workspace
        try:
            endpoint=selected['signal_endpoints'].get(endpoint_id)
            record=selected['agent'].children.get(endpoint['chat_id'])
            if not record or not selected['chats'].can_write(record,request.state.session):raise ValueError()
            secret=selected['signal_endpoints'].reveal(endpoint_id,
                actor=selected['chats'].principal(request.state.session),chat_id=endpoint['chat_id'])
            return JSONResponse({'secret':secret},headers={'Cache-Control':'no-store'})
        except ValueError:raise HTTPException(409,'Copy this active endpoint secret once.')from None

    @app.post('/api/wakes/hooks/{hook_id}/secret')
    async def reveal_wake_secret(hook_id:str,request:Request):
        selected=request.state.workspace
        try:
            source=selected['signal_sources'].get(hook_id)
            record=selected['agent'].children.get(source['chat_id'])
            if not record or not selected['chats'].can_write(record,request.state.session):raise ValueError()
            secret=selected['signal_sources'].reveal(hook_id,actor=selected['chats'].principal(request.state.session),
                chat_id=source['chat_id'])
            return JSONResponse({'secret':secret},headers={'Cache-Control':'no-store'})
        except ValueError:raise HTTPException(409,'Copy this active conversation webhook secret once.')from None

    @app.post('/hooks/{channel}/{environment}')
    @app.get('/hooks/{channel}/{environment}')
    @app.api_route('/api/messaging/webhook/{environment}', methods=['GET','POST'])
    @app.post('/api/messaging/webhook/{environment}/{channel}')
    async def messaging_webhook(environment: str, request: Request, channel: str = 'whatsapp'):
        if channel not in {'whatsapp', 'viber', 'email', 'voice'} or not vertical.builtin_capabilities().get(channel):
            raise HTTPException(404, 'Choose a configured messaging callback.')
        from messaging_provider import ProviderError
        from messaging import MessagingError
        selected = workspaces.get(environment)
        try:
            root = registered_workspace_root(environment)
            if selected is not None:
                provider = selected['messaging'].adapters.get(channel)
            else:
                # Signature verification needs only this selected provider's
                # private account settings; no agent or workspace recovery yet.
                from builtin_capabilities import BuiltInCapabilities
                factories = dict(builtin_provider_factories or {})
                if messaging_provider_factory is not None:
                    factories['whatsapp'] = messaging_provider_factory
                provider = BuiltInCapabilities(root, environment, vertical.builtin_capabilities(),
                    factories=factories).provider(channel)
            if provider is None:
                raise HTTPException(503, 'Complete company messaging setup.')
            if request.method == 'GET':
                if channel != 'whatsapp':
                    raise HTTPException(405, 'Post the signed provider event.')
                query = request.query_params
                return Response(provider.verify_subscription(query.get('hub.mode'), query.get('hub.verify_token'),
                    query.get('hub.challenge')), media_type='text/plain')
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 256 * 1024:
                    raise HTTPException(413, 'Send the bounded delivery event.')
            if selected is None:
                await asyncio.to_thread(provider.verify, bytes(raw), dict(request.headers))
                selected = workspace(environment)
                await ready_workspace(selected)
            result = await asyncio.to_thread(selected['messaging'].handle_webhook,
                bytes(raw), dict(request.headers), channel=channel)
            if channel == 'voice':
                from messaging_composition import callback_response
                body, media_type = callback_response(channel)
                return Response(body, media_type=media_type)
            return result
        except (ProviderError, MessagingError, ValueError):
            raise HTTPException(403, 'Verify the company delivery signature.') from None

    async def phone_tick(selected):
        """One pass of host alert delivery. A failed pass sends nothing and leaves the other ticks running."""
        if not selected.get('phone_enabled', True):
            return
        try:
            await asyncio.to_thread(selected['phone_catch_up'])  # an alert whose first fan-out read failed
        except Exception:
            pass  # the next pass reads it again inside the alert's 900 seconds
        try:
            selected['phone'].publish()  # contact cards the host raised outside a request
            await selected['phone_delivery'].tick()
        except Exception:
            pass  # durable state permits review; the scheduler, reply and stream ticks still run

    async def messaging_tick(selected):
        if not selected.get('messaging_enabled', True):
            return
        from messaging_signals import MessagingSignals
        bridge = selected.get('messaging_signals')
        if bridge is None:
            bridge = MessagingSignals(selected['messaging'], selected['agents'], selected['agent'],
                member=lambda actor: messaging_member(selected, actor),
                answered=selected['phone_delivery'].answered)
            selected['messaging_signals'] = bridge
        await bridge.tick()

    package_lock = threading.Lock()

    def native_package_store(selected):
        from native_packages import NativePackageStore
        with package_lock:
            if 'native_packages' not in selected:
                selected['native_packages'] = NativePackageStore(selected['state'])
            return selected['native_packages']

    async def revoke_native_plugin(selected, *, principal_id, plugin_id):
        managed = getattr(selected['agent'], 'managed_runtime', None)
        if managed is not None:
            revoke = getattr(managed, 'revoke_plugin', None)
            if not callable(revoke):
                return {'cleanup_confirmed': False}
            confirmed = await revoke(selected['agent'], principal_id=principal_id, plugin_id=plugin_id)
            return {'cleanup_confirmed': confirmed is True}
        return {'cleanup_confirmed': False}

    async def native_installation_capabilities(selected, *, principal_id, store):
        managed = getattr(selected['agent'], 'managed_runtime', None)
        if managed is not None:
            from hosted_install import HostedPluginInstaller
            from plugin_routes import _installable
            available = (getattr(selected['agent'], 'plugin_package_store', None) is store
                and isinstance(store.materializer, HostedPluginInstaller)
                and app.state.managed_mcp_host.transport is not None)
            return {'available': available,
                    'installable_ids': await asyncio.to_thread(_installable, store) if available else []}
        return {'available': False, 'installable_ids': []}

    # Installs materialize exact packages for the OpenAI-hosted environment.
    register_plugin_routes(app, store_for_workspace=native_package_store,
        installation_capabilities=native_installation_capabilities,
        revoke_plugin=revoke_native_plugin)

    @app.get("/api/plugins/catalogue")
    async def plugins_catalogue():
        return plugin_catalogue()

    @app.get("/api/plugins/{plugin_id}")
    async def plugin_details(plugin_id: str):
        try:
            return read_plugin(plugin_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, "Choose a plugin from the catalogue.") from exc

    @app.get("/api/connections")
    async def connections(request: Request):
        principal, _ = connection_identity(request)
        return request.state.workspace["connections"].catalogue(principal)

    async def custom_identity(request, key, *, writable=False):
        principal, binding = connection_identity(request, writable=writable)
        selected = request.state.workspace
        try:
            await asyncio.to_thread(selected['custom_connectors'].get, key, owner=principal)
            return principal, binding
        except ValueError:
            pass
        organization = selected['organization_chats']
        try:
            record = await asyncio.to_thread(selected['custom_connectors'].get, key, owner=organization.owner)
        except ValueError:
            raise HTTPException(404, 'Choose a connector from this workspace.') from None
        chat = selected['agent'].children.get(record['chat_id'])
        if (not organization.canonical(chat) or chat.get('dataset') != record['collection']
                or not organization.can_read(chat,request.state.session)):
            raise HTTPException(404, 'Choose a connector from this company conversation.')
        member = messaging_member(selected, request.state.session['principal']['id'])
        if writable and (not member or member['role'] != 'owner'):
            raise HTTPException(403, 'An organization owner configures shared connector access.')
        return organization.owner, binding

    register_custom_routes(app, connection_identity, owner_for_connector=custom_identity)
    def document_previews_for_workspace(selected):
        previews=selected['document_previews']
        previews.enabled=selected['document_preview_readiness'].ready()
        return previews

    register_document_preview_routes(app,previews_for_workspace=document_previews_for_workspace,
        owner_for_file=lambda request,file_id,write=False: file_principal(request,file_id,write=write))

    @app.get("/api/connections/custom")
    async def custom_connections(request: Request):
        principal,_=connection_identity(request)
        selected = request.state.workspace
        rows = await asyncio.to_thread(selected['custom_connectors'].list,owner=principal)
        organization = selected['organization_chats']
        for record in await asyncio.to_thread(selected['custom_connectors'].list,owner=organization.owner):
            chat = selected['agent'].children.get(record['chat_id'])
            if (organization.canonical(chat) and chat.get('dataset') == record['collection']
                    and organization.can_read(chat,request.state.session)):
                rows.append({**record,'organization_shared':True,
                    'can_configure':workspace_writable(request.state.session)
                        and messaging_member(selected,request.state.session['principal']['id'])['role']=='owner'})
        return {'connections':rows}

    @app.post("/api/connections/custom")
    async def create_custom_connection(body: CustomConnectorRequest,request: Request):
        principal,_=connection_identity(request,writable=True)
        selected=request.state.workspace
        if not selected['collections'].contains(body.collection,principal):
            raise HTTPException(400,"Choose a business collection.")
        try:
            record=selected['custom_connectors'].create(**body.model_dump(),owner=principal)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
        identity=record['chat_id'];agent=selected['agent']
        agent.children[identity]={"id":identity,"title":record['name'],"kind":"chat","parent_id":None,
            "owner_principal_id":principal,"dataset":body.collection,"connector_id":record['id'],
            "running":False,"messages":[],"created_at":time.time()}
        agent._save_child(agent.children[identity])
        prompt=f"Help me connect {record['name']}.\n\n{body.description}"
        context=(f"Custom connector setup: {record['id']}. Read its saved brief with get_custom_connector. "
                 "Build its adapter package with save_custom_connector when the target and operations are clear. "
                 "Declare the required credential fields, then call request_custom_connector_credentials to open private input in chat. "
                 f"Selected source scope: {body.collection}.")
        # This automatic chat has no HTTP event subscriber. Consume directly so
        # a bounded ChatStream subscriber queue cannot stall a long setup turn.
        async def build_connector():
            try:
                async for _ in agent.turn(prompt,identity,body.collection,model_context=context):
                    pass
            except Exception:
                agent.children[identity]['messages'].append({'role':'assistant','content':'',
                    'error':'Continue setup to retry the connector build.','tools':[],'results':[]})
            finally:
                agent.children[identity]['running']=False
                agent._save_child(agent.children[identity])
        task=asyncio.create_task(build_connector())
        agent.child_tasks[identity]=task
        def release(task):
            if agent.child_tasks.get(identity) is task:agent.child_tasks.pop(identity,None)
        task.add_done_callback(release)
        return record

    @app.get("/api/connections/custom/{connector_id}")
    async def custom_connection(connector_id: str,request: Request):
        principal,_=await custom_identity(request,connector_id)
        try:
            selected=request.state.workspace
            value=await asyncio.to_thread(selected['custom_connectors'].get,connector_id,owner=principal)
            value['organization_shared'] = principal == selected['organization_chats'].owner
            value['can_configure'] = workspace_writable(request.state.session) and (not value['organization_shared']
                or messaging_member(selected,request.state.session['principal']['id'])['role'] == 'owner')
            value['execution_setup']=await asyncio.to_thread(selected['custom_setup'].describe,connector_id,
                owner=principal,collection=value['collection'])
            value=await selected['vertical'].connection_fields(value,principal,request)
            if selected.get('custom_coordinator') and value['kind']!='sftp':
                value['execution_setup'].update(await selected['custom_coordinator'].status(connector_id,
                    owner=principal,collection=value['collection']))
            return value
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc

    @app.get("/api/connections/custom/{connector_id}/package")
    async def custom_connection_package(connector_id: str,request: Request):
        principal,_=await custom_identity(request,connector_id)
        try:data=request.state.workspace['custom_connectors'].package(connector_id,owner=principal)
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc
        # The validated generated ID is a download label, never a storage path.
        return Response(data,media_type='application/zip',headers={'Cache-Control':'private, no-store',
            'Content-Disposition':f'attachment; filename="{connector_id}.zip"'})

    @app.post("/api/connections/custom/{connector_id}/secrets")
    async def custom_connection_secret(connector_id: str,request: Request):
        try:
            principal,_=await custom_identity(request,connector_id,writable=True)
        except HTTPException as exc:
            if exc.status_code != 404: raise
            raise HTTPException(400,'Use a requested credential field and a valid private value.') from None
        # Parse privately: Pydantic's ordinary validation response can echo inputs.
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>65536:raise HTTPException(413,'Enter one credential at a time.')
        try:
            body=json.loads(raw)
            if not isinstance(body,dict) or set(body)!={'name','value'}:raise ValueError()
            result=request.state.workspace['custom_connectors'].set_secret(connector_id,
                body['name'],body['value'],owner=principal)
        except (ValueError,TypeError):
            raise HTTPException(400,'Use a requested credential field and a valid private value.') from None
        return Response(json.dumps(result),media_type='application/json',headers={'Cache-Control':'private, no-store'})

    @app.delete("/api/connections/custom/{connector_id}/secrets/{name}")
    async def custom_connection_secret_remove(connector_id: str,name: str,request: Request):
        principal,_=await custom_identity(request,connector_id,writable=True)
        try:return request.state.workspace['custom_connectors'].remove_secret(connector_id,name,owner=principal)
        except ValueError as exc:raise HTTPException(404,str(exc)) from exc

    @app.post("/api/connections/{provider}/connect")
    async def connect_provider(provider: str, request: Request):
        principal, binding = connection_identity(request, writable=True)
        broker = request.state.workspace["connections"]
        private_headers = {'Cache-Control': 'private, no-store'}
        try:
            bearer = broker.provider_policy(provider)['auth'] == 'bearer'
        except MCPConnectionError:
            raise HTTPException(400, 'Choose an available connection.', headers=private_headers) from None
        # Parse private values here: model validation errors can echo submitted inputs.
        limit = 20 * 1024 if bearer else 4096
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise HTTPException(413, 'Use a bounded connection setup value.', headers=private_headers)
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Use the connection setup fields.', headers=private_headers) from None
        finally:
            raw.clear()
        if (not isinstance(body, dict) or set(body) - ({'access_mode', 'token'} if bearer else {'access_mode'})
                or body.get('access_mode', 'read') not in ('read', 'read_write')):
            raise HTTPException(400, 'Use the connection setup fields and chosen access level.', headers=private_headers)
        if bearer:
            from mcp_connections import BearerCredential
            try:
                credential = BearerCredential(body.pop('token', None))
                flow = await broker.connect_bearer(provider, credential, principal_id=principal,
                    session_binding=binding, access_mode=body.get('access_mode', 'read'))
            except Exception:
                # Provider/validation exception text may contain the private value.
                raise HTTPException(400, 'Review the private credential and connection access level.',
                                    headers=private_headers) from None
            return Response(json.dumps(flow), media_type='application/json', headers=private_headers)
        try:
            flow = await broker.start(provider,
                principal_id=principal, session_binding=binding, access_mode=body.get('access_mode', 'read'))
            return Response(json.dumps(flow), media_type='application/json', headers=private_headers)
        except MCPConnectionError as exc:
            raise HTTPException(400, str(exc), headers=private_headers) from exc

    @app.get("/api/connections/flows/{flow_id}")
    async def connection_flow(flow_id: str, request: Request):
        principal, binding = connection_identity(request, writable=True)
        try:
            return request.state.workspace["connections"].flow_status(flow_id,
                principal_id=principal, session_binding=binding)
        except MCPConnectionError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/connections/{provider}")
    async def disconnect_provider(provider: str, request: Request):
        principal, _ = connection_identity(request, writable=True)
        try:
            result = await request.state.workspace["connections"].disconnect(provider, principal_id=principal)
            revoke = getattr(request.state.workspace['agent'], 'revoke_provider_approvals', None)
            if revoke is not None:await revoke(provider, owner=principal)
            return result
        except MCPConnectionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/connections/oauth/callback")
    async def oauth_landing():
        # The first cross-site navigation carries no Strict cookie. A same-origin
        # script clears the URL, then POSTs with the original private session.
        return FileResponse(HERE / "web" / "oauth-callback.html", headers={"Cache-Control": "no-store"})

    @app.get("/oauth-callback.js")
    async def oauth_script():
        return FileResponse(HERE / "web" / "oauth-callback.js", media_type="application/javascript")

    @app.post("/api/connections/oauth/callback")
    async def oauth_callback(body: OAuthCallbackRequest, request: Request):
        principal, binding = connection_identity(request, writable=True)
        try:
            return await request.state.workspace["connections"].oauth_callback(**body.model_dump(),
                principal_id=principal, session_binding=binding)
        except MCPConnectionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/")
    async def home():
        assets=host.options.get('page_assets',{})
        tags=[]
        for kind,suffix in (('styles','.css'),('scripts','.js')):
            paths=assets.get(kind,[])
            if not isinstance(paths,(list,tuple)) or len(paths)>32:
                raise ValueError('Declare bounded vertical page assets.')
            for path in paths:
                if (not isinstance(path,str) or not re.fullmatch(r'/[a-zA-Z0-9][a-zA-Z0-9_./-]*',path)
                        or '..' in path or not path.endswith(suffix)):
                    raise ValueError('Use local paths for the selected vertical page assets.')
                tags.append(f'<link rel="stylesheet" href="{path}" />' if kind=='styles'
                    else f'<script src="{path}" defer></script>')
        document=(HERE / 'web' / 'index.html').read_text()
        return Response(document.replace('<!-- vertical-assets -->','\n'.join(tags)),media_type='text/html',
            headers={'Cache-Control':'no-cache'})



    @app.get("/app.js")
    async def script():
        return FileResponse(HERE / "web" / "app.js", media_type="application/javascript")

    @app.get("/question-box.js")
    async def question_box_script():
        return FileResponse(HERE / "web" / "question-box.js", media_type="application/javascript")

    @app.get("/question-box.css")
    async def question_box_style():
        return FileResponse(HERE / "web" / "question-box.css", media_type="text/css")

    @app.get("/brand/rocoto-mark.svg")
    async def logo():
        path = layout.shared_asset("app/public/brand/rocoto-mark.svg")
        return FileResponse(path if path.is_file() else web_asset("control-room/kit/rocoto-mark.svg"), media_type="image/svg+xml")

    @app.get("/fonts/fraunces/fraunces-latin-500-700.woff2")
    async def wordmark_font():
        path = layout.shared_asset("app/public/fonts/fraunces/fraunces-latin-500-700.woff2")
        return FileResponse(path if path.is_file() else web_asset("control-room/kit/inter-latin-500-normal.woff2"), media_type="font/woff2")

    @app.get("/connection-brands/{filename}")
    async def connection_brand(filename: str):
        if filename not in {"axis.svg","gmail.png","square.svg","shopify.svg"}:
            raise HTTPException(404,"Choose a connection brand asset.")
        return FileResponse(layout.shared_asset("app/public/connection-brands") / filename)

    @app.get("/vendor/{filename:path}")
    async def vendor(filename: str):
        if filename not in {"marked-18.0.9.umd.js","dompurify-3.4.16.min.js","d3-7.9.0/d3.min.js"} | rich_vendor_paths:
            raise HTTPException(404,"Choose an application asset.")
        return FileResponse(HERE/"web"/"vendor"/filename)

    @app.get("/assets/chat-dreamscape.png")
    @app.get("/assets/search-dreamscape.png")
    async def workspace_artwork(request: Request):
        return FileResponse(HERE / "web" / "assets" / request.url.path.rsplit("/", 1)[-1],
                            media_type="image/png")

    @app.get("/assets/monet-water-lilies.jpg")
    async def empty_state_artwork():
        return FileResponse(HERE / "web/assets/monet-water-lilies.jpg", media_type="image/jpeg")


    @app.get("/style.css")
    async def style():
        return FileResponse(HERE / "web" / "style.css", media_type="text/css")

    @app.get("/business-summary.js")
    @app.get("/business-summary.css")
    @app.get("/settings-shell.js")
    @app.get("/settings-shell.css")
    @app.get("/harness-shell.js")
    @app.get("/workspace-tabs.js")
    @app.get("/workspace-extensions.js")
    @app.get("/wake-controls.js")
    @app.get("/wake-controls.css")
    @app.get("/workspace-tabs.css")
    @app.get("/workspace-home.js")
    @app.get("/workspace-home.css")
    @app.get("/display-charts.js")
    @app.get("/display-charts.css")
    @app.get("/display-canvas.js")
    @app.get("/display-canvas.css")
    @app.get("/tool-trail.js")
    @app.get("/tool-trail.css")
    @app.get("/rich-content.js")
    @app.get("/rich-content.css")
    @app.get("/rich-diagram.js")
    @app.get("/profile-settings.js")
    @app.get("/profile-settings.css")
    @app.get("/plugin-settings.js")
    @app.get("/plugin-settings.css")
    @app.get("/messaging-settings.js")
    @app.get("/messaging-settings.css")
    @app.get("/workspace-settings.js")
    @app.get("/workspace-settings.css")
    @app.get("/business-map.js")
    @app.get("/business-map.css")
    async def interface_component(request: Request):
        filename = request.url.path.removeprefix("/")
        media_type = "text/css" if filename.endswith(".css") else "application/javascript"
        return FileResponse(web_asset(filename), media_type=media_type)

    @app.get("/rich-diagram.html")
    async def diagram_component():
        return FileResponse(HERE / "web/rich-diagram.html", media_type="text/html", headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; font-src 'self'; img-src data:; connect-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"})

    async def shared_static(asset: str):
        if asset == 'theme.css': return await theme()
        if asset == 'rich-diagram.html': return await diagram_component()
        if asset == 'brand/rocoto-mark.svg': return await logo()
        if asset == 'fonts/fraunces/fraunces-latin-500-700.woff2': return await wordmark_font()
        if asset.startswith('connection-brands/') and asset.count('/') == 1:
            return await connection_brand(asset.split('/', 1)[1])
        relative = Path(asset)
        source = HERE / 'web' / relative
        if (relative.is_absolute() or any(part in {'.', '..'} for part in relative.parts)
                or source.resolve().parent != (HERE / 'web').resolve() and asset not in {
                    'assets/chat-dreamscape.png', 'assets/search-dreamscape.png', 'assets/monet-water-lilies.jpg'}
                or source.suffix not in {'.js', '.css', '.png', '.jpg'} or not source.is_file()):
            raise HTTPException(404, 'Choose a shared page asset.')
        return FileResponse(source)

    @app.get('/static/vendor/{filename:path}')
    async def shared_vendor(filename: str):
        return await vendor(filename)

    @app.get("/api/source-icon")
    async def source_icon(host: str):
        try:
            body, kind = await app.state.source_icons.get(host)
        except ValueError:
            raise HTTPException(400, "Choose a public website domain.") from None
        return Response(body, media_type=kind, headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"})

    @app.get("/theme.css")
    async def theme():
        # Use the shared brand source, so a brand change reaches this independent Python UI.
        source = (layout.shared_asset("app/src/rocoto-theme.ts")).read_text()
        brand = source.split("export const BRAND: Brand = {", 1)[1].split("};", 1)[0]
        values = {}
        for name in ("ink", "accent", "paper"):
            values[name] = re.search(rf"{name}:\s*'([^']+)'", brand).group(1)
        font = re.search(r"const ROCOTO_SANS = '([^']+)'", source).group(1)
        wordmark_font = re.search(r"const ROCOTO_SERIF = '([^']+)'", source).group(1)
        video_tokens = json.loads((HERE / "theme.json").read_text())
        palette = video_tokens.get("palette", {})
        values.update(palette)
        css = "@font-face{font-family:Fraunces;src:url('/static/harness/fonts/fraunces/fraunces-latin-500-700.woff2') format('woff2');font-style:normal;font-weight:500 700;font-display:swap;}"
        css += ":root{" + "".join(f"--{k}:{v};" for k, v in values.items())
        css += f"--font:{font};--wordmark-font:{wordmark_font};--muted:color-mix(in srgb,var(--ink) 60%,var(--paper));"
        css += f"--connection-online:{video_tokens['connection_online']};"
        css += "--border:color-mix(in srgb,var(--ink) 14%,var(--paper));--surface:color-mix(in srgb,var(--ink) 3%,var(--paper));"
        css += "".join(f"--{key}:{value};" for key, value in palette.items())
        typography = video_tokens.get("typography", {})
        css += "".join(f"--{key}:{typography[key]};" for key in
                       ("font", "font-code", "font-size-ui", "font-size-code") if key in typography)
        css += "--radius:0.75rem;--radius-sm:0.5rem;--radius-lg:1.25rem;}"
        return Response(css, media_type="text/css")

    def session_details(session):
        result = {**(session or {"authenticated": False, "environments": []}), "operator": operator}
        fields=getattr(workspace('demo')['vertical'],'session_fields',lambda:{})()
        result['environments']=[{**environment,**(fields if environment['id']=='demo' else {})}
            for environment in result['environments']]
        return result

    @app.get('/healthz')
    async def health():
        revision = None
        for parent in list(HERE.parents)[:5]:
            metadata = parent / '.release.json'
            if metadata.is_file():
                try:
                    value = json.loads(metadata.read_text()).get('sourceCommit')
                    if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{40}', value): revision = value
                except (OSError, ValueError, AttributeError): pass
                break
        return JSONResponse({'status': 'ok', 'vertical': vertical.id, 'release': revision,
            'core': 'openai_managed_agents_api', 'execution': 'openai_hosted'},
            headers={'Cache-Control': 'no-store'})

    @app.get("/api/session")
    async def session_info(request: Request):
        return {**session_details(request.state.session), "demo_browsing":demo_browsing}

    def set_session(response, token, request):
        response.delete_cookie(LEGACY_COOKIE,path="/")
        response.set_cookie(COOKIE,token,max_age=DAY,httponly=True,samesite="strict",secure=request.url.scheme=="https",path="/")

    @app.post("/api/session")
    async def login(body: LoginRequest, request: Request, response: Response):
        try:
            token, session = access.login(body.code)
        except ValueError as exc:
            raise HTTPException(401,str(exc)) from exc
        access.logout((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE)))
        set_session(response,token,request)
        return session_details(session)

    @app.post("/api/session/demo")
    async def demo_session(request: Request, response: Response):
        if not demo_browsing:
            raise HTTPException(403, "Enter your company access code.")
        token, session = access.login(access.issue("demo"),can_query=False)
        access.logout((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE)))
        set_session(response,token,request)
        return session_details(session)

    @app.delete("/api/session")
    async def logout(request: Request, response: Response):
        access.logout((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE)))
        response.delete_cookie(COOKIE,path="/")
        response.delete_cookie(LEGACY_COOKIE,path="/")
        return {"ok":True}

    @app.post("/api/session/company")
    @app.post("/api/environment")
    async def select_environment(body: EnvironmentRequest, request: Request):
        try:
            return session_details(access.switch((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE)),body.environment_id))
        except ValueError as exc:
            raise HTTPException(403,str(exc)) from exc

    @app.get("/api/observers")
    async def observers(request: Request):
        try:
            return {"principals":access.principals((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE)))}
        except ValueError as exc:
            raise HTTPException(403,str(exc)) from exc

    @app.post("/api/observer")
    async def observe(body: ObserverRequest, request: Request):
        try:
            return session_details(access.observe((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE)),body.environment_id,body.principal_id))
        except ValueError as exc:
            raise HTTPException(403,str(exc)) from exc

    @app.delete("/api/observer")
    async def end_observation(request: Request):
        try:
            return session_details(access.clear_observer((request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE))))
        except ValueError as exc:
            raise HTTPException(403,str(exc)) from exc

    @app.get("/api/status")
    async def status(request: Request):
        selected = request.state.workspace
        agent = selected["agent"]
        live_connection = await selected["connection"].status()
        visible_ids = {item["id"] for item in selected["chats"].visible_records(agent.children,request.state.session,include_archived=True)}
        budget = {"mode":"provider_pools", "local_embedding_queries_usd":0,"query_pool":None,
                  "gpu_cash_spend_limit_usd":0,
                  "openai_api":selected["api_usage"].summary()}
        budget.update(selected['vertical'].budget_fields())
        return {"connection":live_connection, "operator":operator,
                "agent": {"authenticated":live_connection["managed_authenticated"],
                "active_chats":{key:value for key,value in agent.status().items() if key in visible_ids},
                "route":live_connection["route"]},
                "budget":budget, **selected['vertical'].status_fields()}



    @app.get("/api/business")
    async def business(request: Request, collection: str):
        try:
            selected=request.state.workspace
            data=await asyncio.to_thread(selected["business"].get,collection)
            principal=selected['chats'].principal(request.state.session)
            if principal:data=await asyncio.to_thread(selected['vertical'].business_fields,principal,collection,data)
            return data
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get('/api/business/map')
    async def business_map(request: Request, collection: str, map_id: str | None = None):
        from profile_activity import access_scope
        import sqlite3
        selected = request.state.workspace
        try:
            binding = await asyncio.to_thread(access_scope, access.path, request.state.session, 'person')
            organization = selected['organization_chats']
            shared = any(organization.canonical(chat) and chat.get('dataset') == collection
                and organization.can_read(chat, request.state.session) for chat in selected['agent'].children.values())
            result = None
            if shared:
                try:
                    result = await asyncio.to_thread(selected['business_maps'].get, selected['business'],
                        owner=organization.owner, collection=collection, map_id=map_id)
                except ValueError:
                    if map_id is None: raise
            if result is None or result['status'] == 'empty':
                result = await asyncio.to_thread(selected['business_maps'].get, selected['business'],
                    owner=binding['principal_id'], collection=collection, map_id=map_id)
            if await asyncio.to_thread(access_scope, access.path, request.state.session, 'person') != binding:
                raise PermissionError('Refresh the map from your current workspace.')
            return Response(json.dumps(result, ensure_ascii=False, allow_nan=False), media_type='application/json',
                headers={'Cache-Control': 'private, no-store'})
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (OSError, sqlite3.Error):
            raise HTTPException(503, 'The saved map is being prepared.') from None

    @app.post("/api/business/profile")
    async def business_profile(body: BusinessProfileRequest, request: Request):
        try:
            return request.state.workspace["business"].update(body.collection,body.expected_revision,body.profile)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/api/business/onboarding")
    async def onboarding(body: BusinessScope, request: Request):
        selected = request.state.workspace
        try:
            business = selected["business"].get(body.collection)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
        import hashlib
        principal = request.state.session["principal"]["id"]
        agent = selected["agent"]
        identity = business["onboarding_chat_id"] + "-" + hashlib.sha256(principal.encode()).hexdigest()[:12]
        if identity not in agent.children:
            agent.children[identity] = {"id":identity,"title":"Business onboarding","kind":"onboarding",
                "onboarding":True,"parent_id":None,"dataset":body.collection,"collection":body.collection,
                "owner_principal_id":principal,"running":False,"messages":[]}
            agent._save_child(agent.children[identity])
        return selected["chats"].get(agent.children[identity],request.state.session)

    @app.get("/api/profile/activity")
    async def profile_activity(request: Request, scope: str = "person", days: int = 364):
        from profile_activity import access_scope, activity
        from fastapi.responses import JSONResponse
        import sqlite3
        if scope not in {"person", "organisation"} or not 1 <= days <= 366:
            raise HTTPException(400, "Choose person or organisation activity for 1 to 366 days.")
        selected = request.state.workspace
        try:
            binding = await asyncio.to_thread(access_scope, access.path, request.state.session, scope)
            # Snapshot only activity metadata on the event loop; text, credentials
            # and in-flight mutable tool records never enter this read model.
            chats = [{key: row.get(key) for key in ("id", "owner_principal_id", "parent_id", "kind", "created_at")}
                for row in selected["agent"].children.values()]
            result = await asyncio.to_thread(activity, selected["state"], scope=scope,
                chats=chats, days=days, **binding)
            current = await asyncio.to_thread(access_scope, access.path, request.state.session, scope)
            if current != binding:
                raise PermissionError("Refresh activity from your current workspace.")
            return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Activity records are being prepared.") from None

    @app.get("/api/usage")
    async def usage(request: Request, chat_id: str | None = None):
        selected = request.state.workspace
        selected["usage"].reconcile_local()
        api_mode = selected["auth"].mode == "openai_api"
        company = selected["api_usage" if api_mode else "usage"].summary()
        native = getattr(selected["agent"],"usage",{})
        current = native.get(chat_id,{}) if chat_id else {}
        if chat_id and not selected["chats"].can_read(selected["agent"].children.get(chat_id),request.state.session):
            raise HTTPException(404,"Choose a chat belonging to the selected person.")
        plan = current.get("plan") or next((item["plan"] for item in reversed(list(native.values())) if item.get("plan")),None)
        if not request.state.session["can_query"] or request.state.session["environment"]["id"] != "demo":
            plan = None
            current = {}
        if api_mode:
            plan = None
        return {"company":company,"context":current.get("context"),
            "codex":plan or {"route":"OpenAI API" if api_mode else "Local founder subscription","limits":[],"checked_at":None},
            "provider_pools":{"openai":selected["api_usage"].summary(),**selected["vertical"].provider_usage()},
            **selected['vertical'].usage_fields(),
            "billing":{"status":"prepaid_api" if api_mode else "unconfigured","auto_reload":False},
            "status":selected.get("usage_status")}

    @app.post("/api/usage/settings")
    async def usage_settings(body: UsageSettingsRequest, request: Request):
        try:
            selected = request.state.workspace
            store = selected["api_usage"] if selected["auth"].mode == "openai_api" else selected["usage"]
            return store.set_monthly_limit(body.expected_revision,
                body.monthly_limit_usd)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc


    async def installed_native_skills(request):
        selected = request.state.workspace
        principal = selected["chats"].principal(request.state.session)
        resolver = getattr(selected["agent"], "_native_skills", None)
        if principal is None or resolver is None: return []
        native = resolver()
        if native is None: return []
        # Display uses pinned public descriptors; private paths only enter the
        # verified native runtime after its mounted skills/list check.
        from native_provider_relay import _read
        return await _read(native.list, principal)

    @app.get("/api/skills/catalogue")
    async def skills_catalogue(request: Request):
        result = request.state.workspace["skills"].catalogue()
        return {**result, "skills": result["skills"] + await installed_native_skills(request)}

    @app.get("/api/skills")
    async def skills_installed(request: Request):
        result = request.state.workspace["skills"].list()
        return {**result, "skills": result["skills"] + await installed_native_skills(request)}

    @app.post("/api/skills/install")
    async def skills_install(body: SkillInstallRequest, request: Request):
        try:
            return request.state.workspace["skills"].install(body.skill_id, expected_revision=body.expected_revision)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/ready-agents")
    @app.get("/api/agents/catalogue")
    async def agent_catalogue(request: Request):
        catalogue = request.state.workspace["vertical"].catalogue()
        if request.url.path == '/api/agents/catalogue':
            return catalogue  # One-release response shape for existing clients.
        return {**{key: value for key, value in catalogue.items() if key != 'templates'},
                'ready_agents': catalogue.get('ready_agents', catalogue.get('templates', []))}

    def participant_agent(record,request):
        """An agent with participant reach shares its conversation with a selected participant."""
        from agent_kind_policy import declared_class_authority
        selected=request.state.workspace
        if not isinstance(record,dict) or declared_class_authority(selected['agents'],record)!='participant_reach':
            return False
        chat=selected['agent'].children.get(record.get('chat_id'))
        return bool(chat and chat.get('agent_id')==record.get('id') and selected['chats'].participant(chat,request.state.session))

    def owned_agent(agent_id,request,*,write=False):
        """The agent for its owner. A participant reads it; its definition and controls stay the owner's."""
        selected=request.state.workspace
        try:
            record=selected['agents'].get(agent_id)
        except ValueError as exc:
            raise HTTPException(404,'Choose an agent belonging to the selected person.') from exc
        permissions=selected['chats']
        if not permissions.can_read(record,request.state.session):
            if write or not participant_agent(record,request):
                raise HTTPException(404,'Choose an agent belonging to the selected person.')
            request.state.scope_guards.append(lambda:participant_agent(record,request))
        if write and not permissions.can_write(record,request.state.session):
            raise HTTPException(403,'Return to the agent owner’s workspace to make changes.')
        return record

    def public_agent(record,request):
        selected=request.state.workspace
        selected['scheduler'].ensure_conversation(record,briefing=True)
        record=selected['agents'].get(record['id'])
        permissions=selected['chats']
        runs=[run for run in record.get('runs',[]) if permissions.can_read(run,request.state.session)]
        return {**record,'runs':runs,'last_run':runs[0] if runs else None,
            'read_only':not permissions.can_write(record,request.state.session),
            'execution':request.state.workspace['capabilities'].execution(record)}

    def capability_snapshot(request, dataset=None, chat_id=None):
        selected = request.state.workspace
        principal=selected['chats'].principal(request.state.session)
        if dataset and not selected['collections'].contains(dataset,principal):
            raise HTTPException(400,'Choose a business collection.')
        return selected['capabilities'].snapshot(dataset,
            principal=principal, chat_id=chat_id,
            read_only=not workspace_writable(request.state.session),
            child=bool(selected['agent'].children.get(chat_id, {}).get('parent_id')))

    @app.get('/api/workspace/capabilities')
    async def workspace_capabilities(request: Request, dataset: str | None = None):
        from custom_coordinator import offload
        return await offload(capability_snapshot,request,dataset)

    @app.get("/api/agents")
    async def agents_list(request: Request):
        selected = request.state.workspace
        available = selected['vertical'].available_credit()
        principal=selected['chats'].principal(request.state.session)
        records=selected['agents'].list(owner_principal_id=principal) if principal else []
        if principal:
            # Tracking Agents whose conversation this person takes part in, for reading.
            owned={record['id'] for record in records}
            joined=[record for record in selected['agents'].list() if record['id'] not in owned and participant_agent(record,request)]
            if joined:
                request.state.scope_guards.append(lambda:all(participant_agent(record,request) for record in joined))
            records=[*records,*joined]
        return {"agents":[public_agent(record,request) for record in records], "capabilities":{
            "modes":["ad_hoc","scheduled","ongoing"], "outputs":["report"],
            "resource_ceiling_usd":str(selected['agents'].inputs.resource_ceiling()), "available_credit_usd":available,
            "cash_spend_limit_usd":1, "max_runs":20, "activation_lifetime_seconds":86400,
            "standing_grants":True,"daily_limits":selected['agents'].signals.usage(),
            **selected["vertical"].agent_capabilities(),
            "recurring_agents":capability_snapshot(request)['recurring_agents']}}

    @app.post("/api/agents")
    async def agents_create(body: dict, request: Request):
        selected = request.state.workspace
        principal=selected['chats'].principal(request.state.session)
        if not principal or not request.state.session['can_query'] or request.state.session.get('viewing_as'):
            raise HTTPException(403,'Create an agent from your own workspace.')
        try:
            record=selected["agents"].create(body, selected["collections"].list(principal),
                owner_principal_id=request.state.session['principal']['id'])
            return public_agent(record,request)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/agents/{agent_id}")
    async def agent_details(agent_id: str, request: Request):
        return public_agent(owned_agent(agent_id,request),request)

    @app.post("/api/agents/{agent_id}/update")
    async def agent_update(agent_id: str, body: dict, request: Request):
        selected = request.state.workspace
        owned_agent(agent_id,request,write=True)
        try:
            expected_revision=body.get('expected_revision')
            if type(expected_revision) is not int or expected_revision < 1:
                raise ValueError('Supply the current agent revision.')
            return selected['agents'].update(agent_id,{key:value for key,value in body.items() if key!='expected_revision'},
                selected['collections'].list(selected['chats'].principal(request.state.session)),expected_revision=expected_revision)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    async def activate_agent(agent_id, request, once, expected_revision):
        selected = request.state.workspace
        definition=owned_agent(agent_id,request,write=True)
        try:
            execution = selected['capabilities'].execution(definition)
            if not execution['ready']:
                raise ValueError(execution['notice'])
            selected['vertical'].validate_activation(definition,once)
            selected['scheduler'].ensure_conversation(definition)
            return selected["agents"].activate(agent_id, request.state.session["expires_at"], once=once,
                expected_revision=expected_revision, standing=not once and definition['mode']!='ad_hoc',
                actor_principal_id=selected['chats'].principal(request.state.session))
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/agents/{agent_id}/activate")
    async def agent_activate(agent_id: str, body: ActivationRequest, request: Request):
        return await activate_agent(agent_id, request, False, body.expected_revision)

    @app.post("/api/agents/{agent_id}/run")
    async def agent_run(agent_id: str, body: ActivationRequest, request: Request):
        return await activate_agent(agent_id, request, True, body.expected_revision)

    @app.post("/api/agents/{agent_id}/pause")
    async def agent_pause(agent_id: str, request: Request):
        owned_agent(agent_id,request,write=True)
        try:
            return await request.state.workspace["scheduler"].pause(agent_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/agents/{agent_id}/runs/{run_id}/report")
    async def agent_report(agent_id: str, run_id: str, request: Request):
        selected = request.state.workspace
        owned_agent(agent_id,request)
        try:
            run = selected["agents"].run_report(agent_id, run_id)
        except ValueError as exc:
            raise HTTPException(404, "Choose a run from this company.") from exc
        if not selected['chats'].can_read(run,request.state.session):
            raise HTTPException(404,'Choose a run belonging to the selected person.')
        request.state.scope_guards.append(lambda:selected['chats'].can_read(run,request.state.session))
        messages = run.get("report_messages")
        if messages is None and run["status"] == "running":
            child=selected['agent'].children.get(run['chat_id'],{})
            if child.get('kind') == 'agent_conversation' and 'message_start' not in run:
                messages=[]
            else:
                messages=child.get('messages',[])[run.get('message_start',0):] if selected['chats'].can_read(child,request.state.session) else []
        payload = {"schema_version":1, "kind":"investigation_report", "agent_id":agent_id,
                   "run":{k:v for k,v in run.items() if k not in {"report_messages", "source_keys"}}, "messages":messages or []}
        return Response(json.dumps(payload, indent=2), media_type="application/json",
                        headers={"Content-Disposition":f'attachment; filename="{run_id}.json"'})

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request):
        agent = request.state.workspace["agent"]
        permissions = request.state.workspace["chats"]
        existing = agent.children.get(body.chat_id)
        if existing is not None and not permissions.can_converse(existing,request.state.session):
            raise HTTPException(403,"Continue a chat from your workspace.")
        require_parent_control(existing)
        # A Tracking Agent's participant writes in its conversation as themself.
        participant = existing is not None and permissions.participant(existing,request.state.session)
        if participant and (body.agent_setup or body.followup_run_chat_id or body.skill_ids or body.clip_attachments):
            raise HTTPException(400,'Send text and your own images in this agent’s conversation.')
        if existing and existing.get('kind') in {'organization_chat','messaging_setup'}:
            if existing.get('dataset') != body.dataset:
                raise HTTPException(400, 'Continue this conversation in its own business.')
            if existing.get('kind') == 'organization_chat' and (body.attachments or body.clip_attachments or body.followup_run_chat_id):
                raise HTTPException(400, 'Use company-owned evidence in this shared conversation.')
        if existing is None and body.chat_id.startswith(('orgchat-','messaging-')):
            raise HTTPException(400, 'Open the company conversation through its setup controls.')
        if existing and existing.get('kind') == 'agent_conversation':
            if existing.get('dataset') != body.dataset:
                raise HTTPException(400,'Continue the agent in its own business.')
            definition=owned_agent(existing['agent_id'],request,write=not participant)
            if any(run.get('status') == 'running' for run in definition.get('runs',[])):
                raise HTTPException(409,'Queue or steer this agent’s active turn.')
        if existing is None and body.chat_id in agent.registry:
            raise HTTPException(403,"Open this earlier chat with its original owner access.")
        if body.dataset and not participant and not request.state.workspace['collections'].contains(
                body.dataset,permissions.principal(request.state.session)):
            raise HTTPException(400,'Choose a business collection.')
        pending_child = agent.child_tasks.get(body.chat_id)
        if body.chat_id in agent.active or pending_child is not None and not pending_child.done():
            raise HTTPException(409, "Queue or steer this chat’s active turn.")
        if agent.children.get(body.chat_id, {}).get("kind") == "agent_run":
            raise HTTPException(409, "Continue this saved run in a new linked chat.")
        try:
            permissions.validate_attachments(body.attachments,request.state.session,write=True)
        except ValueError as exc:
            raise HTTPException(403,str(exc)) from exc
        try:
            agent._input(body.message, body.attachments, body.clip_attachments, body.dataset)
            if body.skill_ids:
                agent.resolve_skills(body.skill_ids, body.chat_id,
                                     owner_principal_id=permissions.principal(request.state.session))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        model_context = []
        if body.followup_run_chat_id:
            prior = agent.children.get(body.followup_run_chat_id)
            if (not prior or prior.get("kind") != "agent_run" or prior.get("dataset") != body.dataset
                    or prior.get("running") or not permissions.can_read(prior,request.state.session)):
                raise HTTPException(400, "Continue a completed run in its original business scope.")
            record = request.state.workspace["agents"].run_report(prior["agent_id"], prior["run_id"])
            if record["status"] == "running":
                raise HTTPException(409, "Wait for this run's report before continuing it.")
            context = [{"answer":item.get("content", ""), "evidence":item.get("results", [])}
                       for item in record.get("report_messages", []) if item.get("role") == "assistant"]
            model_context.append("Prior investigation report (reference data, preserve its evidence and uncertainty):\n" +
                json.dumps({"run_id":prior["run_id"], "source_url":f"/?chat={prior['id']}", "report":context}))
        if body.dataset:
            model_context.append("Selected source scope: " + body.dataset + ". Limit searches to this dataset.")
        if existing is None:
            ready_made = None
            setup = body.agent_setup.model_dump(exclude_none=True) if body.agent_setup else None
            if setup and setup.get('agent_id'):
                saved = owned_agent(setup['agent_id'], request, write=True)
                if saved['dataset'] != body.dataset:
                    raise HTTPException(400, 'Configure the agent in its own business.')
                setup['mode'] = saved['mode']
            elif setup and setup.get('template_id'):
                if setup['template_id'] not in {item['id'] for item in request.state.workspace['vertical'].catalogue()['templates']}:
                    raise HTTPException(400, 'Choose an available ready-made agent.')
                principal = permissions.principal(request.state.session)
                try:
                    definition = request.state.workspace['vertical'].draft_agent(setup['template_id'],body.dataset,principal)
                    ready_made = request.state.workspace['agents'].create(definition, request.state.workspace['collections'].list(principal),
                        owner_principal_id=principal, conversation_chat_id=body.chat_id)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            agent.children[body.chat_id] = {"id":body.chat_id,"title":"New chat","kind":"chat",
                "parent_id":None,"owner_principal_id":request.state.session["principal"]["id"],
                "dataset":body.dataset,"running":False,"messages":[],"created_at":time.time(),
                **({'agent_setup': setup} if setup else {})}
            if ready_made is not None:
                from background_agents import ensure_agent_conversation
                ensure_agent_conversation(request.state.workspace['agents'], agent, ready_made)
            else:
                agent._save_child(agent.children[body.chat_id])
        from chat_stream import ChatStream
        # The browser subscribes to a host-owned task. Reload/navigation detaches
        # only the stream; native turns keep recording to the same private chat.
        if not hasattr(agent, 'invocation_grants'):
            agent.invocation_grants = {}
        agent.invocation_grants[body.chat_id] = {
            'principal': request.state.session['principal']['id'],
            'expires_at': request.state.session['expires_at']}
        stream = ChatStream(agent.turn(body.message, body.chat_id, body.dataset, body.attachments, body.clip_attachments,
                                      model_context="\n\n".join(model_context),
                                      **({'sender':{'principal_id':request.state.session['principal']['id'],
                                                   'display_name':request.state.session['principal']['display_name'], 'channel':'web'}} if existing and (participant or existing.get('kind') in {'organization_chat','messaging_setup'}) else {}),
                                      **({"skill_ids":body.skill_ids} if body.skill_ids else {})))
        agent.child_tasks[body.chat_id] = stream.task
        def release_task(task):
            if agent.child_tasks.get(body.chat_id) is task:
                agent.child_tasks.pop(body.chat_id, None)
        stream.task.add_done_callback(release_task)
        async def events():
            async for event in stream.subscribe():
                yield json.dumps(event) + "\n"
        return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})

    @app.get("/api/chats")
    async def chats(request: Request, include_archived: bool = False):
        selected = request.state.workspace
        records = selected["chats"].visible_records(selected["agent"].children,request.state.session,include_archived=include_archived)
        active = selected["agent"].status()
        request.state.scope_guards.append(lambda:all(selected['chats'].can_read(
            selected['agent'].children.get(row['id']),request.state.session) for row in records))
        return {"chats":[{**record,"running":record["id"] in active} for record in records]}

    @app.post("/api/chats/{chat_id}/metadata")
    async def chat_metadata(chat_id: str, body: ChatMetadataRequest, request: Request):
        selected = request.state.workspace
        record = selected["agent"].children.get(chat_id)
        if record is None:
            raise HTTPException(404,"Choose a saved chat.")
        try:
            return selected["chats"].update(record,request.state.session,**body.model_dump())
        except ValueError as exc:
            raise HTTPException(403,str(exc)) from exc

    @app.get("/api/chats/{chat_id}")
    async def child_chat(chat_id: str, request: Request):
        agent = request.state.workspace["agent"]
        child = agent.children.get(chat_id)
        if child is None:
            raise HTTPException(404, "Choose an existing investigation.")
        try:
            record = request.state.workspace["chats"].get(child,request.state.session)
        except ValueError as exc:
            raise HTTPException(404,str(exc)) from exc
        if any(item.get('credential_requests') for item in child.get('messages',[])):
            agent.refresh_credential_requests(chat_id)
            record=request.state.workspace['chats'].get(child,request.state.session)
        if any(item.get('native_questions') for item in child.get('messages',[])):
            agent.refresh_native_questions(chat_id)
            record=request.state.workspace['chats'].get(child,request.state.session)
        if any(item.get('native_approvals') for item in child.get('messages', [])):
            agent.refresh_native_approvals(chat_id)
            record = request.state.workspace['chats'].get(child, request.state.session)
        if record.get('agent_setup'):
            record = {**record, 'agent_setup':agent.current_agent_setup(chat_id)}
        request.state.scope_guards.append(lambda:request.state.workspace['chats'].can_read(child,request.state.session))
        from custom_coordinator import offload
        try:
            capabilities=await offload(capability_snapshot,request,record.get('dataset'),chat_id)
        except HTTPException:
            # A participant reads the Tracking Agent's conversation in the business its creator
            # registered. Their capabilities stay their own: the ones outside that business.
            if record.get('participant') is not True:raise
            capabilities=await offload(capability_snapshot,request)
        return {**record, 'workspace_capabilities':capabilities,
            "running": child.get("running", False) and (chat_id in agent.active or chat_id in agent.child_tasks and not agent.child_tasks[chat_id].done())}

    def require_parent_control(record):
        if record and record.get("parent_id"):
            raise HTTPException(403, {"code": "child_chat_parent_controlled",
                "message": "Continue this work in the parent chat.",
                "parent_id": record["parent_id"]})

    @app.post("/api/chats/{chat_id}/approvals/{request_id}")
    async def answer_native_approval(chat_id: str, request_id: str, request: Request):
        selected = request.state.workspace
        agent = selected['agent']
        record = agent.children.get(chat_id)
        if not selected['chats'].can_write(record, request.state.session):
            raise HTTPException(403, 'Review provider actions in your workspace.')
        require_parent_control(record)
        owner = record['owner_principal_id']
        actor = selected['chats'].principal(request.state.session)
        organization = selected['organization_chats']
        member = organization.member(actor) if organization.owns(record) else None
        if actor != owner and not (member and member.get('role') == 'owner'):
            raise HTTPException(403, 'An owning account reviews provider actions.')
        raw = bytearray()
        try:
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 4096:
                    raise HTTPException(413, 'Send the approval decision only.')
            body = json.loads(raw)
            if (not isinstance(body, dict) or set(body) != {'action'}
                    or body['action'] not in {'allow_once', 'decline'}):
                raise ValueError()
            result = await agent.resolve_native_approval(chat_id, request_id, owner=owner, **body)
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(400, 'Review the provider action and choose a decision.') from None
        finally:
            raw.clear()
        accepted = result['status'] in {'approved', 'declined', 'delivering', 'dispatching', 'succeeded', 'rejected'}
        return Response(json.dumps({'request':result}), status_code=200 if accepted else 409,
            media_type='application/json', headers={'Cache-Control':'private, no-store'})

    @app.post("/api/chats/{chat_id}/questions/{request_id}")
    async def answer_native_question(chat_id: str, request_id: str, request: Request):
        selected = request.state.workspace
        agent = selected['agent']
        record = agent.children.get(chat_id)
        if not selected['chats'].can_write(record, request.state.session):
            raise HTTPException(403, 'Respond in a chat from your workspace.')
        require_parent_control(record)
        owner = record['owner_principal_id']
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise HTTPException(413, 'Keep the answers within the request size limit.')
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError()
            if body.get('action') == 'submit':
                if set(body) != {'action', 'answers'} or not isinstance(body['answers'], dict):
                    raise ValueError()
            elif body.get('action') in {'skip', 'timeout'}:
                if set(body) != {'action'}:
                    raise ValueError()
            else:
                raise ValueError()
            result = await agent.resolve_native_question(chat_id, request_id, owner=owner, **body)
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(400, 'Review this question and its requested answers.') from None
        finally:
            raw.clear()
        accepted = result['status'] in {'submitted', 'skipped', 'timed_out', 'delivering'}
        return Response(json.dumps({'request':result}), status_code=200 if accepted else 409,
                        media_type='application/json', headers={'Cache-Control':'private, no-store'})

    @app.post("/api/chats/{chat_id}/credential-requests/{request_id}")
    async def answer_credential_request(chat_id: str,request_id: str,request: Request):
        selected=request.state.workspace;agent=selected['agent']
        record=agent.children.get(chat_id)
        if not selected['chats'].can_write(record,request.state.session):
            raise HTTPException(403,'Respond in a chat from your workspace.')
        require_parent_control(record)
        if selected['organization_chats'].canonical(record):
            member = messaging_member(selected, request.state.session['principal']['id'])
            if not member or member['role'] != 'owner':
                raise HTTPException(403, 'An organization owner enters shared connector credentials.')
        owner=record['owner_principal_id']
        # Secret bodies bypass ordinary request models and their input-echoing
        # validation errors. Neither body nor values enter chat/tool arguments.
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>1024*1024:raise HTTPException(413,'Keep credential fields within their size limits.')
        try:
            body=json.loads(raw)
            if not isinstance(body,dict):raise ValueError()
            if body.get('action')=='submit':
                values=body.get('values')
                if (set(body)!={'action','values'} or not isinstance(values,dict) or len(values)>16
                        or any(not isinstance(k,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',k)
                               or not isinstance(v,str) or not 1<=len(v)<=16384 or '\x00' in v
                               for k,v in values.items())):raise ValueError()
            elif body.get('action')=='skip':
                if (set(body)!={'action','reason'} or not isinstance(body.get('reason'),str)
                        or not body['reason'].strip() or len(body['reason'])>1000 or '\x00' in body['reason']):raise ValueError()
            else:raise ValueError()
        except (ValueError,TypeError,RecursionError):
            raise HTTPException(400,'Use the requested private fields or provide a reason for skipping.') from None
        try:
            result=await agent.resolve_credential_request(chat_id,request_id,owner=owner,**body)
        except (ValueError,TypeError):
            raise HTTPException(400,'Review this credential request and its requested fields.') from None
        finally:
            raw.clear()
            if isinstance(body.get('values'),dict):body['values'].clear()
        payload=result if result['status']!='cancelled' else {
            'detail':'Continue setup in chat to request the current credential fields.','request':result}
        return Response(json.dumps(payload),status_code=409 if result['status']=='cancelled' else 200,
                        media_type='application/json',headers={'Cache-Control':'private, no-store'})

    @app.post("/api/chat/{chat_id}/stop")
    async def stop_chat(chat_id: str, request: Request):
        agent = request.state.workspace["agent"]
        if not request.state.workspace["chats"].can_write(agent.children.get(chat_id),request.state.session):
            raise HTTPException(403,"Control a chat from your workspace.")
        require_parent_control(agent.children.get(chat_id))
        try:
            child=agent.children.get(chat_id,{})
            if child.get('kind') == 'agent_conversation':
                definition=owned_agent(child['agent_id'],request,write=True)
                if any(run.get('status') == 'running' for run in definition.get('runs',[])):
                    await request.state.workspace['scheduler'].pause(child['agent_id'])
                    return {'ok':True,'result':{'stopped':True}}
            return await agent.control(chat_id, "stop")
        except (ValueError, RuntimeError, TimeoutError) as exc:
            raise HTTPException(409, str(exc) or "The agent control timed out.") from exc

    @app.post("/api/chat/{chat_id}/steer")
    async def steer_chat(chat_id: str, body: SteerRequest, request: Request):
        agent = request.state.workspace["agent"]
        if not request.state.workspace["chats"].can_converse(agent.children.get(chat_id),request.state.session):
            raise HTTPException(403,"Steer a chat from your workspace.")
        require_parent_control(agent.children.get(chat_id))
        participant = request.state.workspace["chats"].participant(agent.children.get(chat_id),request.state.session)
        if participant and (body.skill_ids or body.clip_attachments):
            raise HTTPException(400,'Send text and your own images in this agent’s conversation.')
        try:
            request.state.workspace['chats'].validate_attachments(body.attachments,request.state.session,write=True)
        except ValueError as exc:
            raise HTTPException(403,str(exc)) from exc
        try:
            return await agent.control(chat_id, "steer", body.message, body.attachments, body.clip_attachments,
                **({'sender':{'principal_id':request.state.session['principal']['id'],
                    'display_name':request.state.session['principal']['display_name'],'channel':'web'}}
                    if participant or agent.children.get(chat_id,{}).get('kind') == 'organization_chat' else {}),
                **({"skill_ids":body.skill_ids} if body.skill_ids else {}))
        except (ValueError, RuntimeError, TimeoutError) as exc:
            raise HTTPException(409, str(exc) or "The agent control timed out.") from exc

    @app.post("/api/attachments")
    async def attachment(file: UploadFile, request: Request):
        from attachments import AttachmentStore
        media_cache = AttachmentStore(request.state.workspace["state"])
        data = await file.read(10 * 1024 * 1024 + 1)
        try:
            saved=await asyncio.to_thread(media_cache.save_attachment, data, file.filename)
            try:
                await asyncio.to_thread(request.state.workspace['chats'].register_attachment,saved['id'],request.state.session)
            except BaseException:
                # A registration failure leaves no publicly addressable orphan.
                (request.state.workspace['state']/'attachments'/(saved['id']+'.png')).unlink(missing_ok=True)
                raise
            return saved
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()

    @app.get("/attachments/{attachment_id}.png")
    async def attachment_image(attachment_id: str, request: Request):
        state = request.state.workspace["state"]
        if not re.fullmatch(r"[a-f0-9]{32}", attachment_id):
            raise HTTPException(404, "Choose an uploaded image.")
        if not request.state.workspace['chats'].can_read_attachment(attachment_id,request.state.session):
            raise HTTPException(404,'Choose an uploaded image belonging to the selected person.')
        path = state / "attachments" / (attachment_id + ".png")
        if not path.is_file():
            raise HTTPException(404, "Choose an uploaded image.")
        return FileResponse(path, media_type="image/png",headers={'Cache-Control':'private, no-store'})


    def file_owner(request, *, write=False):
        permissions = request.state.workspace["chats"]
        if write and not workspace_writable(request.state.session):
            raise HTTPException(403, "Return to your workspace to save files.")
        principal = permissions.principal(request.state.session)
        if principal is None:
            raise HTTPException(403, "Sign in to open your files.")
        return principal






    def file_principal(request, file_id, *, write=False):
        principal = file_owner(request, write=write)
        selected = request.state.workspace
        # Resolve only the exact requested company's record. A private user's
        # record never becomes shared merely because a company chat links it.
        with selected['files'].connect() as db:
            row = db.execute('SELECT owner,chat_id,dataset FROM files WHERE id=? UNION ALL '
                'SELECT owner,chat_id,dataset FROM export_chats WHERE id=?', (file_id,file_id)).fetchone()
        if row and row[1] and row[0] in {principal,selected['organization_chats'].owner}:
            record=selected['agent'].children.get(row[1])
            def history_guard():
                if record is not None:return selected['chats'].can_read(record,request.state.session)
                from scope_host import history_allowed
                return row[0]==principal and history_allowed(selected,{'id':row[1],'dataset':row[2],
                    'owner_principal_id':principal,'messages':[{'role':'assistant'}]},principal)
            if not history_guard():
                raise HTTPException(404,'Choose a file from your current conversation scope.')
            request.state.scope_guards.append(history_guard)
        if row and row[0] == selected['organization_chats'].owner:
            record = selected['agent'].children.get(row[1])
            permitted = selected['chats'].can_write if write else selected['chats'].can_read
            if (not selected['organization_chats'].owns(record) or not permitted(record, request.state.session)
                    or record.get('dataset') != row[2]):
                raise HTTPException(404, 'Choose a file from the current company conversation.')
            return row[0]
        try:
            selected['vertical'].authorize_file(request,file_id,principal)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return principal

    @app.get("/api/files")
    async def list_files(request: Request, chat_id: str | None = None, dataset: str | None = None):
        principal = file_owner(request)
        selected = request.state.workspace
        if chat_id and not selected["chats"].can_read(selected["agent"].children.get(chat_id), request.state.session):
            raise HTTPException(404, "Choose a chat from your workspace.")
        if chat_id and selected['organization_chats'].owns(selected['agent'].children.get(chat_id)):
            principal = selected['organization_chats'].owner
        try:
            rows=await asyncio.to_thread(selected["files"].list,owner_principal_id=principal,chat_id=chat_id,dataset=dataset)
            allowed=[]
            for row in rows:
                try:file_principal(request,row["id"])
                except HTTPException:continue
                allowed.append(row)
            return {"files":allowed}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/files/{file_id}")
    async def get_file(file_id: str, request: Request):
        try:
            return await asyncio.to_thread(request.state.workspace["files"].get, file_id,
                owner_principal_id=file_principal(request, file_id))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/files/{file_id}/content")
    async def file_content(file_id: str, request: Request):
        try:
            result = await asyncio.to_thread(request.state.workspace["files"].content, file_id,
                owner_principal_id=file_principal(request, file_id))
            return Response(json.dumps(result), media_type="application/json", headers={"Cache-Control": "private, no-store"})
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.patch("/api/files/{file_id}")
    async def rename_file(file_id: str, body: FileTitleRequest, request: Request):
        principal = file_principal(request, file_id, write=True)
        files = request.state.workspace["files"]
        try:
            await asyncio.to_thread(files.get, file_id, owner_principal_id=principal)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            return await asyncio.to_thread(files.rename, file_id, body.title, owner_principal_id=principal)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/files/{file_id}/opened")
    async def mark_file_opened(file_id: str, body: FileOpenedRequest, request: Request):
        principal = file_principal(request, file_id, write=True)
        try:
            return await asyncio.to_thread(request.state.workspace["files"].opened, file_id,
                                           owner_principal_id=principal)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/files/{file_id}/download")
    async def download_file(file_id: str, request: Request):
        principal = file_principal(request, file_id)
        selected = request.state.workspace
        try:
            result = await asyncio.to_thread(selected["files"].content, file_id, owner_principal_id=principal)
            record = result["file"]
            if record["kind"] == "media_export":
                path = await selected["vertical"].file_download(record,principal)
                return FileResponse(path, media_type="application/zip", filename=record["name"],
                    headers={"Cache-Control": "private, no-store"})
            from urllib.parse import quote
            _, raw = await asyncio.to_thread(selected["files"].download, file_id, owner_principal_id=principal)
            return Response(raw, media_type=record["media_type"],
                headers={"Cache-Control": "private, no-store",
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(record["name"], safe="")})
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/files/{file_id}/preview")
    async def preview_file(file_id: str, request: Request):
        selected = request.state.workspace
        principal = file_principal(request, file_id)
        try:
            record = await asyncio.to_thread(selected["files"].get, file_id, owner_principal_id=principal)
            native=record.get("representation")=="native_original"
            native_image=(native and record.get("inline_preview_type")=="image"
                and record["media_type"] in {"image/png","image/jpeg","image/webp"})
            from stored_media import MEDIA_TYPES, media_response
            native_media = native and record["media_type"] in MEDIA_TYPES.get(record.get("inline_preview_type"), set())
            if not (native_image or native_media or (not native and record["media_type"]=="application/pdf")):
                raise HTTPException(400, "Open the text preview or download this file.")
            _, raw = await asyncio.to_thread(selected["files"].download, file_id, owner_principal_id=principal)
            if native_media:
                return media_response(raw, record["media_type"], request.headers.get("range"))
            headers={"Cache-Control":"private, no-store","Content-Disposition":"inline"}
            if native_image:headers["Content-Security-Policy"]="default-src 'none'; sandbox; frame-ancestors 'self'"
            return Response(raw, media_type=record["media_type"], headers=headers)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


















    primary=workspace('demo')
    app.state.agent=primary['agent']
    app.state.connection=primary['connection']
    if 'index' in primary:app.state.index=primary['index']
    if vertical_host:
        vertical_host.register_routes(app,host,SimpleNamespace(connection_identity=connection_identity,
            custom_identity=custom_identity,require_parent_control=require_parent_control,file_owner=file_owner))
    # Exact selected-extension aliases precede the generic shared asset fallback.
    app.get('/static/harness/{asset:path}')(shared_static)
    return app


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    os.umask(0o077)
    uvicorn.run(create_app(args.state), host="127.0.0.1", port=args.port, access_log=False)
