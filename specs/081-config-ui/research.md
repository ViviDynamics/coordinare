# Phase 0 Research: Live Config Editing in the Dashboard UI

**Feature**: 081-config-ui | **Date**: 2026-06-06 | **Plan**: [plan.md](./plan.md)

This document consolidates the design decisions that resolve the Technical Context
unknowns and the spec's clarified requirements. Each decision is grounded in the
existing coordinare/performer code rather than introducing new machinery.

---

## D1. Descriptor derivation from the pydantic models

**Decision**: Derive UI descriptors at request time by introspecting the existing
pydantic v2 models (`ProjectConfiguration`, `CoordinareConfiguration`, `PersonasConfig`,
`PerformersConfig`, `Endpoint`, `ModelEndpoint`, `Mode`, `SymphonyConfig`) via
`model_fields` / `model_json_schema()`. A descriptor carries: `key`, `label`,
`help` (from `Field(description=...)`), `type`, `current_value`, `default`
(from `FieldInfo.default` / `default_factory`), `range`/`enum` (from field
constraints and `Literal`/`Enum` annotations), and the three flags
(`editable`, `restart_required`, `secret`), plus `section` and `store`.

**Rationale**:
- The pydantic models are already the single source of truth (per out-of-scope: no
  schema redesign). Introspecting them means descriptors never drift from validation.
- pydantic v2 exposes constraints (`ge`/`le`/`min_length`/`max_length`), `Literal`
  members, and `Field(description=...)` through `model_fields` and the generated JSON
  schema — everything the UI needs for label/help/type/range/enum.
- Flags that can't be inferred from the type system (`editable`, `restart_required`,
  `secret`) are supplied by a small, explicit per-field **annotation table** in
  `config_descriptors.py` keyed by dotted path. This keeps policy (what is hot-reloadable,
  what is a secret) in one auditable place instead of scattered across the models.

**Alternatives considered**:
- *Hand-maintained descriptor catalog* (a static dict of every setting): rejected —
  guaranteed to drift from the models, doubles the maintenance surface, violates the
  "understandable AND correct" intent.
- *Subclassing/annotating the config models with UI metadata*: rejected — couples the
  validation models to the dashboard, and the out-of-scope rule forbids schema redesign.

---

## D2. Spec-078 routing config: coordinare → performer write plumbing

**Decision**: The dashboard reads and writes the **host-side routing-table YAML file**
that performer endpoints already mount into their containers. There is no in-flight
injection into running performer jobs; the **next performer job picks up the edited
file at job start** (performer `main.py` loads `SELFHOSTED_ROUTING_CONFIG` once at
startup). The coordinare locates this file from the performer-endpoint config's
`volumes` (the `VolumeMount` whose container path the `SELFHOSTED_ROUTING_CONFIG`
env points at); `routing_config_service.py` resolves the host path, reads/writes it,
and validates against the spec-078 models.

**Rationale** (grounded in the Explore finding):
- There is **no automatic coordinare→performer injection** of the routing config today.
  It is operator-wired: a `VolumeMount {host_path, container_path, mode}` mounts a host
  YAML into the container and `SELFHOSTED_ROUTING_CONFIG` points to the container path.
- The routing table is **not** a dispatch-payload field — `JobInitPayload`
  (`http_performer_service.py`) carries no routing field — so there is nothing to inject
  per-job even if we wanted to.
- The performer loads the file **once at job start** (`main.py` ~L1110-1117). Editing the
  host file is therefore the correct and only seam: the change is durable and applies to
  every subsequent job, with the well-understood semantic "takes effect on the next job".
- Writing the same file the performer reads, validated with the same spec-078 models the
  performer enforces, gives validation parity with zero new transport.

**Operator-facing semantic surfaced in the UI** (FR + UX): routing-table edits are
flagged **"applies to next performer job"** (a restart-required-style staged state),
distinct from coordinare settings that hot-reload immediately.

**Alternatives considered**:
- *In-flight injection into running performers*: rejected — no transport exists, the file
  is read once at startup, and adding a live-push channel is out of scope and risky.
- *Adding routing to the dispatch payload*: rejected — expands the contract surface and
  duplicates a file the performer already mounts; would require a contract-registry change.
- *Editing inside the container*: rejected — ephemeral containers are discarded; the host
  file is the durable source.

---

## D3. Secret handling (`${VAR}` literals and secret-flagged fields)

**Decision**: Two layered rules, applied centrally in the descriptor serializer:
1. **`${VAR}` placeholders are displayed and edited as the raw literal string**
   (e.g. `${COORDINARE_GITHUB_TOKEN}`), never the expanded value. On save the raw literal
   is written back verbatim — the dashboard never calls `os.path.expandvars` on the way in
   or out. Expansion remains exclusively at config load time (`from_yaml`).
2. **Secret-flagged fields** (per the D1 annotation table — e.g. `github_token`,
   `auth_env`-referenced credentials) are **masked** in every descriptor payload
   (`current_value` redacted to a sentinel like `"••••••"` when the value is a non-`${VAR}`
   literal). A masked field that is submitted unchanged is treated as "no change"; only an
   explicitly edited value overwrites.

**Rationale**:
- Satisfies the clarified FR-017 (raw `${VAR}`, masked secrets, never substitute) and the
  hard constraint "never expose or log secrets/tokens."
- Centralizing masking in the serializer means no endpoint can accidentally leak a secret —
  there is one choke point, easy to unit-test.
- The "masked + unchanged = no-op" rule lets an operator edit a neighboring field in the
  same section without being forced to re-enter secrets.

**Alternatives considered**:
- *Show expanded values read-only*: rejected — leaks the resolved secret and contradicts
  FR-017's raw-literal requirement.
- *Forbid editing any section containing a secret*: rejected — too coarse; operators need
  to edit non-secret neighbors.

---

## D4. Optimistic concurrency (external-edit detection)

**Decision**: On every config read, the server computes and returns a **content hash**
(SHA-256 of the on-disk file bytes) alongside the descriptors. Every save submits that
hash as an `If-Match`-style precondition. The write service re-reads the file, recomputes
the hash, and **rejects the save with a 409-style conflict** if it differs from the
submitted baseline — signalling the file changed underneath the editor (manual edit,
another operator, a `git` checkout). The operator is told to reload and re-apply.

**Rationale**:
- Implements clarified FR-016 (detect-and-warn) with a deterministic, testable mechanism.
- Content hash (not mtime alone) is robust to clock skew and same-second writes; mtime can
  be carried as a secondary cheap check but the hash is authoritative.
- Fits the existing atomic-write pattern: the hash check happens immediately before the
  `tempfile` + `os.replace` swap, under the same code path, so there is no TOCTOU window
  wider than the existing write.

**Alternatives considered**:
- *Last-writer-wins (no check)*: rejected — silently destroys concurrent/manual edits, the
  exact failure the clarification set out to prevent.
- *File locking (flock)*: rejected — single-process dashboard doesn't need cross-process
  locks for correctness here, and locks don't catch out-of-band `git`/editor changes; the
  hash precondition does.

---

## D5. Atomic write-back and comment preservation

**Decision**: Reuse the established atomic pattern — `tempfile.mkstemp(dir=config_path.parent)`
→ write → `os.chmod` to preserve the original mode → `os.replace` (POSIX-atomic rename) —
in a shared `config_write_service.py` used by all save endpoints (config.yaml and the
routing YAML). **Round-trip writes via `yaml.safe_dump`**; accept that this strips
file-level comments, and mitigate by (a) documenting it in quickstart, and (b) scoping
section-level writes so a save rewrites the whole file deterministically from the parsed
+ edited model, not a hand-merge.

**Rationale**:
- The pattern is already proven in the persona/global write paths; reusing it keeps one
  write discipline and avoids partial/corrupt config under mid-write failure.
- `safe_dump` stripping comments is a known limitation; since config.example*.yaml carry the
  documentation and the dashboard now *is* the documentation surface (labels/help), losing
  inline comments in the live `config.yaml` is an acceptable, documented trade-off rather
  than a blocker.

**Alternatives considered**:
- *ruamel.yaml round-trip to preserve comments*: rejected for this round — new dependency,
  and the descriptor UI supersedes inline comments as the human-readable layer. Noted as a
  possible future enhancement, not in scope.

---

## D6. Referential integrity & delete-protection for spec-080 catalogs

**Decision**: Catalog CRUD reuses the existing `_validate_orchestration_catalogs()`
invariants (config.py): `model_endpoint.endpoint` → `endpoints`; `mode.tool/thinking/
classifier` → `model_endpoints`; `performers[role].mode` → `modes`. The write service
runs the **full pydantic + cross-catalog validation on the proposed post-edit config**
before persisting, and additionally enforces **delete-protection**: deleting an `endpoint`,
`model_endpoint`, or `mode` that is still referenced is rejected with an actionable error
naming the referencing entity. This satisfies clarified FR-018 (full CRUD with referential
integrity).

**Rationale**:
- Validating the *whole proposed config object* through the existing models means a save can
  never produce a config the daemon would reject at load — the dashboard and the loader agree
  by construction.
- Delete-protection-while-referenced prevents dangling references that would otherwise only
  surface as a load-time failure on the next reload/restart.

**Alternatives considered**:
- *Cascade delete*: rejected — silently removing referencing modes/performers is destructive
  and surprising; explicit error + operator action is safer.
- *Allow dangling refs, fail at reload*: rejected — defers the error to a worse moment and
  contradicts "reject invalid input before it is persisted."

---

## D7. Hot-reload vs restart-required classification

**Decision**: The D1 annotation table marks each editable field `restart_required: true|false`.
Fields that the daemon's existing `_handle_config_reload()` swaps live (the state-dict /
config_version path) are `false` (hot-reloadable); fields consumed only at process start or
that change container/topology are `true`. After a successful config.yaml save the dashboard
triggers the existing `POST /api/config/reload` path; routing-YAML saves are always flagged
"applies to next performer job" (D2). The UI visually distinguishes live-applied vs staged.

**Rationale**:
- Reuses the existing, proven hot-reload trigger (`daemon._config_reload_trigger` →
  `_handle_config_reload()` atomic state swap + `config_version` bump) — no new reload path.
- An explicit per-field flag is honest about what actually takes effect immediately, which
  the spec requires the UI to communicate (UX Principle III + FR live/restart flagging).

**Alternatives considered**:
- *Assume everything hot-reloads*: rejected — false promise; some settings only bind at
  startup and the operator would be misled.
- *Force a restart for every change*: rejected — defeats the "live wherever safe" goal.

---

## D8 — Invalid-on-disk config at descriptor build

**Decision**: The descriptor layer renders **best-effort** — fields that parse get normal
descriptors; fields that fail validation render read-only with their raw value and an inline
"invalid value on disk" marker, and the owning section carries a non-blocking banner. The
`/all` payload returns 200, not 422.

**Rationale**:
- An operator's primary recovery path is the dashboard itself; hard-failing the view would
  lock them out of the one tool that can fix the bad value.
- Surfacing exactly which field is invalid (rather than a whole-payload error) tells the
  operator where to look.

**Alternatives considered**:
- *422 the whole `/all` payload on any invalid field*: rejected — strands the operator with
  no UI to repair the config that the daemon itself tolerated at load.

---

## Resolved unknowns summary

| Technical Context item | Resolved by |
|------------------------|-------------|
| Descriptor source of truth | D1 (introspect pydantic + annotation table) |
| 078 coordinare→performer plumbing | D2 (write host-side mounted YAML; next job reads it) |
| Secret / `${VAR}` handling | D3 (raw literal, central masking) |
| Concurrent/external edits | D4 (content-hash optimistic concurrency) |
| Atomic write / comments | D5 (tempfile+os.replace; safe_dump trade-off) |
| 080 catalog integrity + delete | D6 (full validation + delete-protection) |
| Live vs restart flagging | D7 (per-field flag + existing reload trigger) |
| Invalid-on-disk config at view time | D8 (best-effort render; read-only marker + banner, no 422) |

All `NEEDS CLARIFICATION` markers are resolved. Ready for Phase 1.
