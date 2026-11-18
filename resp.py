"""RESP2 직렬화/역직렬화.

서버(명령 파싱·응답 인코딩)와 클라이언트(FastAPI 게이트웨이·테스트·벤치마크)가 같은 규칙을
쓰도록 한 모듈에 모았다.
"""

from __future__ import annotations

import asyncio

CRLF = b"\r\n"
# 헤더 줄(`*3`, `$5`)이 끝없이 이어지는 입력으로 버퍼가 자라지 않도록 막는다. Redis의 64KB와 같은 값.
MAX_HEADER_LEN = 64 * 1024
# Redis가 허용하는 multibulk 최대 개수와 같은 값.
MAX_ARGS = 1024 * 1024

OK = b"+OK\r\n"
NIL = b"$-1\r\n"

Reply = bytes | str | int | None


class ProtocolError(Exception):
    """RESP 규칙을 어긴 입력. 이후 바이트의 경계를 믿을 수 없으므로 연결을 닫아야 한다."""


class RespError(Exception):
    """서버가 보낸 오류 응답(`-ERR ...`). 클라이언트 쪽에서만 쓴다."""


def _read_header(buf: bytes | bytearray, pos: int, marker: bytes) -> tuple[int, int] | None:
    """`<marker><숫자>\\r\\n` 한 줄을 읽는다.

    Returns:
        (숫자, 다음 위치). 줄이 아직 다 도착하지 않았으면 None.

    Raises:
        ProtocolError: 타입 문자가 다르거나 숫자가 아닌 경우.
    """
    if pos >= len(buf):
        return None
    if buf[pos : pos + 1] != marker:
        raise ProtocolError(f"expected '{marker.decode()}', got '{chr(buf[pos])}'")
    end = buf.find(CRLF, pos)
    if end < 0:
        if len(buf) - pos > MAX_HEADER_LEN:
            raise ProtocolError("too big header line")
        return None
    digits = bytes(buf[pos + 1 : end])
    # int()는 공백·'+'·'_'까지 받아 주므로 숫자 형식을 직접 확인한다.
    if not (digits.isdigit() or (digits[:1] == b"-" and digits[1:].isdigit())):
        raise ProtocolError("invalid length")
    return int(digits), end + 2


def parse_command(buf: bytes | bytearray, pos: int, max_bulk: int) -> tuple[list[bytes], int] | None:
    """버퍼의 pos 위치에서 명령(bulk string 배열) 하나를 해석한다.

    스트림 상태를 따로 두지 않고 "완성된 명령이 있으면 꺼낸다"는 순수 함수로 만들어,
    네트워크 수신 버퍼와 AOF 파일 재생이 같은 파서를 쓴다.

    Args:
        buf: 수신 버퍼.
        pos: 해석을 시작할 위치.
        max_bulk: bulk string 하나의 최대 바이트 수.

    Returns:
        (인자 목록, 다음 명령의 시작 위치). 명령이 아직 다 도착하지 않았으면 None.
        `*0`처럼 빈 배열이면 인자 목록이 비어 있다.

    Raises:
        ProtocolError: RESP 형식 위반 또는 크기 제한 초과.
    """
    header = _read_header(buf, pos, b"*")
    if header is None:
        return None
    count, pos = header
    if count > MAX_ARGS:
        raise ProtocolError("invalid multibulk length")
    args: list[bytes] = []
    for _ in range(count):
        header = _read_header(buf, pos, b"$")
        if header is None:
            return None
        length, pos = header
        # 본문이 도착하기 전에 길이만 보고 거부해야 큰 요청이 메모리를 차지하지 못한다.
        if length < 0 or length > max_bulk:
            raise ProtocolError(f"invalid bulk length (max {max_bulk} bytes)")
        end = pos + length
        if len(buf) < end + 2:
            return None
        if buf[end : end + 2] != CRLF:
            raise ProtocolError("bulk string is not terminated by CRLF")
        args.append(bytes(buf[pos:end]))
        pos = end + 2
    return args, pos


def simple(text: str) -> bytes:
    return b"+" + text.encode() + CRLF


def error(message: str) -> bytes:
    return b"-" + message.encode() + CRLF


def integer(value: int) -> bytes:
    return b":%d\r\n" % value


def bulk(value: bytes | None) -> bytes:
    if value is None:
        return NIL
    return b"$%d\r\n%s\r\n" % (len(value), value)


def encode_command(*args: bytes | str | int) -> bytes:
    """명령을 bulk string 배열로 인코딩한다. AOF 기록에도 같은 형식을 쓴다."""
    parts = [b"*%d\r\n" % len(args)]
    for arg in args:
        data = arg if isinstance(arg, bytes) else str(arg).encode()
        parts.append(bulk(data))
    return b"".join(parts)


class RespClient:
    """요청 하나에 응답 하나를 받는 최소 RESP 클라이언트."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, host: str, port: int) -> RespClient:
        reader, writer = await asyncio.open_connection(host, port)
        return cls(reader, writer)

    async def call(self, *args: bytes | str | int) -> Reply:
        """명령을 보내고 응답을 돌려준다.

        Returns:
            simple string은 str, bulk string은 bytes, 정수는 int, null은 None.

        Raises:
            RespError: 서버가 오류 응답을 보낸 경우.
            ConnectionError: 응답을 받기 전에 연결이 끊긴 경우.
        """
        self.writer.write(encode_command(*args))
        await self.writer.drain()
        return await self.read_reply()

    async def read_reply(self) -> Reply:
        line = await self.reader.readline()
        if not line.endswith(CRLF):
            raise ConnectionError("connection closed by server")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RespError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            length = int(rest)
            if length < 0:
                return None
            try:
                return (await self.reader.readexactly(length + 2))[:-2]
            except asyncio.IncompleteReadError as exc:
                raise ConnectionError("connection closed by server") from exc
        raise ProtocolError(f"unexpected reply type {kind!r}")

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass  # 이미 끊긴 연결을 닫는 중이라 더 할 일이 없다.
