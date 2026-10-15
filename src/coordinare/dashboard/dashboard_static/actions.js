// 145: buttons for the operator controls that already existed as endpoints:
// cancel the card, skip the current stage, restart from a stage.
//
// Each action confirms first and says what it will do. The requests go to the
// existing /api/cancel, /api/skip-role and /api/restart-from/{role} routes.

var CARD_ACTION_PHASES = { monitoring_performer: true, monitoring_agent: true, dispatching: true };

// What the confirmation dialog says for each action.
function cardActionEffect(kind, stage) {
  if (kind === 'cancel') return 'Cancel this card? This stops the agent, discards its workspace and moves the card back to To Do.';
  if (kind === 'skip') return 'Skip the current stage? The card moves on to its next stage on the next check, without this stage finishing.';
  if (kind === 'restart') return 'Restart from ' + vocabularyKindLabel('stage_labels', stage) + '? The card goes back to that stage on the next check and every later stage runs again.';
  return '';
}

// The request an action sends: {method, url}.
function cardActionRequest(kind, cardId, stage) {
  var query = cardId ? '?card_id=' + encodeURIComponent(cardId) : '';
  if (kind === 'cancel') return { method: 'POST', url: '/api/cancel' + query };
  if (kind === 'skip') return { method: 'POST', url: '/api/skip-role' + query };
  if (kind === 'restart') return { method: 'POST', url: '/api/restart-from/' + encodeURIComponent(stage || '') + query };
  return null;
}

// Plain-language result for a response: {text, retry}.
function cardActionOutcome(kind, status, body) {
  body = body || {};
  if (status === 409 && body.status === 'cycle_in_progress') {
    return { text: 'A check is in progress. Try again in a moment.', retry: true };
  }
  if (status === 409 && body.status === 'not_active_card') {
    return { text: 'Cancel works only for the card the conductor is working on right now, and this is not it (a card held by another symphony cannot be cancelled from here). Nothing was cancelled.', retry: false };
  }
  if (status === 409) return { text: String(body.error || 'Another control is already pending for this card.'), retry: false };
  if (status >= 400) return { text: String(body.error || 'The request was refused.'), retry: false };
  if (kind === 'cancel') {
    return body.status === 'cancelled'
      ? { text: 'Card cancelled.', retry: false }
      : { text: 'There was no active card to cancel.', retry: false };
  }
  return { text: 'Queued. It takes effect on the next check.', retry: false };
}

function pendingControlText(pending) {
  if (!pending || !pending.action) return '';
  if (pending.action === 'skip') return 'Skip pending';
  if (pending.action === 'restart') return 'Restart from ' + vocabularyKindLabel('stage_labels', pending.target_stage) + ' pending';
  if (pending.action === 'veto') return 'Veto pending';
  return pending.action + ' pending';
}

function pendingControlHtml(sess) {
  var text = pendingControlText(sess && sess.pending_control);
  return text ? '<span class="ap-pill card-action-pending" title="Queued by an operator; applied on the next check">' + esc(text) + '</span>' : '';
}

// The page re-renders its tiles on every state update, which replaces the
// menu's DOM. What an action reported, and whether the operator has the menu
// open, therefore live here, keyed by card id, and the markup is rebuilt from
// them. A result is shown for a minute.
var CARD_ACTION_RESULT_MS = 60000;
var _cardActionResults = {};
var _cardActionOpen = {};

function setCardActionResult(cardId, text, retry, now) {
  _cardActionResults[cardId] = { text: text, retry: retry || null, at: now == null ? Date.now() : now };
}

function cardActionResult(cardId, now) {
  var entry = Object.prototype.hasOwnProperty.call(_cardActionResults, cardId) ? _cardActionResults[cardId] : null;
  if (!entry) return null;
  if ((now == null ? Date.now() : now) - entry.at > CARD_ACTION_RESULT_MS) {
    delete _cardActionResults[cardId];
    return null;
  }
  return entry;
}

// The status line for a card: the last result, plus a Retry that carries its
// own action and stage so it works after any re-render.
function cardActionMsgHtml(cardId, now) {
  var entry = cardActionResult(cardId, now);
  if (!entry) return '';
  var retry = entry.retry
    ? ' <button type="button" class="card-action-btn" data-card-retry="1" data-card-action="' + esc(entry.retry.kind) +
      '" data-card-id="' + esc(cardId) + '" data-card-stage="' + esc(entry.retry.stage || '') + '">Retry</button>'
    : '';
  return esc(entry.text) + retry;
}

// 168: the server says which card Cancel would act on (cancel_available). Cancel acts on that
// one card only, so a card another symphony holds gets no Cancel button, and the menu says why.
var CARD_CANCEL_NOTE = 'Cancel is offered only for the card the conductor is working on right now. This card is not it, so it cannot be cancelled from here.';

function cardCancelOffered(sess) {
  return !!sess && sess.cancel_available === true;
}

// The actions menu for a card, or '' when the card has nothing to act on.
function cardActionsHtml(sess, s, now) {
  if (!sess || !sess.card_id || !CARD_ACTION_PHASES[sess.phase]) return '';
  var cardId = String(sess.card_id);
  var id = esc(cardId);
  var cancel = cardCancelOffered(sess)
    ? '<button type="button" class="card-action-btn card-action-danger" data-card-action="cancel" data-card-id="' + id + '">Cancel card</button>'
    : '<span class="card-action-note" style="color:var(--color-text-muted);font-size:12px">' + esc(CARD_CANCEL_NOTE) + '</span>';
  var stages = Array.isArray(s && s.lifecycle_sequence) ? s.lifecycle_sequence : [];
  var options = stages.map(function(stage) {
    return '<option value="' + esc(stage) + '">' + stageLabel(stage) + '</option>';
  }).join('');
  var restart = options
    ? '<span class="card-action-restart"><select aria-label="Stage to restart from">' + options + '</select>' +
      '<button type="button" class="card-action-btn" data-card-action="restart" data-card-id="' + id + '">Restart from stage</button></span>'
    : '';
  var msg = cardActionMsgHtml(cardId, now);
  var open = msg || _cardActionOpen[cardId] === true ? ' open' : '';
  return '<details class="card-actions" data-card-id="' + id + '"' + open + '>' +
    '<summary aria-label="Card actions" title="Card actions">&#8943;</summary>' +
    '<div class="card-actions-menu">' +
      cancel +
      '<button type="button" class="card-action-btn" data-card-action="skip" data-card-id="' + id + '">Skip current stage</button>' +
      restart +
      '<div class="card-action-msg" role="status" aria-live="polite">' + msg + '</div>' +
    '</div></details>';
}

// Write a card's current status line into every menu for it on the page.
function paintCardActionResult(cardId) {
  if (typeof document === 'undefined' || !document.querySelectorAll) return;
  var menus = document.querySelectorAll('.card-actions');
  for (var i = 0; i < menus.length; i++) {
    if (menus[i].getAttribute('data-card-id') !== cardId) continue;
    var msg = menus[i].querySelector('.card-action-msg');
    if (msg) msg.innerHTML = cardActionMsgHtml(cardId);
  }
}

function runCardAction(button) {
  var kind = button.getAttribute('data-card-action');
  var cardId = button.getAttribute('data-card-id') || '';
  var isRetry = button.getAttribute('data-card-retry') === '1';
  var menu = button.closest ? button.closest('.card-actions-menu') : null;
  var select = menu ? menu.querySelector('select') : null;
  // A retry carries the stage it was first asked for; a first click reads the select.
  var stage = kind === 'restart' ? (isRetry ? (button.getAttribute('data-card-stage') || '') : (select ? select.value : '')) : '';
  if (kind === 'restart' && !stage) return;
  // The operator already confirmed the action a retry repeats.
  if (!isRetry && !window.confirm(cardActionEffect(kind, stage))) return;
  var request = cardActionRequest(kind, cardId, stage);
  if (!request) return;
  setCardActionResult(cardId, 'Working…', null);
  paintCardActionResult(cardId);
  fetch(request.url, { method: request.method })
    .then(function(res) {
      return res.json().catch(function() { return {}; }).then(function(body) {
        var outcome = cardActionOutcome(kind, res.status, body);
        setCardActionResult(cardId, outcome.text, outcome.retry ? { kind: kind, stage: stage } : null);
        paintCardActionResult(cardId);
      });
    })
    .catch(function() {
      setCardActionResult(cardId, 'Could not reach the server.', { kind: kind, stage: stage });
      paintCardActionResult(cardId);
    });
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('click', function(event) {
    var button = event.target && event.target.closest ? event.target.closest('[data-card-action]') : null;
    if (button) { event.preventDefault(); event.stopPropagation(); runCardAction(button); }
  });
  // Remember which card's menu the operator has open, across re-renders.
  document.addEventListener('toggle', function(event) {
    var details = event.target;
    if (!details || !details.classList || !details.classList.contains('card-actions')) return;
    var cardId = details.getAttribute('data-card-id');
    if (cardId) _cardActionOpen[cardId] = !!details.open;
  }, true);
}
