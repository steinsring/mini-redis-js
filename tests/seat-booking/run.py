"""Exercise the actual mini-redis-js stack against a temporary reservation file."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import uvicorn
from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse

TESTS = Path(__file__).resolve().parent
ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else TESTS.parents[1]
sys.path.insert(0, str(ROOT))

import fastapi_server as site
import redis
import ticket_store


class ObservedSeats(dict):
    def __init__(self, seats, lookups):
        super().__init__(seats)
        self.lookups = lookups

    def get(self, key, default=None):
        self.lookups.append(key)
        return super().get(key, default)


class ObservedQueue:
    def __init__(self, queue, gate, enqueued):
        self.queue = queue
        self.gate = gate
        self.enqueued = enqueued

    def put_nowait(self, request):
        self.queue.put_nowait(request)
        self.enqueued.append([request.command, request.arguments])

    async def get(self):
        request = await self.queue.get()
        await self.gate.wait()
        return request

    def task_done(self):
        self.queue.task_done()


class TestServer:
    def __init__(self, directory):
        self.path = directory / "reservations.json"
        self.clock = 0
        self.fail_save = False
        self.lookups = []
        self.enqueued = []
        self.processed = []
        self.report = []
        self.finished = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()
        self.store = self.make_store()
        with port_socket() as listener:
            self.port = listener.getsockname()[1]
        self.storage = None

    def make_store(self):
        store = ticket_store.TicketStore(self.path)
        store.seats = ObservedSeats(store.seats, self.lookups)
        save = store._save

        def save_to_disk(reservations):
            if self.fail_save:
                raise OSError("Test fixture: disk save failed")
            save(reservations)

        store._save = save_to_disk
        return store

    async def start_storage(self):
        self.storage = asyncio.create_task(redis.serve(self.store, port=self.port))
        for _ in range(150):
            if self.storage.done():
                self.storage.result()
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
            except OSError:
                await asyncio.sleep(0.02)
            else:
                writer.close()
                await writer.wait_closed()
                return
        raise RuntimeError("Test storage did not start")

    async def stop_storage(self):
        if self.storage is not None:
            self.storage.cancel()
            with suppress(asyncio.CancelledError):
                await self.storage

    async def connect(self, host, port):
        assert (host, port) == ("127.0.0.1", 6379)
        return await asyncio.open_connection(host, self.port)

    def register_routes(self):
        app = site.app

        @app.get("/__booking_test")
        async def harness():
            return HTMLResponse('<!doctype html><meta charset="utf-8">'
                                '<script type="module" src="/__booking_test/scenarios.js"></script>')

        @app.get("/__booking_test/scenarios.js")
        async def scenarios():
            return FileResponse(TESTS / "scenarios.js", media_type="text/javascript")

        @app.get("/__booking_test/setup.js")
        async def setup():
            return FileResponse(TESTS / "setup.js", media_type="text/javascript")

        @app.post("/__booking_test/report")
        async def report(entry: dict):
            if entry.get("finished"):
                self.finished.set()
            else:
                self.report.append(entry)
                print(f'{entry["status"]} {entry["id"]} · {entry["detail"]}', flush=True)
            return {"ok": True}

        @app.get("/__booking_test/state")
        async def state():
            return {
                "lookups": self.lookups,
                "queued": self.enqueued,
                "processed": self.processed,
                "seats": list(self.store.seats),
                "reserved": self.store.booked_seats(),
                "saved": json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {},
                "colors": {color: next(str(i) for i, seat in enumerate(layout["seats"], 1) if seat[2] == color)
                           for color in ("purple", "green", "blue")},
            }

        @app.post("/__booking_test/control")
        async def control(data: dict):
            if data.get("reset"):
                self.gate.set()
                await self.stop_storage()
                self.path.unlink(missing_ok=True)
                self.clock = 0
                self.fail_save = False
                self.lookups.clear()
                self.enqueued.clear()
                self.processed.clear()
                self.store = self.make_store()
                await self.start_storage()
            if data.get("restart"):
                await self.stop_storage()
                self.store = self.make_store()
                await self.start_storage()
            if "clock" in data:
                self.clock = data["clock"]
            if "fail_save" in data:
                self.fail_save = data["fail_save"]
            if "pause" in data:
                self.gate.clear() if data["pause"] else self.gate.set()
            return {"ok": True}

        @app.middleware("http")
        async def browser_instrumentation(request: Request, call_next):
            if request.url.path in {"/booking", "/checkout"}:
                filename = request.url.path.removeprefix("/") + ".html"
                html = (ROOT / "web" / filename).read_text(encoding="utf-8")
                script = '<script src="/__booking_test/setup.js"></script>'
                return HTMLResponse(html.replace("</head>", script + "</head>", 1))
            return await call_next(request)

        layout = json.loads((ROOT / "web/seat-layout.json").read_text(encoding="utf-8"))


def port_socket():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    return listener


async def chrome_report(test, port, profile):
    chrome = Path(os.environ.get("CHROME_PATH") or (
        Path(os.environ["ProgramFiles"]) / "Google/Chrome/Application/chrome.exe"
    ))
    process = subprocess.Popen(
        [str(chrome), "--headless", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--disable-background-networking",
         f"--user-data-dir={profile}",
         f"http://127.0.0.1:{port}/__booking_test"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        await asyncio.wait_for(test.finished.wait(), timeout=70)
        return test.report
    except TimeoutError as error:
        raise RuntimeError(f"Chrome completed {len(test.report)} scenarios before timing out") from error
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)


async def main():
    with TemporaryDirectory(prefix="booking-tests-") as temp, port_socket() as web_socket:
        directory = Path(temp)
        test = TestServer(directory)
        test.register_routes()
        execute, worker, apply = redis.execute, redis.process_reservations, redis.apply_reservation

        async def observe_execute(request, store, queue):
            return await execute(request, store, ObservedQueue(queue, test.gate, test.enqueued))

        async def observe_worker(store, queue):
            await worker(store, ObservedQueue(queue, test.gate, test.enqueued))

        def observe_apply(store, command, arguments):
            test.processed.append([command, arguments])
            return apply(store, command, arguments)

        web = uvicorn.Server(uvicorn.Config(site.app, log_level="warning", access_log=False))
        web_task = None
        with patch.object(site, "asyncio", SimpleNamespace(open_connection=test.connect)), \
                patch.object(ticket_store, "time", SimpleNamespace(monotonic=lambda: test.clock)), \
                patch.object(redis, "execute", observe_execute), \
                patch.object(redis, "process_reservations", observe_worker), \
                patch.object(redis, "apply_reservation", observe_apply):
            try:
                await test.start_storage()
                web_task = asyncio.create_task(web.serve(sockets=[web_socket]))
                for _ in range(150):
                    if web.started:
                        break
                    if web_task.done():
                        web_task.result()
                    await asyncio.sleep(0.02)
                if not web.started:
                    raise RuntimeError("Test web server did not start")
                report = await chrome_report(test, web_socket.getsockname()[1], directory / "chrome")
            finally:
                test.gate.set()
                if web_task is not None:
                    web.should_exit = True
                    await web_task
                await test.stop_storage()

    expected = [f"{group}-{number:02d}" for group, count in
                (("SEAT", 4), ("BOOK", 3), ("HOLD", 4), ("DONE", 4))
                for number in range(1, count + 1)]
    if [entry["id"] for entry in report] != expected:
        raise RuntimeError(f"Incomplete scenario report: {report}")
    passed = sum(entry["status"] == "PASS" for entry in report)
    print(f"{len(report)} tests: {passed} passed, {len(report) - passed} failed")
    return int(passed != len(report))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
