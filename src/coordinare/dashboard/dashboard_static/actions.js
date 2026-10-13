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
    return { text: 'Only the active card can be cancelled, and this is not it. Nothing was cancelled.', retry: false };
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

// The actions menu for a card, or '' when the card has nothing to act on.
function cardActionsHtml(sess, s) {
  if (!sess || !sess.card_id || !CARD_ACTION_PHASES[sess.phase]) return '';
  var id = esc(sess.card_id);
  var stages = Array.isArray(s && s.lifecycle_sequence) ? s.lifecycle_sequence : [];
  var options = stages.map(function(stage) {
    return '<option value="' + esc(stage) + '">' + stageLabel(stage) + '</option>';
  }).join('');
  var restart = options
    ? '<span class="card-action-restart"><select aria-label="Stage to restart from">' + options + '</select>' +
      '<button type="button" class="card-action-btn" data-card-action="restart" data-card-id="' + id + '">Restart from stage</button></span>'
    : '';
  return '<details class="card-actions">' +
    '<summary aria-label="Card actions" title="Card actions">&#8943;</summary>' +
    '<div class="card-actions-menu">' +
      '<button type="button" class="card-action-btn card-action-danger" data-card-action="cancel" data-card-id="' + id + '">Cancel card</button>' +
      '<button type="button" class="card-action-btn" data-card-action="skip" data-card-id="' + id + '">Skip current stage</button>' +
      restart +
      '<div class="card-action-msg" role="status" aria-live="polite"></div>' +
    '</div></details>';
}

function runCardAction(button) {
  var kind = button.getAttribute('data-card-action');
  var cardId = button.getAttribute('data-card-id');
  var menu = button.closest('.card-actions-menu');
  var select = menu ? menu.querySelector('select') : null;
  var stage = kind === 'restart' && select ? select.value : '';
  var msg = menu ? menu.querySelector('.card-action-msg') : null;
  if (!window.confirm(cardActionEffect(kind, stage))) return;
  var request = cardActionRequest(kind, cardId, stage);
  if (!request) return;
  if (msg) msg.textContent = 'Working…';
  fetch(request.url, { method: request.method })
    .then(function(res) {
      return res.json().catch(function() { return {}; }).then(function(body) {
        var outcome = cardActionOutcome(kind, res.status, body);
        if (!msg) return;
        msg.textContent = outcome.text;
        if (outcome.retry) {
          var again = document.createElement('button');
          again.type = 'button';
          again.className = 'card-action-btn';
          again.textContent = 'Retry';
          again.addEventListener('click', function() { runCardAction(button); });
          msg.appendChild(document.createTextNode(' '));
          msg.appendChild(again);
        }
      });
    })
    .catch(function() { if (msg) msg.textContent = 'Could not reach the server.'; });
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('click', function(event) {
    var button = event.target && event.target.closest ? event.target.closest('[data-card-action]') : null;
    if (button) { event.preventDefault(); event.stopPropagation(); runCardAction(button); }
  });
}
