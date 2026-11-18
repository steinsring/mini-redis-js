# mini-Redis

학습용으로 만든 메모리 기반 Key-Value 저장소다. RESP2 프로토콜을 쓰는 TCP 서버(`Redis.py`)와,
HTTP 요청을 RESP로 바꿔 전달하는 FastAPI 게이트웨이(`fastapi_server.py`)로 이루어진다.

요구사항과 설계는 [REQUIREMENTS.md](REQUIREMENTS.md), [ARCHITECTURE.md](ARCHITECTURE.md)에,
실제 Redis와 다른 점은 [REDIS_DIFFERENCES.md](REDIS_DIFFERENCES.md)에 있다.

## 실행

Python 3.13 이상이 필요하다. 서버와 테스트는 표준 라이브러리만 쓰고, 게이트웨이만 fastapi·uvicorn을 쓴다.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python Redis.py --port 6379 --appendonly
```

```bash
MINI_REDIS_PORT=6379 .venv/bin/uvicorn fastapi_server:app --port 8000
```

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

```bash
.venv/bin/python bench.py --port 6379
```

| 서버 옵션 | 기본값 | 뜻 |
| --- | --- | --- |
| `--host`, `--port` | 127.0.0.1, 6379 | 수신 주소 |
| `--maxclients` | 10000 | 동시에 명령을 처리해 주는 연결 수. 넘치면 대기열로 간다 |
| `--maxmemory` | 0 (무제한) | 메모리 한도(바이트) |
| `--maxmemory-policy` | noeviction | `noeviction` 또는 `allkeys-lru` |
| `--dir` | data | RDB, AOF, 스왑 파일 위치 |
| `--appendonly` | 꺼짐 | AOF 사용 |
| `--save-interval` | 60 | 변경이 있을 때 RDB를 저장하는 주기(초). 0이면 자동 저장 안 함 |
| `--max-key-size`, `--max-value-size` | 1024, 1048576 | Key, Value 최대 바이트 수 |

## 구성

| 파일 | 역할 |
| --- | --- |
| `Redis.py` | TCP 서버. 연결 처리, 대기열, 주기 작업, 시작 시 복구 |
| `resp.py` | RESP2 파서·인코더와 최소 클라이언트 |
| `commands.py` | 명령 검증과 실행, AOF 전파 |
| `db.py` | 해시테이블, 키 공간·만료 시각 테이블, 만료 정리, 메모리 한도 |
| `persistence.py` | 스왑 저장소, RDB, AOF |
| `fastapi_server.py` | HTTP → RESP 게이트웨이 |
| `bench.py` | 성능 기준 검증용 부하 스크립트 |

## 동작 정의

### 데이터 타입과 크기 제한

* Key와 Value는 모두 바이너리 안전한 바이트 문자열이다. 다른 타입(List, Hash 등)은 없다.
* Key는 0~1,024바이트, Value는 0~1,048,576바이트(1MiB)다. 옵션으로 바꿀 수 있다.
* Key가 한도를 넘으면 `-ERR key exceeds size limit (max 1024 bytes)`로 답하고 연결은 유지한다.
* Value(정확히는 인자 하나)가 한도를 넘으면 `-ERR Protocol error: invalid bulk length (max 1048576 bytes)`로
  답하고 그 연결을 닫는다. 본문을 받기 전에 길이만 보고 거부해 메모리를 지키기 위해서다.
* 정수 연산의 대상은 부호 있는 64비트 정수의 10진 문자열이다. 공백, `+`, 앞자리 0, 소수는 정수가 아니다.

### 명령

| 명령 | 응답 |
| --- | --- |
| `PING` | `+PONG` |
| `GET key` | 값. 없으면 null(`$-1`) |
| `SET key value [NX \| XX \| IFEQ 비교값] [EX 초 \| PX 밀리초 \| PXAT 시각ms \| KEEPTTL]` | `+OK`. 조건이 맞지 않으면 null |
| `DEL key [key ...]` | 지운 키의 수. 없는 키는 0 |
| `INCR key`, `DECR key`, `INCRBY key n`, `DECRBY key n` | 연산 후 값. 없는 키는 0에서 시작 |
| `EXPIRE key 초`, `PEXPIREAT key 시각ms` | 설정했으면 1, 키가 없으면 0. 이미 지난 시각이어도 1이고, 그 키는 만료된 키로 취급된다 |
| `TTL key`, `PTTL key` | 남은 수명. 수명이 없으면 -1, 키가 없으면 -2 |
| `PERSIST key` | 수명을 지웠으면 1, 아니면 0 |
| `SAVE` | RDB 저장 후 `+OK` |

* 없는 Key의 조회는 null, 삭제는 0으로 서로 구분된다.
* 지원하지 않는 명령은 `-ERR unknown command '이름'`, 인자 개수가 틀리면
  `-ERR wrong number of arguments for '이름' command`, 옵션이 틀리면 `-ERR syntax error`,
  숫자가 아니면 `-ERR value is not an integer or out of range`로 구분해 답한다.
* `SET`은 기존 수명을 지운다(`KEEPTTL`이면 유지). `INCR` 계열은 수명을 건드리지 않는다.

### 만료

* 만료된 키를 지우는 경로는 두 가지뿐이다.
  * 지연 삭제: 명령이 그 키에 접근할 때 만료 시각을 확인해 지운다.
  * 주기적 삭제: 5초마다 만료 시각 테이블에서 최대 10개씩 표본을 뽑아 지운다. 표본의 20%를 넘게 만료돼
    있으면 이어서 검사하고, 30ms를 넘기면 멈춘다.
* 그 밖의 경로는 만료 여부를 따지지 않는다. 만료됐지만 아직 지워지지 않은 키는 RDB에 그대로 저장되고,
  재시작 때 그대로 올라오며, 메모리가 모자라면 그대로 디스크로 나간다. 만료를 AOF에 따로 기록하지도 않는다.
* 어느 경우든 클라이언트에게 만료된 키는 없는 키로 보인다.

### 원자성 범위

* 원자성의 단위는 **명령 하나**다. 명령은 단일 스레드에서 하나씩 끝까지 실행되므로, 한 명령의 중간 상태는
  다른 클라이언트에게 보이지 않는다.
* 따라서 `INCR` 계열(읽기-계산-쓰기)과 조건부 `SET`(`NX`, `XX`, `IFEQ`: 조건 확인-쓰기)은 동시 요청에서도
  갱신을 잃지 않는다.
* 여러 명령을 묶은 원자성(트랜잭션)은 제공하지 않는다. `GET` 후 `SET`처럼 두 명령 사이에는 다른 클라이언트의
  명령이 끼어들 수 있으므로, 그런 경우 `SET ... IFEQ`로 비교 후 교체를 반복해야 한다.

### 처리 실패 시 데이터 변경 여부

| 응답 | 데이터 |
| --- | --- |
| `-ERR ...`, `-OOM ...` | 바뀌지 않는다. 모든 명령은 검증을 끝낸 뒤에 변경을 시작한다 |
| `-ERR internal error` | 바뀌지 않는다. 예상하지 못한 내부 오류이며 서버와 다른 연결은 계속 동작한다 |
| `-MISCONF ...` | **메모리에는 반영됐지만** AOF에 쓰지 못했다. 재시작하면 이 변경은 사라질 수 있다 |
| `-QUEUED ...` | 바뀌지 않는다. 명령을 실행하지 않았다 |
| 응답 없이 연결 끊김 | 서버가 받은 완전한 명령까지만 반영된다. 덜 도착한 명령은 버린다 |

### 처리 한도와 대기열

* 처리 한도는 동시 연결 수(`--maxclients`)다.
* 한도를 넘은 연결은 끊지 않고 도착 순서대로 대기열에 둔다. 대기 중인 연결이 보낸 명령은 실행하지 않고
  `-QUEUED N번째로 대기중입니다.`로 답한다(N은 1부터).
* 처리 중인 연결이 끝나면 가장 오래 기다린 연결이 자리를 넘겨받아 이후 명령부터 정상 처리된다.
* 게이트웨이는 이 응답을 HTTP 503과 `{"detail": "N번째로 대기중입니다."}`로 전달한다.

### 메모리 한도

* 사용량은 메모리에 있는 항목의 Key 길이 + Value 길이의 합이다(객체 오버헤드는 세지 않는 근사치).
* `noeviction`: 쓰기가 한도를 넘기게 되면 `-OOM command not allowed when used memory > 'maxmemory'.`로 거부한다.
* `allkeys-lru`: 표본 5개 중 가장 오래 접근하지 않은 항목을 `<dir>/swap`의 파일로 내보내고 메모리에서 지운다.
  그 Key를 다시 쓰는 명령이 오면 디스크에서 읽어 메모리로 올린다. 더 내보낼 항목이 없으면 `-OOM`으로 거부한다.
* 스왑은 메모리의 연장이지 백업이 아니다. 재시작하면 스왑 파일은 지우고 RDB/AOF에서 복구한다.

### 백업과 복구

* **RDB**(`dump.rdb`): `SAVE` 명령, `--save-interval` 주기, 정상 종료 시점에 전체 데이터(스왑 포함)를 쓴다.
  임시 파일에 쓴 뒤 이름을 바꾸므로 저장 중 실패해도 이전 파일이 남는다.
* **AOF**(`appendonly.aof`): **마지막 RDB 저장 이후의 변경만** 담는다. RDB 저장이 끝나면 AOF를 비운다.
  명령마다 OS 버퍼로 내려보내고 1초마다 fsync한다. 서버 프로세스만 죽으면 승인된 쓰기를 모두 복구하고,
  전원 장애에서는 마지막 최대 1초를 잃을 수 있다.
* AOF에는 받은 명령이 아니라 결과 상태를 기록한다. 예를 들어 `INCR n`의 결과가 3이면 `SET n 3`을, 수명이
  있는 키면 `SET n 3 PXAT 만료시각`을 남긴다. 조건(`NX`, `IFEQ`)과 상대 시간(`EX`)도 결과만 남는다.
* 재시작 시 RDB를 먼저 읽고 그 위에 AOF를 재생한다. 둘을 합쳐야 전체 데이터이므로 `dump.rdb`와
  `appendonly.aof`는 함께 보관해야 한다.
* RDB 저장 직후 AOF를 비우기 전에 서버가 죽으면 이미 RDB에 담긴 기록이 다시 재생된다. 기록이 결과 상태라서
  다시 재생해도 값이 달라지지 않는다.
* `--appendonly` 없이 시작했는데 이전 실행의 AOF 파일이 남아 있으면, 그 내용까지 복구한 뒤 RDB로 저장하고
  AOF 파일을 지운다.
* `--save-interval 0`이면 `SAVE`를 실행하기 전까지 AOF가 계속 커진다.
* 만료 시각은 절대 시각으로 저장되므로 꺼져 있던 시간만큼 수명이 줄어든 상태로 복구된다.
* 백업 파일이 손상됐으면 빈 상태로 시작하지 않고 시작을 중단한다. 단, AOF의 마지막 명령이 쓰다 만 상태인
  경우는 그 부분만 잘라 내고 시작한다.

### HTTP API

Key와 Value는 UTF-8 문자열이고, Key에 `/`는 쓸 수 없다.

| 요청 | 성공 | 실패 |
| --- | --- | --- |
| `GET /keys/{key}` | 200 `{"key", "value"}` | 404 없는 키 |
| `PUT /keys/{key}` `{"value", "ttl_seconds"?, "mode"?: "nx"\|"xx", "if_equals"?}` | 200 `{"key", "stored": true}` | 409 조건 불일치, 413 크기 초과 |
| `DELETE /keys/{key}` | 200 `{"key", "deleted": true}` | 404 없는 키 |
| `POST /keys/{key}/incr` `{"amount"?: 1}` | 200 `{"key", "value"}` | 400 정수가 아닌 값 |

공통 실패: 400 잘못된 요청, 422 본문 형식 오류, 503 대기 중이거나 Redis에 연결할 수 없음, 507 메모리 부족.

## 성능 측정

로컬(macOS, Python 3.14)에서 서버와 부하 발생기를 별도 프로세스로 돌린 결과다. 두 조건을 번갈아 3회씩 쟀다.

**요구사항 기준** (`bench.py`: 초당 1,000건, 연결 20개, 조회 80% / 저장 20%, 회당 5초)

| 조건 | 조회 p95 (3회) | 오류율 | 기준(10ms, 1%) |
| --- | --- | --- | --- |
| 메모리 전용 | 0.54 / 0.58 / 0.57ms | 0% | 통과 |
| AOF 사용 | 0.56 / 0.59 / 0.55ms | 0% | 통과 |

이 부하에서는 두 조건의 차이가 측정 오차 안에 있다. 서버가 대부분 쉬고 있어서, 여기서 재는 지연은 명령 처리
시간보다 대기에서 깨어나는 시간에 좌우되는 것으로 보인다. AOF의 비용은 아래 최대 처리량에서 드러난다.

**최대 처리량** (연결 20개가 쉬지 않고 요청, 값 100바이트, 회당 3초, 3회 평균)

| 조건 | `SET` | `GET` |
| --- | --- | --- |
| 메모리 전용 | 초당 약 75,700건 | 초당 약 87,500건 |
| AOF 사용 | 초당 약 54,200건 | 초당 약 87,300건 |

AOF를 켜면 쓰기마다 파일에 기록하고 flush하므로 `SET` 처리량이 약 29% 낮아진다. `GET`은 AOF에 쓰지 않아
영향이 없다. 부하 발생기도 Python이라 이 수치가 서버만의 한계라고 단정할 수는 없다. 최대 처리량을 잰
스크립트는 저장소에 포함돼 있지 않다.
