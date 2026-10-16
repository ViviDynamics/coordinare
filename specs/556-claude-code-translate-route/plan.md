# Plan

Reuse `_launch_dispatch_proxy` -> `RoutingTable` -> `maybe_launch_proxy` ->
`SelfHostedShim` with `strategy: translate`. Spec 080 resolves model catalogs;
spec 122's gateway routing does not replace this explicit transport override.
The SSE filter already reads content independently of reasoning and opens text
blocks lazily. Preserve that behavior and state the invariant beside the code.

Write regressions first for the exact delta sequence and real config dispatch
selection, default no-op, unmatched models, existing Anthropic routes, auth,
path joining and environment restoration. Mock only network/server startup.
Then update examples and references, run validation, and record deployment
instructions and limitations. No dispatch schema changes are required.
