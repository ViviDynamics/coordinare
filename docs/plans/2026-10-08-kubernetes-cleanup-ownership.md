# Scope Kubernetes orphan cleanup to a deployment

Issue #535

## Scope
In: Stable owner labels on performer pods, ownership filters on startup and session sweeps, Helm release ownership, and conservative handling of legacy pods.
Out: Distributed locking between duplicate replicas of one deployment; deployments sharing a namespace must use distinct owner names.

## Assumptions
- An explicit `kubernetes_owner` identifies a deployment across restarts. Helm supplies a stable hash of the release namespace and name automatically.
- Without an owner, cleanup does nothing. Pods without an owner label are preserved; operators can inspect and remove legacy pods separately.
- Existing managed-by, performer and session filters remain required.

## Tasks
- [x] 1. Prove both sweep paths preserve foreign and legacy pods, and fail closed without ownership.
- [x] 2. Label newly created pods, scope pod names and replacement to the owner, and wire configuration through runtime construction and Helm.
- [x] 3. Run relevant Kubernetes/chart tests, full suite, and adversarial review. CI and Copilot review run through the shipping workflow.
