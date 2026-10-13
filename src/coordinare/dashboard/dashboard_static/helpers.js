// 349: shared dashboard helpers, served verbatim from /static/helpers.js.
// The page pins the sha256 of these bytes into the script-tag ?v= query, so a
// redeploy that changes the file changes the URL and no browser ever renders
// a stale copy from cache.
function fmtDuration(secs) {
  if (secs === null || secs === undefined) return '—';
  return secs.toFixed(2) + 's';
}

function fmtTime(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch(e) { return iso; }
}

function fmtAge(iso) {
  if (!iso) return null;
  var secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return secs + 's';
  var mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ' + (secs % 60) + 's';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

// 136: every display label for an internal code comes from the glossary
// (/static/vocabulary.js, generated from coordinare/vocabulary.py). A code
// with no entry is shown as it is, so a missing label is visible, not
// papered over by title-casing.
function vocabularyLabel(code) {
  var key = String(code || '');
  var known = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY.labels) || {};
  return Object.prototype.hasOwnProperty.call(known, key) ? known[key] : key;
}

function humanPhase(phase) {
  return esc(vocabularyLabel(phase));
}

// 137: a stage says what is happening ("Implement"), an agent says who does it
// ("Implementer"). Several codes are both (security, qa), with different
// labels, so each is looked up by kind and never through humanPhase.
function vocabularyKindLabel(kind, code) {
  var key = String(code || '');
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  var known = v[kind] || {};
  return Object.prototype.hasOwnProperty.call(known, key) ? known[key] : key;
}

function stageLabel(stage) {
  return esc(vocabularyKindLabel('stage_labels', stage));
}

function agentLabel(role) {
  return esc(vocabularyKindLabel('agent_labels', role));
}

// The agent code that does a stage, or '' when the stage has none.
function agentForStage(stage) {
  var key = String(stage || '');
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  var map = v.stage_agent || {};
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : '';
}

// "Implement · Implementer": a stage shown with the agent that does it.
function stageAgentLabel(stage) {
  var agent = agentForStage(stage);
  var known = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY && COORDINARE_VOCABULARY.stage_labels) || {};
  if (!agent || !Object.prototype.hasOwnProperty.call(known, String(stage || ''))) return stageLabel(stage);
  return stageLabel(stage) + ' · ' + agentLabel(agent);
}

// 137: the Personas page says which stage each agent serves.
function personaStageNote(role) {
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  var map = v.stage_agent || {};
  for (var stage in map) {
    if (Object.prototype.hasOwnProperty.call(map, stage) && map[stage] === String(role || '')) {
      return ' <span class="muted" style="font-weight:normal">· ' + stageLabel(stage) + ' stage</span>';
    }
  }
  return ' <span class="muted" style="font-weight:normal">· no pipeline stage</span>';
}

// A role code may be written as an agent ("implementer") or as the stage it
// serves ("implementing"); show the pair either way.
function roleOrStageLabel(code) {
  var key = String(code || '');
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  var stages = v.stage_labels || {};
  if (Object.prototype.hasOwnProperty.call(stages, key)) return stageAgentLabel(key);
  var map = v.stage_agent || {};
  for (var stage in map) {
    if (Object.prototype.hasOwnProperty.call(map, stage) && map[stage] === key) return stageAgentLabel(stage);
  }
  return agentLabel(key);
}

// 136: the Glossary page, rendered from the same data.
function renderGlossaryPage() {
  var el = document.getElementById('glossary-section');
  if (!el) return;
  var v = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY) || {};
  function table(title, terms) {
    if (!Array.isArray(terms) || terms.length === 0) return '';
    return '<h3>' + esc(title) + '</h3><table><tbody>' + terms.map(function(t) {
      return '<tr><td style="white-space:nowrap"><strong>' + esc(t.label) + '</strong></td><td>' + esc(t.description) + '</td></tr>';
    }).join('') + '</tbody></table>';
  }
  el.innerHTML = table('Concepts', v.concepts) + table('Stages', v.stages) + table('Agents', v.agents) +
    table('Conductor status', v.phases) + table('Check outcomes', v.outcomes);
}
var formatPhaseLabel = humanPhase;

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function fmtBytes(b) {
  if (b == null) return '—';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(0) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}
