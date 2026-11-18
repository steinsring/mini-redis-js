"""메모리 한도 정책(noeviction / allkeys-lru + 디스크 스왑)과 실패 시 데이터 일관성 테스트."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from db import POLICY_LRU, Database, OutOfMemory
from persistence import SwapStore
from tests.helpers import FakeClock, ServerTestCase


class NoEvictionTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(maxmemory=20, clock=FakeClock())

    def test_write_up_to_the_limit_is_allowed_and_one_byte_more_is_rejected(self):
        self.db.db_add(b"a", b"x" * 19)  # 정확히 20바이트
        self.assertEqual(self.db.used_memory, 20)
        with self.assertRaises(OutOfMemory):
            self.db.db_add(b"b", b"")
        self.assertIsNone(self.db.lookup_key(b"b"))
        self.assertEqual(self.db.used_memory, 20)
        self.assertEqual(len(self.db.keyspace), 1)

    def test_rejected_overwrite_keeps_old_value(self):
        entry = self.db.db_add(b"a", b"x" * 10)
        with self.assertRaises(OutOfMemory):
            self.db.db_overwrite(entry, b"y" * 20)
        self.assertEqual(self.db.lookup_key(b"a").value, b"x" * 10)
        self.assertEqual(self.db.used_memory, 11)
        self.db.db_overwrite(entry, b"y" * 19)  # 한도 안에서 커지는 것은 허용
        self.assertEqual(self.db.used_memory, 20)

    def test_deleting_frees_space_for_new_writes(self):
        self.db.db_add(b"a", b"x" * 19)
        self.db.db_delete(b"a")
        self.db.db_add(b"b", b"x" * 19)
        self.assertEqual(self.db.used_memory, 20)


class LruSwapTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = FakeClock()
        self.swap = SwapStore(Path(tmp.name) / "swap")
        self.db = Database(maxmemory=30, policy=POLICY_LRU, swap=self.swap, clock=self.clock)

    def add(self, name: bytes) -> None:
        """키 2바이트 + 값 8바이트 = 10바이트짜리 항목을 넣고 시각을 1ms 흘린다."""
        self.db.db_add(name, name * 4)
        self.clock.now += 1

    def touch(self, name: bytes) -> None:
        self.assertIsNotNone(self.db.lookup_key(name))
        self.clock.now += 1

    def test_lru_policy_requires_swap_store(self):
        with self.assertRaises(ValueError):
            Database(maxmemory=30, policy=POLICY_LRU)

    def test_least_recently_used_key_is_moved_to_disk(self):
        for name in (b"k1", b"k2", b"k3"):
            self.add(name)
        self.touch(b"k1")  # k2가 가장 오래 접근하지 않은 키가 된다
        self.touch(b"k3")
        self.add(b"k4")
        self.assertIsNone(self.db.keyspace.get(b"k2"))
        self.assertEqual(self.swap.count, 1)
        self.assertEqual(self.swap.read(b"k2"), (b"k2" * 4, None))
        self.assertEqual(self.db.used_memory, 30)
        for name in (b"k1", b"k3", b"k4"):
            self.assertIsNotNone(self.db.keyspace.get(name))

    def test_lookup_brings_swapped_key_back_and_swaps_out_another(self):
        for name in (b"k1", b"k2", b"k3", b"k4"):
            self.add(name)  # k1이 디스크로 나간다
        self.assertIsNone(self.db.keyspace.get(b"k1"))
        entry = self.db.lookup_key(b"k1")
        self.assertEqual(entry.value, b"k1" * 4)
        self.assertIsNone(self.swap.read(b"k1"))  # 디스크 사본은 지워졌다
        self.assertIsNone(self.db.keyspace.get(b"k2"))  # 대신 k2가 나갔다
        self.assertEqual(self.swap.count, 1)
        self.assertLessEqual(self.db.used_memory, 30)

    def add_k1_with_ttl_then_push_it_to_disk(self) -> int:
        deadline = self.clock.now + 1000
        self.db.set_expire(self.db.db_add(b"k1", b"k1" * 4), deadline)
        self.clock.now += 1
        for name in (b"k2", b"k3", b"k4"):
            self.add(name)
        self.assertIsNone(self.db.keyspace.get(b"k1"))
        self.assertEqual(len(self.db.expires), 0)  # 메모리에서 나간 항목은 만료 테이블에도 없다
        return deadline

    def test_swapped_key_keeps_its_ttl(self):
        deadline = self.add_k1_with_ttl_then_push_it_to_disk()
        entry = self.db.lookup_key(b"k1")
        self.assertEqual(entry.expire_at, deadline)
        self.assertIs(self.db.expires.get(b"k1"), entry)

    def test_swapped_key_that_expired_is_not_returned(self):
        self.add_k1_with_ttl_then_push_it_to_disk()
        self.clock.now += 1000
        self.assertIsNone(self.db.lookup_key(b"k1"))
        self.assertIsNone(self.swap.read(b"k1"))
        self.assertEqual(len(self.db.expires), 0)

    def test_value_larger_than_maxmemory_is_rejected_without_evicting_anything(self):
        self.add(b"k1")
        with self.assertRaises(OutOfMemory):
            self.db.db_add(b"big", b"x" * 40)
        self.assertEqual(self.swap.count, 0)
        self.assertIsNotNone(self.db.keyspace.get(b"k1"))

    def test_key_being_written_is_never_swapped_out_itself(self):
        self.add(b"k1")  # 가장 오래된 키지만 지금 쓰는 대상이다
        self.add(b"k2")
        self.db.db_overwrite(self.db.keyspace.get(b"k1"), b"x" * 20)
        self.assertEqual(self.db.keyspace.get(b"k1").value, b"x" * 20)
        self.assertIsNone(self.db.keyspace.get(b"k2"))
        self.assertEqual(self.swap.read(b"k2"), (b"k2" * 4, None))
        with self.assertRaises(OutOfMemory):  # 자기 자신 말고는 내보낼 것이 없다
            self.db.db_overwrite(self.db.keyspace.get(b"k1"), b"x" * 29)
        self.assertEqual(self.db.keyspace.get(b"k1").value, b"x" * 20)

    def test_records_include_swapped_entries(self):
        for name in (b"k1", b"k2", b"k3", b"k4", b"k5"):
            self.add(name)
        self.assertEqual(self.swap.count, 2)
        self.assertEqual(
            sorted(self.db.records()),
            sorted((name, name * 4, None) for name in (b"k1", b"k2", b"k3", b"k4", b"k5")),
        )

    def test_failed_swap_write_keeps_victim_in_memory(self):
        for name in (b"k1", b"k2", b"k3"):
            self.add(name)
        with mock.patch.object(self.swap, "put", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.db.db_add(b"k4", b"k4" * 4)
        self.assertEqual(len(self.db.keyspace), 3)
        self.assertIsNone(self.db.keyspace.get(b"k4"))
        self.assertEqual(self.db.used_memory, 30)


class ServerNoEvictionTest(ServerTestCase):
    config = {"maxmemory": 100}

    async def test_writes_over_the_limit_get_oom_error_and_reads_keep_working(self):
        self.assertEqual(await self.client.call("SET", "a", "x" * 90), "OK")
        await self.assert_error("OOM command not allowed when used memory > 'maxmemory'.", "SET", "b", "x" * 20)
        await self.assert_error("OOM", "INCR", "counter-with-a-long-name")
        await self.assert_error("OOM", "SET", "a", "x" * 100)
        self.assertEqual(await self.client.call("GET", "a"), b"x" * 90)  # 실패한 덮어쓰기는 반영되지 않았다
        self.assertIsNone(await self.client.call("GET", "b"))
        self.assertEqual(await self.client.call("DEL", "a"), 1)
        self.assertEqual(await self.client.call("SET", "b", "x" * 20), "OK")


class ServerLruTest(ServerTestCase):
    config = {"maxmemory": 1000, "maxmemory_policy": POLICY_LRU}

    async def test_all_keys_stay_readable_while_memory_stays_under_the_limit(self):
        for i in range(100):
            self.assertEqual(await self.client.call("SET", f"key:{i:03}", f"{i:03}" * 30), "OK")
            self.assertLessEqual(self.server.db.used_memory, 1000)
        self.assertGreater(self.server.db.swap.count, 0)
        for i in range(100):
            self.assertEqual(await self.client.call("GET", f"key:{i:03}"), (f"{i:03}" * 30).encode())
            self.assertLessEqual(self.server.db.used_memory, 1000)

    async def test_swapped_keys_can_be_deleted_incremented_and_overwritten(self):
        await self.client.call("SET", "n", "41")
        for i in range(50):
            await self.client.call("SET", f"key:{i:03}", "x" * 90)
        self.assertIsNone(self.server.db.keyspace.get(b"n"))
        self.assertEqual(await self.client.call("INCR", "n"), 42)
        self.assertIsNone(self.server.db.keyspace.get(b"key:000"))
        self.assertEqual(await self.client.call("DEL", "key:000"), 1)
        self.assertIsNone(await self.client.call("GET", "key:000"))
        self.assertIsNone(await self.client.call("SET", "key:001", "new", "NX"))  # 디스크에 있어도 존재하는 키
        self.assertEqual(await self.client.call("SET", "key:001", "new"), "OK")
        self.assertEqual(await self.client.call("GET", "key:001"), b"new")

    async def test_swap_failure_is_reported_and_leaves_data_unchanged(self):
        for i in range(10):
            await self.client.call("SET", f"key:{i}", "x" * 95)
        with mock.patch.object(self.server.db.swap, "put", side_effect=OSError("disk full")):
            with self.assertLogs("mini-redis", level="ERROR"):
                await self.assert_error("ERR internal error", "SET", "new", "x" * 95)
        self.assertIsNone(await self.client.call("GET", "new"))
        for i in range(10):
            self.assertEqual(await self.client.call("GET", f"key:{i}"), b"x" * 95)


class InternalErrorTest(ServerTestCase):
    async def test_unexpected_exception_becomes_error_reply_and_server_survives(self):
        await self.client.call("SET", "k", "v")
        with mock.patch.object(self.server.db, "lookup_key", side_effect=RuntimeError("boom")):
            with self.assertLogs("mini-redis", level="ERROR"):
                await self.assert_error("ERR internal error", "GET", "k")
        self.assertEqual(await self.client.call("GET", "k"), b"v")


if __name__ == "__main__":
    unittest.main()
