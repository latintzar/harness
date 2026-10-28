"""One explicit, tiny commissioning request through the actual company gateway.

The operator commissions the existing company OpenAI pool first. This helper
never funds or reads a key from disk; the protected runtime environment supplies it.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
import uuid

import httpx
from company_usage import CompanyUsage
from openai_gateway import MODEL, ResponsesGateway, prepare_request


async def smoke(state,*,api_key):
    state=Path(state).resolve()
    request={'model':MODEL,'input':'Return exactly: Rocoto API ready.',
             'reasoning':{'effort':'low'},'max_output_tokens':128,'stream':False,'store':False}
    _,bound=prepare_request(request,max_output_tokens=128,allow_web_search=False)
    from decimal import Decimal
    if Decimal(bound['maximum_usd'])>Decimal('.10'):
        raise ValueError('This commissioning request exceeds its authorized ten-cent bound.')
    pool=CompanyUsage.agent_pool(state);before=pool.summary();started=time.monotonic()
    run='commission-'+uuid.uuid4().hex
    async with ResponsesGateway(state,api_key=api_key,run_id=run,pool=pool,
            max_output_tokens=128,allow_web_search=False,maximum_run_usd='.10',maximum_requests=1) as gateway:
        async with httpx.AsyncClient(timeout=180,trust_env=False) as client:
            result=await client.post(gateway.base_url+'/responses',json=request,
                headers={'Authorization':'Bearer '+gateway.token})
        body=result.json()
    return {'run_id':run,'http_status':result.status_code,'elapsed_seconds':round(time.monotonic()-started,3),
        'admission':bound,'model':body.get('model'),'response_id':body.get('id'),'status':body.get('status'),
        'usage':body.get('usage'),'output':body.get('output'),'error':body.get('error'),
        'before':before,'after':pool.summary()}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    if not args.execute:parser.error('Explicit --execute is required for this paid commissioning request.')
    key=os.environ.get('ROCOTO_OPENAI_API_KEY')
    if not key:parser.error('Commission the protected Rocoto API runtime credential first.')
    print(json.dumps(asyncio.run(smoke(args.state,api_key=key)),indent=2))


if __name__=='__main__':main()
