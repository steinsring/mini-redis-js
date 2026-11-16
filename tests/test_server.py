import asyncio
import unittest

from mini_redis import KeyValueStore, RespError, RespSimple, encode, read_value, start_server


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipelined_commands_share_state_across_connections(self):
        store = KeyValueStore()
        async with start_server(store.execute, port=0) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            self.addCleanup(writer.close)
            requests = [[b"SET", b"k", b"\x00\xff"], [b"GET", b"k"],
                        [b"DEL", b"k", b"k"], [b"GET", b"k"]]
            writer.write(b"".join(encode(request) for request in requests))
            await writer.drain()
            for expected in (RespSimple("OK"), b"\x00\xff", 1, None):
                self.assertEqual(await asyncio.wait_for(read_value(reader), 2), expected)
            other_reader, other_writer = await asyncio.open_connection("127.0.0.1", server.port)
            self.addCleanup(other_writer.close)
            other_writer.write(encode([b"SET", b"shared", b"yes"]))
            await other_writer.drain()
            self.assertEqual(await asyncio.wait_for(read_value(other_reader), 2), RespSimple("OK"))
            writer.write(encode([b"GET", b"shared"]))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(read_value(reader), 2), b"yes")
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertEqual(await asyncio.wait_for(other_reader.read(), 2), b"")
        writer.close()
        other_writer.close()
        await writer.wait_closed()
        await other_writer.wait_closed()

    async def test_malformed_frame_closes_only_that_connection(self):
        async with start_server(KeyValueStore().execute, port=0) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"?invalid\r\n")
            await writer.drain()
            self.assertIsInstance(await asyncio.wait_for(read_value(reader), 2), RespError)
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            writer.close()
            await writer.wait_closed()
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(encode([b"GET", b"missing"]))
            await writer.drain()
            self.assertIsNone(await asyncio.wait_for(read_value(reader), 2))
            writer.close()
            await writer.wait_closed()

    async def test_transport_accepts_an_independent_handler(self):
        async def echo(request):
            return request

        async with start_server(echo, port=0) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            payload = [[b"nested", [1, None]], b"binary\xff"]
            writer.write(encode(payload))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(read_value(reader), 2), payload)
            writer.close()
            await writer.wait_closed()
