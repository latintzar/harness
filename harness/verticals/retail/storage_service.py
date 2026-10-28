"""Explicit per-company SFTP polling worker; media preparation runs separately."""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
import os
from pathlib import Path
import signal
import time

from custom_connectors import CustomConnectors
from sftp_intake import SFTPAcquirer, offload, drain
from source_embedding import atomic_json
from source_intake import IntakeActor, SourceStore
from source_service import lock, require
from storage_connections import StorageConnections, StorageSnapshot
from storage_runtime import StorageRuntime, Configuration, HEARTBEAT_TTL


class StorageService:
    def __init__(self,runtime,*,acquirer=None,tick_seconds=1):
        self.runtime=runtime;self.authority=runtime.authority
        self.config=Configuration.load(runtime.state)
        require(self.config and self.config.enabled,'storage_worker_unconfigured')
        self.acquirer=acquirer or SFTPAcquirer(self.authority.sources,
            begin_upload=partial(runtime.host.begin_remote,self.authority.sources))
        self.tick_seconds=tick_seconds;self.stopping=asyncio.Event();self.active={};self.ready=False
    def configured(self):
        try:
            current=Configuration.load(self.runtime.state)
            return bool(current and current.enabled and current.sha256==self.config.sha256)
        except (OSError,ValueError):return False
    def health(self):
        now=self.runtime.clock()
        atomic_json(self.runtime.runs.root/'heartbeat.json',{'schema_version':1,
            'company_id':self.runtime.company,'config_sha256':self.config.sha256,
            'code_sha256':self.runtime.code_sha256,'ready':self.ready and not self.stopping.is_set()and self.configured(),
            'checked_at':now,'expires_at':now+HEARTBEAT_TTL})
    async def pulse(self):
        while not self.stopping.is_set():
            await offload(self.health)
            try:await asyncio.wait_for(self.stopping.wait(),5)
            except TimeoutError:pass
    async def acquire(self,row):
        fd=self.runtime.runs.lock(row['connector'])
        if fd is None:return
        status='needs_attention';error='storage_worker_interrupted';retire=True
        try:
            snapshot=StorageSnapshot(row['document'].encode());config=snapshot.config
            def current():
                return(not self.stopping.is_set()and self.configured()and self.runtime.runs.current(row)
                    and self.runtime.current(snapshot))
            if not current():return
            await offload(self.runtime.runs.update,row,'running',cleanup=False)
            require(current(),'storage_connection_changed')
            result=await self.acquirer.pull(config,actor=IntakeActor(row['owner']),
                credentials=lambda cfg:self.authority.credentials(snapshot),current=lambda cfg:current(),cancelled=self.stopping)
            status=result['status'];error=result['error'];retire=status in {'complete','needs_attention'}
        except asyncio.CancelledError:
            # SFTPAcquirer has already drained its accepted store/SSH operations.
            status='needs_attention';error='storage_worker_interrupted';raise
        except Exception:
            # Only bounded local categories reach metadata; remote diagnostics do not.
            try:
                value=await offload(self.acquirer.journal.status,config)
                error=value.get('error')or 'storage_connection_changed'
            except Exception:error='storage_connection_changed'
        finally:
            # Keep the lease while the receipt is persisted, including repeated Stop.
            try:await drain(asyncio.create_task(offload(self.runtime.runs.update,row,status,cleanup=True,error=error,retire=retire)))
            finally:os.close(fd)
    async def step(self):
        if not self.configured():self.stopping.set();return
        for key,task in tuple(self.active.items()):
            if task.done():
                if not task.cancelled():task.exception()
                self.active.pop(key,None)
        for row in await offload(self.runtime.runs.records):
            if len(self.active)>=2:break
            if row['connector']in self.active:continue
            if row['desired']!='run':
                await offload(self.runtime.runs.reconcile,row);continue
            if not row['cleanup']:
                await offload(self.runtime.runs.reconcile,row);continue
            snapshot=StorageSnapshot(row['document'].encode())
            if not self.runtime.current(snapshot):
                await offload(self.runtime.runs.update,row,'needs_attention',cleanup=True,error='storage_connection_changed',retire=True);continue
            status=await offload(self.acquirer.journal.status,snapshot.config)
            if status['next_check']>self.runtime.clock():continue
            task=asyncio.create_task(self.acquire(row));self.active[row['connector']]=task
    async def run(self):
        # flock is nonblocking; a second worker never publishes readiness.
        with lock(self.runtime.runs.root/'service.lock',exclusive=True):
            pulse=None
            try:
                for row in await offload(self.runtime.runs.records):await offload(self.runtime.runs.reconcile,row)
                self.ready=True;await offload(self.health);pulse=asyncio.create_task(self.pulse())
                while not self.stopping.is_set():
                    await self.step()
                    try:await asyncio.wait_for(self.stopping.wait(),self.tick_seconds)
                    except TimeoutError:pass
            finally:
                self.stopping.set();self.ready=False
                for task in self.active.values():task.cancel()
                for task in self.active.values():
                    try:await drain(task)
                    except Exception:pass
                await drain(asyncio.create_task(self.acquirer.close()))
                if pulse:
                    try:await drain(pulse)
                    except Exception:pass
                await drain(asyncio.create_task(offload(self.health)))


async def main_async(state):
    state=Path(state).absolute()
    authority=StorageConnections(CustomConnectors(state),SourceStore(state))
    config=Configuration.load(state)
    require(config is not None,'storage_worker_unconfigured')
    service=StorageService(StorageRuntime(authority,access_state=config.access_state,environment=config.environment))
    loop=asyncio.get_running_loop()
    for event in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(event,service.stopping.set)
    await service.run()


def main():
    parser=argparse.ArgumentParser(description='Run the explicitly configured SFTP acquisition worker.')
    parser.add_argument('--state',type=Path,required=True)
    args=parser.parse_args();asyncio.run(main_async(args.state))


if __name__=='__main__':main()
