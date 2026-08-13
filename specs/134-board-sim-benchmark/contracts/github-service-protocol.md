# Contract: GitHubServiceProtocol (the seam)

**Single source of truth** for what the coordinare lifecycle depends on from the
GitHub service. Both the real `GitHubService`
(`src/coordinare/services/github.py:449`) and `FakeGitHubService`
(`src/coordinare/services/fake_github.py`) MUST satisfy this. The Protocol lives in
`src/coordinare/graph/state.py` and is enforced by
`tests/unit/test_134_protocol_conformance.py` (asserts the real class and the fake
both structurally satisfy it).

Signatures below are the verified real-service signatures (return shapes are what
nodes rely on). `owner`/`repo` are `str`. All methods are `async` unless noted.

## Board / issues

| Method | Signature → return | Return shape nodes rely on |
|---|---|---|
| `poll_board` | `() -> dict` | `{"snapshot": {column: [item_id,…]}, …}` (`github.py:886`); columns `TODO/BLOCKED/IN_PROGRESS/IN_REVIEW/DONE` |
| `move_card` | `(item_id: str, status: str) -> None` | mutates the card's column |
| `get_issue_details` | `(issue_id: str) -> dict` | issue metadata |
| `check_issue_state` | `(repo, issue_number: int) -> str` | open/closed |
| `get_issue_comments` | `(issue_number: int, since_id: int\|None=None) -> list[dict]` | comment dicts (called via `getattr`, `notify.py:227`) |
| `list_open_issues` | `(owner, repo, first: int=20) -> list[dict]` | |
| `link_to_project` | `(content_id: str, status: str="IN_REVIEW") -> str\|None` | |

## PR / review

| Method | Signature → return | Return shape |
|---|---|---|
| `find_pr_for_issue` | `(issue_node_id: str) -> dict[str,str]\|None` | `{id, url, …}` or None |
| `count_closed_prs_for_issue` | `(issue_node_id: str) -> int` | |
| `get_pr_reviews` | `(pr_id: str) -> list[dict]` | dicts w/ `id, author_login, state, body, submitted_at, comments, commit_oid` |
| `get_pr_review_context` | `(pr_id: str) -> dict` | `{reviews, review_threads, head_oid, review_decision}` |
| `request_reviews` | `(pr_id: str, reviewer_logins: list[str]) -> dict` | |
| `request_reviewers` | `(owner, repo, pr_number: int, reviewers: list[str]) -> None` | |
| `check_mergeability` | `(pr_id: str) -> dict` | 6 keys: `mergeable`(=`MERGEABLE` AND `APPROVED`), `mergeable_raw`, `merge_state_status`, `review_decision`, `head_ref_oid`, `head_ref_name` |
| `squash_merge` | `(pr_id: str) -> dict` | `{merged: bool, id: str, merge_commit: {oid, messageHeadline}\|None}` |
| `add_comment` | `(subject_id: str, body: str) -> dict` | |
| `get_pr_files` | `(owner, repo, pr_number: int) -> dict` | |
| `get_pr_diff` | `(pr_url: str) -> tuple[str, list[str]]` | `(unified_diff, changed_files)` |
| `compare_changed_files` | `(pr_url, base_sha, head_sha) -> list[str]` | filenames |

## CI / checks

| Method | Signature → return | Notes |
|---|---|---|
| `get_required_status_checks` | `(owner, repo, default_branch) -> set[str] \| None` | `None` ⇒ branch protection unreadable → fallback; fake returns e.g. `{"pytest"}` |
| `fetch_failed_job_log` | `(owner, repo, job_id: int, max_chars=6000) -> str` | never raises; `""` on failure |

> **CI rollup is NOT on this Protocol.** `CheckEntry`/`CheckRollup` come from
> `PrChecksService` (`pr_checks_service.py`), which the node builds and caches on the
> service object at `github._pr_checks_service_cache[(owner,repo)]`
> (`monitor_performer.py:2297-2306`). The fake pre-populates that cache with a fake
> `PrChecksService` returning a `CheckRollup` built from real pytest. See
> research.md D4.

## Branches / repo / files

| Method | Signature → return |
|---|---|
| `branch_exists` | `(branch_name: str) -> bool` |
| `branch_has_open_pr` | `(branch_name: str) -> bool\|None` |
| `delete_branch` | `(branch_name: str) -> None` |
| `get_repository_id` | `(owner, repo) -> str` |
| `get_file_content` | `(owner, repo, path, ref="HEAD") -> str\|None` |
| `get_file_blob_sha` | `(owner, repo, path, ref="HEAD") -> str\|None` |
| `list_prs_by_branch_prefix` | `(owner, repo, prefix, state="OPEN", limit=20) -> list[dict]` |

## Labels

| Method | Signature → return |
|---|---|
| `get_label_ids` | `(owner, repo) -> dict[str,str]` |
| `ensure_labels_exist` | `(owner, repo, handled_label, escalation_label) -> dict[str,str]` |
| `add_labels` | `(issue_id: str, label_ids: list[str]) -> None` |

## Lifecycle / auth / attributes

| Member | Kind | Notes |
|---|---|---|
| `initialize` | `async () -> None` | fake: no-op |
| `aclose` | `async () -> None` | fake: cleanup temp repos |
| `current_token` | `async () -> str` | dummy token |
| `_current_token` | `async () -> str` (**private, required**) | reached via `hasattr` at `merge_pr.py:102`, `check_board.py:674`, `dispatch_performer.py:539` |
| `org` / `project_name` | properties (`str`) | |
| `_org` / `_project_name` / `project_id` | attribute reads | `check_board.py:99-100,598` |
| `_pr_checks_service_cache` | attribute **write** | node sets it (D4) → fake must be a plain mutable object |
| `post_comment` | `async (issue_number: int, body: str)` | **not on real class**; latent swallowed call at `monitor_performer.py:4244`; fake records + no-ops (research.md D6) |

## Invariants

1. **No behavior change** to the real `GitHubService` — completing the Protocol is
   pure interface capture; the existing suite stays green (FR-002, SC-003).
2. **The fake never raises** — every path degrades to a safe default, because a raise
   aborts `daemon.start()` (research.md D1).
3. **Shape fidelity** — the fake's reads match the real service's shapes for board,
   CI, reviews, mergeability, and merge (SC-004), verified per operation group in
   `tests/unit/test_134_fake_github.py`.
