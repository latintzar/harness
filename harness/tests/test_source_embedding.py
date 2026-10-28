import asyncio
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
import source_embedding as embedding

@pytest.fixture
def anyio_backend(): return 'asyncio'

@pytest.fixture
def context():
    return {'asset':{'id':'asset-'+'a'*32,'owner':'owner-a','sha256':'c'*64},'source':{'collection':'cafe'},
        'completed_stages':{'prepare':{'profile':'source-media-prepare-v2','recipe':'rocoto-cfr24-h264-segments-v2',
            'manifest_sha256':'d'*64,'image_digest':'sha256:'+'e'*64}}}

@pytest.fixture
def batch(context):
    data=b'opaque-derivative-video'
    return [{'id':context['asset']['id']+'-clip-000000','sha256':hashlib.sha256(data).hexdigest(),
             'size_bytes':len(data),'duration_seconds':1.,'content':data}]

@pytest.fixture
def approval(tmp_path):
    folder=tmp_path/'funding';folder.mkdir(mode=0o700)
    now=datetime.now(timezone.utc)
    value={'schema_version':1,'approval_id':'source-embedding-fixture','purpose':'rocoto-video-embedding-pilot',
        'provider':'modal','workspace':embedding.WORKSPACE,'verified_by':embedding.compute_guard.OWNER,
        'verification_source':'https://modal.com/settings/usage','user_verified':True,
        'verified_at':now.isoformat(),'expires_at':(now+timedelta(hours=1)).isoformat(),
        'net_spend_limit_usd':'0','credit_remaining_usd':'20','reserved_credit_usd':'10','approved_run_gross_usd':'1.50'}
    path=folder/'approval.json';path.write_text(json.dumps(value));path.chmod(0o600)
    return path


KEY='job-'+'b'*32+':1:0'


def result(record):
    return {'model':embedding.MODEL,'model_revision':embedding.REVISION,'dimensions':256,'frames_per_clip':8,
        'attempt_id':record['attempt_id'],'clips':[{'id':clip['id'],'sha256':clip['sha256'],
             'embedding':[1.]+[0.]*255} for clip in record['binding']['clips']]}


class FakeRunner:
    def __init__(self): self.calls=0;self.failure=None;self.corrupt=None
    async def run(self,directory,record,*,cancelled):
        self.calls+=1
        assert Path(record['claimed_receipt']).exists()
        assert embedding.read_json(directory/'attempt.json')['status']=='launching'
        assert (directory/'0000.mp4').read_bytes()==b'opaque-derivative-video'
        embedding.provider_update(directory,record,app_id='ap-fixture',function_call_id='fc-fixture',stop_confirmed=True)
        if self.failure: raise self.failure
        value=result(record)
        if self.corrupt: self.corrupt(value)
        return value


def adapter(tmp_path,approval=None,runner=None,**kwargs):
    return embedding.SourceEmbedding(tmp_path,company_id='company-a',approval_path=approval,
        runner=runner or FakeRunner(),**kwargs)


@pytest.mark.anyio
async def test_default_is_unconfigured_and_never_allocates(tmp_path,context,batch):
    runner=FakeRunner();target=adapter(tmp_path,runner=runner)
    with pytest.raises(embedding.EmbeddingError,match='embedding_admission_required'):
        await target.admit(context,batch,idempotency_key=KEY)
    assert runner.calls==0 and not list(target.root.glob('embedding-*'))


@pytest.mark.anyio
async def test_one_use_claim_is_bound_before_run_and_stays_held(tmp_path,context,batch,approval):
    runner=FakeRunner();target=adapter(tmp_path,approval,runner)
    admission=await target.admit(context,batch,idempotency_key=KEY)
    assert runner.calls==0
    receipt=target.receipt(admission)
    assert receipt['status']=='admitted' and receipt['binding']['scope']['generation']==1
    assert Path(receipt['claimed_receipt']).exists()
    assert receipt['planned_gross_usd']=='1.00' and receipt['actual_cost_usd'] is None
    assert all('path' not in key for key in admission)
    output=await target.run(admission,batch,cancelled=asyncio.Event())
    assert output['attempt_id']==admission['attempt_id'] and runner.calls==1
    receipt=target.receipt(admission)
    assert receipt['status']=='completed' and receipt['budget_status']=='held_pending_provider_evidence'
    assert receipt['actual_cost_usd'] is None
    assert not list((target.root/admission['attempt_id']).glob('*.mp4'))
    assert await target.run(admission,batch,cancelled=asyncio.Event())==output and runner.calls==1


@pytest.mark.anyio
async def test_idempotent_admission_and_changed_hash(tmp_path,context,batch,approval):
    target=adapter(tmp_path,approval)
    first=await target.admit(context,batch,idempotency_key=KEY)
    assert await target.admit(context,batch,idempotency_key=KEY)==first
    changed=[{**batch[0],'sha256':'0'*64}]
    with pytest.raises(embedding.EmbeddingError,match='embedding_binding_changed'):
        await target.admit(context,changed,idempotency_key=KEY)


@pytest.mark.anyio
async def test_second_batch_cannot_consume_same_approval(tmp_path,context,batch,approval):
    runner=FakeRunner();target=adapter(tmp_path,approval,runner)
    await target.admit(context,batch,idempotency_key=KEY)
    with pytest.raises(embedding.compute_guard.ApprovalError):
        await target.admit(context,batch,idempotency_key=KEY[:-1]+'1')
    assert runner.calls==0


@pytest.mark.anyio
async def test_approval_denial_precedes_attempt_creation(tmp_path,context,batch,approval):
    value=json.loads(approval.read_text());value['approved_run_gross_usd']='0.50';approval.write_text(json.dumps(value))
    runner=FakeRunner();target=adapter(tmp_path,approval,runner)
    with pytest.raises(embedding.compute_guard.ApprovalError): await target.admit(context,batch,idempotency_key=KEY)
    assert runner.calls==0 and not list(target.root.glob('embedding-*'))


@pytest.mark.anyio
@pytest.mark.parametrize('change',[{'id':'../other'},{'sha256':'invalid'},{'duration_seconds':8.5},
    {'size_bytes':64*1024**2+1},{'duration_seconds':float('nan')}])
async def test_invalid_batch_rejected_before_claim(tmp_path,context,batch,approval,change):
    target=adapter(tmp_path,approval)
    with pytest.raises(embedding.EmbeddingError): await target.admit(context,[{**batch[0],**change}],idempotency_key=KEY)
    assert not list(approval.parent.glob('*.claimed.json'))


@pytest.mark.anyio
async def test_mutated_bytes_are_rejected_before_remote(tmp_path,context,batch,approval):
    runner=FakeRunner();target=adapter(tmp_path,approval,runner)
    admission=await target.admit(context,batch,idempotency_key=KEY)
    with pytest.raises(embedding.EmbeddingError,match='embedding_input_mismatch'):
        await target.run(admission,[{**batch[0],'content':b'changed'}],cancelled=asyncio.Event())
    assert runner.calls==0 and target.receipt(admission)['budget_status']=='held_pending_provider_evidence'


@pytest.mark.anyio
async def test_ambiguous_remote_failure_never_retries(tmp_path,context,batch,approval):
    runner=FakeRunner();runner.failure=TimeoutError('synthetic remote timeout')
    target=adapter(tmp_path,approval,runner);admission=await target.admit(context,batch,idempotency_key=KEY)
    with pytest.raises(TimeoutError): await target.run(admission,batch,cancelled=asyncio.Event())
    assert target.receipt(admission)['status']=='needs_reconciliation'
    with pytest.raises(embedding.EmbeddingError,match='needs_reconciliation'):
        await target.run(admission,batch,cancelled=asyncio.Event())
    assert runner.calls==1
    other=adapter(tmp_path,approval,runner)
    assert await other.admit(context,batch,idempotency_key=KEY)==admission
    with pytest.raises(embedding.EmbeddingError,match='needs_reconciliation'):
        await other.run(admission,batch,cancelled=asyncio.Event())
    assert runner.calls==1


@pytest.mark.anyio
async def test_other_company_cannot_replay_receipt(tmp_path,context,batch,approval):
    target=adapter(tmp_path,approval);admission=await target.admit(context,batch,idempotency_key=KEY)
    other=embedding.SourceEmbedding(tmp_path,company_id='other',runner=FakeRunner())
    with pytest.raises(embedding.EmbeddingError,match='scope_mismatch'):
        await other.run(admission,batch,cancelled=asyncio.Event())


@pytest.mark.anyio
@pytest.mark.parametrize('field',['model','model_revision','dimensions','frames_per_clip','attempt_id','hash','vector'])
async def test_result_validation_preserves_hold(tmp_path,context,batch,approval,field):
    runner=FakeRunner()
    def corrupt(value):
        if field=='hash': value['clips'][0]['sha256']='0'*64
        elif field=='vector': value['clips'][0]['embedding']=[0.]*256
        else: value[field]='wrong'
    runner.corrupt=corrupt
    target=adapter(tmp_path,approval,runner);admission=await target.admit(context,batch,idempotency_key=KEY)
    with pytest.raises(embedding.EmbeddingError): await target.run(admission,batch,cancelled=asyncio.Event())
    assert target.receipt(admission)['status']=='needs_reconciliation'


@pytest.mark.anyio
async def test_cancel_before_launch_makes_no_provider_call(tmp_path,context,batch,approval):
    runner=FakeRunner();target=adapter(tmp_path,approval,runner)
    admission=await target.admit(context,batch,idempotency_key=KEY)
    cancelled=asyncio.Event();cancelled.set()
    with pytest.raises(embedding.EmbeddingError,match='cancelled_before_launch'):
        await target.run(admission,batch,cancelled=cancelled)
    assert runner.calls==0 and target.receipt(admission)['status']=='admitted'


def test_isolated_runtime_allowlists_environment(tmp_path,monkeypatch):
    config=tmp_path/'modal.toml';config.write_text('[fixture]\nactive=true\n');config.chmod(0o600)
    monkeypatch.setenv('OPENAI_API_KEY','synthetic');monkeypatch.setenv('AWS_SECRET_ACCESS_KEY','synthetic')
    monkeypatch.setenv('MODAL_TOKEN_SECRET','synthetic');monkeypatch.setenv('PYTHONPATH','/untrusted')
    python,environment=embedding.runtime(sys.executable,config)
    assert python==Path(sys.executable)
    assert set(environment)=={'PATH','LANG','MODAL_CONFIG_PATH','PYTHONPATH'}
    assert environment['MODAL_CONFIG_PATH']==str(config)
    assert '/untrusted' not in environment['PYTHONPATH']
    config.chmod(0o644)
    with pytest.raises(embedding.EmbeddingError): embedding.runtime(sys.executable,config)


@pytest.mark.anyio
async def test_repeated_cancellation_drains_input_copy(tmp_path,context,batch,approval,monkeypatch):
    target=adapter(tmp_path,approval);admission=await target.admit(context,batch,idempotency_key=KEY)
    entered,finish=threading.Event(),threading.Event();actual=target._stage
    def paused(*args): entered.set();finish.wait(2);actual(*args)
    monkeypatch.setattr(target,'_stage',paused)
    task=asyncio.create_task(target.run(admission,batch,cancelled=asyncio.Event()))
    await asyncio.to_thread(entered.wait,2);task.cancel();await asyncio.sleep(.01);task.cancel();await asyncio.sleep(.02)
    try: assert not task.done(),'Cancellation returned before bounded source copying had drained'
    finally:
        finish.set();await asyncio.gather(task,return_exceptions=True)
    assert not list((target.root/admission['attempt_id']).glob('*.mp4'))


@pytest.mark.anyio
async def test_child_checkpoints_app_and_call_before_waiting(tmp_path,context,batch,approval):
    target=adapter(tmp_path,approval);admission=await target.admit(context,batch,idempotency_key=KEY)
    directory,record=target._directory(admission);target._stage(directory,record,batch)
    record['status']='launching';embedding.atomic_json(directory/'attempt.json',record)
    events=[]
    workspace=SimpleNamespace(name=embedding.WORKSPACE,hydrate=lambda:None,
        members=SimpleNamespace(list=lambda:[SimpleNamespace(email=embedding.compute_guard.OWNER,role='owner')]))
    modal=SimpleNamespace(__version__=embedding.SDK_VERSION,Workspace=SimpleNamespace(from_context=lambda:workspace))
    class App:
        app_id=None
        def run(self,**kwargs):
            assert kwargs=={'name':embedding.app_name(record),'environment_name':'main'}
            return self
        def __enter__(self): self.app_id='ap-fixture';events.append('app');return self
        def __exit__(self,*args): events.append('exit')
    class Call:
        object_id='fc-fixture'
        def get(self,**kwargs):
            receipt=embedding.read_json(directory/'provider.json')
            assert receipt['app_id']=='ap-fixture' and receipt['function_call_id']==self.object_id
            assert receipt['phase']=='waiting';events.append('get')
            value=result(record)
            value['clips']=[{'path':row['id'],'sha256':row['sha256'],'embedding':row['embedding']} for row in value['clips']]
            return value
    class Worker:
        def spawn(self,clips,queries,export_text):
            receipt=embedding.read_json(directory/'provider.json')
            assert receipt['app_id']=='ap-fixture' and receipt['phase']=='spawn_intent'
            assert queries==[] and export_text is False and clips[0]['content']==batch[0]['content']
            events.append('spawn');return Call()
    def factory(modal,**kwargs):
        assert Path(record['claimed_receipt']).exists();assert kwargs=={'small_batch':True}
        events.append('factory');return App(),Worker()
    def stop(modal,directory,record):
        embedding.provider_update(directory,record,stop_confirmed=True);events.append('stop')
    embedding.child_run(directory,modal_module=modal,worker_factory=factory,stop_function=stop)
    assert events==['factory','app','spawn','get','exit','stop']
    assert embedding.read_json(directory/'worker-result.json')['attempt_id']==admission['attempt_id']


@pytest.mark.anyio
async def test_child_changed_approval_prevents_factory(tmp_path,context,batch,approval):
    target=adapter(tmp_path,approval);admission=await target.admit(context,batch,idempotency_key=KEY)
    directory,record=target._directory(admission);target._stage(directory,record,batch)
    record['status']='launching';embedding.atomic_json(directory/'attempt.json',record)
    value=json.loads(approval.read_text());value['credit_remaining_usd']='19';approval.write_text(json.dumps(value))
    def factory(*args,**kwargs): raise AssertionError('allocation was reached')
    with pytest.raises(embedding.EmbeddingError,match='approval_changed'):
        embedding.child_run(directory,worker_factory=factory)


@pytest.mark.anyio
async def test_small_profile_cannot_expand_approved_attempt(tmp_path,context,batch,approval):
    target=adapter(tmp_path,approval)
    expanded=[{**batch[0],'id':context['asset']['id']+f'-clip-{i:06d}'} for i in range(49)]
    with pytest.raises(embedding.EmbeddingError,match='batch_limit'):
        await target.admit(context,expanded,idempotency_key=KEY)
    assert not list(approval.parent.glob('*.claimed.json'))


def fake_stop_sdk(monkeypatch,record,*,wrong_app=False,tasks_live=False,cancel_error=False):
    events=[]
    class Stub:
        async def AppGetInfo(self,request):
            events.append('info')
            return SimpleNamespace(info=SimpleNamespace(description='other-app' if wrong_app else embedding.app_name(record),
                environment_name='main',lifecycle=SimpleNamespace(app_state=2 if 'stop' in events else 1)))
        async def AppStop(self,request): events.append('stop')
        async def TaskList(self,request):
            events.append('tasks');return SimpleNamespace(tasks=[object()] if tasks_live else [])
    class Client:
        @classmethod
        async def from_env(cls): return SimpleNamespace(_stub=Stub())
    pb=SimpleNamespace(APP_STATE_STOPPED=2,AppGetInfoRequest=lambda **kwargs:kwargs,
                       AppStopRequest=lambda **kwargs:kwargs,TaskListRequest=lambda **kwargs:kwargs)
    monkeypatch.setitem(sys.modules,'modal.client',SimpleNamespace(_Client=Client))
    monkeypatch.setitem(sys.modules,'modal._utils.async_utils',SimpleNamespace(
        synchronizer=SimpleNamespace(create_blocking=lambda function:lambda:asyncio.run(function()))))
    monkeypatch.setitem(sys.modules,'modal_proto',SimpleNamespace(api_pb2=pb))
    async def cancel(**kwargs):
        events.append('cancel')
        if cancel_error: raise RuntimeError('completed call')
    workspace=SimpleNamespace(name=embedding.WORKSPACE,hydrate=lambda:None,
        members=SimpleNamespace(list=lambda:[SimpleNamespace(email=embedding.compute_guard.OWNER,role='owner')]))
    modal=SimpleNamespace(Workspace=SimpleNamespace(from_context=lambda:workspace),
        FunctionCall=SimpleNamespace(from_id=lambda call_id:SimpleNamespace(cancel=SimpleNamespace(aio=cancel))))
    return modal,events


def stop_fixture(tmp_path):
    directory=embedding.private_directory(tmp_path/'attempt')
    record={'attempt_id':'embedding-'+'a'*64,'binding_sha256':'b'*64}
    embedding.provider_update(directory,record,app_id='ap-fixture',function_call_id='fc-fixture')
    return directory,record


def test_stop_rejects_another_app_before_any_cancel(tmp_path,monkeypatch):
    directory,record=stop_fixture(tmp_path)
    modal,events=fake_stop_sdk(monkeypatch,record,wrong_app=True)
    with pytest.raises(embedding.EmbeddingError,match='app_scope_mismatch'):
        embedding.stop_own_app(modal,directory,record)
    assert events==['info']
    assert embedding.read_json(directory/'provider.json')['stop_confirmed'] is False


def test_stop_verifies_absence_even_when_call_already_completed(tmp_path,monkeypatch):
    directory,record=stop_fixture(tmp_path)
    modal,events=fake_stop_sdk(monkeypatch,record,cancel_error=True)
    embedding.stop_own_app(modal,directory,record)
    assert events==['info','cancel','stop','info','tasks']
    assert embedding.read_json(directory/'provider.json')['stop_confirmed'] is True


def test_stop_remains_uncertain_while_app_has_tasks(tmp_path,monkeypatch):
    directory,record=stop_fixture(tmp_path)
    modal,events=fake_stop_sdk(monkeypatch,record,tasks_live=True)
    with pytest.raises(embedding.EmbeddingError,match='stop_unconfirmed'):
        embedding.stop_own_app(modal,directory,record,timeout_seconds=0)
    assert 'stop' in events and embedding.read_json(directory/'provider.json')['stop_confirmed'] is False


@pytest.mark.anyio
async def test_preparation_receipt_is_part_of_immutable_admission(tmp_path,context,batch,approval):
    target=adapter(tmp_path,approval)
    admission=await target.admit(context,batch,idempotency_key=KEY)
    receipt=target.receipt(admission)
    assert receipt['binding']['original_sha256']==context['asset']['sha256']
    assert receipt['binding']['preparation']==context['completed_stages']['prepare']
    context['completed_stages']['prepare']['manifest_sha256']='f'*64
    with pytest.raises(embedding.EmbeddingError,match='binding_changed'):
        await target.admit(context,batch,idempotency_key=KEY)


@pytest.mark.anyio
async def test_subprocess_cancel_during_launch_reaps_before_return(tmp_path,context,batch,approval,monkeypatch):
    target=adapter(tmp_path,approval);admission=await target.admit(context,batch,idempotency_key=KEY)
    directory,record=target._directory(admission)
    started,allow_return,reaped=asyncio.Event(),asyncio.Event(),asyncio.Event()
    signals=[]
    class Process:
        pid=12345;returncode=None
        async def wait(self): await reaped.wait();return self.returncode
    process=Process()
    async def create(*args,**kwargs):
        assert kwargs['start_new_session'] and kwargs['stdout']==asyncio.subprocess.DEVNULL
        started.set();await allow_return.wait();return process
    def kill(pid,signum):
        signals.append((pid,signum));process.returncode=-15;reaped.set()
    monkeypatch.setattr(embedding,'runtime',lambda *args:(Path(sys.executable),{'PATH':'/usr/bin'}))
    monkeypatch.setattr(embedding.asyncio,'create_subprocess_exec',create)
    monkeypatch.setattr(embedding.os,'killpg',kill)
    runner=embedding.SubprocessRunner()
    stopped=[]
    async def stop(*args): stopped.append(True)
    monkeypatch.setattr(runner,'_stop',stop)
    task=asyncio.create_task(runner.run(directory,record,cancelled=asyncio.Event()))
    await started.wait();task.cancel();await asyncio.sleep(.01);task.cancel();await asyncio.sleep(.01)
    assert not task.done()
    allow_return.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert reaped.is_set() and signals==[(12345,embedding.signal.SIGTERM)] and stopped==[True]


@pytest.mark.anyio
async def test_subprocess_result_requires_confirmed_provider_stop(tmp_path,context,batch,approval,monkeypatch):
    target=adapter(tmp_path,approval);admission=await target.admit(context,batch,idempotency_key=KEY)
    directory,record=target._directory(admission)
    embedding.atomic_json(directory/'worker-result.json',result(record))
    embedding.provider_update(directory,record,app_id='ap-fixture',stop_confirmed=False)
    class Process:
        pid=12345;returncode=0
        async def wait(self): return 0
    async def create(*args,**kwargs): return Process()
    monkeypatch.setattr(embedding,'runtime',lambda *args:(Path(sys.executable),{'PATH':'/usr/bin'}))
    monkeypatch.setattr(embedding.asyncio,'create_subprocess_exec',create)
    runner=embedding.SubprocessRunner()
    async def stop(*args): return None
    monkeypatch.setattr(runner,'_stop',stop)
    with pytest.raises(embedding.EmbeddingError,match='provider_stop_unconfirmed'):
        await runner.run(directory,record,cancelled=asyncio.Event())


@pytest.mark.anyio
async def test_stop_helper_exit_race_still_reaps(tmp_path,monkeypatch):
    events=[]
    class Process:
        pid=12345;returncode=None
        async def wait(self): events.append('reaped');return self.returncode
    process=Process()
    async def create(*args,**kwargs): return process
    async def timeout(awaitable,*args,**kwargs):
        awaitable.close();raise TimeoutError()
    def kill(*args):
        process.returncode=0;events.append('already_exited');raise ProcessLookupError()
    monkeypatch.setattr(embedding.asyncio,'create_subprocess_exec',create)
    monkeypatch.setattr(embedding.asyncio,'wait_for',timeout)
    monkeypatch.setattr(embedding.os,'killpg',kill)
    await embedding.SubprocessRunner()._stop(tmp_path,Path(sys.executable),{})
    assert events==['already_exited','reaped']
