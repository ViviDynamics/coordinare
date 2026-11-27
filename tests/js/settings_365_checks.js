'use strict';
// #365 (d), (e) and the Settings "Saved" message. Run by tests/unit/test_365_settings.py.
var assert = require('assert');
var passed = 0;
function ok(name, value) { assert.ok(value, name); passed += 1; }
function eq(name, actual, expected) { assert.deepStrictEqual(actual, expected, name); passed += 1; }

// --- (e) the logins field --------------------------------------------------------------------
eq('commas and spaces both separate logins', cfgParseLogins('alice, bob carol').logins, ['alice', 'bob', 'carol']);
eq('new lines and repeated separators are tolerated', cfgParseLogins('\n alice ,, \n bob ,').logins, ['alice', 'bob']);
eq('an empty field is an empty list', cfgParseLogins('   ').logins, []);
eq('a repeated login is kept once, whatever its case', cfgParseLogins('Alice alice ALICE').logins, ['Alice']);
eq('the repeat is reported', cfgParseLogins('Alice alice').duplicates, ['alice']);
eq('a bot login with its [bot] suffix is a login', cfgParseLogins('renovate[bot]').logins, ['renovate[bot]']);
eq('hyphens inside a login are fine', cfgParseLogins('a-b-c').invalid, []);
eq('an @ is flagged, not quietly dropped', cfgParseLogins('@alice').invalid, ['@alice']);
eq('characters GitHub refuses are flagged', cfgParseLogins('al_ice a.b').invalid, ['al_ice', 'a.b']);
eq('a leading, trailing or doubled hyphen is flagged', cfgParseLogins('-a b- c--d').invalid, ['-a', 'b-', 'c--d']);
eq('a login over 39 characters is flagged', cfgParseLogins(new Array(41).join('a')).invalid, [new Array(41).join('a')]);
eq('a flagged entry is not counted as a login', cfgParseLogins('@alice bob').logins, ['bob']);

var preview = cfgLoginsPreviewHtml(cfgParseLogins('alice, bob'));
ok('the preview counts the logins it read', preview.indexOf('Reads as 2 logins') >= 0);
ok('the preview shows each login', preview.indexOf('>alice<') >= 0 && preview.indexOf('>bob<') >= 0);
var flagged = cfgLoginsPreviewHtml(cfgParseLogins('@alice, <b>x</b>'));
ok('the preview names what is not a login', flagged.indexOf('Not a GitHub login: @alice') >= 0);
ok('the preview says what a login may hold', flagged.indexOf('letters, digits and single hyphens') >= 0);
ok('the preview escapes what was typed', flagged.indexOf('<b>') < 0 && flagged.indexOf('&lt;b&gt;') >= 0);
ok('an empty preview says the list is empty', cfgLoginsPreviewHtml(cfgParseLogins('')).indexOf('No logins') >= 0);

var input = cfgInput({key: 'global.human_reviewers', label: 'Who can approve merges (human_reviewers)', type: 'list',
  current_value: ['alice', 'bob']});
ok('a logins field is marked as one', input.indexOf('data-list="logins"') >= 0);
ok('a logins field shows its parsed list under it', input.indexOf('data-logins-preview') >= 0 && input.indexOf('>alice<') >= 0);
ok('a logins field says how to separate logins', input.indexOf('commas or spaces') >= 0);
var other = cfgInput({key: 'global.env_spec_files', label: 'x', type: 'list', current_value: ['a b']});
ok('another list field is unchanged', other.indexOf('data-list') < 0 && other.indexOf('data-logins-preview') < 0);

function control(attrs, value) {
  return {value: value, getAttribute: function(name) { return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null; }};
}
eq('a logins value is saved as the list it reads as',
  cfgCoerce(control({'data-type': 'list', 'data-list': 'logins'}, 'alice bob, carol')), ['alice', 'bob', 'carol']);
eq('a flagged entry is still sent, so the refusal names it',
  cfgCoerce(control({'data-type': 'list', 'data-list': 'logins'}, '@alice bob')), ['@alice', 'bob']);
eq('another list still splits on commas only',
  cfgCoerce(control({'data-type': 'list'}, 'a b, c')), ['a b', 'c']);

var flaggedControl = control({'data-type': 'list', 'data-list': 'logins', 'data-field': 'human_reviewers'}, '@alice bob');
var fineControl = control({'data-type': 'list', 'data-list': 'logins', 'data-field': 'trusted_bot_reviewers'}, 'renovate[bot]');
var box = {querySelectorAll: function(sel) { return sel === '[data-list="logins"]' ? [flaggedControl, fineControl] : []; }};
var errors = cfgLoginsErrors(box);
eq('a save is refused for the field with a flagged login', errors.map(function(e) { return e.key; }), ['human_reviewers']);
ok('the refusal names the entry', errors[0].message.indexOf('@alice') >= 0);

// --- (d) the symphony form says who can approve ----------------------------------------------
var note = symApproverNoteHtml({overrides: {}});
ok('the symphony form names the rule', note.indexOf('Only the logins in Who can approve merges can approve a merge') >= 0);
ok('the symphony form says what an unlisted approval does', note.indexOf('does not merge') >= 0);
ok('the symphony form links to the setting', note.indexOf('href="/config#global"') >= 0);
var own = symApproverNoteHtml({overrides: {human_reviewers: ['carol', '<i>dan</i>']}});
ok('a symphony with its own list says so and names it', own.indexOf('This symphony sets its own list: carol, &lt;i&gt;dan&lt;/i&gt;') >= 0);

// --- the Settings "Saved" message survives the refresh after a save ---------------------------
function status(text) { return {textContent: text, style: {color: 'green'}}; }
function section(scope, statusEl) {
  return {
    getAttribute: function(name) { return name === 'data-scope' ? scope : null; },
    querySelector: function(sel) { return sel === '[data-status]' ? statusEl : null; }
  };
}
var savedEl = status('Saved · applied live');
var notice = cfgSavedNotice(section('section:global', savedEl), savedEl);
eq('the notice carries the message and its section', notice, {scope: 'section:global', text: 'Saved · applied live', color: 'green'});
eq('a message that is not a save result is not carried', cfgSavedNotice(section('section:global', status('Validating...')), status('Validating...')), null);
var redrawn = status('');
redrawn.style.color = '';
var root = {querySelectorAll: function() { return [section('section:other', status('')), section('section:global', redrawn)]; }};
cfgRestoreNotice(root, notice);
eq('the redrawn section shows the same message', redrawn.textContent, 'Saved · applied live');
eq('in the same colour', redrawn.style.color, 'green');
cfgRestoreNotice(root, null);
eq('no notice changes nothing', redrawn.textContent, 'Saved · applied live');

console.log('SUMMARY ok ' + passed);
