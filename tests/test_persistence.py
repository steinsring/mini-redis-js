"""RDB/AOF 백업과 재시작 복구 테스트."""

import asyncio
import contextlib
import fcntl
import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import persistence
import Redis
from db import POLICY_LRU
from persistence import FSYNC_ALWAYS, FSYNC_EVERYSEC, Aof, PersistenceError, load_rdb, read_aof, save_rdb
from resp import RespClient, encode_command
from tests.helpers import ServerTestCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECORDS = [
    (b"plain", b"value", None),
    (b"with-ttl", b"v", 1_700_000_123_456),
    (b"binary\x00\r\n", bytes(range(256)), None),
    (b"", b"", None),
]


class RdbFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "dump.rdb"

    def test_round_trip(self):
        save_rdb(self.path, RECORDS)
        self.assertEqual(load_rdb(self.path), RECORDS)

    def test_empty_dataset(self):
        save_rdb(self.path, [])
        self.assertEqual(load_rdb(self.path), [])

    def test_corrupted_or_truncated_file_is_rejected(self):
        save_rdb(self.path, RECORDS)
        good = self.path.read_bytes()
        flipped = bytearray(good)
        flipped[len(good) // 2] ^= 0xFF
        for bad in (bytes(flipped), good[:-1], good[: len(good) // 2], b"NOTANRDB" + good[8:], b""):
            self.path.write_bytes(bad)
            with self.assertRaises(PersistenceError):
                load_rdb(self.path)

    def test_failed_save_keeps_previous_file_and_leaves_no_temp_file(self):
        save_rdb(self.path, RECORDS)

        def failing_records():
            yield RECORDS[0]
            raise OSError("disk full")

        with self.assertRaises(OSError):
            save_rdb(self.path, failing_records())
        self.assertEqual(load_rdb(self.path), RECORDS)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["dump.rdb"])

    def test_save_syncs_the_new_data_before_the_rename_and_the_directory_after(self):
        save_rdb(self.path, RECORDS[:1])
        seen = []

        def observe(fd: int) -> None:
            temp_sizes = [p.stat().st_size for p in self.path.parent.iterdir() if p != self.path]
            seen.append((temp_sizes, load_rdb(self.path)))

        # os.fsync를 직접 부르면 macOS에서 장치 캐시까지 내려가지 않으므로 반드시 sync_file을 거쳐야 한다.
        with mock.patch("persistence.sync_file", side_effect=observe), mock.patch("os.fsync") as plain:
            save_rdb(self.path, RECORDS)
        plain.assert_not_called()
        final_size = self.path.stat().st_size
        # 첫 번째: 새 내용을 다 쓴 임시 파일이 있고 원래 파일은 아직 옛 내용이다. 두 번째: 이름 변경이 끝났다.
        self.assertEqual(seen, [([final_size], RECORDS[:1]), ([], RECORDS)])

    def test_failed_data_sync_keeps_previous_file_and_leaves_no_temp_file(self):
        save_rdb(self.path, RECORDS[:1])
        with mock.patch("persistence.sync_file", side_effect=OSError("io error")):
            with self.assertRaises(OSError):
                save_rdb(self.path, RECORDS)
        self.assertEqual(load_rdb(self.path), RECORDS[:1])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["dump.rdb"])

    def test_failed_directory_sync_is_raised_and_leaves_a_readable_file(self):
        save_rdb(self.path, RECORDS[:1])
        with mock.patch("persistence.sync_file", side_effect=[None, OSError("io error")]):
            with self.assertRaises(OSError):
                save_rdb(self.path, RECORDS)
        self.assertEqual(load_rdb(self.path), RECORDS)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["dump.rdb"])


class AofFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "appendonly.aof"

    def write_commands(self) -> None:
        aof = Aof(self.path)
        aof.append([b"SET", b"k", b"v\r\n"])
        aof.append([b"DEL", b"k"])
        aof.close()

    def test_appended_commands_are_read_back_in_order(self):
        self.write_commands()
        self.assertEqual(list(read_aof(self.path)), [[b"SET", b"k", b"v\r\n"], [b"DEL", b"k"]])

    def test_reopening_appends_instead_of_overwriting(self):
        self.write_commands()
        aof = Aof(self.path)
        aof.append([b"INCR", b"n"])
        aof.close()
        self.assertEqual(len(list(read_aof(self.path))), 3)

    def test_truncate_empties_the_log_and_later_appends_start_from_the_beginning(self):
        aof = Aof(self.path)
        aof.append([b"SET", b"old", b"v"])
        aof.truncate()
        self.assertEqual(self.path.stat().st_size, 0)
        aof.append([b"SET", b"new", b"v"])
        aof.close()
        self.assertEqual(list(read_aof(self.path)), [[b"SET", b"new", b"v"]])

    def test_incomplete_last_command_is_dropped_and_truncated(self):
        self.write_commands()
        complete = self.path.read_bytes()
        self.path.write_bytes(complete + encode_command("SET", "half", "written")[:-5])
        with self.assertLogs("mini-redis", level="WARNING"):
            self.assertEqual(len(list(read_aof(self.path))), 2)
        self.assertEqual(self.path.read_bytes(), complete)

    def test_garbage_in_the_middle_is_rejected(self):
        self.write_commands()
        self.path.write_bytes(self.path.read_bytes() + b"garbage\r\n" + encode_command("PING"))
        with self.assertRaises(PersistenceError):
            list(read_aof(self.path))

    def test_everysec_syncs_only_when_asked_and_only_if_something_was_appended(self):
        with mock.patch("persistence.sync_file") as sync:
            aof = Aof(self.path)  # 기본값은 everysec
            self.addCleanup(aof.close)
            aof.append([b"SET", b"k", b"v"])
            aof.append([b"SET", b"k", b"w"])
            self.assertEqual(sync.call_count, 0)
            aof.fsync()
            self.assertEqual(sync.call_count, 1)
            aof.fsync()  # 새로 쓴 것이 없으면 다시 내리지 않는다
            self.assertEqual(sync.call_count, 1)

    def test_always_syncs_each_append_after_the_command_reached_the_file(self):
        sizes_at_sync = []
        with mock.patch("persistence.sync_file", side_effect=lambda fd: sizes_at_sync.append(os.fstat(fd).st_size)):
            aof = Aof(self.path, FSYNC_ALWAYS)
            self.addCleanup(aof.close)
            aof.append([b"SET", b"k", b"v"])
            first = len(encode_command("SET", "k", "v"))
            self.assertEqual(sizes_at_sync, [first])
            aof.append([b"DEL", b"k"])
            self.assertEqual(sizes_at_sync, [first, first + len(encode_command("DEL", "k"))])
            aof.fsync()  # 이미 명령마다 내렸으므로 주기 작업이 할 일이 없다
            self.assertEqual(len(sizes_at_sync), 2)

    def test_always_sync_failure_is_raised_to_the_caller(self):
        aof = Aof(self.path, FSYNC_ALWAYS)
        self.addCleanup(aof.close)
        with mock.patch("persistence.sync_file", side_effect=OSError("io error")):
            with self.assertRaises(OSError):
                aof.append([b"SET", b"k", b"v"])

    def test_unknown_fsync_policy_is_rejected(self):
        for bad in ("none", "ALWAYS", ""):
            with self.assertRaises(ValueError):
                Aof(self.path, bad)

    def test_explicit_everysec_is_the_same_as_default(self):
        with mock.patch("persistence.sync_file") as sync:
            aof = Aof(self.path, FSYNC_EVERYSEC)
            self.addCleanup(aof.close)
            aof.append([b"SET", b"k", b"v"])
            self.assertEqual(sync.call_count, 0)

    @unittest.skipUnless(sys.platform == "darwin", "F_FULLFSYNC는 macOS에만 있다")
    def test_sync_file_flushes_the_drive_cache_on_macos(self):
        with open(self.path, "wb") as file:
            with mock.patch("fcntl.fcntl") as full, mock.patch("os.fsync") as plain:
                persistence.sync_file(file.fileno())
            full.assert_called_once_with(file.fileno(), fcntl.F_FULLFSYNC)
            plain.assert_not_called()
            persistence.sync_file(file.fileno())  # 실제 호출도 오류 없이 끝난다

    @unittest.skipIf(sys.platform == "darwin", "macOS가 아닌 곳의 동작")
    def test_sync_file_uses_fsync_elsewhere(self):
        with open(self.path, "wb") as file:
            with mock.patch("os.fsync") as plain:
                persistence.sync_file(file.fileno())
            plain.assert_called_once_with(file.fileno())


class RdbRecoveryTest(ServerTestCase):
    async def test_save_then_restart_restores_data_and_ttl(self):
        await self.client.call("SET", "plain", "value")
        await self.client.call("SET", "session", "abc", "EX", "100")
        await self.client.call("SET", "short", "gone", "EX", "10")
        self.assertEqual(await self.client.call("SAVE"), "OK")
        self.clock.now += 40_000
        await self.restart()
        self.assertEqual(len(self.server.db.keyspace), 3)  # 꺼져 있는 동안 만료된 키도 일단 올라온다
        self.assertEqual(await self.client.call("GET", "plain"), b"value")
        self.assertEqual(await self.client.call("TTL", "session"), 60)  # 만료 시각은 절대 시각으로 보존된다
        self.assertIsNone(await self.client.call("GET", "short"))  # 조회 시점에 지연 삭제된다
        self.assertEqual(len(self.server.db.keyspace), 2)

    async def test_key_expired_far_in_the_past_does_not_come_back_as_permanent(self):
        await self.client.call("SET", "k", "v")
        await self.client.call("EXPIRE", "k", "-99999999999999")
        await self.client.call("SAVE")  # 아직 지연 삭제되지 않아 스냅샷에 들어간다
        await self.restart()
        self.assertIsNone(await self.client.call("GET", "k"))

    async def test_crash_loses_only_changes_after_the_last_save(self):
        await self.client.call("SET", "saved", "1")
        await self.client.call("SAVE")
        await self.client.call("SET", "unsaved", "2")
        await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "saved"), b"1")
        self.assertIsNone(await self.client.call("GET", "unsaved"))

    async def test_graceful_shutdown_saves_when_auto_save_is_enabled(self):
        await self.restart(save_interval=3600)
        await self.client.call("SET", "k", "v")
        await self.restart(save_interval=3600)
        self.assertEqual(await self.client.call("GET", "k"), b"v")

    async def test_periodic_save_writes_rdb_without_explicit_command(self):
        with mock.patch("Redis.CRON_INTERVAL", 0.01):
            await self.restart(save_interval=0.01)
            await self.client.call("SET", "k", "v")
            await self.eventually(lambda: (self.dir / "dump.rdb").exists())
        self.assertEqual(load_rdb(self.dir / "dump.rdb"), [(b"k", b"v", None)])

    async def test_corrupted_rdb_stops_startup_instead_of_starting_empty(self):
        await self.client.call("SET", "k", "v")
        await self.client.call("SAVE")
        (self.dir / "dump.rdb").write_bytes(b"MINIRDB1 this is not a valid dump")
        with self.assertRaises(PersistenceError):
            await self.restart()

    async def test_save_failure_is_reported_as_error(self):
        with mock.patch("Redis.save_rdb", side_effect=OSError("disk full")):
            await self.assert_error("ERR save failed", "SAVE")
        self.assertEqual(await self.client.call("PING"), "PONG")

    async def test_swapped_out_keys_are_included_in_the_snapshot(self):
        await self.restart(maxmemory=500, maxmemory_policy=POLICY_LRU)
        for i in range(30):
            await self.client.call("SET", f"key:{i:02}", f"{i:02}" * 40)
        self.assertGreater(self.server.db.swap.count, 0)
        await self.client.call("SAVE")
        await self.restart(maxmemory=500, maxmemory_policy=POLICY_LRU)
        for i in range(30):
            self.assertEqual(await self.client.call("GET", f"key:{i:02}"), (f"{i:02}" * 40).encode())

    async def test_swap_files_alone_do_not_survive_a_restart(self):
        await self.restart(maxmemory=500, maxmemory_policy=POLICY_LRU)
        for i in range(30):
            await self.client.call("SET", f"key:{i:02}", "x" * 80)
        await self.restart(maxmemory=500, maxmemory_policy=POLICY_LRU)  # 백업 없이 재시작
        for i in range(30):
            self.assertIsNone(await self.client.call("GET", f"key:{i:02}"))
        self.assertEqual(self.server.db.swap.count, 0)


class AofRecoveryTest(ServerTestCase):
    config = {"appendonly": True}

    async def test_crash_recovery_replays_every_acknowledged_write(self):
        await self.client.call("SET", "a", "1")
        await self.client.call("SET", "b", "old")
        await self.client.call("SET", "b", "new")
        await self.client.call("INCRBY", "n", "10")
        await self.client.call("DECR", "n")
        await self.client.call("SET", "deleted", "x")
        await self.client.call("DEL", "deleted")
        await self.client.call("SET", "ttl", "v", "EX", "100")
        await self.client.call("SET", "kept", "v", "EX", "100")
        await self.client.call("PERSIST", "kept")
        await self.client.call("SET", "later", "v")
        await self.client.call("EXPIRE", "later", "50")
        self.clock.now += 40_000
        await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "a"), b"1")
        self.assertEqual(await self.client.call("GET", "b"), b"new")
        self.assertEqual(await self.client.call("GET", "n"), b"9")
        self.assertIsNone(await self.client.call("GET", "deleted"))
        self.assertEqual(await self.client.call("TTL", "ttl"), 60)
        self.assertEqual(await self.client.call("TTL", "kept"), -1)
        self.assertEqual(await self.client.call("TTL", "later"), 10)

    async def test_failed_or_conditional_commands_do_not_change_replayed_state(self):
        await self.client.call("SET", "k", "first", "NX")
        await self.client.call("SET", "k", "second", "NX")
        await self.client.call("SET", "k", "third", "IFEQ", "nope")
        await self.assert_error("ERR", "INCR", "k")
        await self.client.call("SET", "cas", "1")
        await self.client.call("SET", "cas", "2", "IFEQ", "1")
        await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "k"), b"first")
        self.assertEqual(await self.client.call("GET", "cas"), b"2")

    async def test_key_recreated_after_expiry_is_replayed_as_recreated(self):
        await self.client.call("SET", "n", "5", "PX", "10")
        self.clock.now += 10
        self.assertEqual(await self.client.call("INCR", "n"), 1)  # 만료 뒤 새로 만들어진 키
        await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "n"), b"1")
        self.assertEqual(await self.client.call("TTL", "n"), -1)

    async def test_key_modified_before_expiry_is_not_resurrected_by_replay(self):
        await self.client.call("SET", "n", "5", "PX", "10")
        self.assertEqual(await self.client.call("INCR", "n"), 6)
        self.clock.now += 10
        await self.restart(graceful=False)
        self.assertIsNone(await self.client.call("GET", "n"))

    async def test_actively_expired_key_stays_deleted_after_replay(self):
        await self.client.call("SET", "k", "v", "PX", "10")
        self.clock.now += 10
        self.assertEqual(self.server.db.active_expire_cycle(), 1)
        self.assertEqual(await self.client.call("SET", "k", "again", "NX"), "OK")
        await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "k"), b"again")

    async def test_restart_twice_keeps_appending_to_the_same_log(self):
        await self.client.call("SET", "a", "1")
        await self.restart()
        await self.client.call("SET", "b", "2")
        await self.restart()
        self.assertEqual(await self.client.call("GET", "a"), b"1")
        self.assertEqual(await self.client.call("GET", "b"), b"2")

    async def test_half_written_last_command_is_ignored(self):
        await self.client.call("SET", "a", "1")
        with open(self.dir / "appendonly.aof", "ab") as aof:
            aof.write(encode_command("SET", "a", "torn-write")[:-4])
        with self.assertLogs("mini-redis", level="WARNING"):
            await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "a"), b"1")

    async def test_corrupted_aof_stops_startup(self):
        await self.client.call("SET", "a", "1")
        with open(self.dir / "appendonly.aof", "ab") as aof:
            aof.write(b"garbage\r\n" + encode_command("SET", "a", "2"))
        with self.assertRaises(PersistenceError):
            await self.restart(graceful=False)

    async def test_save_removes_from_aof_what_the_rdb_now_holds(self):
        aof_path = self.dir / "appendonly.aof"
        await self.client.call("SET", "a", "1")
        await self.client.call("INCR", "n")
        self.assertGreater(aof_path.stat().st_size, 0)
        await self.client.call("SAVE")
        self.assertEqual(aof_path.stat().st_size, 0)
        await self.client.call("SET", "b", "2")
        self.assertEqual(list(read_aof(aof_path)), [[b"SET", b"b", b"2"]])  # RDB 이후의 변경만 남는다

    async def test_save_that_could_not_be_synced_does_not_empty_the_aof(self):
        await self.client.call("SET", "k", "v")
        for failure in (OSError("io error"), [None, OSError("io error")]):  # 데이터 / 디렉터리 내리기 실패
            with mock.patch("persistence.sync_file", side_effect=failure):
                await self.assert_error("ERR save failed", "SAVE")
            self.assertEqual(list(read_aof(self.dir / "appendonly.aof")), [[b"SET", b"k", b"v"]])
        await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "k"), b"v")

    async def test_recovery_loads_rdb_then_replays_later_changes_from_aof(self):
        await self.client.call("SET", "k", "in-rdb")
        await self.client.call("SET", "only-in-rdb", "x")
        await self.client.call("INCRBY", "n", "10")
        await self.client.call("SAVE")
        await self.client.call("SET", "k", "in-aof")
        await self.client.call("INCR", "n")
        await self.client.call("SET", "only-in-aof", "y")
        await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "k"), b"in-aof")
        self.assertEqual(await self.client.call("GET", "only-in-rdb"), b"x")
        self.assertEqual(await self.client.call("GET", "n"), b"11")
        self.assertEqual(await self.client.call("GET", "only-in-aof"), b"y")

    async def test_crash_between_rdb_save_and_aof_truncate_does_not_apply_changes_twice(self):
        aof_path = self.dir / "appendonly.aof"
        await self.client.call("SET", "a", "old")
        await self.client.call("SET", "a", "new")
        for _ in range(3):
            await self.client.call("INCR", "n")
        await self.client.call("SET", "t", "v", "EX", "100")
        await self.client.call("PERSIST", "t")
        await self.client.call("SET", "u", "v")
        await self.client.call("EXPIRE", "u", "50")
        await self.client.call("SET", "gone", "v")
        await self.client.call("DEL", "gone")
        log_before_save = aof_path.read_bytes()
        await self.client.call("SAVE")
        aof_path.write_bytes(log_before_save)  # RDB는 새것인데 AOF는 비워지지 못한 상태
        await self.restart(graceful=False)
        self.assertEqual(await self.client.call("GET", "a"), b"new")
        self.assertEqual(await self.client.call("GET", "n"), b"3")
        self.assertEqual(await self.client.call("TTL", "t"), -1)
        self.assertEqual(await self.client.call("TTL", "u"), 50)
        self.assertIsNone(await self.client.call("GET", "gone"))

    async def test_expired_key_is_not_written_to_aof_as_a_delete(self):
        aof_path = self.dir / "appendonly.aof"
        await self.client.call("SET", "lazy", "v", "PX", "10")
        await self.client.call("SET", "active", "v", "PX", "10")
        self.clock.now += 10
        self.assertIsNone(await self.client.call("GET", "lazy"))
        self.assertEqual(self.server.db.active_expire_cycle(), 1)
        self.assertEqual([args[0] for args in read_aof(aof_path)], [b"SET", b"SET"])
        await self.restart(graceful=False)
        self.assertIsNone(await self.client.call("GET", "lazy"))
        self.assertIsNone(await self.client.call("GET", "active"))

    async def test_enabling_aof_on_existing_rdb_does_not_copy_rdb_data_into_the_log(self):
        await self.restart(appendonly=False)
        await self.client.call("SET", "from-rdb", "v", "EX", "100")
        await self.client.call("SAVE")
        await self.restart(appendonly=True)
        self.assertEqual((self.dir / "appendonly.aof").stat().st_size, 0)
        await self.client.call("SET", "from-aof", "v")
        await self.restart(graceful=False, appendonly=True)
        self.assertEqual(await self.client.call("TTL", "from-rdb"), 100)
        self.assertEqual(await self.client.call("GET", "from-aof"), b"v")

    async def test_disabling_aof_folds_the_remaining_log_into_the_rdb(self):
        await self.client.call("SET", "k", "only-in-aof")
        await self.restart(appendonly=False)
        self.assertEqual(await self.client.call("GET", "k"), b"only-in-aof")
        self.assertFalse((self.dir / "appendonly.aof").exists())
        self.assertEqual(load_rdb(self.dir / "dump.rdb"), [(b"k", b"only-in-aof", None)])
        await self.restart(appendonly=False)
        self.assertEqual(await self.client.call("GET", "k"), b"only-in-aof")

    async def test_graceful_shutdown_with_auto_save_leaves_nothing_in_aof(self):
        await self.restart(save_interval=3600)
        await self.client.call("INCR", "n")
        await self.restart(save_interval=3600)
        self.assertEqual(list(read_aof(self.dir / "appendonly.aof")), [])
        self.assertEqual(await self.client.call("GET", "n"), b"1")

    async def test_aof_write_failure_is_reported_and_memory_state_is_defined(self):
        with mock.patch.object(self.server.commands.aof, "append", side_effect=OSError("disk full")):
            with self.assertLogs("mini-redis", level="ERROR"):
                await self.assert_error("MISCONF", "SET", "k", "v")
        self.assertEqual(await self.client.call("GET", "k"), b"v")  # 메모리에는 반영됨(문서화된 동작)

    async def test_replay_works_with_lru_swap(self):
        await self.restart(maxmemory=500, maxmemory_policy=POLICY_LRU)
        for i in range(30):
            await self.client.call("SET", f"key:{i:02}", f"{i:02}" * 40)
        await self.restart(graceful=False, maxmemory=500, maxmemory_policy=POLICY_LRU)
        for i in range(30):
            self.assertEqual(await self.client.call("GET", f"key:{i:02}"), (f"{i:02}" * 40).encode())


class AppendFsyncTest(ServerTestCase):
    """AOF를 디스크에 내리는 시점: everysec(기본)은 1초마다, always는 응답 전에 명령마다."""

    config = {"appendonly": True}

    async def test_default_is_everysec_and_reply_does_not_wait_for_sync(self):
        self.assertEqual(self.server.config.appendfsync, FSYNC_EVERYSEC)
        with mock.patch("persistence.sync_file") as sync:
            self.assertEqual(await self.client.call("SET", "k", "v"), "OK")
            self.assertEqual(sync.call_count, 0)

    async def test_everysec_syncs_from_the_periodic_task(self):
        with mock.patch("Redis.CRON_INTERVAL", 0.01):
            await self.restart()
            with mock.patch("persistence.sync_file") as sync:
                await self.client.call("SET", "k", "v")
                await self.eventually(lambda: sync.call_count == 1)

    async def test_always_syncs_once_per_write_before_replying(self):
        await self.restart(appendfsync=FSYNC_ALWAYS)
        with mock.patch("persistence.sync_file") as sync:
            self.assertEqual(await self.client.call("SET", "k", "v"), "OK")
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(await self.client.call("INCR", "n"), 1)
            self.assertEqual(sync.call_count, 2)
            self.assertEqual(await self.client.call("DEL", "k", "n"), 2)
            self.assertEqual(sync.call_count, 3)

    async def test_always_does_not_sync_for_commands_that_change_nothing(self):
        await self.restart(appendfsync=FSYNC_ALWAYS)
        await self.client.call("SET", "k", "text")
        with mock.patch("persistence.sync_file") as sync:
            await self.client.call("GET", "k")
            await self.client.call("TTL", "k")
            self.assertIsNone(await self.client.call("SET", "k", "other", "NX"))  # 조건 불일치
            self.assertEqual(await self.client.call("DEL", "missing"), 0)
            self.assertEqual(await self.client.call("EXPIRE", "missing", "10"), 0)
            await self.assert_error("ERR", "INCR", "k")  # 실패한 명령
            self.assertEqual(sync.call_count, 0)

    async def test_always_sync_failure_is_reported_and_memory_state_is_defined(self):
        await self.restart(appendfsync=FSYNC_ALWAYS)
        with mock.patch("persistence.sync_file", side_effect=OSError("io error")):
            with self.assertLogs("mini-redis", level="ERROR"):
                await self.assert_error("MISCONF", "SET", "k", "v")
        self.assertEqual(await self.client.call("GET", "k"), b"v")  # AOF 쓰기 실패와 같은 문서화된 동작
        self.assertEqual(await self.client.call("SET", "k", "again"), "OK")  # 장애가 풀리면 다시 정상

    async def test_always_recovers_every_acknowledged_write_after_crash(self):
        await self.restart(appendfsync=FSYNC_ALWAYS)
        for i in range(20):
            await self.client.call("SET", f"key:{i}", i)
        await self.client.call("INCRBY", "n", "7")
        await self.client.call("DEL", "key:0")
        await self.restart(graceful=False, appendfsync=FSYNC_ALWAYS)
        self.assertIsNone(await self.client.call("GET", "key:0"))
        for i in range(1, 20):
            self.assertEqual(await self.client.call("GET", f"key:{i}"), str(i).encode())
        self.assertEqual(await self.client.call("GET", "n"), b"7")

    async def test_always_keeps_working_after_save_truncates_the_log(self):
        await self.restart(appendfsync=FSYNC_ALWAYS)
        await self.client.call("SET", "before", "1")
        await self.client.call("SAVE")
        await self.client.call("SET", "after", "2")
        self.assertEqual(list(read_aof(self.dir / "appendonly.aof")), [[b"SET", b"after", b"2"]])
        await self.restart(graceful=False, appendfsync=FSYNC_ALWAYS)
        self.assertEqual(await self.client.call("GET", "before"), b"1")
        self.assertEqual(await self.client.call("GET", "after"), b"2")

    async def test_unknown_policy_stops_startup(self):
        await self.server.stop()
        self.servers.remove(self.server)
        server = Redis.RedisServer(Redis.Config(port=0, dir=self.dir, appendonly=True, appendfsync="none"))
        with self.assertRaises(ValueError):
            await server.start()


class OptionParsingTest(unittest.TestCase):
    def parse(self, *argv: str) -> Redis.Config:
        with mock.patch("sys.argv", ["Redis.py", *argv]):
            return Redis._parse_args()

    def test_appendfsync_defaults_to_everysec(self):
        self.assertEqual(self.parse("--appendonly").appendfsync, FSYNC_EVERYSEC)

    def test_appendfsync_always_is_accepted(self):
        self.assertEqual(self.parse("--appendonly", "--appendfsync", "always").appendfsync, FSYNC_ALWAYS)

    def test_unknown_appendfsync_value_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse("--appendonly", "--appendfsync", "none")


class KillRecoveryTest(unittest.IsolatedAsyncioTestCase):
    """실제 프로세스를 SIGKILL로 죽여, 종료 절차 없이도 AOF로 복구되는지 확인한다."""

    def start_process(self, port: int, directory: str) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, "Redis.py", "--port", str(port), "--dir", directory, "--appendonly", "--save-interval", "0"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    async def connect(self, port: int) -> RespClient:
        for _ in range(200):
            try:
                return await RespClient.connect("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.02)
        self.fail("서버 프로세스가 뜨지 않았다")

    async def test_sigkill_then_restart_recovers_from_aof(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            process = self.start_process(port, directory)
            client = await self.connect(port)
            for i in range(100):
                await client.call("SET", f"key:{i}", i)
                await client.call("INCR", "counter")
            await client.close()
            os.kill(process.pid, signal.SIGKILL)
            process.wait()

            self.start_process(port, directory)
            client = await self.connect(port)
            self.assertEqual(await client.call("GET", "counter"), b"100")
            for i in range(100):
                self.assertEqual(await client.call("GET", f"key:{i}"), str(i).encode())
            await client.close()


class CronTest(ServerTestCase):
    async def test_expired_keys_are_removed_without_being_accessed(self):
        with mock.patch("Redis.CRON_INTERVAL", 0.01), mock.patch("Redis.EXPIRE_CYCLE_EVERY", 1):
            await self.restart()
            for i in range(50):
                await self.client.call("SET", f"k{i}", "v", "PX", "10")
            await self.client.call("SET", "stay", "v")
            self.clock.now += 10
            await self.eventually(lambda: len(self.server.db.keyspace) == 1)
        self.assertEqual(len(self.server.db.expires), 0)


if __name__ == "__main__":
    unittest.main()
