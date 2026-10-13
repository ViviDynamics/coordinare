'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');

function boot(saved, dark = false, denied = false) {
  const events = {}, mediaEvents = {}, domEvents = {};
  const storage = new Map(saved == null ? [] : [['coordinare.appearance', saved]]);
  const root = {dataset: {}, style: {}};
  const buttons = ['system', 'light', 'dark'].map(value => ({
    dataset: {appearance: value}, attributes: {}, events: {},
    setAttribute(k, v) { this.attributes[k] = v; },
    addEventListener(k, fn) { this.events[k] = fn; },
  }));
  const media = {matches: dark, addEventListener: (k, fn) => { mediaEvents[k] = fn; }};
  const window = {addEventListener: (k, fn) => { events[k] = fn; }, dispatchEvent: () => {},
    matchMedia: () => media, localStorage: {
      getItem: key => { if (denied) throw new Error('denied'); return storage.get(key) || null; },
      setItem: (key, value) => { if (denied) throw new Error('denied'); storage.set(key, value); },
    }};
  const document = {documentElement: root, readyState: 'loading',
    addEventListener: (k, fn) => { domEvents[k] = fn; },
    getElementById: () => null,
    querySelectorAll: selector => selector === '[data-appearance]' ? buttons : []};
  vm.runInNewContext(source, {window, document, CustomEvent: class {constructor(type) {this.type = type;}}});
  return {root, buttons, storage, media, loaded: () => domEvents.DOMContentLoaded(),
    choose: value => { buttons.find(b => b.dataset.appearance === value).events.click(); },
    selected: () => buttons.filter(b => b.attributes['aria-pressed'] === 'true').map(b => b.dataset.appearance),
    os: dark => {media.matches = dark; mediaEvents.change();},
    crossTab: value => events.storage({key: 'coordinare.appearance', newValue: value}),
    clear: () => events.storage({key: null, newValue: null})};
}

for (const [saved, osDark, want] of [[null,false,'light'],[null,true,'dark'],['light',true,'light'],['dark',false,'dark'],['system',true,'dark'],['invalid',false,'light']]) {
  const b = boot(saved, osDark);
  assert.equal(b.root.dataset.theme, want, 'effective appearance must resolve before DOM ready');
  assert.equal(b.root.style.colorScheme, want);
}
let b = boot(null, true);
b.loaded();
assert.deepEqual(b.selected(), ['system']);
b.choose('light');
assert.deepEqual(b.selected(), ['light']);
assert.equal(b.root.dataset.theme, 'light');
assert.equal(b.storage.get('coordinare.appearance'), 'light');
b.os(true);
assert.equal(b.root.dataset.theme, 'light', 'OS changes cannot override explicit choice');
b.choose('system');
assert.deepEqual(b.selected(), ['system']);
assert.equal(b.root.dataset.theme, 'dark');
b.os(false);
assert.equal(b.root.dataset.theme, 'light');
b.crossTab('dark');
assert.equal(b.root.dataset.theme, 'dark');
assert.deepEqual(b.selected(), ['dark']);
b.clear();
assert.equal(b.root.dataset.theme, 'light');
assert.deepEqual(b.selected(), ['system']);
b = boot('dark', false, true);
b.loaded();
assert.equal(b.root.dataset.theme, 'light', 'denied storage falls back to system');
b.choose('dark');
assert.equal(b.root.dataset.theme, 'dark', 'in-memory preference works with denied storage');
b.choose('system');
b.os(true);
assert.equal(b.root.dataset.theme, 'dark');
console.log('appearance behavior checks passed');
