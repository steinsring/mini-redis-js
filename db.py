"""메모리 저장소: 키 공간 테이블과 만료 시각 테이블.

명령 구현은 두 테이블을 직접 만지지 않고 이 모듈의 `lookup_key`, `db_add`, `db_overwrite`,
`set_expire`, `remove_expire`, `db_delete`만 호출한다(ARCHITECTURE.md의 lookupKey, dbAdd 등에
대응하며 Python 이름 규칙에 맞춰 snake_case로 적었다).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Protocol

EXPIRE_CYCLE_SAMPLES = 10
EXPIRE_CYCLE_REPEAT_RATIO = 0.2
EXPIRE_CYCLE_TIME_LIMIT = 0.030  # 초
EVICTION_SAMPLES = 5  # Redis의 maxmemory-samples 기본값

POLICY_NOEVICTION = "noeviction"
POLICY_LRU = "allkeys-lru"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class OutOfMemory(Exception):
    """maxmemory 안에 공간을 마련하지 못했다. 발생 시점에는 아무 데이터도 바뀌지 않았다."""


class Entry:
    """키 하나의 항목. 두 테이블이 같은 객체를 가리키며(포인터), 만료 시각은 항목 안에 둔다."""

    __slots__ = ("key", "value", "expire_at", "last_access")

    def __init__(self, key: bytes, value: bytes, last_access: int = 0):
        self.key = key
        self.value = value
        self.expire_at: int | None = None  # 유닉스 시각(ms). None이면 수명 없음.
        self.last_access = last_access


class SwapStore(Protocol):
    """메모리에서 밀려난 항목을 보관하는 디스크 저장소(persistence.SwapStore)."""

    count: int

    def put(self, key: bytes, value: bytes, expire_at: int | None) -> None: ...
    def read(self, key: bytes) -> tuple[bytes, int | None] | None: ...
    def remove(self, key: bytes) -> None: ...
    def records(self) -> Iterator[tuple[bytes, bytes, int | None]]: ...


class HashTable:
    """체이닝 방식 해시테이블. 버킷에는 Entry 포인터를 담는다.

    내장 dict는 버킷 위치를 드러내지 않아 "버킷을 순서대로 훑다가 멈춘 곳에서 이어가는"
    주기적 만료 샘플링을 구현할 수 없어서 직접 만들었다.
    """

    MIN_SIZE = 16

    def __init__(self) -> None:
        self._buckets: list[list[Entry]] = [[] for _ in range(self.MIN_SIZE)]
        self._count = 0

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Entry]:
        for bucket in self._buckets:
            yield from bucket

    def _bucket(self, key: bytes) -> list[Entry]:
        # 크기가 항상 2의 거듭제곱이라 나머지 연산 대신 비트 마스크를 쓴다.
        return self._buckets[hash(key) & (len(self._buckets) - 1)]

    def get(self, key: bytes) -> Entry | None:
        for entry in self._bucket(key):
            if entry.key == key:
                return entry
        return None

    def put(self, entry: Entry) -> None:
        """포인터를 넣는다. 같은 키가 있으면 새 포인터로 고친다(항목 재할당 대응)."""
        bucket = self._bucket(entry.key)
        for i, existing in enumerate(bucket):
            if existing.key == entry.key:
                bucket[i] = entry
                return
        bucket.append(entry)
        self._count += 1
        if self._count > len(self._buckets):
            self._resize(len(self._buckets) * 2)

    def remove(self, key: bytes) -> Entry | None:
        bucket = self._bucket(key)
        for i, entry in enumerate(bucket):
            if entry.key == key:
                del bucket[i]
                self._count -= 1
                # 줄이지 않으면 대량 삭제 뒤 scan이 빈 버킷만 오래 훑게 된다.
                if len(self._buckets) > self.MIN_SIZE and self._count < len(self._buckets) // 8:
                    self._resize(len(self._buckets) // 2)
                return entry
        return None

    def _resize(self, size: int) -> None:
        entries = list(self)
        self._buckets = [[] for _ in range(size)]
        for entry in entries:
            self._bucket(entry.key).append(entry)

    def scan(self, cursor: int, limit: int) -> tuple[list[Entry], int]:
        """cursor 버킷부터 순서대로 훑어 최대 limit개의 항목을 모은다.

        버킷 중간에서 끊으면 이어서 훑을 때 남은 항목을 놓치므로 버킷 단위로만 자른다.
        그래서 버킷 하나가 limit보다 큰 경우에만 limit을 넘겨 돌려준다.

        Returns:
            (항목 목록, 다음에 이어갈 cursor). 테이블 끝에 닿으면 거기서 멈추고 cursor 0을
            돌려주므로, 호출자는 0을 보고 한 바퀴가 끝났음을 안다. 끝부분이 비어 있으면
            항목 목록이 비어 있을 수 있다.
        """
        size = len(self._buckets)
        if cursor >= size:  # 리사이즈로 테이블이 줄었으면 처음부터 다시 훑는다.
            cursor = 0
        found: list[Entry] = []
        while cursor < size:
            bucket = self._buckets[cursor]
            if found and len(found) + len(bucket) > limit:
                return found, cursor
            found.extend(bucket)
            cursor += 1
            if len(found) >= limit:
                break
        return found, cursor % size


class Database:
    """키 공간 테이블과 만료 시각 테이블을 함께 관리한다.

    공통 규칙: 만료 시각 테이블에 포인터가 있는 항목은 반드시 키 공간 테이블에도 있고
    항목 안에 만료 시각이 있다. 모든 메서드는 await 없이 끝나므로 asyncio 단일 스레드에서
    두 테이블을 차례로 고치는 중간 상태가 다른 명령에 보이지 않는다.

    만료된 키를 지우는 곳은 lookup_key(지연 삭제)와 active_expire_cycle(주기적 삭제) 둘뿐이다.
    저장, 복구, 메모리 확보 같은 다른 경로는 만료 여부를 따지지 않고 항목을 그대로 다룬다.
    """

    def __init__(
        self,
        maxmemory: int = 0,
        policy: str = POLICY_NOEVICTION,
        swap: SwapStore | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        """
        Args:
            maxmemory: 메모리 한도(바이트, 키+값 길이의 합). 0이면 무제한.
            policy: 한도 도달 시 정책. "noeviction" 또는 "allkeys-lru".
            swap: allkeys-lru에서 밀려난 항목을 보관할 디스크 저장소.
            clock: 현재 시각(ms)을 주는 함수. 테스트가 시간을 조종하려고 주입한다.
        """
        if policy == POLICY_LRU and swap is None:
            raise ValueError("allkeys-lru 정책에는 swap 저장소가 필요하다")
        self.keyspace = HashTable()
        self.expires = HashTable()
        self.maxmemory = maxmemory
        self.policy = policy
        self.swap = swap
        self.used_memory = 0
        # 백업 파일을 읽는 동안에는 만료를 적용하지 않는다. 기록 당시 살아 있던 키를
        # 재생 도중 지우면 뒤따르는 수명 변경 기록(PEXPIREAT, PERSIST)이 대상을 찾지 못한다.
        self.loading = False
        self._clock = clock
        self._expire_cursor = 0
        self._evict_cursor = 0

    def now(self) -> int:
        return self._clock()

    def lookup_key(self, key: bytes) -> Entry | None:
        """키를 조회한다. 모든 조회는 이 함수를 거쳐 만료 확인을 받는다.

        항목 안의 만료 시각만 보고 만료 시각 테이블은 조회하지 않는다. 메모리에 없으면
        디스크로 밀려난 항목인지 확인해 다시 올린다.

        Raises:
            OutOfMemory: 디스크의 항목을 올릴 공간을 마련하지 못한 경우.
        """
        entry = self.keyspace.get(key)
        if entry is None:
            entry = self._swap_in(key)
            if entry is None:
                return None
        now = self._clock()
        if self._is_expired(entry, now):
            self.db_delete(key)
            return None
        entry.last_access = now
        return entry

    def db_add(self, key: bytes, value: bytes) -> Entry:
        """새 항목을 할당해 키 공간 테이블에 넣는다. 호출 전에 lookup_key로 없음을 확인해야 한다.

        Raises:
            OutOfMemory: 공간이 없는 경우. 테이블은 바뀌지 않는다.
        """
        self._make_room(len(key) + len(value), protect=key)
        entry = Entry(key, value, self._clock())
        self.keyspace.put(entry)
        self.used_memory += len(key) + len(value)
        return entry

    def db_overwrite(self, entry: Entry, value: bytes) -> None:
        """값만 교체한다. 수명은 건드리지 않으므로 SET은 이어서 remove_expire를 호출한다.

        Python 객체는 값이 바뀌어도 주소가 그대로라 테이블의 포인터를 고칠 일이 없다.

        Raises:
            OutOfMemory: 값이 커져 공간이 모자란 경우. 기존 값은 그대로다.
        """
        delta = len(value) - len(entry.value)
        if delta > 0:
            self._make_room(delta, protect=entry.key)
        entry.value = value
        self.used_memory += delta

    def set_expire(self, entry: Entry, when: int) -> None:
        """항목에 만료 시각을 기록하고, 만료 시각 테이블에 포인터가 없으면 추가한다."""
        entry.expire_at = when
        self.expires.put(entry)

    def remove_expire(self, entry: Entry) -> bool:
        """수명을 제거한다.

        Returns:
            제거할 수명이 있었으면 True.
        """
        if entry.expire_at is None:
            return False
        entry.expire_at = None
        self.expires.remove(entry.key)
        return True

    def db_delete(self, key: bytes) -> bool:
        """메모리의 항목을 지운다. DEL, 만료, 메모리 부족에 의한 제거가 모두 이 함수를 쓴다.

        만료 시각 테이블을 먼저 고쳐야 "만료 테이블의 포인터는 키 공간에도 있다"는 규칙이
        어느 순간에도 깨지지 않는다.

        Returns:
            지운 항목이 있었으면 True.
        """
        entry = self.keyspace.get(key)
        if entry is None:
            return False
        if entry.expire_at is not None:
            self.expires.remove(key)  # ① 만료 시각 테이블의 포인터
        self.keyspace.remove(key)  # ② 키 공간 테이블의 포인터
        self.used_memory -= len(key) + len(entry.value)  # ③ 항목 해제(이후 GC가 회수)
        return True

    def active_expire_cycle(self) -> int:
        """만료 시각 테이블을 조금씩 샘플링해 만료된 키를 지운다. 5초마다 호출된다.

        조회되지 않는 키는 lookup_key의 지연 삭제만으로는 영영 남기 때문에 필요하다.
        한 번에 다 훑으면 명령 처리가 멈추므로 표본의 만료 비율이 높을 때만 이어서 검사하고
        시간 상한을 둔다.

        Returns:
            이번 호출에서 지운 키의 수.
        """
        deadline = time.monotonic() + EXPIRE_CYCLE_TIME_LIMIT
        removed = 0
        while len(self.expires):
            sample, self._expire_cursor = self.expires.scan(self._expire_cursor, EXPIRE_CYCLE_SAMPLES)
            if not sample:
                continue  # 테이블 끝의 빈 버킷만 지났다. cursor가 0으로 돌아갔으니 다음엔 반드시 찾는다.
            now = self._clock()
            expired = [entry for entry in sample if self._is_expired(entry, now)]
            for entry in expired:
                self.db_delete(entry.key)
            removed += len(expired)
            if len(expired) / len(sample) <= EXPIRE_CYCLE_REPEAT_RATIO or time.monotonic() > deadline:
                break
        return removed

    def records(self) -> Iterator[tuple[bytes, bytes, int | None]]:
        """전체 데이터(메모리 + 디스크로 밀려난 항목)를 (키, 값, 만료 시각)으로 낸다.

        수명이 지났지만 아직 지워지지 않은 항목도 그대로 낸다. 복구된 뒤 지연 삭제나
        주기적 삭제가 처리한다.
        """
        for entry in self.keyspace:
            yield entry.key, entry.value, entry.expire_at
        if self.swap is not None:
            yield from self.swap.records()

    def _is_expired(self, entry: Entry, now: int) -> bool:
        return not self.loading and entry.expire_at is not None and entry.expire_at <= now

    def _swap_in(self, key: bytes) -> Entry | None:
        if self.swap is None or self.swap.count == 0:
            return None
        record = self.swap.read(key)
        if record is None:
            return None
        value, expire_at = record
        # 메모리에 올리는 데 성공한 뒤에 디스크 사본을 지운다. 순서가 반대면 공간 확보에
        # 실패했을 때 데이터가 어디에도 남지 않는다.
        entry = self.db_add(key, value)
        if expire_at is not None:
            self.set_expire(entry, expire_at)
        self.swap.remove(key)
        return entry

    def _make_room(self, needed: int, protect: bytes) -> None:
        """needed 바이트가 들어갈 공간을 마련한다.

        Args:
            needed: 추가로 필요한 바이트 수.
            protect: 지금 쓰려는 키. 자기 자신을 밀어내면 안 된다.

        Raises:
            OutOfMemory: noeviction 정책이거나 더 밀어낼 항목이 없는 경우.
        """
        if not self.maxmemory:
            return
        if needed > self.maxmemory:
            raise OutOfMemory  # 전부 밀어내도 들어가지 않으므로 다른 키를 건드리기 전에 거부한다.
        while self.used_memory + needed > self.maxmemory:
            if self.policy != POLICY_LRU or not self._evict_one(protect):
                raise OutOfMemory

    def _evict_one(self, protect: bytes) -> bool:
        """표본 중 가장 오래 접근하지 않은 항목을 디스크로 내보내고 메모리에서 지운다.

        전체에서 정확한 LRU를 찾으려면 매번 모든 키를 훑어야 하므로 Redis처럼 표본에서 고른다.
        """
        assert self.swap is not None
        sample, self._evict_cursor = self.keyspace.scan(self._evict_cursor, EVICTION_SAMPLES)
        candidates = [entry for entry in sample if entry.key != protect]
        if not candidates:
            # 표본이 보호 대상 키 하나뿐일 수 있으므로 전체에서 다시 찾는다.
            candidates = [entry for entry in self.keyspace if entry.key != protect]
            if not candidates:
                return False
        victim = min(candidates, key=lambda entry: entry.last_access)
        # 디스크에 먼저 쓴다. 쓰기가 실패하면 예외가 올라가고 메모리의 항목은 그대로 남는다.
        self.swap.put(victim.key, victim.value, victim.expire_at)
        self.db_delete(victim.key)
        return True
