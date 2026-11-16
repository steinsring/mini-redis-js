import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import cast

from .resp import RespError, RespProtocolError, RespValue, encode, read_value


@dataclass(frozen=True)
class RunningServer:
    server: asyncio.Server
    port: int

    async def serve_forever(self) -> None:
        await self.server.serve_forever()


@asynccontextmanager
async def start_server(
    handler: Callable[[RespValue], Awaitable[RespValue]],
    host: str = "127.0.0.1", port: int = 6379,
) -> AsyncGenerator[RunningServer, None]:
    """Own listener and client lifetimes; command policy belongs to the handler."""
    clients: set[asyncio.Task[None]] = set()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = cast(asyncio.Task[None], asyncio.current_task())
        clients.add(task)
        try:
            while True:
                try:
                    request = await read_value(reader)
                except EOFError:
                    break
                except RespProtocolError:
                    writer.write(encode(RespError("ERR invalid RESP2 frame")))
                    await writer.drain()
                    break
                writer.write(encode(await handler(request)))
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            clients.discard(task)

    server = await asyncio.start_server(handle_client, host, port)
    try:
        yield RunningServer(server, cast(int, server.sockets[0].getsockname()[1]))
    finally:
        server.close()
        pending = list(clients)
        for task in pending:
            _ = task.cancel()
        _ = await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()
