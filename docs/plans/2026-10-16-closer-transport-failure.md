# Closer role silently skipped on transport build failure, leaving garden with no closer

Issue #334

## Scope
In: Fail startup when an enabled closer's configured wire-protocol transport cannot be built. Add regression tests in `tests/unit/test_334_closer_transport_failure.py`, change `_build_performer_services` in `src/coordinare/__main__.py`, and document the startup policy in `docs/onboarding/05-configuration.md`.
Out: Live garden configuration, cluster state, new transport implementations, and a configurable required-role catalog. Those changes are unnecessary to prevent this silent closer failure.

## Assumptions
- The closer is lifecycle-critical. A configured, enabled closer's transport build failure is fatal at boot: emit an error event with role, stage, transport, and cause, then raise `ValueError` chained from the original exception. A warning or dashboard condition could still permit work that cannot finish.
- Conservative scope means only the closer becomes required by this policy. Other roles retain the existing warning and skip or reduced-concurrency behavior.
- An absent closer, an explicitly disabled closer (`max_concurrency: 0`), and an HTTP-only closer without a configured wire-protocol transport retain their existing behavior. This policy handles build failures, rather than requiring every deployment to configure a closer.
- A configured broken closer transport fails even if HTTP coverage could later exist. Operators must remove the broken wire-protocol configuration instead of relying on fallback to hide it.

## Tasks
- [x] 1. Add failing regression tests for the real SSH and Kubernetes transport failures on an enabled closer. Assert fatal error, original cause, and error-level diagnostic context. Run with pytest and observe the current silent return.
- [x] 2. Add the minimal closer-specific fatal branch before the existing skip handler. Verify enabled subprocess closer, disabled closer, absent closer, HTTP-only closer, other-role skips, and partial concurrency remain compatible. Run the new file plus bootstrap and main coverage suites.
- [x] 3. Document the required-role policy and remediation.

## Shipping validation
Run house-rule checks, full tests, preflight, and quality guard before pushing. Shipping stages are recorded in the issue checkpoint through PR creation, required CI, Copilot and adversarial review, and squash merge.
