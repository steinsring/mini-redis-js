import asyncio
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from resp import RespError, RespProtocolError, RespSimple, RespValue, encode, read_value

WEB_DIRECTORY = Path(__file__).with_name("web")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=WEB_DIRECTORY), name="static")


class HoldRequest(BaseModel):
    seat_number: str = Field(min_length=1)
    user_id: str = Field(min_length=1)


class ReservationRequest(BaseModel):
    token: str = Field(min_length=1)
    name: str = Field(min_length=1)
    birth_date: str = Field(min_length=1)
    phone: str = Field(min_length=1)


async def ticket_request(*arguments: str) -> RespValue:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", 6379)
        try:
            writer.write(encode([argument.encode("utf-8") for argument in arguments]))
            await writer.drain()
            reply = await read_value(reader)
        finally:
            writer.close()
            await writer.wait_closed()
    except (OSError, EOFError, RespProtocolError) as error:
        raise HTTPException(502, "예매 서버와 통신할 수 없습니다.") from error
    if isinstance(reply, RespError):
        status = 410 if reply.message == "ERR hold expired" else 400
        raise HTTPException(status, reply.message)
    return reply


@app.get("/")
async def index():
    return FileResponse(WEB_DIRECTORY / "index.html")


@app.get("/booking")
async def booking():
    return FileResponse(WEB_DIRECTORY / "booking.html")


@app.get("/checkout")
async def checkout():
    return FileResponse(WEB_DIRECTORY / "checkout.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def hold_data(reply: RespValue):
    if (
        isinstance(reply, list) and len(reply) == 4
        and isinstance(reply[0], bytes) and isinstance(reply[1], bytes)
        and isinstance(reply[2], int) and isinstance(reply[3], int)
    ):
        return {"token": reply[0].decode(), "seat_number": reply[1].decode(),
                "price": reply[2], "remaining_ms": reply[3]}
    raise HTTPException(502, "좌석 임시점유 응답 형식이 올바르지 않습니다.")


@app.post("/api/holds")
async def hold(request: HoldRequest):
    if not request.user_id.strip():
        raise HTTPException(400, "사용자 ID를 입력하세요.")
    return hold_data(await ticket_request("HOLD", request.user_id.strip(), request.seat_number))


@app.get("/api/holds")
async def get_hold(token: str = Header(alias="X-Hold-Token", min_length=1)):
    return hold_data(await ticket_request("GET", f"hold:{token}"))


@app.delete("/api/holds")
async def cancel_hold(token: str = Header(alias="X-Hold-Token", min_length=1)):
    reply = await ticket_request("CANCEL", token)
    if isinstance(reply, RespSimple) and reply.message == "OK":
        return {"success": True}
    raise HTTPException(502, "예매 취소 응답 형식이 올바르지 않습니다.")


@app.get("/api/seats/reserved")
async def booked_seats():
    reply = await ticket_request("GET", "seats:reserved")
    if isinstance(reply, list) and all(isinstance(seat, bytes) for seat in reply):
        return [seat.decode("utf-8") for seat in reply]
    raise HTTPException(502, "좌석 상태 응답 형식이 올바르지 않습니다.")


@app.get("/api/price")
async def price(seat_number: str = Query(min_length=1)):
    reply = await ticket_request("GET", f"seat:{seat_number}")
    if (
        isinstance(reply, list) and len(reply) == 2
        and isinstance(reply[0], bytes) and isinstance(reply[1], int)
    ):
        return {reply[0].decode("utf-8"): reply[1]}
    raise HTTPException(502, "가격 응답 형식이 올바르지 않습니다.")


@app.post("/api/reservations")
async def reserve(request: ReservationRequest):
    reply = await ticket_request("SET", f"queue:{request.token}", request.name, request.birth_date, request.phone)
    if isinstance(reply, RespSimple) and reply.message == "OK":
        return {"success": True}
    raise HTTPException(502, "예매 응답 형식이 올바르지 않습니다.")


@app.get("/api/reservations")
async def reservations(user_id: str = Query(min_length=1)):
    reply = await ticket_request("GET", f"reservations:{user_id}")
    if isinstance(reply, list) and all(isinstance(seat, bytes) for seat in reply):
        return [seat.decode("utf-8") for seat in reply]
    raise HTTPException(502, "예매 내역 응답 형식이 올바르지 않습니다.")

