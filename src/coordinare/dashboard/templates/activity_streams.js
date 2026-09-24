var AF_SUMMARIES = {
  progress: 'Performer reported progress.', thinking: 'Performer is reasoning.',
  tool_use: 'Performer used a tool.', cost: 'Usage updated.',
  stage_change: 'Workflow stage changed.', recovered: 'Work recovered.',
  quiet: 'No recent activity.', stall: 'Performer stalled.', stuck: 'Work is stuck.',
  error: 'An error was reported.', blocked: 'Work is blocked.', completed: 'Work completed.',
  stream_truncated: 'Output was truncated (stream too long).',
  workflow_step: 'Workflow step started.'
};
function afSummary(e) { return AF_SUMMARIES[e.activity_type] || 'Activity reported.'; }
// 353: collapsed group lines lead with the newest entry's own text instead of
// the generic sentence, so the feed answers "which tool? doing what?" without
// expanding the group. The ingest path already folds the backend's detail
// field into text and redacts it; the preview renders only what the group
// already carries, so there is no new leak path. Flattened to one line and
// capped here because the row is a single summary span.
var AF_PREVIEW_MAX = 120;
function afPreview(entries) {
  for (var i = entries.length - 1; i >= 0; i--) {
    var e = entries[i];
    if (e.activity_type === 'cost' || e.activity_type === 'stream_truncated') continue;
    var text = String(e.text || '').replace(/\s+/g, ' ').trim();
    if (text) return text.length > AF_PREVIEW_MAX
      ? text.slice(0, AF_PREVIEW_MAX - 1) + '…' : text;
  }
  return '';
}
function afRawHtml(entries) {
  var blocks = [];
  var usage = [];
  entries.forEach(function(e) {
    if (e.activity_type === 'cost') { usage.push(e); return; }
    var previous = blocks[blocks.length - 1];
    var fragment = (e.text || '') + (e.truncated ? '… [truncated]' : '');
    if (previous && e.is_delta && previous.delta &&
        previous.kind === e.activity_type && previous.stream === (e.stream_id || '')) {
      previous.text += fragment;
    } else {
      blocks.push({text:fragment, kind:e.activity_type,
                   delta:!!e.is_delta, stream:e.stream_id || ''});
    }
  });
  var output = blocks.map(function(b) {
    return b.kind === 'tool_use' ? '<pre class="af-tool"><code>' + esc(b.text) + '</code></pre>' :
      '<p>' + esc(b.text) + '</p>';
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
  var stream = {progress:true, thinking:true, tool_use:true, cost:true};
  return !!(a && b && a.session_id && a.performer_id &&
    stream[a.activity_type] && stream[b.activity_type] &&
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
function afRowHtml(e, group) {
  var entries = group ? group.entries : [e];
  var latest = entries[entries.length - 1];
  e = afRepresentative(entries);
  var who = e.card_number ? ('#' + e.card_number) : (e.card_title || e.card_id || '');
  var where = who + (e.stage ? ' ' + e.stage : '');
  var count = entries.length;
  var key = group ? group.key : e.seq;
  var preview = afPreview(entries) || afSummary(e);
  var badge = group && group.truncatedCount
    ? '<span class="af-truncated">truncated</span>' : '';
  return '<div class="af-row">' +
    '<details id="af-group-' + esc(String(key)) + '">' +
    '<summary><span class="af-time">' + esc(afTime(latest.timestamp)) + '</span> ' +
    '<span class="af-kind ev-' + esc(e.activity_type) + '">' + esc(afLabel(e.activity_type)) + '</span>' +
    '<span class="af-card">' + esc(where) + '</span> ' +
    '<span class="af-summary">' + esc(preview) + ' (' + count +
    (count === 1 ? ' update)' : ' updates)') + '</span>' + badge + '</summary>' +
    '<div class="af-raw-label">Raw output (may contain other languages; retained text only)</div>' +
    '<div class="af-raw">' + afRawHtml(entries) + '</div>' +
    '</details></div>';
}
function afSyncGroups(reset) {
  var feed = document.getElementById('activity-feed');
  if (!feed) return;
  if (reset) feed.innerHTML = '';
  var wanted = {}, html = '';
  afGroups().forEach(function(g) {
    var latest = g.entries[g.entries.length - 1];
    var e = afRepresentative(g.entries);
    if (!afMatches(e)) return;
    var id = 'af-group-' + g.key;
    wanted[id] = true;
    var node = document.getElementById(id);
    if (!node) { html = afRowHtml(e, g) + html; return; }
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
    summaryEl.textContent = summary + ' (' +
      g.entries.length + (g.entries.length === 1 ? ' update)' : ' updates)');
    var badge = node.querySelector('.af-truncated');
    if (g.truncatedCount && !badge) {
      var mark = document.createElement('span');
      mark.className = 'af-truncated';
      mark.textContent = 'truncated';
      summaryEl.parentNode.insertBefore(mark, summaryEl.nextSibling);
    } else if (!g.truncatedCount && badge) {
      badge.remove();
    }
    var raw = node.querySelector('.af-raw');
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
  feed.querySelectorAll('details[id^="af-group-"]').forEach(function(node) {
    if (!wanted[node.id]) node.parentElement.remove();
  });
}
