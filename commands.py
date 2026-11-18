"""명령 해석과 실행.

핸들러는 await 없이 끝나는 동기 함수다. asyncio 단일 스레드에서 명령 하나가 통째로 실행되므로
명령 하나가 곧 원자성의 단위가 된다. 모든 핸들러는 "검증을 전부 끝낸 뒤에 변경을 시작"해서,
오류 응답을 받은 명령은 데이터를 바꾸지 않았음을 보장한다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from db import Database, Entry, OutOfMemory
from persistence import Aof
from resp import NIL, OK, bulk, error, integer, simple

log = logging.getLogger("mini-redis")

INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)
# Redis처럼 엄격하게 본다: 공백, '+', 앞자리 0, 소수는 정수가 아니다.
_INT_RE = re.compile(rb"0|-?[1-9][0-9]*")

ERR_NOT_INT = "ERR value is not an integer or out of range"
ERR_SYNTAX = "ERR syntax error"
ERR_OOM = "OOM command not allowed when used memory > 'maxmemory'."

# 핸들러 반환값: (응답 바이트, AOF에 남길 명령 또는 None)
Result = tuple[bytes, list[bytes] | None]


class CommandError(Exception):
    """클라이언트에게 그대로 돌려줄 오류. 발생 시점에는 데이터가 바뀌지 않았다."""


def parse_int(raw: bytes) -> int:
    """64비트 부호 있는 정수 문자열을 해석한다.

    Raises:
        CommandError: 정수 형식이 아니거나 64비트 범위를 벗어난 경우.
    """
    # 자릿수를 먼저 확인해 터무니없이 긴 숫자를 int()로 변환하는 비용을 피한다.
    if len(raw) > 20 or not _INT_RE.fullmatch(raw):
        raise CommandError(ERR_NOT_INT)
    value = int(raw)
    if not INT64_MIN <= value <= INT64_MAX:
        raise CommandError(ERR_NOT_INT)
    return value


class CommandExecutor:
    """명령을 검증·실행하고, 데이터를 바꾼 명령을 AOF로 전파한다."""

    def __init__(self, db: Database, max_key_size: int, save: Callable[[], None]) -> None:
        """
        Args:
            db: 명령이 조작할 저장소.
            max_key_size: 키의 최대 바이트 수.
            save: RDB 스냅샷을 쓰는 함수(SAVE 명령용).
        """
        self.db = db
        self.max_key_size = max_key_size
        self.save = save
        self.aof: Aof | None = None  # 복구가 끝난 뒤 서버가 연결한다. 재생 중에는 기록하지 않는다.
        self.dirty = 0  # 마지막 RDB 저장 이후의 변경 횟수

    def execute(self, args: list[bytes]) -> bytes:
        """명령 하나를 실행하고 RESP 응답을 돌려준다. 어떤 입력에도 예외를 밖으로 내지 않는다."""
        name = args[0].upper()
        spec = _COMMANDS.get(name)
        if spec is None:
            return error(f"ERR unknown command '{args[0].decode(errors='replace')}'")
        handler, arity, keys = spec
        if len(args) != arity if arity > 0 else len(args) < -arity:
            return error(f"ERR wrong number of arguments for '{name.decode().lower()}' command")
        for key in args[1:2] if keys == _FIRST_KEY else args[1:] if keys == _ALL_KEYS else ():
            if len(key) > self.max_key_size:
                return error(f"ERR key exceeds size limit (max {self.max_key_size} bytes)")
        try:
            reply, propagate = handler(self, args)
        except CommandError as exc:
            return error(str(exc))
        except OutOfMemory:
            return error(ERR_OOM)
        except Exception:
            # 예상하지 못한 내부 오류가 서버 전체를 죽이지 않도록 이 명령의 실패로만 끝낸다.
            log.exception("명령 실행 중 내부 오류: %r", name)
            return error("ERR internal error")
        if propagate is not None:
            try:
                self._propagate(propagate)
            except OSError:
                # 메모리에는 이미 반영됐다. 성공으로 답하면 클라이언트가 영속성을 오해하므로 사실대로 알린다.
                log.exception("AOF 기록 실패")
                return error("MISCONF change was applied in memory but could not be written to the AOF")
        return reply

    def _propagate(self, args: list[bytes]) -> None:
        self.dirty += 1
        if self.aof is not None:
            self.aof.append(args)


def _state_record(entry: Entry) -> list[bytes]:
    """항목의 지금 상태(값과 만료 시각)를 그대로 재현하는 SET 명령을 만든다.

    값을 바꾸는 명령은 받은 명령이 아니라 이 "결과 상태"를 AOF에 남긴다. 그래야
      - 만료로 사라진 키를 DEL로 따로 기록하지 않아도 재생 결과가 맞고(뒤이은 기록이
        키의 상태를 통째로 덮어쓰므로 재생 중 남아 있던 옛 값이 영향을 주지 않는다),
      - RDB 저장 직후 AOF를 비우기 전에 죽어 이미 RDB에 담긴 기록을 한 번 더 재생하게
        되어도 INCR이 두 번 더해지는 일이 없다.
    """
    record = [b"SET", entry.key, entry.value]
    if entry.expire_at is not None:
        record += [b"PXAT", b"%d" % entry.expire_at]
    return record


def _ping(ex: CommandExecutor, args: list[bytes]) -> Result:
    return simple("PONG"), None


def _get(ex: CommandExecutor, args: list[bytes]) -> Result:
    entry = ex.db.lookup_key(args[1])
    return bulk(None if entry is None else entry.value), None


def _set(ex: CommandExecutor, args: list[bytes]) -> Result:
    """SET key value [NX | XX | IFEQ 비교값] [EX 초 | PX 밀리초 | PXAT 시각ms | KEEPTTL]"""
    key, value = args[1], args[2]
    condition: bytes | None = None
    expected: bytes | None = None
    expire_at: int | None = None
    keep_ttl = False
    now = ex.db.now()
    i = 3
    while i < len(args):
        option = args[i].upper()
        has_next = i + 1 < len(args)
        if option in (b"NX", b"XX") and condition is None:
            condition = option
        elif option == b"IFEQ" and condition is None and has_next:
            condition, expected = option, args[i + 1]
            i += 1
        elif option in (b"EX", b"PX", b"PXAT") and expire_at is None and not keep_ttl and has_next:
            amount = parse_int(args[i + 1])
            expire_at = amount if option == b"PXAT" else now + amount * (1000 if option == b"EX" else 1)
            if amount <= 0 or expire_at > INT64_MAX:
                raise CommandError("ERR invalid expire time in 'set' command")
            i += 1
        elif option == b"KEEPTTL" and expire_at is None and not keep_ttl:
            keep_ttl = True
        else:
            raise CommandError(ERR_SYNTAX)
        i += 1

    entry = ex.db.lookup_key(key)
    if (
        (condition == b"NX" and entry is not None)
        or (condition == b"XX" and entry is None)
        or (condition == b"IFEQ" and (entry is None or entry.value != expected))
    ):
        return NIL, None
    if entry is None:
        entry = ex.db.db_add(key, value)
    else:
        ex.db.db_overwrite(entry, value)
        if not keep_ttl:
            ex.db.remove_expire(entry)
    if expire_at is not None:
        ex.db.set_expire(entry, expire_at)

    return OK, _state_record(entry)


def _del(ex: CommandExecutor, args: list[bytes]) -> Result:
    # lookup_key를 먼저 거쳐야 이미 만료된 키를 "지웠다"고 세지 않는다.
    deleted = [key for key in args[1:] if ex.db.lookup_key(key) is not None and ex.db.db_delete(key)]
    return integer(len(deleted)), [b"DEL", *deleted] if deleted else None


def _add(ex: CommandExecutor, args: list[bytes], delta: int) -> Result:
    """정수 값에 delta를 더한다. 값만 바꾸고 수명은 건드리지 않는다."""
    key = args[1]
    entry = ex.db.lookup_key(key)
    result = (0 if entry is None else parse_int(entry.value)) + delta
    if not INT64_MIN <= result <= INT64_MAX:
        raise CommandError("ERR increment or decrement would overflow")
    value = b"%d" % result
    if entry is None:
        entry = ex.db.db_add(key, value)
    else:
        ex.db.db_overwrite(entry, value)
    return integer(result), _state_record(entry)


def _incr(ex: CommandExecutor, args: list[bytes]) -> Result:
    return _add(ex, args, 1)


def _decr(ex: CommandExecutor, args: list[bytes]) -> Result:
    return _add(ex, args, -1)


def _incrby(ex: CommandExecutor, args: list[bytes]) -> Result:
    return _add(ex, args, parse_int(args[2]))


def _decrby(ex: CommandExecutor, args: list[bytes]) -> Result:
    amount = parse_int(args[2])
    if amount == INT64_MIN:  # 부호를 뒤집으면 64비트를 넘는다. Redis와 같은 오류로 거부한다.
        raise CommandError("ERR decrement would overflow")
    return _add(ex, args, -amount)


def _expire_at(ex: CommandExecutor, key: bytes, when: int, command: str) -> Result:
    if when > INT64_MAX:
        raise CommandError(f"ERR invalid expire time in '{command}' command")
    entry = ex.db.lookup_key(key)
    if entry is None:
        return integer(0), None
    # 이미 지난 시각이어도 여기서 지우지 않는다. 만료된 키의 삭제는 지연 삭제와 주기적 삭제가 맡는다.
    # 음수는 0으로 올린다. 뜻은 같고("이미 만료"), 백업 파일에서 음수는 "수명 없음"을 뜻하기 때문이다.
    when = max(when, 0)
    ex.db.set_expire(entry, when)
    return integer(1), [b"PEXPIREAT", key, b"%d" % when]


def _expire(ex: CommandExecutor, args: list[bytes]) -> Result:
    return _expire_at(ex, args[1], ex.db.now() + parse_int(args[2]) * 1000, "expire")


def _pexpireat(ex: CommandExecutor, args: list[bytes]) -> Result:
    return _expire_at(ex, args[1], parse_int(args[2]), "pexpireat")


def _remaining(ex: CommandExecutor, key: bytes) -> int:
    """남은 수명(ms). 키가 없으면 -2, 수명이 없으면 -1."""
    entry = ex.db.lookup_key(key)
    if entry is None:
        return -2
    if entry.expire_at is None:
        return -1
    return entry.expire_at - ex.db.now()


def _ttl(ex: CommandExecutor, args: list[bytes]) -> Result:
    remaining = _remaining(ex, args[1])
    return integer(remaining if remaining < 0 else (remaining + 500) // 1000), None


def _pttl(ex: CommandExecutor, args: list[bytes]) -> Result:
    return integer(_remaining(ex, args[1])), None


def _persist(ex: CommandExecutor, args: list[bytes]) -> Result:
    entry = ex.db.lookup_key(args[1])
    if entry is None or not ex.db.remove_expire(entry):
        return integer(0), None
    return integer(1), args


def _save(ex: CommandExecutor, args: list[bytes]) -> Result:
    try:
        ex.save()
    except OSError as exc:
        raise CommandError(f"ERR save failed: {exc}") from exc
    return OK, None


_NO_KEY, _FIRST_KEY, _ALL_KEYS = 0, 1, 2

# 이름 -> (핸들러, 인자 수, 키 위치). 인자 수가 음수면 "그 절댓값 이상"을 뜻한다(Redis 관례).
_COMMANDS: dict[bytes, tuple[Callable[[CommandExecutor, list[bytes]], Result], int, int]] = {
    b"PING": (_ping, 1, _NO_KEY),
    b"GET": (_get, 2, _FIRST_KEY),
    b"SET": (_set, -3, _FIRST_KEY),
    b"DEL": (_del, -2, _ALL_KEYS),
    b"INCR": (_incr, 2, _FIRST_KEY),
    b"DECR": (_decr, 2, _FIRST_KEY),
    b"INCRBY": (_incrby, 3, _FIRST_KEY),
    b"DECRBY": (_decrby, 3, _FIRST_KEY),
    b"EXPIRE": (_expire, 3, _FIRST_KEY),
    b"PEXPIREAT": (_pexpireat, 3, _FIRST_KEY),
    b"TTL": (_ttl, 2, _FIRST_KEY),
    b"PTTL": (_pttl, 2, _FIRST_KEY),
    b"PERSIST": (_persist, 2, _FIRST_KEY),
    b"SAVE": (_save, 1, _NO_KEY),
}
