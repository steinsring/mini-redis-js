"""mini-Redis TCP 서버.

`asyncio.start_server()`가 연 TCP 연결마다 `_handle_client`가 실행되어, reader로 받은 바이트를
RESP2 명령으로 해석하고 writer로 응답을 돌려준다.

실행: python Redis.py --port 6379
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from commands import CommandExecutor
from db import POLICY_LRU, POLICY_NOEVICTION, Database, OutOfMemory, now_ms
from persistence import (
    FSYNC_ALWAYS,
    FSYNC_EVERYSEC,
    Aof,
    PersistenceError,
    SwapStore,
    load_rdb,
    read_aof,
    save_rdb,
)
from resp import ProtocolError, error, parse_command

log = logging.getLogger("mini-redis")

READ_CHUNK = 64 * 1024
# 프로토콜 오류 응답을 보낸 뒤 상대가 연결을 닫기를 기다리는 최대 시간(초).
PROTOCOL_ERROR_LINGER = 1.0
CRON_INTERVAL = 1.0
EXPIRE_CYCLE_EVERY = 5  # CRON_INTERVAL 단위
RDB_NAME = "dump.rdb"
AOF_NAME = "appendonly.aof"
SWAP_DIR = "swap"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 6379
    maxclients: int = 10000  # 동시에 명령을 처리해 주는 연결 수. 넘치는 연결은 대기열로 간다.
    maxmemory: int = 0  # 바이트(키+값 길이의 합). 0이면 무제한.
    maxmemory_policy: str = POLICY_NOEVICTION
    dir: Path = field(default_factory=lambda: Path("data"))
    appendonly: bool = False
    appendfsync: str = FSYNC_EVERYSEC  # AOF를 디스크에 내리는 시점. FSYNC_ALWAYS면 명령마다.
    save_interval: float = 60.0  # 변경이 있을 때 RDB를 저장하는 주기(초). 0이면 자동 저장 안 함.
    max_key_size: int = 1024
    max_value_size: int = 1024 * 1024


class RedisServer:
    def __init__(self, config: Config, clock: Callable[[], int] = now_ms) -> None:
        """
        Args:
            config: 서버 설정.
            clock: 현재 시각(ms) 함수. 테스트가 만료를 재현하려고 주입한다.
        """
        self.config = config
        self.port = config.port
        config.dir.mkdir(parents=True, exist_ok=True)
        swap = SwapStore(config.dir / SWAP_DIR) if config.maxmemory_policy == POLICY_LRU else None
        self.db = Database(config.maxmemory, config.maxmemory_policy, swap, clock)
        self.commands = CommandExecutor(self.db, config.max_key_size, self.save)
        self._last_save = time.monotonic()
        # 요청 하나가 차지할 수 있는 수신 버퍼 상한. 가장 큰 정상 요청(SET 최대 키 + 최대 값)에
        # 헤더 여유분을 더한 값이다.
        self._max_request = config.max_key_size + config.max_value_size + READ_CHUNK
        self._active: set[object] = set()
        self._waiting: deque[object] = deque()
        self._server: asyncio.Server | None = None
        self._cron_task: asyncio.Task | None = None

    @property
    def active_clients(self) -> int:
        return len(self._active)

    @property
    def waiting_clients(self) -> int:
        return len(self._waiting)

    async def start(self) -> None:
        """백업에서 데이터를 복구한 뒤 연결을 받기 시작한다.

        복구를 먼저 끝내야 클라이언트가 덜 채워진 데이터를 보지 않는다.

        Raises:
            PersistenceError: 백업 파일이 손상되어 복구할 수 없는 경우.
        """
        self._load()
        self._server = await asyncio.start_server(self._handle_client, self.config.host, self.config.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._cron_task = asyncio.create_task(self._cron())
        log.info("mini-Redis가 %s:%d 에서 대기 중", self.config.host, self.port)

    async def stop(self) -> None:
        """정상 종료: 새 연결을 막고 기존 연결을 닫는다."""
        if self._cron_task is not None:
            self._cron_task.cancel()
            try:
                await self._cron_task
            except asyncio.CancelledError:
                pass
        if self._server is not None:
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()
        # 연결을 모두 닫아 더 이상 변경이 없는 상태에서 마지막 기록을 남긴다.
        if self.commands.aof is not None:
            self.commands.aof.close()
            self.commands.aof = None
        if self.config.save_interval and self.commands.dirty:
            self.save()

    def save(self) -> None:
        """현재 데이터 전체를 RDB 파일로 쓴다. 쓰는 동안 다른 명령은 실행되지 않는다.

        Raises:
            OSError: 디스크 쓰기 실패. 이전 RDB 파일은 그대로 남는다.
        """
        save_rdb(self.config.dir / RDB_NAME, self.db.records())
        # RDB에 담긴 변경은 AOF에 남기지 않는다. RDB가 디스크에 완전히 내려간 뒤에 비워야
        # 어느 시점에 죽어도 "RDB + AOF"로 전체를 복구할 수 있다. 비우기 전에 죽으면 이미
        # RDB에 담긴 기록을 다시 재생하게 되지만, 기록이 결과 상태라서 같은 결과가 된다.
        if self.commands.aof is not None:
            self.commands.aof.truncate()
        else:
            (self.config.dir / AOF_NAME).unlink(missing_ok=True)
        self.commands.dirty = 0
        self._last_save = time.monotonic()

    def _load(self) -> None:
        """RDB를 읽고, 그 위에 AOF를 재생해 메모리를 채운다.

        RDB는 마지막 저장 시점의 전체 데이터이고 AOF는 그 뒤의 변경만 담고 있으므로
        반드시 이 순서여야 한다. 수명이 지난 키도 그대로 올리고, 삭제는 지연 삭제와
        주기적 삭제에 맡긴다.
        """
        rdb_path = self.config.dir / RDB_NAME
        aof_path = self.config.dir / AOF_NAME
        aof_exists = aof_path.exists()
        self.db.loading = True
        try:
            if rdb_path.exists():
                for key, value, expire_at in load_rdb(rdb_path):
                    entry = self.db.db_add(key, value)
                    if expire_at is not None:
                        self.db.set_expire(entry, expire_at)
            if aof_exists:
                # 재생한 만큼 dirty가 올라가, 다음 저장 주기에 RDB로 옮겨지고 AOF가 비워진다.
                for args in read_aof(aof_path):
                    reply = self.commands.execute(args)
                    if reply.startswith(b"-"):
                        raise PersistenceError(f"AOF 재생 실패: {args[0]!r} -> {reply!r}")
        except OutOfMemory as exc:
            raise PersistenceError("백업 데이터가 maxmemory에 들어가지 않는다") from exc
        finally:
            self.db.loading = False
        if self.config.appendonly:
            self.commands.aof = Aof(aof_path, self.config.appendfsync)
        elif aof_exists:
            # AOF를 끈 채로 시작했는데 이전 실행의 기록이 남아 있다. 방금 재생한 내용을 RDB로
            # 옮기고 파일을 없앤다. 남겨 두면 나중에 AOF를 다시 켰을 때 그 사이 저장된 RDB 위에
            # 옛 기록이 재생되어 값이 과거로 되돌아간다.
            self.save()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """연결 하나의 생애. 어떤 식으로 끝나든 finally에서 자원을 정리한다."""
        client = object()  # 연결을 구분하는 토큰
        if len(self._active) < self.config.maxclients:
            self._active.add(client)
        else:
            self._waiting.append(client)
        buf = bytearray()
        try:
            while data := await reader.read(READ_CHUNK):
                buf += data
                replies: list[bytes] = []
                pos = 0
                protocol_error = False
                try:
                    # 한 번에 도착한 명령(파이프라이닝)을 모두 처리하고 응답을 모아서 보낸다.
                    while (parsed := parse_command(buf, pos, self.config.max_value_size)) is not None:
                        args, pos = parsed
                        if args:
                            replies.append(self._dispatch(client, args))
                    if len(buf) - pos > self._max_request:
                        raise ProtocolError("request exceeds size limit")
                except ProtocolError as exc:
                    replies.append(error(f"ERR Protocol error: {exc}"))
                    protocol_error = True
                del buf[:pos]
                if replies:
                    writer.write(b"".join(replies))
                    await writer.drain()
                if protocol_error:
                    await self._linger(reader, writer)
                    break
        except (ConnectionError, TimeoutError) as exc:
            # 상대가 비정상적으로 끊은 경우다. 이 연결만 정리하면 되고 다른 연결에는 영향이 없다.
            log.info("연결이 비정상 종료됨: %r", exc)
        except Exception:
            log.exception("연결 처리 중 예상하지 못한 오류")
        finally:
            self._release(client)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass  # 이미 끊긴 연결이라 더 정리할 것이 없다.

    def _dispatch(self, client: object, args: list[bytes]) -> bytes:
        if client in self._active:
            return self.commands.execute(args)
        # 대기 중인 연결의 명령은 실행하지 않고 순번만 알려 준다.
        position = self._waiting.index(client) + 1
        return error(f"QUEUED {position}번째로 대기중입니다.")

    def _release(self, client: object) -> None:
        """연결이 쓰던 자리를 반납한다. 처리 자리가 비면 가장 오래 기다린 연결을 올린다."""
        if client in self._active:
            self._active.remove(client)
            if self._waiting:
                self._active.add(self._waiting.popleft())
        else:
            self._waiting.remove(client)

    async def _linger(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """프로토콜 오류 응답이 상대에게 도달하도록 쓰기만 닫고 남은 입력을 버린다.

        읽지 않은 입력이 남은 채로 소켓을 닫으면 OS가 RST를 보내 방금 쓴 오류 응답이
        상대에게 전달되지 않을 수 있다.
        """
        if writer.can_write_eof():
            writer.write_eof()
        try:
            async with asyncio.timeout(PROTOCOL_ERROR_LINGER):
                while await reader.read(READ_CHUNK):
                    pass
        except TimeoutError:
            pass  # 상대가 계속 보내고 있어도 정해진 시간 뒤에는 닫는다.

    async def _cron(self) -> None:
        """주기 작업. 한 작업의 실패가 다음 주기를 막지 않도록 주기마다 예외를 잡는다."""
        tick = 0
        while True:
            await asyncio.sleep(CRON_INTERVAL)
            tick += 1
            try:
                if self.commands.aof is not None:
                    self.commands.aof.fsync()
                if tick % EXPIRE_CYCLE_EVERY == 0:
                    self.db.active_expire_cycle()
                overdue = time.monotonic() - self._last_save >= self.config.save_interval
                if self.config.save_interval and self.commands.dirty and overdue:
                    self.save()
            except Exception:
                log.exception("주기 작업 실패")


def _parse_args() -> Config:
    defaults = Config()
    parser = argparse.ArgumentParser(description="학습용 mini-Redis 서버")
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument("--maxclients", type=int, default=defaults.maxclients)
    parser.add_argument("--maxmemory", type=int, default=defaults.maxmemory, help="바이트. 0이면 무제한")
    parser.add_argument(
        "--maxmemory-policy", choices=[POLICY_NOEVICTION, POLICY_LRU], default=defaults.maxmemory_policy
    )
    parser.add_argument("--dir", type=Path, default=defaults.dir, help="RDB/AOF/스왑 파일 디렉터리")
    parser.add_argument("--appendonly", action="store_true", help="AOF 사용")
    parser.add_argument(
        "--appendfsync",
        choices=[FSYNC_EVERYSEC, FSYNC_ALWAYS],
        default=defaults.appendfsync,
        help="AOF를 디스크에 내리는 시점: 1초마다 또는 명령마다",
    )
    parser.add_argument("--save-interval", type=float, default=defaults.save_interval, help="초. 0이면 자동 RDB 저장 안 함")
    parser.add_argument("--max-key-size", type=int, default=defaults.max_key_size)
    parser.add_argument("--max-value-size", type=int, default=defaults.max_value_size)
    return Config(**vars(parser.parse_args()))


async def _run(config: Config) -> None:
    server = RedisServer(config)
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    await stop.wait()
    log.info("종료 신호 수신, 정리 중")
    await server.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_run(_parse_args()))
    except (PersistenceError, OSError) as exc:
        # 손상된 백업이나 포트 충돌처럼 시작할 수 없는 상황은 원인을 한 줄로 알리고 실패로 끝낸다.
        log.error("서버를 시작할 수 없다: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
