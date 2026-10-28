"""Host projection of one isolated native Codex tree; Codex owns delegation/wakeups.

Only the broker, native thread/read lineage, and persisted Rocoto records establish
scope. Native IDs are never accepted as product chat IDs or client authorization.
"""
from __future__ import annotations
import asyncio
import base64
import contextlib
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from jsonschema import Draft202012Validator
from native_events import PublicActivity
from native_approvals import NativeApprovalRouter, ApprovalScope
from native_executor import NativeScope
from deploy.migration.legacy.native_files import NativeFilePublisher, PUBLISH_FILE_TOOL, PublicationError
from native_mcp import NativeMCP, NativeMCPScope
from native_provider_relay import NativeProviderRelay, _read as read_provider_authority
from deploy.migration.legacy.native_policy import policy_digest, provider_approval_policy
from deploy.migration.legacy.native_thread_tools import open_native_thread

class NativeStopped(RuntimeError):
    pass


MAX_CHILDREN = 32
QUIET_SECONDS = 1.0
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def isolated_instructions(product):
    # Product policy supplements the shipped Codex base. Host-only custom child
    # orchestration and the old shell restriction do not describe this runtime.
    product = product.replace(
        'When a request has several independent investigations, delegate them with delegate_investigations,\nthen wait_investigations and synthesize their evidence. Link each child answer with /?chat=CHILD_ID.',
        'Use native collaboration for independent investigations. Children can read the scoped Rocoto MCP tools.\nThe parent performs workspace mutations and paid video verification, then synthesizes their findings.')
    product = product.replace('Activation, purchases, shell execution and external delivery are\nhost-controlled capabilities, outside these investigation tools.',
        'Shell commands and local files run in this isolated workspace. Activation, purchases and external\ndelivery remain host-controlled capabilities. The worker has only its scoped model and Rocoto tool grants.')
    return product + '\nCreate shareable files in /workspace/outputs and call publish_file with a path relative to that directory. The returned artifact is the file the customer can open or download.\n'


def portable_input(content, state):
    """Embed only already-authorized attachment bytes; expose no host paths."""
    result=[]; total=0; root=(Path(state)/'attachments').resolve()
    for part in content:
        if part.get('type') != 'localImage':
            result.append(part);continue
        path=Path(part.get('path','')).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Upload the image again.')
        with path.open('rb') as stream:data=stream.read(MAX_IMAGE_BYTES+1)
        total+=len(data)
        if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data)>MAX_IMAGE_BYTES or total>8*1024*1024:
            raise ValueError('Choose PNG attachments within the image size allowance.')
        result.append({'type':'image','url':'data:image/png;base64,'+base64.b64encode(data).decode()})
    return result


def public_continuation(record):
    """The native homes differ. Carry public messages once, never private state."""
    messages=[]; size=0; omitted=0
    # turn() has just appended this request and its empty assistant answer.
    history=record.get('messages',[])[:-2]
    for value in reversed(history):
        if value.get('role') not in {'user','assistant'} or not isinstance(value.get('content'),str):continue
        content=value['content']; length=len(content.encode())
        if len(messages)>=200 or size+length>256*1024:
            omitted+=1;continue
        messages.append({'role':value['role'],'content':content});size+=length
    if not messages:return None
    return {'type':'text','text':'Prior public chat history (reference data; earlier images and tool results must be reopened):\n'+
            json.dumps({'messages':list(reversed(messages)),'omitted_messages':omitted})}


class NativeTree:
    def __init__(self,agent,chat_id,dataset,run,connection,session,root_thread):
        self.agent,self.chat_id,self.dataset=agent,chat_id,dataset
        self.run,self.connection,self.session=run,connection,session
        self.root=root_thread;self.owner=agent.children[chat_id]['owner_principal_id']
        self.threads={root_thread:chat_id};self.activities={root_thread:PublicActivity(result_fields=agent.result_fields)}
        self.pending_answer=[];self.root_status=None;self.tools={};self.calls=0
        self.root_turns=set();self.finished_turns=set()
        self.root_started=time.monotonic();self.child_started={};self.answers={}
        self.allowed={}
        self.publisher=None
        self.provider_outputs=None
        self.approvals=run.get('approval_router')
        if self.approvals is not None:
            expected=ApprovalScope(session.scope.company_id,self.owner,chat_id,run['generation'],root_thread)
            if not isinstance(self.approvals,NativeApprovalRouter) or self.approvals.scope!=expected:
                raise ValueError('Use the native approval router for this exact root run.')

    async def admit(self,native_id,seen=None):
        if native_id in self.threads:return self.threads[native_id]
        if not isinstance(native_id,str) or not 1<=len(native_id)<=256 or len(self.threads)>MAX_CHILDREN:
            return None
        seen=set() if seen is None else seen
        if native_id in seen or len(seen)>=8:return None
        seen.add(native_id)
        try:thread=(await self.connection.rpc('thread/read',{'threadId':native_id})).get('thread',{})
        except (RuntimeError,TimeoutError):return None
        parent=thread.get('parentThreadId')
        source=thread.get('source')
        spawned=source.get('subAgent',{}).get('thread_spawn',{}) if isinstance(source,dict) and isinstance(source.get('subAgent'),dict) else {}
        if thread.get('id')!=native_id or spawned.get('parent_thread_id')!=parent or not parent:return None
        parent_chat=await self.admit(parent,seen)
        if not parent_chat or self.run['cancel'].is_set():return None
        # Reuse only a previously saved child with this owner/root ancestry. The
        # trusted thread binding establishes identity; the event's title does not.
        old=next((key for key,value in self.agent.registry.items() if value==native_id and
            self.agent.children.get(key,{}).get('native_root_id')==self.chat_id and
            self.agent.children[key].get('owner_principal_id')==self.owner and
            self.agent.children[key].get('dataset')==self.dataset),None)
        identity=old or 'native-child-'+uuid.uuid4().hex
        path=spawned.get('agent_path')
        task_name=path.rsplit('/',1)[-1].replace('_',' ').strip() if isinstance(path,str) else ''
        title=thread.get('name') or task_name.capitalize() or thread.get('agentNickname') or 'Investigation'
        title=title[:120] if isinstance(title,str) else 'Investigation'
        record=self.agent.children.get(identity) or {'id':identity,'title':title,'parent_id':parent_chat,
            'native_root_id':self.chat_id,'dataset':self.dataset,'owner_principal_id':self.owner,
            'messages':[],'kind':'native_child'}
        recent=await self.connection.rpc('thread/turns/list',{'threadId':native_id,'limit':1,'itemsView':'summary','sortDirection':'desc'})
        latest=(recent.get('data') or [{}])[0]
        running=(thread.get('status') or {}).get('type')=='active' or latest.get('status')=='inProgress'
        record['running']=running
        if not running:record['status']=latest.get('status','completed')
        self.agent.children[identity]=record;self.agent._save_child(record);self.agent._remember(identity,native_id)
        self.threads[native_id]=identity;self.activities[native_id]=PublicActivity(result_fields=self.agent.result_fields)
        self.run['child_ids'].add(identity);self.child_started[identity]=time.monotonic()
        self.agent.active[identity]={'cancel':self.run['cancel'],'ready':self.run['ready'],
            'connection':self.connection,'thread_id':native_id,'status':'running' if running else 'completed',
            'generation':self.run['generation'],'native_root_id':self.chat_id,'native_turn_active':running}
        return identity

    def child_event(self,identity):
        record=self.agent.children[identity]
        return {'type':'subchat','chat_id':identity,'parent_id':record['parent_id'],'title':record['title'],
            'status':'running' if record.get('running') else record.get('status','completed'),
            'url':'/?chat='+identity}

    def record_child(self,identity,event):
        from scope_host import active_scope_current
        if not active_scope_current(self.agent,self.chat_id):
            self.run['cancel'].set();return
        record=self.agent.children[identity]
        if identity not in self.answers:
            answer={'role':'assistant','content':'','results':[],'tools':[],
                'started_at':datetime.now(timezone.utc).isoformat()}
            record['messages'].append(answer);self.answers[identity]=answer
        self.agent._record_event(self.answers[identity],event);self.agent._save_child(record)

    def retire_questions(self,reason):
        store=getattr(self.agent,'native_questions',None)
        events=self.approvals.retire(reason) if self.approvals is not None else []
        if store is not None:
            events += [{'type':'user_input_resolved','request':record} for record in
                store.retire_generation(self.chat_id,self.run['generation'],reason=reason)]
        chat=self.agent.children[self.chat_id]
        answer=next((value for value in reversed(chat.get('messages',[])) if value.get('role')=='assistant'),None)
        if answer is not None:
            for event in events:self.agent._record_event(answer,event)
            self.agent._save_child(chat)
        return events

    async def tool(self,item,actor):
        params=item.get('params',{});name=params.get('tool','');args=params.get('arguments',{})
        if isinstance(args,str):
            try:args=json.loads(args)
            except ValueError:args=None
        self.calls+=1
        spec=self.allowed.get(name)
        valid=spec is not None and isinstance(args,dict) and Draft202012Validator(spec['inputSchema']).is_valid(args)
        content=[{'type':'inputText','text':'Use an available parent workspace tool with its declared arguments.'}]
        success=False;results=None
        if valid and actor==self.chat_id and self.calls<=30 and not self.run['cancel'].is_set() and not self.run.get('channel_finished'):
            self.run['tool_events'].put_nowait({'type':'tool_start','id':str(item['id']),'name':name,'arguments':args})
            if name=='publish_file':
                try:
                    if self.publisher is None:raise PublicationError('publication_unavailable')
                    artifact=await self.publisher.publish(self.session,actor_chat_id=actor,**args)
                    content=[{'type':'inputText','text':json.dumps(artifact)}];success=True
                except (PublicationError,ValueError):
                    content=[{'type':'inputText','text':'The file could not be published. Check its relative output path and size, then try again.'}]
            else:
                content,results,success=await self.agent._tool(name,args,self.dataset,self.chat_id)
        if self.run['cancel'].is_set():return
        if results:self.run['tool_events'].put_nowait({'type':'results','results':results})
        if success and name=='set_chat_title':self.run['tool_events'].put_nowait({'type':'title','title':self.agent.children[self.chat_id]['title']})
        if success and name in {'create_file','prepare_export','publish_file'}:
            value=json.loads(content[0]['text']); artifact=value['file'] if name=='prepare_export' else value
            self.run['tool_events'].put_nowait({'type':'artifact','artifact':artifact})
        await self.connection.write({'id':item['id'],'result':{'contentItems':content,'success':success}})
        output='\n'.join(value.get('text','') for value in content if value.get('type')=='inputText')
        self.run['tool_events'].put_nowait({'type':'tool_end','id':str(item['id']),'name':name,
            'success':success,'output':output[:12000],'output_truncated':len(output)>12000,
            **self.agent.tool_result_fields(name, content, success)})
        if success and name in {'send_message','send_alert'} and self.run.get('messaging_delivery') and not self.run.get('channel_finished'):
            # The provider attempt has a durable receipt. Stop native generation
            # after returning that exact tool result; awaiting a human is idle.
            self.run['tool_events'].put_nowait({'type':'channel_delivery','delivery':self.run['messaging_delivery']})
            self.run['channel_finished'] = True
            await self.connection.rpc('turn/interrupt', {'threadId':self.root,'turnId':self.run['turn_id']})


    async def event(self,item):
        """Project one native event after independently admitting its actor."""
        from scope_host import active_scope_current
        if not active_scope_current(self.agent,self.chat_id):
            self.run['cancel'].set();return []
        method=item.get('method');params=item.get('params') or {}
        native_id=params.get('threadId')
        if method=='thread/started':native_id=(params.get('thread') or {}).get('id')
        if native_id is None:
            # Global startup notices carry no chat actor. They grant no authority.
            return []
        actor=await self.admit(native_id)
        if actor is None:
            if 'id' in item:await self.connection.write({'id':item['id'],'error':{'code':-32600,'message':'Use the admitted workspace thread.'}})
            return []
        output=[]
        native_item=params.get('item') or {}
        candidates=[]
        if native_item.get('type')=='subAgentActivity':candidates=[native_item.get('agentThreadId')]
        elif native_item.get('type')=='collabAgentToolCall':candidates=(native_item.get('receiverThreadIds') or [])[:32]
        for candidate in candidates:
            identity=await self.admit(candidate)
            if identity and identity!=self.chat_id:output.append(self.child_event(identity))
        if self.approvals is not None:
            output.extend(await self.approvals.event(item,actor_chat_id=actor))
        elif method=='mcpServer/elicitation/request' and 'id' in item:
            await self.connection.write({'id':item['id'],'result':{'action':'decline','content':None}})
        if method=='mcpServer/elicitation/request':return output
        if method=='item/tool/requestUserInput' and 'id' in item:
            store=getattr(self.agent,'native_questions',None)
            if store is None:
                await self.connection.write({'id':item['id'],'result':{'answers':{}}})
                return output
            request=store.open(root_chat_id=self.chat_id,actor_chat_id=actor,owner=self.owner,
                generation=self.run['generation'],native_request_id=item['id'],params=params,
                actor_title=self.agent.children[actor].get('title','Rocoto'))
            if request.get('status')=='unsupported':
                await self.connection.write({'id':item['id'],'result':{'answers':{}}})
            output.append({'type':'user_input_request','request':request});return output
        if method=='serverRequest/resolved':
            store=getattr(self.agent,'native_questions',None)
            request=store.resolve_native(self.chat_id,self.run['generation'],params.get('requestId')) if store else None
            if request:output.append({'type':'user_input_resolved','request':request})
            return output
        if method=='item/tool/call' and 'id' in item:
            if actor!=self.chat_id:
                await self.connection.write({'id':item['id'],'result':{'success':False,
                    'contentItems':[{'type':'inputText','text':'Use the inherited read-only Rocoto MCP tools; ask the parent to change workspace state.'}]}})
            elif len(self.tools)>=4:
                await self.connection.write({'id':item['id'],'result':{'success':False,
                    'contentItems':[{'type':'inputText','text':'Finish an active workspace tool before starting another.'}]}})
            else:
                task=asyncio.create_task(self.tool(item,actor));self.tools[item['id']]=task
            return output
        if method=='turn/started':
            turn_id=(params.get('turn') or {}).get('id')
            active=self.agent.active[actor];active.update(turn_id=turn_id,native_turn_active=True,status='running')
            if actor==self.chat_id:
                if self.pending_answer:
                    output.append({'type':'progress','item_id':'intermediate-'+str(self.run.get('turn_id')),
                        'activity':'intermediate_answer','text':''.join(self.pending_answer)})
                    self.pending_answer=[]
                self.root_turns.add(turn_id);self.root_status=None;self.run['ready'].set()
                output.append({'type':'running','turn_id':turn_id})
            else:
                self.agent.children[actor]['running']=True;self.answers.pop(actor,None)
                output.append(self.child_event(actor))
            return output
        if method=='turn/completed':
            turn=params.get('turn') or {};turn_id=turn.get('id');status=turn.get('status') or 'completed'
            if actor==self.chat_id:
                if turn_id!=self.run.get('turn_id'):return output
                self.run['native_turn_active']=False;self.root_status=status;self.finished_turns.add(turn_id)
            else:
                self.agent.active[actor]['native_turn_active']=False
                record=self.agent.children[actor];record.update(running=False,status=status)
                self.record_child(actor,{'type':'done','status':status,'completed_at':datetime.now(timezone.utc).isoformat(),
                    'elapsed_ms':round((time.monotonic()-self.child_started[actor])*1000)})
                output.append(self.child_event(actor))
            return output
        resources=[]
        if self.provider_outputs is not None:
            resources=await self.provider_outputs.event(method,params,actor_chat_id=actor)
        public=self.activities[native_id].translate(method,params,request_id=item.get('id'))
        if public is not None:
            if public['type'] in {'user_input_request','user_input_resolved'}:return output
            if actor!=self.chat_id:
                self.record_child(actor,public)
            elif public['type']=='delta' and self.run['child_ids']:self.pending_answer.append(public['text'])
            else:
                if public['type']=='usage':self.agent._remember_usage(actor,public)
                output.append(public)
        elif 'id' in item and method:
            # Approval is never inferred from a model request. The outer worker
            # already fixes its filesystem/network capabilities.
            await self.connection.write({'id':item['id'],'error':{'code':-32601,'message':'This request needs a supported workspace permission.'}})
        for resource in resources:
            if actor!=self.chat_id:self.record_child(actor,resource)
            else:output.append(resource)
        return output

    def settling(self):
        return self.root_status is not None and not self.tools and not any(
            self.agent.children[key].get('running') for key in self.run['child_ids'])

    async def settled(self):
        # Native children can wake the root after its first completion. A quiet
        # transport plus a fresh native root status forms the completion barrier.
        if not self.settling():return False
        thread=(await self.connection.rpc('thread/read',{'threadId':self.root})).get('thread',{})
        status=(thread.get('status') or {}).get('type')
        if thread.get('id')!=self.root or status not in ({'idle','systemError'} if self.root_status=='failed' else {'idle'}):return False
        return self.connection.events.empty() and self.run['tool_events'].empty()

    async def expire_questions(self):
        store = getattr(self.agent, 'native_questions', None)
        if store is None:
            return []
        def active():
            return self.agent.active.get(self.chat_id) is self.run and not self.run['cancel'].is_set()
        async def deliver(native_id, response):
            await asyncio.wait_for(self.connection.write({'id':native_id, 'result':response}, guard=active), 5)
        requests = await store.expire(owner=self.owner, root_chat_id=self.chat_id,
            generation=self.run['generation'], deliver=deliver, cancelled=self.run['cancel'])
        return [{'type':'user_input_resolved', 'request':request} for request in requests]

    async def stream(self):
        while not self.run['cancel'].is_set():
            from scope_host import active_scope_current
            if not active_scope_current(self.agent,self.chat_id):
                self.run['cancel'].set();break
            for event in await self.expire_questions():yield event
            if self.approvals is not None:
                for event in await self.approvals.expire():yield event
            remaining=self.run['deadline']-time.monotonic()
            if remaining<=0:raise TimeoutError('The investigation reached its time limit. Continue with a follow-up.')
            for key,task in list(self.tools.items()):
                if task.done():
                    self.tools.pop(key);task.result()
            native=asyncio.create_task(self.connection.events.get())
            notice=asyncio.create_task(self.run['tool_events'].get())
            cancelled=asyncio.create_task(self.run['cancel'].wait())
            waiters={native,notice,cancelled,*self.tools.values()}
            timeout=min(remaining,(QUIET_SECONDS if self.run['child_ids'] else .01) if self.settling() else 180)
            if self.approvals is not None or getattr(self.agent, 'native_questions', None) is not None or getattr(self.agent,'business_scope_host',None) is not None:
                timeout=min(timeout,1.0)
            try:
                done,_=await asyncio.wait(waiters,timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in (native,notice,cancelled):
                    if not waiter.done():waiter.cancel()
                await asyncio.gather(native,notice,cancelled,return_exceptions=True)
            if not active_scope_current(self.agent,self.chat_id):self.run['cancel'].set()
            if self.run['cancel'].is_set():break
            if self.run.get('channel_finished'):
                if notice in done: yield notice.result()
                while not self.run['tool_events'].empty(): yield self.run['tool_events'].get_nowait()
                self.pending_answer=[]
                self.root_status='waiting_for_reply' if self.run['messaging_delivery']['waiting_for_reply'] else 'delivery_needs_attention'
                break
            if not done:
                if self.settling() and await self.settled():break
                continue
            if notice in done:yield notice.result()
            if native in done:
                value=native.result()
                if value.get('connection_error'):raise RuntimeError('The isolated agent connection ended. Continue with a follow-up.')
                for event in await self.event(value):yield event
        if not self.run['cancel'].is_set() and self.pending_answer:
            yield {'type':'delta','text':''.join(self.pending_answer)}

    async def close(self):
        for task in self.tools.values():task.cancel()
        if self.tools:await asyncio.gather(*self.tools.values(),return_exceptions=True)
        self.tools.clear()
        for native_id,identity in self.threads.items():
            if identity==self.chat_id:continue
            record=self.agent.children[identity]
            if record.get('running'):
                record.update(running=False,status='interrupted')
                self.record_child(identity,{'type':'done','status':'interrupted'})
            self.agent.active.pop(identity,None)


@contextlib.asynccontextmanager
async def cleanup_tracked_lease(launcher,scope,run):
    """Record the launcher's confirmed broker cleanup independently of readiness.

    Explicit __aexit__ lets a successfully cleaned-up body exception (including
    early Stop) retain that confirmation while a cleanup failure stays uncertain.
    Admission failures can include a partially started worker, so only a completed
    exit or the session's broker receipt establishes stopped status.
    """
    manager=launcher.lease(scope,run_id='turn-'+run['generation'],
        **({'workspace_binding':run['workspace_binding']} if run.get('workspace_binding') else {}))
    session=None;run['cleanup_status']='pending'
    try:
        session=await manager.__aenter__()
        try:
            yield session
        except BaseException as error:
            suppressed=await manager.__aexit__(type(error),error,error.__traceback__)
            run['cleanup_status']='stopped'
            if not suppressed:raise
        else:
            await manager.__aexit__(None,None,None)
            run['cleanup_status']='stopped'
    except BaseException:
        if run['cleanup_status']!='stopped':
            run['cleanup_status']='stopped' if getattr(session,'stopped',False) is True else 'uncertain'
        raise
    finally:
        run['cleanup_finished'].set()


async def run_isolated(agent,message,chat_id,dataset,attachments=(),clip_attachments=(),resolved_skills=None,*,model_context=''):
    from agent_bridge import system_instructions, tool_specs
    from deploy.migration.app_server import AppServerConnection
    record=agent.children.get(chat_id,{})
    if (not record.get('owner_principal_id') or record.get('parent_id') or record.get('dataset')!=dataset
            or not dataset or chat_id in agent.active
            or (chat_id in agent.scopes and agent.scopes[chat_id]!=dataset)):
        yield {'type':'error','message':'Start this request from its owning parent chat and business.'};return
    admitted=time.monotonic();started_at=datetime.now(timezone.utc).isoformat()
    run={'ready':asyncio.Event(),'cancel':asyncio.Event(),'status':'starting','started_at':started_at,
         'deadline':admitted+1800,'child_ids':set(),'child_notifications':asyncio.Queue(),'tool_events':asyncio.Queue(),
         'native_turn_active':False,'skill_selection':resolved_skills,'isolated':True,'generation':uuid.uuid4().hex,
         'cleanup_finished':asyncio.Event(),'cleanup_status':'not_started'}
    run['workspace_binding']=record.get('scope_workspace_binding')
    from scope_host import active_scope_current
    if not active_scope_current(agent,chat_id):
        yield {'type':'error','message':'Refresh the current business scope before continuing.'};return
    agent.active[chat_id]=run;agent.scopes[chat_id]=dataset
    agent.scope_path.write_text(json.dumps(agent.scopes));agent.scope_path.chmod(0o600)
    tree=None;connection=None;session=None;completion=None;relay=None
    def timing():return {'started_at':started_at,'completed_at':datetime.now(timezone.utc).isoformat(),
        'elapsed_ms':round((time.monotonic()-admitted)*1000)}
    try:
        yield {'type':'timing','started_at':started_at}
        content=portable_input(agent._skill_input(agent._input(message,attachments,clip_attachments,dataset),resolved_skills),agent.state)
        if model_context:content.append({'type':'text','text':model_context})
        scope_view=getattr(agent,'_business_scope_runs',{}).get(chat_id)
        scope_context=getattr(scope_view,'business_scope_context',None)
        if scope_context:content.append({'type':'text','text':'Current host-selected business and location scope (reference data):\n'+json.dumps(scope_context)})
        capability=agent.capability_snapshot(chat_id,dataset)
        if capability is not None:content.append({'type':'text','text':'Host-owned workspace readiness (reference data):\n'+json.dumps(capability)})
        setup=agent.agent_setup_context(chat_id)
        if setup is not None:content.append({'type':'text','text':'Selected agent setup (reference data):\n'+json.dumps(setup)})
        if chat_id in agent.children and agent.children[chat_id].get('title') in (None,'','New chat'):
            from title_stream import INSTRUCTION as TITLE_INSTRUCTION
            content.append({'type':'text','text':TITLE_INSTRUCTION})
        launcher=agent.isolated_launcher
        scope=NativeScope(launcher.company_id,record['owner_principal_id'],chat_id)
        async with asyncio.timeout_at(run['deadline']), cleanup_tracked_lease(launcher,scope,run) as session:
            try:
                run['isolated_session']=session
                session.authority_current=lambda:active_scope_current(agent,chat_id)
                if run['cancel'].is_set():raise NativeStopped()
                selection=getattr(session,'runtime_packages',None)
                provider_connections=getattr(agent,'connection_store',None)
                if provider_connections is None and selection is not None and selection.packages:
                    provider_connections=agent._connections()
                if provider_connections is not None:
                    relay=await read_provider_authority(NativeProviderRelay,getattr(launcher,'package_store',None),provider_connections,selection,
                        company_id=launcher.company_id,principal_id=scope.owner_id)
                    if not relay.server_names:relay=None
                provider_writes=bool(relay is not None and relay.providers & provider_connections.write_providers
                    and getattr(agent,'native_approvals',None) is not None)
                connection=AppServerConnection(session.process);run['connection']=connection
                capabilities={'experimentalApi':True}
                if provider_writes:capabilities['mcpServerOpenaiFormElicitation']=True
                await connection.rpc('initialize',{'clientInfo':{'name':'rocoto','title':'Rocoto','version':'0.3.0'},'capabilities':capabilities})
                await connection.write({'method':'initialized'})
                from intake_instructions import for_chat as intake_instructions
                params=session.thread_params(isolated_instructions(intake_instructions(agent,chat_id,system_instructions())),[*tool_specs(True, agent=agent),PUBLISH_FILE_TOOL])
                if provider_writes:params['approvalPolicy']=provider_approval_policy()
                digest_params=copy.deepcopy(params)
                # Per-run capability values live only in the worker config, not in
                # persisted toolset identity. Provider contract/model remain hashed.
                digest_params['config'].pop('model_providers.rocoto_metered.base_url',None)
                digest=policy_digest(digest_params)
                old=agent.registry.get(chat_id);metadata=agent.toolsets.get(old,{})
                existing=old if metadata.get('runtime')=='isolated' else None
                if not existing and not record.get('scope_fresh_native'):
                    history=public_continuation(record)
                    if history:content.insert(0,history)
                opened=await open_native_thread(connection,existing_id=existing,known_toolset=metadata.get('digest'),
                    toolset_digest=digest,start_params=params)
                thread=opened['thread'];native_id=thread['id']
                verified=(await connection.rpc('thread/read',{'threadId':native_id,'includeTurns':False})).get('thread',{})
                if verified.get('id')!=native_id or verified.get('parentThreadId'):
                    raise RuntimeError('The isolated workspace could not establish its parent thread.')
                agent._remember_toolset(native_id,digest,opened);agent.toolsets[native_id]['runtime']='isolated'
                agent.toolsets_path.write_text(json.dumps(agent.toolsets));agent.toolsets_path.chmod(0o600)
                agent._remember(chat_id,native_id);run['thread_id']=native_id
                record.pop('scope_fresh_native',None);agent._save_child(record)
                if relay is not None:
                    if provider_writes:
                        approval_scope=ApprovalScope(launcher.company_id,scope.owner_id,chat_id,run['generation'],native_id)
                        relay.configure_writes(agent.native_approvals,approval_scope,enabled=True)
                        run['approval_router']=NativeApprovalRouter(agent.native_approvals,approval_scope,
                            resolve_binding=relay.approval_binding,cancelled=run['cancel'],write=connection.write)
                mcp=NativeMCP(agent,NativeMCPScope(launcher.company_id,scope.owner_id,dataset,chat_id,native_id),
                    company_id=launcher.company_id,provider_relay=relay)
                config=await session.bind_mcp(mcp)
                await connection.rpc('config/value/write',config)
                await connection.rpc('config/mcpServer/reload',None)
                status=await connection.rpc('mcpServerStatus/list',{'threadId':native_id})
                if not any(entry.get('name')=='rocoto' and entry.get('tools') for entry in status.get('data',[])):
                    raise RuntimeError('The scoped workspace tools are still connecting. Try the request again.')
                tree=NativeTree(agent,chat_id,dataset,run,connection,session,native_id)
                if relay is not None:
                    from provider_outputs import ProviderOutputs
                    tree.provider_outputs=ProviderOutputs(agent._files(),relay,owner=scope.owner_id,
                        root_chat_id=chat_id,dataset=dataset,company_id=scope.company_id,
                        current=lambda:agent.active.get(chat_id) is run and not run['cancel'].is_set()
                            and not session._closing and not session.stopped and active_scope_current(agent,chat_id))
                tree.publisher=NativeFilePublisher(agent._files(),company_id=scope.company_id,
                    owner_principal_id=scope.owner_id,root_chat_id=chat_id,dataset=dataset)
                tree.allowed={spec['name']:spec for spec in params['dynamicTools']}
                yield {'type':'thread','thread_id':native_id}
                if run['cancel'].is_set():raise NativeStopped()
                from native_skills import native_skill_inputs
                skill_input=await native_skill_inputs(agent,resolved_skills,run)
                content.extend(skill_input)
                result=await connection.rpc('turn/start',{'threadId':native_id,'input':content},
                    guard=lambda:active_scope_current(agent,chat_id) and not run['cancel'].is_set()
                        and (not skill_input or run['native_skill_guard']()))
                run.update(turn_id=result['turn']['id'],native_turn_active=True,status='running');run['ready'].set()
                async for event in tree.stream():yield event
                if not run['cancel'].is_set() and tree.root_status=='completed':agent.api_verified_at=time.time()
                for event in tree.retire_questions('turn_ended'):yield event
                completion={'type':'done','thread_id':native_id,'status':'interrupted' if run['cancel'].is_set() else tree.root_status}
            finally:
                run['cancel'].set()
                if tree:
                    tree.retire_questions('turn_ended')
                    await tree.close()
                if relay is not None and getattr(relay,'write_transport',None) is not None:
                    await relay.write_transport.close()
                if connection:
                    connection.reader.cancel()
                    await asyncio.gather(connection.reader,return_exceptions=True)
        if completion is not None:
            yield {**completion,**timing()}
    except NativeStopped:
        yield {'type':'done','thread_id':run.get('thread_id'),'status':'interrupted',**timing()}
    except (RuntimeError,TimeoutError,OSError,ValueError,KeyError,ExceptionGroup):
        # Exceptions can contain native configuration/arguments. Keep detailed
        # diagnostics private; public status never reflects bearer values.
        yield {'type':'error','message':'The isolated agent could not finish this request. Continue with a follow-up.'}
        yield {'type':'timing','status':'failed',**timing()}
    finally:
        agent._cancel_credential_waits(chat_id,'stopped' if run['cancel'].is_set() else 'turn_ended')
        run['cancel'].set();run['ready'].set()
        if tree:
            tree.retire_questions('interrupted');await tree.close()
        if connection:
            # The launch lease owns actual worker Stop. Closing the reader is
            # distinct from terminating a subprocess (the worker is brokered).
            connection.reader.cancel();await asyncio.gather(connection.reader,return_exceptions=True)
        run['cleanup_finished'].set()
        agent.active.pop(chat_id,None)
