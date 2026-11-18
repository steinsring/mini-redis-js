"""디스크 저장: 스왑(메모리에서 밀려난 항목), RDB 스냅샷, AOF 로그.

파일 형식은 모두 자체 형식이며 실제 Redis의 RDB/AOF와 호환되지 않는다.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import struct
import sys
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path

from resp import ProtocolError, encode_command, parse_command

log = logging.getLogger("mini-redis")

Record = tuple[bytes, bytes, int | None]  # (키, 값, 만료 시각 ms 또는 None)

RDB_MAGIC = b"MINIRDB1"
# 레코드 헤더: 만료 시각(int64, 없으면 -1), 키 길이(uint32), 값 길이(uint32)
_HEADER = struct.Struct("<qII")
_CRC = struct.Struct("<I")

# AOF를 디스크에 내리는 시점.
FSYNC_EVERYSEC = "everysec"  # 서버가 1초마다. 전원 장애 시 최근 약 1초분을 잃을 수 있다.
FSYNC_ALWAYS = "always"  # 명령마다, 응답하기 전에.


class PersistenceError(Exception):
    """백업 파일이 손상됐거나 읽을 수 없다. 빈 상태로 조용히 시작하지 않도록 시작을 중단시킨다."""


def pack_record(key: bytes, value: bytes, expire_at: int | None) -> bytes:
    return _HEADER.pack(-1 if expire_at is None else expire_at, len(key), len(value)) + key + value


def unpack_records(data: bytes) -> Iterator[Record]:
    """연속으로 이어 붙인 레코드를 차례로 풀어낸다.

    Raises:
        PersistenceError: 데이터가 레코드 경계에서 끝나지 않는 경우.
    """
    pos = 0
    while pos < len(data):
        if pos + _HEADER.size > len(data):
            raise PersistenceError("레코드 헤더가 잘려 있다")
        expire_at, key_len, value_len = _HEADER.unpack_from(data, pos)
        pos += _HEADER.size
        end = pos + key_len + value_len
        if end > len(data):
            raise PersistenceError("레코드 본문이 잘려 있다")
        yield data[pos : pos + key_len], data[pos + key_len : end], None if expire_at < 0 else expire_at
        pos = end


class SwapStore:
    """메모리 한도 때문에 밀려난 항목을 키당 파일 하나로 보관한다.

    메모리의 연장일 뿐 백업이 아니다. 재시작하면 데이터는 RDB/AOF에서 복구하므로,
    이전 실행이 남긴 파일은 시작할 때 지운다(남겨 두면 복구된 최신 값과 어긋난 옛 값이 섞인다).
    """

    _SUFFIX = ".swp"

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        directory.mkdir(parents=True, exist_ok=True)
        for stale in directory.glob("*" + self._SUFFIX):
            stale.unlink()
        self.count = 0

    def _path(self, key: bytes) -> Path:
        # 키는 임의의 바이트라 파일 이름으로 쓸 수 없어 해시를 쓴다.
        return self._dir / (hashlib.sha256(key).hexdigest() + self._SUFFIX)

    def put(self, key: bytes, value: bytes, expire_at: int | None) -> None:
        self._path(key).write_bytes(pack_record(key, value, expire_at))
        self.count += 1

    def read(self, key: bytes) -> tuple[bytes, int | None] | None:
        try:
            data = self._path(key).read_bytes()
        except FileNotFoundError:
            return None
        stored_key, value, expire_at = next(unpack_records(data))
        return (value, expire_at) if stored_key == key else None

    def remove(self, key: bytes) -> None:
        self._path(key).unlink()
        self.count -= 1

    def records(self) -> Iterator[Record]:
        for path in self._dir.glob("*" + self._SUFFIX):
            yield from unpack_records(path.read_bytes())


def save_rdb(path: Path, records: Iterable[Record]) -> None:
    """전체 데이터를 스냅샷 파일로 쓴다.

    임시 파일에 다 쓰고 디스크에 내린 뒤 이름을 바꾼다. 쓰는 도중 실패하거나 전원이 나가도
    이전 스냅샷이 온전히 남는다. AOF와 같은 sync_file로 내린다. 호출자가 이 함수 뒤에 AOF를
    비우므로, RDB가 AOF보다 약한 보장으로 내려가면 그 순간의 전원 장애에 데이터를 잃는다.

    Raises:
        OSError: 디스크 쓰기 실패. 이름을 바꾸기 전에 실패했으면 이전 파일이 그대로 남고,
            디렉터리를 내리다 실패했으면 새 파일로 바뀐 상태다. 어느 쪽이든 읽을 수 있는 파일이다.
    """
    tmp = path.with_name(path.name + ".tmp")
    crc = zlib.crc32(RDB_MAGIC)
    try:
        with open(tmp, "wb") as file:
            file.write(RDB_MAGIC)
            for record in records:
                chunk = pack_record(*record)
                crc = zlib.crc32(chunk, crc)
                file.write(chunk)
            file.write(_CRC.pack(crc))
            file.flush()
            sync_file(file.fileno())
        os.replace(tmp, path)
        # 이름 변경까지 디스크에 내려야 한다. 호출자는 이 함수가 끝나면 AOF를 비우는데,
        # 이름 변경이 전원 장애로 사라지면 옛 RDB와 빈 AOF만 남아 그 사이의 변경을 잃는다.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            sync_file(directory)
        finally:
            os.close(directory)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_rdb(path: Path) -> list[Record]:
    """스냅샷 파일을 읽는다.

    Raises:
        PersistenceError: 형식이 다르거나 체크섬이 맞지 않는 경우.
    """
    data = path.read_bytes()
    if len(data) < len(RDB_MAGIC) + _CRC.size or not data.startswith(RDB_MAGIC):
        raise PersistenceError(f"{path}: mini-Redis RDB 파일이 아니다")
    body, (expected,) = data[: -_CRC.size], _CRC.unpack(data[-_CRC.size :])
    if zlib.crc32(body) != expected:
        raise PersistenceError(f"{path}: 체크섬 불일치(파일 손상)")
    return list(unpack_records(body[len(RDB_MAGIC) :]))


def sync_file(fd: int) -> None:
    """파일 내용을 전원 장애에도 남도록 디스크에 내린다.

    macOS의 fsync는 디스크 장치의 쓰기 캐시까지는 비우지 않아, 전원이 나가면 fsync가 끝난
    데이터도 잃을 수 있다. 장치 캐시까지 비우는 F_FULLFSYNC를 쓴다(Redis도 macOS에서 같다).

    Raises:
        OSError: 디스크 오류이거나, 파일 시스템이 F_FULLFSYNC를 지원하지 않는 경우.
    """
    if sys.platform == "darwin":
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


class Aof:
    """데이터를 바꾼 명령을 RESP 형식으로 덧붙이는 로그."""

    def __init__(self, path: Path, fsync_policy: str = FSYNC_EVERYSEC) -> None:
        """
        Args:
            path: 로그 파일 경로.
            fsync_policy: FSYNC_EVERYSEC 또는 FSYNC_ALWAYS.

        Raises:
            ValueError: 알 수 없는 fsync_policy. 조용히 기본값으로 바꾸면 기대한 내구성 없이 돌게 된다.
        """
        if fsync_policy not in (FSYNC_EVERYSEC, FSYNC_ALWAYS):
            raise ValueError(f"알 수 없는 appendfsync 값: {fsync_policy!r}")
        self._always = fsync_policy == FSYNC_ALWAYS
        self._file = open(path, "ab")
        self._unsynced = False

    def append(self, args: list[bytes]) -> None:
        """명령을 기록하고 OS 버퍼까지 내려보낸다.

        flush까지 하면 서버 프로세스가 죽어도 기록이 남는다. 전원 장애까지 견디려면 디스크에
        내려야 하는데, 명령마다 하면 매우 느리다. everysec은 이를 서버의 1초 주기 작업에 맡기고,
        always는 여기서 바로 내려 "성공 응답을 받은 쓰기는 잃지 않는다"를 지킨다.

        Raises:
            OSError: 쓰기 또는 디스크에 내리기 실패.
        """
        self._file.write(encode_command(*args))
        self._file.flush()
        if self._always:
            sync_file(self._file.fileno())
        else:
            self._unsynced = True

    def fsync(self) -> None:
        if self._unsynced:
            sync_file(self._file.fileno())
            self._unsynced = False

    def truncate(self) -> None:
        """기록을 모두 비운다. 그 내용이 RDB에 안전하게 담긴 뒤에만 호출해야 한다."""
        self._file.truncate(0)
        self._unsynced = True

    def close(self) -> None:
        self.fsync()
        self._file.close()


def read_aof(path: Path) -> Iterator[list[bytes]]:
    """AOF의 명령을 기록된 순서대로 낸다.

    마지막 명령이 쓰다 만 상태면(기록 도중 종료) 그 부분만 잘라 내고 정상 종료한다.
    그 명령은 클라이언트가 성공 응답을 받지 못한 것이므로 버려도 약속을 어기지 않는다.

    Raises:
        PersistenceError: 중간이 손상되어 명령 경계를 알 수 없는 경우.
    """
    data = path.read_bytes()
    pos = 0
    while pos < len(data):
        try:
            parsed = parse_command(data, pos, len(data))
        except ProtocolError as exc:
            raise PersistenceError(f"{path}: {pos}바이트 위치가 손상됨: {exc}") from exc
        if parsed is None:
            log.warning("AOF 끝의 미완성 명령 %d바이트를 잘라 낸다", len(data) - pos)
            os.truncate(path, pos)
            return
        args, pos = parsed
        yield args
