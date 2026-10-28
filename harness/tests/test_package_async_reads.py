"""Package hashing stays off-loop and completes before the mutation fence opens."""
import asyncio
from contextlib import suppress
import fcntl
import os
from pathlib import Path
import sys
import threading

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from native_packages import PackageError
from native_executor import NativeScope
from deploy.migration.legacy.isolated_native import IsolatedNativeLauncher
from test_native_packages import package,snapshot,make
from test_isolated_native import Broker,auth

@pytest.fixture
def anyio_backend():return 'asyncio'

async def heartbeat(beats):
    while True:beats.append(True);await asyncio.sleep(.005)

async def entered(event):
    assert await asyncio.to_thread(event.wait,2),'package read did not start'

class PackageBroker(Broker):
    async def install_packages(self,scope,lease,selection):
        self.events.append('install_packages')
        return {'plugins':{'notion@rocoto-pinned-api':{'enabled':True,'mcp_servers':{}}}}


def blocked(function,beats=None):
    started=threading.Event();release=threading.Event();seen={}
    def read(*args,**kwargs):
        started.set()
        if not release.wait(3):raise AssertionError('bounded fixture timeout')
        if beats is not None:seen['ticks']=len(beats)
        return function(*args,**kwargs)
    return read,started,release,seen


@pytest.mark.anyio
async def test_initial_selection_hashing_keeps_event_loop_responsive(tmp_path,package,snapshot,monkeypatch):
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='alice')
    beats=[];read,start,release,seen=blocked(store.snapshot,beats)
    monkeypatch.setattr(store,'snapshot',read)
    ticker=asyncio.create_task(heartbeat(beats));timer=threading.Timer(.2,release.set);timer.start()
    try:
        async with store.runtime_selection(principal_id='alice') as selected:assert selected.packages[0].id=='notion'
        assert seen['ticks']>=3,'snapshot blocked the HTTP event loop'
    finally:
        release.set();timer.cancel();ticker.cancel()
        with suppress(asyncio.CancelledError):await ticker


@pytest.mark.anyio
@pytest.mark.parametrize('worker_error',[False,True])
async def test_cancelled_initial_hash_drains_before_uninstall_acquires_flock(tmp_path,package,snapshot,monkeypatch,worker_error):
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='alice')
    real=store.snapshot
    def source(*a,**kw):
        if worker_error:raise PackageError('fixture verification failure')
        return real(*a,**kw)
    read,start,release,_=blocked(source);monkeypatch.setattr(store,'snapshot',read)
    yielded=[]
    async def selection():
        async with store.runtime_selection(principal_id='alice'):yielded.append(True)
    task=asyncio.create_task(selection());removing=None
    try:
        await entered(start)
        for _ in range(3):task.cancel();await asyncio.sleep(.01)
        removing=asyncio.create_task(store.uninstall('notion',principal_id='alice'));await asyncio.sleep(.03)
        assert not task.done() and not removing.done() and not yielded
        fd=os.open(store.root/'mutation.lock',os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:os.close(fd)
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        await removing
        assert store.list(principal_id='alice')['plugins']==[] and not yielded
    finally:
        release.set()
        await asyncio.gather(task,*([removing] if removing else []),return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('verification',[1,2])
async def test_lease_validation_hashes_keep_http_loop_responsive(tmp_path,package,snapshot,monkeypatch,verification):
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='alice')
    broker=PackageBroker(tmp_path);scope=NativeScope(store.company_scope,'alice','parent-chat')
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id=scope.company_id,enabled=True,client=broker,package_store=store)
    beats=[];calls=[];real=store.validate_snapshot;start=threading.Event();release=threading.Event();seen={}
    def validate(*a,**kw):
        calls.append(True)
        if len(calls)==verification:
            start.set()
            if not release.wait(3):raise AssertionError('bounded fixture timeout')
            seen['ticks']=len(beats)
        return real(*a,**kw)
    monkeypatch.setattr(store,'validate_snapshot',validate)
    async def tick_after_hash_starts():
        await entered(start);await heartbeat(beats)
    ticker=asyncio.create_task(tick_after_hash_starts())
    def wake():
        if start.wait(3):threading.Event().wait(.2)
        release.set()
    timer=threading.Thread(target=wake,daemon=True);timer.start()
    try:
        async with launcher.lease(scope,run_id='fixture') as session:assert session.plugin_ids=={'notion'}
        assert len(calls)==2 and seen['ticks']>=3,'lease validation blocked the HTTP event loop'
        assert broker.started==1 and broker.stopped==1
    finally:
        release.set();ticker.cancel()
        with suppress(asyncio.CancelledError):await ticker
        await asyncio.to_thread(timer.join,1)


@pytest.mark.anyio
@pytest.mark.parametrize('verification',[1,2])
async def test_cancelled_lease_validation_drains_before_unlock_and_stop(tmp_path,package,snapshot,monkeypatch,verification):
    store,_=make(tmp_path,package,snapshot);await store.install('notion',principal_id='alice')
    broker=PackageBroker(tmp_path);scope=NativeScope(store.company_scope,'alice','parent-chat')
    launcher=IsolatedNativeLauncher(auth(tmp_path),company_id=scope.company_id,enabled=True,client=broker,package_store=store)
    calls=[];real=store.validate_snapshot;read,start,release,_=blocked(real)
    def validate(*a,**kw):
        calls.append(True)
        return read(*a,**kw) if len(calls)==verification else real(*a,**kw)
    monkeypatch.setattr(store,'validate_snapshot',validate)
    yielded=[]
    async def lease():
        async with launcher.lease(scope,run_id='fixture'):yielded.append(True)
    task=asyncio.create_task(lease());removing=None
    try:
        await entered(start);task.cancel();await asyncio.sleep(.02);task.cancel()
        removing=asyncio.create_task(store.uninstall('notion',principal_id='alice'));await asyncio.sleep(.03)
        assert not task.done() and not removing.done() and not yielded
        assert broker.started==0 and broker.stopped==0
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        await removing
        assert broker.started==0 and broker.stopped==1 and not launcher._sessions
        assert store.list(principal_id='alice')['plugins']==[]
    finally:
        release.set();await asyncio.gather(task,*([removing] if removing else []),return_exceptions=True)
