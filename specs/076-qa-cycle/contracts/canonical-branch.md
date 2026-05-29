# Contract — Canonical Branch Name

**Module:** `src/coordinare/services/dispatch_guard.py`
**FRs:** FR-022, FR-023
**Clarifications:** Q2 (slug algorithm)

## Public API

```python
def compute_title_slug(title: str) -> str: ...

def canonical_branch_name(card: dict) -> CanonicalBranchName: ...

@dataclass(frozen=True)
class CanonicalBranchName:
    card_node_id: str
    title_slug: str
    @property
    def full_name(self) -> str:
        return f"coordinare/{self.card_node_id}/{self.title_slug}"
```

## Slug algorithm (deterministic)

```python
def compute_title_slug(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) <= 60:
        return s
    cut = s[:60].rsplit("-", 1)
    return cut[0] if cut[0] else s[:60]
```

## Determinism guarantees

- Pure function: no env, no time, no random.
- Idempotent: `compute_title_slug(compute_title_slug(t))` is undefined (slug input shouldn't be re-slugged), but `compute_title_slug(t)` is stable across processes, hosts, and Python versions.
- ≤60 chars on output.
- Empty / degenerate input (all whitespace, all symbols) → empty string. Callers MUST handle empty slug as a contract violation (refuse to dispatch, log `dispatch_performer.empty_slug_refused`).

## Test vectors (mandatory regression set)

| Input title | Expected slug |
|---|---|
| `"Feature: Time tracking schema and model foundation"` | `feature-time-tracking-schema-and-model-foundation` |
| `"Fix bug #123 in OAuth/SSO flow"` | `fix-bug-123-in-oauth-sso-flow` |
| `"  Already   Has   Whitespace  "` | `already-has-whitespace` |
| `"---leading and trailing dashes---"` | `leading-and-trailing-dashes` |
| `"ALLCAPS"` | `allcaps` |
| `"超長標題不是ASCII"` (no a-z0-9 at all) | `""` (empty — caller refuses) |
| `"a" * 200` | `"a" * 60` (truncated at length cap) |
| `"this-is-a-very-long-title-that-will-need-to-be-truncated-at-the-sixty-character-boundary-and-should-do-so-cleanly"` | `this-is-a-very-long-title-that-will-need-to-be-truncated-at` (truncated at the `-` before position 60) |

These vectors live in `tests/contract/test_canonical_branch_contract.py`. Any change to the algorithm MUST update the vectors AND bump a `SLUG_ALGORITHM_VERSION` constant for explicit migration.

## Resolution rules (FR-022)

When `dispatch_performer` resolves the branch for a card:

1. If exactly one open PR exists on the repo whose head ref starts with `coordinare/<card_node_id>/`:
   - Use that PR's existing head branch (preserves work even if title changed)
   - Verify it matches `canonical_branch_name(card).full_name`. If it matches → proceed. If not → flag as legacy-divergence (FR-024 path: refuse to dispatch + notify operator). Existing PR's branch wins for legitimate compatibility; mismatch is the error case.
2. If zero open PRs match the prefix:
   - Use `canonical_branch_name(card).full_name`
3. If ≥2 open PRs match the prefix:
   - FR-024: refuse dispatch, surface MultiPRDivergence record, emit `daemon.multi_pr_divergence_detected`

## Performer input contract (FR-023)

The performer's job-init payload (`JobInitPayload`) MUST include a new field:

```json
{
  ...
  "canonical_branch": "coordinare/PVTI_lADO…/feature-time-tracking-schema-and-model-foundation"
}
```

The performer's prompt template MUST instruct: "All commits MUST land on the branch named `{canonical_branch}`. Do NOT create a new branch under any circumstances; if your working tree appears to be on a different branch, switch back."

The performer's success-result schema (FR-017) MUST include `pushed_branch: str` so the orchestrator can verify the canonical branch was actually used.

## Verification on success-result ingestion

When `monitor_performer` processes a DONE outcome:
- Read `pushed_branch` from the performer's status.
- Compare with the canonical branch the dispatcher sent in `JobInitPayload`.
- If they disagree → reject the success as a contract violation; emit `monitor_performer.branch_contract_violated`; transition card to BLOCKED with a structured reason.

## Test coverage

- `tests/contract/test_canonical_branch_contract.py` — exhaustive test vector matrix; algorithm-version constant assertion.
- `tests/unit/services/test_canonical_branch_naming.py` — edge cases (empty title, all-symbol title, exactly-60-char title, 61-char title).
