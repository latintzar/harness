"""Local transport tests: byte fidelity, backpressure and bounded admission."""
import asyncio
from pathlib import Path
import tempfile
import unittest

import relay


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temporary.name) / 'fixture.sock')
        self.servers = []
        self.writers = []
        self.tasks = []
        relay.MAX_CONNECTIONS = 16
        relay.active_connections = 0

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.temporary.cleanup()

    async def listen(self, handler):
        async def captured(reader, writer):
            self.writers.append(writer)
            self.tasks.append(asyncio.current_task())
            try:
                await handler(reader, writer)
            finally:
                writer.close()
        self.servers.append(await asyncio.start_unix_server(captured, self.path))
        async def forward(reader, writer):
            self.tasks.append(asyncio.current_task())
            await relay.connection(reader, writer, self.path)
        server = await asyncio.start_server(forward, '127.0.0.1', 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def client(self, port):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        self.writers.append(writer)
        return reader, writer

    async def test_large_bidirectional_payload_and_half_close(self):
        async def echo(reader, writer):
            while data := await reader.read(8192):
                writer.write(data)
                await writer.drain()
        port = await self.listen(echo)
        reader, writer = await self.client(port)
        data = bytes(range(256)) * 8192
        async def send():
            for start in range(0, len(data), 32768):
                writer.write(data[start:start+32768])
                await writer.drain()
            writer.write_eof()
        sender = asyncio.create_task(send())
        output = await asyncio.wait_for(reader.readexactly(len(data)), 5)
        await sender
        self.assertEqual(output, data)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'')

    async def test_excess_connections_are_closed_without_waiting_queue(self):
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def wait(reader, writer):
            entered.set()
            await finish.wait()
        relay.MAX_CONNECTIONS = 1
        port = await self.listen(wait)
        _, first = await self.client(port)
        await asyncio.wait_for(entered.wait(), 2)
        second, _ = await self.client(port)
        self.assertEqual(await asyncio.wait_for(second.read(), 2), b'')
        self.assertEqual(relay.active_connections, 1)
        first.close()
        finish.set()

    async def test_absent_socket_returns_eof_without_destination_fallback(self):
        async def forward(reader, writer):
            await relay.connection(reader, writer, self.path)
        server = await asyncio.start_server(forward, '127.0.0.1', 0)
        self.servers.append(server)
        reader, _ = await self.client(server.sockets[0].getsockname()[1])
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')


if __name__ == '__main__':
    unittest.main()
