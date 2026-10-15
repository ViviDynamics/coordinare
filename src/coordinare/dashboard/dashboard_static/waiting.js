// 149: why a card that is not progressing is waiting. The conductor decided
// each of these; this script only names the decision. It never re-derives one
// from the board, and an unknown or absent reason renders nothing.
//
// Inputs: card = {id, column, has_session, block_cause}; snapshot =
// {session_skip_reasons, open_questions, todo_waits, todo_capacity, now
// (optional, ms)}.
//
// 142: a card in the Blocked column is one of three kinds: Needs human (a
// person must act, with the reason the conductor holds), Waiting on #N (a
// dependency), or Queued (capacity, To Do only). A stop with no known reason
// reads "Needs human" and nothing more.

function waitLabel(kind) {
  return vocabularyKindLabel('wait_labels', kind);
}

// "1 of 1 card slots in use", plus the queue when there is one. The same shape
// as busyText(), which counts performers rather than cards.
function slotsText(inUse, limit, queued) {
  var a = Number(inUse) || 0;
  var m = Number(limit) || 0;
  var q = Number(queued) || 0;
  return a + ' of ' + m + ' card slots in use' + (q > 0 ? ' · ' + q + ' queued' : '');
}

// Blockers arrive as issue numbers (sessions) or as preformatted labels
// ("#42", "org/repo#42") from the To Do dependency check.
function waitBlockersText(blockers) {
  var list = Array.isArray(blockers) ? blockers : [];
  return list.map(function(b) { return typeof b === 'number' ? '#' + b : String(b); }).join(', ');
}

// "2h ago" from an ISO timestamp, or '' when there is no usable timestamp.
// 157: the one relative age (fmtAgo, helpers.js), so the waiting reasons read like every other age.
function waitAgo(iso, now) {
  return fmtAgo(iso, now);
}

function waitHas(map, key) {
  return !!map && Object.prototype.hasOwnProperty.call(map, key);
}

// 142: the reason shown after "Needs human:" is one line, however long the
// question. The full text is kept in the title.
var WAIT_REASON_CHARS = 160;

// The Blocked-column kind for a stop that needs a person. The reason is what the
// conductor holds (an open question, or the cause it named); none is invented.
function needsHumanReason(reason, title) {
  var full = String(reason || '').replace(/\s+/g, ' ').trim();
  var short = full.length > WAIT_REASON_CHARS ? full.slice(0, WAIT_REASON_CHARS - 3) + '...' : full;
  return {
    kind: 'needs_human',
    text: waitLabel('needs_human') + (short ? ': ' + short : ''),
    title: title || 'A person has to act before this card goes on. The conductor does not record why it stopped.',
  };
}

// 142: the words for a cause the conductor named (block_cause), or '' when it
// named none. An unknown code is never shown as if it were a reason.
function blockCauseText(code) {
  var map = (typeof COORDINARE_VOCABULARY !== 'undefined' && COORDINARE_VOCABULARY.block_cause_labels) || {};
  return waitHas(map, code) ? String(map[code]) : '';
}

// The open question for a blocked card, if the conductor is holding one.
function waitOpenQuestion(questions, cardId) {
  var list = Array.isArray(questions) ? questions : [];
  for (var i = 0; i < list.length; i++) {
    if (list[i] && String(list[i].card_id) === cardId) return list[i];
  }
  return null;
}

// The one source for the queued words. The swimlane reason, the To Do header
// and the performer role's skipped-card list all read it, so they cannot
// disagree. reason is the conductor's own name: 'pipeline_capacity' (every
// card slot is in use) or 'review_capacity' (the review cap holds new cards).
// facts = {capacity: {in_use, limit}, waiting: cards parked on review}.
// Returns null for any other reason.
function queuedWaitText(reason, facts) {
  var f = facts || {};
  if (reason === 'pipeline_capacity') {
    var slots = f.capacity
      ? slotsText(f.capacity.in_use, f.capacity.limit, 0)
      : 'waiting for a free card slot';
    return waitLabel('queued') + ': ' + slots;
  }
  if (reason === 'review_capacity') {
    var n = Number(f.waiting) || 0;
    return waitLabel('queued') + ': ' + n + (n === 1 ? ' card' : ' cards') + ' waiting on review';
  }
  return null;
}

function queuedReason(reason, facts, detail) {
  return {
    kind: 'queued',
    text: queuedWaitText(reason, facts),
    title: detail || 'The conductor works this many cards at once. This card is picked up when a slot frees.',
  };
}

function waitingReason(card, snapshot) {
  var none = {kind: 'none', text: '', title: ''};
  var c = card || {};
  var snap = snapshot || {};
  var id = String(c.id || '');
  if (!id) return none;
  var skip = waitHas(snap.session_skip_reasons, id) ? snap.session_skip_reasons[id] : null;
  var capacity = snap.todo_capacity || null;

  if (c.column === 'BLOCKED') {
    var question = waitOpenQuestion(snap.open_questions, id);
    if (question) {
      var ago = waitAgo(question.asked_at, snap.now || Date.now());
      var asked = ago ? 'Asked ' + ago + '. ' : '';
      return needsHumanReason(question.text, (asked + String(question.text || '')).replace(/\s+/g, ' ').trim());
    }
    if (skip && skip.reason === 'dependency_blocked' && waitBlockersText(skip.blockers)) {
      return dependencyReason(skip.blockers);
    }
    // A card the dependency check moved here (no session) keeps the wait it was moved for.
    if (!c.has_session) {
      var moved = waitHas(snap.todo_waits, id) ? snap.todo_waits[id] : null;
      if (moved && moved.reason === 'waiting_on' && waitBlockersText(moved.blockers)) {
        return dependencyReason(moved.blockers);
      }
    }
    var cause = blockCauseText(c.block_cause);
    return needsHumanReason(cause, cause ? 'The conductor stopped this card: ' + cause + '.' : undefined);
  }

  // 266: a card the symphony's env bootstrap holds. The words are the server's, so they match the Symphonies page.
  if (skip && skip.reason === 'env_bootstrap') {
    return {kind: 'queued', text: String(skip.detail || ''), title: String(skip.detail || '')};
  }

  // 171: a card in review whose documentation run waits for a free slot. Not a To Do
  // card, so it is handled before the To Do branch below.
  if (c.has_session && skip && skip.reason === 'side_run_waiting') {
    return {kind: 'queued', text: 'Documentation update waiting for a free slot', title: String(skip.detail || '')};
  }

  if (c.column !== 'TODO') return none;

  // A card with a session takes its reason from the session's skip record.
  if (c.has_session) {
    if (skip && (skip.reason === 'pipeline_capacity' || skip.reason === 'review_capacity')) {
      return queuedReason(skip.reason, {capacity: skip.capacity, waiting: skip.waiting_on_review}, skip.detail);
    }
    if (skip && skip.reason === 'dependency_blocked' && waitBlockersText(skip.blockers)) {
      return dependencyReason(skip.blockers);
    }
    return none;
  }

  var wait = waitHas(snap.todo_waits, id) ? snap.todo_waits[id] : null;
  if (!wait) return none;
  if (wait.reason === 'queued') {
    return queuedReason('pipeline_capacity', {capacity: capacity}, null);
  }
  if (wait.reason === 'waiting_on') {
    if (!waitBlockersText(wait.blockers)) return none;
    return dependencyReason(wait.blockers);
  }
  if (wait.reason === 'escalated') {
    return {
      kind: 'needs_human',
      text: waitLabel('needs_human') + ': the advocate escalated it to a person',
      title: 'A person now owns this card. The conductor will not start it.',
    };
  }
  if (wait.reason === 'assigned_elsewhere') {
    return {
      kind: 'filtered',
      text: waitLabel('filtered') + ': assigned to someone else',
      title: 'The ownership filter leaves cards assigned to other people alone.',
    };
  }
  if (wait.reason === 'unassigned') {
    return {
      kind: 'filtered',
      text: waitLabel('filtered') + ': unassigned',
      title: 'This coordinare only takes cards assigned to it, and this card has no assignee.',
    };
  }
  return none;
}

// 142: the kind and reason of a session in the blocked phase, for the views that
// show a session (the Active performers tiles and the card detail) rather than a
// column. The same decision the Blocked column reads, so the words cannot differ.
function blockedSessionText(sess, questions, skipReasons) {
  var card = {id: sess ? sess.card_id : '', column: 'BLOCKED', has_session: true,
    block_cause: sess ? sess.block_cause : null};
  var reason = waitingReason(card, {open_questions: questions, session_skip_reasons: skipReasons, now: Date.now()});
  return reason.text || waitLabel('needs_human');
}

// 142: the words for a card the conductor did not pick up this check, on the
// Performers page. A dependency names its blockers, a capacity hold uses the queued
// words, and any other skip reads as not picked up. The wire code is never shown.
function skipReasonText(skip) {
  var r = skip || {};
  if (r.reason === 'dependency_blocked' && waitBlockersText(r.blockers)) {
    return dependencyReason(r.blockers).text;
  }
  return queuedWaitText(r.reason, {capacity: r.capacity, waiting: r.waiting_on_review}) || 'Not picked up this check';
}

function dependencyReason(blockers) {
  return {
    kind: 'waiting_on',
    text: waitLabel('waiting_on') + ' ' + waitBlockersText(blockers),
    title: 'The conductor does not start this card until the cards it depends on are done.',
  };
}

// The To Do header tooltip: the slot count, how many cards queue, and, when a
// review cap applies, how many cards wait on review against it.
function todoHeaderTitle(capacity, queuedCount) {
  if (!capacity) return '';
  var text = slotsText(capacity.in_use, capacity.limit, queuedCount);
  if (capacity.review_limit !== null && capacity.review_limit !== undefined) {
    text += ' · ' + (Number(capacity.waiting_on_review) || 0) + ' of ' + capacity.review_limit +
      ' cards waiting on review';
  }
  return text;
}

function waitingLineHtml(reason) {
  if (!reason || reason.kind === 'none' || !reason.text) return '';
  return '<div class="swimlane-wait" data-wait="' + esc(reason.kind) + '" title="' + esc(reason.title) + '">' +
    esc(reason.text) + '</div>';
}
