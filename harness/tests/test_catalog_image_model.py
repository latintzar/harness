"""Provider receipt and internal demo allowance integration, without paid calls."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
import httpx
import pytest
from PIL import Image
sys.path.insert(0,str(Path(__file__).parents[1]))
from catalog_image_model import FundedItemModel, MODEL, PRICING
from company_usage import CompanyUsage
from compute_guard import OWNER


def allocated(tmp_path):
    pool=CompanyUsage.openai_pool(tmp_path)
    summary=pool.summary()
    record={'schema_version':1,'record_id':'internal-demo-fixture','kind':'operator_demo_budget',
        'environment_id':'retail-demo','provider':'openai','company_scope':pool.company_scope,
        'parent_company_scope':summary['parent_company_scope'],'verified_by':OWNER,'user_authorized':True,
        'authorization_reference':'test-only authorization fixture','amount_usd':'1',
        'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    path=pool.state/'operator.json';path.write_text(json.dumps(record));path.chmod(0o600)
    pool.import_operator_budget(path,lifecycle='development',environment_id='retail-demo')
    return pool,path


def test_internal_budget_is_distinct_scoped_expiring_and_not_customer_money(tmp_path,monkeypatch):
    pool,path=allocated(tmp_path)
    summary=pool.summary()
    assert summary['prepaid_funded_usd']=='0.000000'
    assert summary['provider_credit_funded_usd']=='0.000000'
    assert summary['operator_demo_budget_usd']=='1.000000'
    assert summary['funding_kind']=='operator_demo_budget'
    pool.import_operator_budget(path,lifecycle='development',environment_id='retail-demo')
    assert pool.summary()['available_usd']=='1.000000'
    with pytest.raises(ValueError):pool.import_operator_budget(path,lifecycle='production',environment_id='retail-demo')
    other=CompanyUsage.openai_pool(tmp_path/'other')
    with pytest.raises(ValueError):other.import_operator_budget(path,lifecycle='development',environment_id='retail-demo')
    reservation=pool.reserve('.01','before-expiry')
    pool.bind_openai_request(reservation['reservation_id'],request_id='before-expiry',model=MODEL,pricing=PRICING)
    later=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
    monkeypatch.setattr(pool,'_now',lambda:later)
    assert pool.summary()['available_usd']=='0.000000'
    with pytest.raises(ValueError):pool.reserve('.01','after-expiry')
    with pytest.raises(ValueError):pool.mark_started(reservation['reservation_id'])


def test_metered_original_image_request_settles_once_and_does_not_bill_customer(tmp_path):
    pool,_=allocated(tmp_path);calls=[]
    def transport(request):
        body=json.loads(request.content);calls.append(body)
        assert body['model']==MODEL and body['tools']==[] and body['max_output_tokens']==768
        assert body['input'][0]['content'][1]['detail']=='original'
        answer={'description':'Green carton','visible_text':'','object_bbox':[.2,.2,.4,.5],
            'product_id':None,'uncertainty':'Label is too small to read.'}
        return httpx.Response(200,json={'id':'resp_item_test','model':MODEL,'status':'completed','service_tier':'default',
            'usage':{'input_tokens':400,'output_tokens':100,'input_tokens_details':{'cached_tokens':0}},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(answer)}]}]})
    model=FundedItemModel(tmp_path,'test-key',transport=httpx.MockTransport(transport))
    args=dict(owner='peter',image=Image.new('RGB',(400,200)),description='Lifts carton',text='',candidates=[],request_id='a'*64)
    result=model(**args)
    assert result['product_id'] is None and result['model']==MODEL
    assert pool.summary()['provider_actual_cost_usd']=='0.000200'
    assert pool.summary()['retail_charged_usd']=='0.000000'
    with pytest.raises(ValueError):model(**args)
    assert len(calls)==1


def test_unfunded_call_never_contacts_provider(tmp_path):
    def transport(request):pytest.fail('Unfunded company must not call provider')
    model=FundedItemModel(tmp_path,'test-key',transport=httpx.MockTransport(transport))
    with pytest.raises(ValueError):model(owner='peter',image=Image.new('RGB',(10,10)),description='',text='',candidates=[],request_id='b'*64)


def test_shelf_schema_output_bound_and_usage_receipt_use_one_mocked_request(tmp_path):
    pool,_=allocated(tmp_path);calls=[]
    def transport(request):
        body=json.loads(request.content);calls.append(body)
        assert body['max_output_tokens']==4096 and body['tools']==[] and not body['store']
        assert len(body['input'])==1 and body['input'][0]['content'][1]['detail']=='original'
        assert body['text']['format']['schema']['properties']['products']['maxItems']==24
        answer={'products':[{'label':'Tea carton','description':'Green carton','bbox':[.2,.2,.4,.5],
            'visible_text':'TEA','barcode_candidates':[],'brand':None,'size':None,'uncertainty':'Label partly visible'}], 'notes':''}
        return httpx.Response(200,json={'id':'resp_shelf_test','model':MODEL,'status':'completed','service_tier':'default',
            'usage':{'input_tokens':400,'output_tokens':1000,'input_tokens_details':{'cached_tokens':0}},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(answer)}]}]})
    model=FundedItemModel(tmp_path,'test-key',transport=httpx.MockTransport(transport))
    args={'owner':'peter','image':Image.new('RGB',(400,200)),'request_id':'c'*64}
    result=model.extract_shelf(**args)
    assert result['products'][0]['label']=='Tea carton'
    assert len(result['usage_receipt_sha256'])==64
    assert pool.summary()['provider_actual_cost_usd']=='0.001280'
    assert pool.summary()['retail_charged_usd']=='0.000000'
    with pytest.raises(ValueError):model.extract_shelf(**args)
    assert len(calls)==1
