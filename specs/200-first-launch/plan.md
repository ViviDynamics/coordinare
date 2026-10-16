# Implementation plan

Use a before-model preset expansion in ProjectConfiguration so file, environment,
dashboard and future init callers share the same behavior. Keep presets opt-in.
Extend doctor through an async shared preflight module; reuse GitHubService's
project lookup and column vocabulary, start_checks and the existing port check.
Use bounded metadata requests and version or protocol health probes.

Keep first-launch presentation in small modules. Thread idle explanations through
the existing per-symphony runtime and snapshot paths and declare browser scope.
Reuse current empty-state markup. Avoid new dependencies and protected-path edits.
