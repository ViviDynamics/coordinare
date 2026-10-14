// 149: why a card that is not progressing is waiting. The conductor decided
// each of these; this script only names the decision. It never re-derives one
// from the board, and an unknown or absent reason renders nothing.
//
// Inputs: card = {id, column, has_session}; snapshot = {session_skip_reasons,
// open_questions, todo_waits, todo_capacity, now (optional, ms)}.

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
function waitAgo(iso, now) {
  if (!iso) return '';
  var ms = new Date(iso).getTime();
  if (!isFinite(ms)) return '';
  var mins = Math.floor((now - ms) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  return Math.floor(mins / 60) + 'h ago';
}

function waitHas(map, key) {
  return !!map && Object.prototype.hasOwnProperty.call(map, key);
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
      return {
        kind: 'needs_human',
        text: waitLabel('needs_human') + ': clarification asked' + (ago ? ' ' + ago : ''),
        title: 'The conductor is waiting for an answer to a question on this card.',
      };
    }
    if (skip && skip.reason === 'dependency_blocked' && waitBlockersText(skip.blockers)) {
      return dependencyReason(skip.blockers);
    }
    return none;
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
  if (wait.reason === 'review_capacity') {
    return queuedReason('review_capacity', {waiting: capacity && capacity.waiting_on_review}, wait.detail);
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
