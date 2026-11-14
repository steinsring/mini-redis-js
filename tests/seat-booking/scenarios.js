const results = [];
const sample = { name: 'TEST FIXTURE', birth_date: '2000-01-01', phone: '000-0000-0000' };
const frames = [];

function assert(value, message) {
  if (!value) throw new Error(message);
}

function equal(actual, expected, label) {
  const received = JSON.stringify(actual);
  const wanted = JSON.stringify(expected);
  assert(received === wanted, label + ' 기대값 ' + wanted + ', 실제값 ' + received);
}

async function waitFor(predicate, label) {
  for (let attempt = 0; attempt < 100; attempt++) {
    const value = predicate();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  throw new Error(label + ' 확인 시간 초과');
}

async function api(path, options) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  return { status: response.status, data: await response.json() };
}

const post = (path, data) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
});
const state = async () => (await api('/__booking_test/state')).data;
const control = async data => equal((await post('/__booking_test/control', data)).status, 200, '테스트 환경');
const hold = (seat, user) => post('/api/holds', { seat_number: String(seat), user_id: user });
const complete = (token, overrides = {}) => post('/api/reservations', { token, ...sample, ...overrides });

async function makeHold(seat, user) {
  const response = await hold(seat, user);
  equal(response.status, 200, '임시점유 준비');
  return response.data.token;
}

function open(path) {
  const frame = document.createElement('iframe');
  const instrumentation = { elapsed: 0, requests: [] };
  frame.bookingTest = instrumentation;
  frame.src = path;
  document.body.append(frame);
  frames.push(frame);
  const doc = () => frame.contentDocument;
  return {
    frame, doc, requests: instrumentation.requests,
    path: () => frame.contentWindow.location.pathname,
    token: () => frame.contentWindow.location.hash.slice(1),
    seat: (number) => doc().querySelector('[data-seat="' + number + '"]'),
    text: selector => doc().querySelector(selector)?.textContent,
    button: label => [...doc().querySelectorAll('button')].find(node => node.textContent === label),
    tick(seconds) {
      instrumentation.elapsed = seconds * 1000;
      doc().dispatchEvent(new frame.contentWindow.Event('visibilitychange'));
    },
  };
}

async function booking() {
  const page = open('/booking');
  await waitFor(() => page.path() === '/booking' &&
    page.seat(1) && page.text('.map-status') === '', '좌석 상태 표시');
  return page;
}

async function select(page, number) {
  const length = page.requests.length;
  assert(page.seat(number)?.disabled === false, number + '번이 선택 가능한 좌석이어야 함');
  page.seat(number).click();
  return waitFor(() => page.requests.slice(length).find(request => request.path === '/api/price' &&
    request.body !== undefined && !page.text('.seat-price')?.includes('불러오는 중')), number + '번 가격 표시');
}

async function checkout(number = 1, user = 'test-user-a') {
  const page = await booking();
  page.doc().querySelector('#user-id').value = user;
  await select(page, number);
  page.button('예매하기').click();
  await waitFor(() => page.path() === '/checkout' && page.text('[role=timer]') && page.token(), '예매 정보 입력 페이지');
  return page;
}

function fill(page, missing = '') {
  for (const [id, value] of Object.entries({ name: sample.name, 'birth-date': sample.birth_date, phone: sample.phone })) {
    page.doc().getElementById(id).value = id === missing ? '' : value;
  }
}

const commands = (snapshot, field, command) => snapshot[field].filter(entry => entry[0] === command);

async function test(id, run) {
  try {
    await control({ reset: true });
    const detail = await run();
    results.push({ id, status: 'PASS', detail });
  } catch (error) {
    results.push({ id, status: 'FAIL', detail: error.message });
  } finally {
    frames.splice(0).forEach(frame => frame.remove());
  }
  await post('/__booking_test/report', results.at(-1));
}

await test('SEAT-01', async () => {
  const page = open('/');
  const link = await waitFor(() => page.doc().querySelector('a.booking-link'), '공연 첫 페이지 예매 링크');
  link.click();
  await waitFor(() => page.path() === '/booking' && page.seat(1), '예매 링크로 좌석 배치도 이동');
  return '공연 첫 페이지의 예매하기 클릭으로 /booking 이동 및 좌석 배치도 표시';
});

await test('SEAT-02', async () => {
  await makeHold(2, 'test-user-b');
  equal((await complete(await makeHold(3, 'test-user-c'))).status, 200, '3번 예매 완료 준비');
  const page = await booking();
  const snapshot = await state();
  const seatNumbers = [...page.doc().querySelectorAll('[data-seat]')].map(node => node.dataset.seat);
  equal(seatNumbers, snapshot.seats, '해시테이블의 좌석 번호와 화면 표시');
  const disabled = [...page.doc().querySelectorAll('[data-seat]:disabled')].map(node => node.dataset.seat);
  equal(disabled, snapshot.reserved, '저장 상태와 화면의 비활성 좌석');
  equal([page.seat(1).disabled, page.seat(2).disabled, page.seat(3).disabled], [false, true, true], '1번 가능·2번 점유·3번 예매 완료');
  return '예매 가능 1번만 선택 가능했고, 점유 2번과 예매 완료 3번이 비활성화됨';
});

await test('SEAT-03', async () => {
  await makeHold(2, 'test-user-b');
  await complete(await makeHold(3, 'test-user-c'));
  const page = await booking();
  await select(page, 1);
  const before = await state();
  const count = page.requests.length;
  page.seat(2).click();
  page.seat(3).click();
  const after = await state();
  equal([page.text('.seat-summary'), page.requests.length - count, after.lookups.length - before.lookups.length,
    after.queued.length - before.queued.length], ['1번 좌석', 0, 0, 0], '선택 불가 좌석 클릭 영향');
  return '선택된 1번 유지, 클릭한 2번과 3번에 가격 조회와 큐 등록 각각 0건';
});

await test('SEAT-04', async () => {
  const page = await booking();
  const { colors } = await state();
  const results = [];
  for (const [color, price] of [['purple', 100000], ['green', 50000], ['blue', 30000]]) {
    const number = colors[color];
    const record = await select(page, number);
    const snapshot = await state();
    const shown = page.text('.seat-price');
    const expected = new Intl.NumberFormat('ko-KR').format(price) + '원';
    results.push({ color, number, key: snapshot.lookups.at(-1), expected,
      status: record.status, price: record.body[number], shown });
  }
  equal(results.map(item => [item.key, item.status]), results.map(item => [item.number, 200]), '세 좌석의 해시테이블 key와 HTTP 응답');
  equal(results.map(item => item.price), [100000, 50000, 30000],
    '색상별 가격. ' + results.map(item => item.color + ' ' + item.number + '번 화면 ' + item.shown).join(', '));
  equal(results.map(item => item.shown), results.map(item => item.expected), '색상별 가격 표시');
  return '세 좌석에 각각 올바른 key가 전달됐고, 가격 100000·50000·30000을 받음';
});

await test('BOOK-01', async () => {
  const page = await booking();
  page.doc().querySelector('#user-id').value = 'test-user-a';
  await select(page, 1);
  const button = page.button('예매하기');
  await control({ pause: true });
  try {
    button.click();
    await waitFor(() => page.requests.some(record => record.method === 'POST' &&
      record.path === '/api/holds'), '임시 점유 요청 발신');
    equal(page.path(), '/booking', '임시 점유 응답 대기 중 페이지');
    equal(button.disabled, true, '응답 대기 중 예매 버튼 비활성화');
    button.click();
    equal(page.requests.filter(record => record.method === 'POST' && record.path === '/api/holds').length,
      1, '대기 중 연속 클릭으로 발생한 임시 점유 요청');
    equal((await state()).processed.length, 0, '서버 응답 전에는 처리된 요청이 없음');
  } finally {
    await control({ pause: false });
  }
  await waitFor(() => page.path() === '/checkout' && page.text('[role=timer]') && page.token(), '점유 성공 후 예매 정보 입력 페이지');
  const snapshot = await state();
  equal(commands(snapshot, 'queued', 'HOLD'), [['HOLD', ['test-user-a', '1']]], 'HOLD 큐 등록');
  equal(commands(snapshot, 'processed', 'HOLD').length, 1, 'HOLD 큐 처리 건수');
  assert(snapshot.reserved.includes('1') && page.text('.checkout-summary').includes('1번 좌석'), '임시점유와 해당 좌석 표시');
  const requests = page.requests.filter(record => record.path === '/api/holds');
  equal(requests.map(({ page, method, status }) => [page, method, status]),
    [['/booking', 'POST', 200], ['/checkout', 'GET', 200]], '점유 발급과 이동 후 기존 점유 조회 순서');
  equal(page.token(), requests[0].body.token, '입력 페이지가 최초 점유 토큰을 사용');
  return '처리 대기와 연속 클릭에도 좌석 선택 화면 유지. HOLD 1건 성공 후 이동해 같은 점유를 조회';
});

await test('BOOK-02', async () => {
  const outcomes = [];
  for (const occupied of ['임시 점유', '예매 완료']) {
    await control({ reset: true });
    const page = await booking();
    await select(page, 1);
    const token = await makeHold(1, 'test-user-b');
    if (occupied === '예매 완료') equal((await complete(token)).status, 200, '다른 사용자 예매 준비');
    assert(page.button('예매하기')?.disabled === false, '이전 화면에서 예매 버튼 사용 가능');
    page.button('예매하기').click();
    const notice = await waitFor(() => {
      const message = page.text('.action-reserve [role="status"]') || page.text('.availability-error');
      return message?.includes('다른 사용자') && message;
    }, occupied + ' 충돌 안내');
    const snapshot = await state();
    assert(snapshot.reserved.includes('1'), '먼저 점유한 1번이 유지돼야 함');
    equal(snapshot.saved['1']?.user_id ?? null, occupied === '예매 완료' ? 'test-user-b' : null, '기존 사용자 완료 기록');
    equal(page.requests.find(record => record.method === 'POST' && record.path === '/api/holds')?.status, 400, '충돌 응답');
    const path = page.path();
    outcomes.push(occupied + ' → ' + path + ', 안내=' + notice);
    if (path === '/booking') {
      assert(!page.doc().getElementById('name') && page.seat(2).disabled === false,
        '실패 뒤 좌석 선택 화면에서 다른 좌석을 선택할 수 있어야 함');
      await select(page, 2);
      page.button('예매하기').click();
      await waitFor(() => page.path() === '/checkout' &&
        page.text('.checkout-summary')?.includes('2번 좌석'), '실패 후 다른 좌석으로 예매 재시도');
    }
    page.frame.remove();
  }
  assert(outcomes.every(item => item.includes('→ /booking,')), '실패 시 좌석 선택 페이지 유지. 실제 ' + outcomes.join(' / '));
  return '두 종류의 충돌 모두 좌석 선택 화면에서 실패 안내. 같은 화면에서 다른 좌석 예매 가능';
});

await test('BOOK-03', async () => {
  await control({ pause: true });
  const first = hold(1, 'test-user-a');
  try {
    for (let attempt = 0; !commands(await state(), 'queued', 'HOLD').length && attempt < 60; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    const second = hold(1, 'test-user-b');
    for (let attempt = 0; commands(await state(), 'queued', 'HOLD').length < 2 && attempt < 60; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    const pending = await state();
    equal(commands(pending, 'queued', 'HOLD'), [['HOLD', ['test-user-a', '1']], ['HOLD', ['test-user-b', '1']]], '동시 대기 요청 순서');
    equal(pending.processed.length, 0, '두 요청이 대기 중이어야 함');
    await control({ pause: false });
    const [a, b] = await Promise.all([first, second]);
    equal([a.status, b.status, b.data.detail], [200, 400, 'ERR seat unavailable'], '한 사용자만 성공');
    const finished = await state();
    equal(finished.processed, pending.queued, 'FIFO 처리');
    equal(finished.reserved, ['1'], '점유 좌석');
    return 'A·B 두 요청이 동시에 대기했고 등록 순서대로 처리돼 A만 점유에 성공';
  } finally {
    await control({ pause: false });
  }
});

await test('HOLD-01', async () => {
  const page = await checkout();
  equal(page.text('[role=timer]'), '05:00', '입력 페이지 최초 타이머');
  await control({ clock: 299 });
  page.tick(299);
  await waitFor(() => page.text('[role=timer]') === '00:01', '4분 59초 후 남은 1초');
  const other = await booking();
  equal(other.seat(1).disabled, true, '다른 사용자 화면의 1번');
  equal((await hold(1, 'test-user-b')).status, 400, '다른 사용자의 점유 요청');
  return '05:00 시작, 4분 59초에 00:01 표시 및 다른 사용자 선택·점유 차단';
});

await test('HOLD-02', async () => {
  const page = await checkout();
  const token = page.token();
  page.button('예매 취소').click();
  await waitFor(() => page.path() === '/', '취소 후 공연 첫 페이지');
  const snapshot = await state();
  equal(commands(snapshot, 'queued', 'CANCEL'), [['CANCEL', [token]]], '취소 큐 등록');
  equal(commands(snapshot, 'processed', 'CANCEL').length, 1, '취소 큐 처리');
  equal((await hold(1, 'test-user-b')).status, 200, '취소 후 다른 사용자가 점유');
  return 'CANCEL 큐 1건 처리, 공연 첫 페이지로 복귀하고 다른 사용자가 좌석 점유';
});

await test('HOLD-03', async () => {
  const page = await checkout();
  const token = page.token();
  await control({ clock: 300 });
  page.tick(300);
  await waitFor(() => page.path() === '/', '5분 만료 후 공연 첫 페이지');
  equal((await complete(token)).status, 410, '만료 점유로 예매 완료');
  equal((await hold(1, 'test-user-b')).status, 200, '만료 뒤 좌석 재점유');
  return '5분에 공연 첫 페이지로 복귀, 이전 완료 요청 410, 다른 사용자는 점유 가능';
});

await test('HOLD-04', async () => {
  await complete(await makeHold(2, 'test-user-b'));
  const page = await checkout();
  const token = page.token();
  await control({ restart: true });
  const other = await booking();
  equal([other.seat(1).disabled, other.seat(2).disabled], [false, true], 'TCP 서버 재시작 후 실제 화면');
  equal((await complete(token)).status, 410, '재시작 전 임시 점유 토큰');
  equal((await state()).saved['2']?.user_id, 'test-user-b', '재시작 전 완료한 예매 복원');
  return '저장 서버 재시작 후 1번 점유 해제·이전 토큰 실패, 2번 완료 기록과 비활성 상태 유지';
});

await test('DONE-01', async () => {
  const page = await checkout();
  for (const field of ['name', 'birth-date', 'phone']) {
    fill(page, field);
    page.button('예매 완료').click();
    equal(page.doc().getElementById(field).validity.valueMissing, true, field + ' 필수 입력');
    assert(!page.text('.checkout-complete'), '필수 입력 누락이 성공으로 표시되면 안 됨');
  }
  const snapshot = await state();
  equal([page.requests.filter(request => request.path === '/api/reservations').length, commands(snapshot, 'queued', 'SET').length,
    Object.keys(snapshot.saved).length], [0, 0, 0], '누락 입력의 서버 요청·큐·JSON 기록');
  return '이름·생년월일·핸드폰 번호를 각각 비우면 입력 검증으로 차단되고 저장 0건';
});

await test('DONE-02', async () => {
  const page = await checkout();
  fill(page);
  page.button('예매 완료').click();
  await waitFor(() => page.text('.checkout-complete')?.includes('예매가 완료되었습니다'), '예매 성공 안내');
  const snapshot = await state();
  const request = page.requests.find(record => record.path === '/api/reservations');
  equal([request.status, request.body.success, commands(snapshot, 'queued', 'SET').length,
    commands(snapshot, 'processed', 'SET').length], [200, true, 1, 1], '완료 처리');
  equal(snapshot.saved, { '1': { user_id: 'test-user-a', ...sample } }, 'JSON 내용');
  equal((await booking()).seat(1).disabled, true, '완료 좌석 선택 불가');
  return 'SET 큐 1건, 성공 응답과 입력값이 일치하는 JSON 1건, 해당 좌석 비활성화';
});

await test('DONE-03', async () => {
  await complete(await makeHold(2, 'test-user-b'));
  const previous = (await state()).saved;
  const page = await checkout();
  fill(page);
  await control({ fail_save: true });
  page.button('예매 완료').click();
  await waitFor(() => page.text('.availability-error')?.includes('저장하지 못했습니다'), '저장 실패 안내');
  const response = page.requests.find(record => record.path === '/api/reservations');
  equal([response.status, response.body.detail, Boolean(page.text('.checkout-complete'))],
    [400, 'ERR could not save reservation', false], '저장 실패 응답과 표시');
  equal((await state()).saved, previous, '기존 JSON 보존');
  const active = await api('/api/holds', { headers: { 'X-Hold-Token': page.token() } });
  equal(active.status, 200, '좌석이 완료로 바뀌지 않고 기존 점유 유지');
  return '저장 실패 안내와 HTTP 오류 확인. 기존 기록 보존, 신규 완료 기록 0건';
});

await test('DONE-04', async () => {
  const first = await checkout(1, 'test-user-a');
  fill(first);
  first.button('예매 완료').click();
  await waitFor(() => first.text('.checkout-complete'), '첫 예매 성공');
  const other = await booking();
  other.doc().getElementById('user-id').value = 'test-user-a';
  await select(other, 2);
  other.button('예매하기').click();
  await waitFor(() => (other.text('.action-reserve [role="status"]') ||
    other.text('.availability-error'))?.includes('이 ID로 이미 예매'), '같은 ID 재예매 차단 안내');
  const record = other.requests.find(request => request.method === 'POST' && request.path === '/api/holds');
  equal([record.status, record.body.detail], [400, 'ERR user already has a reservation'], '같은 ID 두 번째 요청');
  equal(Object.keys((await state()).saved), ['1'], '같은 ID 완료 기록');
  return '같은 ID의 다른 좌석 요청이 거부됐고 첫 좌석 JSON 기록만 유지';
});

await post('/__booking_test/report', { finished: true });
