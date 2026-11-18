"""브라우저의 HTTP 요청을 받아 RESP 바이트스트림으로 바꿔 mini-Redis에 전달하는 게이트웨이.

실행: MINI_REDIS_PORT=6379 uvicorn fastapi_server:app --port 8000

여러 게이트웨이 인스턴스를 띄워도 상태는 모두 Redis에 있으므로, 요청이 어느 인스턴스로 가든
같은 값을 본다.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from resp import Reply, RespClient, RespError

REDIS_TIMEOUT = 5.0  # 초. Redis가 응답하지 않을 때 HTTP 요청이 무한정 매달리지 않게 한다.


class RedisConnection:
    """Redis와의 TCP 연결 하나를 유지하며, 필요할 때 다시 연결한다."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._client: RespClient | None = None
        # RESP는 응답에 요청 식별자가 없어 순서로만 짝을 맞춘다. 요청-응답 한 쌍이 끝나기 전에
        # 다른 요청이 끼어들면 응답이 뒤바뀌므로 락으로 한 번에 하나씩만 보낸다.
        self._lock = asyncio.Lock()

    async def call(self, *args: bytes | str | int) -> Reply:
        """명령 하나를 보내고 응답을 받는다.

        실패한 명령을 자동으로 다시 보내지 않는다. 응답만 유실된 경우 INCR 같은 명령이
        두 번 적용될 수 있기 때문이다. 대신 연결을 버리고 다음 요청에서 새로 연결한다.

        Raises:
            RespError: Redis가 오류로 답한 경우.
            OSError: Redis에 연결할 수 없거나 응답이 제때 오지 않은 경우.
        """
        async with self._lock:
            try:
                async with asyncio.timeout(REDIS_TIMEOUT):
                    if self._client is None:
                        self._client = await RespClient.connect(self._host, self._port)
                    return await self._client.call(*args)
            except RespError as exc:
                if str(exc).startswith("ERR Protocol error"):
                    self.reset()  # 프로토콜 오류 뒤에는 Redis가 연결을 닫는다.
                raise
            except BaseException:
                # 타임아웃·취소·연결 오류 뒤에는 이 연결에 주인 없는 응답이 남아 있을 수 있다.
                self.reset()
                raise

    def reset(self) -> None:
        if self._client is not None:
            self._client.writer.close()
            self._client = None


class SetBody(BaseModel):
    value: str
    ttl_seconds: int | None = Field(default=None, gt=0)
    mode: Literal["nx", "xx"] | None = None  # nx: 키가 없을 때만, xx: 키가 있을 때만
    if_equals: str | None = None  # 현재 값이 이 값과 같을 때만 변경


class IncrBody(BaseModel):
    amount: int = 1  # 음수면 감소


def _http_error(message: str) -> tuple[int, str]:
    """Redis 오류 응답을 HTTP 상태 코드와 안내 문구로 바꾼다."""
    code, _, detail = message.partition(" ")
    if code == "QUEUED":
        return 503, detail
    if code == "OOM":
        return 507, detail
    if message.startswith("ERR Protocol error"):
        return 413, detail  # 게이트웨이가 만드는 요청에서 프로토콜 오류는 크기 제한 초과뿐이다.
    if code == "ERR" and detail != "internal error":
        return 400, detail
    return 500, message


def create_app(redis_host: str | None = None, redis_port: int | None = None) -> FastAPI:
    """게이트웨이 앱을 만든다. 주소를 생략하면 환경변수 MINI_REDIS_HOST / MINI_REDIS_PORT를 쓴다."""
    redis = RedisConnection(
        redis_host or os.environ.get("MINI_REDIS_HOST", "127.0.0.1"),
        redis_port or int(os.environ.get("MINI_REDIS_PORT", "6379")),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        redis.reset()

    app = FastAPI(title="mini-Redis gateway", lifespan=lifespan)

    # 엔드포인트마다 try/except를 반복하지 않고, Redis 호출의 실패를 여기서 한곳에 모아 처리한다.
    @app.exception_handler(RespError)
    async def redis_replied_with_error(request: Request, exc: RespError) -> JSONResponse:
        status, detail = _http_error(str(exc))
        return JSONResponse({"detail": detail}, status_code=status)

    @app.exception_handler(OSError)
    async def redis_unreachable(request: Request, exc: OSError) -> JSONResponse:
        return JSONResponse({"detail": "storage unavailable"}, status_code=503)

    @app.get("/keys/{key}")
    async def read_key(key: str) -> dict:
        value = await redis.call("GET", key)
        if value is None:
            raise HTTPException(404, "key not found")
        try:
            return {"key": key, "value": value.decode()}
        except UnicodeDecodeError:
            # RESP로 직접 저장한 바이너리 값은 JSON 문자열로 표현할 수 없다.
            raise HTTPException(422, "value is not valid UTF-8; read it over RESP") from None

    @app.put("/keys/{key}")
    async def write_key(key: str, body: SetBody) -> dict:
        args: list[str | int] = ["SET", key, body.value]
        if body.mode is not None:
            args.append(body.mode.upper())
        if body.if_equals is not None:
            args += ["IFEQ", body.if_equals]
        if body.ttl_seconds is not None:
            args += ["EX", body.ttl_seconds]
        if await redis.call(*args) is None:
            raise HTTPException(409, "condition not met")
        return {"key": key, "stored": True}

    @app.delete("/keys/{key}")
    async def delete_key(key: str) -> dict:
        if await redis.call("DEL", key) == 0:
            raise HTTPException(404, "key not found")
        return {"key": key, "deleted": True}

    @app.post("/keys/{key}/incr")
    async def increment_key(key: str, body: IncrBody = IncrBody()) -> dict:
        return {"key": key, "value": await redis.call("INCRBY", key, body.amount)}

    return app


app = create_app()
