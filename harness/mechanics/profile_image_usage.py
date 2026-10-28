"""Images API receipts settled into the existing company OpenAI funding pool.

Images receipts retain their own endpoint and provider request identity. They are
never represented as Responses API output or an invoice reconciliation.
"""
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import re

from company_usage import _money, _provider_money, _read_private

MODEL = 'gpt-image-2.5-flare-2026-09-08'
PRICING = {'revision':'openai-images-standard-2026-10-06-flare',
    'source_url':'https://developers.openai.com/api/docs/models/gpt-image-2.5-flare',
    'input_usd_per_million':'5', 'cached_input_usd_per_million':'1.25',
    'cache_write_input_usd_per_million':'5', 'output_usd_per_million':'30', 'tool_usd_per_call':{}}
INPUT_BOUND = 8192
OUTPUT_BOUND = 8192
MAXIMUM_USD = '0.286720'


def measured_usage(value):
    if not isinstance(value,dict) or set(value)!={'input_tokens','input_tokens_details','output_tokens','total_tokens'}:
        raise ValueError('Review the complete image-generation usage receipt.')
    detail=value['input_tokens_details']
    if not isinstance(detail,dict) or set(detail)!={'text_tokens','image_tokens'}:
        raise ValueError('Review the image request input breakdown.')
    if any(type(v) is not int or not 0<=v<=100_000_000 for v in [value['input_tokens'],value['output_tokens'],value['total_tokens'],*detail.values()]):
        raise ValueError('Review measured image token counts.')
    if detail['image_tokens']!=0 or detail['text_tokens']!=value['input_tokens'] or value['total_tokens']!=value['input_tokens']+value['output_tokens']:
        raise ValueError('Use this text-only generation request’s measured usage.')
    return value


def actual_cost(usage):
    measured_usage(usage)
    return (usage['input_tokens']*Decimal(PRICING['input_usd_per_million'])
        +usage['output_tokens']*Decimal(PRICING['output_usd_per_million']))/1_000_000


def bind(pool,reservation_id,request_id):
    pool.bind_openai_request(reservation_id,request_id=request_id,model=MODEL,pricing=PRICING)


def settle(pool,reservation_id,receipt_path):
    raw=_read_private(receipt_path);receipt=json.loads(raw)
    fields={'schema_version','provider','kind','endpoint','parent_company_scope','pool_scope','reservation_id',
        'request_id','provider_request_id','model','usage','pricing','provider_actual_usd','received_at','image_sha256'}
    if (not isinstance(receipt,dict) or set(receipt)!=fields or receipt['schema_version']!=1
            or receipt['provider']!='openai' or receipt['kind']!='images_usage'
            or receipt['endpoint']!='/v1/images/generations' or receipt['pool_scope']!=pool.company_scope
            or receipt['reservation_id']!=reservation_id or receipt['model']!=MODEL or receipt['pricing']!=PRICING
            or not isinstance(receipt['provider_request_id'],str)
            or not re.fullmatch(r'req_[A-Za-z0-9_-]{1,190}',receipt['provider_request_id'])
            or not isinstance(receipt['image_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',receipt['image_sha256'])):
        raise ValueError('Use the admitted image request’s original usage receipt.')
    try:
        if datetime.fromisoformat(receipt['received_at'].replace('Z','+00:00')).tzinfo is None:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise ValueError('Record a timezone-qualified image receipt time.')from None
    exact=actual_cost(receipt['usage']);actual=_provider_money(exact)
    if Decimal(str(receipt['provider_actual_usd']))!=exact:
        raise ValueError('Reconcile measured image tokens at their pinned rates.')
    digest=hashlib.sha256(raw).hexdigest()
    with pool.connect() as db:
        db.execute('BEGIN IMMEDIATE');binding=pool._openai_binding(db)
        if receipt['parent_company_scope']!=binding['parent_company_scope']:
            raise ValueError('Use this company’s image receipt.')
        record=pool._reservation(db,reservation_id)
        request=db.execute('SELECT * FROM openai_requests WHERE reservation_id=?',(reservation_id,)).fetchone()
        if (not request or request['request_id']!=receipt['request_id'] or request['model']!=MODEL
                or json.loads(request['pricing'])!=PRICING):
            raise ValueError('Keep the image request’s original identity and pricing.')
        prior=db.execute('SELECT * FROM openai_response_charges WHERE response_id=? OR reservation_id=?',
            (receipt['provider_request_id'],reservation_id)).fetchone()
        if prior:
            if (prior['reservation_id'],prior['response_id'],prior['receipt_sha256'])!=(reservation_id,receipt['provider_request_id'],digest):
                raise ValueError('Settle each image request once with its immutable receipt.')
            return pool._summary(db)
        if record['status']!='running':raise ValueError('Settle an admitted image request.')
        db.execute('INSERT INTO openai_response_charges VALUES(?,?,?)',(receipt['provider_request_id'],reservation_id,digest))
        charge=int((actual*Decimal(record['retail_multiplier'] or '1')).to_integral_value(rounding=ROUND_CEILING))
        pool._append(db,'settled:'+reservation_id,'settled',charge,reservation_id,
            provider_actual=actual,retail_charge=charge,provider_actual_usd_raw=str(exact),
            bound_exceeded=actual>_money(record['provider_maximum_usd']),
            source={'provider':'openai','request_id':receipt['request_id'],'provider_request_id':receipt['provider_request_id'],
                'receipt_sha256':digest,'model':MODEL,'usage':receipt['usage'],'pricing':PRICING,
                'endpoint':'/v1/images/generations','received_at':receipt['received_at'],
                'basis':'images_usage_at_pinned_rates','invoice_status':'unreconciled'})
        return pool._summary(db)
