"""FastAPI 게이트웨이 테스트: 실제 HTTP 요청이 RESP로 변환되어 Redis에 반영되는지 확인한다."""

import asyncio
import json
import unittest
import urllib.error
import urllib.parse
import urllib.request

import uvicorn

from fastapi_server import create_app
from tests.helpers import ServerTestCase


class GatewayTestCase(ServerTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.gateways: list[tuple[uvicorn.Server, asyncio.Task]] = []
        self.base = await self.start_gateway()

    async def asyncTearDown(self) -> None:
        for gateway, task in self.gateways:
            gateway.should_exit = True
            await task
        await super().asyncTearDown()

    async def start_gateway(self) -> str:
        app = create_app("127.0.0.1", self.server.port)
        gateway = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        task = asyncio.create_task(gateway.serve())
        self.gateways.append((gateway, task))
        await self.eventually(lambda: gateway.started)
        port = gateway.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def http(self, method: str, path: str, body: dict | None = None, base: str | None = None):
        """(상태 코드, JSON 본문)을 돌려준다. 이벤트 루프를 막지 않도록 스레드에서 호출한다."""

        def send():
            request = urllib.request.Request(
                (base or self.base) + urllib.parse.quote(path),
                data=None if body is None else json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read())

        return await asyncio.to_thread(send)


class GatewayCrudTest(GatewayTestCase):
    async def test_insert_read_update_delete(self):
        self.assertEqual(await self.http("PUT", "/keys/name", {"value": "Kim Min"}), (200, {"key": "name", "stored": True}))
        self.assertEqual(await self.http("GET", "/keys/name"), (200, {"key": "name", "value": "Kim Min"}))
        self.assertEqual(await self.client.call("GET", "name"), b"Kim Min")  # RESP로 직접 봐도 같은 값
        await self.http("PUT", "/keys/name", {"value": "Lee"})
        self.assertEqual(await self.http("GET", "/keys/name"), (200, {"key": "name", "value": "Lee"}))
        self.assertEqual(await self.http("DELETE", "/keys/name"), (200, {"key": "name", "deleted": True}))
        self.assertEqual((await self.http("GET", "/keys/name"))[0], 404)

    async def test_missing_key_is_404_for_read_and_delete(self):
        self.assertEqual(await self.http("GET", "/keys/missing"), (404, {"detail": "key not found"}))
        self.assertEqual(await self.http("DELETE", "/keys/missing"), (404, {"detail": "key not found"}))

    async def test_unicode_key_and_value(self):
        await self.http("PUT", "/keys/이름", {"value": "김민"})
        self.assertEqual(await self.http("GET", "/keys/이름"), (200, {"key": "이름", "value": "김민"}))
        self.assertEqual(await self.client.call("GET", "이름"), "김민".encode())

    async def test_ttl(self):
        await self.http("PUT", "/keys/session", {"value": "abc", "ttl_seconds": 60})
        self.assertEqual(await self.client.call("TTL", "session"), 60)
        self.clock.now += 60_000
        self.assertEqual((await self.http("GET", "/keys/session"))[0], 404)

    async def test_conditional_writes(self):
        self.assertEqual((await self.http("PUT", "/keys/k", {"value": "1", "mode": "xx"}))[0], 409)
        self.assertEqual((await self.http("PUT", "/keys/k", {"value": "1", "mode": "nx"}))[0], 200)
        self.assertEqual(
            await self.http("PUT", "/keys/k", {"value": "2", "mode": "nx"}), (409, {"detail": "condition not met"})
        )
        self.assertEqual((await self.http("PUT", "/keys/k", {"value": "2", "if_equals": "nope"}))[0], 409)
        self.assertEqual((await self.http("PUT", "/keys/k", {"value": "2", "if_equals": "1"}))[0], 200)
        self.assertEqual(await self.http("GET", "/keys/k"), (200, {"key": "k", "value": "2"}))

    async def test_incr(self):
        self.assertEqual(await self.http("POST", "/keys/n/incr"), (200, {"key": "n", "value": 1}))
        self.assertEqual(await self.http("POST", "/keys/n/incr", {"amount": 9}), (200, {"key": "n", "value": 10}))
        self.assertEqual(await self.http("POST", "/keys/n/incr", {"amount": -11}), (200, {"key": "n", "value": -1}))

    async def test_bad_requests(self):
        await self.http("PUT", "/keys/text", {"value": "abc"})
        status, body = await self.http("POST", "/keys/text/incr")
        self.assertEqual((status, body["detail"]), (400, "value is not an integer or out of range"))
        self.assertEqual((await self.http("PUT", "/keys/k", {}))[0], 422)  # value 누락
        self.assertEqual((await self.http("PUT", "/keys/k", {"value": "v", "ttl_seconds": 0}))[0], 422)
        self.assertEqual((await self.http("PUT", "/keys/k", {"value": "v", "mode": "nx", "if_equals": "x"}))[0], 400)
        self.assertEqual((await self.http("PUT", "/keys/" + "k" * 1025, {"value": "v"}))[0], 400)
        self.assertEqual((await self.http("GET", "/keys/k"))[0], 404)

    async def test_value_over_limit_is_413_and_gateway_recovers(self):
        status, _ = await self.http("PUT", "/keys/big", {"value": "v" * (1024 * 1024 + 1)})
        self.assertEqual(status, 413)
        self.assertEqual((await self.http("PUT", "/keys/small", {"value": "v"}))[0], 200)
        self.assertEqual((await self.http("GET", "/keys/big"))[0], 404)

    async def test_concurrent_http_requests_lose_no_update(self):
        await asyncio.gather(*(self.http("POST", "/keys/counter/incr") for _ in range(50)))
        self.assertEqual(await self.client.call("GET", "counter"), b"50")

    async def test_different_gateways_show_the_same_state(self):
        other = await self.start_gateway()
        await self.http("PUT", "/keys/shared", {"value": "from-first"})
        self.assertEqual(await self.http("GET", "/keys/shared", base=other), (200, {"key": "shared", "value": "from-first"}))
        await self.http("POST", "/keys/hits/incr")
        self.assertEqual(await self.http("POST", "/keys/hits/incr", base=other), (200, {"key": "hits", "value": 2}))

    async def test_storage_down_is_503(self):
        await self.http("PUT", "/keys/k", {"value": "v"})
        await self.server.stop()
        self.assertEqual(await self.http("GET", "/keys/k"), (503, {"detail": "storage unavailable"}))


class GatewayQueueTest(GatewayTestCase):
    config = {"maxclients": 1}

    async def test_queue_position_is_relayed_to_http_clients(self):
        # self.client가 하나뿐인 처리 자리를 차지하고 있어 게이트웨이의 연결은 대기열로 간다.
        self.assertEqual(await self.http("GET", "/keys/k"), (503, {"detail": "1번째로 대기중입니다."}))
        await self.client.close()
        await self.eventually(lambda: self.server.waiting_clients == 0)
        self.assertEqual((await self.http("PUT", "/keys/k", {"value": "v"}))[0], 200)


class GatewayOomTest(GatewayTestCase):
    config = {"maxmemory": 50}

    async def test_out_of_memory_is_507(self):
        status, body = await self.http("PUT", "/keys/k", {"value": "v" * 100})
        self.assertEqual(status, 507)
        self.assertIn("maxmemory", body["detail"])


if __name__ == "__main__":
    unittest.main()
