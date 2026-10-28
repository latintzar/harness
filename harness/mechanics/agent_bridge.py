"""Workspace chat bridge with managed OpenAI turns and admitted harness tools."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from agent_auth import AgentAuth


# Native app-server code mode joins dynamic text/image items into newline-separated strings.
# Rendering image URLs explicitly keeps base64 out of text output and supplies model vision.
FRAME_OUTPUT_RENDER_JS = 'for (const line of r.split("\\n")) { if (line.startsWith("data:image/")) image(line); else if (line.trim()) text(line); }'


def system_instructions():
    """Compose product instructions from the current vertical for each managed turn."""
    import pack
    from customer_workflows import CUSTOMER_WORKFLOW_INSTRUCTIONS
    from harness_tools import developer_prompt
    template = pack.active()
    return '\n'.join((template.prompt('system'), CUSTOMER_WORKFLOW_INSTRUCTIONS, developer_prompt(template)))


def __getattr__(name):
    # Older proof callers import SYSTEM; runtime paths compose instructions per turn.
    if name == 'SYSTEM':
        return system_instructions()
    raise AttributeError(name)


# A turn that a participant other than the creator started keeps the phone answer to
# that participant. Every other tool resolves as the conversation's creator.
PARTICIPANT_TOOLS = frozenset({'send_message', 'send_alert'})
CREATOR_TOOLS = ('This agent’s creator runs its searches, files, connectors and settings from their own messages. '
    'Answer this participant from the conversation and its alerts.')
PARTICIPANT_SAVED = 'Your message is saved in this conversation. {creator} runs this agent’s searches and answers from their own messages.'


def participant_turn(agent, chat_id):
    """The participant whose own message started this conversation's turn, else None.

    A saved agent's tools, searches, files and connected accounts resolve as
    its creator. A turn that another participant's message started therefore
    keeps the conversation's own history and the phone answer to that
    participant; everything that resolves as the creator waits for the
    creator's own message. The mark lasts for the whole turn, also once the
    creator adds guidance to it. The lineage links are host-authored.
    """
    children = getattr(agent, 'children', {})
    record, seen = children.get(chat_id) or {}, set()
    while len(seen) < 64:
        parent = record.get('native_root_id') or record.get('parent_id')
        if not parent or parent in seen or parent not in children:
            break
        seen.add(parent)
        record = children[parent]
    return getattr(agent, '_turn_guests', {}).get(record.get('id', chat_id))


def tool_specs(children=True, *, agent=None):
    registry = workspace_registry(agent)
    return registry.specs(write=children)




def workspace_registry(agent=None):
    if agent is not None and getattr(agent, "tool_registry", None) is not None:
        return agent.tool_registry
    import pack
    from tool_families import build_registry
    registry = build_registry(agent, families=pack.active().import_tool_families())
    if agent is not None:
        agent.tool_registry = registry
    return registry


class HarnessAgent:
    def __init__(self, state: Path, retrieval=None, corpus=None, *, tool_only: bool = False):
        self.state, self.retrieval, self.corpus = state, retrieval, corpus
        self.tool_registry = None
        self.vertical = None
        self.tool_only = tool_only
        self.auth = None if tool_only else AgentAuth(state)
        self.api_verified_at = None
        self.isolated_launcher = None
        self.plugin_package_store = None
        self.native_questions = None
        self.native_approvals = None
        if not tool_only:
            from native_questions import NativeQuestions
            self.native_questions = NativeQuestions(state)
            self.native_questions.recover()
            from native_approvals import NativeApprovals
            self.native_approvals = NativeApprovals(state)
            self.native_approvals.recover()
        self.active = {}
        self.child_tasks = {}
        self.children = {}
        self.focus_tracker = None
        self.automation_store = None
        self.camera_store = None
        self.export_store = None
        self.file_store = None
        self.connection_store = None
        self.custom_connector_store = None
        self.workspace_capabilities = None
        self.credential_waiters = {}
        self.clarification_waiters = {}
        self.investigation_plans = None
        self.business_store = None
        self.skill_store = None
        # Local MCP reuses registered tools without taking ownership of the
        # web runtime, its pending questions, credentials or chat registry.
        if tool_only:
            return
        self.usage_path = state / "native-usage.json"
        self.usage = json.loads(self.usage_path.read_text()) if self.usage_path.exists() else {}
        self.chat_dir = state / "chats"
        self.chat_dir.mkdir(mode=0o700, exist_ok=True)
        for path in self.chat_dir.glob("*.json"):
            data = json.loads(path.read_text())
            self.children[data["id"]] = data
        self.scope_path = state / "agent-chat-scopes.json"
        self.scopes = json.loads(self.scope_path.read_text()) if self.scope_path.exists() else {}
        self.registry_path = state / "agent-chats.json"
        self.registry = json.loads(self.registry_path.read_text()) if self.registry_path.exists() else {}
        self.toolsets_path = state / "native-toolsets.json"
        self.toolsets = json.loads(self.toolsets_path.read_text()) if self.toolsets_path.exists() else {}
        # Native processes do not survive an application restart. Preserve their
        # request cards and retire orphaned waits before accepting private input.
        if (self.state/'custom-connectors'/'connectors.sqlite').exists():
            self._custom_connectors().cancel_credential_requests(reason='runtime_restarted')
            for identity in self.children:
                self.refresh_credential_requests(identity)

    def result_fields(self, name, content, success):
        renderer = getattr(self.vertical, 'result_fields', None)
        fields = renderer(name, content, success) if renderer else {}
        return fields

    def tool_result_fields(self, name, content, success):
        fields = self.result_fields(name, content, success)
        return {**fields, 'tool_fields': copy.deepcopy(fields)} if fields else {}

    def _plans(self):
        return self.vertical.plans()

    def _business(self):
        if self.business_store is None:
            from business_context import BusinessStore
            self.business_store = BusinessStore(self.state)
        return self.business_store

    def _skills(self):
        if self.skill_store is None:
            from skills import SkillStore
            self.skill_store = SkillStore(self.state)
        return self.skill_store

    def _native_skills(self):
        store = getattr(self, 'plugin_package_store', None)
        if store is None:
            # Compatibility for the deployed migration source and its fixtures.
            launcher = getattr(self, 'isolated_launcher', None)
            store = getattr(launcher, 'package_store', None) if getattr(launcher, 'enabled', False) else None
        if store is None:
            return None
        from native_skills import NativePluginSkills, NativeSkillError
        from workspace_identity import workspace_scope
        if store.state != self.state.absolute() or workspace_scope(self.state) != store.company_scope:
            raise NativeSkillError("Refresh installed skills in this workspace.")
        return NativePluginSkills(store)

    def _connections(self):
        if self.connection_store is None:
            from mcp_connections import MCPConnections
            from provider_configuration import registered_clients_for, remote_providers_for
            origin = os.environ.get('ROCOTO_PUBLIC_ORIGIN', os.environ.get('ROCOTO_VIDEO_PUBLIC_ORIGIN', ''))
            self.connection_store = MCPConnections(self.state,
                public_origin=origin,
                registered_clients=registered_clients_for(self.state, public_origin=origin),
                remote_providers=remote_providers_for(self.state, public_origin=origin))
        return self.connection_store

    def _files(self):
        if self.file_store is None:
            from chat_files import ChatFiles
            self.file_store = ChatFiles(self.state, exports=self.export_store)
        return self.file_store

    def _business_maps(self):
        if getattr(self, 'business_map_store', None) is None:
            from business_maps import BusinessMaps
            self.business_map_store = BusinessMaps(self.state)
        return self.business_map_store

    def _custom_connectors(self):
        if self.custom_connector_store is None:
            from custom_connectors import CustomConnectors
            self.custom_connector_store=CustomConnectors(self.state)
        return self.custom_connector_store

    def _custom_execution(self):
        if getattr(self, 'custom_execution_manager', None) is None:
            from custom_execution import CustomExecution
            self.custom_execution_manager = CustomExecution(self._custom_connectors())
        return self.custom_execution_manager

    def _custom_setup(self):
        from custom_setup import CustomSetup
        return CustomSetup(self._custom_execution())

    def _storage_runtime(self):
        factory = getattr(self, 'storage_runtime_factory', None)
        if factory is None:
            raise ValueError('Choose a workspace with an installed storage connection provider.')
        return factory()

    def _publish_credential(self,chat_id,record):
        chat=self.children.get(chat_id)
        if chat is None:return
        answers=[item for item in chat.get('messages',[]) if item.get('role')=='assistant']
        answer=next((item for item in answers if any(row.get('id')==record['id']
                    for row in item.get('credential_requests',[]))),answers[-1] if answers else None)
        if answer is None:return
        prior=next((row for row in answer.get('credential_requests',[]) if row.get('id')==record['id']),None)
        if prior==record:return
        event={'type':'credential_request' if record['status']=='pending' else 'credential_request_resolved',
               'request':copy.deepcopy(record)}
        self._record_event(answer,event)
        self._save_child(chat)
        run=self.active.get(chat_id)
        if run is not None:run.setdefault('tool_events',asyncio.Queue()).put_nowait(event)

    def refresh_credential_requests(self,chat_id):
        chat=self.children.get(chat_id,{})
        owner=chat.get('owner_principal_id')
        if not owner or chat.get('parent_id'):return
        for record in self._custom_connectors().credential_requests(owner=owner,chat_id=chat_id):
            self._publish_credential(chat_id,record)

    def _cancel_credential_waits(self,chat_id,reason):
        chat=self.children.get(chat_id,{})
        owner=chat.get('owner_principal_id')
        if not owner or getattr(self,'custom_connector_store',None) is None:return
        for record in self._custom_connectors().cancel_credential_requests(chat_id=chat_id,owner=owner,reason=reason):
            self._publish_credential(chat_id,record)
            waiter=self.credential_waiters.get(record['id'])
            if waiter:waiter['event'].set()

    async def resolve_credential_request(self,chat_id,request_id,*,owner,action,values=None,reason=None):
        chat=self.children.get(chat_id,{})
        if chat.get('owner_principal_id')!=owner or chat.get('parent_id'):
            raise ValueError('Respond in the owning parent chat.')
        store=self._custom_connectors()
        record=store.credential_request(request_id,owner=owner,chat_id=chat_id)
        waiter=self.credential_waiters.get(request_id)
        run=self.active.get(chat_id)
        if record['status']=='pending' and (not waiter or waiter['chat_id']!=chat_id or waiter['owner']!=owner
                or run is None or run['cancel'].is_set()):
            self._cancel_credential_waits(chat_id,'stopped' if run and run['cancel'].is_set() else 'runtime_restarted')
            record=store.credential_request(request_id,owner=owner,chat_id=chat_id)
        if record['status']!='cancelled':
            # No await between the live-wait check and transactional submission:
            # Stop and Submit linearize on this event loop and the SQLite record.
            record=store.resolve_credential_request(request_id,owner=owner,chat_id=chat_id,
                action=action,values=values,reason=reason)
        self._publish_credential(chat_id,record)
        if waiter:waiter['event'].set()
        return record

    async def _wait_for_credentials(self,chat_id,connector_id,reason,scope):
        chat=self.children[chat_id];owner=chat['owner_principal_id'];run=self.active.get(chat_id)
        if run is None or run['cancel'].is_set():raise ValueError('Continue setup in an active parent chat.')
        remaining=min(30,max(1,int(run.get('deadline',time.monotonic()+30)-time.monotonic())))
        store=self._custom_connectors()
        record=store.request_credentials(connector_id,owner=owner,chat_id=chat_id,collection=scope,
                                         reason=reason,expires_in=remaining)
        waiter={'chat_id':chat_id,'owner':owner,'event':asyncio.Event()}
        self.credential_waiters[record['id']]=waiter
        run['status']='waiting_for_credentials'
        self._publish_credential(chat_id,record)
        try:
            while True:
                if run['cancel'].is_set():self._cancel_credential_waits(chat_id,'stopped')
                record=store.credential_request(record['id'],owner=owner,chat_id=chat_id)
                if record['status']!='pending':
                    self._publish_credential(chat_id,record)
                    return record
                # This polls local request truth only, including source revision
                # and expiry; it never sends model or provider requests.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(waiter['event'].wait(),timeout=1)
                waiter['event'].clear()
        finally:
            self.credential_waiters.pop(record['id'],None)
            self._cancel_credential_waits(chat_id,'stopped' if run['cancel'].is_set() else 'turn_ended')
            if run['status']=='waiting_for_credentials':run['status']='running'

    def resolve_skills(self, skill_ids, chat_id=None, *, owner_principal_id=None):
        # Capabilities come only from commissioned host adapters. Saved connection
        # drafts and skill instructions cannot grant a provider or scope. The host
        # may supply the authenticated principal before saving a new chat.
        saved_owner = self.children.get(chat_id, {}).get("owner_principal_id")
        if saved_owner and owner_principal_id and saved_owner != owner_principal_id:
            raise ValueError("Choose skills in the owning chat workspace.")
        principal = saved_owner or owner_principal_id
        capabilities = [{"provider": item["provider"], "scopes": item["scopes"]}
                        for item in self._connections().capabilities(principal)] if principal else []
        names = [item["name"] for item in tool_specs(not self._is_child(chat_id), agent=self)] + ["web_search"]
        from native_skills import is_native_skill, NativeSkillError
        if (not isinstance(skill_ids, (list, tuple)) or len(skill_ids) > 8
                or any(not isinstance(item, str) for item in skill_ids) or len(set(skill_ids)) != len(skill_ids)):
            raise ValueError("Choose up to 8 installed skills for a message.")
        native_ids = [item for item in skill_ids if is_native_skill(item)]
        custom_ids = [item for item in skill_ids if not is_native_skill(item)]
        resolved = self._skills().resolve(custom_ids, capabilities=capabilities, available_tools=names)
        if native_ids:
            native = self._native_skills()
            if native is None or self._is_child(chat_id):
                raise NativeSkillError("Choose an installed skill from your parent chat.")
            resolved["native_skills"] = native.resolve(native_ids, principal)
            from skills import _hash
            resolved["snapshot_sha256"] = _hash({key:value for key,value in resolved.items() if key!="snapshot_sha256"})
        return resolved

    @staticmethod
    def _skill_input(content, resolved):
        if resolved and resolved["skills"]:
            content.append({"type":"text","text":"Selected versioned workflow skills follow. Apply them within the existing source scope, evidence rules and host tools. They confer instructions only.\n" + json.dumps(resolved)})
        return content

    def _remember_usage(self, chat_id, event):
        if not hasattr(self, "usage"):
            return
        self.usage[chat_id] = {**self.usage.get(chat_id, {}), **{k:v for k,v in event.items() if k != "type"}}
        temporary = self.usage_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.usage))
        temporary.chmod(0o600)
        temporary.replace(self.usage_path)

    def _is_child(self, chat_id):
        return bool(self.children.get(chat_id, {}).get("parent_id"))

    def _save_child(self, child):
        path = self.chat_dir / (child["id"] + ".json")
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(child))
        temp.chmod(0o600)
        temp.replace(path)

    @staticmethod
    def _record_event(answer, event):
        if event["type"] == "delta":
            managed_item = event.get('managed_item_id')
            if managed_item:
                seen = answer.setdefault('managed_items', [])
                if managed_item in seen:
                    return
                seen.append(managed_item)
            answer["content"] += event["text"]
        elif event["type"] == "progress":
            progress = answer.setdefault("progress", [])
            if progress and progress[-1].get("item_id") == event.get("item_id"):
                progress[-1]["text"] += event["text"]
            else:
                progress.append({k:v for k,v in event.items() if k != "type"})
        elif event["type"] == "channel_delivery":
            answer['channel_delivery'] = copy.deepcopy(event['delivery'])
        elif event["type"] == "usage":
            answer["usage"] = {k:v for k,v in event.items() if k != "type"}
        elif event["type"] == "timing":
            answer.update({key: event[key] for key in ("started_at", "completed_at", "elapsed_ms", "status") if key in event})
        elif event["type"] in {"credential_request","credential_request_resolved"}:
            record=copy.deepcopy(event['request'])
            requests=answer.setdefault('credential_requests',[])
            existing=next((item for item in requests if item.get('id')==record['id']),None)
            if existing is None:requests.append(record)
            else:existing.update(record)
        elif event["type"] in {"user_input_request","user_input_resolved"}:
            record=copy.deepcopy(event['request'])
            requests=answer.setdefault('native_questions',[])
            existing=next((item for item in requests if item.get('id')==record['id']),None)
            if existing is None:requests.append(record)
            else:existing.update(record)
        elif event["type"] in {"approval_request", "approval_resolved"}:
            record = copy.deepcopy(event['request'])
            requests = answer.setdefault('native_approvals', [])
            existing = next((item for item in requests if item.get('id') == record['id']), None)
            if existing is None:
                requests.append(record)
            else:
                existing.update(record)
        elif event["type"] == "agent":
            record = copy.deepcopy(event['agent'])
            cards = answer.setdefault('agents', [])
            existing = next((item for item in cards if item.get('id') == record['id']), None)
            if existing is None:
                cards.append(record)
            else:
                existing.update(record)
        elif event["type"] == "artifact":
            artifact = event["artifact"]
            artifacts = answer.setdefault("artifacts", [])
            existing = next((item for item in artifacts if item.get("id") == artifact.get("id")), None)
            if existing is None:
                artifacts.append(artifact)
            else:
                existing.update(artifact)
        elif event["type"] == "evidence_presentation":
            answer["evidence_presentation"] = copy.deepcopy(event["presentation"])
        elif event["type"] == "business_map":
            answer["business_map"] = copy.deepcopy(event["map"])
        elif event["type"] == "results":
            for result in event["results"]:
                existing = next((r for r in answer["results"] if r["id"] == result["id"]), None)
                if existing is None:
                    answer["results"].append(result)
                else:
                    existing.update(result)
        elif event["type"] == "tool_start":
            answer["tools"].append({"id":event.get("id"), "name":event["name"], "arguments":event["arguments"], "status":"running"})
        elif event["type"] == "tool_end":
            tool = next((t for t in reversed(answer["tools"]) if (t.get("id") == event["id"] if event.get("id") else t["name"] == event["name"]) and t.get("status") == "running"), None)
            if tool is not None:
                tool["status"] = "complete" if event.get("success") else "failed"
                if event.get("output"):
                    tool["output"] = event["output"]
                if isinstance(event.get('tool_fields'), dict):
                    tool.update(copy.deepcopy(event['tool_fields']))
        elif event["type"] == "subchat":
            children = answer.setdefault("subchats", [])
            old = next((child for child in children if child.get("chat_id")==event.get("chat_id")),None)
            data = {k:v for k,v in event.items() if k != "type"}
            if old is None: children.append(data)
            else: old.update(data)
        elif event["type"] == "error":
            answer["error"] = event["message"]
        elif event["type"] == "done":
            answer["status"] = event.get("status")
            answer.update({key: event[key] for key in ("started_at", "completed_at", "elapsed_ms") if key in event})
            if event.get("status") == "interrupted":
                answer["stopped"] = True

    @staticmethod
    def _complete_answer_timing(answer):
        # A disconnected consumer cannot receive a final event. Keep the measured
        # wall-clock boundary in persisted history when its generator is cancelled.
        if answer.get("started_at") and not answer.get("completed_at"):
            finished = datetime.now(timezone.utc)
            answer["completed_at"] = finished.isoformat()
            answer["elapsed_ms"] = max(0, round((finished - datetime.fromisoformat(answer["started_at"])).total_seconds() * 1000))

    def _remember(self, chat_id, thread_id):
        self.registry[chat_id] = thread_id
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.registry))
        tmp.chmod(0o600)
        tmp.replace(self.registry_path)

    def _remember_toolset(self, thread_id, digest, opened):
        self.toolsets[thread_id] = {"digest":digest, **{k:opened[k] for k in
            ("migrated_from", "copied_messages", "omissions", "image_provenance", "migration_export_sha256") if k in opened}}
        temporary = self.toolsets_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.toolsets))
        temporary.chmod(0o600)
        temporary.replace(self.toolsets_path)

    def status(self):
        return {key: {"status": value["status"], "thread_id": value.get("thread_id"),
                      "turn_id": value.get("turn_id"), "tool": value.get("tool"), "started_at": value.get("started_at")}
                for key, value in self.active.items()}

    def _clip_attachments(self, ids=(), dataset=None):
        if not ids:
            return []
        return self.vertical.clip_attachments(ids, dataset)

    def _input(self, message, attachments=(), clip_attachments=(), dataset=None):
        content = [{"type": "text", "text": message}]
        if clip_attachments:
            selected = self._clip_attachments(clip_attachments, dataset)
            content.append({"type":"text", "text":"Attached Gallery intervals (source references, not observations):\n" + json.dumps(selected)})
        for attachment in attachments:
            if not isinstance(attachment, str) or len(attachment) != 32 or any(c not in "0123456789abcdef" for c in attachment):
                raise ValueError("Choose an uploaded image.")
            path = self.state / "attachments" / (attachment + ".png")
            if not path.is_file():
                raise ValueError("Upload the image again.")
            content.append({"type": "localImage", "path": str(path)})
        return content

    def capability_snapshot(self, chat_id, scope):
        provider = getattr(self, 'workspace_capabilities', None)
        if provider is None:
            return None
        record = self.children.get(chat_id, {})
        if not record.get('owner_principal_id') or record.get('dataset') != scope:
            raise ValueError('Read capabilities from the owning chat and business.')
        return provider.snapshot(scope, principal=record['owner_principal_id'], chat_id=chat_id,
                                 child=bool(record.get('parent_id')), actor=self._authority_principal(chat_id))

    def agent_setup_context(self, chat_id):
        record = self.children.get(chat_id, {})
        setup = record.get('agent_setup')
        if not setup or record.get('parent_id'):
            return None
        if setup.get('agent_id'):
            saved = self.automation_store.get(setup['agent_id'])
            if saved.get('owner_principal_id') != record.get('owner_principal_id') or saved['dataset'] != record.get('dataset'):
                raise ValueError('Configure an agent belonging to this person and business.')
            return {k: saved[k] for k in ('id', 'revision', 'status', *self.automation_store.definition_model.model_fields)}
        if setup.get('template_id'):
            from agent_catalogue import catalogue
            template = next((t for t in catalogue()['templates'] if t['id'] == setup['template_id']), None)
            if template is None:
                raise ValueError('Choose an available agent template.')
            return {'template': template}
        return setup

    def current_agent_setup(self, chat_id):
        setup = self.children.get(chat_id, {}).get('agent_setup')
        if not setup:
            return None
        try:
            context = self.agent_setup_context(chat_id)
        except (ValueError, KeyError):
            return {'status':'unavailable'}
        if context is None:
            return None
        effective = context.get('template', context)
        return {**setup, 'mode':effective['mode']}

    def _approval_history_scope(self, root_chat_id, owner):
        from native_approvals import ApprovalScope
        from workspace_identity import workspace_scope
        return ApprovalScope(workspace_scope(self.state), owner, root_chat_id, 'history',
            self.registry.get(root_chat_id) or 'closed')

    def refresh_native_approvals(self, chat_id):
        store = self.native_approvals
        chat = self.children.get(chat_id, {})
        owner = chat.get('owner_principal_id')
        root = chat.get('native_root_id') or chat_id
        if store is None or not owner or self.children.get(root, {}).get('owner_principal_id') != owner:
            return
        scope = self._approval_history_scope(root, owner)
        latest = {item['id']:item for item in store.for_chat(scope=scope)}
        changed = False
        for message in chat.get('messages', []):
            for request in message.get('native_approvals', []):
                current = latest.get(request.get('id'))
                if current is not None and current != request:
                    request.clear(); request.update(current); changed = True
        if changed:
            self._save_child(chat)

    async def revoke_provider_approvals(self, provider, *, owner):
        if self.native_approvals is None:return
        from workspace_identity import workspace_scope
        from native_approvals import NativeApprovalRouter
        company = workspace_scope(self.state)
        # The connection broker has already fenced this owner's generation.
        # Close durable inactive requests as well as live native waits.
        self.native_approvals.invalidate(company_id=company, owner=owner,
            provider=provider, reason='connection_changed')
        for chat_id, run in list(self.active.items()):
            router = run.get('approval_router')
            if (not isinstance(router, NativeApprovalRouter) or router.store is not self.native_approvals
                    or router.scope.company_id != company or router.scope.owner != owner):continue
            for event in await router.revoke_provider(provider):
                run['tool_events'].put_nowait(event)
        for chat_id, record in list(self.children.items()):
            if record.get('owner_principal_id') == owner:
                self.refresh_native_approvals(chat_id)

    async def resolve_native_approval(self, chat_id, request_id, *, owner, action):
        from native_approvals import NativeApprovalRouter
        chat = self.children.get(chat_id, {})
        if chat.get('owner_principal_id') != owner or chat.get('parent_id') or self.native_approvals is None:
            raise ValueError('Review provider actions in the owning parent chat.')
        scope = self._approval_history_scope(chat_id, owner)
        # Lookup checks the saved company, owner and parent even when the live
        # native generation has ended. History scope cannot authorize dispatch.
        self.native_approvals.get(request_id, scope=scope)
        run = self.active.get(chat_id)
        router = run.get('approval_router') if run else None
        if not isinstance(router, NativeApprovalRouter):
            result = self.native_approvals.interrupt(request_id, scope=scope)
        else:
            if (router.store is not self.native_approvals or router.scope.owner != owner
                    or router.scope.root_chat_id != chat_id or router.scope.company_id != scope.company_id
                    or router.scope.generation != run.get('generation') or router.cancelled is not run.get('cancel')):
                raise ValueError('Use the approval from this active parent chat.')
            event = await router.answer(request_id, owner=owner, action=action)
            result = event['request']
            if self.active.get(chat_id) is run:
                run['tool_events'].put_nowait(event)
        self.refresh_native_approvals(chat_id)
        return result

    def refresh_native_questions(self, chat_id):
        chat=self.children.get(chat_id,{})
        owner=chat.get('owner_principal_id')
        if not owner:return
        root=chat.get('native_root_id') or chat_id
        root_record=self.children.get(root,{})
        if root_record.get('owner_principal_id')!=owner:return
        latest={item['id']:item for item in self.native_questions.for_chat(root,owner=owner)}
        changed=False
        for message in chat.get('messages',[]):
            for request in message.get('native_questions',[]):
                current=latest.get(request.get('id'))
                if current is not None and current!=request:
                    request.clear();request.update(current);changed=True
        if changed:self._save_child(chat)

    def _publish_question(self, chat_id, record):
        chat=self.children.get(chat_id)
        if chat is None:return
        messages=[item for item in chat.get('messages',[]) if item.get('role')=='assistant']
        answer=next((item for item in messages if any(row.get('id')==record['id']
            for row in item.get('native_questions',[]))),messages[-1] if messages else None)
        if answer is None:return
        event={'type':'user_input_request' if record['status']=='pending' else 'user_input_resolved','request':copy.deepcopy(record)}
        self._record_event(answer,event);self._save_child(chat)
        run=self.active.get(chat_id)
        if run is not None:run['tool_events'].put_nowait(event)

    async def _deliver_clarification(self, chat_id, request_id, waiter, run, response):
        if (self.active.get(chat_id) is not run or run['cancel'].is_set()
                or self.clarification_waiters.get(request_id) is not waiter):
            raise RuntimeError('This clarification has closed.')
        waiter['response']=copy.deepcopy(response)

    async def _wait_for_clarification(self, chat_id, arguments, scope):
        chat=self.children.get(chat_id,{})
        owner=chat.get('owner_principal_id');run=self.active.get(chat_id)
        if (not owner or chat.get('parent_id') or chat.get('dataset')!=scope or run is None
                or not run.get('generation') or run['cancel'].is_set()):
            raise ValueError('Ask questions from the active owning conversation.')
        if any(item['chat_id']==chat_id for item in self.clarification_waiters.values()):
            raise ValueError('Complete the current clarification before asking another.')
        store=self.native_questions
        record=store.open_host(arguments,root_chat_id=chat_id,owner=owner,generation=run['generation'])
        waiter={'chat_id':chat_id,'run':run,'event':asyncio.Event(),'response':{'answers':{}}}
        self.clarification_waiters[record['id']]=waiter
        run['status']='waiting_for_answer';self._publish_question(chat_id,record)
        async def deliver(native_id,response):
            await self._deliver_clarification(chat_id,record['id'],waiter,run,response)
        try:
            while True:
                if self.active.get(chat_id) is not run or run['cancel'].is_set():
                    record=store.interrupt(record['id'],owner=owner,root_chat_id=chat_id,reason='stopped')
                else:
                    record=store.get(record['id'],owner=owner,root_chat_id=chat_id)
                    if record['status']=='pending' and datetime.fromisoformat(record['expires_at'])<=datetime.now(timezone.utc):
                        record=await store.answer(record['id'],owner=owner,root_chat_id=chat_id,generation=run['generation'],
                            action='timeout',deliver=deliver,cancelled=run['cancel'])
                if record['status'] not in {'pending','delivering'}:
                    self._publish_question(chat_id,record)
                    return {'request_id':record['id'],'status':record['status'],
                        'answers':{key:value['answers'] for key,value in waiter['response']['answers'].items()}
                        if record['status']=='submitted' else {}}
                with contextlib.suppress(TimeoutError):await asyncio.wait_for(waiter['event'].wait(),1)
                waiter['event'].clear()
        finally:
            self.clarification_waiters.pop(record['id'],None)
            final=store.interrupt(record['id'],owner=owner,root_chat_id=chat_id,
                reason='stopped' if run['cancel'].is_set() else 'turn_ended')
            if final!=record:self._publish_question(chat_id,final)
            if run.get('status')=='waiting_for_answer':run['status']='running'

    async def resolve_native_question(self, chat_id, request_id, *, owner, action, answers=None):
        record=self.children.get(chat_id,{})
        if record.get('owner_principal_id')!=owner or record.get('parent_id'):
            raise ValueError('Answer questions from the owning parent chat.')
        store=self.native_questions
        current=store.get(request_id,owner=owner,root_chat_id=chat_id)
        run=self.active.get(chat_id)
        if store.host_request(request_id,owner=owner,root_chat_id=chat_id):
            waiter=self.clarification_waiters.get(request_id)
            if not waiter or waiter['run'] is not run or not run or not run.get('generation'):
                return store.interrupt(request_id,owner=owner,root_chat_id=chat_id,reason='runtime_ended')
            async def deliver_host(native_id,response):
                await self._deliver_clarification(chat_id,request_id,waiter,run,response)
            result=await store.answer(request_id,owner=owner,root_chat_id=chat_id,generation=run['generation'],
                action=action,answers=answers,deliver=deliver_host,cancelled=run['cancel'])
            self._publish_question(chat_id,result);waiter['event'].set()
            return result
        if run is None or not run.get('generation') or not run.get('connection'):
            # The native endpoint is gone. Recovery retires only this chat's
            # pending generations and never invents a submitted answer.
            for question in store.for_chat(chat_id,owner=owner):
                if question['id']==request_id and question['status'] in {'pending','delivering'}:
                    # Store generation is private; recover() is startup-only.
                    return store.interrupt(request_id,owner=owner,root_chat_id=chat_id,reason='runtime_ended')
            return current
        generation=run['generation']
        async def deliver(native_id,payload):
            def active():
                return (self.active.get(chat_id) is run and not run['cancel'].is_set()
                    and run.get('generation')==generation
                    and store.get(request_id,owner=owner,root_chat_id=chat_id)['status']=='delivering')
            await run['connection'].write({'id':native_id,'result':payload},guard=active)
        result=await store.answer(request_id,owner=owner,root_chat_id=chat_id,generation=generation,
            action=action,answers=answers or {},deliver=deliver,cancelled=run['cancel'])
        run['tool_events'].put_nowait({'type':'user_input_resolved','request':result})
        return result

    async def control(self, chat_id, operation, message="", attachments=(), clip_attachments=(), skill_ids=(), *, sender=None):
        record = self.children.get(chat_id, {})
        if record.get('parent_id'):
            raise ValueError('Control managed subagents through their parent chat.')
        if operation != 'stop' and record.get('kind') == 'organization_chat':
            organization = getattr(self, 'organization_chats', None)
            if (not isinstance(sender, dict) or sender.get('channel') != 'web' or organization is None
                    or not organization.owns(record) or not organization.member(sender.get('principal_id'))):
                raise ValueError('Steer from the authenticated company member.')
            if attachments or clip_attachments:
                raise ValueError('Use company-owned evidence in this shared conversation.')
        run = self.active.get(chat_id)
        managed = getattr(self, 'managed_runtime', None)
        if run is not None and run.get('managed') and managed is not None:
            if operation == 'stop':
                self._cancel_credential_waits(chat_id, 'stopped')
            return await managed.control(self, chat_id, operation, message,
                attachments, clip_attachments, skill_ids, sender=sender)
        pending = self.child_tasks.get(chat_id)
        if operation == 'stop' and run is None and managed is not None and pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            from managed_sessions import ManagedSessions
            from workspace_identity import workspace_scope
            if ManagedSessions(self.state).pending_binding(workspace_scope(self.state), chat_id) is not None:
                raise RuntimeError('The managed session is awaiting Stop confirmation.')
            record['running'] = False
            self._save_child(record)
            return {'ok': True, 'result': {'stopped': True}}
        raise ValueError('Control the current managed conversation.')

    async def turn(self, message: str, chat_id: str, dataset: str | None = None, attachments=(), clip_attachments=(), skill_ids=(), *, model_context: str = "", sender=None):
        from scope_host import agent_scope
        try:
            async with agent_scope(self, chat_id, dataset, sender=sender, admit=True):
                async for event in self._recorded_turn(message, chat_id, dataset, attachments,
                        clip_attachments, skill_ids, model_context=model_context, sender=sender):
                    yield event
        except ValueError:
            yield {'type':'error','message':'Continue in a conversation permitted by your current business scope.'}

    async def _recorded_turn(self, message: str, chat_id: str, dataset: str | None = None, attachments=(), clip_attachments=(), skill_ids=(), *, model_context: str = "", sender=None):
        child = self.children.get(chat_id)
        try:
            resolved = self.resolve_skills(skill_ids, chat_id) if skill_ids else None
            if child and (child.get("kind") == "agent_run" or child.get("active_run_id")) and self.automation_store is not None:
                self.automation_store.pin_skills(child.get("active_run_id") or child["run_id"], resolved or [])
        except ValueError as exc:
            yield {"type":"error","message":str(exc)}
            return
        if child is None or chat_id in self.active:
            async for event in self._turn(message,chat_id,dataset,attachments,clip_attachments,resolved_skills=resolved,model_context=model_context):
                yield event
            return
        organization = getattr(self, 'organization_chats', None)
        if sender is None and child.get('kind') != 'organization_chat':
            sender = {'principal_id': child.get('owner_principal_id'), 'channel': 'web'}
        # A Tracking Agent's own conversation also takes a message from one of
        # its current participants, typed in the app or sent from their phone.
        phone = getattr(self, 'phone_chat', None)
        from agent_kind_policy import messaging_channels
        admitted_channels = {'web', 'webhook', 'timer', *messaging_channels(self)}
        if (not isinstance(sender, dict) or sender.get('channel') not in admitted_channels
                or not isinstance(sender.get('principal_id'), str)
                or child.get('kind') == 'organization_chat' and
                    (organization is None or not organization.owns(child) or not organization.member(sender['principal_id']))
                or child.get('kind') != 'organization_chat' and sender['principal_id'] != child.get('owner_principal_id')
                    and not (phone is not None and child.get('kind') == 'agent_conversation' and not child.get('parent_id')
                        and phone.participant_chat(chat_id, sender['principal_id']))):
            yield {'type':'error','message':'Continue through the authenticated conversation owner.'}
            return
        if not hasattr(self, '_turn_senders'): self._turn_senders = {}
        sender = copy.deepcopy(sender)
        if sender.get('admission_key') and any(item.get('channel', {}).get('admission_key') == sender['admission_key'] for item in child['messages']):
            yield {'type':'done','status':'already_admitted'}
            return
        self._turn_senders[chat_id] = sender
        if not hasattr(self, '_turn_guests'): self._turn_guests = {}
        if self._guest(child, sender): self._turn_guests[chat_id] = sender['principal_id']  # see participant_turn
        child.pop('waiting_for_reply', None)
        from title_stream import TitleStream
        title_stream = None
        if child.get("title") in (None, "", "New chat"):
            title_stream = TitleStream(child.get("title") or "New chat")
        child["messages"].append({"role":"user","content":message,"sender_principal_id":sender["principal_id"],"channel":sender,"created_at":datetime.now(timezone.utc).isoformat(),"skill_selection":resolved,"attachments":[{"id":i,"url":f"/attachments/{i}.png"} for i in attachments],
            "clip_attachments":self._clip_attachments(clip_attachments,dataset) if clip_attachments else []})
        answer = {"role":"assistant","content":"","results":[],"tools":[]}
        child["messages"].append(answer)
        child["running"] = True
        self._save_child(child)
        try:
            async for event in self._turn(message,chat_id,dataset,attachments,clip_attachments,resolved_skills=resolved,model_context=model_context):
                from scope_host import active_scope_current
                if not active_scope_current(self,chat_id):
                    run=self.active.get(chat_id)
                    if run is not None:run['cancel'].set()
                    raise ValueError('The business scope changed during this request.')
                for projected in title_stream.feed(event) if title_stream else [event]:
                    if projected.get("type") == "title" and not projected.get("partial"):
                        child["title"] = projected["title"]
                    self._record_event(answer, projected)
                    self._save_child(child)
                    yield projected
        finally:
            self._turn_senders.pop(chat_id, None)
            self._turn_guests.pop(chat_id, None)
            getattr(self, 'invocation_grants', {}).pop(chat_id, None)
            child["running"] = False
            self._complete_answer_timing(answer)
            self._save_child(child)

    async def _turn(self, message: str, chat_id: str, dataset: str | None = None, attachments=(), clip_attachments=(), resolved_skills=None, *, model_context: str = ""):
        managed = getattr(self, 'managed_runtime', None)
        if managed is None:
            yield {'type': 'error', 'message': 'Connect this workspace to the OpenAI-managed Agents API.'}
            return
        if participant_turn(self, chat_id):
            creator = getattr(self, 'messaging_member', None)
            try: named = (creator(self.children.get(chat_id, {}).get('owner_principal_id')) or {}).get('display_name') if callable(creator) else None
            except Exception: named = None
            yield {'type': 'delta', 'text': PARTICIPANT_SAVED.format(creator=named or 'This agent’s creator')}
            yield {'type': 'done', 'status': 'completed'}
            return
        phase = 'recovery'
        try:
            async for _ in managed.recover(self, chat_id):
                pass
            phase = 'run'
            async for event in managed.run(self, message, chat_id, dataset, attachments,
                    clip_attachments, resolved_skills, model_context=model_context):
                yield event
        except (RuntimeError, TimeoutError, OSError, ValueError) as error:
            from managed_diagnostics import record_failure
            record_failure(self.state, chat_id, error, phase=phase)
            yield {'type': 'error', 'message': 'This managed turn needs review before it can continue.'}

    def _public_clip(self, record):
        return self.vertical.public_clip(record)

    def _inspection_metadata(self, record, frames, include_observations=False):
        return self.vertical.inspection_metadata(record, frames, include_observations)

    def _guest(self, child, sender):
        """This sender as a participant other than the conversation's creator, else None."""
        principal = sender.get('principal_id') if isinstance(sender, dict) else None
        if not principal or child.get('kind') == 'organization_chat' or principal == child.get('owner_principal_id'):
            return None
        return principal

    def _authority_principal(self, chat_id):
        """Separate the live human authority from the conversation's data owner."""
        record = self.children.get(chat_id, {})
        owner = record.get('owner_principal_id')
        organization = getattr(self, 'organization_chats', None)
        if organization is None or owner != organization.owner: return owner
        if not organization.owns(record): return None
        root = record if organization.canonical(record) else self.children.get(record.get('managed_root_id') or record.get('native_root_id'), {})
        if not organization.canonical(root): return None
        sender = getattr(self, '_turn_senders', {}).get(root.get('id'), {})
        principal = sender.get('principal_id')
        return principal if organization.member(principal) else None

    async def _tool(self, name, args, scope=None, chat_id=None):
        from scope_host import agent_scope
        try:
            async with agent_scope(self, chat_id, scope):
                return await self._scoped_tool(name, args, scope, chat_id)
        except (ValueError, KeyError, OSError, RuntimeError, TimeoutError) as error:
            return [{'type': 'inputText', 'text': str(error)}], None, False

    async def _scoped_tool(self, name, args, scope=None, chat_id=None):
        from host import Admission
        from scope_host import current_admission
        from tool_families import ToolContext
        from workspace_identity import workspace_scope
        if participant_turn(self, chat_id) and name not in PARTICIPANT_TOOLS:
            raise ValueError(CREATOR_TOOLS)
        record = self.children.get(chat_id, {})
        actor = self._authority_principal(chat_id)
        admission = current_admission()
        if admission is None:
            # Explicit hostless callers still bind to their saved conversation.
            binding = (record.get('owner_principal_id'), record.get('dataset'))
            admission = Admission(lambda: bool(actor and binding == (
                self.children.get(chat_id, {}).get('owner_principal_id'),
                self.children.get(chat_id, {}).get('dataset'))))
        context = ToolContext(state=self.state, company_id=workspace_scope(self.state),
            actor=actor, data_owner=record.get('owner_principal_id'), collection=scope,
            chat_id=chat_id, record=record, run=self.active.get(chat_id),
            agents=self.automation_store, agent=self, admission=admission, extension=self.vertical)
        result = await workspace_registry(self).invoke(context, name, args)
        if isinstance(result, tuple) and len(result) == 3:
            return result
        return [{'type': 'inputText', 'text': json.dumps(result)}], None, True

    async def _common_tool(self, name, args, scope=None, chat_id=None):
        try:
            if participant_turn(self, chat_id) and name not in PARTICIPANT_TOOLS:
                raise ValueError(CREATOR_TOOLS)
            if name in {'read_file', 'list_files'}:
                from scope_host import history_allowed
                selected = getattr(self, 'business_scope_host', None)
                if selected is not None and not history_allowed(selected, self.children.get(chat_id, {}), self._authority_principal(chat_id)):
                    raise ValueError('Select the authorized conversation history to reopen its files.')
            results = None
            run = self.active.get(chat_id)
            organization = getattr(self, 'organization_chats', None)
            if (organization is not None and self.children.get(chat_id, {}).get('owner_principal_id') == organization.owner
                    and not self._authority_principal(chat_id)):
                raise ValueError('Continue through a current company member’s conversation.')
            if name in {'send_message','send_alert'}:
                from agent_kind_policy import messaging_channels, has_participant_reach_context, participant_recipient
                from messaging import MessagingActor
                from custom_coordinator import offload
                record = self.children.get(chat_id, {})
                service = getattr(self, 'messaging_store', None)
                sender = getattr(self, '_turn_senders', {}).get(chat_id)
                if (not service or not sender or not run or run.get('cancel').is_set()
                        or record.get('parent_id') or record.get('dataset') != scope
                        or set(args) != ({'text'} if name == 'send_message' else {'purpose','language','parameters'})):
                    raise ValueError('Send a message from the verified member’s active parent conversation.')
                if run.get('messaging_claim'):
                    raise ValueError('Review this turn’s saved message receipt.')
                from managed_turn import current_managed_call
                managed_call = current_managed_call()
                request_id = ('managed-' + hashlib.sha256(json.dumps(managed_call, sort_keys=True).encode()).hexdigest()
                              if managed_call else 'host-' + run['generation'])
                run['messaging_claim'] = request_id
                # A Tracker context reaches the turn's own participant and nobody
                # else. The host resolves that member; the arguments carry text only.
                try:
                    recipient = await offload(participant_recipient, self, chat_id, sender) if has_participant_reach_context(self, chat_id) else None
                except ValueError:
                    run.pop('messaging_claim', None)  # A refused recipient leaves this turn's one send unused.
                    raise
                channel = recipient['channel'] if recipient else sender.get('channel') if sender.get('channel') in messaging_channels(self) else None
                actor = MessagingActor(service.company_id, sender['principal_id'], scope, chat_id, sender['principal_id'])
                await offload(service.bind_conversation, actor, channel=channel)
                # Participation, the binding and company membership can change
                # while the conversation binds. Read them again ahead of delivery.
                if (recipient is not None or has_participant_reach_context(self, chat_id)) and await offload(participant_recipient, self, chat_id, sender) != recipient:
                    raise ValueError('Review this agent’s current participants, then send again.')
                data = await (service.send_message(actor, args['text'], request_id=request_id, channel=channel, cancelled=run['cancel'])
                    if name == 'send_message' else service.send_alert(actor, args, request_id=request_id, channel=channel, cancelled=run['cancel']))
                run['messaging_delivery'] = data
                record['waiting_for_reply'] = data['waiting_for_reply']
                self._save_child(record)
            elif name == 'request_clarification':
                data=await self._wait_for_clarification(chat_id,args,scope)
                return [{"type":"inputText","text":json.dumps(data)}],None,data['status'] in {'submitted','skipped','timed_out'}
            elif name == 'get_workspace_capabilities':
                from custom_coordinator import offload
                data = await offload(self.capability_snapshot,chat_id,scope)
                if data is None:
                    raise ValueError('Open this tool through the configured workspace host.')
            elif name in {"create_custom_connector", "list_custom_connectors", "get_custom_connector", "save_custom_connector", "request_custom_connector_credentials",
                          "propose_custom_connector_target", "test_custom_connector", "read_custom_connector"}:
                record=self.children.get(chat_id,{})
                principal=record.get("owner_principal_id")
                if not principal or not scope or (record.get("parent_id") and name not in {'read_custom_connector','list_custom_connectors','get_custom_connector'}) or record.get("dataset")!=scope:
                    raise ValueError("Set up custom connectors in their owning parent chat.")
                if self.custom_connector_store is None:
                    from custom_connectors import CustomConnectors
                    self.custom_connector_store=CustomConnectors(self.state)
                store=self.custom_connector_store
                if name == 'create_custom_connector':
                    if set(args) != {'name','description','kind'} or not run or run['cancel'].is_set():
                        raise ValueError('Prepare a connector in the active parent conversation.')
                    from custom_coordinator import offload
                    data = await offload(store.create, **args, owner=principal,collection=scope,chat_id=chat_id)
                elif name=="list_custom_connectors":
                    data={"connections":await asyncio.to_thread(store.list,owner=principal,collection=scope)}
                else:
                    saved=await asyncio.to_thread(store.get,args['connector_id'],owner=principal)
                    if saved['collection']!=scope:
                        raise ValueError("Use a custom connector from this business.")
                    if name=="get_custom_connector":
                        from custom_tool_contracts import ADAPTER_INSTRUCTIONS
                        data=await asyncio.to_thread(store.source,args['connector_id'],owner=principal)
                        data['execution_setup']=await asyncio.to_thread(self._custom_setup().describe,args['connector_id'],owner=principal,collection=scope)
                        data['adapter_instructions']=ADAPTER_INSTRUCTIONS
                        if self.vertical is not None:
                            data.update(await self.vertical.connector_details(saved, args['connector_id'], principal, scope))
                    elif name=="propose_custom_connector_target":
                        if set(args)!={'connector_id','target','reason','expected_revision'}:raise ValueError('Provide the current connector access proposal.')
                        data=await asyncio.to_thread(self._custom_setup().propose,args['connector_id'],args['target'],args['reason'],
                            expected_revision=args['expected_revision'],owner=principal,collection=scope)
                        data['review_url']=f"/?customize=1&tab=connections&item={args['connector_id']}"
                    elif name in {'test_custom_connector','read_custom_connector'}:
                        coordinator=getattr(self,'custom_coordinator',None)
                        if coordinator is None:raise ValueError('Custom execution is being commissioned. Continue preparing the connector and its access proposal.')
                        cancelled=run['cancel']if run is not None else asyncio.Event()
                        if name=='test_custom_connector':
                            if set(args)!={'connector_id','expected_revision'}:raise ValueError('Choose the current connector version to test.')
                            data=await coordinator.test(args['connector_id'],owner=principal,collection=scope,
                                expected_revision=args['expected_revision'],cancelled=cancelled)
                        else:
                            if set(args)!={'connector_id','tool','arguments'}:raise ValueError('Choose a declared connector read tool.')
                            data=await coordinator.invoke(args['connector_id'],args['tool'],args['arguments'],
                                owner=principal,collection=scope,cancelled=cancelled)
                    elif name=="request_custom_connector_credentials":
                        if set(args)!={'connector_id','reason'}:raise ValueError('Provide a connector and the reason for access.')
                        data=await self._wait_for_credentials(chat_id,args['connector_id'],args['reason'],scope)
                        return [{"type":"inputText","text":json.dumps(data)}],None,data['status']!='cancelled'
                    else:
                        data=await asyncio.to_thread(store.save_package,args['connector_id'],args['package'],
                            expected_revision=args['expected_revision'],owner=principal)
            elif name == 'write_connection':
                from managed_connections import write_connection
                data = await write_connection(self, chat_id, args)
            elif name in {"list_connections", "list_connection_tools", "read_connection"}:
                principal = self.children.get(chat_id, {}).get("owner_principal_id")
                if not principal:
                    raise ValueError("Open a saved chat in your workspace to use its connections.")
                connections = self._connections()
                if name == "list_connections":
                    data = await asyncio.to_thread(connections.catalogue, principal)
                elif name == "list_connection_tools":
                    data = await connections.tools(args["provider"], principal_id=principal)
                else:
                    data = await connections.call(args["provider"], args["tool"], args["arguments"], principal_id=principal)
                    if isinstance(data.get("result"), dict) and data["result"].get("isError"):
                        return [{"type": "inputText", "text": json.dumps(data)}], None, False
            elif name in {'publish_profile_cover','generate_profile_cover'}:
                host=getattr(self,'profile_covers',None)
                record=self.children.get(chat_id,{})
                owner=record.get('owner_principal_id')
                def cover_current():
                    live=self.children.get(chat_id,{})
                    from scope_host import current_admission
                    admission = current_admission()
                    check = admission.current if admission is not None else None
                    return bool(host and run and self.active.get(chat_id) is run and not run['cancel'].is_set()
                        and owner and live.get('owner_principal_id')==owner and live.get('dataset')==scope
                        and not live.get('parent_id') and (check is None or check()))
                if not cover_current():
                    raise ValueError('Update profile artwork from its active parent conversation.')
                data=await host.tool(name,args,owner=owner,actor_id=self._authority_principal(chat_id),
                    chat_id=chat_id,collection=scope,turn_id=run['generation'],current=cover_current,
                    emit=run.setdefault('tool_events',asyncio.Queue()).put_nowait)
            elif name == "publish_business_map":
                from custom_coordinator import offload
                import sqlite3
                record = self.children.get(chat_id, {})
                owner = record.get('owner_principal_id')
                task = asyncio.current_task()
                def current():
                    saved = self.children.get(chat_id, {})
                    session = run.get('isolated_session') if run else None
                    authorized = getattr(self, 'business_map_authorized', None)
                    return bool(run and self.active.get(chat_id) is run and not task.cancelling()
                        and not run.get('cancel', asyncio.Event()).is_set() and scope
                        and saved.get('owner_principal_id') == owner and owner
                        and saved.get('dataset') == scope and not saved.get('parent_id')
                        and not getattr(session, '_closing', False) and not getattr(session, 'stopped', False)
                        and (authorized is None or authorized(self._authority_principal(chat_id))))
                if not current() or not isinstance(args, dict) or set(args) != {'file_id', 'expected_profile_revision'}:
                    raise ValueError('Publish a map in its active owning parent chat and business collection.')
                store = await offload(self._business_maps)
                created = []
                try:
                    data = await offload(store.publish, self._files(), self._business(),
                        file_id=args['file_id'], expected_profile_revision=args['expected_profile_revision'],
                        owner=owner, chat_id=chat_id, collection=scope, current=current, created=created)
                    if not current():
                        raise ValueError('The map publication request has finished.')
                    run.setdefault('tool_events', asyncio.Queue()).put_nowait({'type': 'business_map', 'map': data})
                except BaseException as exc:
                    if created:
                        await offload(store.discard, created, owner=owner, chat_id=chat_id, collection=scope)
                    if isinstance(exc, sqlite3.Error):
                        raise ValueError('The map store is busy. Try this publication again.') from None
                    raise
            elif name in {"create_file", "list_files", "read_file"}:
                record = self.children.get(chat_id, {})
                if (record.get("parent_id") or not record.get("owner_principal_id") or not scope
                        or record.get("dataset", scope) != scope):
                    raise ValueError("Create and read files in their owning parent chat and business collection.")
                authority = {"owner_principal_id": record["owner_principal_id"], "chat_id": chat_id, "dataset": scope}
                if name == "create_file":
                    data = await asyncio.to_thread(self._files().create, args["document"], **authority)
                elif name == "list_files":
                    data = {"files": await asyncio.to_thread(self._files().list, **authority)}
                else:
                    offset, limit = args.get("offset", 0), args.get("limit", 50000)
                    if type(offset) is not int or not 0 <= offset <= 1048576 or type(limit) is not int or not 1 <= limit <= 50000:
                        raise ValueError("Read a file page of up to 50,000 characters at a valid offset.")
                    data = await asyncio.to_thread(self._files().content, args["file_id"], **authority)
                    text_content = data.get("content")
                    if isinstance(text_content, str):
                        data.update(content=text_content[offset:offset+limit], total_characters=len(text_content),
                                    offset=offset, next_offset=offset+limit if offset+limit<len(text_content) else None)
            elif name == "set_chat_title":
                title = args.get("title")
                child = self.children.get(chat_id)
                if child is None or not isinstance(title,str) or not title.strip() or len(title)>80 or any(ord(c)<32 for c in title):
                    raise ValueError("Choose a short title for the current saved chat.")
                child["title"] = title.strip()
                self._save_child(child)
                data = {"title":child["title"]}
            elif name in {"list_skills","create_skill","update_skill"}:
                if name == "list_skills":
                    data = self._skills().catalogue()
                else:
                    if self._is_child(chat_id):
                        raise ValueError("Return skill proposals to the parent chat.")
                    if name == "create_skill":
                        data = self._skills().create(args["definition"], origin="agent")
                    else:
                        data = self._skills().update(args["skill_id"], args["expected_revision"], args["definition"])
                    data = {**data,"next_action":"Review this skill in Marketplace, then add its chosen revision to your skills."}
            elif name in {"get_business_profile","update_business_profile"}:
                if not scope:
                    raise ValueError("Choose a business collection for onboarding.")
                if name == "get_business_profile":
                    data = await asyncio.to_thread(self._business().get,scope)
                    from business_maps import format_guide
                    data = {**data, 'map_format': format_guide(data)}
                else:
                    if chat_id and self._is_child(chat_id):
                        raise ValueError("Return researched facts to the parent chat for a profile update.")
                    data = await asyncio.to_thread(self._business().update,scope,args["expected_revision"],args["profile"],origin=args["origin"])
                if getattr(self,'profile_covers',None):
                    data=await asyncio.to_thread(self.profile_covers.business,self._authority_principal(chat_id),scope,data)
            elif name == 'invoke_agent':
                from agent_invocation import invoke
                data = invoke(self, chat_id, args)
                from background_agents import ensure_agent_conversation
                ensure_agent_conversation(self.automation_store, self, data)
                run.setdefault('tool_events', asyncio.Queue()).put_nowait({'type':'agent', 'agent':{
                    key:data[key] for key in ('id','title','chat_id','mode','status','conversation_url')}})
            elif name in {"get_agent","update_agent","propose_agent"}:
                if self.automation_store is None or not scope:
                    raise ValueError("Choose a business collection before proposing a saved agent.")
                origin = self.children.get(chat_id, {})
                actor = self._authority_principal(chat_id)
                if (not actor or not origin.get('owner_principal_id') or origin.get('parent_id')
                        or origin.get('dataset') != scope):
                    raise ValueError("Use the owning parent conversation in this business.")
                if name != 'get_agent' and run is not None and run.get('cancel', asyncio.Event()).is_set():
                    raise ValueError("Continue agent setup from an active conversation.")
                if args.get("skill_ids"):
                    self.resolve_skills(args["skill_ids"], owner_principal_id=actor)
                input_context = self.automation_store.inputs.definition_context(self, chat_id, args)
                datasets = self.vertical.collection_names()
                if name == "propose_agent":
                    setup = origin.get('agent_setup')
                    attach_here = bool(origin['owner_principal_id'] == actor and setup
                        and not setup.get('agent_id') and not origin.get('agent_id'))
                    defaults={k:setup[k] for k in ("agent_kind","input_kind") if setup and k in setup}
                    data = self.automation_store.create({**defaults, **args, "dataset":scope}, datasets,
                        owner_principal_id=actor, input_context=input_context,
                        conversation_chat_id=chat_id if attach_here else None)
                else:
                    current = self.automation_store.get(args["agent_id"])
                    if (current["dataset"] != scope or not current.get("owner_principal_id")
                            or current["owner_principal_id"] != actor):
                        raise ValueError("Choose an agent from your workspace and this business.")
                    data = current
                    if name == "update_agent":
                        definition = {k:v for k,v in args.items() if k not in {"agent_id","expected_revision"}}
                        data = self.automation_store.update(args["agent_id"],{**definition,"dataset":scope},
                            datasets,expected_revision=args["expected_revision"],input_context=input_context)
                from workspace_capabilities import WorkspaceCapabilities
                execution = (getattr(self, 'workspace_capabilities', None) or WorkspaceCapabilities()).execution(data)
                data = {**data, 'execution':execution,
                    'activation':execution['notice'] or 'The operator reviews and activates this draft in Agents.'}
                from background_agents import ensure_agent_conversation
                ensure_agent_conversation(self.automation_store, self, data,
                    briefing=name == 'propose_agent' and data['chat_id'] != chat_id)
                if run is not None:
                    run.setdefault('tool_events', asyncio.Queue()).put_nowait({'type':'agent', 'agent':{
                        key:data[key] for key in ('id','title','chat_id','mode','status','conversation_url')}})
            else:
                raise ValueError("Choose one of the registered workspace tools.")
            return [{"type": "inputText", "text": json.dumps(data)}], results, True
        except (ValueError, KeyError, OSError, RuntimeError, TimeoutError) as exc:
            return [{"type": "inputText", "text": str(exc)}], None, False


# Saved migration/proof callers retain their historical import during the managed cutover.
CodexVideoAgent = HarnessAgent
