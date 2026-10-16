// Small SVG primitives shared by statistics pages. Callers supply observations,
// labels, formatted text and optional markers. Nulls break lines, never become zero.
var STATS_CHART_WIDTH = 320;
var STATS_CHART_HEIGHT = 72;
var STATS_CHART_PAD = 6;

function statsChart(series, label, format, markers) {
  var points = series || [];
  var edits = markers || [];
  var values = points.filter(function(p) { return typeof p.value === 'number' && isFinite(p.value); });
  var max = Math.max.apply(null, values.map(function(p) { return p.value; }).concat([1]));
  var coords = points.concat(edits).map(function(p, i) { return typeof p.x === 'number' ? p.x : i; });
  var low = Math.min.apply(null, coords.concat([coords.length ? coords[0] : 0]));
  var high = Math.max.apply(null, coords.concat([low + 1]));
  function x(p, i) { return STATS_CHART_PAD + ((typeof p.x === 'number' ? p.x : i) - low) / (high - low) * (STATS_CHART_WIDTH - STATS_CHART_PAD * 2); }
  function y(value) { return STATS_CHART_HEIGHT - STATS_CHART_PAD - value / max * (STATS_CHART_HEIGHT - STATS_CHART_PAD * 2); }
  var lines = [];
  var path = '';
  points.forEach(function(p, i) {
    if (typeof p.value !== 'number' || !isFinite(p.value)) { if (path) lines.push(path); path = ''; return; }
    path += (path ? ' L ' : 'M ') + x(p, i) + ' ' + y(p.value);
  });
  if (path) lines.push(path);
  var alternative = points.map(function(p) { return p.label + ': ' + format(p.value); }).join('; ');
  var svg = lines.map(function(line) { return '<path d="' + esc(line) + '" fill="none" stroke="currentColor" stroke-width="2"/>'; }).join('');
  values.forEach(function(p) {
    var i = points.indexOf(p);
    svg += '<circle cx="' + esc(x(p, i)) + '" cy="' + esc(y(p.value)) + '" r="2" fill="currentColor"/>';
  });
  edits.forEach(function(edit, i) {
    alternative += '; ' + edit.label;
    svg += '<path d="' + esc('M ' + x(edit, i) + ' 0 V ' + STATS_CHART_HEIGHT) + '" stroke="currentColor" stroke-dasharray="3 3"><title>' + esc(edit.label) + '</title></path>';
  });
  return '<svg class="stats-chart" viewBox="0 0 ' + esc(STATS_CHART_WIDTH) + ' ' + esc(STATS_CHART_HEIGHT) + '" role="img" aria-label="' + esc(label + '. ' + alternative) + '"><title>' + esc(label) + '</title><desc>' + esc(alternative || 'No recorded observations') + '</desc>' + svg + '</svg>';
}

function statsBars(items, label) {
  var max = Math.max.apply(null, (items || []).map(function(item) { return item.value; }).concat([1]));
  return '<div class="stats-bars" role="img" aria-label="' + esc(label + ': ' + items.map(function(item) { return item.label + ' ' + item.value; }).join(', ')) + '">' + items.map(function(item) {
    return '<div title="' + esc(label + ': ' + item.label + ' ' + item.value) + '"><span>' + esc(item.label) + '</span><svg viewBox="0 0 100 8" aria-hidden="true"><rect width="' + esc(item.value / max * 100) + '" height="8" fill="currentColor"/></svg><span>' + esc(item.value) + '</span></div>';
  }).join('') + '</div>';
}
