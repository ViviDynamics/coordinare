# Research: Optional Notification Channels (spec 139)

**Date**: 2026-08-29 | **Verified against**: main @ 07dd94e

## R1 — Where the failure actually is

**Finding**: not delivery. Config load.

```python
NotificationsConfig(channels=[{"name": "slack", "kind": "slack"}])
# ValidationError: webhook_url  Field required        config.py:116

NotificationsConfig(channels=[], routing=[{"event_type": "card_stuck", "channels": ["ghost"]}])
# ValidationError: Routing entry references unknown channel 'ghost'    config.py:158
```

Delivery is already resilient: `notification.py:210` gathers with `return_exceptions=True`, and a
circuit breaker sits behind it. So the issue's "a failing channel must not stall the daemon" is
satisfied for the case it describes, and unsatisfied for the case it does not — a channel that
fails *before* the daemon exists.

**Consequence for the fix**: the change belongs at the boundary between config and the notification
service, not in delivery.

## R2 — Where to degrade: the validator, or the consumer?

**Decision**: keep the validators, and have the *daemon* tolerate what they reject.

**Rationale**: the validators are also what `config validate` uses. Deleting the rule would fix the
daemon and blind the linter at the same time, which trades a boot failure for a silent typo — the
worse of the two, because a boot failure at least tells you immediately.

**Alternatives considered**:

- *Delete the validation.* Rejected: `config validate` stops reporting the problem, so a typo in a
  channel name becomes undiscoverable.
- *A `strict` flag on the model, set false by the daemon.* Rejected as a second code path through
  validation that both callers must be reasoned about together; the failure mode is a rule that is
  enforced in tests and not in production.
- *Validate leniently, then re-validate strictly in `config validate`.* This is the chosen shape,
  expressed as: the model keeps its rules; the daemon builds its channel set from the entries that
  pass, and reports the rest.

## R3 — Which failures degrade, and which still stop the daemon

**Decision**: only notification configuration degrades.

**Rationale**: the test is what happens if coordinare proceeds. With a broken notification channel it
does its work and tells nobody — recoverable, and the posture line makes it visible. With a broken
GitHub or model-endpoint configuration it would do the *wrong* work, or none, while appearing
healthy. Those must keep failing loudly, and this spec must not become a precedent for relaxing
them.

## R4 — The always-available stall surface

**Finding**: a stall is currently never logged. Searching `daemon.py` around the watchdog turns up
exactly one log line, `stuck_card_notification_failed` — a message about the *notification*
failing, not about the card being stuck. With no channels there is nothing to fail, so nothing is
logged at all.

**Decision**: log the stall itself at warning level, unconditionally.

**Rationale**: it costs one line, depends on no subsystem, and is visible to an operator running
headless. The activity feed already receives the signal (`daemon.py:3205`), but reading it needs
the dashboard.

**Deduplication**: the notification path already computes a `dedup_key`
(`f"stuck:{card_id}:{phase}"`). Reusing it keeps the log and the notification agreeing about what
counts as the same stall, rather than inventing a second policy that drifts.

## R5 — The posture line

**Decision**: one line at startup, structured, listing active channels by name, skipped channels
with reasons, and the surfaces stall signals will reach.

**Rationale**: the developer in the issue could not distinguish "nothing is wrong" from "nothing is
watching". That is the whole problem, and it is answered by a sentence.

**Credential safety**: the line names channels, never their configuration. Channel *names* are
operator-chosen labels; webhook URLs are secrets. A test asserts no configured secret value appears
in the emitted line, rather than trusting the implementation to have selected the right fields.

## R6 — Not breaking what works

`config.py`'s notification models are used by the dashboard config API and by `config validate`.
The chosen shape touches neither: the models keep their rules, and only the daemon's construction
of its channel set changes. Existing tests for both should pass unmodified, which is the evidence
FR-005 and SC-007 ask for.
