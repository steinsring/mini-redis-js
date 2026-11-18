"""TCP 서버 통합 테스트: 실제 소켓으로 RESP 명령을 보내 요구사항 1, 2, 4를 검증한다."""

import asyncio
import unittest

from resp import encode_command
from tests.helpers import ServerTestCase

INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)


class BasicCommandsTest(ServerTestCase):
    async def test_ping(self):
        self.assertEqual(await self.client.call("PING"), "PONG")

    async def test_set_then_get(self):
        self.assertEqual(await self.client.call("SET", "name", "Kim Min"), "OK")
        self.assertEqual(await self.client.call("GET", "name"), b"Kim Min")

    async def test_get_missing_key_returns_null(self):
        self.assertIsNone(await self.client.call("GET", "missing"))

    async def test_set_overwrites_existing_value(self):
        await self.client.call("SET", "k", "old")
        self.assertEqual(await self.client.call("SET", "k", "new"), "OK")
        self.assertEqual(await self.client.call("GET", "k"), b"new")

    async def test_del_existing_and_missing_are_distinguishable(self):
        await self.client.call("SET", "k", "v")
        self.assertEqual(await self.client.call("DEL", "k"), 1)
        self.assertIsNone(await self.client.call("GET", "k"))
        self.assertEqual(await self.client.call("DEL", "k"), 0)

    async def test_del_counts_each_existing_key_once(self):
        await self.client.call("SET", "a", "1")
        await self.client.call("SET", "b", "2")
        self.assertEqual(await self.client.call("DEL", "a", "a", "b", "missing"), 2)

    async def test_many_requests_on_one_connection(self):
        for i in range(200):
            self.assertEqual(await self.client.call("SET", f"k{i}", i), "OK")
        for i in range(200):
            self.assertEqual(await self.client.call("GET", f"k{i}"), str(i).encode())

    async def test_pipelined_requests_are_answered_in_order(self):
        self.client.writer.write(
            encode_command("SET", "k", "1") + encode_command("INCR", "k") + encode_command("GET", "k")
        )
        replies = [await self.client.read_reply() for _ in range(3)]
        self.assertEqual(replies, ["OK", 2, b"2"])

    async def test_command_split_across_tcp_segments(self):
        data = encode_command("SET", "k", "hello")
        for i in range(len(data)):
            self.client.writer.write(data[i : i + 1])
            await self.client.writer.drain()
        self.assertEqual(await self.client.read_reply(), "OK")
        self.assertEqual(await self.client.call("GET", "k"), b"hello")

    async def test_values_and_keys_are_binary_safe(self):
        value = bytes(range(256)) + b"\r\n"
        await self.client.call("SET", b"\x00k\r\n", value)
        self.assertEqual(await self.client.call("GET", b"\x00k\r\n"), value)

    async def test_empty_key_and_empty_value(self):
        await self.client.call("SET", "", "")
        self.assertEqual(await self.client.call("GET", ""), b"")

    async def test_command_name_is_case_insensitive(self):
        await self.client.call("set", "k", "v")
        self.assertEqual(await self.client.call("GeT", "k"), b"v")

    async def test_different_clients_see_the_same_key(self):
        other = await self.connect()
        await self.client.call("SET", "shared", "1")
        self.assertEqual(await other.call("GET", "shared"), b"1")
        await other.call("SET", "shared", "2")
        self.assertEqual(await self.client.call("GET", "shared"), b"2")

    async def test_empty_command_is_ignored(self):
        self.client.writer.write(b"*0\r\n" + encode_command("PING"))
        self.assertEqual(await self.client.read_reply(), "PONG")


class ErrorHandlingTest(ServerTestCase):
    async def test_unknown_command_and_wrong_arguments_are_distinguishable(self):
        await self.assert_error("ERR unknown command 'FOO'", "FOO", "a")
        await self.assert_error("ERR wrong number of arguments for 'get' command", "GET")
        await self.assert_error("ERR wrong number of arguments for 'get' command", "GET", "a", "b")
        await self.assert_error("ERR wrong number of arguments for 'set' command", "SET", "a")
        await self.assert_error("ERR wrong number of arguments for 'del' command", "DEL")

    async def test_set_syntax_errors_leave_data_unchanged(self):
        await self.client.call("SET", "k", "orig")
        for options in (["BOGUS"], ["NX", "XX"], ["EX"], ["EX", "10", "PX", "10"], ["EX", "10", "KEEPTTL"], ["IFEQ"]):
            await self.assert_error("ERR syntax error", "SET", "k", "changed", *options)
        await self.assert_error("ERR value is not an integer or out of range", "SET", "k", "changed", "EX", "abc")
        for seconds in ("0", "-1", str(INT64_MAX)):
            await self.assert_error("ERR invalid expire time in 'set' command", "SET", "k", "changed", "EX", seconds)
        self.assertEqual(await self.client.call("GET", "k"), b"orig")
        self.assertEqual(await self.client.call("TTL", "k"), -1)

    async def test_key_size_limit(self):
        self.assertEqual(await self.client.call("SET", "k" * 1024, "v"), "OK")
        await self.assert_error("ERR key exceeds size limit (max 1024 bytes)", "SET", "k" * 1025, "v")
        await self.assert_error("ERR key exceeds size limit (max 1024 bytes)", "GET", "k" * 1025)
        await self.assert_error("ERR key exceeds size limit (max 1024 bytes)", "DEL", "ok", "k" * 1025)
        self.assertIsNone(await self.client.call("GET", "ok"))

    async def test_value_at_size_limit_is_stored(self):
        value = b"v" * (1024 * 1024)
        self.assertEqual(await self.client.call("SET", "big", value), "OK")
        self.assertEqual(await self.client.call("GET", "big"), value)

    async def test_value_over_size_limit_is_a_protocol_error_and_closes_only_that_connection(self):
        other = await self.connect()
        await self.assert_error("ERR Protocol error", "SET", "big", b"v" * (1024 * 1024 + 1))
        self.assertEqual(await self.client.reader.read(), b"")  # 서버가 이 연결을 닫았다
        self.assertIsNone(await other.call("GET", "big"))
        self.assertEqual(await other.call("PING"), "PONG")

    async def test_garbage_input_gets_protocol_error_without_killing_the_server(self):
        other = await self.connect()
        self.client.writer.write(b"GARBAGE\r\n")
        with self.assertRaises(Exception) as caught:
            await self.client.read_reply()
        self.assertIn("Protocol error", str(caught.exception))
        self.assertEqual(await self.client.reader.read(), b"")
        self.assertEqual(await other.call("PING"), "PONG")

    async def test_commands_before_a_protocol_error_are_still_answered(self):
        self.client.writer.write(encode_command("SET", "k", "v") + b"!!\r\n")
        self.assertEqual(await self.client.read_reply(), "OK")
        other = await self.connect()
        self.assertEqual(await other.call("GET", "k"), b"v")

    async def test_abrupt_disconnect_does_not_affect_other_clients(self):
        other = await self.connect()
        await other.call("SET", "k", "v")
        self.client.writer.write(b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$100\r\npartial")
        await self.client.writer.drain()
        self.client.writer.transport.abort()  # FIN 없이 RST로 끊는다
        await self.eventually(lambda: self.server.active_clients == 1)
        self.assertEqual(await other.call("GET", "k"), b"v")  # 미완성 명령은 반영되지 않았다

    async def test_normal_close_releases_connection_resources(self):
        others = [await self.connect() for _ in range(5)]
        await self.eventually(lambda: self.server.active_clients == 6)
        for other in others:
            await other.close()
        await self.eventually(lambda: self.server.active_clients == 1)
        self.assertEqual(await self.client.call("PING"), "PONG")


class ExpireTest(ServerTestCase):
    async def test_set_with_ex_expires_after_ttl(self):
        await self.client.call("SET", "k", "v", "EX", "60")
        self.assertEqual(await self.client.call("TTL", "k"), 60)
        self.clock.now += 59_999
        self.assertEqual(await self.client.call("GET", "k"), b"v")
        self.clock.now += 1
        self.assertIsNone(await self.client.call("GET", "k"))
        self.assertEqual(await self.client.call("TTL", "k"), -2)

    async def test_set_with_px_and_pxat(self):
        await self.client.call("SET", "a", "v", "PX", "1500")
        await self.client.call("SET", "b", "v", "PXAT", str(self.clock.now + 2500))
        self.assertEqual(await self.client.call("PTTL", "a"), 1500)
        self.assertEqual(await self.client.call("PTTL", "b"), 2500)

    async def test_plain_set_removes_ttl_and_keepttl_keeps_it(self):
        await self.client.call("SET", "k", "v", "EX", "60")
        await self.client.call("SET", "k", "v2", "KEEPTTL")
        self.assertEqual(await self.client.call("TTL", "k"), 60)
        await self.client.call("SET", "k", "v3")
        self.assertEqual(await self.client.call("TTL", "k"), -1)
        self.clock.now += 120_000
        self.assertEqual(await self.client.call("GET", "k"), b"v3")

    async def test_expire_only_applies_to_existing_keys(self):
        self.assertEqual(await self.client.call("EXPIRE", "missing", "10"), 0)
        self.assertEqual(await self.client.call("TTL", "missing"), -2)
        await self.client.call("SET", "k", "v")
        self.assertEqual(await self.client.call("TTL", "k"), -1)
        self.assertEqual(await self.client.call("EXPIRE", "k", "10"), 1)
        self.assertEqual(await self.client.call("TTL", "k"), 10)
        self.assertEqual(await self.client.call("EXPIRE", "k", "30"), 1)  # 중복 설정은 갱신
        self.assertEqual(await self.client.call("TTL", "k"), 30)

    async def test_expire_in_the_past_leaves_the_key_to_lazy_deletion(self):
        for ttl in ("0", "-1", "-99999999999999"):
            await self.client.call("SET", "k", "v")
            self.assertEqual(await self.client.call("EXPIRE", "k", ttl), 1)
            self.assertIsNotNone(self.server.db.keyspace.get(b"k"))  # 명령이 직접 지우지 않는다
            self.assertIsNone(await self.client.call("GET", "k"))  # 조회 시점에 지연 삭제
            self.assertIsNone(self.server.db.keyspace.get(b"k"))

    async def test_expire_rejects_bad_ttl_and_keeps_key(self):
        await self.client.call("SET", "k", "v")
        await self.assert_error("ERR value is not an integer or out of range", "EXPIRE", "k", "soon")
        await self.assert_error("ERR invalid expire time in 'expire' command", "EXPIRE", "k", str(INT64_MAX))
        self.assertEqual(await self.client.call("TTL", "k"), -1)

    async def test_persist(self):
        await self.client.call("SET", "k", "v", "EX", "10")
        self.assertEqual(await self.client.call("PERSIST", "k"), 1)
        self.assertEqual(await self.client.call("TTL", "k"), -1)
        self.assertEqual(await self.client.call("PERSIST", "k"), 0)  # 수명이 없는 키
        self.assertEqual(await self.client.call("PERSIST", "missing"), 0)
        self.clock.now += 60_000
        self.assertEqual(await self.client.call("GET", "k"), b"v")

    async def test_incr_does_not_touch_ttl(self):
        await self.client.call("SET", "n", "1", "EX", "60")
        self.assertEqual(await self.client.call("INCR", "n"), 2)
        self.assertEqual(await self.client.call("TTL", "n"), 60)

    async def test_expired_key_can_be_recreated(self):
        await self.client.call("SET", "k", "v", "PX", "10")
        self.clock.now += 10
        self.assertEqual(await self.client.call("SET", "k", "again", "NX"), "OK")
        self.assertEqual(await self.client.call("TTL", "k"), -1)
        self.assertEqual(await self.client.call("DEL", "k"), 1)

    async def test_del_of_expired_key_reports_zero(self):
        await self.client.call("SET", "k", "v", "PX", "10")
        self.clock.now += 10
        self.assertEqual(await self.client.call("DEL", "k"), 0)


class AtomicOperationsTest(ServerTestCase):
    async def test_incr_and_decr_family(self):
        self.assertEqual(await self.client.call("INCR", "n"), 1)  # 없는 키는 0에서 시작
        self.assertEqual(await self.client.call("INCRBY", "n", "41"), 42)
        self.assertEqual(await self.client.call("DECR", "n"), 41)
        self.assertEqual(await self.client.call("DECRBY", "n", "50"), -9)
        self.assertEqual(await self.client.call("INCRBY", "n", "-1"), -10)
        self.assertEqual(await self.client.call("GET", "n"), b"-10")

    async def test_incr_rejects_non_integer_values_and_keeps_them(self):
        for value in ("abc", "1.5", " 12", "012", "+5", "", "1_000"):
            await self.client.call("SET", "n", value)
            await self.assert_error("ERR value is not an integer or out of range", "INCR", "n")
            self.assertEqual(await self.client.call("GET", "n"), value.encode())

    async def test_incrby_rejects_non_integer_argument(self):
        await self.assert_error("ERR value is not an integer or out of range", "INCRBY", "n", "x")
        await self.assert_error("ERR value is not an integer or out of range", "INCRBY", "n", str(INT64_MAX + 1))
        self.assertIsNone(await self.client.call("GET", "n"))

    async def test_overflow_is_rejected_and_value_is_unchanged(self):
        await self.client.call("SET", "max", str(INT64_MAX))
        await self.assert_error("ERR increment or decrement would overflow", "INCR", "max")
        self.assertEqual(await self.client.call("GET", "max"), str(INT64_MAX).encode())
        await self.client.call("SET", "min", str(INT64_MIN))
        await self.assert_error("ERR increment or decrement would overflow", "DECR", "min")
        await self.assert_error("ERR decrement would overflow", "DECRBY", "min", str(INT64_MIN))
        self.assertEqual(await self.client.call("GET", "min"), str(INT64_MIN).encode())

    async def test_set_nx_only_sets_when_absent(self):
        self.assertEqual(await self.client.call("SET", "k", "first", "NX"), "OK")
        self.assertIsNone(await self.client.call("SET", "k", "second", "NX"))
        self.assertEqual(await self.client.call("GET", "k"), b"first")

    async def test_set_xx_only_sets_when_present(self):
        self.assertIsNone(await self.client.call("SET", "k", "v", "XX"))
        self.assertIsNone(await self.client.call("GET", "k"))
        await self.client.call("SET", "k", "v")
        self.assertEqual(await self.client.call("SET", "k", "v2", "XX"), "OK")
        self.assertEqual(await self.client.call("GET", "k"), b"v2")

    async def test_set_ifeq_only_sets_when_current_value_matches(self):
        self.assertIsNone(await self.client.call("SET", "k", "new", "IFEQ", "old"))  # 없는 키
        self.assertIsNone(await self.client.call("GET", "k"))
        await self.client.call("SET", "k", "old")
        self.assertIsNone(await self.client.call("SET", "k", "new", "IFEQ", "other"))
        self.assertEqual(await self.client.call("GET", "k"), b"old")
        self.assertEqual(await self.client.call("SET", "k", "new", "IFEQ", "old"), "OK")
        self.assertEqual(await self.client.call("GET", "k"), b"new")

    async def test_failed_condition_does_not_touch_ttl(self):
        await self.client.call("SET", "k", "v", "EX", "60")
        self.assertIsNone(await self.client.call("SET", "k", "v2", "NX", "EX", "5"))
        self.assertEqual(await self.client.call("TTL", "k"), 60)

    async def test_concurrent_incr_loses_no_update(self):
        async def worker():
            client = await self.connect()
            for _ in range(100):
                await client.call("INCR", "counter")

        await asyncio.gather(*(worker() for _ in range(50)))
        self.assertEqual(await self.client.call("GET", "counter"), b"5000")

    async def test_concurrent_set_nx_has_exactly_one_winner(self):
        async def worker(i: int):
            client = await self.connect()
            return i, await client.call("SET", "lock", f"owner-{i}", "NX")

        results = await asyncio.gather(*(worker(i) for i in range(50)))
        winners = [i for i, reply in results if reply == "OK"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(await self.client.call("GET", "lock"), f"owner-{winners[0]}".encode())

    async def test_concurrent_compare_and_set_loses_no_update(self):
        await self.client.call("SET", "n", "0")

        async def worker():
            client = await self.connect()
            for _ in range(20):
                while True:
                    current = await client.call("GET", "n")
                    if await client.call("SET", "n", int(current) + 1, "IFEQ", current) == "OK":
                        break

        await asyncio.gather(*(worker() for _ in range(20)))
        self.assertEqual(await self.client.call("GET", "n"), b"400")

    async def test_concurrent_writes_never_produce_a_mixed_value(self):
        values = [bytes([65 + i]) * 5000 for i in range(10)]

        async def writer(value: bytes):
            client = await self.connect()
            for _ in range(30):
                await client.call("SET", "k", value)
                self.assertIn(await client.call("GET", "k"), values)

        await asyncio.gather(*(writer(value) for value in values))


class WaitingQueueTest(ServerTestCase):
    config = {"maxclients": 2}

    async def test_clients_over_the_limit_are_told_their_queue_position(self):
        second = await self.connect()
        self.assertEqual(await second.call("PING"), "PONG")
        third = await self.connect()
        fourth = await self.connect()
        await self.assert_error("QUEUED 1번째로 대기중입니다.", "PING", client=third)
        await self.assert_error("QUEUED 2번째로 대기중입니다.", "PING", client=fourth)
        self.assertEqual(self.server.waiting_clients, 2)

    async def test_commands_from_waiting_clients_are_not_executed(self):
        await self.connect()
        waiting = await self.connect()
        await self.assert_error("QUEUED 1번째로 대기중입니다.", "SET", "k", "v", client=waiting)
        self.assertIsNone(await self.client.call("GET", "k"))

    async def test_waiting_clients_are_served_in_arrival_order_when_a_slot_frees(self):
        second = await self.connect()
        third = await self.connect()
        fourth = await self.connect()
        await self.assert_error("QUEUED 2번째로 대기중입니다.", "PING", client=fourth)
        await second.close()
        await self.eventually(lambda: self.server.waiting_clients == 1)
        self.assertEqual(await third.call("SET", "k", "v"), "OK")  # 먼저 온 대기자가 승격
        await self.assert_error("QUEUED 1번째로 대기중입니다.", "PING", client=fourth)

    async def test_waiting_client_that_leaves_is_removed_from_the_queue(self):
        await self.connect()
        third = await self.connect()
        fourth = await self.connect()
        await self.assert_error("QUEUED 2번째로 대기중입니다.", "PING", client=fourth)
        await third.close()
        await self.eventually(lambda: self.server.waiting_clients == 1)
        await self.assert_error("QUEUED 1번째로 대기중입니다.", "PING", client=fourth)
        self.assertEqual(self.server.active_clients, 2)


if __name__ == "__main__":
    unittest.main()
