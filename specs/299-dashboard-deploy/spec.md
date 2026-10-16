# 299: Dashboard availability during deploys

Serve the authenticated dashboard shell and assets before preflight, service
bootstrap, state restoration, board reconciliation and env cache initialization.
State APIs and SSE return 503 with Retry-After: 2 and a starting JSON status until
startup completes. Authentication and origin checks run before the startup gate.

Expose /dashboard/ready on the dashboard listener for Kubernetes routing. Keep
/ready for daemon readiness and /live for liveness. Show starting in the shell,
and retain the rendered state behind a restarting banner during connection loss.
Retry read requests and SSE with capped exponential backoff and jitter. Clear the
banner only after a fresh state snapshot, without reloading the document.

Cache versioned scripts for a year, other assets briefly, and never cache HTML.
Use a five second preStop drain for endpoint propagation, a thirty second pod
grace budget, and a twenty second HTTP request drain. Close SSE streams on SIGTERM.
Probe dashboard readiness every two seconds with no initial delay.

Validate with ASGI startup and authentication tests, node reconnect checks,
rendered Helm assertions and a browser test that restarts the live server on the
same port under an open page. Measure a real rollout separately: this sandbox
cannot bind sockets or run browsers, so no production gap measurement is claimed.
The single writer and ReadWriteOnce StatefulSet remain unchanged.
