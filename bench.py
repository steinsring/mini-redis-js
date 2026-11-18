"""성능 요구사항 검증용 부하 스크립트.

기준(REQUIREMENTS.md 3): 초당 1,000건의 요청에서 조회(GET)의 95%가 10ms 이내, 오류율 1% 이하.

실행 중인 서버에 대고 돌린다:
    python Redis.py --port 6379 &
    python bench.py --port 6379
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time

from resp import RespClient

KEYS = 1000
VALUE = "x" * 100


async def _worker(client: RespClient, start: float, interval: float, count: int, stats: dict) -> None:
    """정해진 시각표대로 요청을 보낸다.

    지연 시간을 "보낸 시각"이 아니라 "보내기로 한 시각"부터 잰다. 서버가 느려져 뒤 요청의
    출발이 밀린 시간까지 지연으로 잡아야 실제 사용자가 겪는 대기 시간이 된다.
    """
    for i in range(count):
        scheduled = start + i * interval
        delay = scheduled - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        key = f"bench:{random.randrange(KEYS)}"
        is_get = random.random() < 0.8  # 조회 80%, 저장 20%
        try:
            reply = await (client.call("GET", key) if is_get else client.call("SET", key, VALUE))
            ok = reply == (VALUE.encode() if is_get else "OK")
        except Exception:
            # 오류율로 집계하는 것이 목적이라 종류를 가리지 않고 실패 한 건으로 센다.
            ok = False
        stats["total"] += 1
        if not ok:
            stats["errors"] += 1
        else:
            stats["get_ms" if is_get else "set_ms"].append((time.perf_counter() - scheduled) * 1000)


def _summary(latencies: list[float]) -> tuple[float, float, float, float]:
    """정렬된 지연 시간에서 (p50, p95, p99, 최대)를 뽑는다. 비어 있으면 안 된다."""
    last = len(latencies) - 1
    p50, p95, p99 = (latencies[min(last, int(len(latencies) * q))] for q in (0.50, 0.95, 0.99))
    return p50, p95, p99, latencies[-1]


async def _run(args: argparse.Namespace) -> bool:
    clients = [await RespClient.connect(args.host, args.port) for _ in range(args.connections)]
    for i in range(KEYS):
        await clients[0].call("SET", f"bench:{i}", VALUE)

    stats: dict = {"total": 0, "errors": 0, "get_ms": [], "set_ms": []}
    per_connection = args.rate * args.duration // args.connections
    interval = args.connections / args.rate
    started = time.perf_counter()
    # 연결마다 출발 시각을 조금씩 어긋나게 해서 요청이 한 순간에 몰리지 않게 한다.
    await asyncio.gather(
        *(
            _worker(client, started + n * interval / args.connections, interval, per_connection, stats)
            for n, client in enumerate(clients)
        )
    )
    elapsed = time.perf_counter() - started
    for client in clients:
        await client.close()

    get_ms, set_ms = sorted(stats["get_ms"]), sorted(stats["set_ms"])
    if not get_ms:
        print("성공한 조회가 없어 측정할 수 없다")
        return False
    throughput = stats["total"] / elapsed
    error_rate = stats["errors"] / stats["total"]
    print(f"요청 {stats['total']}건 / {elapsed:.1f}초 = 초당 {throughput:.0f}건 (목표 {args.rate})")
    # 통과 여부는 요구사항대로 조회만 보지만, AOF처럼 쓰기에만 드는 비용은 조회 지연에 잘 드러나지 않으므로
    # 저장과 전체 요청의 지연도 함께 보여 준다.
    for name, latencies in (("전체", sorted(get_ms + set_ms)), ("조회", get_ms), ("저장", set_ms)):
        if latencies:
            print(f"{name} 지연 p50 %.2fms, p95 %.2fms, p99 %.2fms, 최대 %.2fms" % _summary(latencies))
    p95 = _summary(get_ms)[1]
    print(f"오류율 {error_rate:.3%} ({stats['errors']}건)")
    passed = throughput >= args.rate * 0.99 and p95 <= args.p95_ms and error_rate <= args.max_error_rate
    print("결과:", "통과" if passed else "미달")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(description="mini-Redis 부하 측정")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--rate", type=int, default=1000, help="초당 요청 수")
    parser.add_argument("--duration", type=int, default=10, help="초")
    parser.add_argument("--connections", type=int, default=20)
    parser.add_argument("--p95-ms", type=float, default=10.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    sys.exit(0 if asyncio.run(_run(parser.parse_args())) else 1)


if __name__ == "__main__":
    main()
