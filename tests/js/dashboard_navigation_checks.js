'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
function boot(saved, desktop = true, denied = false) {
  const callbacks = {}, mediaEvents = {}, attrs = {}, classes = new Set();
  const root = {dataset: {}};
  const storage = new Map(saved == null ? [] : [['coordinare.navigationCollapsed', saved]]);
  const menu = {classList: {toggle: (name, on) => {if (on) classes.add(name); else classes.delete(name);}}};
  const button = {setAttribute: (key, value) => {attrs[key] = value;}, textContent: ''};
  const media = {matches: desktop, addEventListener: (key, fn) => {mediaEvents[key] = fn;}};
  const window = {matchMedia: () => media, addEventListener: (key, fn) => {callbacks[key] = fn;}, localStorage: {
    getItem: key => {if (denied) throw Error('denied'); return storage.get(key) || null;},
    setItem: (key, value) => {if (denied) throw Error('denied'); storage.set(key, value);},
  }};
  const document = {documentElement: root, readyState: 'loading', addEventListener: (key, fn) => {callbacks[key] = fn;},
    getElementById: id => id === 'navbar-menu' ? menu : id === 'navbar-hamburger' ? button : null};
  vm.runInNewContext(source, {window, document});
  const beforeReady = root.dataset.navigationCollapsed;
  callbacks.DOMContentLoaded();
  return {window, root, beforeReady, attrs, classes, storage,
    resize: desktop => {media.matches = desktop; mediaEvents.change();}};
}
let b = boot(null);
assert.equal(b.beforeReady, 'false');
assert.equal(b.attrs['aria-expanded'], 'true');
assert.equal(b.attrs['aria-label'], 'Collapse navigation');
b.window.toggleNavMenu();
assert.equal(b.root.dataset.navigationCollapsed, 'true');
assert.equal(b.attrs['aria-expanded'], 'false');
assert.equal(b.attrs['aria-label'], 'Expand navigation');
assert.equal(b.storage.get('coordinare.navigationCollapsed'), 'true');
b.window.closeMobileNav();
assert.equal(b.root.dataset.navigationCollapsed, 'true', 'desktop choice survives navigation');
b.resize(false);
assert.equal(b.classes.has('open'), false);
assert.equal(b.attrs['aria-expanded'], 'false');
b.window.toggleNavMenu();
assert.equal(b.classes.has('open'), true);
assert.equal(b.attrs['aria-expanded'], 'true');
b.window.closeMobileNav();
assert.equal(b.classes.has('open'), false);
b.resize(true);
assert.equal(b.attrs['aria-expanded'], 'false', 'desktop collapse is restored after a mobile viewport');
b.window.toggleNavMenu();
assert.equal(b.root.dataset.navigationCollapsed, 'false');
b = boot('true');
assert.equal(b.beforeReady, 'true', 'saved collapsed width applies before paint');
b = boot('invalid');
assert.equal(b.beforeReady, 'false');
b = boot('true', true, true);
assert.equal(b.beforeReady, 'false');
b.window.toggleNavMenu();
assert.equal(b.root.dataset.navigationCollapsed, 'true', 'drawer still works when storage is denied');
console.log('navigation behavior checks passed');
