import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
import re
from pathlib import Path

from resp import RespError, RespProtocolError, RespSimple, RespValue, encode, read_value
from ticket_store import (
    Hold, HoldExpiredError, ReservationLimitError, SeatHeldError, SeatUnavailableError, TicketStore,
)


@dataclass
class ReservationRequest:
    command: str
    arguments: list[str]
    result: asyncio.Future[RespValue]


async def process_reservations(
    store: TicketStore, queue: asyncio.Queue[ReservationRequest]
) -> None:
    while True:
        request = await queue.get()
        try:
            try:
                response = await asyncio.to_thread(apply_reservation, store, request.command, request.arguments)
            except HoldExpiredError:
                response = RespError("ERR hold expired")
            except SeatUnavailableError:
                response = RespError("ERR seat unavailable")
            except ReservationLimitError:
                response = RespError("ERR user already has a reservation")
            except OSError:
                response = RespError("ERR could not save reservation")
            if not request.result.done():
                request.result.set_result(response)
        finally:
            queue.task_done()


def hold_reply(hold: Hold, store: TicketStore) -> RespValue:
    return [hold.token.encode(), hold.seat_number.encode(), store.seats[hold.seat_number].price,
            hold.remaining_ms()]


def apply_reservation(store: TicketStore, command: str, args: list[str]) -> RespValue:
    if command == "HOLD":
        return hold_reply(store.hold(args[1], args[0]), store)
    if command == "CANCEL":
        store.cancel(args[0])
        return RespSimple("OK")

    token, name, birth_date, phone = args
    name, phone = name.strip(), phone.strip()
    try:
        valid_date = date.fromisoformat(birth_date).isoformat() == birth_date
    except ValueError:
        valid_date = False
    if not name or not valid_date or not re.fullmatch(r"\+?[0-9][0-9 -]*", phone):
        return RespError("ERR invalid reservation details")
    store.reserve(token, {"name": name, "birth_date": birth_date, "phone": phone})
    return RespSimple("OK")


async def execute(
    request: RespValue, store: TicketStore, queue: asyncio.Queue[ReservationRequest]
) -> RespValue:
    if not isinstance(request, list) or not request or not all(isinstance(part, bytes) for part in request):
        return RespError("ERR expected an array of bulk strings")
    try:
        command, *args = (part.decode("utf-8") for part in request)
    except UnicodeDecodeError:
        return RespError("ERR invalid UTF-8 in request")

    command = command.upper()
    write_arguments = {"HOLD": 2, "CANCEL": 1, "SET": 4}
    if command in write_arguments:
        if command == "SET" and len(args) == 2:
            return RespError("ERR seat hold and reservation details required")
        if len(args) != write_arguments[command] or not all(args):
            return RespError("ERR invalid arguments")
        if command == "SET":
            prefix, _, token = args[0].partition(":")
            if prefix != "queue" or not token:
                return RespError("ERR invalid key")
            args[0] = token
        result = asyncio.get_running_loop().create_future()
        queue.put_nowait(ReservationRequest(command, args, result))
        return await result
    if command != "GET":
        return RespError("ERR unsupported command")
    if len(args) != 1:
        return RespError("ERR invalid arguments")
    prefix, _, key = args[0].partition(":")
    if not key:
        return RespError("ERR invalid key")

    if command == "GET" and args[0] == "seats:reserved":
        return [seat.encode("utf-8") for seat in store.booked_seats()]
    if command == "GET" and prefix == "seat":
        try:
            price = store.price(key)
        except SeatHeldError:
            return RespError("ERR seat held")
        except SeatUnavailableError:
            return RespError("ERR seat unavailable")
        return [key.encode("utf-8"), price] if price is not None else RespError("ERR unknown seat")
    if command == "GET" and prefix == "reservations":
        return [seat.encode("utf-8") for seat in store.reservations(key)]
    if command == "GET" and prefix == "hold":
        try:
            return hold_reply(store.active_hold(key), store)
        except HoldExpiredError:
            return RespError("ERR hold expired")
    return RespError("ERR invalid arguments")


async def serve(store: TicketStore, host: str = "127.0.0.1", port: int = 6379) -> None:
    queue: asyncio.Queue[ReservationRequest] = asyncio.Queue()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    request = await read_value(reader)
                except EOFError:
                    break
                except RespProtocolError:
                    writer.write(encode(RespError("ERR invalid RESP2 frame")))
                    await writer.drain()
                    break
                response = await execute(request, store, queue)
                writer.write(encode(response))
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle_client, host, port)
    worker = asyncio.create_task(process_reservations(store, queue))
    try:
        async with server:
            await server.serve_forever()
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


if __name__ == "__main__":
    path = Path(__file__).with_name("reservations.json")
    try:
        asyncio.run(serve(TicketStore(path)))
    except KeyboardInterrupt:
        pass
