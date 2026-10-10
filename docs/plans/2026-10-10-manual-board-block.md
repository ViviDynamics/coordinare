# Honor manual board blocks

Issue #12

## Scope
In: Treat a newly observed operator Blocked move as an owned board pause; stop foreground/side workers, preserve PR/feedback identity, and release admission only after confirmed stop. Persist and resume that pause through existing state fields.
Out: Changes to clarification answers, detected-block recovery or approval gates.

## Assumptions
- A Blocked board column with a working local phase and no daemon block reason represents a new external hold.
- Already blocked sessions with questions or a system reason retain their existing recovery path.
- Explicit moves to Todo, In Progress or In Review resume the preserved intent.

## Tasks
- [x] 1. Reproduce a manual block during PR monitoring and active work, with no worker/handoff as applicable.
- [x] 2. Preserve manual pause through stop, restart and explicit resume; retain existing clarification/system block behavior.
- [x] 3. Verify queue admission and unknown stop safety with focused and full tests.
- [ ] 4. Complete adversarial review, configured review and CI gates.
