// Keep real requests and event handlers, while controlling the periodic clock
// and recording which page initiated each API call.
(() => {
  const test = window.frameElement?.bookingTest;
  if (!test) return;
  Object.defineProperty(performance, 'now', { value: () => test.elapsed });
  const setIntervalOriginal = window.setInterval.bind(window);
  window.setInterval = (callback, delay, ...args) =>
    delay === 4000 || delay === 250 ? 0 : setIntervalOriginal(callback, delay, ...args);

  const fetchOriginal = window.fetch.bind(window);
  window.fetch = async (input, options) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const path = url.pathname;
    if (!path.startsWith('/api/')) return fetchOriginal(input, options);
    const record = { page: location.pathname, path, method: options?.method ?? 'GET',
      userId: url.searchParams.get('user_id') };
    test.requests.push(record);
    const response = await fetchOriginal(input, options);
    record.status = response.status;
    record.body = await response.clone().json();
    return response;
  };
})();
