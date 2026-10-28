import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
from test_isolated_turn import root_agent

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
async def test_stop_during_admission_requires_successful_broker_cleanup(tmp_path):
    agent=root_agent(tmp_path);entered=asyncio.Event();release=asyncio.Event()
    class FailedCleanupLauncher:
        company_id='company'
        enabled=True
        @asynccontextmanager
        async def lease(self,scope,run_id):
            entered.set();await release.wait()
            try:yield SimpleNamespace()
            finally:raise RuntimeError('failed_stop_fence')
    agent.isolated_launcher=FailedCleanupLauncher()
    async def drain():return [item async for item in agent._turn('Report','chat','cafe')]
    task=asyncio.create_task(drain());await entered.wait()
    stopped=asyncio.create_task(agent.control('chat','stop'))
    await asyncio.sleep(0)
    assert agent.active['chat']['cancel'].is_set()
    release.set();events=await task
    assert any(item['type']=='error' for item in events)
    # A failed broker cleanup must not be reported as a confirmed Stop.
    with pytest.raises(RuntimeError,match='confirmation'):
        await stopped


@pytest.mark.anyio
async def test_stop_ignores_ready_until_cleanup_is_confirmed(tmp_path):
    agent=root_agent(tmp_path);entered=asyncio.Event();release=asyncio.Event();cleanup=asyncio.Event();confirm=asyncio.Event()
    class SlowSuccessfulLauncher:
        company_id='company'
        enabled=True
        @asynccontextmanager
        async def lease(self,scope,run_id):
            entered.set();await release.wait()
            try:yield SimpleNamespace()
            finally:cleanup.set();await confirm.wait()
    agent.isolated_launcher=SlowSuccessfulLauncher()
    async def drain():return [item async for item in agent._turn('Report','chat','cafe')]
    task=asyncio.create_task(drain());await entered.wait()
    stopped=asyncio.create_task(agent.control('chat','stop'));await asyncio.sleep(0)
    run=agent.active['chat'];run['ready'].set();release.set();await cleanup.wait()
    await asyncio.sleep(.01)
    assert not stopped.done(), 'Turn readiness cannot confirm broker cleanup.'
    confirm.set();events=await task
    assert (await stopped)['result']['stopped'] is True
    assert events[-1]['status']=='interrupted'
    assert run['cleanup_status']=='stopped' and run['cleanup_finished'].is_set()


@pytest.mark.anyio
async def test_entry_failure_is_uncertain_until_broker_reconciliation(tmp_path):
    agent=root_agent(tmp_path);entered=asyncio.Event();release=asyncio.Event()
    class EntryFailure:
        company_id='company'
        enabled=True
        @asynccontextmanager
        async def lease(self,scope,run_id):
            entered.set();await release.wait();raise RuntimeError('failed_start_and_cleanup')
            yield
    agent.isolated_launcher=EntryFailure()
    async def drain():return [item async for item in agent._turn('Report','chat','cafe')]
    task=asyncio.create_task(drain());await entered.wait()
    stopped=asyncio.create_task(agent.control('chat','stop'));await asyncio.sleep(0)
    run=agent.active['chat'];release.set();await task
    with pytest.raises(RuntimeError,match='confirmation'):await stopped
    assert run['cleanup_status']=='uncertain'


@pytest.mark.anyio
async def test_failure_before_admission_confirms_that_no_worker_started(tmp_path):
    agent=root_agent(tmp_path)
    def invalid_input(*args):raise ValueError('invalid fixture attachment')
    agent._input=invalid_input
    stream=agent._turn('Report','chat','cafe')
    # Force isolated routing while deliberately failing before lease admission.
    agent.isolated_launcher=SimpleNamespace(enabled=True,company_id='company')
    assert (await anext(stream))['type']=='timing'
    run=agent.active['chat'];stopped=asyncio.create_task(agent.control('chat','stop'));await asyncio.sleep(0)
    events=[item async for item in stream]
    assert any(item['type']=='error' for item in events)
    assert (await stopped)['result']['stopped'] is True
    assert run['cleanup_status']=='not_started'
