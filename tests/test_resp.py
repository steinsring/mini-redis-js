import asyncio
import unittest

from mini_redis import RespError, RespProtocolError, RespSimple, encode, read_value


class RespTests(unittest.IsolatedAsyncioTestCase):
    async def decode(self, frame):
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()
        return await read_value(reader)

    async def test_supported_types_roundtrip(self):
        for value in (RespSimple("OK"), RespError("ERR"), b"", b"\x00\xff\r\n",
                      None, [], [[b"a", [1, None]], [], b"\x00\xff"]):
            with self.subTest(value=value):
                self.assertEqual(await self.decode(encode(value)), value)

    async def test_signed_integer_boundaries(self):
        for value in (-(2**63), -1, 0, 2**63 - 1):
            self.assertEqual(await self.decode(encode(value)), value)
        for value in (-(2**63) - 1, 2**63):
            with self.subTest(value=value):
                with self.assertRaises(RespProtocolError):
                    await self.decode(f":{value}\r\n".encode())
                with self.assertRaises(ValueError):
                    encode(value)

    async def test_excessive_nesting_is_protocol_error(self):
        with self.assertRaises(RespProtocolError):
            await self.decode(b"*1\r\n" * 129 + b"$1\r\nx\r\n")

    async def test_malformed_and_incomplete_frames(self):
        for frame in (b"?x\r\n", b":x\r\n", b"$-2\r\n", b"*x\r\n", b"*1\r\n",
                      b"$4\r\nabc\r\n", b"$1\r\naXX", b"+OK\n", b"+\xff\r\n"):
            with self.subTest(frame=frame):
                with self.assertRaises(RespProtocolError):
                    await self.decode(frame)

    async def test_empty_stream_is_eof(self):
        with self.assertRaises(EOFError):
            await self.decode(b"")

    async def test_wire_format_and_line_safety(self):
        self.assertEqual(encode(b"\x00\xff"), b"$2\r\n\x00\xff\r\n")
        self.assertEqual(encode(-42), b":-42\r\n")
        self.assertEqual(encode(None), b"$-1\r\n")
        self.assertEqual(encode(RespError("ERR\r\n+OK")), b"-ERR  +OK\r\n")
