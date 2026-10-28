"""Host-selected native authentication and company-bound metered API leases.

No authentication cache is read, copied or mutated. API provider keys remain in
the host gateway; child native processes receive only their local run capability.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
from urllib.parse import quote,urlsplit
import asyncio

import httpx

from company_usage import CompanyUsage


MODES=frozenset({"founder_local_chatgpt","openai_api","unconfigured"})
AUTH_DOCS="https://learn.chatgpt.com/docs/app-server#auth-endpoints"
# asyncio copies this immutable binding into child tasks. Closing the parent's
# HTTP lease resets only its own task context; existing children retain the same
# durable admission ID and open their own short-lived transport capabilities.
_API_INVESTIGATION: ContextVar[tuple[str,str] | None] = ContextVar("rocoto_api_investigation",default=None)
_MESSAGES={
    "authentication_unconfigured":"Select and commission the agent authentication route.",
    "hosted_requires_openai_api":"Hosted investigations require the commissioned Rocoto OpenAI API route.",
    "managed_api_required":"Commission the Rocoto OpenAI-managed Agents API for this workspace.",
    "rocoto_api_key_required":"Commission the Rocoto OpenAI API credential in protected runtime configuration.",
    "prepaid_company_funding_required":"API investigations require prepaid company funds and monthly headroom.",
    "api_metered_ready":"API requests use this company's funded, metered model route.",
    "api_reconciliation_required":"Reconcile the saved provider usage before another model request.",
}


def _hosted(origin,host):
    # A missing origin on the selected public staging consumer cannot reopen its
    # independently authenticated founder cache as the customer inference route.
    if host=="rocoto-staging":return True
    if origin in (None,""):return False
    if not isinstance(origin,str):raise ValueError("Use the configured application origin.")
    try:
        parsed=urlsplit(origin)
        if (parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"","/"}):
            raise ValueError()
        _=parsed.port
        return parsed.hostname.lower() not in {"localhost","127.0.0.1","::1"}
    except ValueError:
        raise ValueError("Use the configured application HTTP or HTTPS origin.") from None


@dataclass(frozen=True)
class NativeLaunch:
    mode: str
    child_env: dict=field(repr=False)
    cli_config: dict
    expected_account_type: str
    login_status_required: bool
    isolated: bool=False
    gateway: object=field(default=None,repr=False,compare=False)

    @property
    def cli_args(self):
        return [part for key,value in self.cli_config.items() for part in ("-c",key+"="+json.dumps(value))]


class AgentAuth:
    """Host-selected auth; API keys stay in the per-run Responses gateway."""
    def __init__(self,state,*,public_origin=None,mode=None,environ=None,runtime_host=None):
        self.state=Path(state).resolve()
        self.environment=dict(os.environ if environ is None else environ)
        self.mode=mode if mode is not None else self.environment.get("ROCOTO_AGENT_AUTH_MODE","unconfigured")
        if self.mode not in MODES:raise ValueError("Select a supported agent authentication mode.")
        origin=public_origin if public_origin is not None else self.environment.get("ROCOTO_PUBLIC_ORIGIN", self.environment.get("ROCOTO_VIDEO_PUBLIC_ORIGIN"))
        self.hosted=_hosted(origin,runtime_host if runtime_host is not None else socket.gethostname())

    def _environment(self):
        return {key:value for key,value in self.environment.items()
            if not key.startswith(("OPENAI_","AZURE_OPENAI_","ROCOTO_OPENAI_"))
            and key not in {"CODEX_ACCESS_TOKEN","CODEX_API_KEY","CODEX_AUTH_JSON","ROCOTO_AGENT_GATEWAY_TOKEN"}}

    def status(self):
        status={"mode":self.mode,"hosted":self.hosted,"inference_enabled":False,
            "authentication_prepared":False,"billing_status":"unconfigured","auth_docs":AUTH_DOCS}
        reason="authentication_unconfigured"
        if self.mode=="founder_local_chatgpt":
            reason="hosted_requires_openai_api" if self.hosted else "managed_api_required"
        elif self.mode=="openai_api":
            key=self.environment.get("ROCOTO_OPENAI_API_KEY")
            valid=isinstance(key,str) and bool(key) and len(key)<16384 and not any(c.isspace() for c in key)
            status["api_key_configured"]=valid
            reason="rocoto_api_key_required"
            if valid:
                pool=CompanyUsage.agent_pool(self.state)
                usage=pool.summary()
                reason="prepaid_company_funding_required"
                native=(pool.state.name=='native-agent-api' and usage.get('operator_purpose')=='native_agent_chat'
                    and usage.get('operator_model')=='gpt-6-astra' and Decimal(usage['operator_demo_budget_usd'])>0)
                prepaid=(pool.state.name=='openai-api' and Decimal(usage["prepaid_funded_usd"])>0
                    and Decimal(usage["provider_credit_funded_usd"])==0)
                if ((native or prepaid) and min(Decimal(usage["available_usd"]),Decimal(usage["monthly_available_usd"]))>0):
                    reason="api_metered_ready"
                    breaker=pool.state/"gateway"/"admission-review-required.json"
                    if breaker.exists():reason="api_reconciliation_required"
                    status.update(authentication_prepared=True,inference_enabled=not breaker.exists(),
                        billing_status="operator_native_request_metering" if native else "openai_prepaid_request_metering",
                        budget_activation_verified=True)
        status.update(reason=reason,message=_MESSAGES[reason])
        return status

    def launch(self):
        status=self.status()
        if not status["inference_enabled"]:raise ValueError(status["message"])
        if self.mode!="founder_local_chatgpt":raise ValueError("Open the metered API lease before launching the native agent.")
        return NativeLaunch(mode=self.mode,child_env=self._environment(),
            cli_config={"forced_login_method":"chatgpt","model_provider":"openai"},
            expected_account_type="chatgpt",login_status_required=True)

    async def verify_api_credentials(self,*,timeout=3,transport=None):
        """Read current model access; this does not execute or prove inference."""
        from openai_gateway import MODEL
        key=self.environment.get("ROCOTO_OPENAI_API_KEY")
        if (self.mode!="openai_api" or not isinstance(key,str) or not key
                or len(key)>=16384 or any(c.isspace() for c in key)):
            return {"authenticated":False,"check_status":"rocoto_api_key_required",
                "message":_MESSAGES["rocoto_api_key_required"]}
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(timeout=timeout,transport=transport,
                        trust_env=False,follow_redirects=False) as client:
                    response=await client.get("https://api.openai.com/v1/models/"+quote(MODEL,safe=""),
                        headers={"Authorization":"Bearer "+key})
            if response.status_code==200:
                if response.json().get("id")!=MODEL:
                    raise ValueError("Unexpected model-access response")
                return {"authenticated":True,"check_status":"authenticated",
                    "message":"OpenAI verified access to the configured model."}
            if response.status_code in {401,403}:
                return {"authenticated":False,"check_status":"unauthorized",
                    "message":"Reconnect the Rocoto OpenAI API credential to restore model access."}
            if response.status_code==404:
                return {"authenticated":False,"check_status":"model_unavailable",
                    "message":"The configured model requires access from this OpenAI project."}
            return {"authenticated":False,"check_status":"unavailable",
                "message":"OpenAI access needs a fresh check. The next status check will retry."}
        except (TimeoutError,httpx.TimeoutException):
            return {"authenticated":False,"check_status":"timeout",
                "message":"The OpenAI connection check timed out. The next status check will retry."}
        except (httpx.HTTPError,ValueError,AttributeError):
            return {"authenticated":False,"check_status":"error",
                "message":"OpenAI access needs a fresh check. The next status check will retry."}

    @asynccontextmanager
    async def lease(self,*,run_id,gateway_factory=None,unix_socket=None,activity_owner=None):
        """Historical adapter fixture lease; managed execution uses ManagedPolicy.turn."""
        status=self.status()
        if not status["inference_enabled"]:raise ValueError(status["message"])
        if unix_socket is not None and self.mode!="openai_api":
            raise ValueError("Isolated native workers use the commissioned API gateway.")
        if self.mode=="founder_local_chatgpt":
            yield self.launch()
            return
        if gateway_factory is None:
            from openai_gateway import ResponsesGateway
            gateway_factory=ResponsesGateway
        company_scope=CompanyUsage.agent_pool(self.state).summary()["company_scope"]
        inherited=_API_INVESTIGATION.get()
        if inherited is not None and inherited[0]!=company_scope:
            raise ValueError("A child investigation must retain its parent company scope.")
        binding=inherited or (company_scope,run_id)
        if activity_owner is not None:
            from profile_activity import bind_model_run
            from custom_coordinator import offload
            await offload(bind_model_run,self.state,binding[1],activity_owner)
        token=_API_INVESTIGATION.set(binding)
        try:
            options={} if unix_socket is None else {"unix_socket":unix_socket,"allow_native_tool_search":True}
            async with gateway_factory(self.state,api_key=self.environment["ROCOTO_OPENAI_API_KEY"],run_id=binding[1],**options) as gateway:
                environment=self._environment() if unix_socket is None else {}
                environment["ROCOTO_AGENT_GATEWAY_TOKEN"]=gateway.token
                yield NativeLaunch(mode="openai_api",child_env=environment,cli_config=gateway.cli_config,
                    expected_account_type="gateway",login_status_required=False,
                    isolated=unix_socket is not None,gateway=gateway)
        finally:
            _API_INVESTIGATION.reset(token)
