"""Fixed loopback-to-Unix byte relay; credentials/routing/accounting stay on host."""
import asyncio
import contextlib

MAX_CONNECTIONS = 16
active_connections = 0


async def copy_stream(reader, writer):
    while data := await asyncio.wait_for(reader.read(65536), timeout=180):
        writer.write(data)
        await writer.drain()
    with contextlib.suppress(OSError, NotImplementedError):
        writer.write_eof()


async def connection(reader, writer, socket_path):
    global active_connections
    # Refuse excess clients immediately; a semaphore alone would accumulate
    # unbounded waiting handlers inside an adversarial workspace.
    if active_connections >= MAX_CONNECTIONS:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return
    active_connections += 1
    other = None
    tasks = []
    try:
        source, other = await asyncio.wait_for(asyncio.open_unix_connection(socket_path), timeout=5)
        tasks = [asyncio.create_task(copy_stream(reader, other)),
                 asyncio.create_task(copy_stream(source, writer))]
        await asyncio.gather(*tasks)
    except (OSError, TimeoutError, ConnectionError):
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for stream in (writer, other):
            if stream:
                stream.close()
                with contextlib.suppress(OSError):
                    await stream.wait_closed()
        active_connections -= 1


async def main():
    servers = []
    for port, path in [(8765, '/run/rocoto/model.sock'), (8766, '/run/rocoto/mcp.sock')]:
        async def connected(reader, writer, selected=path):
            await connection(reader, writer, selected)
        servers.append(await asyncio.start_server(connected, '127.0.0.1', port, limit=65536))
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == '__main__':
    asyncio.run(main())
