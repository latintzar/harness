"""Explicit native tool roundtrip commissioning through the funded API gateway.

No ledger funding, credential-cache access, GPU work or source-footage processing.
The operator runs this once after the direct gateway proof on the same company pool.
"""
from __future__ import annotations
import argparse
import asyncio
from functools import partial
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from agent_auth import AgentAuth
from deploy.migration.app_server import AppServerConnection
from company_usage import CompanyUsage
from native_events import PublicActivity
from deploy.migration.legacy.native_policy import startup_policy, cli_args, thread_params
from openai_gateway import ResponsesGateway, _private_json


async def smoke(state,*,executable=None):
    state=Path(state).resolve();pool=CompanyUsage.agent_pool(state)
    before=pool.summary();run_id='native-commission-'+uuid.uuid4().hex
    directory=state/'api-commissioning'/run_id;directory.mkdir(mode=0o700,parents=True)
    started=time.monotonic();events=[];tool_calls=0;final=[];thread_id=None;turn_status=None
    connection=None;failure=None
    auth=AgentAuth(state,mode='openai_api',runtime_host='rocoto-staging')
    gateway_factory=partial(ResponsesGateway,max_output_tokens=2048,allow_web_search=False,
                            maximum_run_usd='1',maximum_requests=4)
    instructions=('This is a commissioning test. Call commissioning_ping with nonce rocoto exactly once. '
                  'After the tool returns status ok, reply exactly: Rocoto API ready. Use only that scoped tool.')
    tools=[{'name':'commissioning_ping','description':'Return this company commissioning nonce.',
        'inputSchema':{'type':'object','properties':{'nonce':{'type':'string','enum':['rocoto']}},
                       'required':['nonce'],'additionalProperties':False}}]
    try:
        async with auth.lease(run_id=run_id,gateway_factory=gateway_factory) as launch:
            exe=executable or shutil.which('codex')
            if not exe:raise ValueError('Install the declared native Codex runtime first.')
            config=startup_policy(exe,directory,launch.child_env)
            config.update(launch.cli_config)
            config['web_search']='disabled'
            process=await asyncio.create_subprocess_exec(exe,'app-server','--listen','stdio://',
                *cli_args(config),env=launch.child_env,cwd=str(directory),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,limit=16*1024*1024)
            connection=AppServerConnection(process)
            try:
                await connection.rpc('initialize',{'clientInfo':{'name':'rocoto_api_commission','version':'0.1.0'},
                    'capabilities':{'experimentalApi':True}})
                await connection.write({'method':'initialized'})
                params=thread_params(directory,instructions,tools)
                params['config'].update({'web_search':'disabled','model_reasoning_effort':'max'})
                result=await connection.rpc('thread/start',params);thread_id=result['thread']['id']
                turn=await connection.rpc('turn/start',{'threadId':thread_id,'input':[{'type':'text',
                    'text':'Run the commissioning_ping tool with nonce rocoto and return the required exact final response.'}]})
                activity=PublicActivity()
                deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    event=await asyncio.wait_for(connection.events.get(),max(1,deadline-time.monotonic()))
                    if 'connection_error' in event:raise RuntimeError(event['connection_error'])
                    method=event.get('method');params=event.get('params',{})
                    public=activity.translate(method,params)
                    if public:
                        events.append(public)
                        if public['type']=='delta':final.append(public['text'])
                    if method=='item/tool/call':
                        args=params.get('arguments',{})
                        if isinstance(args,str):args=json.loads(args)
                        valid=params.get('tool')=='commissioning_ping' and args=={'nonce':'rocoto'} and tool_calls==0
                        if valid:tool_calls+=1
                        answer={'status':'ok','nonce':'rocoto','company_scope':pool.summary()['parent_company_scope']} if valid else {'status':'denied'}
                        events.append({'type':'tool_call','tool':params.get('tool'),'success':valid})
                        await connection.write({'id':event['id'],'result':{'success':valid,
                            'contentItems':[{'type':'inputText','text':json.dumps(answer)}]}})
                    elif method=='turn/completed':
                        turn_status=params.get('turn',{}).get('status')
                        if turn_status=='failed':failure=(params['turn'].get('error') or {}).get('message','Native turn failed')
                        break
                    elif 'id' in event and method:
                        await connection.write({'id':event['id'],'error':{'code':-32601,'message':'Only commissioning_ping is enabled.'}})
                else:failure='Native commissioning reached its wall-time limit.'
            finally:
                await connection.close();connection=None
    except (RuntimeError,ValueError,TimeoutError,OSError) as exc:
        failure=str(exc)[:500]
    finally:
        if connection is not None:await connection.close()
    answer=''.join(final)
    result={'run_id':run_id,'thread_id':thread_id,'status':turn_status,'tool_calls':tool_calls,
        'final':answer,'passed':turn_status=='completed' and tool_calls==1 and answer.strip()=='Rocoto API ready.',
        'elapsed_seconds':round(time.monotonic()-started,3),'failure':failure,'events':events,
        'before':before,'after':pool.summary(),'limits':{'maximum_run_usd':'1','maximum_output_tokens_per_request':2048,
            'maximum_provider_requests':4,'web_search':False,'maximum_wall_seconds':180}}
    _private_json(directory/'result.json',result)
    return {**result,'record_path':str(directory/'result.json')}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--codex');parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    if not args.execute:parser.error('Explicit --execute is required for this paid native commissioning proof.')
    if not os.environ.get('ROCOTO_OPENAI_API_KEY'):parser.error('Commission the protected Rocoto API runtime credential first.')
    result=asyncio.run(smoke(args.state,executable=args.codex))
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
