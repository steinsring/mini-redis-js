"""Run the in-memory key/value TCP server."""

import asyncio

from mini_redis import KeyValueStore, start_server


async def main() -> None:
    store = KeyValueStore()
    async with start_server(store.execute) as server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
