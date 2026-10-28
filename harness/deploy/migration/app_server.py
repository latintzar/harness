"""Historical JSON-RPC transport for explicit saved-state migration and retained proofs.

The managed application does not launch or import this transport.
"""
import asyncio
import contextlib
import json


class AppServerConnection:
    """One reader dispatches RPC replies while tool work and controls run concurrently."""
    def __init__(self, process):
        self.process = process
        self.sequence = 0
        self.pending = {}
        self.events = asyncio.Queue()
        self.write_lock = asyncio.Lock()
        self.reader = asyncio.create_task(self._read())

    async def write(self, value, *, guard=None):
        async with self.write_lock:
            if guard is not None and not guard():
                raise RuntimeError("This native request has ended.")
            self.process.stdin.write((json.dumps(value) + "\n").encode())
            await self.process.stdin.drain()

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                pending = self.pending.get(value.get("id")) if "method" not in value else None
                if pending is not None:
                    if not pending.done():
                        if "error" in value:
                            pending.set_exception(RuntimeError(value["error"].get("message", "Agent request failed.")))
                        else:
                            pending.set_result(value.get("result", {}))
                else:
                    await self.events.put(value)
        except Exception as exc:
            await self.events.put({"connection_error": str(exc)})
        finally:
            for pending in self.pending.values():
                if not pending.done():
                    pending.set_exception(RuntimeError("The agent connection ended."))
            await self.events.put({"connection_error": "The agent connection ended."})

    async def rpc(self, method, params, *, guard=None):
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.write({"id": key, "method": method, "params": params}, guard=guard)
            return await asyncio.wait_for(future, timeout=45)
        finally:
            self.pending.pop(key, None)

    async def close(self):
        self.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.reader
        if self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=5)
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
