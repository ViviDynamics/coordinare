# Plan

Add a small validated nested model to `models/performer_endpoint.py`. Keep the
mount directory and destination filename fixed to avoid path validation and
mounting over the whole `/etc/coordinare` tree. Project only the chosen key.

In `services/kubernetes_runtime.py:build_pod_manifest`, copy the supplied env,
inject the routing path when configured, and append the volume and mount alongside
the optional cache. Preserve minimal duck-typed configs used by existing tests.
Do not change the Docker runtime, lifecycle, job payload or Helm RBAC.

Tests first: validation, defaults, per-endpoint isolation, env precedence, cache
coexistence, fake API dispatch and Helm config passthrough. Then implementation,
docs and required checks. Review the full local diff. No network or commits.
