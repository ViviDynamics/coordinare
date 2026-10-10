# Detect overdue performers independently of retained siblings

Issue #23

## Scope
In: evaluate each live monitoring session using its own dispatch/phase-entry clock, card identity and cooldown; retain the legacy flat-state detector when no session collection exists.
Out: board routing, worker timeout policy, approval/CI gates and infrastructure changes. Schema32 adds only the optional durable dispatch clock.

## Assumptions
- A live monitoring session has an agent_dispatch session_id. Its durable dispatch clock is preferred; older snapshots use persisted progress as a conservative minimum age or begin timing at first observation.
- The monitoring alert measures phase duration, independently of the dashboard quiet timer and replacement watchdog.
- Paused, terminal and gate-only retained sessions are not live workers.
- A fresh dispatch starts a fresh alert episode; another card never inherits its cooldown.

## Tasks
- [x] 1. Reproduce held-sibling suppression and incorrect aggregate threshold with the real detector.
- [x] 2. Evaluate named live workers independently; test multiple workers, repeats and fresh replacement.
- [x] 3. Preserve legacy detection and disabled settings; verify inactive/gate-only exclusions and notification/feed identity.
- [ ] 4. Execute adversarial review, full preflight and exact-head CI before merge; deploy and replay controlled original interaction.

Fresh Copilot review found that persistent restored workers lack the dispatch clock. Three valid restart regressions and a strict-contract version regression fail before the fix. Schema32 persists/restores the clock, retains older defaults, and uses a conservative fallback when no old clock exists.
