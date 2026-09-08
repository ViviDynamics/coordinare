# Contract: the card-less dispatch

Both roles are started the way the wiki seeding run is started. This is the
shape the daemon must build and the guarantees the two sides owe each other.

## What the daemon sends

A `card_context` dict, hand-built, plus a synthesised `WorkspaceInfo`.

| Key | Value | Why |
|---|---|---|
| `id` | `f"advocate-{symphony}"` / `f"curator-{symphony}"` | synthetic, and the key MUST be `id`. The transport reads `card_context["id"]` (`http_performer_service.py:823,1026`) and nothing in coordinare reads `card_id`, so the wiki-init precedent's `card_id` is dropped and reaches the wire as an empty string. An empty id is legal, but a stable synthetic one makes the container label and the logs readable |
| `role` | `"advocate"` / `"curator"` | must match the branch added to the performer's status handling |
| `workflow` | `"advocate"` / `"curator"` | selects the workflow adapter. Non-empty, always: there is no prose path |
| `repo_url` | `https://github.com/{org}/{repo}.git` | validated by `Score`; drives the clone |
| `branch` | `f"advocate/{sanitised}"` / `f"curator/{sanitised}"` | never pushed. A branch that does not exist remotely becomes a local checkout off the default branch, which is what the run wants |
| `base_branch` | the repository default | |
| `title` | a fixed human-readable string | `Score.title` is mandatory |
| `description` | what the run is for | |
| `persona_instructions` | the role's effective persona | reaches the model as instruction, never inside documentation |
| `backend` | resolved for the role | |
| `project_id` | the board node id | curator only; empty means it cannot add and must say so |
| model block | as resolved | spread in, as every other dispatch does |

```python
workspace_info = WorkspaceInfo(
    path=None,                 # the performer self-clones
    branch=card_context["branch"],
    repo_url=card_context["repo_url"],
    github_token=token,        # required: a card-less dispatch never
)                              # flows through WorkspaceManager.prepare()
```

`path=None` is a first-class shape, not a workaround: the workspace-completeness
check requires a token only when a path is present, and requires `repo_url` and
`branch` either way.

## Ordering the daemon must observe

```python
state.<role>_in_flight = True          # BEFORE the dispatch, never after
try:
    result = await svc.dispatch_card(card_context, workspace_info=workspace_info)
except Exception as exc:
    state.<role>_in_flight = False     # a synchronous failure must not wedge the role
    register_failure(state, str(exc))
```

Setting the marker after the call clobbers the reset that a synchronous failure
performs, and the role never runs again. This is a bug the existing card-less
paths already hit and documented.

## What comes back

`dispatch_card` returns a dict carrying `session_id` and `container_id`. The
daemon starts one poll task per run, polling `svc.check_status(session_id)`
until the status is terminal, then calls the completion handler.

The completion handler MUST:

1. clear the in-flight marker,
2. write the outcome onto the persisted per-role fields,
3. reset the attempt counter on success, or increment it and trip the breaker at
   the bound on failure,
4. call `snapshot_save_fn`, because a card-less completion moves no lifecycle
   signature and the gated save would otherwise defer it past a restart.

## What the performer owes

- Its own terminal status, `advocate_complete` or `curation_complete`, added to
  the closed status literal so the job's poll loop terminates.
- Its own branch in the status handler, placed **before** the shared tail that
  lints, pushes and opens a pull request.
- No commit, no push, no pull request, evidenced by the executed
  `git status --porcelain` carried on the record.
- A report under the key `advocate` or `curation`, matching the schema beside
  this file. A report that does not match takes the error path; it must never
  fall through to the prose handling.

## Registry

`base_branch` is a live `card_context` key that predates this feature and is
absent from the registry, as `doc_mode` is. Neither is introduced here, so
neither is this feature's to fix, but both are named so the omission is a known
gap rather than a discovery.

`Score.project_id` is a new field on the coordinare to performer dispatch
payload, so it must be added to the field registry in
`specs/contracts/dispatch-payload.md`. A field the dispatch injects but `Score`
does not declare is dropped silently, and a field `Score` declares but the
registry does not record is how that class of bug goes unnoticed.
