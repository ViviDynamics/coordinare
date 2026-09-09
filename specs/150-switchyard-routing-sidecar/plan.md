# Issue #204 completion plan

Current upstream v0.2.0 remains a demo server. The gateway is now reachable and serves
ada/qwen3-8b and spark/glm-5.3-flash; replace the retired 20b/120b evaluation pair
with those explicit weak/strong tiers. Deliver an opt-in operator compose service,
pinned image build, routes and an ordinary litellm endpoint example. Defaults stay off.

Add an opt-in upstream session header to the existing self-hosted routing descriptor.
Each per-job shim owns a random stable identity (all turns share it, the next job gets
a new one). Reject direct reroute with this option because it bypasses the header shim.
Health probes deliberately omit the session header, so they cannot pre-escalate a job.
Use the existing 099 completion gate for dispatch failure on a dead sidecar.

Observe durable routing JSONL and judge parse warnings, and compare identical bounded
real tasks via static weak/strong and sidecar routes. Record actual gateway model usage
and limits; recommend keep/port/retire based on evidence. Never load retired models or
bypass LiteLLM. Document session isolation, parse-error alarms, TLS/network isolation
and the upstream maturity warning. No automatic deployment selection.

## Analysis before implementation

The prior spike explicitly deferred production dispatch changes and live comparison
because the gateway was unreachable. #204 now authorizes completing that work. Its F12
requires session identity, which the shim can provide without new backend kinds or
changing CoordinareState. Existing health and proxy transport tests cover the seam.
No new Python dependency. The service is opt-in and not part of main compose defaults.

The public routes example uses operator placeholders; the exact measured routes
are retained in `live/routes.toml`. Set `SWITCHYARD_ROUTES_FILE` to that file's
absolute path to reproduce this experiment. `evaluate.py` uses an isolated real
implementer lifecycle on the tiny-multiply board fixture, with real pytest and
synthetic approval; it is not a full-lifecycle or general quality measurement.
