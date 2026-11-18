"""서버 통합 테스트 공용 도구."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from Redis import Config, RedisServer
from resp import RespClient


class FakeClock:
    """테스트가 sleep 없이 만료를 재현하도록 시각(ms)을 직접 움직인다."""

    def __init__(self, now: int = 1_700_000_000_000):
        self.now = now

    def __call__(self) -> int:
        return self.now


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    """임시 디렉터리와 가짜 시계로 서버를 띄우고, 테스트가 끝나면 모두 정리한다."""

    config: dict = {}

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = FakeClock()
        self.servers: list[RedisServer] = []
        self.clients: list[RespClient] = []
        self.server = await self.start_server(**self.config)
        self.client = await self.connect()

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        for server in self.servers:
            await server.stop()

    async def start_server(self, **overrides) -> RedisServer:
        options = {"port": 0, "dir": self.dir, "save_interval": 0} | overrides
        server = RedisServer(Config(**options), clock=self.clock)
        await server.start()
        self.servers.append(server)
        return server

    async def restart(self, graceful: bool = True, **overrides) -> None:
        """같은 디렉터리로 서버를 다시 띄운다.

        graceful=False는 충돌을 흉내 낸다: 이전 서버의 종료 절차(마지막 저장)를 밟지 않고
        디스크에 남아 있는 파일만으로 새 서버를 시작한다.
        """
        if graceful:
            await self.server.stop()
            self.servers.remove(self.server)
        self.server = await self.start_server(**(self.config | overrides))
        self.client = await self.connect()

    async def connect(self, server: RedisServer | None = None) -> RespClient:
        client = await RespClient.connect("127.0.0.1", (server or self.server).port)
        self.clients.append(client)
        return client

    async def eventually(self, condition: Callable[[], bool], timeout: float = 2.0) -> None:
        """서버가 연결 종료 같은 비동기 사건을 처리할 때까지 기다린다."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("조건이 제한 시간 안에 충족되지 않았다")
            await asyncio.sleep(0.005)

    async def assert_error(self, prefix: str, *args, client: RespClient | None = None) -> None:
        from resp import RespError

        with self.assertRaises(RespError) as caught:
            await (client or self.client).call(*args)
        self.assertTrue(str(caught.exception).startswith(prefix), str(caught.exception))
