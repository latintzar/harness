"""User-approved execution gate leaves archive definitions and history intact."""
from retail_agent_inputs import RetailInputProvider
import asyncio,json,sys,time
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from background_agents import AgentScheduler
from agent_test_support import retail_agent_store as AgentStore
from retired_tools import ARCHIVE_AGENT_REASON,is_retired_archive_agent


def test_legacy_archive_cannot_refresh_screen_claim_activate_or_dispatch(tmp_path):
    class Index:
        def refresh(self):raise AssertionError('Retired archive refreshed its index')
        @property
        def clips(self):raise AssertionError('Retired archive read inputs')
    class Agent:
        children={};child_tasks={};active={}
        def _save_child(self,_):pass
        async def turn(self,*args,**kwargs):raise AssertionError('Retired archive dispatched inference')
    async def run():
        store=AgentStore(tmp_path,input_provider=RetailInputProvider(index=Index()));scheduler=AgentScheduler(store,Agent(),asyncio.Semaphore(1))
        row=store.create({'title':'Saved archive watch','instruction':'Preserve historical brief','dataset':'cafe','input_kind':'camera_footage','mode':'scheduled','interval_seconds':60},['cafe'],owner_principal_id='alice')
        with store.connect() as db:
            original=store._read(db,'agents',row['id']);original.update(status='active',next_run_at=0,expires_at=time.time()+600)
            store._save(db,'agents',original)
            saved=db.execute('SELECT body FROM agents WHERE id=?',(row['id'],)).fetchone()[0]
        await scheduler.tick()
        assert scheduler.tasks=={} and store.claim(row['id'],{}) is None
        assert store.inputs.prepare_screening(row['id'],{}) is None
        with pytest.raises(ValueError,match='list_tracker_events'):store.activate(row['id'],time.time()+500)
        with pytest.raises(ValueError,match='list_tracker_events'):await scheduler._execute({'definition':original})
        assert store.get(row['id'])['status']=='retired' and store.get(row['id'])['retirement_reason']==ARCHIVE_AGENT_REASON
        with store.connect() as db:
            assert db.execute('SELECT body FROM agents WHERE id=?',(row['id'],)).fetchone()[0]==saved
            assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==0
    asyncio.run(run())


def test_only_archive_scheduler_kind_is_retired():
    assert is_retired_archive_agent({'input_kind':'camera_footage'})
    assert not is_retired_archive_agent({'input_kind':'tracker_events','agent_kind':'action'})
    assert not is_retired_archive_agent({'input_kind':'tracker_events','agent_kind':'tracker'})
    assert not is_retired_archive_agent({'input_kind':'future_commissioned_stream'})
