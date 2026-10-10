# Detect overdue performers independently of retained siblings

Issues #23 and #27

## Scope
In: evaluate each live monitoring session using its own dispatch/phase-entry clock, card identity and cooldown; retain the legacy flat-state detector when no session collection exists.
Out: board routing, worker timeout policy, approval/CI gates and infrastructure changes. Schema32 adds the optional durable dispatch clock and the paired production clock/fingerprint; timeout policy stays intact.

## Assumptions
- A live monitoring session has an agent_dispatch session_id. Its durable dispatch clock is preferred; older snapshots use persisted progress as a conservative minimum age or begin timing at first observation.
- The monitoring alert measures phase duration, independently of the dashboard quiet timer and replacement watchdog.
- Paused, terminal and gate-only retained sessions are not live workers.
- A fresh dispatch starts a fresh alert episode; another card never inherits its cooldown.

## Tasks
- [x] 1. Reproduce held-sibling suppression and incorrect aggregate threshold with the real detector.
- [x] 2. Evaluate named live workers independently; test multiple workers, repeats and fresh replacement.
- [x] 3. Preserve legacy detection and disabled settings; verify inactive/gate-only exclusions and notification/feed identity.
- [x] 4. Preserve production clock/fingerprint through card projection and real JSON restart; test healthy work, repeated tools, no-production timeout and absolute ceiling.
- [ ] 5. Execute adversarial review, full preflight and exact-head CI before merge; deploy and replay controlled original interaction.

Fresh Copilot review found that persistent restored workers lack the dispatch clock. Three valid restart regressions and a strict-contract version regression fail before the fix. Schema32 persists/restores the clock, retains older defaults, and uses a conservative fallback when no old clock exists.

Independent adversarial review reproduced dispatch restoration causing a healthy worker timeout before polling. The existing card projection also omitted production evidence and leaked it into a fresh sibling (#27). Paired production clock/fingerprint now round-trip through sessions and schema32, preserving current timeout semantics. Two failing actual regressions and strict contract reproduction precede the fix.
