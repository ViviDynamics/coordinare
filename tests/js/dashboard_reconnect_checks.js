'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const streams = [];
const timers = new Map();
let nextTimer = 0;
const messages = [];
const requests = [];
const responses = [];
const elements = {
  'daemon-readiness': {textContent: ''},
  'disconnected-banner': {style: {}, textContent: ''},
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
  _lastState: null,
  fetch(url, options) {
    requests.push({url, options});
    const next = responses.shift() || {status: 200};
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
  },
  isPerformersPath() { return false; }, isBootstrapRunPath() { return false; }, isCardPath() { return false; },
  afUpdateFilterOptions() {}, afSyncVisible() {}, bootstrapLiveActivity() {},
  EventSource: Source, URLSearchParams, console, Math: Object.assign(Object.create(Math), {random: () => 0.5}), location: {pathname: '/'},
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
context.window = context;
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
assert.equal(streams.length, 1);
assert.equal(streams[0].url, '/events');
function runTimer() {
  assert.equal(timers.size, 1, 'A closed stream must schedule exactly one retry');
  const [id, timer] = timers.entries().next().value;
  timers.delete(id); timer.callback(); return timer.delay;
}
const cases = {
  jitter() {
    context.Math.random = () => 0;
    assert.equal(context.retryDelay(1000), 500);
    context.Math.random = () => 1;
    assert.equal(context.retryDelay(1000), 1000);
  },
  async api() {
    responses.push({status: 503}, {status: 200});
    const result = context.fetch('/api/state');
    await Promise.resolve();
    assert.match(elements['disconnected-banner'].textContent, /Coordinare is starting/);
    assert.equal(requests.length, 1);
    runTimer();
    assert.equal((await result).status, 200);
    assert.equal(requests.length, 2);
    streams[0].emit('state_update', {phase: 'ready'});
    assert.equal(elements['disconnected-banner'].style.display, 'none');
    responses.push(new Error('network down'), {status: 200});
    const recovered = context.fetch('/api/state');
    await Promise.resolve();
    assert.equal(elements['disconnected-banner'].style.display, 'block');
    runTimer();
    assert.equal((await recovered).status, 200);
  },
  async mutation() {
    responses.push({status: 503});
    assert.equal((await context.fetch('/api/force-poll', {method: 'POST'})).status, 503);
    assert.equal(requests.length, 1);
    assert.equal(timers.size, 0, 'Never replay a mutation');
    responses.push(new Error('lost response'));
    await assert.rejects(context.fetch('/api/force-poll', {method: 'POST'}));
    assert.equal(requests.length, 2);
    assert.equal(timers.size, 0);
  },
  native() {
    streams[0].open(); streams[0].error(Source.CONNECTING);
    assert.equal(timers.size, 1, 'All reconnects use bounded jittered backoff');
    assert.match(elements['disconnected-banner'].textContent, /Reconnecting, Coordinare is restarting/);
    assert.equal(elements['connection-label'].textContent, 'Reconnecting');
    assert.match(elements['connection-status'].title, /none received yet/);
    runTimer(); streams.at(-1).open(); assert.equal(streams.length, 2);
    assert.equal(elements['connection-label'].textContent, 'Waiting for updates');
    streams.at(-1).emit('state_update', {phase: 'idle'});
    assert.equal(elements['disconnected-banner'].style.display, 'none');
  },
  closed() {
    streams[0].error(Source.CLOSED); streams[0].error(Source.CLOSED);
    assert.equal(timers.size, 1, 'Repeated errors must share one retry');
    runTimer(); assert.equal(streams.length, 2);
    streams[1].open(); assert.equal(timers.size, 0);
    streams.at(-1).emit('state_update', {phase: 'idle'});
    assert.equal(elements['disconnected-banner'].style.display, 'none');
  },
  backoff() {
    const delays = [];
    for (let i = 0; i < 8; i++) { streams.at(-1).error(Source.CLOSED); delays.push(runTimer()); }
    assert.ok(delays[0] > 0 && delays[1] > delays[0], 'Repeated failures need backoff');
    assert.ok(delays.every(d => d <= 30000), 'Retry delay must remain bounded');
    assert.equal(delays.at(-1), delays.at(-2), 'Backoff must reach its cap');
    streams.at(-1).open(); streams.at(-1).emit('state_update', {phase: 'idle'}); streams.at(-1).error(Source.CLOSED);
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
  banner() {
    streams[0].emit('state_update', {phase: 'working'});
    const previous = context._lastState;
    streams[0].error(Source.CLOSED);
    assert.match(elements['disconnected-banner'].textContent, /Reconnecting, Coordinare is restarting/);
    assert.equal(context._lastState, previous, 'Disconnect retains the last view');
    runTimer(); streams.at(-1).open();
    assert.equal(elements['disconnected-banner'].style.display, 'block', 'Open alone is not daemon readiness');
    streams.at(-1).emit('state_update', {phase: 'recovered'});
    assert.equal(elements['disconnected-banner'].style.display, 'none');
    assert.equal(context._lastState.phase, 'recovered');
    assert.match(elements['daemon-readiness'].textContent, /Daemon ready/);
    streams.at(-1).emit('state_update', {phase: 'idle', daemon_ready: false});
    assert.match(elements['daemon-readiness'].textContent, /Daemon degraded/);
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
Promise.resolve().then(cases[process.argv[3]]).then(() => {
  console.log('PASS ' + process.argv[3]);
}).catch(error => { console.error(error); process.exitCode = 1; });
