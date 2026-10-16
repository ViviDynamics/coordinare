# Tasks

- [x] T001 Write and run failing tests for generation, prompts and discovery.
- [x] T002 Implement concern-specific setup helpers and CLI dispatch.
- [x] T003 Verify failures, safe reruns, environment loading and installed data.
- [x] T004 Run checks, review the diff and write the requested PR body.

Verification: 362 selected cases passed, including 47 init cases. Touched-file
ruff, mypy, the ruff ratchet and the first-launch node checks exited 0. Independent
adversarial review findings were reproduced as failing tests and corrected. A
wheel build remains an operator check because hatchling is absent locally; resource
loading and inclusion metadata were tested. Full-suite, live GitHub/harness and
browser checks remain with the operator as requested.
