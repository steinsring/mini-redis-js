# mini-redis-js

Python 표준 라이브러리로 만든 학습용 인메모리 key/value TCP 서버입니다.
RESP2로 GET, SET, DEL을 처리합니다. 외부 패키지 설치는 필요하지 않습니다.

## 실행

Python 3.10 이상에서 저장소 루트에서 실행합니다.

```sh
python redis.py
```

기본 주소는 `127.0.0.1:6379`입니다. 별도로 설치한 `redis-cli`로 접속할 수 있습니다.

```sh
redis-cli SET greeting hello
redis-cli GET greeting
redis-cli DEL greeting
```

| 명령 | 응답 |
| --- | --- |
| `SET key value` | `+OK` (기존 값 덮어쓰기) |
| `GET key` | bulk string, 없는 키는 null bulk string |
| `DEL key [key ...]` | 실제 삭제한 키 개수 (중복 키는 한 번만 계산) |

명령 이름은 대소문자를 구분하지 않습니다. 키와 값은 바이너리이며 빈 값과
빈 키도 허용합니다. 키 접두어는 특별한 의미가 없습니다. 잘못된 인자와
지원하지 않는 명령은 RESP 오류를 반환합니다. 클라이언트 간 데이터는 공유하고,
서버를 재시작하면 메모리 데이터는 사라집니다.

## 라이브러리 API

```python
import asyncio
from mini_redis import KeyValueStore, start_server

async def main():
    store = KeyValueStore()
    async with start_server(store.execute, host="127.0.0.1", port=6380) as server:
        await server.serve_forever()

asyncio.run(main())
```

`start_server(handler, host, port)`는 비동기 RESP 핸들러를 받는 async context
manager입니다. 컨텍스트 진입 후 접속할 수 있으며, `port=0`이면 `server.port`로
할당된 포트를 확인합니다. 종료 시 리스너와 열린 클라이언트도 정리합니다.

`mini_redis`는 RESP 타입, `encode`, `read_value`도 공개합니다. RESP2의 simple
string, error, signed 64-bit integer, binary bulk string, null, 중첩 배열을
처리합니다. 디코더는 루트 기준 깊이 128 초과를 거부합니다.

## 테스트

```sh
python -B -S -m unittest discover -s tests -v
```

메모리 저장소, 실제 TCP 연결과 파이프라이닝, 연결 종료, 잘못된 프레임,
바이너리 데이터, RESP 경계와 외부 패키지 없는 독립 실행을 검증합니다.

## 범위

이 저장소는 KV/RESP/TCP 코어만 포함합니다. 예매 정책, HTTP 서버, 웹 화면,
React 런타임과 응용 데이터 저장 코드는 포함하지 않습니다.
전체 Redis 호환 서버는 아니며 TTL, RESP3, 인증, 트랜잭션, Pub/Sub,
영속 저장과 클러스터링은 지원하지 않습니다.
