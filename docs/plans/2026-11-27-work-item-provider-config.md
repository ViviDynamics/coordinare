# Select the work item provider in config, with GitHub Projects as the default

Issue #394 (part of epic #393)

## Scope
In:
- An optional `work_items` block per symphony with a `provider` key. No block
  means GitHub Projects with today's fields; existing configs load, save and run
  unchanged.
- One factory, `services/work_item_providers.build_board_provider`, and every
  construction site calls it: daemon startup in `__main__.py` and the daemon's
  per-symphony swap and restore.
- `coordinare validate` names the field for an unknown provider and prints the
  provider each symphony reads. The dashboard symphony API and detail page show
  the provider; create, update and validate accept the block and refuse an
  unknown name. The MCP symphony tools pass it through and describe it.

Out:
- A second selectable provider. The registry holds only `github_projects`; the
  in-memory provider from #400 stays a test and bench provider. A provider for
  another tracker registers itself here when it exists (#405, #406).
- Making `github_project_number` optional. It is still the GitHub Projects
  board's address, and no other provider is selectable yet.
- Typed work items (#395), routing every write through the provider (#396),
  work item references (#397).

## Assumptions
- The block carries only `provider` for now. GitHub Projects reads its project
  from the symphony's existing `github_project_number`.
- The per-symphony restore puts back the board the swap replaced instead of
  building a new GitHub adapter, so a configured board survives a cycle. When the
  state had no board, it builds the default through the factory, as before.
- The validate text output gains one line per symphony; the JSON check list is
  unchanged, because its check ids are a contract (#399).

## Tasks
- [x] 1. Config block, registry and factory: no block is GitHub Projects, an
  unknown name is refused naming the field.
- [x] 2. Construction sites call the factory; nothing else constructs a provider.
- [x] 3. The daemon's swap builds the symphony's own board and the restore puts
  the previous one back.
- [x] 4. Validate output, dashboard API and page, MCP description.
