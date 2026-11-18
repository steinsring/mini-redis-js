"""해시테이블과 저장소 계층(키 공간/만료 시각 테이블) 단위 테스트."""

import unittest

from db import Database, Entry, HashTable


class FakeClock:
    """테스트가 sleep 없이 만료를 재현하도록 시각을 직접 움직인다."""

    def __init__(self, now: int = 1_000_000):
        self.now = now

    def __call__(self) -> int:
        return self.now


def key(i: int) -> bytes:
    return b"key:%d" % i


class HashTableTest(unittest.TestCase):
    def test_put_get_remove(self):
        table = HashTable()
        entry = Entry(b"a", b"1")
        table.put(entry)
        self.assertIs(table.get(b"a"), entry)
        self.assertEqual(len(table), 1)
        self.assertIs(table.remove(b"a"), entry)
        self.assertIsNone(table.get(b"a"))
        self.assertEqual(len(table), 0)

    def test_missing_key(self):
        table = HashTable()
        self.assertIsNone(table.get(b"nope"))
        self.assertIsNone(table.remove(b"nope"))

    def test_put_same_key_replaces_pointer_without_duplicating(self):
        table = HashTable()
        old, new = Entry(b"a", b"1"), Entry(b"a", b"2")
        table.put(old)
        table.put(new)
        self.assertIs(table.get(b"a"), new)
        self.assertEqual(len(table), 1)

    def test_grows_and_shrinks_without_losing_entries(self):
        table = HashTable()
        for i in range(2000):
            table.put(Entry(key(i), b"v"))
        self.assertEqual(len(table), 2000)
        self.assertTrue(all(table.get(key(i)) is not None for i in range(2000)))
        for i in range(1990):
            table.remove(key(i))
        self.assertEqual(len(table), 10)
        self.assertEqual(sorted(e.key for e in table), sorted(key(i) for i in range(1990, 2000)))

    def test_scan_resumes_from_cursor_and_visits_every_entry_once_per_lap(self):
        table = HashTable()
        for i in range(300):
            table.put(Entry(key(i), b"v"))
        seen: list[bytes] = []
        cursor = 0
        while True:
            batch, cursor = table.scan(cursor, 10)
            self.assertTrue(0 < len(batch) <= 10, len(batch))
            seen.extend(e.key for e in batch)
            if cursor == 0:
                break
        self.assertEqual(sorted(seen), sorted(key(i) for i in range(300)))

    def test_scan_of_empty_table(self):
        self.assertEqual(HashTable().scan(0, 10), ([], 0))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.db = Database(clock=self.clock)

    def assert_invariants(self):
        """공통 규칙: 만료 테이블의 포인터는 키 공간에도 있고 항목에 만료 시각이 있다."""
        for entry in self.db.expires:
            self.assertIs(self.db.keyspace.get(entry.key), entry)
            self.assertIsNotNone(entry.expire_at)
        with_ttl = [e for e in self.db.keyspace if e.expire_at is not None]
        self.assertEqual(len(with_ttl), len(self.db.expires))

    def test_add_then_lookup(self):
        entry = self.db.db_add(b"k", b"v")
        self.assertIs(self.db.lookup_key(b"k"), entry)
        self.assertEqual(entry.value, b"v")
        self.assertEqual(len(self.db.expires), 0)

    def test_lookup_missing_key(self):
        self.assertIsNone(self.db.lookup_key(b"missing"))

    def test_set_expire_adds_same_pointer_to_expires_table(self):
        entry = self.db.db_add(b"k", b"v")
        self.db.set_expire(entry, self.clock.now + 1000)
        self.assertIs(self.db.expires.get(b"k"), entry)
        self.db.set_expire(entry, self.clock.now + 5000)
        self.assertEqual(len(self.db.expires), 1)
        self.assertEqual(entry.expire_at, self.clock.now + 5000)
        self.assert_invariants()

    def test_lookup_deletes_expired_key_from_both_tables(self):
        entry = self.db.db_add(b"k", b"v")
        self.db.set_expire(entry, self.clock.now + 1000)
        self.clock.now += 999
        self.assertIs(self.db.lookup_key(b"k"), entry)  # 만료 1ms 전: 살아 있음
        self.clock.now += 1
        self.assertIsNone(self.db.lookup_key(b"k"))  # 만료 시각 정각: 만료
        self.assertIsNone(self.db.keyspace.get(b"k"))
        self.assertIsNone(self.db.expires.get(b"k"))
        self.assertEqual(self.db.used_memory, 0)

    def test_overwrite_changes_value_and_keeps_expire(self):
        entry = self.db.db_add(b"k", b"old")
        self.db.set_expire(entry, self.clock.now + 1000)
        self.db.db_overwrite(entry, b"new-value")
        self.assertEqual(self.db.lookup_key(b"k").value, b"new-value")
        self.assertEqual(entry.expire_at, self.clock.now + 1000)
        self.assert_invariants()

    def test_remove_expire(self):
        entry = self.db.db_add(b"k", b"v")
        self.assertFalse(self.db.remove_expire(entry))  # 수명이 없던 키
        self.db.set_expire(entry, self.clock.now + 1000)
        self.assertTrue(self.db.remove_expire(entry))
        self.assertIsNone(entry.expire_at)
        self.assertEqual(len(self.db.expires), 0)
        self.clock.now += 10_000
        self.assertIs(self.db.lookup_key(b"k"), entry)

    def test_delete_removes_from_both_tables(self):
        entry = self.db.db_add(b"k", b"v")
        self.db.set_expire(entry, self.clock.now + 1000)
        self.assertTrue(self.db.db_delete(b"k"))
        self.assertIsNone(self.db.lookup_key(b"k"))
        self.assertEqual(len(self.db.keyspace), 0)
        self.assertEqual(len(self.db.expires), 0)
        self.assertFalse(self.db.db_delete(b"k"))  # 이미 없는 키

    def test_used_memory_tracks_key_and_value_bytes(self):
        entry = self.db.db_add(b"key", b"12345")
        self.assertEqual(self.db.used_memory, 8)
        self.db.db_overwrite(entry, b"1")
        self.assertEqual(self.db.used_memory, 4)
        self.db.db_delete(b"key")
        self.assertEqual(self.db.used_memory, 0)

    def test_expired_key_stays_until_lazy_or_periodic_deletion_removes_it(self):
        for name in (b"lazy", b"active"):
            self.db.set_expire(self.db.db_add(name, b"v"), self.clock.now + 10)
        self.clock.now += 10
        # 만료 시각이 지났어도 지연 삭제나 주기적 삭제가 돌기 전에는 그대로 남아 있다.
        self.assertEqual(len(self.db.keyspace), 2)
        self.assertEqual(len(list(self.db.records())), 2)
        self.assertIsNone(self.db.lookup_key(b"lazy"))  # 지연 삭제
        self.assertEqual(len(self.db.keyspace), 1)
        self.assertEqual(self.db.active_expire_cycle(), 1)  # 주기적 삭제
        self.assertEqual(len(self.db.keyspace), 0)

    def test_loading_mode_does_not_expire_keys(self):
        entry = self.db.db_add(b"k", b"v")
        self.db.set_expire(entry, self.clock.now - 1)
        self.db.loading = True
        self.assertIs(self.db.lookup_key(b"k"), entry)
        self.db.loading = False
        self.assertIsNone(self.db.lookup_key(b"k"))


class ActiveExpireCycleTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.db = Database(clock=self.clock)

    def add_with_ttl(self, count: int, ttl: int, prefix: bytes = b"t") -> None:
        for i in range(count):
            entry = self.db.db_add(prefix + key(i), b"v")
            self.db.set_expire(entry, self.clock.now + ttl)

    def test_keeps_sampling_while_expired_ratio_is_high(self):
        self.add_with_ttl(200, ttl=10)
        self.clock.now += 10
        self.assertEqual(self.db.active_expire_cycle(), 200)
        self.assertEqual(len(self.db.keyspace), 0)
        self.assertEqual(len(self.db.expires), 0)

    def test_stops_after_one_sample_when_nothing_expired(self):
        self.add_with_ttl(200, ttl=10_000)
        self.assertEqual(self.db.active_expire_cycle(), 0)
        self.assertEqual(len(self.db.keyspace), 200)

    def test_keys_without_ttl_are_never_touched(self):
        for i in range(50):
            self.db.db_add(key(i), b"v")
        self.clock.now += 10**9
        self.assertEqual(self.db.active_expire_cycle(), 0)
        self.assertEqual(len(self.db.keyspace), 50)

    def test_cursor_resumes_so_sparse_expired_keys_are_eventually_found(self):
        self.add_with_ttl(990, ttl=10**9, prefix=b"alive")
        self.add_with_ttl(10, ttl=10, prefix=b"dead")
        self.clock.now += 10
        first = self.db.active_expire_cycle()
        self.assertLess(first, 10)  # 만료 비율이 낮아 한 번에 전부 훑지 않는다
        for _ in range(400):
            self.db.active_expire_cycle()
        self.assertEqual(len(self.db.expires), 990)
        self.assertEqual(len(self.db.keyspace), 990)

    def test_empty_expires_table(self):
        self.assertEqual(self.db.active_expire_cycle(), 0)


if __name__ == "__main__":
    unittest.main()
