# Quickstart: Implementer-Side Rebase

## Configure

On by default. To switch it off:

```yaml
implementer_rebase:
  enabled: false
```

With it off, the daemon neither rebases (that code is gone) nor asks anyone to: branches are updated only when a human or a performer happens to do it.

## Verify

```sh
make lint && make typecheck && make test-all
.venv/bin/pytest agent/performer/tests/unit/test_555_requested_rebase.py   # real git, local bare remote
COORDINARE_GITHUB_TOKEN=placeholder .venv/bin/python -m coordinare config validate --config config.example.yaml
grep -rn "run_rebase_round\|force_push_with_lease\|rebase_branch\|ls-remote" src/coordinare/services/rebase.py src/coordinare/graph src/coordinare/daemon.py   # expect no output
```

Operator check after deploy, for a card whose PR is conflicting or behind:

- daemon: `rebase.triggered ... outcome=delegated` once, then `rebase.performer_dispatched`;
- performer: `workspace.rebase.clean` (or `workspace.rebase.conflict`), then `push_branch.rebased_onto_base`;
- no `rebase.failed` anywhere;
- if the implementer could not resolve it: the card is BLOCKED with "still conflicts with main ... after the implementer was asked to rebase it".

## Recovering the garden card

The wedge clears on upgrade without any config change: the guard no longer runs git, and the feature is on by default, so PR 81's branch is handed to the implementer on the first cycle. The performer image must be upgraded together with the daemon, because the rebase and the lease push live in the performer.

## Audit: other daemon-side paths that run git or touch code (FR-012)

Row 7 is fixed by this feature. The others are tracked as separate issues.

| # | Where | What the daemon does | Mutating | Reached when |
| --- | --- | --- | --- | --- |
| 1 | `workspace.py` `WorkspaceManager.prepare` (`_run_git`: clone, config, remote set-url, fetch, checkout -b) | clones the target repo onto the daemon filesystem at dispatch | local files only, no push | every dispatch through `dispatch_performer._setup_workspace`, except `agent_transport: kubernetes` |
| 2 | `workspace.py` `_resolve_branch` calling `github.delete_branch` (REST `DELETE git/refs/heads/<branch>`) | deletes a pre-existing remote branch that collides with a new card's generated name | yes, a ref | new card with no PR; runs before the kubernetes early return |
| 3 | `graph/nodes/monitor/verdict.py` `_ci_lint_gate` | runs `ci_detection.detect()` on the daemon clone, then executes the repo-derived lint command there | executes repo code | stage advance, when a daemon clone exists |
| 4 | `dispatch_performer._run_security_floor` calling `security_scanner.scan_diff` | runs semgrep and bandit over the daemon clone | read-only | security role without `workflow: security` |
| 5 | `services/persona_classifier._read_project_context` | reads `CLAUDE.md` / `AGENTS.md` from the daemon clone | read-only | `classify_scope` |
| 6 | `transport/subprocess_transport.py` with `agent_service.dispatch_card` | spawns the agent as a daemon child process working in the daemon clone | yes, by design | `agent_transport: subprocess` (the config default) |
| 7 | `services/rebase.fetch_main_sha` | was `git ls-remote` in an empty temp dir; **now a REST read** (`GitHubService.get_branch_head_sha`) | read-only | preflight, `check_board`, pre-dispatch guard |
| 8 | `github.squash_merge`; `wiki_init.try_auto_merge` | merges a PR through the API | yes, by the platform | `merge_pr` node; wiki seed PR |
| 9 | `utils/doc_dedup.merge_duplicate_sections` | rewrites `*.md` under a workspace path | would be | no caller found (dead code) |

Notes:

- For HTTP and Docker performers the container clones for itself (`http_performer_service` sends only `repo_url` and `branch`), so the daemon clone in row 1 serves only rows 3, 4, 5 and 6.
- Row 8 is an orchestration decision carried out by the platform, not a code edit. It is listed for completeness.
- No contents-API write, `createCommitOnBranch`, `updateRef` or update-branch call exists in the daemon.
