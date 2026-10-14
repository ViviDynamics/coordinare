var AF_SUMMARIES = {
  progress: 'Performer reported progress.', thinking: 'Performer is reasoning.',
  tool_use: 'Performer used a tool.', tool_result: 'A tool returned a result.',
  cost: 'Usage updated.',
  stage_change: 'Workflow stage changed.', recovered: 'Work recovered.',
  quiet: 'No recent activity.', stall: 'Performer stalled.', stuck: 'Work is stuck.',
  error: 'An error was reported.', blocked: 'Work is blocked.', completed: 'Work completed.',
  stream_truncated: 'Output was truncated (stream too long).',
  workflow_step: 'Workflow step started.',
  // 429: the verdict verb and reason are in the entry text itself.
  observer_verdict: 'Observer reported a verdict.',
  // 145: the entry text says which control the operator used.
  operator_action: 'An operator took an action.'
};
// 148: one glyph per lifecycle kind, so a lifecycle row is recognisable at a
// glance without the chip. Decorative: the label beside it says the same thing.
var AF_ICONS = {
  stage_change: '>', workflow_step: '#', feedback: '<', blocked: '!', stuck: '!', stall: '!',
  error: '!', recovered: '+', completed: '+', operator_action: '*', observer_verdict: '?',
  quiet: '~', stream_truncated: '~'
};
function afSummary(e) { return AF_SUMMARIES[e.activity_type] || 'Activity reported.'; }
function afIcon(type) { return AF_ICONS[type] || '*'; }
// 148: the four feed filters. Lifecycle, Agent and Errors show by default and
// Tools starts collapsed out of the way. The choice is remembered per browser.
var AF_CHIP_DEFAULTS = {lifecycle: true, agent: true, tools: false, errors: true};
var AF_CHIP_KEY = 'coordinare.activityChips';
function afLoadChips() {
  var chips = {};
  Object.keys(AF_CHIP_DEFAULTS).forEach(function(name) { chips[name] = AF_CHIP_DEFAULTS[name]; });
  try {
    var saved = JSON.parse(window.localStorage.getItem(AF_CHIP_KEY) || 'null');
    if (saved && typeof saved === 'object') {
      Object.keys(AF_CHIP_DEFAULTS).forEach(function(name) {
        if (typeof saved[name] === 'boolean') chips[name] = saved[name];
      });
    }
  } catch(err) { /* no storage (private mode, blocked): the defaults apply */ }
  return chips;
}
var _afChips = afLoadChips();
function afSaveChips() {
  try { window.localStorage.setItem(AF_CHIP_KEY, JSON.stringify(_afChips)); }
  catch(err) { /* the choice still holds for this page load */ }
}
// 148: the row kind picks the chip and the row's look. A failed tool result
// also answers to Errors, so a failure is never hidden along with its tool.
function afRowKind(e) {
  var type = e.activity_type;
  if (type === 'tool_use' || type === 'tool_result') return 'tools';
  if (type === 'progress' || type === 'thinking' || type === 'cost' || type === 'stream_truncated') return 'agent';
  if (type === 'error') return 'errors';
  return 'lifecycle';
}
function afVisible(e) {
  if (_afChips[afRowKind(e)]) return true;
  return e.activity_type === 'tool_result' && e.status === 'error' && !!_afChips.errors;
}
// 148: streamed kinds fold into one collapsible group per burst. Everything
// else (lifecycle, errors, quiet, stall) is a single row that is never folded.
function afStreamType(type) {
  return type === 'progress' || type === 'thinking' || type === 'tool_use' ||
    type === 'tool_result' || type === 'cost';
}
// 353: a collapsed group line leads with the newest meaningful entry's own text.
// 148: a tool call shows its summary, a successful result is not worth a line.
// The preview renders only what the group already carries, so there is no new leak path.
var AF_PREVIEW_MAX = 120;
function afPreview(entries) {
  for (var i = entries.length - 1; i >= 0; i--) {
    var e = entries[i];
    if (e.activity_type === 'cost' || e.activity_type === 'stream_truncated') continue;
    if (e.activity_type === 'tool_result' && e.status !== 'error') continue;
    var line = e.activity_type === 'tool_result' ? (e.text || e.summary) : (e.summary || e.text);
    var text = String(line || '').replace(/\s+/g, ' ').trim();
    if (text) return text.length > AF_PREVIEW_MAX
      ? text.slice(0, AF_PREVIEW_MAX - 1) + '…' : text;
  }
  return '';
}
// 148: the body text of one entry. A tool call shows its summary ("Bash · uv run
// pytest -q"), not the tool name the backend also sent.
function afLine(e) {
  var base = e.activity_type === 'tool_use' ? (e.summary || e.text) : e.text;
  return (base || '') + (e.truncated ? '… [truncated]' : '');
}
// 135: a count with thousands separators, written out so it reads the same in every browser.
function afCount(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
// 135: a stream's truncation marker is bookkeeping for the cap, not an update of its own.
function afUpdateCount(entries) {
  return entries.filter(function(e) { return e.activity_type !== 'stream_truncated'; }).length;
}
// 135: one clipped field (text or summary), when the daemon recorded its original length.
// Null when nothing was clipped, and also when no length was recorded (an entry from before
// 135), so an old entry shows nothing rather than a guess.
function afClip(e, field) {
  var length = e[field + '_length'];
  var kept = String(e[field] || '').length;
  if (typeof length !== 'number' || length <= kept) return null;
  return {kept: kept, hidden: length - kept};
}
function afCutPhrase(label, cut) {
  return label + ' clipped at ' + afCount(cut.kept) + ' characters' +
    (cut.updates > 1 ? ' in ' + afCount(cut.updates) + ' updates' : '') +
    ' (' + afCount(cut.hidden) + ' characters hidden)';
}
// 135: what the feed hid from one group, in plain words, with a link to the full transcript.
// Empty when nothing was cut. Every figure is a count the daemon recorded at the cut.
function afCutHtml(entries) {
  var shown = 0, hidden = 0, anchor = null, cuts = {};
  entries.forEach(function(e) {
    if (e.activity_type === 'stream_truncated') {
      if (typeof e.hidden_updates === 'number' && e.hidden_updates > 0) {
        hidden += e.hidden_updates;
        anchor = anchor || e;
      }
      return;
    }
    shown++;
    ['text', 'summary'].forEach(function(field) {
      var clip = afClip(e, field);
      if (!clip) return;
      var cut = cuts[field] || (cuts[field] = {updates: 0, kept: 0, hidden: 0});
      cut.updates++;
      cut.hidden += clip.hidden;
      cut.kept = Math.max(cut.kept, clip.kept);
      anchor = anchor || e;
    });
  });
  var parts = [];
  if (hidden > 0) parts.push('Showing ' + afCount(shown) + ' of ' + afCount(shown + hidden) + ' updates');
  if (cuts.text) parts.push(afCutPhrase('text', cuts.text));
  if (cuts.summary) parts.push(afCutPhrase('tool summary', cuts.summary));
  if (!parts.length) return '';
  // The link opens the run at the first cut, when the entry has a run. A lifecycle event has none.
  var link = anchor ? runEntryLinkHtml(anchor, 'Open full transcript') : '';
  return esc(parts.join(' · ')) + (link ? ' · ' + link : '');
}
// 135: the line for entries the bounded log dropped from the feed. Empty when none were.
function afDroppedText(dropped) {
  if (!(dropped > 0)) return '';
  return 'Older activity is not shown: ' + afCount(dropped) +
    ' entries were dropped to keep the newest ' + afCount(AF_MAX_ROWS) + '.';
}
function afRawHtml(entries) {
  var blocks = [];
  var usage = [];
  entries.forEach(function(e) {
    if (e.activity_type === 'cost') { usage.push(e); return; }
    var previous = blocks[blocks.length - 1];
    var fragment = afLine(e);
    if (previous && e.is_delta && previous.delta &&
        previous.kind === e.activity_type && previous.stream === (e.stream_id || '')) {
      previous.text += fragment;
    } else {
      blocks.push({text:fragment, kind:e.activity_type,
                   delta:!!e.is_delta, stream:e.stream_id || '', status:e.status || ''});
    }
  });
  var output = blocks.map(function(b) {
    if (b.kind === 'tool_use') return '<pre class="af-tool"><code>' + esc(b.text) + '</code></pre>';
    if (b.kind === 'tool_result') {
      return '<p class="af-result af-result-' + esc(b.status) + '">' + esc(b.text) + '</p>';
    }
    return '<p class="af-narration">' + esc(b.text) + '</p>';
  }).join('');
  if (usage.length) {
    // Snapshots may be cumulative or incremental depending on the backend.
    // Preserve the reported values; never infer a total by adding them here.
    output += '<details class="af-usage"><summary>Usage reports (' + usage.length +
      ')</summary>' + usage.map(function(e) {
        return '<p>' + esc((e.text || '') + (e.truncated ? '… [truncated]' : '')) + '</p>';
      }).join('') + '</details>';
  }
  return output;
}
function afRepresentative(entries) {
  for (var i = entries.length - 1; i >= 0; i--) {
    if (entries[i].activity_type !== 'cost' &&
        entries[i].activity_type !== 'stream_truncated') return entries[i];
  }
  return entries[entries.length - 1];
}
function afCanGroup(a, b) {
  return !!(a && b && a.session_id && a.performer_id &&
    afStreamType(a.activity_type) && afStreamType(b.activity_type) &&
    // 148: narration and tool calls never share a group, so each keeps its look.
    afRowKind(a) === afRowKind(b) &&
    a.card_id === b.card_id && a.stage === b.stage &&
    a.session_id === b.session_id && a.performer_id === b.performer_id);
}
// 353: the anchor skips folded truncation markers, so a stream that keeps
// flowing after its own truncation notice rejoins the group it belongs to.
function afGroupAnchor(group) {
  for (var i = group.entries.length - 1; i >= 0; i--) {
    if (group.entries[i].activity_type !== 'stream_truncated') {
      return group.entries[i];
    }
  }
  return null;
}
// 353: a truncation marker annotates the stream it truncated — fold it into
// that group as a badge rather than spending a top-level line on bookkeeping.
// An orphan marker (no matching group before it) keeps the legacy standalone
// line, which is the only sensible render when nothing precedes it.
function afCanFold(group, e) {
  var a = group ? afGroupAnchor(group) : null;
  return !!(a && a.session_id && a.card_id === e.card_id &&
    a.stage === e.stage && a.session_id === e.session_id &&
    a.performer_id === e.performer_id);
}
function afGroups() {
  var groups = [];
  _afEntries.forEach(function(e) {
    var g = groups.length ? groups[groups.length - 1] : null;
    if (e.activity_type === 'stream_truncated' && afCanFold(g, e)) {
      g.entries.push(e);
      g.truncatedCount = (g.truncatedCount || 0) + 1;
      return;
    }
    var anchor = g ? afGroupAnchor(g) : null;
    if (g && anchor && afCanGroup(anchor, e)) g.entries.push(e);
    else groups.push({key: e._afGroupKey == null ? e.seq : e._afGroupKey, entries:[e]});
  });
  return groups;
}
// 148: every row carries the id af-group-<key>. The key is the seq of the
// group's first entry, so it never changes as the group grows. That id is the
// anchor a transcript link (#133) will attach to.
function afPlainRowHtml(e, id, where) {
  var text = (e.text || '') + (e.truncated ? '… [truncated]' : '');
  // 135: a plain row is one entry that never changes, so its cut line is drawn once, here.
  var cut = afCutHtml([e]);
  return '<div class="af-row af-plain af-row-' + esc(afRowKind(e)) + '" id="' + id + '">' +
    '<span class="af-icon" aria-hidden="true">' + esc(afIcon(e.activity_type)) + '</span>' +
    '<span class="af-time">' + esc(afTime(e.timestamp)) + '</span>' +
    '<span class="af-kind ev-' + esc(e.activity_type) + '">' + esc(afLabel(e.activity_type)) + '</span>' +
    '<span class="af-card">' + esc(where) + '</span>' +
    '<span class="af-plain-text">' + esc(text || afSummary(e)) + '</span>' + runEntryLinkHtml(e) +
    (cut ? '<div class="af-notes">' + cut + '</div>' : '') + '</div>';
}
function afRowHtml(e, group) {
  var entries = group ? group.entries : [e];
  var latest = entries[entries.length - 1];
  e = afRepresentative(entries);
  var who = e.card_number ? ('#' + e.card_number) : (e.card_title || e.card_id || '');
  // 137: the stage's one display name, when the glossary is loaded.
  var stageName = e.stage && typeof vocabularyKindLabel === 'function' ? vocabularyKindLabel('stage_labels', e.stage) : e.stage;
  var where = who + (stageName ? ' ' + stageName : '');
  var count = afUpdateCount(entries);
  var key = group ? group.key : e.seq;
  var id = 'af-group-' + esc(String(key));
  // Lifecycle events are plain rows. Errors keep their collapsible line (353).
  if (afRowKind(e) === 'lifecycle') return afPlainRowHtml(latest, id, where);
  var preview = afPreview(entries) || afSummary(e);
  var badge = group && group.truncatedCount
    ? '<span class="af-truncated">truncated</span>' : '';
  return '<div class="af-row af-row-' + esc(afRowKind(e)) + '" id="' + id + '">' +
    '<details>' +
    '<summary><span class="af-time">' + esc(afTime(latest.timestamp)) + '</span> ' +
    '<span class="af-kind ev-' + esc(e.activity_type) + '">' + esc(afLabel(e.activity_type)) + '</span>' +
    '<span class="af-card">' + esc(where) + '</span> ' +
    '<span class="af-summary">' + esc(preview) + ' (' + count +
    (count === 1 ? ' update)' : ' updates)') + '</span>' + badge + runEntryLinkHtml(latest) + '</summary>' +
    '<div class="af-raw-label">Agent output</div>' +
    // 135: what was hidden from this group, and how to see all of it. Empty when nothing was cut.
    '<div class="af-notes">' + afCutHtml(entries) + '</div>' +
    '<div class="af-raw">' + afRawHtml(entries) + '</div>' +
    '</details></div>';
}
function afSyncGroups(reset) {
  var feed = document.getElementById('activity-feed');
  if (!feed) return;
  if (reset) feed.innerHTML = '';
  var wanted = {}, html = '';
  afGroups().forEach(function(group) {
    // 148: the chips decide which entries of a burst show; a burst with none
    // showing has no row at all.
    var entries = group.entries.filter(afVisible);
    if (!entries.length) return;
    var g = {key: group.key, entries: entries,
             truncatedCount: entries.filter(function(x) { return x.activity_type === 'stream_truncated'; }).length};
    var latest = g.entries[g.entries.length - 1];
    var e = afRepresentative(g.entries);
    if (!afMatches(e)) return;
    var id = 'af-group-' + g.key;
    wanted[id] = true;
    var node = document.getElementById(id);
    if (!node) { html = afRowHtml(e, g) + html; return; }
    // Lifecycle rows are one entry and never change after they are drawn.
    var raw = node.querySelector('.af-raw');
    if (!raw) return;
    // Preserve the details element and its summary: open state and keyboard
    // focus survive streaming updates. Only text/diagnostic children change.
    node.querySelector('.af-time').textContent = afTime(latest.timestamp);
    var chip = node.querySelector('.af-kind');
    chip.className = 'af-kind ev-' + e.activity_type;
    chip.textContent = afLabel(e.activity_type);
    // 353: the collapsed line carries the newest entry's text; the truncation
    // badge is inserted or removed to match the folded marker count.
    var summary = afPreview(g.entries) || afSummary(e);
    var summaryEl = node.querySelector('.af-summary');
    var count = afUpdateCount(g.entries);
    summaryEl.textContent = summary + ' (' + count + (count === 1 ? ' update)' : ' updates)');
    // 135: a marker's count changes without a new row, so the cut line is redrawn on every pass.
    var notes = node.querySelector('.af-notes');
    if (notes) notes.innerHTML = afCutHtml(g.entries);
    var badge = node.querySelector('.af-truncated');
    if (g.truncatedCount && !badge) {
      var mark = document.createElement('span');
      mark.className = 'af-truncated';
      mark.textContent = 'truncated';
      summaryEl.parentNode.insertBefore(mark, summaryEl.nextSibling);
    } else if (!g.truncatedCount && badge) {
      badge.remove();
    }
    var signature = g.entries[0].seq + ':' + latest.seq;
    if (raw.getAttribute('data-events') !== signature) {
      var usage = raw.querySelector('.af-usage');
      var usageOpen = usage && usage.open;
      var usageFocused = usage && document.activeElement === usage.querySelector('summary');
      raw.innerHTML = afRawHtml(g.entries);
      usage = raw.querySelector('.af-usage');
      if (usage) {
        usage.open = !!usageOpen;
        if (usageFocused) usage.querySelector('summary').focus();
      }
      raw.setAttribute('data-events', signature);
    }
  });
  if (html) feed.insertAdjacentHTML('afterbegin', html);
  // Raw retention is authoritative, even when all incoming events are filtered.
  feed.querySelectorAll('[id^="af-group-"]').forEach(function(node) {
    if (!wanted[node.id]) node.remove();
  });
}
// 148: a chip toggles its kind and the feed redraws with the new choice.
function onActivityChipToggle(button) {
  var name = button.getAttribute('data-chip');
  if (!Object.prototype.hasOwnProperty.call(AF_CHIP_DEFAULTS, name)) return;
  _afChips[name] = !_afChips[name];
  afSaveChips();
  afApplyChipState();
  afRender();
}
function afApplyChipState() {
  var buttons = document.querySelectorAll('.af-chip');
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].setAttribute('aria-pressed', _afChips[buttons[i].getAttribute('data-chip')] ? 'true' : 'false');
  }
}
