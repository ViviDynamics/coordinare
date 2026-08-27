# Phase 1 Data Model: License and Legal Posture

**Feature**: 142-license-and-legal-posture | **Date**: 2026-08-27

**No persistence.** This feature adds no coordinare state, no `state_store.py` field, and no
schema version bump. The entities below are in-memory shapes that exist only inside the
enforcement test and the audit generator, described here because the spec names them and because
the shapes determine what the checks can assert.

---

## PackageLicense

One distributed package and what it is licensed under. Built by the audit derivation (research
R3) and consumed by the FR-020 check.

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Canonical distribution name as it appears in `uv.lock`. |
| `version` | `str` | From the lock, not from the installed environment, so the audit describes what is pinned. |
| `license_id` | `str` | Resolved by precedence: `License-Expression`, then short free-text `License`, then `License ::` classifier. `"UNKNOWN"` when none resolves. |
| `source_field` | `str` | Which of the three fields supplied `license_id`. Recorded so a reader can judge how trustworthy the identifier is. |
| `consumed_as` | `"import" \| "subprocess"` | Which position the package occupies (research R6). Defaults to `import`; `subprocess` is set explicitly for the small set of executables. |
| `distributed_via` | `"pypi_dependency" \| "container_image"` | Distinguishes a declared dependency a user resolves themselves from a binary baked into an image we ship. |

**Derivation rule**: transitive closure over `uv.lock` from each distributed project's declared
runtime dependencies. Development dependency groups are excluded, since development dependencies
are never conveyed to recipients.

**Validation rules**:
- `license_id` of `"UNKNOWN"` is a rejection, not a gap to be filled in later (FR-019).
- A package in the closure with no installed metadata is a hard error, not a skip. A silent skip
  would let an unlicensed package pass by being absent.
- First-party packages (`coordinare`, `performer`, `coordinare-service-inference`) are exempt from the
  allowlist and instead asserted to declare the ELv2 reference (FR-003, FR-004). They currently
  report `UNKNOWN`, which is precisely the gap FR-003 closes.

---

## LicenseVerdict

The result of judging one `PackageLicense` against the accepted set.

| Field | Type | Notes |
|---|---|---|
| `package` | `PackageLicense` | The subject. |
| `outcome` | `"permitted" \| "exception" \| "rejected"` | Three states, not two. The middle one is the point. |
| `rationale` | `str \| None` | Required and non-empty when `outcome == "exception"`; `None` otherwise. |

**Validation rule**: an `exception` without a rationale fails the check. This is what stops the
exception mechanism from decaying into a second allowlist that nobody has thought about. Adding a
weak-copyleft package stays possible, but it costs a sentence explaining why it is acceptable.

---

## PostureFile

A public-facing document subject to the wording guard (research R2).

| Field | Type | Notes |
|---|---|---|
| `path` | `Path` | Repository-relative. |
| `must_exist` | `bool` | True for the six required documents (FR-001, FR-002, FR-008, FR-014, and the two templates). |
| `required_phrases` | `list[str]` | Text that must be present, e.g. `source-available` in the README. |
| `forbidden_phrases` | `list[str]` | `open source`, `open-source`, `OSI`, matched case-insensitively. |
| `allowed_exceptions` | `list[tuple[str, str]]` | `(path, matched text)` pairs deliberately permitted. Empty at implementation time. |

**Why `allowed_exceptions` exists while starting empty**: the guard is a file-scoped total ban,
so the first legitimate third-party mention inside a posture file would otherwise leave an author
with no option but to weaken the check. An explicit escape hatch keeps the failure a conversation
rather than a deletion.

---

## AuthorRelationship

Derived per pull request by the external-contributions workflow. Not stored.

| Value | Source | Action |
|---|---|---|
| `internal` | `author_association` in `OWNER`, `MEMBER`, `COLLABORATOR` | None. Silent. |
| `bot` | `user.type == "Bot"` | None. Silent. |
| `external` | `author_association` in `NONE`, `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER` | Comment, label, close. |
| `external_security` | `external` **and** a security keyword matches title or body | Comment, label. **Do not close.** |

**State transition**: a pull request moves through this classification exactly once per `opened`
or `reopened` event. There is no stored state and no reconciliation; the classification is
re-derived from the event each time, which is why a reopened pull request is re-evaluated rather
than remembered.

**Trust note**: `author_association` and `user.type` are computed by GitHub and cannot be set by
the author. The security keyword match is the only input drawn from author-controlled text, and
its sole effect is to suppress the close (research R4).

---

## ScrubItem

One line of the pre-public checklist (FR-021). A recorded document, not code.

| Field | Notes |
|---|---|
| `check` | A concrete command or UI step, never an intention. "Grep the history for `.env`" rather than "make sure no secrets leaked". |
| `scope` | Working tree, git history, or repository settings. |
| `outcome` | Recorded when executed: what was found and what was done. |
| `owner` | This feature, or another spec (145 owns removing internal infrastructure references; 144 owns hardening posture). |
| `timing` | `now` or `at_flip`. Items that can only be done on a public repository, such as enabling private vulnerability reporting (research R5), are marked `at_flip`. |

**Why `timing` is a field rather than a note**: R5 established that at least one required step is
impossible until the repository is public. Without an explicit marker, a checklist that is fully
ticked would imply a state that has not been reached.
