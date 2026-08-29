# Feature Specification: Notification Channels Fully Optional

**Feature Branch**: `139-optional-notification-channels`
**Created**: 2026-08-29
**Status**: Draft
**Issue**: [#187](https://github.com/ViviDynamics/coordinare/issues/187)

## Overview

A developer disabled Slack during setup because it "wasn't working", and then could not tell
whether agents were stuck. Two things have to be true for that to be a supported choice rather
than a degraded one: turning a channel off must not break anything, and the stall signal must
still reach them.

## Rescope

The issue was written before the code was checked. Verified against `main` at 07dd94e, **two of
its four scope items are already satisfied and one names the wrong cause**. This spec covers what
is actually missing.

| Issue item | Reality |
|---|---|
| 1. Zero channels is supported | **Already true.** `channels` and `routing` both default to `[]` and load cleanly. Needs a regression test, not an implementation. |
| 2. A failing channel is non-fatal | **Half true, wrong layer.** *Delivery* failures are already absorbed — `notification.py:210` gathers with `return_exceptions=True`. *Load* failures are not absorbed anywhere, and that is the harder failure. |
| 3. Stall visible without channels | **Mostly true.** `daemon.py:3205` records to the activity feed before dispatching. The remaining gap is narrower: that feed is only readable through the dashboard. |
| 4. Startup posture line | **Missing**, as described. |

The reported pain is almost certainly item 2 at the layer the issue did not look at. A Slack
channel missing its `webhook_url`, or a routing entry naming a channel that was removed, raises
during config load — so coordinare does not start at all. An operator part-way through setup gets a
daemon that will not boot, which reads as "Slack isn't working" and is fixed by deleting the
channel.

## Clarifications

### Session 2026-08-29

- Q: Turning load errors into warnings weakens validation. Where is the line? → A: By subsystem.
  Notifications are non-essential: coordinare's work is unaffected by their absence, so a
  misconfiguration degrades that channel and coordinare runs. Essential configuration — GitHub,
  board, model endpoints — still refuses to start, because proceeding would do the wrong thing
  rather than merely tell nobody about it.
- Q: Does relaxing this hide typos? → A: It must not. `config validate` continues to report these
  as errors, so the tool for finding configuration problems still finds them; only the daemon
  declines to treat them as fatal. Strict in the linter, forgiving at runtime.
- Q: What is the always-available stall surface, given the feed needs the dashboard? → A: The log.
  A stall is currently never logged at all — only its *notification failure* is. A warning-level
  log line costs nothing, needs no subsystem, and is visible to an operator running headless.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Setup survives a half-configured channel (Priority: P1) — MVP

An operator adds a Slack channel, has not pasted the webhook URL yet, and starts coordinare. It
runs, tells them that channel is inactive and why, and does its work.

**Why this priority**: This is the reported failure. Everything else improves a situation the
operator never reaches if the daemon will not boot.

**Independent Test**: Start with a `slack` channel lacking `webhook_url`; the daemon runs and logs
one line naming the channel and the missing field.

**Acceptance Scenarios**:

1. **Given** a channel missing a required field, **When** coordinare starts, **Then** it starts,
   that channel is inactive, and one log line names the channel and what is missing.
2. **Given** a routing entry naming a channel that no longer exists, **When** coordinare starts,
   **Then** it starts, that entry is ignored, and one log line names the entry and the missing
   channel.
3. **Given** either of the above, **When** the operator runs `config validate`, **Then** it is
   still reported as an error, because that is the tool for finding configuration problems.
4. **Given** a channel that is fully configured, **When** coordinare starts, **Then** nothing about
   its behaviour changes.

### User Story 2 - Know where alerts will land (Priority: P1)

An operator reads one line at startup and knows whether anything will tell them a card is stuck.

**Why this priority**: Equal-first. Silence is indistinguishable from health, and the developer in
the issue could not tell which they had.

**Independent Test**: Start with zero channels and read the line; start with a working channel and
read the difference.

**Acceptance Scenarios**:

1. **Given** no channels, **When** coordinare starts, **Then** one line says no channels are
   configured and names where stall signals will appear.
2. **Given** active channels, **When** coordinare starts, **Then** the line names them.
3. **Given** a channel that was skipped as misconfigured, **When** coordinare starts, **Then** the
   line distinguishes active from inactive.
4. **Given** any configuration, **When** the line is emitted, **Then** it contains no webhook URL,
   token, or other credential.

### User Story 3 - A stall is visible with nothing else running (Priority: P2)

An operator running headless — no channels, no dashboard — still learns that a card is stuck.

**Why this priority**: The gap is real but narrower than the issue implies, and only reachable by
an operator who has turned off both surfaces.

**Independent Test**: Trigger the stall watchdog with no channels and no dashboard reader; a
warning-level log line names the card and how long it has been stuck.

**Acceptance Scenarios**:

1. **Given** a stalled card, **When** the watchdog fires, **Then** a warning-level log line names
   the card and the duration, regardless of channel or dashboard configuration.
2. **Given** channels are configured, **When** the watchdog fires, **Then** the log line is emitted
   as well as the notification, not instead of it.
3. **Given** the same card stays stuck, **When** the watchdog fires repeatedly, **Then** the log
   does not repeat without limit.

### Edge Cases

- **Every channel is misconfigured.** Coordinare runs with none active, and the posture line says
  so plainly rather than implying alerts are covered.
- **A channel is valid but unreachable.** Already handled by the retry and circuit-breaker path;
  it must stay that way, and a regression test should pin it since this spec touches nearby code.
- **A routing entry references a channel that was skipped**, not one that never existed. Both
  ignore the entry, but the reason differs and the message should reflect which.
- **A misconfiguration that is not about a missing field** — an unparseable template, a malformed
  URL. Same treatment: that channel is inactive, coordinare runs.
- **Repeating the posture line.** It is emitted once at startup, not per cycle.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A channel that cannot be used MUST NOT prevent coordinare from starting. It is
  inactive; coordinare runs.
- **FR-002**: A routing entry naming a channel that is absent or inactive MUST NOT prevent
  coordinare from starting. The entry is ignored.
- **FR-003**: Each skipped channel and ignored routing entry MUST be reported once, naming what was
  skipped and why, so the degradation is discoverable without reading the config.
- **FR-004**: `config validate` MUST continue to report these as errors. Relaxing the daemon must
  not blunt the tool for finding configuration problems.
- **FR-005**: A fully-configured channel's behaviour MUST be unchanged.
- **FR-006**: Coordinare MUST emit exactly one startup line describing the effective notification
  posture: which channels are active, which were skipped, and where stall signals will appear.
- **FR-007**: That line MUST contain no credential — no webhook URL, token, or password.
- **FR-008**: A stall MUST be recorded at warning level in the log whenever the watchdog fires,
  independent of any channel or the dashboard.
- **FR-009**: The stall log MUST NOT repeat unboundedly for a card that stays stuck.
- **FR-010**: Delivery failures MUST remain non-fatal and MUST NOT block a card, unchanged.
- **FR-011**: Zero channels MUST remain a clean configuration, locked by test.

### Key Entities

- **Channel**: a delivery destination, now either active or skipped-with-a-reason.
- **Routing entry**: a mapping from event to channels, now ignored when its channels are unusable.
- **Notification posture**: the derived summary reported at startup.
- **Stall signal**: emitted to the log always, to the activity feed, and to channels when any are
  active.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Coordinare starts and completes a cycle with a channel that is missing a required
  field.
- **SC-002**: Coordinare starts and completes a cycle with a routing entry naming a channel that
  does not exist.
- **SC-003**: `config validate` still reports both as errors.
- **SC-004**: An operator can tell from one startup line whether anything will alert them.
- **SC-005**: No credential appears in that line, verified by test.
- **SC-006**: A stalled card produces a warning-level log line with no channels and no dashboard.
- **SC-007**: A fully-configured channel behaves exactly as before, evidenced by existing tests
  passing unmodified.
- **SC-008**: Zero channels remains clean.

## Assumptions

- Notifications are non-essential to coordinare's work. This is the premise the whole spec rests
  on: it justifies degrading rather than failing, and it does not extend to any other subsystem.
- Operators read startup logs. If they do not, the posture line helps nobody — but no cheaper
  surface exists for someone running headless.
- The existing retry and circuit-breaker path for delivery is adequate and is not revisited.

## Out of Scope

- Any change to how notifications are *delivered*: retry policy, circuit breakers, templates.
- New channel kinds.
- Making the dashboard activity feed readable without the dashboard.
- Alert routing or escalation policy.
- Relaxing validation for any subsystem other than notifications. GitHub, board and model-endpoint
  configuration still refuse to start, and deliberately so.
