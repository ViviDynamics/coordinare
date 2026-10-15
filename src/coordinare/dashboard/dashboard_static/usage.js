// 134: token and cost display. Every figure is computed by the daemon
// (services/token_usage.py) and arrives as a usage summary, so this file only
// formats it. The functions are pure (no DOM reads) except renderSpend, which
// writes the status strip's spend figures. Served from /static/usage.js.

// Integers with thousands separators, independent of the browser locale.
function usageCount(value) {
  var n = Number(value);
  if (!isFinite(n) || n <= 0) return '0';
  return String(Math.floor(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

// Breakdown lines show six decimals: a figure in a tooltip must add up by hand.
function usageMoney(value) {
  var n = Number(value);
  if (!isFinite(n) || n <= 0) return '$0';
  return '$' + n.toFixed(6);
}

function usagePricingText(pricing) {
  var p = pricing || {};
  if (p.source === 'table' && p.as_of) return 'Pricing as of ' + p.as_of + '.';
  if (p.source === 'deprecated_flat_rate') return 'Pricing: deprecated flat rate, no pricing date.';
  return 'Pricing: no price configured.';
}

// The tooltip body: tokens by kind, cost by kind, each model, and the pricing date.
function usageBreakdownText(summary) {
  var s = summary || {};
  var t = s.tokens || {};
  var cost = s.cost || {};
  var kinds = cost.kind_usd || {};
  var lines = [];
  lines.push('Tokens: input ' + usageCount(t.input) + ', output ' + usageCount(t.output) +
    ', cache read ' + usageCount(t.cache_read) + ', cache write ' + usageCount(t.cache_write) +
    ' (total ' + usageCount(t.total) + ')');
  if (cost.usd != null) {
    lines.push('Cost: input ' + usageMoney(kinds.input_usd) + ', output ' + usageMoney(kinds.output_usd) +
      ', cache read ' + usageMoney(kinds.cache_read_usd) + ', cache write ' + usageMoney(kinds.cache_write_usd) +
      ' (total ' + usageMoney(cost.usd) + ')');
  }
  (Array.isArray(s.models) ? s.models : []).forEach(function(m) {
    var name = m.model || 'model not recorded';
    var count = usageCount((m.tokens || {}).total);
    if (m.priced) {
      lines.push(name + ': ' + count + ' tokens, ' + usageMoney(m.cost_usd));
    } else {
      lines.push(name + ': ' + count + ' tokens, no price (' + (m.reason || 'no price configured') + ')');
    }
  });
  lines.push(usagePricingText(s.pricing));
  return lines.join('\n');
}

// A summary is usable when it came from the ledger (tracked) and has tokens.
function usageTrackedSummary(summary) {
  return !!(summary && summary.tracked !== false && summary.tokens);
}

// The total of a summary, or the fallback (the status counter) when none is tracked.
function usageTotalOf(summary, fallback) {
  if (usageTrackedSummary(summary)) return Number(summary.tokens.total) || 0;
  return fallback != null ? fallback : null;
}

// {text, title}: the cost, or "unknown" with the reason, or "not tracked".
// Never a number that was not computed from a price.
function usageCostReadout(summary) {
  var notTracked = 'Token usage is not recorded for this figure.';
  if (!summary) return { text: 'not tracked', title: notTracked, unknown: true };
  if (summary.tracked === false) {
    return { text: 'not tracked', title: summary.reason || notTracked, unknown: true };
  }
  var cost = summary.cost || {};
  var breakdown = usageBreakdownText(summary);
  if (cost.usd == null) {
    return {
      text: 'unknown',
      title: 'Cost unknown: ' + (cost.unknown_reason || 'no price configured') + '\n' + breakdown,
      unknown: true,
    };
  }
  return { text: fmtCost(cost.usd), title: breakdown, unknown: false };
}

// {text, title}: tokens with the breakdown on hover. Untracked tokens say "total only".
function usageTokensReadout(summary, fallbackTotal) {
  var s = summary || {};
  if (usageTrackedSummary(s)) {
    return { text: usageCount(s.tokens.total) + ' tokens', title: usageBreakdownText(s) };
  }
  var total = s.tokens && s.tokens.total != null ? s.tokens.total : fallbackTotal;
  if (!(Number(total) > 0)) {
    return { text: 'not tracked', title: s.reason || 'Token usage is not recorded for this figure.' };
  }
  return {
    text: usageCount(total) + ' tokens (total only)',
    title: (s.reason || 'transcripts disabled') + ': this is the status counter, with no split by kind or model',
  };
}

// The cost cell: value on screen, breakdown in the tooltip. Every interpolated value is escaped.
function usageCostHtml(summary) {
  var readout = usageCostReadout(summary);
  return '<span title="' + esc(readout.title) + '">' + esc(readout.text) + '</span>';
}

// One period's figure for the strip ("$1.20"), or null when its cost is not priced (#238).
function spendFigure(summary) {
  var cost = (summary && summary.cost) || {};
  if (cost.usd == null) return null;
  return fmtCost(cost.usd);
}

// The hover line for an unpriced period: which period, and the reason the daemon gave.
function spendUnpricedNote(label, summary) {
  var cost = (summary && summary.cost) || {};
  if (cost.usd != null) return '';
  var reason = cost.unknown_reason || 'no price configured';
  return label + ' is not priced: ' + reason + (/\.$/.test(reason) ? '' : '.');
}

// The header strip: "$1.20 today · $8.40 week" (#238). An unpriced period says so; when neither is priced
// the strip reads "Spend not priced". The reasons are on hover.
function spendStripText(block) {
  if (!block || block.tracked === false) return 'Spend not tracked';
  var today = spendFigure(block.today);
  var week = spendFigure(block.week);
  if (today == null && week == null) return 'Spend not priced';
  return (today == null ? 'today not priced' : today + ' today') + ' · ' +
    (week == null ? 'week not priced' : week + ' week');
}

function spendStripTitle(block) {
  if (!block || block.tracked === false) {
    return (block && block.reason) || 'Transcripts are off, so spend is not recorded.';
  }
  var notes = [spendUnpricedNote('Today', block.today), spendUnpricedNote('This week', block.week)]
    .filter(function(note) { return note !== ''; });
  return (notes.length ? notes.join('\n') + '\n\n' : '') +
    'Days are UTC. Today is ' + (block.day || '') + '; this week runs from ' + (block.week_start || '') +
    ' to ' + (block.day || '') + '.\n\nToday:\n' + usageBreakdownText(block.today) +
    '\n\nThis week:\n' + usageBreakdownText(block.week);
}

// Writes the status strip's spend figures from one block (154: the strip is their only home).
function renderSpend(block) {
  var strip = document.getElementById('spend-strip');
  if (strip) {
    strip.textContent = spendStripText(block);
    strip.title = spendStripTitle(block);
  }
}
