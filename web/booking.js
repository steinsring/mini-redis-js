import { FunctionComponent, h, useEffect, useState } from './mini-react/phase2/runtime.js';
import { Header, Icon } from './common.js';
import { SeatMap } from './seat-map.js';

const won = new Intl.NumberFormat('ko-KR');

async function request(path, options) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  const data = await response.json();
  if (!response.ok) {
    const messages = {
      'ERR user already has a reservation': '이 ID로 이미 예매했거나 예매를 진행 중입니다.',
      'ERR seat unavailable': '다른 사용자가 이 좌석을 선택했습니다. 좌석을 다시 선택하세요.',
    };
    const detail = typeof data.detail === 'string' ? data.detail : '입력값을 확인하세요.';
    throw Object.assign(new Error(messages[detail] ?? detail), { code: detail });
  }
  return data;
}

function BookingPanel({ selection, booked, busy, reserve, reservation, history, lookup }) {
  const occupied = selection && booked?.includes(selection.seat);
  return h('aside', { className: 'booking booking-panel', 'aria-labelledby': 'booking-title' },
    h('div', { className: 'booking-head' },
      h('h2', { id: 'booking-title' }, '예매 정보'),
      h('span', { className: 'demo-label' }, '테스트 예매')),
    h('p', { className: 'booking-concert' }, 'AFTERGLOW LIVE'),
    h('div', { className: 'session' }, h(Icon, { name: 'calendar' }), '12.12 (토) 19:00'),
    h('div', { className: 'field' },
      h('label', { htmlFor: 'user-id' }, '사용자 ID'),
      h('input', { id: 'user-id', value: 'user-123', required: true, autocomplete: 'off' })),
    h('div', { className: 'selection', 'aria-live': 'polite' },
      selection ? [
        h('p', { className: 'seat-summary' }, selection.seat + '번 좌석'),
        h('p', { className: 'seat-price' }, selection.error ?? (occupied ? '선택할 수 없는 좌석입니다.'
          : selection.price !== undefined ? won.format(selection.price) + '원' : '가격을 불러오는 중…')),
      ] : h('p', { className: 'seat-summary' }, '좌석을 선택하면 가격이 표시됩니다.')),
    h('div', { className: 'action action-reserve', 'aria-busy': busy },
      selection ? h('button', { type: 'button', disabled: busy || !booked || !!occupied || selection.price === undefined,
        onClick: reserve }, busy ? '예매 처리 중…' : occupied ? '선택할 수 없는 좌석' : '예매하기') : null,
      h('p', { role: 'status', 'data-error': reservation.error }, reservation.text)),
    h('div', { className: 'history action' },
      h('button', { type: 'button', disabled: !!history.busy, onClick: lookup }, '예매 내역 조회'),
      h('p', { role: 'status', 'data-error': history.error }, history.text)));
}

function userId() {
  const input = document.getElementById('user-id');
  if (!input.reportValidity() || !input.value.trim()) throw new Error('사용자 ID를 입력하세요.');
  return input.value.trim();
}

function App() {
  const [selection, setSelection] = useState(null);
  const [booked, setBooked] = useState(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [statusError, setStatusError] = useState('');
  const [reservation, setReservation] = useState({ text: '' });
  const [history, setHistory] = useState({ text: '' });
  const busy = !!reservation.busy;

  useEffect(() => {
    const controller = new AbortController();
    let refreshing = false;
    async function refresh() {
      if (refreshing) return;
      refreshing = true;
      try {
        const seats = await request('/api/seats/reserved', { signal: controller.signal });
        if (controller.signal.aborted) return;
        setBooked(current => current?.length === seats.length &&
          current.every((seat, index) => seat === seats[index]) ? current : seats);
        setStatusError('');
      } catch (error) {
        if (!controller.signal.aborted) setStatusError('좌석 상태를 불러오지 못했습니다. 다시 확인하고 있습니다.');
      } finally {
        refreshing = false;
      }
    }
    refresh();
    const interval = setInterval(refresh, 4000);
    return () => { controller.abort(); clearInterval(interval); };
  }, [refreshVersion]);

  async function chooseSeat(seat) {
    if (busy || !booked || booked.includes(seat)) return;
    const next = { seat };
    setSelection(next);
    setReservation({ text: '' });
    try {
      const prices = await request('/api/price?' + new URLSearchParams({ seat_number: seat }));
      setSelection(current => current === next ? { ...next, price: prices[seat] } : current);
    } catch (error) {
      const reserved = error.code === 'ERR seat unavailable';
      const held = error.code === 'ERR seat held';
      const message = reserved ? '이미 예매된 좌석입니다'
        : held ? '다른 사용자가 이 좌석을 임시 점유 중입니다.' : error.message;
      setSelection(current => current === next ? { ...next, error: message } : current);
      if (reserved || held) {
        setBooked(current => current?.includes(seat) ? current : [...(current ?? []), seat]);
        setRefreshVersion(current => current + 1);
      }
    }
  }

  async function reserve() {
    if (busy || selection?.price === undefined || !booked || booked.includes(selection.seat)) return;
    const { seat } = selection;
    try {
      const user = userId();
      setReservation({ busy: true, text: '' });
      const hold = await request('/api/holds', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seat_number: seat, user_id: user }),
      });
      window.location.assign('/checkout#' + hold.token);
    } catch (error) {
      setReservation({ error: true, text: error.message });
    }
  }

  async function lookup() {
    try {
      const user = userId();
      setHistory({ busy: true, text: '조회 중…' });
      const seats = await request('/api/reservations?' + new URLSearchParams({ user_id: user }));
      setHistory({ text: seats.length ? seats.map(seat => seat + '번').join(', ') : '예매 내역이 없습니다.' });
    } catch (error) {
      setHistory({ error: true, text: error.message });
    }
  }

  return h('div', null,
    h(Header),
    h('main', { className: 'ticketing' },
      h('a', { href: '/', className: 'back-link' }, '← 공연 상세'),
      h('h1', { className: 'booking-page-title' }, '좌석 선택'),
      h('div', { className: 'layout' },
        h('div', { className: 'map-column' },
          h(SeatMap, { selection, booked, busy, chooseSeat }),
          h('p', { className: 'availability-error', role: 'status' }, statusError)),
        h(BookingPanel, { selection, booked, busy, reserve, reservation, history, lookup }))));
}

new FunctionComponent(App, document.getElementById('app')).mount();
