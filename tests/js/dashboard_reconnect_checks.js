'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const streams = [];
const timers = new Map();
let nextTimer = 0;
const messages = [];
const elements = {
  'disconnected-banner': {style: {}},
  'nav-sse-dot': {classList: {add() {}, remove() {}}, title: ''},
  'force-poll-btn': {disabled: false},
  'connection-status': {title: ''},
  'connection-label': {textContent: ''},
};
class Source {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  constructor(url) { this.url = url; this.readyState = 0; this.listeners = {}; this.closes = 0; streams.push(this); }
  addEventListener(kind, callback) { this.listeners[kind] = callback; }
  close() { this.readyState = 2; this.closes++; }
  emit(kind, data) { this.listeners[kind]({data: JSON.stringify(data)}); }
  error(state) { this.readyState = state; this.onerror(); }
  open() { this.readyState = 1; this.onopen(); }
}
const context = {
  EventSource: Source, console, location: {pathname: '/'},
  document: {getElementById: id => elements[id]},
  setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
  clearTimeout(id) { timers.delete(id); },
  afNoteMessage() { messages.push('message'); },
  afAppend(entries) { messages.push(entries); },
  afSyncRun() {},
  renderState(state) { messages.push(state); },
  renderPerformersPage() {}, renderHistoryPage() {}, renderSymphoniesPage() {}, renderDashboardExtras() {},
  updateAttention() {},
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
assert.equal(streams.length, 1);
assert.equal(streams[0].url, '/events');
function runTimer() {
  assert.equal(timers.size, 1, 'A closed stream must schedule exactly one retry');
  const [id, timer] = timers.entries().next().value;
  timers.delete(id); timer.callback(); return timer.delay;
}
const cases = {
  native() {
    streams[0].open(); streams[0].error(Source.CONNECTING);
    assert.equal(timers.size, 0, 'Native reconnect must not create a competing retry');
    assert.equal(elements['connection-label'].textContent, 'Reconnecting');
    assert.match(elements['connection-status'].title, /none received yet/);
    streams[0].open(); assert.equal(streams.length, 1);
    assert.equal(elements['connection-label'].textContent, 'Waiting for updates');
    assert.equal(elements['disconnected-banner'].style.display, 'none');
  },
  closed() {
    streams[0].error(Source.CLOSED); streams[0].error(Source.CLOSED);
    assert.equal(timers.size, 1, 'Repeated errors must share one retry');
    runTimer(); assert.equal(streams.length, 2);
    streams[1].open(); assert.equal(timers.size, 0);
    assert.equal(elements['disconnected-banner'].style.display, 'none');
  },
  backoff() {
    const delays = [];
    for (let i = 0; i < 8; i++) { streams.at(-1).error(Source.CLOSED); delays.push(runTimer()); }
    assert.ok(delays[0] > 0 && delays[1] > delays[0], 'Repeated failures need backoff');
    assert.ok(delays.every(d => d <= 30000), 'Retry delay must remain bounded');
    assert.equal(delays.at(-1), delays.at(-2), 'Backoff must reach its cap');
    streams.at(-1).open(); streams.at(-1).error(Source.CLOSED);
    assert.equal(runTimer(), delays[0], 'A healthy connection resets retry delay');
  },
  handlers() {
    streams[0].error(Source.CLOSED); runTimer(); const current = streams.at(-1);
    current.open(); current.emit('activity_event', {entries: ['real tool']});
    current.emit('heartbeat', {}); current.emit('state_update', {phase: 'working'});
    assert.equal(messages.filter(m => m === 'message').length, 3);
    assert.equal(JSON.stringify(messages.find(Array.isArray)), '["real tool"]');
    assert.equal(context._lastState.phase, 'working');
    assert.equal(messages.find(m => m && m.phase).phase, 'working');
  },
  stale() {
    const old = streams[0]; old.error(Source.CLOSED); runTimer(); const current = streams.at(-1);
    current.error(Source.CLOSED); const pending = timers.size;
    old.open(); old.error(Source.CLOSED); old.emit('state_update', {phase: 'stale'});
    assert.equal(timers.size, pending, 'Stale callbacks must not cancel or duplicate current retry');
    assert.equal(messages.length, 0, 'Replaced sources must not overwrite current state');
    runTimer(); assert.equal(streams.length, 3);
  },
};
assert.ok(cases[process.argv[3]], 'Unknown browser check');
cases[process.argv[3]]();
console.log('PASS ' + process.argv[3]);
