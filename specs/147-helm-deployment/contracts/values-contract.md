# Contract: The Values Surface

**Feature**: 147-helm-deployment | **Enforces**: FR-005, FR-006, FR-008, FR-010, FR-013, FR-016

A chart's values are its public API. Renaming one breaks every operator's values file silently at
upgrade time, so this records what the chart promises.

## Promises

1. **Nothing in `values.yaml` is a secret.** Credentials arrive through `existingSecret` or the
   chart's own Secret, never as a value. A values file is the thing operators commit to git.
2. **`dashboard.trustedHosts` is visible and narrow.** The chart seeds it with exactly the
   release's in-cluster Service DNS name. It is never a wildcard, never a bind-all address, and
   never populated from a template the operator cannot see with `helm show values`. This entry
   widens a control spec 144 deliberately left opt-in; being legible about it is the condition on
   which that widening is acceptable.
3. **Defaults are the safe posture.** No Ingress, no metrics exposure, nothing reachable from
   outside the cluster, a pinned image tag, one replica.
4. **Optional means optional.** `performers.cacheClaim` empty must install successfully on a
   cluster with no StorageClass. A chart that requires a PVC nobody can provision does not run on
   the small clusters this is meant to support.
5. **`replicaCount` is a correctness constraint, not a tuning knob.** It is exposed so the value is
   discoverable and its guard explicable, not because more than one is supported.

## Compatibility

Values may be **added** freely. **Removing or renaming** one is a breaking change requiring a
chart major-version bump and a note in the chart README. A value's *default* may tighten only when
the looser default was a security weakness; any other default change is breaking.

## Not promised

Template file names, helper names, and rendered object names beyond those a `Service` DNS entry
depends on. Operators depend on values, not on the templates that consume them.
