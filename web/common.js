import { h } from './mini-react/phase2/runtime.js';

export function Icon({ name, label }) {
  return h('span', { className: 'icon icon-' + name, role: label ? 'img' : null,
    'aria-label': label, 'aria-hidden': label ? null : true, title: label },
    name === 'heart' ? '♡' : name === 'share' ? '↗' : null);
}

export function Header() {
  return h('header', { className: 'site-header' },
    h('div', { className: 'site-bar' },
      h('div', { className: 'wordmark' }, h('span', { className: 'mark', 'aria-hidden': true }), 'encore', h('span', null, '.')),
      h('span', { className: 'site-category' }, '콘서트'),
      h('div', { className: 'site-icons' },
        h(Icon, { name: 'search', label: '검색' }),
        h(Icon, { name: 'person', label: '내 계정' }))));
}
