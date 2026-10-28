"""Runtime identity and startup/Stop never come from generated source."""
import asyncio
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from custom_connectors import CustomConnectors
from custom_runtime import CustomRuntime
from custom_execution import IMAGE, PROFILE
import custom_worker


class Coordinator:
    def __init__(self,manager,gateway,client):
        self.manager,self.gateway,self.client=manager,gateway,client
        self.reconciled=0;self.calls=[];self.closed=False
    async def reconcile(self):self.reconciled+=1
    async def invoke(self,*args,**kwargs):self.calls.append(('invoke',args,kwargs));return {'result':{}}
    async def test(self,*args,**kwargs):self.calls.append(('test',args,kwargs));return {'status':'passed'}
    async def stop(self,*args,**kwargs):self.calls.append(('stop',args,kwargs));return {'execution_status':'idle'}
    async def revoke(self,*args,**kwargs):self.calls.append(('revoke',args,kwargs));return {'active_status':'revoked'}
    async def close(self):self.closed=True;await self.gateway.close()


def expected():
    return {'schema_version':1,'available':True,'profile':PROFILE,'image_digest':IMAGE,
        'worker_sha256':hashlib.sha256(Path(custom_worker.__file__).read_bytes()).hexdigest()}


@pytest.mark.parametrize('field',['available','profile','image_digest','worker_sha256','schema_version'])
def test_mismatched_installed_identity_blocks_execution_before_reconciliation(tmp_path,field):
    class Client:
        async def capabilities(self,scope):return {**expected(),field:'incorrect'}
    async def check():
        runtime=CustomRuntime(CustomConnectors(tmp_path),client=Client(),coordinator_factory=Coordinator)
        with pytest.raises(ValueError,match='runtime version'):await runtime.invoke('connector')
        assert runtime.coordinator.reconciled==0 and not runtime.coordinator.calls
        # Owner revocation remains available even when startup verification fails.
        assert await runtime.revoke('connector')=={'active_status':'revoked'}
        await runtime.close();assert runtime.coordinator.closed
    asyncio.run(check())


def test_cancelled_readiness_waiter_leaves_shared_initialization_owned(tmp_path):
    async def check():
        entered=asyncio.Event();release=asyncio.Event();scopes=[]
        class Client:
            async def capabilities(self,scope):
                scopes.append(scope);entered.set();await release.wait();return expected()
        runtime=CustomRuntime(CustomConnectors(tmp_path),client=Client(),coordinator_factory=Coordinator)
        first=asyncio.create_task(runtime.ready());await entered.wait();first.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        release.set();await runtime.ready();await runtime.ready()
        assert len(scopes)==1 and runtime.coordinator.reconciled==1
        assert scopes[0].company_id==runtime.manager.company
        assert await runtime.invoke('connector',owner='alice',collection='shop')=={'result':{}}
        await runtime.close()
        with pytest.raises(ValueError,match='closing'):await runtime.ready()
    asyncio.run(check())


def test_shutdown_drains_pending_identity_probe_and_closes_gateway(tmp_path):
    async def check():
        entered=asyncio.Event();drained=asyncio.Event()
        class Client:
            async def capabilities(self,scope):
                entered.set()
                try:await asyncio.Event().wait()
                finally:drained.set()
        runtime=CustomRuntime(CustomConnectors(tmp_path),client=Client(),coordinator_factory=Coordinator)
        waiter=asyncio.create_task(runtime.ready());await entered.wait();await runtime.close()
        with pytest.raises(asyncio.CancelledError):await waiter
        assert drained.is_set() and runtime.coordinator.closed
    asyncio.run(check())


def test_failed_app_startup_still_closes_custom_runtime(tmp_path,monkeypatch):
    import server
    from test_skill_runtime import make_app
    calls=[]
    class FailedRuntime:
        def __init__(self,store):
            from custom_execution import CustomExecution
            self.manager=CustomExecution(store)
        async def ready(self):calls.append('ready');raise ValueError('Fixture readiness failure')
        async def close(self):calls.append('close')
    monkeypatch.setenv('ROCOTO_CUSTOM_EXECUTOR_ENABLED','1')
    import custom_runtime
    monkeypatch.setattr(custom_runtime,'CustomRuntime',FailedRuntime)
    app=make_app(tmp_path,monkeypatch)
    async def check():
        with pytest.raises(ValueError,match='Fixture readiness'):
            async with app.router.lifespan_context(app):raise AssertionError('Unexpected startup')
    asyncio.run(check());assert calls==['ready','close']


def test_failed_initialization_retries_on_a_later_request_and_shares_waiters(tmp_path):
    async def check():
        entered=asyncio.Event();release=asyncio.Event()
        class Client:
            def __init__(self):self.calls=0
            async def capabilities(self,scope):
                self.calls+=1
                if self.calls==1:
                    entered.set();await release.wait()
                    raise ValueError('Temporary broker outage')
                await asyncio.sleep(0)
                return expected()
        client=Client();runtime=CustomRuntime(CustomConnectors(tmp_path),client=client,coordinator_factory=Coordinator)
        waiters=[asyncio.create_task(runtime.ready())for _ in range(3)]
        await entered.wait();release.set()
        first=await asyncio.gather(*waiters,return_exceptions=True)
        assert all(isinstance(value,ValueError)for value in first)
        assert client.calls==1 and runtime.coordinator.reconciled==0
        # Nothing retries until another explicit request arrives.
        await asyncio.sleep(0);assert client.calls==1
        await asyncio.gather(runtime.ready(),runtime.ready())
        assert client.calls==2 and runtime.coordinator.reconciled==1
        await runtime.close()
        with pytest.raises(ValueError,match='closing'):await runtime.ready()
        assert client.calls==2
    asyncio.run(check())


def test_cancelled_initialization_can_be_verified_again_before_shutdown(tmp_path):
    async def check():
        entered=asyncio.Event()
        class Client:
            def __init__(self):self.calls=0
            async def capabilities(self,scope):
                self.calls+=1
                if self.calls==1:entered.set();await asyncio.Event().wait()
                return expected()
        client=Client();runtime=CustomRuntime(CustomConnectors(tmp_path),client=client,coordinator_factory=Coordinator)
        first=asyncio.create_task(runtime.ready());await entered.wait();runtime._initialization.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        await runtime.ready()
        assert client.calls==2 and runtime.coordinator.reconciled==1
        await runtime.close()
    asyncio.run(check())
