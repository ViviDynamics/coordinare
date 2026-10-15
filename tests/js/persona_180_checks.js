var assert = require('assert');
var hostile = '<script>globalThis.pwned = true</script>\n<img src=x onerror="pwned=true">\n[bad](javascript:alert(1))\n[bad](data:text/html,test)\n[entity](jav&#x61;script:alert(1))';
var html = personaMarkdown(hostile);
assert(!/<script|<img|onerror=|href="(?:javascript|data):/i.test(html.replace(/&lt;[^\n]*/g, '')));
assert(html.indexOf('&lt;script&gt;') >= 0, 'raw HTML is escaped');
assert(!globalThis.pwned, 'source is never evaluated');
assert(personaMarkdown('# Heading\n\n**bold** and `code`\n\n- one\n- two\n\n```js\n<script>\n```').indexOf('<h1>Heading</h1>') >= 0);
assert(personaMarkdown('[safe](https://example.com)').indexOf('href="https://example.com"') >= 0);
assert(personaMarkdown('`**literal**`').indexOf('<code>**literal**</code>') >= 0);
assert(personaHighlight('# Heading\n**bold** <script>').indexOf('md-heading') >= 0);
assert(personaHighlight('<script>').indexOf('&lt;script&gt;') >= 0);
var attrs = {'data-md-original': JSON.stringify(' a\r\n\r\nb  \r\n'), 'data-md-loaded': 'a\n\nb  \n'};
var field = {value: 'a\n\nb  \n', getAttribute: function(k) { return attrs[k] === undefined ? null : attrs[k]; }};
assert.strictEqual(personaSourceValue(field), ' a\r\n\r\nb  \r\n', 'untouched control returns exact original');
field.value = 'edited  \n';
assert.strictEqual(personaSourceValue(field), 'edited  \n', 'typed whitespace is preserved');
assert.deepStrictEqual(personaEdit('abc', 1, 2, 'bold'), {text: 'a**b**c', start: 3, end: 4});
assert.strictEqual(personaEdit('a\nb', 0, 3, 'indent').text, '  a\n  b');
assert.strictEqual(personaEdit('abc', 1, 1, 'indent').text, 'a  bc');
assert.strictEqual(personaEdit('  a\n  b', 0, 7, 'outdent').text, 'a\nb');
console.log('persona 180 checks passed');

// The selector and shared Config dirty/snapshot path run with a small DOM adapter.
function control(value) {
  var attributes = {'data-md-source': '', 'data-field': 'instructions', 'data-type': 'text', 'data-orig': JSON.stringify(value)};
  var result = {
    value: value,
    getAttribute: function(k) { return Object.prototype.hasOwnProperty.call(attributes, k) ? attributes[k] : null; },
    setAttribute: function(k, v) { attributes[k] = v; },
    removeAttribute: function(k) { delete attributes[k]; },
    hasAttribute: function(k) { return Object.prototype.hasOwnProperty.call(attributes, k); },
    closest: function() { return null; },
  };
  personaSetSource(result, value);
  return result;
}
var fields = [control('first\r\n'), control('second')];
var dirtyNodes = [{hidden: true}, {hidden: true}];
var panels = fields.map(function(field, i) {
  return {hidden: true, getAttribute: function() { return ['implementer', 'reviewer'][i]; },
    querySelector: function() { return field; }};
});
var attributes = [{}, {}];
var tabs = fields.map(function(field, i) {
  return {
    getAttribute: function(k) {
      if (k === 'data-persona-tab') return ['implementer', 'reviewer'][i];
      if (k === 'aria-controls') return 'panel-' + i;
      return attributes[i][k];
    },
    setAttribute: function(k, v) { attributes[i][k] = v; },
    querySelector: function() { return dirtyNodes[i]; },
  };
});
var editors = fields.map(function(field) {
  var highlight = {};
  var preview = {};
  return {querySelector: function(selector) {
    if (selector === '[data-md-source]') return field;
    return selector === '[data-md-highlight]' ? highlight : preview;
  }};
});
var root = {
  getAttribute: function() { return 'config'; },
  querySelectorAll: function(selector) {
    if (selector === '[data-persona-tab]') return tabs;
    if (selector === '[data-persona-panel]') return panels;
    if (selector === '[data-md-editor]') return editors;
    return [];
  },
};
var document = {getElementById: function(id) { return panels[Number(id.slice(-1))]; }};
var location = {hash: '#personas/implementer'};
var routes = [];
function pushRoute(path) { routes.push(path); location.hash = path.slice(path.indexOf('#')); }
function pathWithSymphony(path) { return path + '?fixture'; }
personaSelect(root, 'implementer', false);
assert.deepStrictEqual(panels.map(function(p) { return p.hidden; }), [false, true]);
assert.strictEqual(tabs[0].getAttribute('aria-selected'), 'true');
fields[0].value = 'first edited  \n';
personaRefresh(root);
assert.deepStrictEqual(dirtyNodes.map(function(n) { return n.hidden; }), [false, true]);
personaSelect(root, 'reviewer', true);
assert.deepStrictEqual(panels.map(function(p) { return p.hidden; }), [true, false]);
assert.strictEqual(fields[0].value, 'first edited  \n', 'selecting another persona retains the draft');
assert.strictEqual(routes[0], '/config#personas/reviewer?fixture');
fields[1].value = 'second edited';
personaSelect(root, 'unknown', false);
assert.strictEqual(panels[0].hidden, false, 'unknown links fall back to the first persona');
function cfgBox(field) {
  return {
    querySelectorAll: function(selector) { return selector === '[data-field]' ? [field] : []; },
    getAttribute: function(k) { return k === 'data-scope' ? 'item:personas/implementer' : null; },
    hasAttribute: function() { return false; },
  };
}
var entries = cfgEntries(cfgBox(fields[0]));
assert.strictEqual(entries[0].changed, true);
assert.strictEqual(entries[0].after, 'first edited  \n');
assert.deepStrictEqual(cfgPendingFor(cfgBox(fields[0]), entries).changes, {instructions: 'first edited  \n'});
fields[0].value = 'first\r\n';
assert.strictEqual(cfgEntries(cfgBox(fields[0]))[0].changed, false, 'exact source baseline shares Config dirty detection');
console.log('persona selector and shared Config checks passed');

assert.strictEqual(personaEdit('\ntext', 0, 0, 'heading').text, '## \ntext');

// Exercise the mounted key handler with live textarea selection, including Home on macOS.
var keyHandlers = {};
root.addEventListener = function(name, handler) { keyHandlers[name] = handler; };
var keyboardField = fields[0];
var keyboardEditor = editors[0];
keyboardField.addEventListener = function() {};
keyboardField.focus = function() {};
keyboardField.setSelectionRange = function(start, end, direction) {
  this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction || 'none';
};
keyboardField.closest = function(selector) {
  if (selector === '[data-md-editor]') return keyboardEditor;
  if (selector === '[data-persona-context]') return root;
  return null;
};
fields[1].addEventListener = function() {};
root.querySelectorAll = (function(original) {
  return function(selector) { return selector === '[data-md-source]' ? fields : original(selector); };
})(root.querySelectorAll);
personaMount(root);
function pressPersonaKey(key, shift) {
  var event = {target: keyboardField, key: key, shiftKey: !!shift, prevented: false,
    preventDefault: function() { this.prevented = true; }};
  keyHandlers.keydown(event);
  return event;
}
keyboardField.value = 'abc';
keyboardField.setSelectionRange(0, 0);
assert(pressPersonaKey('Tab').prevented);
assert.strictEqual(keyboardField.value, '  abc', 'Tab reads the live caret at zero');
assert.strictEqual(keyboardField.selectionStart, PERSONA_INDENT.length);
keyboardField.value = 'abc';
keyboardField.setSelectionRange(keyboardField.value.length, keyboardField.value.length);
pressPersonaKey('Home');
pressPersonaKey('Tab');
assert.strictEqual(keyboardField.value, '  abc', 'Home then Tab indents at the line start');
keyboardField.setSelectionRange(0, 0);
pressPersonaKey('Tab', true);
assert.strictEqual(keyboardField.value, 'abc', 'Shift+Tab at zero outdents the current full line');
keyboardField.value = '  abc\n  def\n  ghi';
keyboardField.setSelectionRange(1, keyboardField.value.indexOf('  ghi'));
pressPersonaKey('Tab', true);
assert.strictEqual(keyboardField.value, 'abc\ndef\n  ghi', 'outdent excludes a line at the selection end boundary');
keyboardField.value = 'abc\ndef';
keyboardField.setSelectionRange(keyboardField.value.length, keyboardField.value.length);
pressPersonaKey('Home');
pressPersonaKey('Tab');
assert.strictEqual(keyboardField.value, 'abc\n  def', 'Home uses the current line');
keyboardField.setSelectionRange(keyboardField.value.length, keyboardField.value.length);
pressPersonaKey('Tab', true);
assert.strictEqual(keyboardField.value, 'abc\ndef', 'outdent works at the end of a line');
assert.strictEqual(keyboardField.selectionStart, keyboardField.value.length, 'outdent preserves the caret');
keyboardField.value = '\tabc\n def';
keyboardField.setSelectionRange(0, keyboardField.value.length);
pressPersonaKey('Tab', true);
assert.strictEqual(keyboardField.value, 'abc\ndef', 'outdent handles tabs and a single space');
keyboardField.value = 'abc';
keyboardField.setSelectionRange(0, 0);
pressPersonaKey('Escape');
assert.strictEqual(pressPersonaKey('Tab').prevented, false, 'Escape then Tab leaves the source');
assert.strictEqual(keyboardField.value, 'abc');
assert(pressPersonaKey('Tab').prevented, 'the escape state is consumed once');
console.log('persona mounted keyboard checks passed');
