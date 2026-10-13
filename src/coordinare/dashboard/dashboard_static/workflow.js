// Maps a phase to the node ID that should be highlighted
var PHASE_NODE = {
  'idle':             'CB',
  'dispatching':      'AC',
  'monitoring_agent': 'MA',
  'monitoring_performer': 'MA',
  'monitoring_pr':    'MP',
  'merging':          'MR',
  'relay_feedback':   'RF',
  'blocked':          'HB',
  'recovery':         'CB',
};

var _lastRenderedPhase = null;

function flowColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue('--color-' + name).trim();
}

function buildFlowDef(phase) {
  var active = PHASE_NODE[phase] || '';
  var lines = [
    'graph TD',
    '  CB["check_board"]',
    '  AC["assess_card"]',
    '  DC["dispatch_card"]',
    '  MA["monitor_agent"]',
    '  MP["monitor_pr"]',
    '  MR["merge_pr"]',
    '  RF["relay_feedback"]',
    '  HB["handle_blocked"]',
    '  N["notify"]',
    '  IDLE(["idle — waiting"])',
    '  WAIT1(["wait next cycle"])',
    '  WAIT2(["wait next cycle"])',
    '  DONE1(["done"])',
    '  DONE2(["done"])',
    '  DONE3(["done"])',
    '',
    '  CB -->|"todo"| AC',
    '  CB -->|"in_progress"| MA',
    '  CB -->|"in_review"| MP',
    '  CB -->|"blocked"| HB',
    '  CB -->|"idle"| IDLE',
    '',
    '  AC -->|"sufficient"| DC',
    '  AC -->|"needs info"| HB',
    '',
    '  DC -->|"dispatched"| N',
    '  DC -->|"error"| HB',
    '',
    '  MA -->|"pr opened"| MP',
    '  MA -->|"blocked"| HB',
    '  MA -->|"working"| WAIT1',
    '',
    '  MP -->|"approved"| MR',
    '  MP -->|"changes"| RF',
    '  MP -->|"blocked"| HB',
    '  MP -->|"pending"| WAIT2',
    '',
    '  MR --> N',
    '  HB --> N',
    '  N --> DONE1',
    '  RF --> DONE2',
    '',
    '  classDef default fill:' + flowColor('bg-elevated') + ',stroke:' + flowColor('border-control') + ',color:' + flowColor('text-primary'),
    '  classDef terminal fill:' + flowColor('bg-base') + ',stroke:' + flowColor('border-control') + ',color:' + flowColor('text-muted') + ',font-style:italic',
    '  classDef active fill:' + flowColor('bg-row-selected') + ',stroke:' + flowColor('focus') + ',color:' + flowColor('text-primary') + ',font-weight:bold,stroke-width:2px',
    '  class IDLE,WAIT1,WAIT2,DONE1,DONE2,DONE3 terminal',
  ];
  if (active) lines.push('  class ' + active + ' active');
  return lines.join('\n');
}

var _renderSeq = 0;
async function updateFlowChart(phase) {
  if (phase === _lastRenderedPhase) return;
  _lastRenderedPhase = phase;
  var seq = ++_renderSeq;
  var def = buildFlowDef(phase);
  try {
    mermaid.initialize({startOnLoad: false, theme: 'base', themeVariables: {
      background: flowColor('bg-surface'), primaryColor: flowColor('bg-elevated'),
      primaryTextColor: flowColor('text-primary'), primaryBorderColor: flowColor('border-control'),
      lineColor: flowColor('text-muted'), secondaryColor: flowColor('bg-surface'),
      tertiaryColor: flowColor('bg-base'), fontFamily: 'system-ui, sans-serif',
    }, flowchart: {curve: 'basis', useMaxWidth: true}});
    var id = 'fc' + seq;
    var result = await mermaid.render(id, def);
    if (seq === _renderSeq) {
      document.getElementById('flow-chart').innerHTML = result.svg;
    }
  } catch(e) {
    console.error('mermaid render failed', e);
  }
}

window.addEventListener('coordinare:appearance', function () {
  _lastRenderedPhase = null;
  if (typeof _lastState !== 'undefined' && _lastState) updateFlowChart(_lastState.phase);
});
