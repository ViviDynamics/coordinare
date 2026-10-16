# Plan

Reuse launch_preflight and config_validation. Add a small setup_reports module
with curated messages and fixes so arbitrary input cannot enter JSON. Extend
existing CLI parsers and handlers. Define report and answers schemas in docs.
Use existing pytest and jsonschema dependencies, without changing CI gates.

Write regression tests first, run them red, implement, then run related setup
tests, lint, mypy and the ruff ratchet. No daemon launch or external writes.
