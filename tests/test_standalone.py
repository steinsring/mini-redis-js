import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class StandaloneTests(unittest.TestCase):
    def test_package_runs_alone_without_site_packages(self):
        package = Path(__file__).resolve().parents[1] / "mini_redis"
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(package, Path(directory) / "mini_redis",
                            ignore=shutil.ignore_patterns("__pycache__"))
            result = subprocess.run(
                [sys.executable, "-B", "-S", "-c", """
import asyncio
from mini_redis import KeyValueStore, RespSimple, encode, read_value, start_server
async def main():
    async with start_server(KeyValueStore().execute, port=0) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(encode([b"SET", b"standalone", b"works"]) +
                     encode([b"GET", b"standalone"]))
        await writer.drain()
        assert await asyncio.wait_for(read_value(reader), 2) == RespSimple("OK")
        assert await asyncio.wait_for(read_value(reader), 2) == b"works"
        writer.close()
        await writer.wait_closed()
asyncio.run(main())
"""],
                cwd=directory, env={**os.environ, "PYTHONPATH": directory},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
