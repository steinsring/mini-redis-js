import { h } from './mini-react/phase2/runtime.js';

const response = await fetch(new URL('./seat-layout.json', import.meta.url));
if (!response.ok) throw new Error('좌석 배치도를 불러오지 못했습니다.');
const { seats } = await response.json();
const accessibleMarkers = [
  [182, 175], [199, 175], [228, 175], [245, 175], [262, 175], [279, 175], [296, 175], [313, 175],
  [340, 175], [357, 175], [42, 303], [58, 303], [75, 303], [155, 303], [172, 303], [189, 303],
  [349, 303], [366, 303], [383, 303], [464, 303], [481, 303], [498, 303],
];

function position(x, y) {
  return 'left:' + (x / 544 * 100) + '%;top:' + (y / 367 * 100) + '%';
}

export function SeatMap({ selection, booked, busy, chooseSeat }) {
  const unavailable = new Set(booked);
  return h('section', { className: 'seat-map', 'aria-labelledby': 'map-heading' },
    h('div', { className: 'map-head' },
      h('div', null,
        h('h2', { id: 'map-heading' }, '좌석 배치도'),
        h('p', { className: 'map-hint' }, '배치도에서 좌석을 선택하세요.')),
      h('div', { className: 'legend' },
        h('span', null, h('i', { className: 'legend-available' }), '선택 가능'),
        h('span', null, h('i', { className: 'legend-booked' }), '선택 불가'))),
    h('div', { className: 'arena-scroll' },
      h('div', { className: 'arena' },
        h('div', { className: 'arena-outline', 'aria-hidden': true }),
        h('div', { className: 'arena-floor', 'aria-hidden': true }),
        h('div', { className: 'stage' }, 'STAGE'),
        h('div', { className: 'floor-label', 'aria-hidden': true }, '1F'),
        accessibleMarkers.map(([x, y]) =>
          h('span', { key: 'marker-' + x + '-' + y, className: 'accessible-marker',
            'aria-hidden': true, style: position(x, y) }, '♿︎')),
        seats.map(([x, y, color], index) => {
          const number = String(index + 1);
          const reserved = unavailable.has(number);
          return h('button', {
            key: number,
            type: 'button',
            className: 'seat seat-' + color + (reserved ? ' seat-booked' : ''),
            disabled: !booked || busy || reserved,
            'aria-label': number + '번 좌석' + (reserved ? ' 선택 불가' : ''),
            'aria-pressed': selection?.seat === number,
            'data-seat': number,
            style: position(x, y),
            onClick: () => chooseSeat(number),
          });
        }))),
    h('p', { className: 'map-status', role: 'status' }, booked ? '' : '좌석 상태를 불러오는 중입니다.'));
}

