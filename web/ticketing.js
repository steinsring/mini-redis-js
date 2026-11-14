import { FunctionComponent, h } from './mini-react/phase2/runtime.js';
import { Header, Icon } from './common.js';

function Concert() {
  return h('section', { className: 'concert', 'aria-labelledby': 'concert-title' },
    h('figure', { className: 'poster', 'aria-hidden': true },
      h('img', { src: '/static/afterglow-concert.jpg', alt: '', width: 800, height: 1000 }),
      h('div', { className: 'poster-type' }, h('span', null, 'AFTERGLOW'), h('span', null, 'LIVE')),
      h('span', { className: 'poster-date' }, '12 DEC 2026 / SEOUL')),
    h('div', { className: 'concert-info' },
      h('p', { className: 'category' }, '콘서트'),
      h('h1', { id: 'concert-title' }, 'AFTERGLOW LIVE'),
      h('p', { className: 'subtitle' }, '애프터글로우 라이브 인 서울'),
      h('dl', { className: 'event-facts' },
        h('dt', null, '공연일시'), h('dd', null, '2026.12.12 (토) 19:00'),
        h('dt', null, '공연장소'), h('dd', null, '서울 애프터글로우 홀'),
        h('dt', null, '관람시간'), h('dd', null, '90분')),
      h('div', { className: 'event-icons' },
        h(Icon, { name: 'heart', label: '관심 공연' }),
        h(Icon, { name: 'share', label: '공유' }))));
}

function App() {
  return h('div', null,
    h(Header),
    h('main', { className: 'ticketing' },
      h('p', { className: 'breadcrumb' }, '콘서트', h('span', { 'aria-hidden': true }, ' / '), '공연 상세'),
      h('div', { className: 'layout' },
        h(Concert),
        h('section', { className: 'booking', 'aria-labelledby': 'booking-title' },
          h('div', { className: 'booking-head' },
            h('h2', { id: 'booking-title' }, '공연 예매'),
            h('span', { className: 'demo-label' }, '테스트 예매')),
          h('div', { className: 'session' },
            h(Icon, { name: 'calendar' }),
            h('span', null, '12.12 (토) ', h('strong', null, '19:00'))),
          h('a', { className: 'booking-link', href: '/booking' }, '예매하기')))));
}

new FunctionComponent(App, document.getElementById('app')).mount();
