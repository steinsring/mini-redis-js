import { FunctionComponent, h, useEffect, useState } from './mini-react/phase2/runtime.js';
import { Header, Icon } from './common.js';

const won = new Intl.NumberFormat('ko-KR');

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, cache: 'no-store',
    headers: options.headers });
  if (response.status === 410) {
    window.location.replace('/');
    return null;
  }
  const data = await response.json();
  if (!response.ok) {
    const messages = {
      'ERR seat unavailable': '다른 사용자가 이 좌석을 선택했습니다. 좌석을 다시 선택하세요.',
      'ERR user already has a reservation': '이 ID로 이미 예매했거나 예매를 진행 중입니다.',
      'ERR invalid reservation details': '이름, 생년월일, 휴대폰 번호를 확인하세요.',
      'ERR could not save reservation': '예매 정보를 저장하지 못했습니다. 다시 시도하세요.',
    };
    throw new Error(messages[data.detail] ?? '요청을 처리하지 못했습니다. 다시 시도하세요.');
  }
  return data;
}

function Input({ id, label, type, autocomplete }) {
  return h('div', { className: 'field' },
    h('label', { htmlFor: id }, label),
    h('input', { id, type, autocomplete, required: true }));
}

function readDetails(token) {
  const name = document.getElementById('name');
  const birthDate = document.getElementById('birth-date');
  const phone = document.getElementById('phone');
  if (![name, birthDate, phone].every(input => input.reportValidity())) return null;
  return { token, name: name.value.trim(), birth_date: birthDate.value, phone: phone.value.trim() };
}

function formatTime(seconds) {
  return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
}

function App() {
  const [hold, setHold] = useState(null);
  const [now, setNow] = useState(performance.now());
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);
  const [error, setError] = useState('');
  const remaining = hold ? Math.max(0, Math.ceil((hold.deadline - now) / 1000)) : 0;

  useEffect(() => {
    const controller = new AbortController();
    const token = window.location.hash.slice(1);
    if (!token) {
      window.location.replace('/');
      return;
    }
    request('/api/holds', {
      headers: { 'X-Hold-Token': token },
      signal: controller.signal,
    }).then(result => {
      if (result && !controller.signal.aborted) {
        const received = performance.now();
        setNow(received);
        setHold({ ...result, deadline: received + result.remaining_ms });
      }
    }).catch(failure => {
      if (!controller.signal.aborted) setError(failure.message);
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!hold || complete) return;
    const tick = () => setNow(performance.now());
    const interval = setInterval(tick, 250);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(interval);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [hold, complete]);

  useEffect(() => {
    if (hold && !remaining && !busy && !complete) window.location.replace('/');
  }, [hold, remaining, busy, complete]);

  async function cancel() {
    setBusy(true);
    setError('');
    try {
      await request('/api/holds', { method: 'DELETE', headers: { 'X-Hold-Token': hold.token } });
      window.location.replace('/');
    } catch (failure) {
      setError(failure.message);
      setBusy(false);
    }
  }

  async function reserve() {
    const details = readDetails(hold.token);
    if (!details || !remaining || busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await request('/api/reservations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(details),
      });
      if (result?.success) setComplete(true);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  return h('div', null, h(Header),
    h('main', { className: 'ticketing checkout' },
      h('h1', { className: 'booking-page-title' }, complete ? '예매 완료' : '예매 정보 입력'),
      h('section', { className: 'booking checkout-panel', 'aria-busy': busy },
        h('p', { className: 'booking-concert' }, 'AFTERGLOW LIVE'),
        h('div', { className: 'session' }, h(Icon, { name: 'calendar' }), '12.12 (토) 19:00'),
        hold ? h('div', { className: 'checkout-summary' },
          h('p', null, hold.seat_number + '번 좌석'),
          h('strong', null, won.format(hold.price) + '원')) : null,
        complete ? h('div', { className: 'checkout-complete' },
          h('p', { role: 'status' }, '예매가 완료되었습니다.'),
          h('a', { href: '/', className: 'booking-link' }, '공연 상세로 돌아가기'))
          : hold ? [
            h('div', { className: 'checkout-timer' },
              h('span', null, '남은 시간'),
              h('strong', { role: 'timer' }, formatTime(remaining))),
            h('div', { className: 'checkout-fields' },
              h(Input, { id: 'name', label: '이름', type: 'text', autocomplete: 'name' }),
              h(Input, { id: 'birth-date', label: '생년월일', type: 'date', autocomplete: 'bday' }),
              h(Input, { id: 'phone', label: '휴대폰 번호', type: 'tel', autocomplete: 'tel' })),
            h('div', { className: 'checkout-actions' },
              h('button', { type: 'button', disabled: busy, onClick: cancel }, '예매 취소'),
              h('div', { className: 'action-reserve' },
                h('button', { type: 'button', disabled: busy || !remaining, onClick: reserve }, '예매 완료'))),
          ] : error ? h('a', { href: '/booking', className: 'back-link' }, '좌석 다시 선택')
            : h('p', { role: 'status' }, '좌석을 확인하고 있습니다…'),
        h('p', { className: 'availability-error', role: 'status' }, error))));
}

new FunctionComponent(App, document.getElementById('app')).mount();
