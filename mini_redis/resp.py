import asyncio
from dataclasses import dataclass
from typing import TypeAlias


class RespProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class RespSimple:
    message: str


@dataclass(frozen=True)
class RespError:
    message: str


RespValue: TypeAlias = RespSimple | RespError | int | bytes | list["RespValue"] | None


async def _read_line(reader: asyncio.StreamReader) -> bytes:
    line = await reader.readline()
    if not line.endswith(b"\r\n"):
        raise RespProtocolError("Expected CRLF")
    return line[:-2]


def _integer(value: bytes) -> int:
    digits = value[1:] if value.startswith((b"-", b"+")) else value
    if not digits.isdigit():
        raise RespProtocolError("Invalid RESP integer")
    return int(value)


async def _read_value(reader: asyncio.StreamReader, prefix: bytes, depth: int) -> RespValue:
    if depth > 128:
        raise RespProtocolError("RESP nesting limit exceeded")
    if prefix not in (b"+", b"-", b":", b"$", b"*"):
        raise RespProtocolError("Unsupported RESP prefix")
    line = await _read_line(reader)
    if prefix == b"+":
        return RespSimple(line.decode("utf-8"))
    if prefix == b"-":
        return RespError(line.decode("utf-8"))
    length = _integer(line)
    if prefix == b":":
        if not -(2**63) <= length < 2**63:
            raise RespProtocolError("RESP integer out of range")
        return length
    if length == -1:
        return None
    if length < 0:
        raise RespProtocolError("Invalid RESP length")
    if prefix == b"$":
        data = await reader.readexactly(length)
        if await reader.readexactly(2) != b"\r\n":
            raise RespProtocolError("Expected bulk string terminator")
        return data
    return [await _read_value(reader, await reader.readexactly(1), depth + 1) for _ in range(length)]


async def read_value(reader: asyncio.StreamReader) -> RespValue:
    prefix = await reader.read(1)
    if not prefix:
        raise EOFError
    try:
        return await _read_value(reader, prefix, 0)
    except RespProtocolError:
        raise
    except (asyncio.IncompleteReadError, ValueError) as error:
        raise RespProtocolError("Invalid or incomplete RESP frame") from error


def encode(value: RespValue) -> bytes:
    if isinstance(value, (RespSimple, RespError)):
        prefix = b"+" if isinstance(value, RespSimple) else b"-"
        message = value.message.replace("\r", " ").replace("\n", " ")
        return prefix + message.encode("utf-8") + b"\r\n"
    if isinstance(value, bytes):
        return f"${len(value)}\r\n".encode("ascii") + value + b"\r\n"
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise ValueError("RESP integer out of range")
        return f":{value}\r\n".encode("ascii")
    if isinstance(value, list):
        return f"*{len(value)}\r\n".encode("ascii") + b"".join(encode(item) for item in value)
    return b"$-1\r\n"

