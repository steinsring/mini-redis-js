import json
import os
import time
from secrets import token_urlsafe
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass
class Seat:
    price: int
    user_id: str | None = None
    details: dict[str, str] | None = None


class ReservationLimitError(Exception):
    pass


class SeatUnavailableError(Exception):
    pass


class SeatHeldError(SeatUnavailableError):
    pass


class HoldExpiredError(Exception):
    pass


@dataclass(frozen=True)
class Hold:
    token: str
    seat_number: str
    user_id: str
    expires_at: float

    def remaining_ms(self) -> int:
        return max(0, int((self.expires_at - time.monotonic()) * 1000))


class TicketStore:
    def __init__(self, reservations_path: Path) -> None:
        self.reservations_path = reservations_path
        with Path(__file__).with_name("web").joinpath("seat-layout.json").open(encoding="utf-8") as file:
            layout = json.load(file)
        self.seats = {
            str(number): Seat(layout["prices"][color])
            for number, (_, _, color) in enumerate(layout["seats"], start=1)
        }
        self.users = {"user-123": "", "user-456": ""}
        self.holds: dict[str, Hold] = {}
        self._restore()

    def price(self, seat_number: str) -> int | None:
        seat = self.seats.get(seat_number)
        if seat is None:
            return None
        held = any(hold.seat_number == seat_number and hold.remaining_ms()
                   for hold in self.holds.copy().values())
        if seat.user_id is not None:
            raise SeatUnavailableError
        if held:
            raise SeatHeldError
        return seat.price

    def hold(self, seat_number: str, user_id: str) -> Hold:
        for token, hold in list(self.holds.items()):
            if not hold.remaining_ms():
                del self.holds[token]
        if self.reservations(user_id):
            raise ReservationLimitError
        if seat_number not in self.seats or seat_number in self.booked_seats():
            raise SeatUnavailableError
        if any(hold.user_id == user_id for hold in self.holds.values()):
            raise ReservationLimitError
        hold = Hold(token_urlsafe(24), seat_number, user_id, time.monotonic() + 300)
        self.holds[hold.token] = hold
        return hold

    def active_hold(self, token: str) -> Hold:
        hold = self.holds.get(token)
        if hold is None or not hold.remaining_ms():
            raise HoldExpiredError
        return hold

    def cancel(self, token: str) -> None:
        self.holds.pop(token, None)

    def reserve(self, token: str, details: dict[str, str]) -> None:
        hold = self.active_hold(token)
        seat = self.seats[hold.seat_number]
        if seat.user_id is not None:
            raise SeatUnavailableError
        if self.reservations(hold.user_id):
            raise ReservationLimitError

        reservations = {
            number: reserved.details if reserved.details is not None else reserved.user_id
            for number, reserved in self.seats.items()
            if reserved.user_id is not None
        }
        record = {"user_id": hold.user_id, **details}
        reservations[hold.seat_number] = record
        self._save(reservations)
        seat.user_id = hold.user_id
        seat.details = record
        del self.holds[token]

    def booked_seats(self) -> list[str]:
        held = {hold.seat_number for hold in self.holds.copy().values() if hold.remaining_ms()}
        return [number for number, seat in self.seats.items() if seat.user_id is not None or number in held]

    def reservations(self, user_id: str) -> list[str]:
        return [number for number, seat in self.seats.items() if seat.user_id == user_id]

    def _restore(self) -> None:
        try:
            with self.reservations_path.open(encoding="utf-8") as file:
                reservations = json.load(file)
        except FileNotFoundError:
            return

        if not isinstance(reservations, dict):
            raise ValueError("reservations.json must map seat numbers to user IDs")
        for seat_number, record in reservations.items():
            user_id = record.get("user_id") if isinstance(record, dict) else record
            if seat_number not in self.seats or not isinstance(user_id, str) or not user_id:
                raise ValueError("reservations.json contains an invalid reservation")
            seat = self.seats[seat_number]
            seat.user_id = user_id
            if isinstance(record, dict):
                seat.details = record

    def _save(self, reservations: dict[str, str | dict[str, str] | None]) -> None:
        temporary_path = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.reservations_path.parent, delete=False
            ) as file:
                temporary_path = Path(file.name)
                json.dump(reservations, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            temporary_path.replace(self.reservations_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
