# Persistent config implementation plan

Goal: implement the supplied #284 design in the existing worktree without commits.

1. Write failing seed lifecycle and chart rendering tests. Implement a small seed
   service with atomic YAML writes, hash metadata, conservative drift status,
   explicit reset marker and raw secret policy. Check first boot, repeat boots,
   upgrades, reset, missing metadata and failures with real temporary files.
2. Write failing dashboard tests for drift data and placeholder round trips.
   Add status to the existing Config snapshot and render a safe section summary
   and reset instructions. Keep scope filtering and all existing save checks.
3. Render the chart with persistence enabled, disabled and a custom state path.
   Wire a nonroot seed init container, writable state path and readonly seed mount.
   Document upgrade, reset, backup, rollback, secret and reload semantics in README
   and NOTES. Keep volumeClaimTemplates unchanged.
4. Run touched file lint, strict mypy, ruff ratchet, relevant pytest and node checks.
   Review the full diff against all acceptance items. Run the adoption lifecycle
   through ASGI without sockets. The browser scenario also uses ASGI interception;
   its Chromium launch requires macOS Mach port permission unavailable here.

Review focus: partial writes, lost seed metadata, read only mounts, literal secret
submission and section names containing HTML must fail safely or be escaped.

Review blocker followup: regress the exact garden render, pointer names and real
literal secrets first. Regress boot overlays, image pins by endpoint ID, recovery
and normalized drift. Add selective adoption in a separate small router with
masked previews, confirmation and guards for both seed and live versions. Verify
the chart checksum and document garden migration and offline PVC recovery.
