# Resume feedback on the existing PR head

Issue #10

## Scope
In: Resolve a retained PR's current head branch before preparing its workspace; preserve prior commits on local and Kubernetes transports; fail safely if its head cannot be resolved.
Out: Branch naming for new cards, review routing, and merge approval policies.

## Assumptions
- A retained PR node ID is the authoritative identity; title and generated branch prefixes are not.
- Existing PR branches must bypass stale-branch deletion and suffix strategies.
- An unavailable or empty head must stop setup before worker creation.

## Tasks
- [x] 1. Reproduce custom and legacy branch misrouting, including renamed cards and unavailable heads.
- [x] 2. Resolve the current PR head without stale-branch cleanup; keep new-card behavior.
- [x] 3. Verify a real local clone preserves existing PR commits and pushes back to the same branch.
- [ ] 4. Run focused and full preflight, adversarial review, configured review and CI gates.
