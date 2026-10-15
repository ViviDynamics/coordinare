# Plan

Keep the persona API contract and global scope. Add personas.js with a shared selector, safe Markdown renderer and editor, loaded before config.js. Keep inactive editors mounted so shared Config dirty tracking and conflict restoration retain drafts. Render the read only page with the same selector and renderer. Scope outer Config tab queries so nested persona tabs do not interfere.

Use an escaped Markdown subset with explicit safe link protocols and no raw HTML or image loading. Add theme token styles in shell.css. Route persona persistence through config_write_service.atomic_write_yaml, retaining role and length validation. Preserve line endings for an untouched textarea through an exact source baseline.

Write Node and API regression tests first, then browser tests. Analyze spec, plan and tasks before implementation. Run focused tests, lint, mypy and the ruff ratchet. Leave browser and full suite execution to the operator.
