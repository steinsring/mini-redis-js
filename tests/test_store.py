import unittest

from mini_redis import KeyValueStore, RespError, RespSimple


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = KeyValueStore()

    async def test_set_get_overwrite_and_missing(self):
        self.assertIsNone(await self.store.execute([b"GET", b"missing"]))
        for value in (b"first", b"second", b""):
            self.assertEqual(await self.store.execute([b"SET", b"key", value]), RespSimple("OK"))
            self.assertEqual(await self.store.execute([b"GET", b"key"]), value)

    async def test_binary_and_empty_keys_are_ordinary_keys(self):
        for key in (b"", b"\x00\xff", b"seat:1", b"queue:token"):
            self.assertEqual(await self.store.execute([b"SET", key, b"\x00\xff\r\n"]), RespSimple("OK"))
            self.assertEqual(await self.store.execute([b"GET", key]), b"\x00\xff\r\n")

    async def test_delete_counts_existing_keys_once(self):
        await self.store.execute([b"SET", b"a", b""])
        await self.store.execute([b"SET", b"b", b"value"])
        self.assertEqual(await self.store.execute([b"DEL", b"a", b"a", b"missing", b"b"]), 2)
        self.assertIsNone(await self.store.execute([b"GET", b"a"]))
        self.assertIsNone(await self.store.execute([b"GET", b"b"]))

    async def test_commands_are_case_insensitive(self):
        self.assertEqual(await self.store.execute([b"sEt", b"k", b"v"]), RespSimple("OK"))
        self.assertEqual(await self.store.execute([b"gEt", b"k"]), b"v")
        self.assertEqual(await self.store.execute([b"dEl", b"k"]), 1)

    async def test_invalid_requests_do_not_change_values(self):
        await self.store.execute([b"SET", b"k", b"v"])
        for request in (None, b"GET", [], [b"GET"], [b"GET", b"k", b"extra"],
                        [b"SET", b"k"], [b"SET", b"k", b"v", b"extra"],
                        [b"DEL"], [b"SET", b"k", None], [b"HOLD", b"u", b"1"]):
            with self.subTest(request=request):
                self.assertIsInstance(await self.store.execute(request), RespError)
                self.assertEqual(await self.store.execute([b"GET", b"k"]), b"v")

    async def test_instances_have_independent_data(self):
        await self.store.execute([b"SET", b"k", b"v"])
        self.assertIsNone(await KeyValueStore().execute([b"GET", b"k"]))
