Krafton Jungle week11-12

## 테스트 티켓팅

브라우저의 mini-react 화면에서 좌석 가격 조회, 예매, 예매 내역 조회를 실행합니다.
FastAPI가 HTTP 요청을 RESP2로 변환해 Python asyncio TCP 서버로 전달합니다.

Python 3.10 이상에서 저장소 폴더를 연 뒤 의존성을 설치합니다.

```powershell
python -m pip install -r requirements.txt
```

같은 폴더에서 터미널 두 개를 열고 각각 실행합니다.

```powershell
python redis.py
```

```powershell
python -m uvicorn fastapi_server:app --host 127.0.0.1 --port 8000
```

Chrome에서 http://127.0.0.1:8000 을 엽니다. 공연 상세에서 **예매하기**를 눌러 좌석 배치도로 이동합니다. 배치도에는 참고 이미지의 사각 좌석 997개가 표시되며 보라색은 100,000원, 초록색은 50,000원, 하늘색은 30,000원입니다. 좌석을 바로 선택하면 서버에서 가격을 조회합니다. 임시점유 중이거나 예매가 끝난 좌석은 회색으로 표시됩니다. 좌석을 선택해 예매 정보 페이지에 들어가면 5분간 임시점유합니다. 이름, 생년월일, 휴대폰 번호를 입력하고 예매를 완료하세요. 취소하거나 5분이 지나면 공연 상세로 돌아갑니다. 테스트 사용자 ID로 `user-123`과 `user-456`을 사용할 수 있습니다.

## RESP2

TCP 서버는 `127.0.0.1:6379`에서 bulk string 배열을 받습니다. 아래 명령은 배열의 내용을 읽기 쉽게 적은 것입니다.

| 배열의 내용 | 응답 |
| --- | --- |
| `GET seat:1` | 좌석 번호와 가격 배열. 예: `["1", 50000]` |
| `HOLD user-123 1` | 임시점유 토큰, 좌석 번호, 가격, 남은 밀리초 배열 |
| `GET hold:<token>` | 현재 임시점유 정보 |
| `CANCEL <token>` | 임시점유 해제 후 `+OK` |
| `SET queue:<token> <name> <birth_date> <phone>` | FIFO 처리와 JSON 저장이 끝나면 `+OK`, 실패하면 `-ERR` |
| `GET reservations:user-123` | 예매한 좌석 번호 배열 |

완료된 예매는 예매 정보를 포함해 서버 파일 옆 `reservations.json`에 저장됩니다. 서버 재시작 시 완료된 예매를 복원하고 진행 중인 임시점유를 해제합니다.

