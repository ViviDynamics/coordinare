# Offline stub benchmark runs

Issue #308. Stub board benchmarks must never enable authenticated live git preflight against their production-shaped config. Real benchmark mode must retain its fake-server token and git boundary. No timeout increase or assertion removal is acceptable.

Acceptance: a complete stub optimizer search succeeds with zero SHA-fetch attempts; default fake-service token behavior remains available for real mode; optimizer and build checks pass.
