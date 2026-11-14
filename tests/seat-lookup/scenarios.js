const results = [];
const endpoint = '/__seat_tests';

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function equal(actual, expected, label) {
  const received = JSON.stringify(actual);
  const wanted = JSON.stringify(expected);
  assert(received === wanted, `${label} 기대값 ${wanted}, 실제값 ${received}`);
}

async function waitFor(predicate, label) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  throw new Error(`${label} 확인 시간 초과`);
}

async function json(path, options) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  const body = await response.json();
  assert(response.ok, `${path} HTTP ${response.status} ${JSON.stringify(body)}`);
  return body;
}

async function post(path, data) {
  return json(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

async function reserveAsAnotherUser(number = 1) {
  const hold = await post('/api/holds', { seat_number: String(number), user_id: 'seat-test-other-user-' + number });
  const reservation = await post('/api/reservations', {
    token: hold.token,
    name: 'TEST FIXTURE',
    birth_date: '2000-01-01',
    phone: '000-0000-0000',
  });
  equal(reservation.success, true, '다른 사용자의 테스트 예매');
}

async function test(id, run) {
  const frame = document.createElement('iframe');
  try {
    const doc = () => frame.contentDocument;
    const win = () => frame.contentWindow;
    const seat = number => doc().querySelector(`[data-seat="${number}"]`);
    async function openBooking() {
      frame.remove();
      await post(`${endpoint}/reset`, {});
      frame.seatPriceRequests = [];
      frame.seatStatusRequests = [];
      frame.src = '/booking?manual-seat-poll';
      document.body.append(frame);
      await waitFor(() => seat(1)?.disabled === false && seat(2)?.disabled === true && seat(10)?.disabled === false,
        '1번·10번 선택 가능, 2번 선택 불가');
    }
    await openBooking();
    const prices = () => frame.seatPriceRequests;
    const statusRequests = () => frame.seatStatusRequests.length;
    async function clickSeat(number) {
      const count = prices().length;
      seat(number).click();
      await waitFor(() => win().location.pathname === '/checkout' ||
        prices()?.[count]?.finished, `${number}번 클릭 응답 또는 예매 페이지 이동`);
      return prices()?.[count];
    }
    const detail = await run({ doc, win, seat, prices, statusRequests, clickSeat, openBooking });
    results.push({ id, status: 'PASS', detail });
  } catch (error) {
    results.push({ id, status: 'FAIL', detail: error.message });
  } finally {
    frame.remove();
  }
}

await test('SEAT-01', async ({ doc, win, clickSeat }) => {
  const price = await clickSeat(1);
  equal([win().location.pathname, price?.status, price?.body], ['/booking', 200, { '1': 30000 }],
    '좌석 선택 직후 경로와 조회 결과');
  await waitFor(() => doc().querySelector('.seat-summary')?.textContent === '1번 좌석' &&
    doc().querySelector('.action-reserve button')?.disabled === false, '1번 선택 및 예매 버튼 활성화');
  doc().querySelector('.action-reserve button').click();
  await waitFor(() => win().location.pathname === '/checkout' &&
    doc().querySelector('.checkout-summary')?.textContent.includes('1번 좌석') &&
    doc().querySelector('[role=timer]'), '점유 성공 뒤 1번 예매 입력 페이지');
  return '좌석을 선택하면 가격만 표시되고, 예매하기를 눌러 점유에 성공한 뒤 입력 페이지로 이동';
});

await test('SEAT-02', async ({ clickSeat }) => {
  await clickSeat(10);
  const observed = await json(`${endpoint}/lookups`);
  equal(observed, [{ key: '10', price: 100000, reserved: false }], '해시테이블 조회 기록');
  return '실제 해시테이블에서 좌석 번호 문자열 "10"으로 한 번 조회해 10번의 100,000원 레코드를 읽음';
});

await test('SEAT-03', async ({ doc, win, clickSeat, openBooking }) => {
  for (const [number, price, formatted] of [[1, 30000, '30,000원'], [10, 100000, '100,000원']]) {
    if (number === 10) await openBooking();
    const result = await clickSeat(number);
    equal([result.seat, result.status, result.body], [String(number), 200, { [number]: price }], '브라우저가 받은 가격 응답');
    await waitFor(() => doc().querySelector('.seat-price')?.textContent === formatted ||
      (win().location.pathname === '/checkout' && doc().querySelector('#app')?.textContent.includes(formatted)),
    `${number}번의 화면 가격 ${formatted}`);
  }
  return '1번은 숫자 30000, 10번은 숫자 100000을 받았고 화면에도 해당 금액이 표시됨';
});

await test('SEAT-04', async ({ doc, seat, clickSeat, openBooking }) => {
  await reserveAsAnotherUser();
  assert(!seat(1).disabled, '클릭 직전에는 1번이 선택 가능한 이전 화면이어야 함');
  const reserved = await clickSeat(1);
  equal(await json(endpoint + '/lookups'), [{ key: '1', price: 30000, reserved: true }],
    '다른 사용자가 예매한 뒤 조회한 레코드');
  equal([reserved.status, reserved.body], [400, { detail: 'ERR seat unavailable' }], '예매 완료 오류와 가격 반환 없음');

  await openBooking();
  await post('/api/holds', { seat_number: '1', user_id: 'seat-test-holding-user' });
  assert(!seat(1).disabled, '클릭 직전에는 점유 변경이 화면에 반영되지 않아야 함');
  const held = await clickSeat(1);
  equal([held.status, held.body], [400, { detail: 'ERR seat held' }], '임시 점유 오류와 가격 반환 없음');
  await waitFor(() => doc().body.textContent.includes('임시 점유') && seat(1).disabled, '임시 점유 안내와 비활성화');
  return '저장소에서 예매 완료와 임시 점유를 구별하고 두 경우 모두 가격 반환을 차단함';
});

await test('SEAT-05', async ({ doc, seat, statusRequests, clickSeat }) => {
  const disabledSeats = () => [...doc().querySelectorAll('[data-seat]:disabled')].map(node => node.dataset.seat);
  equal(disabledSeats(), ['2'], '초기 화면에서 예매된 좌석');
  await reserveAsAnotherUser(1);
  await reserveAsAnotherUser(3);
  equal([seat(1).disabled, seat(3).disabled], [false, false], '예매 후 아직 갱신되지 않은 화면');
  const before = statusRequests();
  await clickSeat(1);
  await waitFor(() => doc().body.textContent.includes('이미 예매된 좌석입니다'), '예매 완료 안내');
  await waitFor(() => seat(1).disabled && seat(3).disabled, '즉시 전체 좌석 재조회와 화면 갱신');
  assert(statusRequests() > before, '가격 조회 충돌 직후 전체 좌석 상태 API를 호출해야 함');
  equal(disabledSeats(), ['1', '2', '3'], '전체 좌석 갱신 결과');
  assert(seat(1).classList.contains('seat-booked'), '1번에 예매 완료 스타일 적용');
  return '예매 완료 안내와 전체 좌석 즉시 재조회로 클릭한 1번과 다른 좌석 3번이 함께 비활성화됨';
});

await test('SEAT-06', async ({ seat, prices, clickSeat }) => {
  assert(seat(2).disabled, '처음부터 예매된 2번은 비활성화되어야 함');
  seat(2).click();
  equal([prices().length, (await json(endpoint + '/lookups')).length], [0, 0], '초기 예매 좌석의 조회');
  await reserveAsAnotherUser();
  await clickSeat(1);
  await waitFor(() => seat(1).disabled, '충돌 후 1번 비활성화');
  const previous = prices().length;
  const previousLookups = (await json(endpoint + '/lookups')).length;
  seat(1).click();
  seat(2).click();
  equal([prices().length - previous, (await json(endpoint + '/lookups')).length - previousLookups],
    [0, 0], '비활성 좌석 재선택의 가격 요청과 해시테이블 조회');
  return '처음부터 예매된 2번과 클릭으로 갱신된 1번 모두 재선택 시 가격 조회와 반환 0건';
});

const report = new TextEncoder().encode(JSON.stringify(results));
document.body.dataset.seatReport = btoa(String.fromCharCode(...report));
document.getElementById('result').textContent = JSON.stringify(results, null, 2);
