"""Company-bound, loopback Responses transport with prepaid request admission.

Only the gateway sees the provider key. The token reservation is an explicit
calculated upper estimate (UTF-8 bytes, structural margin, documented image
patches, and one bounded web-search re-input); it is not an OpenAI dollar cap.
Unexpected usage retains its hold and trips a persistent admission circuit breaker.
Opaque history uses the official count endpoint before generation. Counting cost
is recorded as unspecified by the published rate card, never asserted free.
No websocket, provider-side conversation or automatic retry.
"""
from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image
import uvicorn

from company_usage import CompanyUsage

MODEL="gpt-6-astra"
PRICING={"revision":"openai-standard-2026-10-04-gpt-6-astra-v1",
    "source_url":"https://developers.openai.com/api/docs/pricing",
    "input_usd_per_million":"10","cached_input_usd_per_million":"1",
    "cache_write_input_usd_per_million":"12.5","output_usd_per_million":"50",
    "tool_usd_per_call":{"web_search":"0.01"},
    "long_context":{"threshold_tokens":272000,"input_usd_per_million":"20",
        "cached_input_usd_per_million":"2","cache_write_input_usd_per_million":"25",
        "output_usd_per_million":"75"}}
MAX_BODY=24*1024*1024
MAX_RESPONSE=32*1024*1024
LOCAL_COUNT_THRESHOLD=240000
MODEL_CONTEXT=1050000
MAX_OUTPUT=8192
SEARCH_CONTEXT=128000
UPSTREAM="https://api.openai.com/v1/responses"
KEY_ENV="ROCOTO_AGENT_GATEWAY_TOKEN"
ISOLATED_RELAY_URL="http://127.0.0.1:8765/v1"
ALLOWED_FIELDS={"model","input","instructions","tools","tool_choice","parallel_tool_calls",
    "reasoning","text","stream","store","include","prompt_cache_key","prompt_cache_options",
    "service_tier","max_output_tokens","max_tool_calls","metadata","truncation","background",
    "previous_response_id","conversation","temperature","top_p","client_metadata"}
ALLOWED_ITEM_TYPES={"message","input_text","output_text","input_image","function_call",
    "function_call_output","custom_tool_call","custom_tool_call_output","reasoning",
    "summary_text","refusal","web_search_call"}


def _private_json(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    raw=json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    # Immutable receipts: each generated UUID has one admission/terminal record.
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"wb") as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())


def _integer(value,minimum,maximum):
    if type(value) is not int or not minimum<=value<=maximum:
        raise ValueError("Use the supported bounded request settings.")
    return value


def _input_bound(value):
    """Conservative local estimate; keep source images intact, charge patches.

    Byte fallback is deliberately much larger than characters/4. Structure gets
    another128 tokens per object/list plus1024 request framing tokens. This remains
    a versioned calculated bound, rather than pretending to be provider token count.
    """
    objects=0;image_tokens=0;images=0
    def visit(item):
        nonlocal objects,image_tokens,images
        if isinstance(item,dict):
            objects+=1
            if item.get("type")=="input_image":
                if set(item)-{"type","image_url","detail","prompt_cache_breakpoint"}:
                    raise ValueError("Supply bounded inline image evidence.")
                url=item.get("image_url","")
                if not isinstance(url,str) or not re.fullmatch(r"data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+",url):
                    raise ValueError("Supply inline JPEG, PNG or WEBP evidence.")
                try:
                    raw=base64.b64decode(url.split(",",1)[1],validate=True)
                    with Image.open(io.BytesIO(raw)) as image:
                        width,height=image.size
                        if image.format not in {"JPEG","PNG","WEBP"} or getattr(image,"n_frames",1)!=1:
                            raise ValueError()
                        image.verify()
                except Exception:
                    raise ValueError("Supply a valid still image.") from None
                if not 1<=width<=65535 or not 1<=height<=65535:
                    raise ValueError("Image dimensions exceed this transport's verified bounds.")
                detail=item.get("detail","auto")
                if detail not in {"auto","original","high","low"}:
                    raise ValueError("Select a supported image detail level.")
                patches=math.ceil(width/32)*math.ceil(height/32)
                if detail in {"auto","original"} and patches>30000:
                    raise ValueError("Image patches exceed the model's supported limit.")
                if detail=="high":patches=min(patches,2500)
                if detail=="low":patches=min(patches,256)
                image_tokens+=math.ceil(patches*1.2)+2
                images+=1
                return {**item,"image_url":"[inline image bytes accounted as visual patches]"}
            # Opaque provider references cannot be counted or isolated locally.
            if isinstance(item.get("type"), str) and item.get("type") in {"item_reference","input_file","input_audio","input_video"}:
                raise ValueError("This transport accepts visible text, tool history and inline image evidence.")
            if "file_id" in item or "file_url" in item:
                raise ValueError("Provider file references require separate admission.")
            return {k:visit(v) for k,v in item.items()}
        if isinstance(item,list):objects+=1;return [visit(v) for v in item]
        if item is None or isinstance(item,(str,bool,int,float)):return item
        raise ValueError("Supply a JSON model request.")
    cleaned=visit(value)
    text_bytes=len(json.dumps(cleaned,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode())
    return text_bytes+128*objects+1024+image_tokens,{"basis":"utf8-bytes-framing-and-image-patches-v1",
        "text_bytes":text_bytes,"structure_count":objects,"image_count":images,"image_token_bound":image_tokens}


def _has_opaque(value):
    if isinstance(value,dict):
        return bool(value.get("encrypted_content")) or value.get("type")=="compaction" or any(_has_opaque(v) for v in value.values())
    return isinstance(value,list) and any(_has_opaque(v) for v in value)


def _price_admission(bound,output,web,evidence):
    total_input=bound+(bound+SEARCH_CONTEXT+output if web else 0)
    maximum_usd=(Decimal(total_input)*25+Decimal(output)*75)/1_000_000+Decimal(web)/100
    evidence.update(input_token_bound=total_input,initial_input_token_bound=bound,
        output_token_bound=output,builtin_call_bound=web,
        maximum_usd=str(maximum_usd.quantize(Decimal("0.000001"),rounding=ROUND_CEILING)),
        assurance="calculated_conservative_bound")
    return evidence


def _client_tool_search(item, *, output=False):
    allowed={"type","id","call_id","status","execution","tools" if output else "arguments"}
    if not isinstance(item,dict) or set(item)-allowed or item.get("execution")!="client":
        raise ValueError("Use native client-executed tool discovery.")
    if output:
        if not isinstance(item.get("tools"),list):raise ValueError("Use bounded discovered tools.")
    else:
        args=item.get("arguments")
        if (not isinstance(args,dict) or set(args)-{"query","limit"}
                or not isinstance(args.get("query"),str) or len(args["query"])>8192
                or ("limit" in args and (type(args["limit"]) not in (int,float) or not 1<=args["limit"]<=32))):
            raise ValueError("Use bounded native tool discovery arguments.")


def prepare_request(body,*,max_output_tokens=MAX_OUTPUT,allow_web_search=True,allow_native_tool_search=False):
    if not isinstance(body,dict) or set(body)-ALLOWED_FIELDS or body.get("model")!=MODEL:
        raise ValueError("Use the commissioned model and Responses request fields.")
    value=copy.deepcopy(body)
    if value.get("previous_response_id") is not None or value.get("conversation") is not None:
        raise ValueError("Send this company's explicit conversation history.")
    if value.get("background") not in (None,False) or value.get("store") not in (None,False):
        raise ValueError("Use the bounded foreground, stateless model route.")
    if value.get("service_tier") not in (None,"auto","default"):
        raise ValueError("Use the commissioned standard pricing tier.")
    if value.get("truncation") not in (None,"disabled"):
        raise ValueError("Keep explicit conversation evidence within the context bound.")
    if type(value.get("stream",True)) is not bool:raise ValueError("Use a supported stream setting.")
    value.update(store=False,background=False,service_tier="default",truncation="disabled",stream=value.get("stream",True))
    for field in ("previous_response_id","conversation"):value.pop(field,None)
    maximum=_integer(max_output_tokens,16,MAX_OUTPUT)
    output=value.get("max_output_tokens")
    value["max_output_tokens"]=min(_integer(output,1,128000),maximum) if output is not None else maximum
    tools=value.get("tools",[])
    extra_tools=[]
    for item in value.get("input",[]) if isinstance(value.get("input"),list) else []:
        if isinstance(item,dict) and item.get("type") in {"tool_search_call","tool_search_output"}:
            if not allow_native_tool_search:raise ValueError("Use the isolated native discovery route.")
            _client_tool_search(item,output=item["type"]=="tool_search_output")
            if item["type"]=="tool_search_output":extra_tools.extend(item["tools"])
        if isinstance(item,dict) and item.get("type")=="additional_tools":
            extra=item.get("tools")
            if not isinstance(extra,list):raise ValueError("Use supported additional tool definitions.")
            extra_tools.extend(extra)
    def flatten(items,depth=0):
        if depth>3:raise ValueError("Use bounded native tool namespaces.")
        output=[]
        for item in items:
            if isinstance(item,dict) and item.get("type")=="namespace":
                nested=item.get("tools")
                if not isinstance(nested,list):raise ValueError("Use supported tool namespaces.")
                output.extend(flatten(nested,depth+1))
            else:output.append(item)
        return output
    if not isinstance(tools,list) or len(tools)>96:raise ValueError("Use the commissioned tool definitions.")
    all_tools=flatten(tools+extra_tools)
    if len(all_tools)>96:raise ValueError("Use bounded native tool definitions.")
    web=0;discovery=0
    for tool in all_tools:
        if not isinstance(tool,dict):raise ValueError("Use the commissioned tool definitions.")
        kind=tool.get("type")
        if kind in {"function","custom"}:continue
        if kind=="tool_search":
            if (not allow_native_tool_search or discovery or tool.get("execution")!="client"
                    or set(tool)-{"type","execution","description","parameters"}
                    or not isinstance(tool.get("parameters"),dict)
                    or tool["parameters"].get("type")!="object"
                    or ("description" in tool and not isinstance(tool["description"],str))):
                raise ValueError("Use the single native client discovery definition.")
            discovery+=1
            continue
        if kind not in {"web_search","web_search_preview"} or not allow_web_search:
            raise ValueError("This provider tool has no commissioned budget route.")
        if web:raise ValueError("Use a single web search definition.")
        web+=1
        if set(tool)-{"type","search_context_size","external_web_access","filters","user_location","return_token_budget"}:
            raise ValueError("Use the bounded web search settings.")
        tool.update(type="web_search",return_token_budget="default")
        if tool.get("search_context_size") not in (None,"low","medium","high"):
            raise ValueError("Use a supported search context size.")
    value["max_tool_calls"]=1
    reasoning=value.get("reasoning") or {}
    if not isinstance(reasoning,dict) or set(reasoning)-{"effort","summary","generate_summary","context"}:
        raise ValueError("Use the supported model reasoning settings.")
    if reasoning.get("effort") not in (None,"low","medium","high","xhigh","max"):
        raise ValueError("Use a supported Astra API reasoning effort.")
    if reasoning.get("context") not in (None,"all_turns","auto"):
        raise ValueError("Use supported reasoning history settings.")
    cache=value.get("prompt_cache_options") or {}
    if not isinstance(cache,dict) or set(cache)-{"mode","ttl"} or cache.get("ttl") not in (None,"30m"):
        raise ValueError("Use standard prompt caching without prewarm or comparison requests.")
    bound,evidence=_input_bound(value)
    opaque=_has_opaque(value)
    # One initial forward, and at most one built-in-search continuation. Count
    # generated tokens again as possible re-input, and reserve the full128k search
    # content window. Use the highest standard token rate even for short context.
    # A large byte estimate is a reason to count, not a reduced model context.
    evidence["requires_provider_count"] = opaque or bound>LOCAL_COUNT_THRESHOLD
    if discovery:evidence["client_tool_search"]=True
    return value,_price_admission(bound,value["max_output_tokens"],web,evidence)


def prepare_compaction(body,*,allow_native_tool_search=False):
    fields={"model","input","instructions","prompt_cache_key","prompt_cache_options","service_tier"}
    if not isinstance(body,dict) or set(body)-fields or body.get("model")!=MODEL:
        raise ValueError("Use the explicit company context for native compaction.")
    # Reuse all input/tool/cache validation. Compaction itself accepts no output
    # token cap; reserve the model's full published128k output maximum.
    validated,evidence=prepare_request({**body,"stream":False},allow_web_search=False,
        allow_native_tool_search=allow_native_tool_search)
    result={key:value for key,value in validated.items() if key in fields}
    result["service_tier"]="default"
    evidence.update(requires_provider_count=True,endpoint="responses/compact")
    return result,_price_admission(evidence["initial_input_token_bound"],128000,0,evidence)



def usage_receipt(response,*,request_id,reservation_id,pool_scope,parent_scope,admission):
    compact=admission.get("endpoint")=="responses/compact"
    if compact:
        if not isinstance(response,dict) or response.get("object")!="response.compaction":
            raise ValueError("Retain compaction admission until its usage is returned.")
        # The documented compaction response omits model/status; the exact model
        # is host-bound before POST and HTTP200 supplies the terminal state.
        response={**response,"model":MODEL,"status":"completed"}
    if (not isinstance(response,dict) or response.get("status") not in {"completed","incomplete"}
            or response.get("model")!=MODEL or response.get("service_tier") not in (None,"default")):
        raise ValueError("Retain the reservation until complete priced provider usage is available.")
    usage=response.get("usage") or {};details=usage.get("input_tokens_details") or {}
    input_tokens=_integer(usage.get("input_tokens"),0,100_000_000)
    cached=_integer(details.get("cached_tokens"),0,input_tokens)
    writes=_integer(details.get("cache_write_tokens"),0,input_tokens-cached)
    output=_integer(usage.get("output_tokens"),0,100_000_000)
    items=response.get("output")
    if not isinstance(items,list):raise ValueError("Retain the reservation until tool usage is available.")
    web=sum(item.get("type")=="web_search_call" for item in items if isinstance(item,dict))
    allowed={"message","compaction"} if compact else {"message","reasoning","function_call","custom_tool_call","web_search_call"}
    if not compact and admission.get("client_tool_search") is True:
        allowed.add("tool_search_call")
        for item in items:
            if isinstance(item,dict) and item.get("type")=="tool_search_call":_client_tool_search(item)
    if any(not isinstance(item,dict) or item.get("type") not in allowed for item in items):
        raise ValueError("Retain the reservation for unrecognized output usage.")
    if input_tokens>admission["input_token_bound"] or output>admission["output_token_bound"] or web>admission["builtin_call_bound"]:
        raise ValueError("Provider usage exceeded its calculated admission bound.")
    rates=PRICING["long_context"] if input_tokens>272000 else PRICING
    cost=((input_tokens-cached-writes)*Decimal(rates["input_usd_per_million"])
        +cached*Decimal(rates["cached_input_usd_per_million"])
        +writes*Decimal(rates["cache_write_input_usd_per_million"])
        +output*Decimal(rates["output_usd_per_million"]))/1_000_000+Decimal(web)/100
    return {"schema_version":1,"provider":"openai","kind":"responses_usage",
        "parent_company_scope":parent_scope,"pool_scope":pool_scope,"reservation_id":reservation_id,
        "request_id":request_id,"response_id":response.get("id"),"model":MODEL,"status":response["status"],
        "usage":{"input_tokens":input_tokens,"cached_input_tokens":cached,
            "cache_write_input_tokens":writes,"output_tokens":output},"pricing":PRICING,
        "additional_usage":([{"kind":"web_search","quantity":web,"unit_usd":"0.01"}] if web else []),
        "provider_actual_usd":str(cost),"received_at":datetime.now(timezone.utc).isoformat()}


class _LoopbackServer(uvicorn.Server):
    # The owning app controls process signals. Per-run listeners must never
    # replace its SIGTERM/SIGINT handlers while another chat is active.
    @contextmanager
    def capture_signals(self):
        yield


class ResponsesGateway:
    """One company/run capability, finite HTTP lifecycle, durable money evidence."""
    def __init__(self,state,*,api_key,run_id,pool=None,transport=None,max_output_tokens=MAX_OUTPUT,allow_web_search=True,maximum_run_usd="20",maximum_requests=32,unix_socket=None,allow_native_tool_search=False):
        self.state=Path(state).resolve();self._api_key=api_key
        if not isinstance(api_key,str) or not api_key or any(c.isspace() for c in api_key):
            raise ValueError("Configure the Rocoto OpenAI API credential.")
        if not isinstance(run_id,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}",run_id):
            raise ValueError("Provide a host-owned run identifier.")
        self.run_id=run_id;self.pool=pool or CompanyUsage.agent_pool(self.state)
        expected=CompanyUsage.agent_pool(self.state)
        if self.pool.company_scope!=expected.company_scope:raise ValueError("Use this company's OpenAI billing pool.")
        usage=self.pool.summary()
        if usage.get('funding_kind')=='operator_demo_budget' and (self.pool.state.name!='native-agent-api'
                or usage.get('operator_purpose')!='native_agent_chat' or usage.get('operator_model')!=MODEL):
            raise ValueError('Authorize native agent requests through their dedicated company allowance.')
        self.parent_scope=usage["parent_company_scope"]
        self.token=secrets.token_urlsafe(48);self.base_url=None
        self.unix_socket=Path(unix_socket) if unix_socket is not None else None
        self.allow_native_tool_search=allow_native_tool_search is True
        self._socket_identity=None;self._listener=None
        self.max_output_tokens=_integer(max_output_tokens,16,MAX_OUTPUT);self.allow_web_search=allow_web_search
        self.maximum_run_usd=Decimal(maximum_run_usd)
        if not self.maximum_run_usd.is_finite() or not 0<self.maximum_run_usd<=40:
            raise ValueError("Use a bounded funded run allowance.")
        self.maximum_requests=_integer(maximum_requests,1,64)
        self.directory=self.pool.state/"gateway";self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.breaker=self.directory/"admission-review-required.json"
        self.run_path=self.directory/"runs.sqlite"
        with sqlite3.connect(self.run_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, run TEXT NOT NULL, held TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS input_counts (id TEXT PRIMARY KEY, run TEXT NOT NULL)")
        self.run_path.chmod(0o600)
        self.client=httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(900,connect=20),limits=httpx.Limits(max_connections=4))
        self.app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
        self.app.add_api_route("/v1/responses",self.handle,methods=["POST"])
        self.app.add_api_route("/v1/responses/compact",self.handle,methods=["POST"])
        self._server=None;self._task=None;self._closed=False;self._active=set()

    @property
    def cli_config(self):
        if not self.base_url:raise ValueError("Start the company gateway before the native process.")
        prefix="model_providers.rocoto_metered."
        return {"model_provider":"rocoto_metered","model":MODEL,"model_reasoning_effort":"max",
            "cli_auth_credentials_store":"ephemeral",prefix+"name":"Rocoto metered OpenAI",
            prefix+"base_url":self.base_url,prefix+"wire_api":"responses",prefix+"env_key":KEY_ENV,
            prefix+"requires_openai_auth":False,prefix+"supports_websockets":False,
            prefix+"supports_standalone_web_search":False,prefix+"request_max_retries":0,
            prefix+"stream_max_retries":0,prefix+"stream_idle_timeout_ms":180000}

    async def __aenter__(self):
        try:
            if self.unix_socket is not None:
                path=self.unix_socket
                parent=path.parent.lstat()
                if (not path.is_absolute() or path.name!="model.sock" or not stat.S_ISDIR(parent.st_mode)
                        or parent.st_uid!=os.geteuid() or parent.st_mode&0o077 or path.exists() or path.is_symlink()):
                    raise ValueError("Use the broker's private unused model socket.")
                sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self._listener=sock
                sock.bind(str(path));path.chmod(0o600)
                info=path.lstat();self._socket_identity=(info.st_dev,info.st_ino)
                self.base_url=ISOLATED_RELAY_URL
            else:
                sock=socket.socket();self._listener=sock
                sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                sock.bind(("127.0.0.1",0))
                self.base_url=f"http://127.0.0.1:{sock.getsockname()[1]}/v1"
            sock.listen(16);sock.setblocking(False)
        except BaseException:
            await self.__aexit__(None,None,None)
            raise
        config=uvicorn.Config(self.app,log_level="critical",access_log=False,lifespan="off",timeout_graceful_shutdown=2)
        self._server=_LoopbackServer(config)
        self._task=asyncio.create_task(self._server.serve(sockets=[sock]))
        for _ in range(200):
            if self._server.started:return self
            if self._task.done():await self._task
            await asyncio.sleep(.01)
        await self.__aexit__(None,None,None)
        raise RuntimeError("The company model gateway did not start.")

    async def __aexit__(self,*exc):
        self.revoke()
        for response in list(self._active):
            with suppress(Exception):await response.aclose()
        with suppress(Exception):await self.client.aclose()
        if self._server:self._server.should_exit=True
        if self._task:
            try:await asyncio.wait_for(self._task,5)
            except (TimeoutError,asyncio.CancelledError):
                self._task.cancel();await asyncio.gather(self._task,return_exceptions=True)
        if self._listener:self._listener.close()
        if self.unix_socket is not None and self._socket_identity is not None:
            # The broker freezes ownership after start and later removes the run
            # directory. Do not remove a replacement socket or fight that owner.
            with suppress(OSError):
                info=self.unix_socket.lstat()
                if (info.st_dev,info.st_ino)==self._socket_identity:self.unix_socket.unlink()
        self.token="";self._api_key=""

    def revoke(self):
        """Immediately close admission before stopping the entire native tree."""
        self._closed=True
        self.token=""

    def _error(self,message,status=400):
        return JSONResponse({"error":{"message":message,"type":"rocoto_admission_error"}},status_code=status)

    def _run_reserve(self,request_id,amount):
        with sqlite3.connect(self.run_path,timeout=15) as db:
            db.execute("BEGIN IMMEDIATE")
            records=db.execute("SELECT held FROM requests WHERE run=?",(self.run_id,)).fetchall()
            if len(records)>=self.maximum_requests or sum((Decimal(r[0]) for r in records),Decimal(0))+Decimal(amount)>self.maximum_run_usd:
                raise ValueError("This run reached its request or spending ceiling.")
            db.execute("INSERT INTO requests VALUES(?,?,?)",(request_id,self.run_id,amount))

    def _run_settle(self,request_id,amount):
        with sqlite3.connect(self.run_path,timeout=15) as db:
            db.execute("UPDATE requests SET held=? WHERE id=? AND run=?",(str(amount),request_id,self.run_id))

    def _admission_headroom(self):
        """A preflight hint only; the subsequent reservations enforce races."""
        summary=self.pool.summary()
        with sqlite3.connect(self.run_path,timeout=15) as db:
            records=db.execute("SELECT held FROM requests WHERE run=?",(self.run_id,)).fetchall()
        if len(records)>=self.maximum_requests:
            raise ValueError("This run reached its request ceiling.")
        remaining=self.maximum_run_usd-sum((Decimal(r[0]) for r in records),Decimal(0))
        return min(remaining,Decimal(summary["available_usd"]),Decimal(summary["monthly_available_usd"]))

    def _finish(self,response,request_id,reservation_id,admission):
        try:
            receipt=usage_receipt(response,request_id=request_id,reservation_id=reservation_id,
                pool_scope=self.pool.company_scope,parent_scope=self.parent_scope,admission=admission)
            path=self.directory/(request_id+".usage.json")
            _private_json(path,receipt)
            self.pool.settle_openai(reservation_id,path)
            self._run_settle(request_id,receipt["provider_actual_usd"])
        except (ValueError,KeyError,TypeError,OSError):
            # Unknown usage is never released. Preserve a coarse, non-secret
            # circuit breaker so a new chat cannot hide a rate/schema mismatch.
            if not self.breaker.exists():
                try:_private_json(self.breaker,{"request_id":request_id,"reservation_id":reservation_id,
                    "reason":"provider_usage_requires_reconciliation","at":datetime.now(timezone.utc).isoformat()})
                except FileExistsError:pass

    async def _count_input(self,body,admission,request_id):
        summary=self.pool.summary()
        if min(Decimal(summary["available_usd"]),Decimal(summary["monthly_available_usd"]))<=0:
            raise ValueError("Fund this company before model input preflight.")
        with sqlite3.connect(self.run_path,timeout=15) as db:
            db.execute("BEGIN IMMEDIATE")
            count=db.execute("SELECT COUNT(*) FROM input_counts WHERE run=?",(self.run_id,)).fetchone()[0]
            if count>=self.maximum_requests:raise ValueError("This run reached its preflight-count ceiling.")
            db.execute("INSERT INTO input_counts VALUES(?,?)",(request_id,self.run_id))
        fields={"model","input","instructions","parallel_tool_calls","reasoning","text","tool_choice","tools","truncation"}
        result=await self.client.post(UPSTREAM+"/input_tokens",json={k:v for k,v in body.items() if k in fields},
            headers={"Authorization":"Bearer "+self._api_key,"X-Client-Request-Id":request_id+"-count"})
        if result.status_code!=200:raise ValueError("Provider input counting is required for this request.")
        value=result.json()
        if value.get("object")!="response.input_tokens":raise ValueError("Use the provider's complete input-count response.")
        count=_integer(value.get("input_tokens"),0,MODEL_CONTEXT)
        if count+admission["output_token_bound"]>MODEL_CONTEXT:
            raise ValueError("The counted request exceeds the model's documented context.")
        # Store count/provenance only, not opaque history or private prompts.
        _private_json(self.directory/(request_id+".count.json"),{"request_id":request_id,
            "endpoint":"responses/input_tokens","input_tokens":count,
            "billing_status":"not_priced_separately_in_published_rate_card",
            "at":datetime.now(timezone.utc).isoformat()})
        evidence={**admission,"basis":"provider_exact_input_count_plus_bounded_continuation",
            "provider_count":count,"count_request_id":request_id+"-count"}
        return _price_admission(count,admission["output_token_bound"],admission["builtin_call_bound"],evidence)

    async def handle(self,request:Request):
        supplied=request.headers.get("authorization","")
        if not hmac.compare_digest(supplied,"Bearer "+self.token) or not self.token:
            return self._error("Use this run's model capability.",401)
        if self._closed or self.breaker.exists():return self._error("Model billing evidence requires reconciliation before another request.",503)
        if request.url.query:return self._error("Use the commissioned Responses route.")
        if request.headers.get("content-type","").split(";",1)[0]!="application/json":
            return self._error("Send a JSON Responses request.",415)
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>MAX_BODY:return self._error("Reduce the request payload.",413)
        try:
            compact=request.url.path.endswith("/compact")
            if compact:body,admission=prepare_compaction(json.loads(raw),allow_native_tool_search=self.allow_native_tool_search)
            else:body,admission=prepare_request(json.loads(raw),max_output_tokens=self.max_output_tokens,
                allow_web_search=self.allow_web_search,allow_native_tool_search=self.allow_native_tool_search)
            request_id="request-"+uuid.uuid4().hex
            headroom=self._admission_headroom()
            # Counting cannot reduce generated-token or built-in-call admission.
            minimum=_price_admission(0,admission["output_token_bound"],admission["builtin_call_bound"],{})
            if Decimal(minimum["maximum_usd"])>headroom:
                raise ValueError("The bounded response exceeds this run's remaining allowance.")
            if admission.get("requires_provider_count") or Decimal(admission["maximum_usd"])>headroom:
                admission=await self._count_input(body,admission,request_id)
            if self._closed:raise ValueError("The model lease has ended.")
            reservation=self.pool.reserve(admission["maximum_usd"],request_id)
            rid=reservation["reservation_id"]
            try:self._run_reserve(request_id,admission["maximum_usd"])
            except BaseException:
                self.pool.release_before_remote(rid)
                raise
        except (ValueError,TypeError,RecursionError,httpx.HTTPError):
            if request.url.path.endswith("/compact"):
                return JSONResponse({"error":{
                    "message":"Native compaction is paused: its request settings or remaining company/turn allowance do not admit the full reserved bound. Conversation history remains intact.",
                    "type":"rocoto_admission_error","code":"compaction_admission_paused"}},status_code=402)
            return self._error("Request settings or company spending headroom do not admit this model call.",402)
        started=False
        try:
            if self._closed:raise ValueError("The model lease has ended.")
            self.pool.bind_openai_request(rid,request_id=request_id,model=MODEL,pricing=PRICING)
            _private_json(self.directory/(request_id+".admission.json"),{
                "run_id":self.run_id,"request_id":request_id,"reservation_id":rid,"model":MODEL,
                "request_sha256":hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest(),
                "admission":admission,"pricing":PRICING,"at":datetime.now(timezone.utc).isoformat()})
            upstream_request=self.client.build_request("POST",UPSTREAM+("/compact" if compact else ""),json=body,headers={
                "Authorization":"Bearer "+self._api_key,"Content-Type":"application/json",
                "X-Client-Request-Id":request_id})
            self.pool.mark_started(rid);started=True
            upstream=await self.client.send(upstream_request,stream=True)
            self._active.add(upstream)
        except BaseException:
            if not started:
                self.pool.release_before_remote(rid)
                self._run_settle(request_id,"0")
            if not isinstance(__import__('sys').exc_info()[1],Exception):raise
            return self._error("The provider request ended with its spending reservation retained.",502)
        if upstream.status_code!=200:
            error_raw=bytearray()
            try:
                async for chunk in upstream.aiter_bytes():
                    error_raw.extend(chunk)
                    if len(error_raw)>16384:break
                error_value=json.loads(error_raw).get("error",{})
                # Provider messages can echo user input. Keep them only in the
                # private receipt, never return them through native/UI errors.
                safe={key:str(error_value.get(key,""))[:2000].replace(self._api_key,"[redacted]")
                    for key in ("type","code","param","message")}
                _private_json(self.directory/(request_id+".error.json"),{"http_status":upstream.status_code,
                    "request_id":request_id,"reservation_id":rid,"provider_error":safe})
            except (ValueError,TypeError,AttributeError):pass
            finally:await upstream.aclose();self._active.discard(upstream)
            return self._error(f"Provider HTTP {upstream.status_code}; private request {request_id} retains its reservation.",502)
        if compact or not body.get("stream",True):
            try:
                data=bytearray()
                async for chunk in upstream.aiter_bytes():
                    data.extend(chunk)
                    if len(data)>MAX_RESPONSE:raise ValueError()
                response=json.loads(data)
                self._finish(response,request_id,rid,admission)
                return JSONResponse(response)
            except (ValueError,httpx.HTTPError):return self._error("The provider response requires billing reconciliation.",502)
            finally:await upstream.aclose();self._active.discard(upstream)
        async def stream():
            lines=[];terminal=False;total=0
            try:
                async for line in upstream.aiter_lines():
                    total+=len(line)+1
                    if total>MAX_RESPONSE:break
                    if line:
                        lines.append(line)
                        continue
                    event="\n".join(lines);lines=[]
                    data="\n".join(item[5:].lstrip() for item in event.splitlines() if item.startswith("data:"))
                    if data and data!="[DONE]":
                        try:item=json.loads(data)
                        except ValueError:item={}
                        if item.get("type") in {"response.completed","response.incomplete"}:
                            if not terminal:self._finish(item.get("response"),request_id,rid,admission)
                            terminal=True
                    yield (event+"\n\n").encode()
            finally:
                await upstream.aclose();self._active.discard(upstream)
                # Missing terminal usage keeps the admitted hold. The next request
                # may use only the separately available company balance.
        return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-store"})
