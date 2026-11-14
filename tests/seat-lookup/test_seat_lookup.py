"""Run the six seat lookup scenarios against the real application in Chrome."""

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import uvicorn
from fastapi.responses import FileResponse, HTMLResponse

TESTS = Path(__file__).resolve().parent
ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else TESTS.parents[1]
sys.path.insert(0, str(ROOT))

import fastapi_server
import redis
from ticket_store import TicketStore


class ObservedSeats(dict):
    def __init__(self, seats):
        super().__init__(seats)
        self.lookups = []

    def get(self, key, default=None):
        seat = super().get(key, default)
        self.lookups.append({
            "key": key,
            "price": seat.price if seat else None,
            "reserved": seat.user_id is not None if seat else None,
        })
        return seat


def listening_socket():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    return listener


async def wait_for_storage(port, task):
    for _ in range(150):
        if task.done():
            task.result()
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.02)
        else:
            writer.close()
            await writer.wait_closed()
            return
    raise RuntimeError("Test storage did not start")


def chrome_report(port, profile):
    chrome = Path(os.environ.get("CHROME_PATH") or (
        Path(os.environ["ProgramFiles"]) / "Google/Chrome/Application/chrome.exe"
    ))
    run = subprocess.run(
        [str(chrome), "--headless", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--disable-background-networking", "--dump-dom",
         "--virtual-time-budget=28000", f"--user-data-dir={profile}",
         f"http://127.0.0.1:{port}/__seat_tests"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    match = re.search(r'data-seat-report="([A-Za-z0-9+/=]+)"', run.stdout)
    if run.returncode or not match:
        raise RuntimeError(f"Chrome did not return a report\n{run.stdout[:500]}\n{run.stderr[-1000:]}")
    return json.loads(base64.b64decode(match[1]))


async def main():
    with TemporaryDirectory(prefix="seat-lookup-tests-") as temporary, listening_socket() as web_socket:
        path = Path(temporary)
        store = TicketStore(path / "reservations.json")
        store.seats = ObservedSeats(store.seats)
        store.seats["1"].price = 30_000
        store.seats["10"].price = 100_000
        with listening_socket() as storage_socket:
            storage_port = storage_socket.getsockname()[1]
        storage = asyncio.create_task(redis.serve(store, port=storage_port))

        async def connect_storage(host, port):
            if (host, port) != ("127.0.0.1", 6379):
                raise RuntimeError(f"Unexpected storage address {host}:{port}")
            return await asyncio.open_connection(host, storage_port)

        app = fastapi_server.app

        @app.get("/__seat_tests")
        async def harness():
            return HTMLResponse('<!doctype html><meta charset="utf-8"><output id="result"></output>'
                                '<script type="module" src="/__seat_tests/scenarios.js"></script>')

        @app.get("/__seat_tests/scenarios.js")
        async def scenarios():
            return FileResponse(TESTS / "scenarios.js", media_type="text/javascript")

        @app.post("/__seat_tests/reset")
        async def reset():
            for seat in store.seats.values():
                seat.user_id = None
                seat.details = None
            store.seats["2"].user_id = "test-existing-reservation"
            store.holds.clear()
            store.seats.lookups.clear()
            return {"ready": True}

        @app.get("/__seat_tests/lookups")
        async def lookups():
            return store.seats.lookups

        @app.middleware("http")
        async def control_background_polling(request, call_next):
            if request.url.path == "/booking" and "manual-seat-poll" in request.query_params:
                html = (ROOT / "web" / "booking.html").read_text(encoding="utf-8")
                # Keep the booking page's initial fetch and click handlers intact. Only pause
                # the periodic refresh so it cannot silently fix a failed click-time update.
                script = """<script>
const realSeatInterval = window.setInterval.bind(window);
window.setInterval = (callback, delay, ...args) => {
  if (delay !== 4000) return realSeatInterval(callback, delay, ...args);
  return 0;
};
const seatPriceRequests = window.frameElement.seatPriceRequests;
const seatStatusRequests = window.frameElement.seatStatusRequests;
const realSeatFetch = window.fetch.bind(window);
window.fetch = async (input, options) => {
  const url = new URL(input instanceof Request ? input.url : input, location.href);
  if (url.pathname === '/api/seats/reserved') seatStatusRequests.push(url.pathname);
  if (url.pathname !== '/api/price') return realSeatFetch(input, options);
  const entry = { seat: url.searchParams.get('seat_number') };
  seatPriceRequests.push(entry);
  try {
    const response = await realSeatFetch(input, options);
    entry.status = response.status;
    entry.body = await response.clone().json();
    return response;
  } finally {
    entry.finished = true;
  }
};
</script>"""
                return HTMLResponse(html.replace("</head>", script + "</head>", 1))
            return await call_next(request)

        web_port = web_socket.getsockname()[1]
        web = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        web_task = None
        try:
            await wait_for_storage(storage_port, storage)
            with patch.object(fastapi_server, "asyncio", SimpleNamespace(open_connection=connect_storage)):
                web_task = asyncio.create_task(web.serve(sockets=[web_socket]))
                for _ in range(150):
                    if web.started:
                        break
                    if web_task.done():
                        web_task.result()
                    await asyncio.sleep(0.02)
                if not web.started:
                    raise RuntimeError("Test web server did not start")
                report = await asyncio.to_thread(chrome_report, web_port, path / "chrome")
        finally:
            if web_task is not None:
                web.should_exit = True
                await web_task
            storage.cancel()
            with suppress(asyncio.CancelledError):
                await storage

    if [entry["id"] for entry in report] != [f"SEAT-{i:02d}" for i in range(1, 7)]:
        raise RuntimeError(f"Incomplete Chrome test report: {report}")
    for entry in report:
        print(f'{entry["status"]} {entry["id"]} · {entry["detail"]}')
    passed = sum(entry["status"] == "PASS" for entry in report)
    print(f"{len(report)} tests: {passed} passed, {len(report) - passed} failed")
    return int(passed != len(report))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
