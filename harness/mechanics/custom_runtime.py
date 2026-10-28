"""Host-commissioned custom runtime, verified against the installed broker."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from custom_execution import CustomExecution, RuntimeProfile, IMAGE
from custom_http import CustomHTTP


class CustomRuntime:
    def __init__(self, store, *, client=None, coordinator_factory=None):
        import custom_worker
        from custom_coordinator import CustomCoordinator
        from native_custom_client import CustomClient
        checksum = hashlib.sha256(Path(custom_worker.__file__).read_bytes()).hexdigest()
        self.manager = CustomExecution(store,runtime=RuntimeProfile(IMAGE,checksum))
        self.client = client or CustomClient()
        self.gateway = CustomHTTP(self.manager.company)
        self.coordinator = (coordinator_factory or CustomCoordinator)(self.manager,self.gateway,self.client)
        self._initialization = None
        self.closed = False

    async def _initialize(self):
        from native_executor import NativeScope
        async with asyncio.timeout(180):
            scope = NativeScope(self.manager.company,'runtime-readiness','custom-readiness')
            async with asyncio.timeout(10):
                value = await self.client.capabilities(scope)
            if (self.closed or value.get('schema_version')!=1 or value.get('available') is not True
                    or any(value.get(key)!=expected for key,expected in self.manager.runtime.document().items())):
                raise ValueError('Verify the commissioned custom runtime version.')
            await self.coordinator.reconcile()
            if self.closed:
                raise ValueError('Custom execution is closing.')

    async def ready(self):
        if self.closed:
            raise ValueError('Custom execution is closing.')
        initialization = self._initialization
        if (initialization is not None and initialization.done()
                and (initialization.cancelled() or initialization.exception() is not None)):
            self._initialization = None
        if self._initialization is None:
            self._initialization = asyncio.create_task(self._initialize())
            self._initialization.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._initialization)

    async def test(self,*args,**kwargs):
        await self.ready()
        return await self.coordinator.test(*args,**kwargs)

    async def invoke(self,*args,**kwargs):
        await self.ready()
        return await self.coordinator.invoke(*args,**kwargs)

    async def status(self,*args,**kwargs):
        await self.ready()
        return await self.coordinator.status(*args,**kwargs)

    async def stop(self,*args,**kwargs):
        await self.ready()
        return await self.coordinator.stop(*args,**kwargs)

    async def revoke(self,*args,**kwargs):
        # Revocation closes the store even if runtime identity is unavailable.
        return await self.coordinator.revoke(*args,**kwargs)

    async def close(self):
        from custom_coordinator import drain
        self.closed = True
        if self._initialization and not self._initialization.done():
            self._initialization.cancel()
            try:await drain(self._initialization)
            except Exception:pass
        await drain(asyncio.create_task(self.coordinator.close()))
