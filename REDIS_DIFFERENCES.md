# mini-Redis와 Redis의 차이점

이 문서는 이 저장소의 구현(mini-Redis)이 실제 Redis와 어디가 같고 어디가 다른지 정리한다.
Redis 쪽 설명은 오픈소스 Redis 7~8의 기본 설정을 기준으로 했고, 이 환경에는 Redis가 설치돼 있지 않아
실제 Redis와 나란히 실행해 비교하지는 않았다. `redis-cli`나 `redis-py` 같은 기존 클라이언트로 접속하는
것도 검증하지 않았다.

## 한눈에 보기

| 영역 | Redis | mini-Redis |
| --- | --- | --- |
| 구현 | C, 자체 이벤트 루프(epoll/kqueue) | Python, asyncio |
| 프로토콜 | RESP2, RESP3, 인라인 명령 | RESP2의 "bulk string 배열" 요청만 |
| 데이터 타입 | String, List, Hash, Set, Sorted Set, Stream 등 | String만 |
| 명령 수 | 수백 개 | 14개 |
| Key / Value 최대 크기 | 각각 512MB | Key 1KB, Value 1MiB (설정 가능) |
| 트랜잭션·스크립트 | MULTI/EXEC, WATCH, Lua, Functions | 없음. 명령 하나만 원자적 |
| 메모리 초과 시 | 정책에 따라 키를 **삭제**하거나 쓰기 거부 | 키를 **디스크로 옮기거나** 쓰기 거부 |
| 접속 한도 초과 시 | 오류를 보내고 연결을 끊음 | 대기열에 두고 순번을 알려 줌 |
| 백업 | RDB(백그라운드 fork), AOF(재작성으로 압축) | 자체 형식 RDB(동기 저장)와, 그 이후의 변경만 담는 AOF |
| 인증·암호화 | requirepass, ACL, TLS | 없음 (요구사항에서 제외) |
| 복제·분산 | Replication, Sentinel, Cluster | 없음 |
| 부가 인터페이스 | 없음 | FastAPI HTTP 게이트웨이 |

## 1. 프로토콜

| 항목 | Redis | mini-Redis |
| --- | --- | --- |
| 요청 형식 | RESP 배열과 인라인 명령(`PING\r\n`) 모두 허용 | RESP 배열만. 인라인 명령은 프로토콜 오류 |
| 응답 타입 | simple string, error, integer, bulk, array, RESP3 타입들 | simple string, error, integer, bulk, null bulk. 배열 응답을 내는 명령이 없다 |
| 프로토콜 버전 협상 | `HELLO`로 RESP3 전환 | 없음 |
| 인자 하나의 최대 크기 | 512MB (`proto-max-bulk-len`) | Value 한도와 같은 1MiB |
| 요청 버퍼 상한 | 1GB (`client-query-buffer-limit`) | Key 한도 + Value 한도 + 64KB |
| 프로토콜 오류 처리 | 오류 응답 후 연결 종료 | 같다. 추가로, 오류 응답이 확실히 전달되도록 쓰기만 먼저 닫고 최대 1초간 남은 입력을 버린 뒤 종료한다 |
| 파이프라이닝 | 지원 | 지원 (한 번에 도착한 명령을 순서대로 처리하고 응답을 묶어 보냄) |

## 2. 명령

mini-Redis가 지원하는 명령은 `PING`, `GET`, `SET`, `DEL`, `INCR`, `DECR`, `INCRBY`, `DECRBY`, `EXPIRE`,
`PEXPIREAT`, `TTL`, `PTTL`, `PERSIST`, `SAVE`가 전부다. 같은 이름의 명령도 다음이 다르다.

| 명령 | Redis | mini-Redis |
| --- | --- | --- |
| `PING` | `PING [message]` | 인자 없는 `PING`만 |
| `SET` 조건 | `NX`, `XX`. Redis 8.4부터 `IFEQ`, `IFNE`, `IFDEQ`, `IFDNE` | `NX`, `XX`, `IFEQ` |
| `SET` 수명 | `EX`, `PX`, `EXAT`, `PXAT`, `KEEPTTL` | `EX`, `PX`, `PXAT`, `KEEPTTL` |
| `SET ... GET` | 이전 값을 돌려주는 `GET` 옵션 지원 | 없음 |
| `EXPIRE` | `NX`, `XX`, `GT`, `LT` 옵션 지원(7.0+) | 옵션 없음 |
| `DEL` | 같음 | 같음 |
| `INCR` 계열 | 정수 인코딩으로 저장 | 10진 문자열로 저장. 범위와 오류 메시지는 같다 |
| `SAVE` | 같은 역할. 보통은 `BGSAVE`를 쓴다 | `SAVE`만 있다 |
| 알 수 없는 명령 | `ERR unknown command 'X', with args beginning with: ...` | `ERR unknown command 'X'` |

Redis에는 있지만 mini-Redis에 없는 것 가운데 자주 쓰는 것: `EXISTS`, `MGET`/`MSET`, `APPEND`, `KEYS`/`SCAN`,
`SELECT`(mini-Redis는 DB가 하나뿐이다), `FLUSHALL`, `INFO`, `CONFIG`, `CLIENT`, `COMMAND`, `AUTH`, `QUIT`,
`MULTI`/`EXEC`, `EVAL`, Pub/Sub, 그리고 String 외 모든 타입의 명령.

## 3. 저장 구조와 만료

| 항목 | Redis | mini-Redis |
| --- | --- | --- |
| 해시테이블 | `dict`. 테이블 두 개를 두고 조금씩 옮기는 점진적 리해시 | 직접 만든 체이닝 테이블. 크기를 바꿀 때 한 번에 전부 옮긴다(그 순간 지연 발생) |
| 값 표현 | 타입과 인코딩을 가진 객체(정수, 짧은 문자열 최적화 등) | Python `bytes` 하나 |
| 만료 시각의 위치 | Redis 7까지는 만료 테이블이 "키 → 만료 시각"을 직접 가진다 | ARCHITECTURE.md대로 항목 안에 두고, 만료 테이블은 항목 포인터만 가진다 |
| 항목 재할당 | 값이 커지면 객체 주소가 바뀌어 포인터를 고쳐야 한다 | Python 객체는 주소가 유지되어 포인터를 고치는 경로가 실행되지 않는다 |
| 지연 삭제 | 조회 시 만료 확인 후 삭제 | 같다 (`lookup_key`) |
| 주기적 정리 주기 | 초당 10회(`hz 10`) | 5초마다 |
| 정리 표본 | 한 번에 20개, 만료 비율이 기준(문서상 25%)을 넘으면 반복 | 한 번에 최대 10개, 20%를 넘으면 반복 |
| 정리 시간 상한 | 주기의 일정 비율(CPU 약 25%) | 호출당 30ms |
| 표본 선택 | 커서로 테이블을 이어서 훑음 | 같다. 다만 버킷 단위로만 끊어서 표본이 10개보다 적을 수 있다 |
| 만료의 기록 | 만료된 키를 `DEL`로 AOF·복제본에 전파 | 기록하지 않는다. 쓰기를 결과 상태로 기록하므로 `DEL` 없이도 재생 결과가 맞다 |
| 지난 시각으로 `EXPIRE` | 그 자리에서 키를 지운다 | 만료 시각만 기록한다. 삭제는 지연 삭제나 주기적 삭제가 한다 |
| 복구 시 이미 만료된 키 | RDB를 읽을 때 건너뛴다 | 그대로 올린 뒤 지연 삭제나 주기적 삭제가 지운다 |
| 복구 중 만료 | 로딩 중에는 만료를 적용하지 않음 | 같다 |

mini-Redis는 만료된 키를 지우는 경로를 지연 삭제와 주기적 삭제 둘로만 제한한다. 클라이언트가 보는 결과는
Redis와 같지만(만료된 키는 없는 키), 만료된 데이터가 메모리·RDB·스왑 파일에 남아 있는 시간이 더 길다.

만료된 키가 메모리에 남아 있는 시간은 mini-Redis 쪽이 훨씬 길 수 있다. 조회되지 않는 키는 최대 5초를
기다려야 정리 대상이 되고, 한 번에 훑는 양도 적다.

## 4. 메모리 한도와 정책

| 항목 | Redis | mini-Redis |
| --- | --- | --- |
| 사용량 계산 | 할당자가 실제로 쓴 메모리(구조체 오버헤드 포함) | Key 길이 + Value 길이의 합. 실제 프로세스 메모리는 이보다 훨씬 크다 |
| 정책 | `noeviction`, `allkeys-lru`, `allkeys-lfu`, `allkeys-random`, `volatile-lru`, `volatile-lfu`, `volatile-random`, `volatile-ttl` | `noeviction`, `allkeys-lru` |
| 밀려난 키 | **삭제된다.** 다시 조회하면 없는 키다 | **디스크(`swap/`)로 옮겨진다.** 다시 접근하면 메모리로 돌아온다 |
| LRU 근사 | 무작위 표본 5개와 후보 풀, 초 단위 LRU 시계 | 커서로 훑은 표본 5개, ms 단위 접근 시각, 후보 풀 없음 |
| 한도 확인 시점 | 명령 실행 전에 현재 사용량만 확인. 명령 하나만큼은 한도를 넘을 수 있다 | 쓰기 직전에 필요한 바이트를 정확히 계산. 한도를 넘지 않는다 |
| 거부 응답 | `-OOM command not allowed when used memory > 'maxmemory'.` | 같다 |

같은 `allkeys-lru`라는 이름을 쓰지만 의미가 다르다. Redis에서는 캐시처럼 데이터가 사라지는 정책이고,
mini-Redis에서는 데이터를 잃지 않는 대신 디스크 입출력 비용을 치르는 정책이다. 이에 따른 제약이 있다.

* 스왑 입출력은 명령 실행 중에 동기적으로 일어나 그동안 다른 명령이 기다린다.
* 메모리에 없는 Key를 조회할 때마다(스왑에 항목이 하나라도 있으면) 파일 존재 여부를 확인한다.
* 디스크로 나간 항목은 만료 시각 테이블에 없어 주기적 정리 대상이 아니다. 수명이 지났어도 다시 접근할 때까지
  파일로 남는다. 이미 만료된 항목도 밀려날 때는 지우지 않고 디스크로 옮긴다.
* `DEL`이나 `SET`도 대상이 디스크에 있으면 먼저 메모리로 올린 뒤 처리한다.

## 5. 백업과 복구

| 항목 | Redis | mini-Redis |
| --- | --- | --- |
| RDB 형식 | 버전이 있는 바이너리 형식, LZF 압축, CRC64 | 자체 형식(`MINIRDB1`), 압축 없음, CRC32. 서로 읽을 수 없다 |
| RDB 저장 방식 | `BGSAVE`: fork한 자식이 저장해 서비스가 멈추지 않는다 | 이벤트 루프 안에서 동기 저장. 저장하는 동안 모든 명령이 기다린다 |
| 자동 저장 조건 | "N초 동안 M회 이상 변경"의 조합(`save 3600 1 300 100 60 10000`) | "변경이 있고 N초 경과" 한 가지(`--save-interval`) |
| RDB와 AOF의 관계 | 각각이 전체 데이터의 사본이다. AOF를 켜면 AOF만으로 복구한다 | RDB는 마지막 저장 시점의 전체, AOF는 그 뒤의 변경분이다. 둘을 합쳐야 전체다 |
| AOF 구조 | 7.0부터 기반 파일 + 증분 파일 + 매니페스트(`appendonlydir/`) | 증분 파일 하나(`appendonly.aof`). 기반 역할은 `dump.rdb`가 한다 |
| AOF 압축 | `BGREWRITEAOF`로 현재 상태만 남기도록 재작성 | 재작성 없음. 대신 RDB를 저장할 때마다 AOF를 비운다. 자동 저장을 끄면(`--save-interval 0`) `SAVE` 전까지 계속 커진다 |
| AOF 동기화 | `appendfsync always / everysec / no`. everysec은 백그라운드 스레드. always는 한 번에 처리한 명령들을 모아 응답 전에 한 번 내린다 | `--appendfsync everysec / always`. `no`는 없다. everysec은 메인 스레드에서 내리고, always는 명령 하나마다 내린다 |
| 디스크에 내리는 방법 | AOF는 Linux `fdatasync`, macOS `F_FULLFSYNC`, 그 밖은 `fsync`. RDB는 어디서나 `fsync` | RDB와 AOF 모두 macOS `F_FULLFSYNC`, 그 밖은 `fsync`. RDB 저장 뒤에 AOF를 비우므로 둘을 같은 수준으로 맞췄다 |
| AOF에 남는 형태 | 받은 명령을 기록하되 상대 시간은 절대 시각으로 바꾼다. `INCR`은 `INCR`로 남는다 | 결과 상태를 기록한다. `INCR`도 `SET 키 결과값 [PXAT 만료시각]`으로 남는다 |
| 복구 순서 | AOF가 켜져 있으면 AOF, 아니면 RDB | RDB를 읽고 그 위에 AOF를 재생 |
| AOF를 끄고 재시작 | 남아 있는 AOF 파일을 무시한다 | 남아 있는 AOF까지 복구한 뒤 RDB로 저장하고 AOF 파일을 지운다 |
| 끝이 잘린 AOF | 잘린 부분을 버리고 시작(`aof-load-truncated yes`) | 같다 |
| 중간이 손상된 AOF | 시작 거부. `redis-check-aof`로 복구 | 시작 거부. 복구 도구는 없다 |
| AOF 쓰기 실패 | 복구될 때까지 이후 쓰기를 `MISCONF`로 거부(always면 종료) | 그 명령에만 `MISCONF`로 답한다(always에서 디스크에 내리지 못한 경우 포함). 메모리에는 이미 반영됐고 이후 쓰기를 막지 않는다 |
| 종료 시 저장 | 저장 조건이 설정돼 있으면 RDB 저장 | `--save-interval`이 0이 아니고 변경이 있으면 RDB 저장 |

## 6. 연결과 동시성

| 항목 | Redis | mini-Redis |
| --- | --- | --- |
| 명령 실행 | 단일 스레드 (I/O 스레드는 선택) | asyncio 단일 스레드 |
| 접속 한도(`maxclients`) 초과 | `-ERR max number of clients reached` 후 연결 종료 | 대기열에 넣고, 대기 중 명령에 `-QUEUED N번째로 대기중입니다.`로 응답. 자리가 나면 순서대로 처리 |
| 대기열 크기 | 해당 없음 | 제한 없음. 대기 연결이 계속 쌓일 수 있다 |
| 유휴 연결 정리 | `timeout`, TCP keepalive | 없음. 클라이언트가 끊을 때까지 유지 |
| 출력 버퍼 제한 | 느린 클라이언트를 끊는 한도가 있다 | 없음. `drain()`의 흐름 제어에만 의존 |
| 원자성 범위 | 명령 하나, MULTI/EXEC 묶음, 스크립트 | 명령 하나 |
| 값 비교 후 교체 | WATCH + MULTI, 스크립트, 8.4부터 `SET ... IFEQ` | `SET ... IFEQ` |

`-QUEUED` 응답은 Redis에 없는 mini-Redis 고유의 것이다. Redis에서 `QUEUED`는 MULTI 안에서 명령이 큐에
들어갔다는 **성공** 응답(`+QUEUED`)이므로, 이름은 같아도 뜻이 전혀 다르다.

## 7. 보안과 운영

* 인증(`AUTH`, ACL)과 TLS가 없다. 사용자 결정으로 요구사항에서 제외했다. 기본 수신 주소가 127.0.0.1이므로,
  `--host`를 외부 주소로 바꾸면 누구나 읽고 쓸 수 있다는 점에 유의해야 한다.
* 복제, Sentinel, Cluster가 없다. 서버 하나가 유일한 사본이다.
* `INFO`, `CONFIG`, `SLOWLOG`, `MONITOR` 같은 관측·운영 명령이 없다. 설정은 시작 옵션으로만 바꾼다.

## 8. HTTP 게이트웨이

Redis에는 HTTP 인터페이스가 없다. mini-Redis는 `fastapi_server.py`가 HTTP 요청을 RESP로 바꿔 전달한다.

* 게이트웨이는 Redis 연결 하나를 유지하고 요청을 한 번에 하나씩 보낸다. 게이트웨이를 거친 처리량은 이 직렬화에
  묶인다.
* 실패한 명령을 자동으로 다시 보내지 않는다. Redis가 재시작되면 그 사이의 요청은 503을 받고, 다음 요청부터
  새 연결로 처리된다.
* HTTP에서는 Key와 Value가 UTF-8 문자열이어야 한다. 바이너리 값은 RESP로 직접 다뤄야 한다.

## 9. 성능

Redis는 C로 작성됐고 mini-Redis는 Python 인터프리터 위에서 동작한다. 같은 장비에서 Redis를 함께 측정하지
않았으므로 두 구현의 처리량을 수치로 비교할 수는 없다. 아래는 mini-Redis만 로컬에서 잰 값이다.

| 측정(연결 20개, 세 조건을 번갈아 3회) | 메모리 전용 | AOF everysec | AOF always |
| --- | --- | --- | --- |
| 초당 1,000건(조회 80%)에서 전체 p95 | 0.49 / 0.49 / 0.49ms | 0.51 / 0.51 / 0.51ms | 26.0 / 41.7 / 20.9ms |
| 같은 부하에서 조회 p95 | 0.49 / 0.49 / 0.49ms | 0.49 / 0.49 / 0.49ms | 25.2 / 40.4 / 20.1ms |
| 같은 부하에서 조회 p99 | 0.60 / 0.58 / 0.60ms | 0.95 / 0.78 / 0.80ms | 32.7 / 53.3 / 34.8ms |
| 같은 부하에서 저장 p95 | 0.49 / 0.49 / 0.50ms | 0.55 / 0.55 / 0.57ms | 28.9 / 45.1 / 23.7ms |
| 같은 부하에서 오류율 | 0% | 0% | 0% |
| 쉬지 않고 보낼 때 초당 처리 건수(저장만) | 약 75,800건 | 약 53,900건 | 약 250건 |
| 쉬지 않고 보낼 때 초당 처리 건수(조회 80%) | 약 84,000건 | 약 73,900건 | 약 1,150건 |

* everysec은 초당 1,000건에서 p95까지 메모리 전용과 거의 같다. 1초에 한 번 메인 스레드에서 디스크에 내리는
  동안의 멈춤이 p99와 최대 지연에 나타난다. Redis는 이 작업을 백그라운드 스레드에서 한다.
* everysec의 처리량 비용은 쓰기마다 파일에 기록하고 flush하는 시간이다. 저장만 보내면 약 29%, 조회 80%
  구성으로는 약 12% 낮아진다.
* always는 저장 한 건마다 약 4ms 동안 서버 전체가 멈춰 저장이 초당 약 250건으로 떨어지고, 초당 1,000건
  부하에서 조회 p95가 성능 기준(10ms)을 넘긴다. macOS에서 장치 캐시까지 비우는 방식 기준이며, 다른
  운영체제나 디스크에서는 재 보지 않았다.
* 부하 발생기도 Python이라 처리 건수가 서버만의 한계라고 단정할 수는 없다.

큰 Value, 스왑이 활발한 상황, RDB 저장 중의 지연, 게이트웨이를 거친 경로의 성능은 측정하지 않았다.
