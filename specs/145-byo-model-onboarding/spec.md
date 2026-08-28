# Feature Specification: BYO-Model Onboarding

**Feature Branch**: `145-byo-model-onboarding`
**Created**: 2026-08-28
**Status**: Draft
**Issue**: [#199](https://github.com/ViviDynamics/coordinare/issues/199) (launch-blocking, the last of three)

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Nothing a stranger sees points at our infrastructure (Priority: P1)

Someone who clones coordinare should never encounter an address, hostname, or model identifier
that belongs to ViviDynamics. Not because those are secret, but because they are useless to the
reader and actively misleading: a config example naming a host that does not exist for them is a
config example that cannot work.

**Why this priority**: This is the one that makes everything else possible. A preset that
references an internal host is not a preset, and a quickstart that assumes our model roster is
not a quickstart. It is also the item the spec-142 pre-public scrub got wrong, so it needs to end
with something better than another manual sweep.

**Independent Test**: An automated check over everything a user reads finds no internal address,
hostname, or model identifier, and that check demonstrably fails when one is reintroduced.

**Acceptance Scenarios**:

1. **Given** the tracked tree excluding `specs/`, **When** the guard runs, **Then** no
   user-facing file contains the internal model-host address, the internal gateway hostname, or
   an internal model identifier prefix.
2. **Given** a developer reintroduces one, **When** the suite runs, **Then** it fails and names
   the file and the offending value.
3. **Given** the `specs/` tree, **When** the guard runs, **Then** it is excluded, because those
   are a historical record and rewriting them would falsify the project's own history.
4. **Given** illustrative addresses that are not real infrastructure (documentation placeholders,
   test fixtures demonstrating a non-loopback bind), **When** the guard runs, **Then** it does not
   flag them. A guard that cries wolf gets weakened.

---

### User Story 2 - A stranger can point coordinare at their own model endpoint (Priority: P1)

A first-time self-hoster has Ollama on their laptop, or an OpenAI-compatible endpoint, or an
Anthropic API key. They should find a worked configuration for their case and adapt it by
changing an address and a model name, not by reverse-engineering which of nine personas need
which settings.

**Why this priority**: Equal to US1 and dependent on it. This is the adoption make-or-break: the
core loop works, and what fails for outsiders is the first thirty minutes.

**Independent Test**: For each of the three provider shapes, a preset exists that a reader can
adapt by editing only an endpoint address and model names, and its structure is validated by test
rather than by eye.

**Acceptance Scenarios**:

1. **Given** the presets, **When** a reader looks for their provider, **Then** one of Ollama
   local, OpenAI-compatible, or Anthropic API covers it.
2. **Given** a preset, **When** it is loaded, **Then** it validates against coordinare's own
   configuration validator, so a preset cannot ship broken.
3. **Given** a preset, **When** a reader adapts it, **Then** the values they must change are
   marked, and no value they must change is buried in an unrelated section.

---

### User Story 3 - Setup failures are diagnosed before a card is dispatched (Priority: P2)

An operator who has misconfigured something should learn which thing, at startup, with the fix
named. They should not discover a missing token permission when a card fails halfway through a
run, having already spent model budget.

**Why this priority**: Below the presets because it improves a path that exists rather than
creating one. It matters because the failure it prevents is expensive and confusing: a
mid-pipeline failure looks like coordinare being broken rather than coordinare being unconfigured.

**Independent Test**: With a deliberately broken configuration (unreachable endpoint, absent
model, insufficient token scope), a preflight reports each problem and names its fix, without
dispatching anything.

**Acceptance Scenarios**:

1. **Given** an unreachable model endpoint, **When** preflight runs, **Then** it reports that
   endpoint and what to check.
2. **Given** a reachable endpoint that does not serve the configured model, **When** preflight
   runs, **Then** it says so and lists what the endpoint does serve, since that is almost always
   the fix.
3. **Given** a token missing a required permission, **When** preflight runs, **Then** it names
   the permission, cross-referenced with spec 144's token matrix.
4. **Given** every check passing, **When** preflight runs, **Then** it says so plainly and exits
   successfully, so it is usable as a setup gate.

---

### User Story 4 - A newcomer is not asked to provision nine personas (Priority: P2)

Coordinare has nine performer roles. A newcomer should be told which are required to see a card
move end to end, and which are optional, so they can reach a first result and then add roles.

**Why this priority**: Cheap and high-leverage. The nine-role table is impressive and, to someone
deciding whether to try this at all, intimidating.

**Independent Test**: Documentation states the minimum set, and a configuration containing only
that set validates and runs.

**Acceptance Scenarios**:

1. **Given** the documentation, **When** a newcomer looks for the minimum, **Then** the required
   roles are named and the rest are marked optional.
2. **Given** a minimal configuration, **When** it is validated, **Then** it passes.

---

### Edge Cases

- **Not every internal-looking string is internal.** Documentation placeholders (`10.20.30.40`),
  and test fixtures that demonstrate a non-loopback bind (`192.168.1.50` in the spec-144 guard
  tests), are legitimate and must not be flagged. The guard distinguishes specific known-internal
  values from the general shape of a private address.
- **`specs/` is excluded deliberately.** It is a historical record. Rewriting it to remove
  references that were true at the time would falsify the project's own history. Whether that
  tree is published at all is a separate decision, and this feature does not make it.
- **Comments are not defaults.** Most `spark/*` occurrences in source are illustrative comments
  rather than configured values. They still mislead a reader and are still replaced, but the
  distinction matters for judging risk honestly.
- **A preset that validates is not a preset that works.** Structural validation cannot prove an
  endpoint answers. That gap is what US3's preflight covers, and neither substitutes for the
  other.
- **The guard must be shown to fail.** The spec-142 scrub recorded a clean result from a grep that
  silently matched nothing, because `git grep -E` does not support `\b`. Any check added here must
  be demonstrated failing on a known-present value before its passing result means anything.

## Requirements *(mandatory)*

### Functional Requirements

**Removing internal references**

- **FR-001**: No tracked file outside `specs/` may contain the internal model-host address
  (`192.168.3.30`), the internal gateway hostname (`litellm.vividynamics.com`), or an internal
  model-identifier prefix (`spark/`). Verified counts at 2026-08-28: **12**, **8**, and **48**
  files respectively.
- **FR-002**: An automated check MUST enforce FR-001 and MUST name the file and the offending
  value on failure.
- **FR-003**: The check MUST exclude `specs/`, and MUST NOT flag documentation placeholders or
  test fixtures that merely resemble internal values.
- **FR-004**: The check MUST be demonstrated to fail on a reintroduced value. A check never shown
  to fail is indistinguishable from one that is not running, which is exactly how the spec-142
  scrub recorded a false clean result.
- **FR-005**: Replacements MUST be values a reader can act on: a documented placeholder hostname
  or a generic model name, never a different real address.

**Provider presets**

- **FR-006**: Worked configurations MUST exist for Ollama local, any OpenAI-compatible endpoint,
  and the Anthropic API.
- **FR-007**: Each preset MUST validate against coordinare's own configuration validator, enforced
  by test, so a broken preset cannot ship.
- **FR-008**: Each preset MUST mark the values a reader is expected to change.

**Preflight**

- **FR-009**: A preflight check MUST verify model-endpoint reachability, presence of the
  configured models, GitHub token permissions, and board configuration.
- **FR-010**: Every preflight failure MUST name the concrete fix.
- **FR-011**: A model-not-found failure MUST list what the endpoint does serve.
- **FR-012**: Preflight MUST be runnable as a standalone setup gate and report success plainly.

**Documentation**

- **FR-013**: A golden-path walkthrough MUST take a reader from clone to first dispatched card
  using a preset, with no internal references encountered.
- **FR-014**: The minimum-viable persona set MUST be documented, distinguishing required roles
  from optional ones.

## Success Criteria *(mandatory)*

- **SC-001**: Zero tracked non-spec files contain the three internal values, verified by a check
  that has been demonstrated to fail when one is reintroduced.
- **SC-002**: A reader with Ollama can adapt a preset by editing only an endpoint and model names.
- **SC-003**: Each preset validates against the real configuration validator.
- **SC-004**: A deliberately broken configuration produces a preflight report naming each problem
  and its fix, with nothing dispatched.
- **SC-005**: The minimum persona set is documented and a configuration containing only it
  validates.
- **SC-006**: No existing test regresses, and no continuous-integration workflow changes its
  triggers or required status.

## Assumptions

- `specs/` is excluded from the guard. Whether it is published is a separate, unmade decision,
  recorded on spec 142's pre-public scrub.
- Replacing illustrative `spark/*` comments is worth doing even though they are not defaults,
  because they teach a reader a naming convention that is meaningless outside our infrastructure.
- Preflight verifies what it can reach. It cannot prove a model will produce good output, only
  that the endpoint exists and serves the name configured.
- The spec-142 scrub's private-address row was corrected on 2026-08-28 from a false clean result.
  This feature is what closes the item it should have flagged.

## Out of Scope

- Publishing the repository.
- The `specs/` tree's contents, or the decision about publishing it.
- Dashboard authentication (spec 143), threat model (spec 144, merged).
- Changing which model backends are supported, or adding new ones.
